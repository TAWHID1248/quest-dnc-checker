"""
Credit-purchase fulfilment.

Turns a confirmed card payment into credits:
  * `fulfil_checkoutpage_payment` — a paid Checkout Page payment
  * `fulfil_checkout_session`     — a paid Stripe Checkout Session

Both are idempotent (keyed on the provider's payment/session id), so they are
safe to call more than once and safe for the provider to retry.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from .checkoutpage_utils import REF_PARAM, read_ref, tier_name_for_page
from .models import CreditTransaction, Invoice, Payment
from .tasks import send_invoice_email_task

logger = logging.getLogger(__name__)
User = get_user_model()


def _as_dict(obj):
    """Plain dicts pass through; Stripe SDK objects are converted via to_dict()."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, 'to_dict_recursive'):
        return obj.to_dict_recursive()
    return obj.to_dict()


def fulfil_checkout_session(session) -> Payment | None:
    """
    Grant credits for a completed, paid Checkout Session.

    Returns the Payment (new or pre-existing), or None if the session is not
    paid or its metadata is unusable. Never raises for duplicate deliveries.
    """
    session = _as_dict(session)
    session_id = session.get('id', '')

    if session.get('payment_status') != 'paid':
        logger.info("Checkout session %s not paid yet (%s); skipping", session_id, session.get('payment_status'))
        return None

    existing = Payment.objects.filter(stripe_session_id=session_id).first()
    if existing:
        logger.info("Checkout session %s already fulfilled as %s", session_id, existing.payment_id)
        return existing

    metadata = _as_dict(session.get('metadata') or {})
    user_id = metadata.get('user_id') or session.get('client_reference_id')
    try:
        credits = int(metadata.get('credits', 0))
    except (TypeError, ValueError):
        credits = 0
    if not user_id or credits <= 0:
        logger.error("Checkout session %s missing user/credits metadata: %s", session_id, metadata)
        return None

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("Checkout session %s: user %s not found", session_id, user_id)
        return None

    amount = Decimal(session.get('amount_total') or 0) / 100      # cents → USD
    pi = session.get('payment_intent') or ''
    pi_id = pi if isinstance(pi, str) else pi.get('id', '')

    return _record_purchase(
        user=user,
        amount=amount,
        credits=credits,
        tier_name=metadata.get('tier_name', ''),
        provider=Payment.Provider.STRIPE,
        idempotency_field='stripe_session_id',
        idempotency_value=session_id,
        stripe_pi_id=pi_id,
    )


def fulfil_checkoutpage_payment(payment: dict) -> Payment | None:
    """
    Grant credits for a paid Checkout Page payment.

    `payment` is the payment object fetched from the Checkout Page API (never
    the raw webhook body). The buyer is identified by the signed `ref` query
    parameter we attached to the checkout link, falling back to the email
    entered at checkout. The tier — and so the credit amount — comes from the
    page the payment was made on, never from anything the browser can edit.

    Returns the Payment (new or pre-existing), or None if the payment cannot
    be fulfilled. Idempotent on the Checkout Page payment id.
    """
    from .views import PRICING_TIERS            # avoid a circular import at load time

    cp_id = payment.get('id') or ''
    if not cp_id:
        logger.error("Checkout Page payment without an id; skipping")
        return None

    if payment.get('status') != 'paid':
        logger.info("Checkout Page payment %s not paid (%s); skipping", cp_id, payment.get('status'))
        return None

    if not payment.get('livemode', False) and not settings.CHECKOUTPAGE_ALLOW_TEST_PAYMENTS:
        logger.warning("Checkout Page payment %s is a test payment; not granting credits", cp_id)
        return None

    existing = Payment.objects.filter(checkoutpage_payment_id=cp_id).first()
    if existing:
        logger.info("Checkout Page payment %s already fulfilled as %s", cp_id, existing.payment_id)
        return existing

    currency = (payment.get('currency') or 'usd').lower()
    if currency != 'usd':
        logger.error("Checkout Page payment %s is in %s, expected usd; skipping", cp_id, currency)
        return None

    tier_name = tier_name_for_page(payment.get('pageSlug'))
    tier = next((t for t in PRICING_TIERS if t['name'] == tier_name), None)
    if tier is None:
        logger.error("Checkout Page payment %s: page %r is not mapped to a pricing tier",
                     cp_id, payment.get('pageSlug'))
        return None

    user = None
    user_id = read_ref((payment.get('queryParameters') or {}).get(REF_PARAM, ''))
    if user_id:
        user = User.objects.filter(pk=user_id).first()
    if user is None:
        email = (payment.get('customerEmail') or '').strip()
        if email:
            user = User.objects.filter(email__iexact=email).first()
    if user is None:
        logger.error("Checkout Page payment %s: no matching user (email %s) — grant credits manually",
                     cp_id, payment.get('customerEmail'))
        return None

    quantity = payment.get('quantity') or 1
    try:
        quantity = max(int(quantity), 1)
    except (TypeError, ValueError):
        quantity = 1

    cents = payment.get('amountPaid')
    if cents is None:
        cents = payment.get('amount') or 0
    amount = Decimal(str(cents)) / 100                             # cents → USD

    return _record_purchase(
        user=user,
        amount=amount,
        credits=tier['credits'] * quantity,
        tier_name=tier['name'],
        provider=Payment.Provider.CHECKOUT_PAGE,
        idempotency_field='checkoutpage_payment_id',
        idempotency_value=cp_id,
        stripe_pi_id=payment.get('stripePaymentIntentId') or '',
    )


def _record_purchase(*, user, amount, credits, tier_name, provider,
                     idempotency_field, idempotency_value, stripe_pi_id='') -> Payment | None:
    """
    Write Payment + credit top-up + ledger entry + Invoice in one transaction
    and queue the invoice email. The unique `idempotency_field` makes a
    concurrent duplicate lose with IntegrityError instead of double-granting.
    """
    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                user=user,
                amount=amount,
                credits=credits,
                status=Payment.Status.COMPLETED,
                provider=provider,
                stripe_pi_id=stripe_pi_id,
                **{idempotency_field: idempotency_value},
            )

            # SELECT FOR UPDATE: the scrub worker also mutates credits.
            locked_user = User.objects.select_for_update().get(pk=user.pk)
            locked_user.credits += credits
            locked_user.save(update_fields=['credits'])

            txn = CreditTransaction.objects.create(
                user=user,
                type=CreditTransaction.Type.PURCHASE,
                amount=credits,
                price=amount,
            )
            invoice = Invoice.objects.create(
                user=user,
                transaction=txn,
                payment=payment,
                credits=credits,
                amount=amount,
                notes=f"{tier_name} plan — {credits:,} DNC scrubbing credits".strip(' —'),
            )
    except IntegrityError:
        # Two deliveries raced (webhook retry / success page); the other one won.
        logger.info("%s %s fulfilled concurrently", idempotency_field, idempotency_value)
        return Payment.objects.filter(**{idempotency_field: idempotency_value}).first()

    transaction.on_commit(lambda: send_invoice_email_task.delay(invoice.pk))
    logger.info(
        "Granted %d credits to %s for $%s (payment %s, %s %s)",
        credits, user.email, amount, payment.payment_id, idempotency_field, idempotency_value,
    )
    return payment
