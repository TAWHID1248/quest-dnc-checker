from django.urls import path
from . import views

app_name = 'billing'

urlpatterns = [
    path('', views.billing_home, name='home'),

    # PayPal AJAX endpoints
    path('paypal/create-order/',  views.create_paypal_order_view,  name='paypal_create_order'),
    path('paypal/capture-order/', views.capture_paypal_order_view, name='paypal_capture_order'),

    # PayPal webhook (csrf_exempt inside the view)
    path('paypal/webhook/', views.paypal_webhook, name='paypal_webhook'),
]
