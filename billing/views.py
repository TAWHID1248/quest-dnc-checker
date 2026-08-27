from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import Payment

# ── Pricing tiers ────────────────────────────────────────────────────────────

PRICING_TIERS = [
    {
        'name': 'Starter',
        'price': 10,
        'credits': 100_000,
        'credits_display': '100,000',
        'per_k': '$0.10',
        'highlighted': False,
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Email Support',
        ],
    },
    {
        'name': 'Professional',
        'price': 20,
        'credits': 250_000,
        'credits_display': '250,000',
        'per_k': '$0.08',
        'highlighted': True,
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Priority Email Support',
            'Bulk Processing',
        ],
    },
    {
        'name': 'Enterprise',
        'price': 50,
        'credits': 1_000_000,
        'credits_display': '1,000,000',
        'per_k': '$0.05',
        'highlighted': False,
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Dedicated Support',
            'API Access (coming soon)',
            'Volume Discounts',
        ],
    },
]

# ── Billing home ─────────────────────────────────────────────────────────────

@login_required
def billing_home(request):
    recent_payments = Payment.objects.filter(user=request.user).order_by('-created_at')[:10]
    return render(request, 'billing/home.html', {
        'tiers': PRICING_TIERS,
        'recent_payments': recent_payments,
    })
