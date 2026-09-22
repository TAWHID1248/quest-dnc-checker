"""
Credit-purchase fulfilment.

`fulfil_checkout_session` is the single place that turns a paid Stripe
Checkout Session into credits. It is idempotent (keyed on the session id),
so it is safe to call from both the webhook and the success page, and safe
for Stripe to retry.
"""

import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

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
    tier_name = metadata.get('tier_name', '')

    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                user=user,
                amount=amount,
                credits=credits,
                status=Payment.Status.COMPLETED,
                provider=Payment.Provider.STRIPE,
                stripe_session_id=session_id,
                stripe_pi_id=pi_id,
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
        # Webhook and success page raced; the other side won.
        logger.info("Checkout session %s fulfilled concurrently", session_id)
        return Payment.objects.filter(stripe_session_id=session_id).first()

    transaction.on_commit(lambda: send_invoice_email_task.delay(invoice.pk))
    logger.info(
        "Granted %d credits to %s for $%s (payment %s, session %s)",
        credits, user.email, amount, payment.payment_id, session_id,
    )
    return payment
