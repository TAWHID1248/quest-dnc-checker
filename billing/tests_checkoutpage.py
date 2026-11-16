"""Checkout Page integration tests (the Checkout Page API is mocked; no network)."""
import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .checkoutpage_utils import (
    CheckoutPageError,
    build_checkout_url,
    make_ref,
    read_ref,
    retrieve_payment,
    verify_webhook_signature,
)
from .models import CreditTransaction, Invoice, Payment
from .services import fulfil_checkoutpage_payment

User = get_user_model()

SECRET = 'cp_webhook_secret_123'
CP_SETTINGS = dict(
    CHECKOUTPAGE_API_KEY='cp_key',
    CHECKOUTPAGE_WEBHOOK_SECRET=SECRET,
    CHECKOUTPAGE_PAGE_URLS={
        'Starter': 'https://checkdnc.checkoutpage.com/starter',
        'Professional': 'https://checkdnc.checkoutpage.com/professional/',
        'Enterprise': '',
    },
    CHECKOUTPAGE_ALLOW_TEST_PAYMENTS=False,
    CELERY_TASK_ALWAYS_EAGER=True,
)
PAYMENT_ID = '6464cc07822f15f2aca067dd'


def _payment(user=None, **overrides):
    payment = {
        'id': PAYMENT_ID,
        'status': 'paid',
        'livemode': True,
        'currency': 'usd',
        'amount': 1000,
        'amountPaid': 1000,
        'quantity': 1,
        'pageSlug': 'starter',
        'customerEmail': 'someone-else@example.com',
        'stripePaymentIntentId': 'pi_cp_1',
        'queryParameters': {'ref': make_ref(user)} if user else {},
    }
    payment.update(overrides)
    return payment


@override_settings(**CP_SETTINGS)
class UtilsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='buyer+x@example.com', password='x', name='Buyer')

    def test_checkout_url_carries_signed_ref_and_email(self):
        url = build_checkout_url(self.user, {'name': 'Starter'})
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        self.assertEqual(parts.netloc + parts.path, 'checkdnc.checkoutpage.com/starter')
        self.assertEqual(query['customer_email'], ['buyer+x@example.com'])
        self.assertEqual(read_ref(query['ref'][0]), self.user.pk)

    def test_tier_without_page_has_no_url(self):
        self.assertIsNone(build_checkout_url(self.user, {'name': 'Enterprise'}))

    def test_forged_ref_rejected(self):
        self.assertIsNone(read_ref(make_ref(self.user)[:-2] + 'xx'))
        self.assertIsNone(read_ref(str(self.user.pk)))
        self.assertIsNone(read_ref(''))

    def test_signature(self):
        body = b'{"event":"payment.paid"}'
        good = 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_webhook_signature(body, good))
        self.assertFalse(verify_webhook_signature(body + b' ', good))
        self.assertFalse(verify_webhook_signature(body, good.replace('sha256=', 'sha1=')))
        self.assertFalse(verify_webhook_signature(body, ''))

    def test_retrieve_payment(self):
        ok = mock.Mock(status_code=200, **{'json.return_value': {'data': {'id': PAYMENT_ID}}})
        with mock.patch('billing.checkoutpage_utils.requests.get', return_value=ok) as get:
            self.assertEqual(retrieve_payment(PAYMENT_ID), {'id': PAYMENT_ID})
        self.assertEqual(get.call_args.kwargs['headers'], {'Authorization': 'Bearer cp_key'})
        self.assertTrue(get.call_args.args[0].endswith(f'/v1/payments/{PAYMENT_ID}'))

        with mock.patch('billing.checkoutpage_utils.requests.get', return_value=mock.Mock(status_code=404)) as get:
            self.assertIsNone(retrieve_payment(PAYMENT_ID))
        self.assertEqual(get.call_count, 1)        # no test-mode lookup in production

        with mock.patch('billing.checkoutpage_utils.requests.get', return_value=mock.Mock(status_code=500)):
            with self.assertRaises(CheckoutPageError):
                retrieve_payment(PAYMENT_ID)

    @override_settings(CHECKOUTPAGE_ALLOW_TEST_PAYMENTS=True)
    def test_retrieve_payment_falls_back_to_test_mode_when_allowed(self):
        responses = [mock.Mock(status_code=404),
                     mock.Mock(status_code=200, **{'json.return_value': {'data': {'id': PAYMENT_ID}}})]
        with mock.patch('billing.checkoutpage_utils.requests.get', side_effect=responses) as get:
            self.assertEqual(retrieve_payment(PAYMENT_ID), {'id': PAYMENT_ID})
        self.assertEqual(get.call_args.kwargs['params'], {'livemode': 'false'})


