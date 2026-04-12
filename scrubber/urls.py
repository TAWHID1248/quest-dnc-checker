from django.urls import path
from . import views

app_name = 'scrubber'

urlpatterns = [
    path('', views.scrubber_home, name='home'),
]
