import json
import logging

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Payment
from .services import fulfil_checkout_session
from .stripe_utils import (
    construct_webhook_event,
    create_checkout_session,
    retrieve_checkout_session,
    stripe_enabled,
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

# ── Billing home ─────────────────────────────────────────────────────────────

def _get_tier(name):
    return next((t for t in PRICING_TIERS if t['name'] == name), None)


@login_required
def billing_home(request):
    recent_payments = Payment.objects.filter(user=request.user).order_by('-created_at')[:10]
    return render(request, 'billing/home.html', {
        'tiers': PRICING_TIERS,
        'recent_payments': recent_payments,
        'stripe_enabled': stripe_enabled(),
    })


# ── Stripe Checkout ──────────────────────────────────────────────────────────

@login_required
@require_POST
def create_checkout(request):
    """Create a hosted Checkout Session for the chosen tier and redirect to it."""
    if not stripe_enabled():
        messages.error(request, 'Card payments are not available right now. Please contact us on WhatsApp.')
        return redirect('billing:home')

    tier = _get_tier(request.POST.get('tier', ''))
    if tier is None:
        return HttpResponseBadRequest('Unknown pricing tier')

    success_url = request.build_absolute_uri(reverse('billing:checkout_success')) + '?session_id={CHECKOUT_SESSION_ID}'
    cancel_url = request.build_absolute_uri(reverse('billing:checkout_cancel'))
    try:
        session = create_checkout_session(request.user, tier, success_url, cancel_url)
    except stripe.error.StripeError as exc:
        logger.exception("Stripe checkout session creation failed for user %s", request.user.pk)
        messages.error(request, f'Could not start card checkout: {exc.user_message or "Stripe error"}. Please try again or contact us.')
        return redirect('billing:home')

    return redirect(session.url, permanent=False)


@login_required
def checkout_success(request):
    """
    Landing page after Stripe Checkout. The webhook normally grants credits
    first; this view re-checks the session so the user sees their balance
    immediately even if the webhook is delayed (fulfilment is idempotent).
    """
    session_id = request.GET.get('session_id', '')
    if session_id and stripe_enabled():
        try:
            session = retrieve_checkout_session(session_id)
        except stripe.error.StripeError:
            logger.exception("Could not retrieve checkout session %s", session_id)
            session = None

        if session is not None:
            if str(session.get('client_reference_id')) != str(request.user.pk):
                logger.warning("User %s opened success page for session %s belonging to another user",
                               request.user.pk, session_id)
                return redirect('billing:home')
            payment = fulfil_checkout_session(session)
            if payment:
                request.user.refresh_from_db(fields=['credits'])
                messages.success(
                    request,
                    f'Payment received — {payment.credits:,.0f} credits added. '
                    f'Your balance is now {request.user.credits:,.0f} credits.',
                )
                return redirect('billing:home')

    messages.info(request, 'Payment is being confirmed. Your credits will appear within a minute.')
    return redirect('billing:home')


@login_required
def checkout_cancel(request):
    messages.warning(request, 'Card checkout was cancelled. No charge was made.')
    return redirect('billing:home')


# ── Stripe webhook ───────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def stripe_webhook(request):
    """
    Stripe → app. Signature-verified with STRIPE_WEBHOOK_SECRET.
    Always answers 200 for handled/ignored events so Stripe stops retrying;
    400 only for bad signatures or malformed payloads.
    """
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')

    if not settings.STRIPE_WEBHOOK_SECRET:
        logger.error("STRIPE_WEBHOOK_SECRET not set; rejecting webhook")
        return HttpResponse(status=400)

    try:
        event = construct_webhook_event(payload, sig_header)
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook signature verification failed")
        return HttpResponse(status=400)
    except (ValueError, json.JSONDecodeError):
        return HttpResponse(status=400)

    event_type = event['type']
    data_obj = event['data']['object']
    logger.info("Stripe webhook received: %s", event_type)

    try:
        if event_type in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            fulfil_checkout_session(data_obj)
        elif event_type == 'checkout.session.async_payment_failed':
            logger.warning("Checkout session %s payment failed", data_obj.get('id'))
        else:
            logger.debug("Unhandled Stripe event: %s", event_type)
    except Exception:
        logger.exception("Error handling Stripe event %s", event_type)

    return JsonResponse({'received': True})
