"""
Invoice PDF rendering (reportlab, pure Python — no system deps needed on Railway).

`render_invoice_pdf(invoice)` returns the PDF bytes. It is deterministic for a
given invoice so it can be regenerated on every download instead of stored.
"""

from decimal import Decimal
from io import BytesIO

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor('#1f2328')
MUTED = colors.HexColor('#57606a')
LINE = colors.HexColor('#d0d7de')
BRAND = colors.HexColor('#0d1117')
ACCENT = colors.HexColor('#3b82f6')
PAID = colors.HexColor('#116329')
PAID_BG = colors.HexColor('#dafbe1')


def _styles():
    base = getSampleStyleSheet()
    return {
        'brand': ParagraphStyle('brand', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=20, textColor=colors.white, leading=24),
        'brand_sub': ParagraphStyle('brand_sub', parent=base['Normal'], fontName='Helvetica', fontSize=9, textColor=colors.HexColor('#8b949e'), leading=12),
        'title': ParagraphStyle('title', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=22, textColor=colors.white, alignment=TA_RIGHT, leading=26),
        'label': ParagraphStyle('label', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=8, textColor=MUTED, leading=11),
        'body': ParagraphStyle('body', parent=base['Normal'], fontName='Helvetica', fontSize=10, textColor=INK, leading=14),
        'body_r': ParagraphStyle('body_r', parent=base['Normal'], fontName='Helvetica', fontSize=10, textColor=INK, leading=14, alignment=TA_RIGHT),
        'bold': ParagraphStyle('bold', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=10, textColor=INK, leading=14),
        'bold_r': ParagraphStyle('bold_r', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=10, textColor=INK, leading=14, alignment=TA_RIGHT),
        'total_r': ParagraphStyle('total_r', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=13, textColor=INK, leading=16, alignment=TA_RIGHT),
        'muted': ParagraphStyle('muted', parent=base['Normal'], fontName='Helvetica', fontSize=8.5, textColor=MUTED, leading=12),
        'paid': ParagraphStyle('paid', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=9, textColor=PAID, leading=12, alignment=TA_RIGHT),
    }


def _company_lines():
    company = getattr(settings, 'INVOICE_COMPANY', {})
    lines = [company.get('name', 'CheckDNC')]
    lines += [part.strip() for part in (company.get('address') or '').split(',') if part.strip()]
    if company.get('email'):
        lines.append(company['email'])
    if company.get('website'):
        lines.append(company['website'])
    return lines


def _payment_reference(invoice):
    payment = invoice.payment
    if payment is None:
        return 'Credits granted by CheckDNC support'
    parts = [f"Payment {payment.payment_id}", payment.get_provider_display()]
    if payment.stripe_pi_id:
        parts.append(payment.stripe_pi_id)
    return ' · '.join(parts)


