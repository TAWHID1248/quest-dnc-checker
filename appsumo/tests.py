import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import AppSumoLicense, AppSumoWebhookEvent

User = get_user_model()

API_KEY = 'test-appsumo-api-key'
TIERS = {1: 100_000, 2: 250_000, 3: 1_000_000}


@override_settings(APPSUMO_API_KEY=API_KEY, APPSUMO_TIER_CREDITS=TIERS)
class WebhookTests(TestCase):
    def _post(self, payload, sign=True):
        body = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        headers = {'HTTP_X_APPSUMO_TIMESTAMP': timestamp}
        if sign:
            sig = hmac.new(API_KEY.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()
            headers['HTTP_X_APPSUMO_SIGNATURE'] = sig
        return self.client.post(
            reverse('appsumo:webhook'), body, content_type='application/json', **headers,
        )

    def test_rejects_unsigned_request(self):
        resp = self._post({'event': 'activate', 'license_key': 'k1'}, sign=False)
        self.assertEqual(resp.status_code, 403)

    def test_test_event_returns_success_echo(self):
        resp = self._post({'event': 'activate', 'license_key': 'k1', 'test': True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'success': True, 'event': 'activate'})
        self.assertFalse(AppSumoLicense.objects.exists())
        self.assertTrue(AppSumoWebhookEvent.objects.filter(test=True).exists())

    def test_purchase_creates_pending_license(self):
        resp = self._post({'event': 'purchase', 'license_key': 'k1', 'tier': 2})
        self.assertEqual(resp.json()['success'], True)
        lic = AppSumoLicense.objects.get(license_key='k1')
        self.assertEqual(lic.status, AppSumoLicense.Status.PENDING)
        self.assertEqual(lic.tier, 2)

    def test_activate_grants_credits_to_linked_user(self):
        user = User.objects.create_user('buyer@x.com', 'pw', name='B')
        AppSumoLicense.objects.create(license_key='k1', user=user)
        self._post({'event': 'activate', 'license_key': 'k1', 'tier': 1})
        user.refresh_from_db()
        self.assertEqual(user.credits, Decimal(100_000))
        # Replay is idempotent
        self._post({'event': 'activate', 'license_key': 'k1', 'tier': 1})
        user.refresh_from_db()
        self.assertEqual(user.credits, Decimal(100_000))

    def test_upgrade_grants_tier_difference(self):
        user = User.objects.create_user('up@x.com', 'pw', name='U')
        AppSumoLicense.objects.create(
            license_key='old', user=user, tier=1,
            status=AppSumoLicense.Status.ACTIVE, credits_granted=100_000,
        )
        user.credits = 100_000
        user.save()
        self._post({'event': 'upgrade', 'license_key': 'new', 'prev_license_key': 'old', 'tier': 2})
        user.refresh_from_db()
        self.assertEqual(user.credits, Decimal(250_000))
        new = AppSumoLicense.objects.get(license_key='new')
        self.assertEqual(new.user, user)
        self.assertEqual(new.credits_granted, Decimal(250_000))
        old = AppSumoLicense.objects.get(license_key='old')
        self.assertEqual(old.status, AppSumoLicense.Status.DEACTIVATED)

    def test_deactivate_claws_back_credits(self):
        user = User.objects.create_user('re@x.com', 'pw', name='R')
        user.credits = 120_000
        user.save()
        AppSumoLicense.objects.create(
            license_key='k1', user=user, tier=1,
            status=AppSumoLicense.Status.ACTIVE, credits_granted=100_000,
        )
        self._post({'event': 'deactivate', 'license_key': 'k1'})
        user.refresh_from_db()
        self.assertEqual(user.credits, Decimal(20_000))
        lic = AppSumoLicense.objects.get(license_key='k1')
        self.assertEqual(lic.status, AppSumoLicense.Status.DEACTIVATED)


@override_settings(APPSUMO_API_KEY=API_KEY, APPSUMO_TIER_CREDITS=TIERS)
class OAuthRedirectTests(TestCase):
    def test_bare_get_returns_200_for_validation(self):
        resp = self.client.get(reverse('appsumo:oauth_redirect'))
        self.assertEqual(resp.status_code, 200)

    @patch('appsumo.views.exchange_code_for_license_key', return_value='lk-1')
    def test_logged_in_user_gets_license_linked(self, _mock):
        user = User.objects.create_user('in@x.com', 'pw', name='I')
        AppSumoLicense.objects.create(license_key='lk-1', tier=2)
        self.client.force_login(user)
        resp = self.client.get(reverse('appsumo:oauth_redirect'), {'code': 'abc'})
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual(user.credits, Decimal(250_000))

    @patch('appsumo.views.exchange_code_for_license_key', return_value='lk-2')
    def test_anonymous_user_stashes_key_and_links_on_register(self, _mock):
        resp = self.client.get(reverse('appsumo:oauth_redirect'), {'code': 'abc'})
        self.assertRedirects(resp, reverse('accounts:register'), fetch_redirect_response=False)
        self.assertEqual(self.client.session['appsumo_license_key'], 'lk-2')

        AppSumoLicense.objects.create(license_key='lk-2', tier=1)
        self.client.post(reverse('accounts:register'), {
            'name': 'New Buyer',
            'email': 'new@x.com',
            'password1': 'S0me-strong-pass',
            'password2': 'S0me-strong-pass',
        })
        user = User.objects.get(email='new@x.com')
        self.assertEqual(user.credits, Decimal(100_000))
        self.assertEqual(AppSumoLicense.objects.get(license_key='lk-2').user, user)
