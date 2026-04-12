from django.urls import path
from . import views

app_name = 'admin_panel'

urlpatterns = [
    # Dashboard
    path('', views.dashboard, name='home'),

    # Client management
    path('clients/', views.client_list, name='client_list'),
    path('clients/<int:user_id>/', views.client_detail, name='client_detail'),
    path('clients/<int:user_id>/toggle/', views.client_toggle, name='client_toggle'),
    path('clients/<int:user_id>/credits/', views.client_adjust_credits, name='client_credits'),

    # Support tickets
    path('tickets/', views.ticket_list, name='ticket_list'),
    path('tickets/<int:ticket_id>/status/', views.ticket_update_status, name='ticket_status'),

    # Payments
    path('payments/', views.payment_list, name='payment_list'),
]
