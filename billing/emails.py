import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def send_credit_invoice_email(invoice) -> bool:
    """Email an invoice to the user after an admin grants credits.

    Returns True if the email was handed to the backend without error.
    Never raises — invoice delivery must not block the credit grant.
    """
    user = invoice.user
    subject = f"Invoice {invoice.invoice_number} — {invoice.credits:,.0f} credits added to your account"

    context = {
        'invoice': invoice,
        'user': user,
        'credits_display': f"{invoice.credits:,.0f}",
        'amount_display': f"{invoice.amount:,.2f}",
        'balance_display': f"{user.credits:,.0f}",
    }
    text_body = (
        f"Hello {user.display_name},\n\n"
        f"Credits have been added to your CheckDNC account.\n\n"
        f"Invoice\n"
        f"-------\n"
        f"  Invoice #:      {invoice.invoice_number}\n"
        f"  Date:           {invoice.created_at:%b %d, %Y}\n"
        f"  Credits added:  {invoice.credits:,.0f}\n"
        f"  Amount:         ${invoice.amount:,.2f}\n"
        f"  New balance:    {user.credits:,.0f} credits\n\n"
        f"Log in to start scrubbing:\n"
        f"https://app.checkdnc.net/scrubber/\n\n"
        f"— The CheckDNC Team"
    )
    html_body = render_to_string('billing/emails/credit_invoice.html', context)

    try:
        msg = EmailMultiAlternatives(
            subject, text_body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send invoice email %s to %s", invoice.invoice_number, user.email)
        return False

    logger.info("Sent invoice email %s to %s", invoice.invoice_number, user.email)
    return True
