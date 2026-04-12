from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.shortcuts import render, redirect

from billing.models import CreditTransaction, PaymentMethod
from scrubber.models import ScrubJob
from .forms import LoginForm, RegisterForm, ProfileForm


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
        user = form.save()
        login(request, user)
        messages.success(request, 'Account created successfully.')
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
    })
