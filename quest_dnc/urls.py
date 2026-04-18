from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
from accounts.views import dashboard_view


def health_check(request):
    return HttpResponse("OK", content_type="text/plain")


urlpatterns = [
    path('health/', health_check, name='health_check'),
    path('admin/', admin.site.urls),
    path('accounts/social/', include('allauth.urls')),
    path('accounts/', include('accounts.urls', namespace='accounts')),
    path('dashboard/', dashboard_view, name='dashboard'),
    path('scrubber/', include('scrubber.urls', namespace='scrubber')),
    path('billing/', include('billing.urls', namespace='billing')),
    path('support/', include('support.urls', namespace='support')),
    path('panel/', include('admin_panel.urls', namespace='admin_panel')),
    path('', dashboard_view, name='home'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
