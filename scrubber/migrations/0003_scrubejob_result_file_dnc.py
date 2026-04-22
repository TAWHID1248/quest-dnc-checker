from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scrubber', '0002_add_error_message'),
    ]

    operations = [
        migrations.AddField(
            model_name='scrubjob',
            name='result_file_dnc',
            field=models.FileField(blank=True, null=True, upload_to='scrub_results/%Y/%m/'),
        ),
    ]
