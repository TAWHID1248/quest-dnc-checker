import logging
import secrets
from urllib.parse import urlencode

import requests
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from billing.models import CreditTransaction
from scrubber.models import ScrubJob
from .forms import LoginForm, RegisterForm, ProfileForm
from .services import (
    PromoCodeError,
    grant_signup_credits,
    queue_welcome_email,
    resolve_promo_code,
    signup_success_message,
)

User = get_user_model()

logger = logging.getLogger(__name__)


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.get_user())
        from appsumo.services import link_pending_session_license
        if link_pending_session_license(request, request.user):
            messages.success(request, 'Your AppSumo license is activated — credits have been added to your account.')
        return redirect(request.GET.get('next', 'dashboard'))
    return render(request, 'accounts/login.html', {'form': form})


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            promo_code_obj = resolve_promo_code(form.cleaned_data.get('promo_code'))
        except PromoCodeError as exc:
            form.add_error('promo_code', str(exc))
            return render(request, 'accounts/register.html', {'form': form})

        with transaction.atomic():
            user = form.save()
            signup_credits, promo_credits = grant_signup_credits(user, promo_code_obj)

        messages.success(request, signup_success_message(signup_credits, promo_credits))
        queue_welcome_email(user, promo_credits, signup_credits)

        login(request, user)
        from appsumo.services import link_pending_session_license
        if link_pending_session_license(request, user):
            messages.success(request, 'Your AppSumo license is activated — credits have been added to your account.')
        return redirect('dashboard')
    return render(request, 'accounts/register.html', {'form': form})


# ── Google OAuth 2.0 ─────────────────────────────────────────────────────────

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'


def _google_redirect_uri(request):
    return request.build_absolute_uri(reverse('accounts:google_callback'))


def google_login(request):
    """Start the Google sign-in flow. Accepts ?promo=CODE and ?next=/path."""
    if request.user.is_authenticated:
        return redirect('dashboard')
    if not settings.GOOGLE_CLIENT_ID:
        messages.error(request, 'Google sign-in is not configured.')
        return redirect('accounts:login')

    state = secrets.token_urlsafe(32)
    request.session['google_oauth_state'] = state
    request.session['google_oauth_promo'] = (request.GET.get('promo') or '').strip().upper()[:20]
    next_url = request.GET.get('next', '')
    request.session['google_oauth_next'] = next_url if url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ) else ''

    params = {
        'client_id': settings.GOOGLE_CLIENT_ID,
        'redirect_uri': _google_redirect_uri(request),
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'access_type': 'online',
        'prompt': 'select_account',
    }
    return redirect(f'{GOOGLE_AUTH_URL}?{urlencode(params)}')


def google_callback(request):
    """Handle Google's redirect: verify state, exchange code, sign in or sign up."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    expected_state = request.session.pop('google_oauth_state', None)
    promo_code_str = request.session.pop('google_oauth_promo', '')
    next_url = request.session.pop('google_oauth_next', '') or 'dashboard'

    if request.GET.get('error'):
        logger.info("Google sign-in cancelled/denied: %s", request.GET['error'])
        messages.error(request, 'Google sign-in was cancelled.')
        return redirect('accounts:login')

    code = request.GET.get('code')
    state = request.GET.get('state')
    if not code or not state or not expected_state or not secrets.compare_digest(state, expected_state):
        messages.error(request, 'Google sign-in failed (invalid state). Please try again.')
        return redirect('accounts:login')

    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            'code': code,
            'client_id': settings.GOOGLE_CLIENT_ID,
            'client_secret': settings.GOOGLE_CLIENT_SECRET,
            'redirect_uri': _google_redirect_uri(request),
            'grant_type': 'authorization_code',
        }, timeout=15)
        resp.raise_for_status()
        access_token = resp.json()['access_token']

        resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=15,
        )
        resp.raise_for_status()
        info = resp.json()
        email = info['email']
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.error("Google OAuth exchange failed: %s", exc)
        messages.error(request, 'Could not sign in with Google. Please try again.')
        return redirect('accounts:login')

    if not info.get('email_verified', False):
        messages.error(request, 'Your Google account email is not verified. Please use another sign-in method.')
        return redirect('accounts:login')

    email = User.objects.normalize_email(email).lower()
    user = User.objects.filter(email__iexact=email).first()
    created = False

    if user is None:
        try:
            promo_code_obj = resolve_promo_code(promo_code_str)
        except PromoCodeError as exc:
            # Don't block the signup over a bad promo code — create the account
            # without the bonus and tell the user why.
            promo_code_obj = None
            messages.warning(request, f'{exc} Your account was created without the promo bonus.')

        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=None,  # unusable password; user can set one via "Forgot password"
                name=(info.get('name') or '').strip()[:150],
            )
            signup_credits, promo_credits = grant_signup_credits(user, promo_code_obj)
        created = True
        messages.success(request, signup_success_message(signup_credits, promo_credits))
        queue_welcome_email(user, promo_credits, signup_credits)
        logger.info("Created account via Google sign-in: %s", email)

    if not user.is_active:
        messages.error(request, 'This account has been deactivated.')
        return redirect('accounts:login')

    login(request, user)
    from appsumo.services import link_pending_session_license
    if link_pending_session_license(request, user):
        messages.success(request, 'Your AppSumo license is activated — credits have been added to your account.')
    return redirect('dashboard' if created else next_url)


def logout_view(request):
    logout(request)
    return redirect('accounts:login')


@login_required
def dashboard_view(request):
    jobs = ScrubJob.objects.filter(user=request.user)
    totals = jobs.aggregate(
        total_numbers=Sum('total'),
        total_clean=Sum('clean'),
        total_dnc=Sum('dnc'),
    )
    total_numbers = totals['total_numbers'] or 0
    total_clean = totals['total_clean'] or 0
    total_dnc = totals['total_dnc'] or 0
    clean_rate = round((total_clean / total_numbers * 100), 1) if total_numbers else 0
    recent_jobs = jobs.select_related('user')[:8]
    return render(request, 'dashboard.html', {
        'total_numbers': total_numbers,
        'total_clean': total_clean,
        'total_dnc': total_dnc,
        'clean_rate': clean_rate,
        'recent_jobs': recent_jobs,
    })


@login_required
def profile_view(request):
    active_tab = request.GET.get('tab', 'profile')
    profile_form = ProfileForm(request.POST or None, instance=request.user)

    if request.method == 'POST' and active_tab == 'profile' and profile_form.is_valid():
        profile_form.save()
        messages.success(request, 'Profile updated successfully.')
        return redirect(f"{request.path}?tab=profile")

    credit_history = CreditTransaction.objects.filter(user=request.user).select_related('scrub_job')[:50]

    return render(request, 'accounts/profile.html', {
        'form': profile_form,
        'active_tab': active_tab,
        'credit_history': credit_history,
    })
