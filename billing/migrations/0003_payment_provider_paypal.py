from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0002_invoice'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='provider',
            field=models.CharField(
                choices=[('stripe', 'Stripe'), ('paypal', 'PayPal'), ('manual', 'Manual')],
                default='stripe',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='payment',
            name='paypal_order_id',
            field=models.CharField(
                blank=True,
                help_text='PayPal Order ID — idempotency key for credit grants',
                max_length=255,
                null=True,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name='payment',
            name='stripe_pi_id',
            field=models.CharField(blank=True, help_text='Legacy Stripe PaymentIntent ID', max_length=255),
        ),
    ]
