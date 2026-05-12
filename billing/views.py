import json
import logging

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Payment, PaymentMethod
from .stripe_utils import (
    create_payment_intent,
    create_setup_intent,
    detach_payment_method,
    get_or_create_customer,
)
from .webhooks import (
    handle_payment_intent_failed,
    handle_payment_intent_succeeded,
    handle_setup_intent_succeeded,
)

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
def stripe_config(request):
    """Return Stripe publishable key as JSON — avoids template caching issues."""
    return JsonResponse({'publishable_key': settings.STRIPE_PUBLISHABLE_KEY})


@login_required
def billing_home(request):
    recent_payments = Payment.objects.filter(user=request.user).order_by('-created_at')[:10]
    return render(request, 'billing/home.html', {
        'tiers': PRICING_TIERS,
        'recent_payments': recent_payments,
        'stripe_pk': settings.STRIPE_PUBLISHABLE_KEY,
    })


# ── Create PaymentIntent (AJAX) ───────────────────────────────────────────────

@login_required
@require_POST
def create_payment_intent_view(request):
    """
    Called by the frontend to get a client_secret before confirming payment.
    Returns JSON: {client_secret, payment_intent_id, publishable_key}
    """
    try:
        data      = json.loads(request.body)
        tier_name = data.get('tier', '').lower()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    tier = _TIER_MAP.get(tier_name)
    if not tier:
        return JsonResponse({'error': 'Unknown pricing tier.'}, status=400)

    try:
        customer = get_or_create_customer(request.user)
        pi = create_payment_intent(
            amount_usd=tier['price'],
            customer_id=customer.id,
            user_id=request.user.pk,
            tier_name=tier['name'],
            credits=tier['credits'],
        )
        return JsonResponse({
            'client_secret':      pi.client_secret,
            'payment_intent_id':  pi.id,
            'publishable_key':    settings.STRIPE_PUBLISHABLE_KEY,
        })
    except stripe.error.StripeError as exc:
        logger.exception("Stripe error creating PaymentIntent for user %s", request.user.pk)
        return JsonResponse({'error': str(exc.user_message)}, status=502)
    except Exception as exc:
        logger.exception("Unexpected error creating PaymentIntent")
        return JsonResponse({'error': 'An unexpected error occurred.'}, status=500)


# ── Create SetupIntent (AJAX) ─────────────────────────────────────────────────

@login_required
@require_POST
def create_setup_intent_view(request):
    """Return a SetupIntent client_secret for saving a card without charging."""
    try:
        customer = get_or_create_customer(request.user)
        si = create_setup_intent(customer.id)
        return JsonResponse({
            'client_secret':   si.client_secret,
            'publishable_key': settings.STRIPE_PUBLISHABLE_KEY,
        })
    except stripe.error.StripeError as exc:
        logger.exception("Stripe error creating SetupIntent for user %s", request.user.pk)
        return JsonResponse({'error': str(exc.user_message)}, status=502)
    except Exception:
        logger.exception("Unexpected error creating SetupIntent")
        return JsonResponse({'error': 'An unexpected error occurred.'}, status=500)


# ── Delete payment method ─────────────────────────────────────────────────────

@login_required
@require_POST
def delete_payment_method(request, pm_id):
    """Detach from Stripe and delete the local record."""
    pm = get_object_or_404(PaymentMethod, pk=pm_id, user=request.user)

    try:
        detach_payment_method(pm.stripe_pm_id)
    except stripe.error.StripeError as exc:
        logger.warning("Stripe detach failed for %s: %s", pm.stripe_pm_id, exc)
        # Still delete locally — card may already be detached on Stripe's side

    was_default = pm.is_default
    pm.delete()

    # Promote the next card to default if the deleted one was default
    if was_default:
        next_pm = PaymentMethod.objects.filter(user=request.user).first()
        if next_pm:
            next_pm.is_default = True
            next_pm.save(update_fields=['is_default'])

    messages.success(request, 'Payment method removed.')
    return redirect('/accounts/profile/?tab=payments')


# ── Set default payment method ────────────────────────────────────────────────

