"""
Credit-granting service.

Called from both the PayPal capture endpoint (primary path) and the PayPal
webhook (fallback).  Idempotent: guarded by the unique constraint on
Payment.paypal_order_id, so duplicate deliveries can never double-credit.
"""

import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from .models import CreditTransaction, Payment

logger = logging.getLogger(__name__)
User = get_user_model()


def grant_paypal_credits(order_id: str, user_id, credits: int, amount_usd: Decimal):
    """
    Credit `user_id` with `credits` for a captured PayPal order.

    Returns the created Payment, or None if the order was already processed
    (or the user no longer exists).
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error('grant_paypal_credits: user %s not found (order %s)', user_id, order_id)
        return None

    if Payment.objects.filter(paypal_order_id=order_id).exists():
        logger.info('PayPal order %s already processed', order_id)
        return None

    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                user=user,
                amount=amount_usd,
                credits=credits,
                status=Payment.Status.COMPLETED,
                provider=Payment.Provider.PAYPAL,
                paypal_order_id=order_id,
            )

            # SELECT FOR UPDATE prevents races with concurrent scrub deductions
            locked_user = User.objects.select_for_update().get(pk=user.pk)
            locked_user.credits += credits
            locked_user.save(update_fields=['credits'])

            CreditTransaction.objects.create(
                user=user,
                type=CreditTransaction.Type.PURCHASE,
                amount=credits,
                price=amount_usd,
            )
    except IntegrityError:
        # Capture endpoint and webhook raced — the other writer won.
        logger.info('PayPal order %s processed concurrently', order_id)
        return None

    logger.info(
        'Credited %d credits to user %s (payment %s, PayPal order %s)',
        credits, user.email, payment.payment_id, order_id,
    )
    return payment
