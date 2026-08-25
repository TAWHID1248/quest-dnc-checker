import json
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Payment
from .paypal_utils import PayPalError, capture_order, create_order, verify_webhook_signature
from .services import grant_paypal_credits

logger = logging.getLogger(__name__)

# ── Pricing tiers ────────────────────────────────────────────────────────────

PRICING_TIERS = [
    {
        'name': 'Starter',
        'price': 10,
        'credits': 100_000,
        'credits_display': '100,000',
        'per_k': '$0.10',
        'highlighted': False,
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Email Support',
        ],
    },
    {
        'name': 'Professional',
        'price': 20,
        'credits': 250_000,
        'credits_display': '250,000',
        'per_k': '$0.08',
        'highlighted': True,
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Priority Email Support',
            'Bulk Processing',
        ],
    },
    {
        'name': 'Enterprise',
        'price': 50,
        'credits': 1_000_000,
        'credits_display': '1,000,000',
        'per_k': '$0.05',
        'highlighted': False,
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Dedicated Support',
            'API Access (coming soon)',
            'Volume Discounts',
        ],
    },
]

_TIER_MAP = {t['name'].lower(): t for t in PRICING_TIERS}


# ── Billing home ─────────────────────────────────────────────────────────────

@login_required
def billing_home(request):
    recent_payments = Payment.objects.filter(user=request.user).order_by('-created_at')[:10]
    return render(request, 'billing/home.html', {
        'tiers': PRICING_TIERS,
        'recent_payments': recent_payments,
        'paypal_client_id': settings.PAYPAL_CLIENT_ID,
    })


# ── PayPal: create order (AJAX) ──────────────────────────────────────────────

@login_required
@require_POST
def create_paypal_order_view(request):
    """Called by the PayPal button's createOrder callback. Returns {order_id}."""
    try:
        data = json.loads(request.body)
        tier_name = data.get('tier', '').lower()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    tier = _TIER_MAP.get(tier_name)
    if not tier:
        return JsonResponse({'error': 'Unknown pricing tier.'}, status=400)

    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        return JsonResponse({'error': 'PayPal is not configured.'}, status=503)

    try:
        order = create_order(
            amount_usd=tier['price'],
            tier_name=tier['name'],
            credits=tier['credits'],
            user_id=request.user.pk,
        )
    except PayPalError as exc:
        logger.exception('PayPal order creation failed for user %s', request.user.pk)
        return JsonResponse({'error': str(exc)}, status=502)

    return JsonResponse({'order_id': order['id']})


# ── PayPal: capture order (AJAX) ─────────────────────────────────────────────

@login_required
@require_POST
def capture_paypal_order_view(request):
    """
    Called by the PayPal button's onApprove callback.  Captures the order
    server-side, verifies it belongs to this user, then credits the account.
    The webhook remains as a fallback — both paths are idempotent via the
    unique paypal_order_id.
    """
    try:
        data = json.loads(request.body)
        order_id = data.get('order_id', '').strip()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    if not order_id:
        return JsonResponse({'error': 'Missing order_id.'}, status=400)

    try:
        order = capture_order(order_id)
    except PayPalError as exc:
        logger.exception('PayPal capture failed for order %s', order_id)
        return JsonResponse({'error': str(exc)}, status=502)

    try:
        unit = order['purchase_units'][0]
        capture = unit['payments']['captures'][0]
    except (KeyError, IndexError):
        return JsonResponse({'error': 'Payment not completed yet.'}, status=402)

    custom_id = capture.get('custom_id') or unit.get('custom_id') or ''
    try:
        user_id, credits, _tier = custom_id.split(':', 2)
        user_id, credits = int(user_id), int(credits)
    except ValueError:
        logger.error('PayPal order %s has malformed custom_id %r', order_id, custom_id)
        return JsonResponse({'error': 'Order metadata invalid. Contact support.'}, status=400)

    if user_id != request.user.pk:
        logger.warning('PayPal order %s user_id=%s != request user %s',
                       order_id, user_id, request.user.pk)
        return JsonResponse({'error': 'Payment does not belong to your account.'}, status=403)

    if capture.get('status') != 'COMPLETED':
        # e.g. an eCheck still clearing — the webhook will credit when it completes
        return JsonResponse({
            'error': f"Payment is {capture.get('status', 'processing').lower()} — "
                     'credits will be added automatically once it clears.',
        }, status=402)

    try:
        amount = Decimal(capture['amount']['value'])
    except (KeyError, InvalidOperation):
        logger.error('PayPal order %s capture has no valid amount', order_id)
        return JsonResponse({'error': 'Order amount invalid. Contact support.'}, status=400)

    grant_paypal_credits(order['id'], user_id, credits, amount)

    request.user.refresh_from_db()
    return JsonResponse({'ok': True, 'credits': request.user.credits})


# ── PayPal webhook ───────────────────────────────────────────────────────────

@csrf_exempt
def paypal_webhook(request):
    """
    Fallback credit-delivery path.  Signature-verified against PAYPAL_WEBHOOK_ID
    via PayPal's verification API — unverifiable deliveries are rejected.
    """
    if request.method != 'POST':
        return HttpResponse(status=405)

    try:
        event = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if not settings.PAYPAL_WEBHOOK_ID:
        logger.error('PAYPAL_WEBHOOK_ID not set — rejecting PayPal webhook')
        return HttpResponse(status=400)

    try:
        if not verify_webhook_signature(request.headers, event):
            logger.warning('PayPal webhook signature verification failed')
            return HttpResponse(status=400)
    except PayPalError:
        logger.exception('PayPal webhook verification errored')
        return HttpResponse(status=400)

    event_type = event.get('event_type', '')
    logger.info('PayPal webhook received: %s', event_type)

    try:
        if event_type == 'PAYMENT.CAPTURE.COMPLETED':
            resource = event.get('resource', {})
            order_id = (resource.get('supplementary_data', {})
                                .get('related_ids', {})
                                .get('order_id'))
            custom_id = resource.get('custom_id', '')
            try:
                user_id, credits, _tier = custom_id.split(':', 2)
                user_id, credits = int(user_id), int(credits)
                amount = Decimal(resource['amount']['value'])
            except (ValueError, KeyError, InvalidOperation):
                logger.error('PayPal capture webhook missing/malformed data (order %s, custom_id %r)',
                             order_id, custom_id)
                return HttpResponse(status=200)

            if order_id:
                grant_paypal_credits(order_id, user_id, credits, amount)
            else:
                logger.error('PayPal capture webhook has no order_id (capture %s)', resource.get('id'))
        else:
            logger.debug('Unhandled PayPal event: %s', event_type)
    except Exception:
        # Log internally; return 200 so PayPal stops retrying a poisoned event
        logger.exception('Error handling PayPal event %s', event_type)

    return HttpResponse(status=200)
