"""
Checkout Page (checkoutpage.com) wrappers.

Credit purchases are sold on hosted Checkout Page pages, one page per pricing
tier. The buyer is sent to the page for their tier with a signed `ref` query
parameter that identifies their account; Checkout Page stores query parameters
on the payment, so the webhook can tell who paid without trusting the browser.

Everything that talks to Checkout Page lives here:
  * building the hosted page URL for a user + tier
  * verifying webhook signatures (HMAC-SHA256 of the raw body)
  * fetching a payment from the REST API (the authoritative record)

Docs: https://checkoutpage.com/docs/api and /docs/api/v1/webhooks/signature
"""

import hashlib
import hmac
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from django.conf import settings
from django.core import signing

logger = logging.getLogger(__name__)

API_BASE = 'https://api.checkoutpage.com/v1'
API_TIMEOUT = 15                       # seconds
REF_PARAM = 'ref'                      # query parameter carrying the signed user reference
REF_SALT = 'billing.checkoutpage.ref'
REF_MAX_AGE = 7 * 24 * 3600            # a checkout link stays valid for a week


class CheckoutPageError(Exception):
    """The Checkout Page API could not be reached or returned an error."""


# ── Configuration ────────────────────────────────────────────────────────────

def page_urls() -> dict:
    """{tier name: hosted page URL} for the tiers that have a page configured."""
    return {name: url for name, url in settings.CHECKOUTPAGE_PAGE_URLS.items() if url}


def checkoutpage_enabled() -> bool:
    """True when the API key, webhook secret and at least one tier page are set."""
    return bool(
        settings.CHECKOUTPAGE_API_KEY
        and settings.CHECKOUTPAGE_WEBHOOK_SECRET
        and page_urls()
    )


def _slug(url: str) -> str:
    """Last path segment of a page URL, e.g. https://x.checkoutpage.com/starter → 'starter'."""
    return urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1].lower()


def tier_name_for_page(page_slug: str) -> str | None:
    """Map the slug of the page a payment was made on back to a pricing tier name."""
    page_slug = (page_slug or '').lower()
    if not page_slug:
        return None
    for name, url in page_urls().items():
        if _slug(url) == page_slug:
            return name
    return None


# ── Hosted page URL ──────────────────────────────────────────────────────────

def make_ref(user) -> str:
    """Signed, tamper-proof reference to `user` (safe to expose in a URL)."""
    return signing.dumps({'u': user.pk}, salt=REF_SALT, compress=True)


def read_ref(token: str):
    """Return the user id inside a `ref` token, or None if missing/forged/expired."""
    if not token:
        return None
    try:
        return signing.loads(token, salt=REF_SALT, max_age=REF_MAX_AGE).get('u')
    except signing.BadSignature:
        return None


def build_checkout_url(user, tier: dict) -> str | None:
    """
    Hosted page URL for `tier`, tagged with the buyer's signed reference and
    their email prefilled. None if the tier has no page configured.
    """
    base = page_urls().get(tier['name'])
    if not base:
        return None
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[REF_PARAM] = make_ref(user)
    query['customer_email'] = user.email      # prefills the page's email field
    return urlunsplit(parts._replace(query=urlencode(query)))


# ── Webhook ──────────────────────────────────────────────────────────────────

def verify_webhook_signature(payload: bytes, signature_header: str) -> bool:
    """
    Check the `x-webhook-signature` header ("sha256=<hex>") against an
    HMAC-SHA256 of the raw request body keyed with the webhook secret.
    """
    secret = settings.CHECKOUTPAGE_WEBHOOK_SECRET
    if not secret or not signature_header:
        return False
    algorithm, _, received = signature_header.strip().partition('=')
    if algorithm != 'sha256' or not received:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.strip().lower())


# ── REST API ─────────────────────────────────────────────────────────────────

def _get_payment(payment_id: str, livemode: bool | None):
    params = {} if livemode is None else {'livemode': 'true' if livemode else 'false'}
    try:
        return requests.get(
            f'{API_BASE}/payments/{payment_id}',
            headers={'Authorization': f'Bearer {settings.CHECKOUTPAGE_API_KEY}'},
            params=params,
            timeout=API_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise CheckoutPageError(f'Checkout Page API unreachable: {exc}') from exc


def retrieve_payment(payment_id: str) -> dict | None:
    """
    Fetch a payment by id. Returns the payment dict, or None if it does not
    exist. Raises CheckoutPageError on network/API failures so the caller can
    ask Checkout Page to retry the webhook.

    Test-mode payments are only looked up when CHECKOUTPAGE_ALLOW_TEST_PAYMENTS
    is on (they are free, so production must never see them).
    """
    response = _get_payment(payment_id, livemode=None)
    if response.status_code == 404 and settings.CHECKOUTPAGE_ALLOW_TEST_PAYMENTS:
        response = _get_payment(payment_id, livemode=False)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise CheckoutPageError(
            f'Checkout Page API returned {response.status_code} for payment {payment_id}'
        )
    try:
        return response.json().get('data') or None
    except ValueError as exc:
        raise CheckoutPageError('Checkout Page API returned invalid JSON') from exc
