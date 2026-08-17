from django.db import migrations, models


def agents_to_clients(apps, schema_editor):
    CustomUser = apps.get_model('accounts', 'CustomUser')
    CustomUser.objects.filter(role='agent').update(role='client')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_add_sub_admin_role'),
    ]

    operations = [
        migrations.RunPython(agents_to_clients, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='customuser',
            name='role',
            field=models.CharField(
                choices=[('client', 'Client'), ('sub_admin', 'Sub Admin'), ('admin', 'Admin')],
                default='client',
                max_length=10,
            ),
        ),
    ]
