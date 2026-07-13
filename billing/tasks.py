import logging

from celery import shared_task

from .emails import send_credit_invoice_email
from .models import Invoice

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def send_invoice_email_task(invoice_id):
    """Send the invoice email from the worker, where SMTP is configured."""
    try:
        invoice = Invoice.objects.select_related('user').get(pk=invoice_id)
    except Invoice.DoesNotExist:
        logger.warning("Invoice %s no longer exists; skipping email", invoice_id)
        return

    sent = send_credit_invoice_email(invoice)
    if sent != invoice.email_sent:
        invoice.email_sent = sent
        invoice.save(update_fields=['email_sent'])
