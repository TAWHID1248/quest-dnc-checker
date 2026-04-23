from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scrubber', '0003_scrubejob_result_file_dnc'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='scrubjob',
            name='litigator',
        ),
        migrations.AlterField(
            model_name='scrubjob',
            name='scrub_types',
            field=models.JSONField(
                default=list,
                help_text='List of scrub types: federal_dnc, state_dnc',
            ),
        ),
    ]
