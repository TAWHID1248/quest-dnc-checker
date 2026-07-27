"""
AppSumo Licensing API helpers.

OAuth token exchange + license retrieval (docs.licensing.appsumo.com) and
credit-granting logic shared by the OAuth redirect view and webhook handlers.
"""

import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from billing.models import CreditTransaction

from .models import AppSumoLicense

logger = logging.getLogger(__name__)
User = get_user_model()

TOKEN_URL = 'https://appsumo.com/openid/token/'
LICENSE_URL = 'https://appsumo.com/openid/license_key/'


class AppSumoError(Exception):
    pass


def exchange_code_for_license_key(code, redirect_uri):
    """OAuth code -> access token -> license key. Raises AppSumoError."""
    try:
        resp = requests.post(TOKEN_URL, data={
            'client_id': settings.APPSUMO_CLIENT_ID,
            'client_secret': settings.APPSUMO_CLIENT_SECRET,
            'code': code,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        }, timeout=15)
        resp.raise_for_status()
        access_token = resp.json()['access_token']

        resp = requests.get(LICENSE_URL, params={'access_token': access_token}, timeout=15)
        resp.raise_for_status()
        return resp.json()['license_key']
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.error("AppSumo OAuth exchange failed: %s", exc)
        raise AppSumoError('Could not verify your AppSumo purchase. Please try again.') from exc


def tier_credits(tier):
    """Credits included in an AppSumo tier (APPSUMO_TIER_CREDITS setting)."""
    if not tier:
        return Decimal(0)
    return Decimal(settings.APPSUMO_TIER_CREDITS.get(int(tier), 0))


def sync_license_credits(license_obj):
    """
    Grant the user any credits owed for this license (idempotent top-up).

    target = credits for the license's tier; if more than already granted and
    a user is linked, grant the difference. Called from both the OAuth link
    and webhook handlers, so ordering between the two doesn't matter.
    """
    if not license_obj.user or license_obj.status == AppSumoLicense.Status.DEACTIVATED:
        return

    target = tier_credits(license_obj.tier)
    with transaction.atomic():
        lic = AppSumoLicense.objects.select_for_update().get(pk=license_obj.pk)
        diff = target - lic.credits_granted
        if diff <= 0:
            return
        user = User.objects.select_for_update().get(pk=lic.user_id)
        user.credits += diff
        user.save(update_fields=['credits'])
        lic.credits_granted = target
        lic.save(update_fields=['credits_granted', 'updated_at'])
        CreditTransaction.objects.create(
            user=user,
            type=CreditTransaction.Type.PURCHASE,
            amount=diff,
            price=0,
        )
    logger.info("AppSumo: granted %s credits to %s (license %s, tier %s)",
                diff, license_obj.user.email, license_obj.license_key, license_obj.tier)


def link_license_to_user(license_key, user):
    """Attach a redeemed license to a user account and grant its credits."""
    lic, _ = AppSumoLicense.objects.get_or_create(license_key=license_key)
    if lic.user_id and lic.user_id != user.pk:
        raise AppSumoError('This AppSumo license is already linked to another account.')
    if not lic.user_id:
        lic.user = user
        if not lic.activated_at:
            lic.activated_at = timezone.now()
        lic.save(update_fields=['user', 'activated_at', 'updated_at'])
    sync_license_credits(lic)
    return lic


def link_pending_session_license(request, user):
    """
    Link a license key stashed in the session (OAuth redirect hit while the
    buyer was logged out). Called from login/register views after login().
    """
    license_key = request.session.pop('appsumo_license_key', None)
    if not license_key:
        return None
    try:
        return link_license_to_user(license_key, user)
    except AppSumoError:
        logger.exception("AppSumo: failed to link pending license %s to %s",
                         license_key, user.email)
        return None


def revoke_license_credits(license_obj):
    """
    Claw back credits on deactivation (refund). Removes up to the amount this
    license granted, never taking the balance below zero.
    """
    if not license_obj.user or license_obj.credits_granted <= 0:
        return
    with transaction.atomic():
        lic = AppSumoLicense.objects.select_for_update().get(pk=license_obj.pk)
        user = User.objects.select_for_update().get(pk=lic.user_id)
        clawback = min(user.credits, lic.credits_granted)
        if clawback > 0:
            user.credits -= clawback
            user.save(update_fields=['credits'])
            CreditTransaction.objects.create(
                user=user,
                type=CreditTransaction.Type.REFUND,
                amount=-clawback,
                price=0,
            )
        lic.credits_granted = 0
        lic.save(update_fields=['credits_granted', 'updated_at'])
    logger.info("AppSumo: revoked %s credits from %s (license %s deactivated)",
                clawback, license_obj.user.email, license_obj.license_key)
