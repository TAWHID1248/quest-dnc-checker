from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scrubber', '0004_remove_litigator'),
    ]

    operations = [
        migrations.AddField(
            model_name='scrubjob',
            name='processed_count',
            field=models.PositiveIntegerField(default=0, help_text='Numbers checked so far (used for resume)'),
        ),
        migrations.AddField(
            model_name='scrubjob',
            name='partial_data_file',
            field=models.FileField(
                blank=True, null=True,
                upload_to='scrub_partial/%Y/%m/',
                help_text='Serialised partial clean+DNC lists saved on pause',
            ),
        ),
        migrations.AlterField(
            model_name='scrubjob',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('queued', 'Queued'),
                    ('processing', 'Processing'),
                    ('paused', 'Paused'),
                    ('completed', 'Completed'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
