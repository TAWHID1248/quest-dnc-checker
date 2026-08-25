"""
PayPal REST API wrappers (Checkout v2 Orders).

All calls to the PayPal API live here so the rest of the app never talks to
PayPal directly.  Flow: create_order() → buyer approves in the PayPal popup →
capture_order().  Webhook signatures are verified server-side via
verify_webhook_signature().

Access tokens are cached in the Django cache until shortly before expiry.
"""

import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_TOKEN_CACHE_KEY = 'paypal_access_token'
_TIMEOUT = 20  # seconds per API call


class PayPalError(Exception):
    """Raised when a PayPal API call fails."""


def _post(url, **kwargs):
    try:
        return requests.post(url, timeout=_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise PayPalError(f'PayPal API unreachable: {exc}') from exc


def get_access_token() -> str:
    """Client-credentials OAuth token, cached until 60s before expiry."""
    token = cache.get(_TOKEN_CACHE_KEY)
    if token:
        return token

    resp = _post(
        f'{settings.PAYPAL_API_BASE}/v1/oauth2/token',
        auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
        data={'grant_type': 'client_credentials'},
    )
    if resp.status_code != 200:
        logger.error('PayPal token request failed (%s): %s', resp.status_code, resp.text[:500])
        raise PayPalError('Could not authenticate with PayPal.')

    data = resp.json()
    token = data['access_token']
    cache.set(_TOKEN_CACHE_KEY, token, max(int(data.get('expires_in', 3600)) - 60, 60))
    return token


def _auth_headers() -> dict:
    return {
        'Authorization': f'Bearer {get_access_token()}',
        'Content-Type': 'application/json',
    }


# ── Orders ───────────────────────────────────────────────────────────────────

def create_order(amount_usd: int, tier_name: str, credits: int, user_id: int) -> dict:
    """
    Create a one-time PayPal order for a credit purchase.

    custom_id carries "user_id:credits:tier_name" so both the capture endpoint
    and the webhook can resolve who bought what without trusting the client.
    """
    payload = {
        'intent': 'CAPTURE',
        'purchase_units': [{
            'custom_id': f'{user_id}:{credits}:{tier_name}',
            'description': f'CheckDNC {tier_name} plan — {credits:,} credits',
            'amount': {'currency_code': 'USD', 'value': f'{amount_usd}.00'},
        }],
    }
    resp = _post(f'{settings.PAYPAL_API_BASE}/v2/checkout/orders',
                 headers=_auth_headers(), json=payload)
    if resp.status_code not in (200, 201):
        logger.error('PayPal create order failed (%s): %s', resp.status_code, resp.text[:500])
        raise PayPalError('Could not create PayPal order.')
    return resp.json()


def get_order(order_id: str) -> dict:
    try:
        resp = requests.get(f'{settings.PAYPAL_API_BASE}/v2/checkout/orders/{order_id}',
                            headers=_auth_headers(), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise PayPalError(f'PayPal API unreachable: {exc}') from exc
    if resp.status_code != 200:
        logger.error('PayPal get order %s failed (%s): %s', order_id, resp.status_code, resp.text[:500])
        raise PayPalError('Could not retrieve PayPal order.')
    return resp.json()


def capture_order(order_id: str) -> dict:
    """
    Capture an approved order.  If the order was already captured (e.g. the
    buyer double-clicked or the webhook raced us), fetch and return it instead
    so the caller can proceed idempotently.
    """
    resp = _post(f'{settings.PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture',
                 headers=_auth_headers())
    if resp.status_code in (200, 201):
        return resp.json()

    if resp.status_code == 422 and 'ORDER_ALREADY_CAPTURED' in resp.text:
        logger.info('PayPal order %s already captured — fetching it', order_id)
        return get_order(order_id)

    logger.error('PayPal capture %s failed (%s): %s', order_id, resp.status_code, resp.text[:500])
    raise PayPalError('Could not capture PayPal payment.')


# ── Webhook verification ─────────────────────────────────────────────────────

def verify_webhook_signature(headers, event: dict) -> bool:
    """
    Ask PayPal to verify a webhook delivery's signature.
    `headers` is the Django request.headers mapping.
    """
    payload = {
        'transmission_id':   headers.get('Paypal-Transmission-Id'),
        'transmission_time': headers.get('Paypal-Transmission-Time'),
        'cert_url':          headers.get('Paypal-Cert-Url'),
        'auth_algo':         headers.get('Paypal-Auth-Algo'),
        'transmission_sig':  headers.get('Paypal-Transmission-Sig'),
        'webhook_id':        settings.PAYPAL_WEBHOOK_ID,
        'webhook_event':     event,
    }
    resp = _post(f'{settings.PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature',
                 headers=_auth_headers(), json=payload)
    if resp.status_code != 200:
        logger.error('PayPal webhook verification call failed (%s): %s',
                     resp.status_code, resp.text[:500])
        return False
    return resp.json().get('verification_status') == 'SUCCESS'
