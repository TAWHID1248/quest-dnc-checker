from django.urls import path
from . import views

app_name = 'scrubber'

urlpatterns = [
    path('', views.scrubber_home, name='home'),
    path('status/<str:job_id>/', views.job_status, name='job_status'),
    path('download/<str:job_id>/', views.download_result, name='download_result'),
]
