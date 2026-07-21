import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


@shared_task(name='accounts.tasks.send_welcome_email', ignore_result=True)
def send_welcome_email(user_id, promo_credits=0):
    """Send the signup welcome email from the worker, where SMTP is configured."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.warning("User %s no longer exists; skipping welcome email", user_id)
        return

    if promo_credits:
        subject = f"Welcome to CheckDNC — {promo_credits:,.0f} credits added to your account"
        body = (
            f"Hello {user.display_name},\n\n"
            f"Welcome to CheckDNC! Your account is ready.\n\n"
            f"Your promo code was applied successfully:\n\n"
            f"  Credits added:  {promo_credits:,.0f}\n"
            f"  Balance:        {user.credits:,.0f} credits\n\n"
            f"Log in to start scrubbing:\n"
            f"https://app.checkdnc.net/scrubber/\n\n"
            f"— The CheckDNC Team"
        )
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
