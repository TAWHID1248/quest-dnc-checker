import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage

logger = logging.getLogger(__name__)

SUPPORT_EMAIL = 'support@checkdnc.net'


@shared_task(name='support.tasks.send_support_email')
def send_support_email(name, email, number, message):
    body = (
        f"New support request from CheckDNC\n"
        f"{'-' * 40}\n"
        f"Name:    {name}\n"
        f"Email:   {email}\n"
        f"Number:  {number or '—'}\n"
        f"{'-' * 40}\n\n"
        f"{message}\n"
    )
    msg = EmailMessage(
        subject=f"[CheckDNC Support] Message from {name}",
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[SUPPORT_EMAIL],
        reply_to=[email],
    )
    msg.send(fail_silently=False)
    logger.info("Support email sent for %s <%s>", name, email)