@override_settings(**CP_SETTINGS)
class FulfilmentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='buyer@example.com', password='x', name='Buyer')
        self.user.credits = 500
        self.user.save()

    def _fulfil(self, payment):
        with mock.patch('billing.services.send_invoice_email_task') as task, \
             self.captureOnCommitCallbacks(execute=True):
            result = fulfil_checkoutpage_payment(payment)
        self.user.refresh_from_db()
        return result, task

    def test_grants_credits_once(self):
        p1, task = self._fulfil(_payment(self.user))
        p2, _ = self._fulfil(_payment(self.user))            # webhook retry
        self.assertEqual(p1.pk, p2.pk)
        self.assertEqual(self.user.credits, 100_500)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(p1.amount, Decimal('10.00'))
        self.assertEqual(p1.provider, 'checkoutpage')
        self.assertEqual(p1.status, 'completed')
        self.assertEqual(p1.checkoutpage_payment_id, PAYMENT_ID)
        self.assertEqual(p1.provider_ref, PAYMENT_ID)
        self.assertEqual(CreditTransaction.objects.filter(type='purchase').count(), 1)
        inv = Invoice.objects.get()
        self.assertEqual(inv.payment_id, p1.pk)
        self.assertIn('Starter plan', inv.notes)
        task.delay.assert_called_once_with(inv.pk)

    def test_credits_come_from_the_page_not_the_amount(self):
        payment = _payment(self.user, pageSlug='professional', amount=2000, amountPaid=1600)
        p, _ = self._fulfil(payment)
        self.assertEqual(p.credits, 250_000)
        self.assertEqual(p.amount, Decimal('16.00'))          # coupon applied

    def test_falls_back_to_customer_email(self):
        p, _ = self._fulfil(_payment(customerEmail='Buyer@Example.com'))
        self.assertEqual(p.user, self.user)
        self.assertEqual(self.user.credits, 100_500)

    def test_ref_wins_over_customer_email(self):
        other = User.objects.create_user(email='other@example.com', password='x', name='Other')
        p, _ = self._fulfil(_payment(self.user, customerEmail='other@example.com'))
        other.refresh_from_db()
        self.assertEqual(p.user, self.user)
        self.assertEqual(other.credits, 0)

    def test_rejected_payments_grant_nothing(self):
        cases = {
            'unpaid': _payment(self.user, status='pending'),
            'test payment': _payment(self.user, livemode=False),
            'wrong currency': _payment(self.user, currency='eur'),
            'unknown page': _payment(self.user, pageSlug='something-else'),
            'tier without page': _payment(self.user, pageSlug=''),
            'unknown buyer': _payment(queryParameters={'ref': 'forged'}),
            'no id': _payment(self.user, id=''),
        }
        for label, payment in cases.items():
            with self.subTest(label):
                result, _ = self._fulfil(payment)
                self.assertIsNone(result)
        self.assertEqual(self.user.credits, 500)
        self.assertEqual(Payment.objects.count(), 0)

    @override_settings(CHECKOUTPAGE_ALLOW_TEST_PAYMENTS=True)
    def test_test_payment_allowed_when_enabled(self):
        p, _ = self._fulfil(_payment(self.user, livemode=False))
        self.assertIsNotNone(p)
        self.assertEqual(self.user.credits, 100_500)


