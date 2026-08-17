from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_add_agent_role'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customuser',
            name='role',
            field=models.CharField(
                choices=[('client', 'Client'), ('agent', 'Agent'), ('sub_admin', 'Sub Admin'), ('admin', 'Admin')],
                default='client',
                max_length=10,
            ),
        ),
    ]
