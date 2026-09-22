"""
Stripe API wrappers.

All calls to the Stripe SDK live here so the rest of the app never imports
`stripe` directly. Credit purchases use hosted Stripe Checkout: the customer
enters card details on Stripe's page, so no card data ever touches this app.

Sandbox vs live is controlled entirely by which keys are configured
(pk_test_/sk_test_ for the sandbox, pk_live_/sk_live_ for production).
"""

import logging

import stripe
from django.conf import settings

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def stripe_enabled() -> bool:
    """True when both a publishable and a secret key are configured."""
    return bool(settings.STRIPE_SECRET_KEY and settings.STRIPE_PUBLISHABLE_KEY)


# ── Customer ────────────────────────────────────────────────────────────────

def get_or_create_customer(user):
    """
    Return the Stripe Customer ID for `user`, creating the customer if needed.
    Persists the ID back to the user row on first creation. A stale ID (the
    customer was deleted in the Stripe dashboard) is replaced transparently.
    """
    if user.stripe_customer_id:
        try:
            customer = stripe.Customer.retrieve(user.stripe_customer_id)
            if not customer.get('deleted'):
                return customer.id
        except stripe.error.InvalidRequestError:
            logger.warning("Stripe customer %s not found, recreating", user.stripe_customer_id)

    customer = stripe.Customer.create(
        email=user.email,
        name=user.display_name,
        metadata={'user_id': str(user.pk)},
    )
    user.stripe_customer_id = customer.id
    user.save(update_fields=['stripe_customer_id'])
    logger.info("Created Stripe customer %s for user %s", customer.id, user.pk)
    return customer.id


# ── Checkout Session ─────────────────────────────────────────────────────────

def create_checkout_session(user, tier: dict, success_url: str, cancel_url: str):
    """
    Create a hosted Checkout Session for a one-off credit purchase.

    `tier` is one of billing.views.PRICING_TIERS. The user id, tier name and
    credit amount are stored in metadata so the webhook can grant credits
    without trusting anything sent from the browser.
    """
    customer_id = get_or_create_customer(user)
    metadata = {
        'user_id':   str(user.pk),
        'tier_name': tier['name'],
        'credits':   str(tier['credits']),
    }
    return stripe.checkout.Session.create(
        mode='payment',
        customer=customer_id,
        payment_method_types=['card'],
        line_items=[{
            'quantity': 1,
            'price_data': {
                'currency': 'usd',
                'unit_amount': int(tier['price']) * 100,   # cents
                'product_data': {
                    'name': f"CheckDNC {tier['name']} plan",
                    'description': f"{tier['credits_display']} DNC scrubbing credits",
                },
            },
        }],
        metadata=metadata,
        payment_intent_data={'metadata': metadata},
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user.pk),
    )


def retrieve_checkout_session(session_id: str):
    """Fetch a Checkout Session (used by the success page as a webhook fallback)."""
    return stripe.checkout.Session.retrieve(session_id)


# ── Webhook ──────────────────────────────────────────────────────────────────

def construct_webhook_event(payload: bytes, sig_header: str):
    """
    Verify and parse a Stripe webhook event.
    Raises stripe.error.SignatureVerificationError on an invalid signature.
    """
    return stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
