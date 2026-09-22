import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from .pdf import render_invoice_pdf

logger = logging.getLogger(__name__)


def send_credit_invoice_email(invoice) -> bool:
    """Email an invoice to the user after an admin grants credits.

    Returns True if the email was handed to the backend without error.
    Never raises — invoice delivery must not block the credit grant.
    """
    user = invoice.user
    subject = f"Invoice {invoice.invoice_number} — {invoice.credits:,.0f} credits added to your account"
    invoice_url = settings.SITE_URL + reverse('billing:invoice_pdf', args=[invoice.invoice_number])
    invoices_url = settings.SITE_URL + reverse('billing:invoice_list')

    context = {
        'invoice': invoice,
        'user': user,
        'credits_display': f"{invoice.credits:,.0f}",
        'amount_display': f"{invoice.amount:,.2f}",
        'balance_display': f"{user.credits:,.0f}",
        'invoice_url': invoice_url,
        'invoices_url': invoices_url,
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
        f"Your invoice PDF is attached. You can also download it any time:\n"
        f"{invoice_url}\n\n"
        f"Log in to start scrubbing:\n"
        f"{settings.SITE_URL}/scrubber/\n\n"
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
        try:
            msg.attach(invoice.pdf_filename, render_invoice_pdf(invoice), 'application/pdf')
        except Exception:
            # The email is still useful without the attachment; the PDF stays downloadable in-app.
            logger.exception("Could not render PDF for invoice %s; sending email without attachment", invoice.invoice_number)
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send invoice email %s to %s", invoice.invoice_number, user.email)
        return False

    logger.info("Sent invoice email %s to %s", invoice.invoice_number, user.email)
    return True
