"""Stripe Checkout integration tests (Stripe SDK is mocked; no network)."""
import json
import time
from decimal import Decimal
from unittest import mock

import stripe
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import CreditTransaction, Invoice, Payment
from .services import fulfil_checkout_session

User = get_user_model()


def _session(session_id='cs_test_123', user_id=1, credits=100000, amount=1000, status='paid'):
    return {
        'id': session_id,
        'object': 'checkout.session',
        'payment_status': status,
        'amount_total': amount,
        'payment_intent': 'pi_test_123',
        'client_reference_id': str(user_id),
        'metadata': {'user_id': str(user_id), 'tier_name': 'Starter', 'credits': str(credits)},
    }


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class FulfilmentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='buyer@example.com', password='x', name='Buyer')
        self.user.credits = 500
        self.user.save()

    def test_grants_credits_once(self):
        with mock.patch('billing.services.send_invoice_email_task') as task, \
             self.captureOnCommitCallbacks(execute=True):
            p1 = fulfil_checkout_session(_session(user_id=self.user.pk))
            p2 = fulfil_checkout_session(_session(user_id=self.user.pk))   # webhook retry
        self.user.refresh_from_db()
        self.assertEqual(p1.pk, p2.pk)
        self.assertEqual(self.user.credits, 100500)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(p1.amount, Decimal('10.00'))
        self.assertEqual(p1.provider, 'stripe')
        self.assertEqual(p1.status, 'completed')
        self.assertEqual(CreditTransaction.objects.filter(type='purchase').count(), 1)
        inv = Invoice.objects.get()
        self.assertEqual(inv.amount, Decimal('10.00'))
        task.delay.assert_called_once_with(inv.pk)

    def test_unpaid_session_ignored(self):
        self.assertIsNone(fulfil_checkout_session(_session(user_id=self.user.pk, status='unpaid')))
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 500)
        self.assertEqual(Payment.objects.count(), 0)

    def test_bad_metadata_ignored(self):
        s = _session(user_id=self.user.pk)
        s['metadata'] = {}
        s['client_reference_id'] = None
        self.assertIsNone(fulfil_checkout_session(s))
        self.assertEqual(Payment.objects.count(), 0)


class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='buyer@example.com', password='x', name='Buyer')
        self.client.login(username='buyer@example.com', password='x')

    def test_modal_shows_card_button_when_configured(self):
        r = self.client.get(reverse('billing:home'))
        self.assertContains(r, 'Pay with Card')
        self.assertContains(r, reverse('billing:create_checkout'))

    @override_settings(STRIPE_PUBLISHABLE_KEY='', STRIPE_SECRET_KEY='')
    def test_modal_hides_card_button_without_keys(self):
        r = self.client.get(reverse('billing:home'))
        self.assertNotContains(r, 'Pay with Card')
        self.assertContains(r, 'Coming soon')

    def test_create_checkout_redirects_to_stripe(self):
        fake = mock.Mock(url='https://checkout.stripe.com/c/pay/cs_test_123')
        with mock.patch('billing.views.create_checkout_session', return_value=fake) as create:
            r = self.client.post(reverse('billing:create_checkout'), {'tier': 'Professional'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], fake.url)
        args = create.call_args.args
        self.assertEqual(args[0], self.user)
        self.assertEqual(args[1]['credits'], 250_000)
        self.assertIn('/billing/checkout/success/?session_id={CHECKOUT_SESSION_ID}', args[2])
        self.assertIn('/billing/checkout/cancel/', args[3])

    def test_create_checkout_rejects_unknown_tier(self):
        r = self.client.post(reverse('billing:create_checkout'), {'tier': 'Gold'})
        self.assertEqual(r.status_code, 400)

    def test_create_checkout_requires_post(self):
        r = self.client.get(reverse('billing:create_checkout'))
        self.assertEqual(r.status_code, 405)

    def test_success_page_grants_credits_as_fallback(self):
        with mock.patch('billing.views.retrieve_checkout_session', return_value=_session(user_id=self.user.pk)), \
             mock.patch('billing.services.send_invoice_email_task'):
            r = self.client.get(reverse('billing:checkout_success') + '?session_id=cs_test_123', follow=True)
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 100_000)
        self.assertContains(r, '100,000 credits added')

    def test_success_page_handles_real_sdk_object(self):
        """retrieve_checkout_session must hand the view a dict even when Stripe returns an SDK object."""
        sdk_obj = stripe.checkout.Session.construct_from(_session(user_id=self.user.pk), 'sk_test_dummy')
        with mock.patch('billing.stripe_utils.stripe.checkout.Session.retrieve', return_value=sdk_obj), \
             mock.patch('billing.services.send_invoice_email_task'):
            r = self.client.get(reverse('billing:checkout_success') + '?session_id=cs_test_123', follow=True)
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 100_000)
        self.assertContains(r, '100,000 credits added')

    def test_success_page_rejects_other_users_session(self):
        other = User.objects.create_user(email='other@example.com', password='x', name='Other')
        with mock.patch('billing.views.retrieve_checkout_session', return_value=_session(user_id=other.pk)), \
             mock.patch('billing.services.send_invoice_email_task'):
            self.client.get(reverse('billing:checkout_success') + '?session_id=cs_test_123')
        other.refresh_from_db()
        self.assertEqual(other.credits, 0)
        self.assertEqual(Payment.objects.count(), 0)

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        r = self.client.post(reverse('billing:create_checkout'), {'tier': 'Starter'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/accounts/login/', r['Location'])


class WebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='buyer@example.com', password='x', name='Buyer')

    def _signed_post(self, event, secret='whsec_dummy'):
        payload = json.dumps(event)
        ts = int(time.time())
        sig = stripe.WebhookSignature._compute_signature(f'{ts}.{payload}', secret)
        return self.client.post(
            reverse('billing:stripe_webhook'), data=payload, content_type='application/json',
            HTTP_STRIPE_SIGNATURE=f't={ts},v1={sig}',
        )

    def test_checkout_completed_grants_credits(self):
        event = {'id': 'evt_1', 'type': 'checkout.session.completed',
                 'data': {'object': _session(user_id=self.user.pk, credits=1_000_000, amount=5000)}}
        with mock.patch('billing.services.send_invoice_email_task'):
            r = self._signed_post(event)
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 1_000_000)
        self.assertEqual(Payment.objects.get().amount, Decimal('50.00'))

    def test_sdk_object_payload_is_handled(self):
        """fulfil_checkout_session must accept a stripe.StripeObject, not just dicts."""
        obj = stripe.checkout.Session.construct_from(_session(user_id=self.user.pk), 'sk_test_dummy')
        with mock.patch('billing.services.send_invoice_email_task'):
            payment = fulfil_checkout_session(obj)
        self.assertIsNotNone(payment)
        self.assertEqual(payment.stripe_pi_id, 'pi_test_123')

    def test_bad_signature_rejected(self):
        event = {'id': 'evt_1', 'type': 'checkout.session.completed',
                 'data': {'object': _session(user_id=self.user.pk)}}
        r = self._signed_post(event, secret='whsec_wrong')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Payment.objects.count(), 0)

    @override_settings(STRIPE_WEBHOOK_SECRET='')
    def test_missing_secret_rejects(self):
        r = self.client.post(reverse('billing:stripe_webhook'), data='{}', content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_unrelated_event_acknowledged(self):
        event = {'id': 'evt_2', 'type': 'customer.created', 'data': {'object': {'id': 'cus_1'}}}
        r = self._signed_post(event)
        self.assertEqual(r.status_code, 200)
