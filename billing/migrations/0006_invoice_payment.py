from django.db import migrations, models
import django.db.models.deletion


def link_existing_invoices(apps, schema_editor):
    """Best-effort backfill: match Stripe invoices to payments via the ledger note."""
    Invoice = apps.get_model('billing', 'Invoice')
    Payment = apps.get_model('billing', 'Payment')
    for invoice in Invoice.objects.filter(payment__isnull=True, amount__gt=0):
        payment = (
            Payment.objects
            .filter(user_id=invoice.user_id, amount=invoice.amount, credits=invoice.credits, invoice__isnull=True)
            .order_by('-created_at')
            .first()
        )
        if payment is not None:
            invoice.payment = payment
            invoice.save(update_fields=['payment'])


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0005_payment_stripe_session_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='payment',
            field=models.OneToOneField(
                blank=True,
                help_text='Card payment this invoice was issued for (empty for admin credit grants)',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='invoice',
                to='billing.payment',
            ),
        ),
        migrations.RunPython(link_existing_invoices, migrations.RunPython.noop),
    ]
