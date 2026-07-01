import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentPromoCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(db_index=True, max_length=20, unique=True)),
                ('sequence', models.PositiveIntegerField()),
                ('status', models.CharField(
                    choices=[('active', 'Active'), ('expired', 'Expired'), ('used', 'Used')],
                    default='active',
                    max_length=10,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField()),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('agent', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='promo_codes',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('used_by', models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='used_promo_code',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AlterUniqueTogether(
            name='agentpromocode',
            unique_together={('agent', 'sequence')},
        ),
        migrations.AddIndex(
            model_name='agentpromocode',
            index=models.Index(fields=['agent', 'status'], name='agents_agent_status_idx'),
        ),
        migrations.AddIndex(
            model_name='agentpromocode',
            index=models.Index(fields=['status', 'expires_at'], name='agents_status_expires_idx'),
        ),
    ]
