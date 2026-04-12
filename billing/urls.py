from django.urls import path
from . import views

app_name = 'billing'

urlpatterns = [
    path('', views.billing_home, name='home'),

    # Stripe AJAX endpoints
    path('create-payment-intent/', views.create_payment_intent_view, name='create_payment_intent'),
    path('create-setup-intent/',   views.create_setup_intent_view,   name='create_setup_intent'),

    # Payment method management
    path('payment-method/<int:pm_id>/delete/',      views.delete_payment_method,      name='pm_delete'),
    path('payment-method/<int:pm_id>/set-default/', views.set_default_payment_method, name='pm_set_default'),

    # Stripe webhook (csrf_exempt inside the view)
    path('webhook/', views.stripe_webhook, name='webhook'),
]
