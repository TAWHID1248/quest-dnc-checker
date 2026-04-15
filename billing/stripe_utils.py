"""
Stripe API wrappers.

All calls to the Stripe SDK live here so the rest of the app never imports
`stripe` directly.  This makes it trivial to mock in tests.

stripe.api_key is set once at module load from Django settings.
"""

import logging

import stripe
from django.conf import settings

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


# ── Customer ────────────────────────────────────────────────────────────────

def get_or_create_customer(user) -> stripe.Customer:
    """
    Return the Stripe Customer for `user`, creating one if needed.
    Persists the customer ID back to the user row on first creation.
    """
    if user.stripe_customer_id:
        try:
            return stripe.Customer.retrieve(user.stripe_customer_id)
        except stripe.error.InvalidRequestError:
            # Customer was deleted in Stripe dashboard — recreate it
            logger.warning("Stripe customer %s not found, recreating", user.stripe_customer_id)

    customer = stripe.Customer.create(
        email=user.email,
        name=user.display_name,
        metadata={'user_id': str(user.pk)},
    )
    user.stripe_customer_id = customer.id
    user.save(update_fields=['stripe_customer_id'])
    logger.info("Created Stripe customer %s for user %s", customer.id, user.pk)
    return customer


# ── PaymentIntent ────────────────────────────────────────────────────────────

def create_payment_intent(
    amount_usd: int,
    customer_id: str,
    user_id: int,
    tier_name: str,
    credits: int,
) -> stripe.PaymentIntent:
    """
    Create a PaymentIntent for a credit purchase.

    Args:
        amount_usd:  Dollar amount (integer — e.g. 20 for $20.00).
        customer_id: Stripe customer ID.
        user_id:     Django user PK, stored in metadata for the webhook.
        tier_name:   Pricing tier name ('Starter', 'Professional', 'Enterprise').
        credits:     Number of credits being purchased.

    Returns:
        Stripe PaymentIntent object (use `.client_secret` on the frontend).
    """
    return stripe.PaymentIntent.create(
        amount=amount_usd * 100,          # Stripe works in cents
        currency='usd',
        customer=customer_id,
        payment_method_types=['card'],    # Card Element only — no redirect methods
        metadata={
            'user_id':   str(user_id),
            'tier_name': tier_name,
            'credits':   str(credits),
        },
    )


# ── SetupIntent (save card without charging) ─────────────────────────────────

def create_setup_intent(customer_id: str) -> stripe.SetupIntent:
    """
    Create a SetupIntent so the user can save a card without being charged.
    The webhook `setup_intent.succeeded` will save the PaymentMethod locally.
    """
    return stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=['card'],    # Card Element only
        metadata={'save_card': 'true'},
    )


# ── PaymentMethod ────────────────────────────────────────────────────────────

def retrieve_payment_method(pm_id: str) -> stripe.PaymentMethod:
    return stripe.PaymentMethod.retrieve(pm_id)


def detach_payment_method(pm_id: str) -> stripe.PaymentMethod:
    """Detach a payment method from its customer (removes from Stripe)."""
    return stripe.PaymentMethod.detach(pm_id)


# ── Webhook ──────────────────────────────────────────────────────────────────

def construct_webhook_event(payload: bytes, sig_header: str) -> stripe.Event:
    """
    Verify and parse a Stripe webhook event.
    Raises stripe.error.SignatureVerificationError on invalid signature.
    """
    return stripe.Webhook.construct_event(
        payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
    )