@login_required
@require_POST
def set_default_payment_method(request, pm_id):
    pm = get_object_or_404(PaymentMethod, pk=pm_id, user=request.user)
    PaymentMethod.objects.filter(user=request.user, is_default=True).update(is_default=False)
    pm.is_default = True
    pm.save(update_fields=['is_default'])
    messages.success(request, f'Card ending in {pm.last4} set as default.')
    return redirect('/accounts/profile/?tab=payments')


# ── Payment complete (frontend callback after confirmCardPayment) ─────────────

@login_required
@require_POST
def payment_complete(request):
    """
    Called by the frontend immediately after stripe.confirmCardPayment() succeeds.
    Retrieves the PaymentIntent from Stripe to verify status, then credits the user.

    This is the primary credit-delivery path for local dev (where the Stripe CLI
    webhook forwarder may not be running).  The webhook handler is kept as a
    belt-and-suspenders fallback — both are idempotent via the stripe_pi_id
    unique constraint on Payment.

    Always returns JSON — never HTML — so the frontend can parse the response.
    """
    try:
        # ── Parse request ────────────────────────────────────────────────
        try:
            data = json.loads(request.body)
            pi_id = data.get('payment_intent_id', '').strip()
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse({'error': 'Invalid request body.'}, status=400)

        if not pi_id or not pi_id.startswith('pi_'):
            return JsonResponse({'error': 'Invalid payment_intent_id.'}, status=400)

        # ── Retrieve & verify PaymentIntent from Stripe ──────────────────
        try:
            pi = stripe.PaymentIntent.retrieve(pi_id)
        except stripe.error.StripeError as exc:
            logger.exception("Could not retrieve PaymentIntent %s", pi_id)
            msg = getattr(exc, 'user_message', None) or str(exc)
            return JsonResponse({'error': msg}, status=502)

        import json as _json
        pi_dict = _json.loads(str(pi))   # normalise StripeObject → plain dict

        if pi_dict.get('status') != 'succeeded':
            return JsonResponse(
                {'error': f"Payment not completed (status: {pi_dict.get('status')})."},
                status=402,
            )

        # ── Verify the PI belongs to this user ───────────────────────────
        user_id_meta = pi_dict.get('metadata', {}).get('user_id')
        if str(user_id_meta) != str(request.user.pk):
            logger.warning(
                "payment_complete: PI %s metadata user_id=%s != request user %s",
                pi_id, user_id_meta, request.user.pk,
            )
            return JsonResponse({'error': 'Payment does not belong to your account.'}, status=403)

        # ── Credit the account (idempotent) ─────────────────────────────
        handle_payment_intent_succeeded(pi_dict)

        # ── Return updated balance ───────────────────────────────────────
        request.user.refresh_from_db()
        return JsonResponse({'ok': True, 'credits': request.user.credits})

    except Exception:
        logger.exception("Unhandled error in payment_complete for user %s", request.user.pk)
        return JsonResponse({'error': 'An unexpected error occurred. Contact support.'}, status=500)


# ── Stripe Webhook ────────────────────────────────────────────────────────────

@csrf_exempt
def stripe_webhook(request):
    """
    Receives and verifies Stripe webhook events.
    Must be excluded from CSRF middleware (Stripe signs requests itself).
    """
    payload    = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')

    if not settings.STRIPE_WEBHOOK_SECRET:
        logger.warning("STRIPE_WEBHOOK_SECRET not set — skipping signature verification")
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return HttpResponse(status=400)
    else:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError:
            logger.warning("Stripe webhook signature verification failed")
            return HttpResponse(status=400)
        except Exception:
            return HttpResponse(status=400)

    event_type = event['type']
    data_obj   = event['data']['object']

    logger.info("Stripe webhook received: %s", event_type)

    try:
        if event_type == 'payment_intent.succeeded':
            handle_payment_intent_succeeded(data_obj)

        elif event_type == 'payment_intent.payment_failed':
            handle_payment_intent_failed(data_obj)

        elif event_type == 'setup_intent.succeeded':
            handle_setup_intent_succeeded(data_obj)

        else:
            logger.debug("Unhandled Stripe event: %s", event_type)

    except Exception:
        logger.exception("Error handling Stripe event %s", event_type)
        # Return 200 so Stripe doesn't keep retrying — we log internally
        return HttpResponse(status=200)

    return HttpResponse(status=200)
