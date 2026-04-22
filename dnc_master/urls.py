from django.urls import path
from . import views

urlpatterns = [
    path('', views.dnc_master_home, name='dnc_master'),
    path('upload/', views.dnc_master_upload, name='dnc_master_upload'),
    path('upload/<int:job_id>/status/', views.dnc_master_upload_status, name='dnc_master_upload_status'),
]
