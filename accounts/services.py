"""
Signup helpers shared by the email/password register view and Google OAuth.

Promo-code resolution, free-credit granting and the welcome email live here so
both signup paths behave identically.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing.models import CreditTransaction

logger = logging.getLogger(__name__)

PROMO_CODE_CREDITS = 100_000


class PromoCodeError(Exception):
    """Raised when a promo code is invalid or expired; message is user-facing."""


def resolve_promo_code(code_str):
    """Return the ACTIVE AgentPromoCode for code_str, or None if blank.

    Raises PromoCodeError with a user-facing message when the code is invalid
    or expired (expired codes are marked EXPIRED as a side effect).
    """
    from agents.models import AgentPromoCode

    code_str = (code_str or '').strip().upper()
    if not code_str:
        return None
    try:
        promo = AgentPromoCode.objects.select_related('agent').get(
            code=code_str,
            status=AgentPromoCode.Status.ACTIVE,
        )
    except AgentPromoCode.DoesNotExist:
        raise PromoCodeError('Invalid promo code. Please check and try again.')
    if promo.expires_at < timezone.now():
        promo.status = AgentPromoCode.Status.EXPIRED
        promo.save(update_fields=['status'])
        raise PromoCodeError('This promo code has expired. Please request a new one from your agent.')
    return promo


def grant_signup_credits(user, promo_code_obj=None):
    """Grant free signup credits (+ promo credits) to a freshly created user.

    Returns (signup_credits, promo_credits). Marks the promo code USED and
    rotates the agent's next code.
    """
    from agents.models import AgentPromoCode

    signup_credits = settings.SIGNUP_FREE_CREDITS
    promo_credits = PROMO_CODE_CREDITS if promo_code_obj else 0

    with transaction.atomic():
        if signup_credits:
            user.credits += signup_credits
            CreditTransaction.objects.create(
                user=user,
                type=CreditTransaction.Type.ADJUSTMENT,
                amount=signup_credits,
                price=0,
            )
        if promo_code_obj:
            user.credits += promo_credits
            CreditTransaction.objects.create(
                user=user,
                type=CreditTransaction.Type.ADJUSTMENT,
                amount=promo_credits,
                price=0,
            )
        if signup_credits or promo_code_obj:
            user.save(update_fields=['credits'])
        if promo_code_obj:
            promo_code_obj.status = AgentPromoCode.Status.USED
            promo_code_obj.used_by = user
            promo_code_obj.used_at = timezone.now()
            promo_code_obj.save(update_fields=['status', 'used_by', 'used_at'])

    if promo_code_obj:
        from agents.utils import generate_next_promo_code
        generate_next_promo_code(promo_code_obj.agent)

    return signup_credits, promo_credits


def queue_welcome_email(user, promo_credits, signup_credits):
    try:
        from .tasks import send_welcome_email
        send_welcome_email.delay(user.pk, promo_credits, signup_credits)
    except Exception:
        logger.exception("Failed to queue welcome email for %s", user.email)


def signup_success_message(signup_credits, promo_credits):
    total = signup_credits + promo_credits
    if total:
        return f'Account created! {total:,} free credits have been added to your account.'
    return 'Account created successfully.'