@override_settings(**CP_SETTINGS)
class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='buyer@example.com', password='x', name='Buyer')
        self.client.login(username='buyer@example.com', password='x')

    def test_modal_shows_card_button(self):
        r = self.client.get(reverse('billing:home'))
        self.assertContains(r, 'Pay with Card')

    @override_settings(CHECKOUTPAGE_API_KEY='', STRIPE_PUBLISHABLE_KEY='', STRIPE_SECRET_KEY='')
    def test_modal_hides_card_button_when_nothing_configured(self):
        r = self.client.get(reverse('billing:home'))
        self.assertNotContains(r, 'Pay with Card')

    def test_create_checkout_redirects_to_checkout_page_not_stripe(self):
        with mock.patch('billing.views.create_checkout_session') as stripe_create:
            r = self.client.post(reverse('billing:create_checkout'), {'tier': 'Professional'})
        stripe_create.assert_not_called()
        self.assertEqual(r.status_code, 302)
        parts = urlsplit(r['Location'])
        self.assertEqual(parts.netloc + parts.path, 'checkdnc.checkoutpage.com/professional/')
        self.assertEqual(read_ref(parse_qs(parts.query)['ref'][0]), self.user.pk)

    def test_create_checkout_for_tier_without_page(self):
        r = self.client.post(reverse('billing:create_checkout'), {'tier': 'Enterprise'}, follow=True)
        self.assertRedirects(r, reverse('billing:home'))
        self.assertContains(r, 'not available right now')

    def test_create_checkout_rejects_unknown_tier(self):
        r = self.client.post(reverse('billing:create_checkout'), {'tier': 'Gold'})
        self.assertEqual(r.status_code, 400)

    def test_success_page_after_webhook(self):
        with mock.patch('billing.services.send_invoice_email_task'):
            fulfil_checkoutpage_payment(_payment(self.user))
        r = self.client.get(reverse('billing:checkout_success') + '?ref=abc', follow=True)
        self.assertContains(r, '100,000 credits added')

    def test_success_page_before_webhook(self):
        r = self.client.get(reverse('billing:checkout_success'), follow=True)
        self.assertContains(r, 'Payment is being confirmed')


@override_settings(**CP_SETTINGS)
class WebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='buyer@example.com', password='x', name='Buyer')

    def _post(self, event, secret=SECRET, signature=None):
        payload = json.dumps(event).encode()
        if signature is None:
            signature = 'sha256=' + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return self.client.post(
            reverse('billing:checkoutpage_webhook'), data=payload, content_type='application/json',
            HTTP_X_WEBHOOK_SIGNATURE=signature,
        )

    def _event(self, **payment):
        return {'event': 'payment.paid', 'eventId': 'evt_1',
                'data': {'payment': {'id': PAYMENT_ID, 'status': 'paid', **payment}}}

    def test_payment_paid_grants_credits_from_api_record(self):
        # The webhook body claims a bigger page; only the API record counts.
        api_payment = _payment(self.user)
        with mock.patch('billing.views.retrieve_payment', return_value=api_payment) as retrieve, \
             mock.patch('billing.services.send_invoice_email_task'):
            r1 = self._post(self._event(pageSlug='professional'))
            r2 = self._post(self._event())                       # retry
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        retrieve.assert_called_with(PAYMENT_ID)
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 100_000)
        self.assertEqual(Payment.objects.count(), 1)

    def test_bad_signature_rejected(self):
        with mock.patch('billing.views.retrieve_payment') as retrieve:
            r = self._post(self._event(), secret='wrong')
            r_missing = self._post(self._event(), signature='')
        self.assertEqual((r.status_code, r_missing.status_code), (401, 401))
        retrieve.assert_not_called()
        self.assertEqual(Payment.objects.count(), 0)

    @override_settings(CHECKOUTPAGE_WEBHOOK_SECRET='')
    def test_missing_secret_rejects(self):
        r = self._post(self._event(), secret='anything')
        self.assertEqual(r.status_code, 400)

    def test_other_events_acknowledged(self):
        with mock.patch('billing.views.retrieve_payment') as retrieve:
            r = self._post({'event': 'payment.created', 'eventId': 'evt_2', 'data': {'payment': {'id': PAYMENT_ID}}})
        self.assertEqual(r.status_code, 200)
        retrieve.assert_not_called()

    def test_api_outage_asks_for_retry(self):
        with mock.patch('billing.views.retrieve_payment', side_effect=CheckoutPageError('down')):
            r = self._post(self._event())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(Payment.objects.count(), 0)

    def test_unknown_payment_acknowledged(self):
        with mock.patch('billing.views.retrieve_payment', return_value=None):
            r = self._post(self._event())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Payment.objects.count(), 0)

    def test_test_payment_grants_nothing_in_production(self):
        with mock.patch('billing.views.retrieve_payment', return_value=_payment(self.user, livemode=False)):
            r = self._post(self._event())
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 0)
