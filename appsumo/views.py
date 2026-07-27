"""
AppSumo Partner API endpoints.

  POST /appsumo/webhook/   — license lifecycle events from AppSumo
  GET  /appsumo/redirect/  — OAuth redirect after a buyer activates on AppSumo

Webhook requests are authenticated with HMAC-SHA256 over
"<X-Appsumo-Timestamp><raw body>" keyed with the AppSumo API key.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import AppSumoLicense, AppSumoWebhookEvent
from .services import (
    AppSumoError,
    exchange_code_for_license_key,
    link_license_to_user,
    revoke_license_credits,
    sync_license_credits,
)

logger = logging.getLogger(__name__)


def _signature_valid(request):
    signature = request.headers.get('X-Appsumo-Signature', '')
    timestamp = request.headers.get('X-Appsumo-Timestamp', '')
    if not settings.APPSUMO_API_KEY:
        logger.error("APPSUMO_API_KEY not configured; rejecting webhook")
        return False
    message = timestamp.encode() + request.body
    expected = hmac.new(settings.APPSUMO_API_KEY.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


@csrf_exempt
@require_POST
def webhook(request):
    if not _signature_valid(request):
        return JsonResponse({'success': False, 'error': 'invalid signature'}, status=403)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'invalid JSON'}, status=400)

    event = payload.get('event', '')
    license_key = payload.get('license_key', '')
    is_test = bool(payload.get('test'))

    AppSumoWebhookEvent.objects.create(
        license_key=license_key, event=event, test=is_test, payload=payload,
    )
    logger.info("AppSumo webhook: %s license=%s test=%s", event, license_key, is_test)

    # Validation/test events: acknowledge without touching real data
    if is_test:
        return JsonResponse({'success': True, 'event': event})

    if event == 'purchase':
        AppSumoLicense.objects.get_or_create(
            license_key=license_key,
            defaults={'tier': payload.get('tier')},
        )

    elif event == 'activate':
        lic, _ = AppSumoLicense.objects.get_or_create(license_key=license_key)
        lic.tier = payload.get('tier') or lic.tier
        lic.status = AppSumoLicense.Status.ACTIVE
        if not lic.activated_at:
            lic.activated_at = timezone.now()
        lic.save(update_fields=['tier', 'status', 'activated_at', 'updated_at'])
        sync_license_credits(lic)

    elif event in ('upgrade', 'downgrade'):
        prev_key = payload.get('prev_license_key', '')
        lic, _ = AppSumoLicense.objects.get_or_create(license_key=license_key)
        lic.tier = payload.get('tier') or lic.tier
        lic.status = AppSumoLicense.Status.ACTIVE
        lic.prev_license_key = prev_key
        # Carry the user + granted-credit tally over from the old license so
        # an upgrade only grants the tier difference (no clawback on downgrade)
        prev = AppSumoLicense.objects.filter(license_key=prev_key).first() if prev_key else None
        if prev:
            lic.user = lic.user or prev.user
            lic.credits_granted = max(lic.credits_granted, prev.credits_granted)
            prev.status = AppSumoLicense.Status.DEACTIVATED
            prev.credits_granted = 0
            prev.save(update_fields=['status', 'credits_granted', 'updated_at'])
        lic.save()
        sync_license_credits(lic)

    elif event == 'deactivate':
        lic = AppSumoLicense.objects.filter(license_key=license_key).first()
        if lic:
            revoke_license_credits(lic)
            lic.status = AppSumoLicense.Status.DEACTIVATED
            lic.save(update_fields=['status', 'updated_at'])

    # 'migrate' and anything unknown: logged above, acknowledged below

    return JsonResponse({'success': True, 'event': event})


@require_GET
def oauth_redirect(request):
    code = request.GET.get('code')

    # AppSumo validates this URL with a bare GET — must return 200 OK
    if not code:
        return HttpResponse('OK')

    redirect_uri = request.build_absolute_uri(request.path)
    try:
        license_key = exchange_code_for_license_key(code, redirect_uri)
    except AppSumoError as exc:
        messages.error(request, str(exc))
        return redirect('accounts:login')

    if request.user.is_authenticated:
        try:
            link_license_to_user(license_key, request.user)
        except AppSumoError as exc:
            messages.error(request, str(exc))
            return redirect('dashboard')
        messages.success(request, 'Your AppSumo license is activated — credits have been added to your account.')
        return redirect('dashboard')

    # Not logged in: stash the key, send the buyer to register (or log in)
    request.session['appsumo_license_key'] = license_key
    messages.info(request, 'AppSumo purchase verified! Create your account (or log in) to activate your credits.')
    return redirect('accounts:register')
