import logging

from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, Q
from django.shortcuts import render, redirect
from django.utils import timezone

from billing.models import CreditTransaction, PaymentMethod
from scrubber.models import ScrubJob
from .forms import LoginForm, RegisterForm, ProfileForm

logger = logging.getLogger(__name__)


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.get_user())
        return redirect(request.GET.get('next', 'dashboard'))
    return render(request, 'accounts/login.html', {'form': form})


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        promo_code_str = form.cleaned_data.get('promo_code', '').strip().upper()
        promo_code_obj = None

        if promo_code_str:
            from agents.models import AgentPromoCode
            try:
                promo_code_obj = AgentPromoCode.objects.select_related('agent').get(
                    code=promo_code_str,
                    status=AgentPromoCode.Status.ACTIVE,
                )
                if promo_code_obj.expires_at < timezone.now():
                    promo_code_obj.status = AgentPromoCode.Status.EXPIRED
                    promo_code_obj.save(update_fields=['status'])
                    promo_code_obj = None
                    form.add_error('promo_code', 'This promo code has expired. Please request a new one from your agent.')
                    return render(request, 'accounts/register.html', {'form': form})
            except AgentPromoCode.DoesNotExist:
                form.add_error('promo_code', 'Invalid promo code. Please check and try again.')
                return render(request, 'accounts/register.html', {'form': form})

        with transaction.atomic():
            user = form.save()
            if promo_code_obj:
                user.credits += 100_000
                user.save(update_fields=['credits'])
                CreditTransaction.objects.create(
                    user=user,
                    type=CreditTransaction.Type.ADJUSTMENT,
                    amount=100_000,
                    price=0,
                )
                promo_code_obj.status = AgentPromoCode.Status.USED
                promo_code_obj.used_by = user
                promo_code_obj.used_at = timezone.now()
                promo_code_obj.save(update_fields=['status', 'used_by', 'used_at'])

        if promo_code_obj:
            from agents.utils import generate_next_promo_code
            generate_next_promo_code(promo_code_obj.agent)
            messages.success(request, 'Account created! 100,000 credits have been added to your account.')
        else:
            messages.success(request, 'Account created successfully.')

        try:
            from .tasks import send_welcome_email
            send_welcome_email.delay(user.pk, 100_000 if promo_code_obj else 0)
        except Exception:
            logger.exception("Failed to queue welcome email for %s", user.email)

        login(request, user)
        return redirect('dashboard')
    return render(request, 'accounts/register.html', {'form': form})


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

    payment_methods = PaymentMethod.objects.filter(user=request.user)
    credit_history = CreditTransaction.objects.filter(user=request.user).select_related('scrub_job')[:50]

    return render(request, 'accounts/profile.html', {
        'form': profile_form,
        'active_tab': active_tab,
        'payment_methods': payment_methods,
        'credit_history': credit_history,
        'stripe_pk': settings.STRIPE_PUBLISHABLE_KEY,
    })
