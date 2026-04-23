from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dnc_master', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dncmasterlist',
            name='list_type',
            field=models.CharField(
                choices=[
                    ('federal_dnc', 'Federal DNC'),
                    ('state_dnc', 'State DNC'),
                ],
                max_length=20,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name='dncuploadjob',
            name='list_type',
            field=models.CharField(
                choices=[
                    ('federal_dnc', 'Federal DNC'),
                    ('state_dnc', 'State DNC'),
                ],
                max_length=20,
            ),
        ),
    ]
