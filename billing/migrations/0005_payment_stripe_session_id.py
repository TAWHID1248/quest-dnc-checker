from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0004_paypal_removed_help_text'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='stripe_session_id',
            field=models.CharField(
                blank=True, help_text='Stripe Checkout Session ID (idempotency key for credit grants)',
                max_length=255, null=True, unique=True,
            ),
        ),
        migrations.AlterField(
            model_name='payment',
            name='stripe_pi_id',
            field=models.CharField(blank=True, help_text='Stripe PaymentIntent ID', max_length=255),
        ),
    ]
