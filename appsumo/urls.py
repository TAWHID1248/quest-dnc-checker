from django.urls import path
from . import views

app_name = 'appsumo'

urlpatterns = [
    path('webhook/', views.webhook, name='webhook'),
    path('redirect/', views.oauth_redirect, name='oauth_redirect'),
]
