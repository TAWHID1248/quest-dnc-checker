from django.db import migrations

# One-off cleanup (2026-08-23): delete these specific accounts (and their
# related records via cascade). All other users are untouched.
REMOVE_EMAILS = [
    'eyfojinj@immenseignite.info',
    'qjowtqvk@immenseignite.info',
    'nebsdev1@gmail.com',
    'stawhid79@gmail.com',
    'uptore110@gmail.com',
    'storerestore2022@gmail.com',
    'io@gmail.com',
    'tim@gmail.com',
    'storerestore220@gmail.com',
    'storerestore20@gmail.com',
    'tom@gmail.com',
]


def remove_users(apps, schema_editor):
    CustomUser = apps.get_model('accounts', 'CustomUser')
    deleted, _ = CustomUser.objects.filter(email__in=REMOVE_EMAILS).delete()
    print(f'\n  Deleted {deleted} records for {len(REMOVE_EMAILS)} listed accounts.')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_remove_agent_role'),
    ]

    operations = [
        migrations.RunPython(remove_users, migrations.RunPython.noop),
    ]
