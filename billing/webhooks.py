"""
Stripe webhook handlers.

Each handler is a plain function called by the webhook view after signature
verification.  Handlers are idempotent: they check whether a Payment/
PaymentMethod record already exists before creating a new one, so safe to
replay on duplicate deliveries.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction

from .models import CreditTransaction, Payment, PaymentMethod
from .stripe_utils import retrieve_payment_method

logger = logging.getLogger(__name__)
User = get_user_model()


# ── payment_intent.succeeded ─────────────────────────────────────────────────

def handle_payment_intent_succeeded(pi) -> None:
    """
    Credit the user after a successful payment.

    Idempotent: guarded by Payment.stripe_pi_id unique constraint — a second
    call for the same PaymentIntent will hit IntegrityError which we swallow.
    """
    metadata = pi.get('metadata', {})
    user_id   = metadata.get('user_id')
    tier_name = metadata.get('tier_name', '')
    credits   = int(metadata.get('credits', 0))
    amount    = pi['amount'] / 100          # cents → dollars

    if not user_id or not credits:
        logger.warning("payment_intent.succeeded missing metadata: %s", pi['id'])
        return

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("payment_intent.succeeded: user %s not found", user_id)
        return

    # Idempotency check
    if Payment.objects.filter(stripe_pi_id=pi['id']).exists():
        logger.info("payment_intent.succeeded already processed: %s", pi['id'])
        return

    # Resolve the PaymentMethod used
    pm_obj  = None
    pm_id   = pi.get('payment_method')
    if pm_id:
        pm_obj = PaymentMethod.objects.filter(stripe_pm_id=pm_id, user=user).first()

    with transaction.atomic():
        # 1. Create Payment record
        payment = Payment.objects.create(
            user=user,
            amount=amount,
            credits=credits,
            method=pm_obj,
            status=Payment.Status.COMPLETED,
            stripe_pi_id=pi['id'],
        )

        # 2. Add credits to user (SELECT FOR UPDATE prevents race)
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        locked_user.credits += credits
        locked_user.save(update_fields=['credits'])

        # 3. CreditTransaction audit record
        CreditTransaction.objects.create(
            user=user,
            type=CreditTransaction.Type.PURCHASE,
            amount=credits,
            price=amount,
            payment_method=pm_obj,
        )

    logger.info(
        "Credited %d credits to user %s (payment %s, PI %s)",
        credits, user.email, payment.payment_id, pi['id'],
    )


# ── payment_intent.payment_failed ────────────────────────────────────────────

def handle_payment_intent_failed(pi) -> None:
    """Mark an existing pending Payment as failed, if one exists."""
    payment = Payment.objects.filter(stripe_pi_id=pi['id']).first()
    if payment and payment.status == Payment.Status.PENDING:
        payment.status = Payment.Status.FAILED
        payment.save(update_fields=['status'])
        logger.info("Marked payment %s as FAILED (PI %s)", payment.payment_id, pi['id'])


# ── setup_intent.succeeded ───────────────────────────────────────────────────

def handle_setup_intent_succeeded(si) -> None:
    """
    Save a new PaymentMethod after the user completes card setup.
    Makes it the default if the user has no existing default.
    """
    metadata  = si.get('metadata', {})
    pm_id     = si.get('payment_method')
    customer_id = si.get('customer')

    if not pm_id or not customer_id:
        return

    # Resolve user from Stripe customer ID
    try:
        user = User.objects.get(stripe_customer_id=customer_id)
    except User.DoesNotExist:
        logger.error("setup_intent.succeeded: no user for customer %s", customer_id)
        return

    # Idempotency
    if PaymentMethod.objects.filter(stripe_pm_id=pm_id).exists():
        logger.info("PaymentMethod %s already saved", pm_id)
        return

    # Fetch card details from Stripe
    try:
        stripe_pm = retrieve_payment_method(pm_id)
    except Exception as exc:
        logger.error("Could not retrieve PaymentMethod %s: %s", pm_id, exc)
        return

    card = stripe_pm.get('card', {})
    brand   = card.get('brand', 'other').lower()
    last4   = card.get('last4', '0000')
    exp_m   = card.get('exp_month', 1)
    exp_y   = card.get('exp_year', 2099)

    has_default = PaymentMethod.objects.filter(user=user, is_default=True).exists()

    PaymentMethod.objects.create(
        user=user,
        stripe_pm_id=pm_id,
        card_type=brand if brand in PaymentMethod.CardType.values else PaymentMethod.CardType.OTHER,
        last4=last4,
        exp_date=f"{exp_m:02d}/{exp_y}",
        is_default=not has_default,   # first card becomes default automatically
    )
    logger.info("Saved PaymentMethod %s for user %s", pm_id, user.email)
