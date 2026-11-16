import json
import logging
from datetime import timedelta

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .checkoutpage_utils import (
    CheckoutPageError,
    build_checkout_url,
    checkoutpage_enabled,
    retrieve_payment,
    verify_webhook_signature,
)
from .models import Invoice, Payment
from .pdf import render_invoice_pdf
from .services import fulfil_checkout_session, fulfil_checkoutpage_payment
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
    recent_payments = (
        Payment.objects.filter(user=request.user)
        .select_related('invoice')
        .order_by('-created_at')[:10]
    )
    return render(request, 'billing/home.html', {
        'tiers': PRICING_TIERS,
        'recent_payments': recent_payments,
        'card_enabled': checkoutpage_enabled() or stripe_enabled(),
    })


# ── Invoices ─────────────────────────────────────────────────────────────────

@login_required
def invoice_list(request):
    invoices = Invoice.objects.filter(user=request.user).select_related('payment').order_by('-created_at')
    return render(request, 'billing/invoices.html', {'invoices': invoices})


@login_required
def invoice_pdf(request, invoice_number):
    """
    Stream the invoice as a PDF. Owners can always download their own invoices;
    staff can download any (linked from the admin payments page).
    """
    qs = Invoice.objects.select_related('user', 'payment')
    if not getattr(request.user, 'is_staff_member', False):
        qs = qs.filter(user=request.user)
    invoice = get_object_or_404(qs, invoice_number=invoice_number)

    pdf = render_invoice_pdf(invoice)
    disposition = 'inline' if request.GET.get('view') else 'attachment'
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'{disposition}; filename="{invoice.pdf_filename}"'
    response['Content-Length'] = len(pdf)
    return response


# ── Card checkout (Checkout Page, or Stripe when Checkout Page is not set up) ─

@login_required
@require_POST
def create_checkout(request):
    """
    Send the buyer to the hosted checkout for the chosen tier.

    Checkout Page is used when it is configured; otherwise this falls back to
    a Stripe hosted Checkout Session.
    """
    tier = _get_tier(request.POST.get('tier', ''))
    if tier is None:
        return HttpResponseBadRequest('Unknown pricing tier')

    if checkoutpage_enabled():
        url = build_checkout_url(request.user, tier)
        if url is None:
            logger.error("No Checkout Page URL configured for tier %s", tier['name'])
            messages.error(request, f"Card payment for the {tier['name']} plan is not available right now. "
                                    'Please contact us on WhatsApp.')
            return redirect('billing:home')
        return redirect(url, permanent=False)

    if not stripe_enabled():
        messages.error(request, 'Card payments are not available right now. Please contact us on WhatsApp.')
        return redirect('billing:home')

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
    Landing page after a card payment.

    Checkout Page redirects here once the buyer has paid; credits are granted
    by its webhook, so this only reports whether that has landed yet.

    Stripe sends `?session_id=`; the webhook normally grants credits first and
    this view re-checks the session so the user sees their balance immediately
    even if the webhook is delayed (fulfilment is idempotent).
    """
    session_id = request.GET.get('session_id', '')
    if not session_id:
        recent = Payment.objects.filter(
            user=request.user,
            provider=Payment.Provider.CHECKOUT_PAGE,
            status=Payment.Status.COMPLETED,
            created_at__gte=timezone.now() - timedelta(minutes=15),
        ).first()
        if recent:
            request.user.refresh_from_db(fields=['credits'])
            messages.success(
                request,
                f'Payment received — {recent.credits:,.0f} credits added. '
                f'Your balance is now {request.user.credits:,.0f} credits.',
            )
            return redirect('billing:home')
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


# ── Checkout Page webhook ────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def checkoutpage_webhook(request):
    """
    Checkout Page → app (event: payment.paid). Signature-verified with
    CHECKOUTPAGE_WEBHOOK_SECRET against the raw body.

    The webhook body is only used to learn the payment id; the payment itself
    is re-fetched from the Checkout Page API before any credits are granted.
    Answers 200 for handled/ignored events, 401 for a bad signature, and 503
    when the API cannot be reached so Checkout Page retries the delivery.
    """
    payload = request.body
    if not settings.CHECKOUTPAGE_WEBHOOK_SECRET:
        logger.error("CHECKOUTPAGE_WEBHOOK_SECRET not set; rejecting webhook")
        return HttpResponse(status=400)

    if not verify_webhook_signature(payload, request.META.get('HTTP_X_WEBHOOK_SIGNATURE', '')):
        logger.warning("Checkout Page webhook signature verification failed")
        return HttpResponse(status=401)

    try:
        event = json.loads(payload)
    except ValueError:
        return HttpResponse(status=400)
    if not isinstance(event, dict):
        return HttpResponse(status=400)

    event_type = event.get('event', '')
    logger.info("Checkout Page webhook received: %s (%s)", event_type, event.get('eventId'))

    if event_type != 'payment.paid':
        return JsonResponse({'received': True})

    data = event.get('data') or {}
    payment_id = ((data.get('payment') or {}).get('id') or data.get('id') or '')
    if not payment_id:
        logger.error("Checkout Page payment.paid event without a payment id")
        return JsonResponse({'received': True})

    try:
        payment = retrieve_payment(payment_id)
    except CheckoutPageError:
        logger.exception("Could not fetch Checkout Page payment %s", payment_id)
        return HttpResponse(status=503)

    if payment is None:
        logger.error("Checkout Page payment %s not found via the API", payment_id)
        return JsonResponse({'received': True})

    try:
        fulfil_checkoutpage_payment(payment)
    except Exception:
        logger.exception("Error fulfilling Checkout Page payment %s", payment_id)
        return HttpResponse(status=500)

    return JsonResponse({'received': True})
