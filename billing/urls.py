from django.urls import path
from . import views

app_name = 'billing'

urlpatterns = [
    path('', views.billing_home, name='home'),

    # Invoices
    path('invoices/', views.invoice_list, name='invoice_list'),
    path('invoices/<str:invoice_number>/pdf/', views.invoice_pdf, name='invoice_pdf'),

    # Stripe hosted Checkout
    path('checkout/', views.create_checkout, name='create_checkout'),
    path('checkout/success/', views.checkout_success, name='checkout_success'),
    path('checkout/cancel/', views.checkout_cancel, name='checkout_cancel'),

    # Stripe → app (csrf_exempt; signature-verified inside the view)
    path('webhook/', views.stripe_webhook, name='stripe_webhook'),
]
