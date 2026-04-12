from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from billing.models import Payment


PRICING_TIERS = [
    {
        'name': 'Starter',
        'price': 20,
        'credits': 100_000,
        'credits_display': '100,000',
        'per_k': '$0.20',
        'color': 'secondary',
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Email Support',
        ],
        'highlighted': False,
    },
    {
        'name': 'Professional',
        'price': 50,
        'credits': 500_000,
        'credits_display': '500,000',
        'per_k': '$0.10',
        'color': 'primary',
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'Litigator List Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Priority Email Support',
            'Bulk Processing',
        ],
        'highlighted': True,
    },
    {
        'name': 'Enterprise',
        'price': 75,
        'credits': 1_000_000,
        'credits_display': '1,000,000',
        'per_k': '$0.075',
        'color': 'success',
        'features': [
            'Federal DNC Scrubbing',
            'State DNC Scrubbing',
            'Litigator List Scrubbing',
            'Wireless Scrubbing',
            'CSV & TXT Upload',
            'Downloadable Results',
            'Dedicated Support',
            'API Access (coming soon)',
            'Volume Discounts',
        ],
        'highlighted': False,
    },
]


@login_required
def billing_home(request):
    recent_payments = Payment.objects.filter(user=request.user).order_by('-created_at')[:5]
    return render(request, 'billing/home.html', {
        'tiers': PRICING_TIERS,
        'recent_payments': recent_payments,
    })
