import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


@shared_task(name='accounts.tasks.send_password_reset_email', ignore_result=True)
def send_password_reset_email(subject, body, to_email):
    """Send the password-reset email from the worker, where email is configured."""
    send_mail(
        subject, body,
        settings.DEFAULT_FROM_EMAIL,
        [to_email],
        fail_silently=False,
    )
    logger.info("Sent password reset email to %s", to_email)


@shared_task(name='accounts.tasks.send_welcome_email', ignore_result=True)
def send_welcome_email(user_id, promo_credits=0, signup_credits=0):
    """Send the signup welcome email from the worker, where SMTP is configured."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.warning("User %s no longer exists; skipping welcome email", user_id)
        return

    total_credits = (promo_credits or 0) + (signup_credits or 0)
    if total_credits:
        subject = f"Welcome to CheckDNC — {total_credits:,.0f} free credits added to your account"
        lines = [
            f"Hello {user.display_name},",
            "",
            "Welcome to CheckDNC! Your account is ready.",
            "",
        ]
        if signup_credits:
            lines.append(f"  Free signup credits:  {signup_credits:,.0f}")
        if promo_credits:
            lines.append(f"  Promo code credits:   {promo_credits:,.0f}")
        lines += [
            f"  Balance:              {user.credits:,.0f} credits",
            "",
            "Log in to start scrubbing:",
            "https://app.checkdnc.net/scrubber/",
            "",
            "— The CheckDNC Team",
        ]
        body = "\n".join(lines)
    else:
        subject = "Welcome to CheckDNC"
        body = (
            f"Hello {user.display_name},\n\n"
            f"Welcome to CheckDNC! Your account is ready.\n\n"
            f"Purchase credits and upload your first file to start scrubbing:\n"
            f"https://app.checkdnc.net/scrubber/\n\n"
            f"— The CheckDNC Team"
        )

    send_mail(
        subject, body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )
    logger.info("Sent welcome email to %s", user.email)
