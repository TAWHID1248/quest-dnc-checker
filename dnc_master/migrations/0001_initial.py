from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DncMasterList',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('list_type', models.CharField(choices=[('federal_dnc', 'Federal DNC'), ('state_dnc', 'State DNC'), ('litigator', 'Litigator')], max_length=20, unique=True)),
                ('record_count', models.BigIntegerField(default=0)),
                ('last_updated', models.DateTimeField(blank=True, null=True)),
                ('is_loading', models.BooleanField(default=False)),
                ('last_uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['list_type']},
        ),
        migrations.CreateModel(
            name='DncUploadJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('list_type', models.CharField(choices=[('federal_dnc', 'Federal DNC'), ('state_dnc', 'State DNC'), ('litigator', 'Litigator')], max_length=20)),
                ('mode', models.CharField(choices=[('replace', 'Replace'), ('append', 'Append')], default='replace', max_length=10)),
                ('file', models.FileField(upload_to='dnc_uploads/%Y/%m/')),
                ('original_filename', models.CharField(blank=True, max_length=255)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=20)),
                ('total_rows', models.BigIntegerField(default=0)),
                ('records_loaded', models.BigIntegerField(default=0)),
                ('error_message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('uploaded_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='dnc_uploads', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-created_at']},
        ),
        # Raw SQL table for the actual 250M phone numbers — not a Django model
        # so we get full control over schema and avoid auto-id overhead.
        migrations.RunSQL(
            sql="""
                CREATE TABLE IF NOT EXISTS dnc_master_numbers (
                    number    BIGINT   NOT NULL,
                    list_type SMALLINT NOT NULL,
                    state     CHAR(2),
                    PRIMARY KEY (number, list_type)
                )
            """,
            reverse_sql="DROP TABLE IF EXISTS dnc_master_numbers",
        ),
    ]
