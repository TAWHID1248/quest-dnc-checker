from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_add_stripe_customer_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customuser',
            name='role',
            field=models.CharField(
                choices=[('client', 'Client'), ('agent', 'Agent'), ('admin', 'Admin')],
                default='client',
                max_length=10,
            ),
        ),
    ]
