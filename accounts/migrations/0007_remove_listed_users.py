from django.db import migrations

# One-off cleanup (2026-08-26): delete these specific accounts (and their
# related records via cascade). All other users are untouched.
REMOVE_EMAILS = [
    'paintim1248+t3@gmail.com',
    'robertyaki1@gmail.com',
    'paintim1248+api3@gmail.com',
    'paintim1248+t2@gmail.com',
    'millervon790@gmail.com',
    'paintim1248+t1@gmail.com',
    'paintim1248@gmail.com',
    'parkerjacob504+test2@gmail.com',
    'tawhidurrahman@nebs-it.com',
    'parkerjacob504+test1@gmail.com',
    'parkerjacob504@gmail.com',
    'ume@hotmail.com',
    'frzfardin2020@gmail.com',
    'hopperfin@yahoo.com',
    'danielcook1248@gmail.com',
    'topten@gmail.com',
    'smnaymurrahman@nebs-it.com',
    'mixblustteam@gmail.com',
    'tawhid@gmail.com',
    'claude-paypal-test-2026@example.com',
]


# django-allauth was removed from the project but its tables remain in the
# production DB with FKs to accounts_customuser; the ORM can't cascade into
# them, so they must be cleared with raw SQL before deleting the users.
ORPHAN_TABLE_CLEANUP = [
    ('account_emailconfirmation',
     'DELETE FROM account_emailconfirmation WHERE email_address_id IN '
     '(SELECT id FROM account_emailaddress WHERE user_id = ANY(%s))'),
    ('account_emailaddress',
     'DELETE FROM account_emailaddress WHERE user_id = ANY(%s)'),
    ('socialaccount_socialtoken',
     'DELETE FROM socialaccount_socialtoken WHERE account_id IN '
     '(SELECT id FROM socialaccount_socialaccount WHERE user_id = ANY(%s))'),
    ('socialaccount_socialaccount',
     'DELETE FROM socialaccount_socialaccount WHERE user_id = ANY(%s)'),
]


def remove_users(apps, schema_editor):
    CustomUser = apps.get_model('accounts', 'CustomUser')
    ids = list(
        CustomUser.objects.filter(email__in=REMOVE_EMAILS).values_list('id', flat=True)
    )
    if not ids:
        print('\n  No listed accounts found; nothing to delete.')
        return

    with schema_editor.connection.cursor() as cursor:
        for table, sql in ORPHAN_TABLE_CLEANUP:
            cursor.execute('SELECT to_regclass(%s)', [table])
            if cursor.fetchone()[0] is not None:
                cursor.execute(sql, [ids])

    deleted, _ = CustomUser.objects.filter(id__in=ids).delete()
    print(f'\n  Deleted {deleted} records for {len(ids)} listed accounts.')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_remove_listed_users'),
    ]

    operations = [
        migrations.RunPython(remove_users, migrations.RunPython.noop),
    ]
