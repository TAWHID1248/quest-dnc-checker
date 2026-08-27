# PayPal checkout was removed (Aug 2026); paypal_order_id is kept as
# display-only legacy data. State-only change (help_text), no schema change.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_payment_provider_paypal'),
    ]

    operations = [
        migrations.AlterField(
            model_name='payment',
            name='paypal_order_id',
            field=models.CharField(
                blank=True,
                help_text='Legacy PayPal Order ID (PayPal removed Aug 2026)',
                max_length=255,
                null=True,
                unique=True,
            ),
        ),
    ]
