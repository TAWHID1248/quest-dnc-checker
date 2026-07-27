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
            name='AppSumoWebhookEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('license_key', models.CharField(blank=True, max_length=64)),
                ('event', models.CharField(max_length=30)),
                ('test', models.BooleanField(default=False)),
                ('payload', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='AppSumoLicense',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('license_key', models.CharField(max_length=64, unique=True)),
                ('prev_license_key', models.CharField(blank=True, help_text='Previous license key (set on upgrade/downgrade events)', max_length=64)),
                ('tier', models.PositiveIntegerField(blank=True, null=True)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('active', 'Active'), ('deactivated', 'Deactivated')], default='pending', max_length=20)),
                ('credits_granted', models.DecimalField(decimal_places=2, default=0, help_text='Total credits granted to the user for this license', max_digits=10)),
                ('activated_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='appsumo_licenses', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='appsumowebhookevent',
            index=models.Index(fields=['license_key'], name='appsumo_app_license_0b91c7_idx'),
        ),
        migrations.AddIndex(
            model_name='appsumowebhookevent',
            index=models.Index(fields=['created_at'], name='appsumo_app_created_631d22_idx'),
        ),
        migrations.AddIndex(
            model_name='appsumolicense',
            index=models.Index(fields=['user'], name='appsumo_app_user_id_d68207_idx'),
        ),
        migrations.AddIndex(
            model_name='appsumolicense',
            index=models.Index(fields=['status'], name='appsumo_app_status_a2abc9_idx'),
        ),
    ]