def render_invoice_pdf(invoice) -> bytes:
    st = _styles()
    user = invoice.user
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch, topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title=f"Invoice {invoice.invoice_number}", author=_company_lines()[0],
    )
    width = letter[0] - doc.leftMargin - doc.rightMargin
    story = []

    # ── Header band ──
    header = Table(
        [[Paragraph(_company_lines()[0], st['brand']), Paragraph('INVOICE', st['title'])],
         [Paragraph('Phone number DNC compliance scrubbing', st['brand_sub']), Paragraph(invoice.invoice_number, ParagraphStyle('num', parent=st['brand_sub'], alignment=TA_RIGHT))]],
        colWidths=[width * 0.6, width * 0.4],
    )
    header.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BRAND),
        ('LEFTPADDING', (0, 0), (-1, -1), 18), ('RIGHTPADDING', (0, 0), (-1, -1), 18),
        ('TOPPADDING', (0, 0), (-1, 0), 18), ('BOTTOMPADDING', (0, -1), (-1, -1), 18),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story += [header, Spacer(1, 22)]

    # ── From / Bill to / Meta ──
    from_block = [Paragraph('FROM', st['label'])] + [Paragraph(line, st['body']) for line in _company_lines()]
    to_lines = [user.display_name]
    if user.company:
        to_lines.append(user.company)
    to_lines.append(user.email)
    if user.phone:
        to_lines.append(user.phone)
    to_block = [Paragraph('BILLED TO', st['label'])] + [Paragraph(line, st['body']) for line in to_lines]

    status_text = 'PAID' if invoice.is_paid_purchase else 'NO CHARGE'
    meta_rows = [
        [Paragraph('Invoice date', st['muted']), Paragraph(invoice.created_at.strftime('%b %d, %Y'), st['body_r'])],
        [Paragraph('Invoice #', st['muted']), Paragraph(invoice.invoice_number, st['body_r'])],
        [Paragraph('Status', st['muted']), Paragraph(status_text, st['paid'])],
    ]
    if invoice.payment is not None:
        meta_rows.append([Paragraph('Payment ID', st['muted']), Paragraph(invoice.payment.payment_id, st['body_r'])])
    meta = Table(meta_rows, colWidths=[width * 0.14, width * 0.2])
    meta.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))

    blocks = Table([[from_block, to_block, meta]], colWidths=[width * 0.33, width * 0.33, width * 0.34])
    blocks.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story += [blocks, Spacer(1, 26)]

    # ── Line items ──
    credits_display = f"{invoice.credits:,.0f}"
    amount_display = f"${invoice.amount:,.2f}"
    credits = Decimal(invoice.credits)
    if credits and invoice.is_paid_purchase:
        unit = f"${(Decimal(invoice.amount) * 1000 / credits):,.4f} / 1,000"
    else:
        unit = '—'
    items = [
        [Paragraph('DESCRIPTION', st['label']), Paragraph('CREDITS', ParagraphStyle('lr', parent=st['label'], alignment=TA_RIGHT)),
         Paragraph('RATE', ParagraphStyle('lr2', parent=st['label'], alignment=TA_RIGHT)), Paragraph('AMOUNT', ParagraphStyle('lr3', parent=st['label'], alignment=TA_RIGHT))],
        [Paragraph(invoice.description, st['body']), Paragraph(credits_display, st['body_r']),
         Paragraph(unit, st['body_r']), Paragraph(amount_display, st['body_r'])],
        ['', '', Paragraph('Subtotal', st['body_r']), Paragraph(amount_display, st['body_r'])],
        ['', '', Paragraph('Tax', st['body_r']), Paragraph('$0.00', st['body_r'])],
        ['', '', Paragraph('Total', st['bold_r']), Paragraph(amount_display, st['total_r'])],
    ]
    table = Table(items, colWidths=[width * 0.46, width * 0.16, width * 0.2, width * 0.18])
    table.setStyle(TableStyle([
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, LINE),
        ('LINEBELOW', (0, 1), (-1, 1), 0.5, LINE),
        ('LINEABOVE', (2, 4), (-1, 4), 0.8, INK),
        ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story += [table, Spacer(1, 20)]

    # ── Payment note ──
    note = Table([[Paragraph(
        f"<b>{'Paid in full.' if invoice.is_paid_purchase else 'No payment due.'}</b> {_payment_reference(invoice)}. "
        f"{credits_display} credits were added to your CheckDNC account on {invoice.created_at.strftime('%b %d, %Y')}. Credits never expire.",
        ParagraphStyle('note', parent=st['body'], textColor=PAID),
    )]], colWidths=[width])
    note.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PAID_BG),
        ('LEFTPADDING', (0, 0), (-1, -1), 14), ('RIGHTPADDING', (0, 0), (-1, -1), 14),
        ('TOPPADDING', (0, 0), (-1, -1), 10), ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    story += [note, Spacer(1, 30)]

    # ── Footer ──
    story.append(Paragraph(
        f"Thank you for using {_company_lines()[0]}. Download this invoice any time from "
        f"<font color='#3b82f6'>{settings.SITE_URL}/billing/invoices/</font>. "
        f"Questions? Open a support ticket from your dashboard or email {settings.INVOICE_COMPANY.get('email', '')}.",
        st['muted'],
    ))

    doc.build(story)
    return buf.getvalue()
