from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
from django.views.generic import TemplateView
from accounts.views import dashboard_view


def health_check(request):
    return HttpResponse("OK", content_type="text/plain")


urlpatterns = [
    path('health/', health_check, name='health_check'),
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls', namespace='accounts')),
    path('dashboard/', dashboard_view, name='dashboard'),
    path('scrubber/', include('scrubber.urls', namespace='scrubber')),
    path('billing/', include('billing.urls', namespace='billing')),
    path('support/', include('support.urls', namespace='support')),
    path('appsumo/', include('appsumo.urls', namespace='appsumo')),
    path('panel/', include('admin_panel.urls', namespace='admin_panel')),
    path(
        'terms-of-service/',
        TemplateView.as_view(template_name='legal/terms_of_service.html'),
        name='terms_of_service',
    ),
    path(
        'refund-policy/',
        TemplateView.as_view(template_name='legal/refund_policy.html'),
        name='refund_policy',
    ),
    path(
        'privacy-policy/',
        TemplateView.as_view(template_name='legal/privacy_policy.html'),
        name='privacy_policy',
    ),
    path('', dashboard_view, name='home'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
