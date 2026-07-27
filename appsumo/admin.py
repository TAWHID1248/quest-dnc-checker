from django.contrib import admin

from .models import AppSumoLicense, AppSumoWebhookEvent


@admin.register(AppSumoLicense)
class AppSumoLicenseAdmin(admin.ModelAdmin):
    list_display = ('license_key', 'user', 'tier', 'status', 'credits_granted', 'activated_at')
    list_filter = ('status', 'tier')
    search_fields = ('license_key', 'prev_license_key', 'user__email')
    raw_id_fields = ('user',)


@admin.register(AppSumoWebhookEvent)
class AppSumoWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('event', 'license_key', 'test', 'created_at')
    list_filter = ('event', 'test')
    search_fields = ('license_key',)
    readonly_fields = ('license_key', 'event', 'test', 'payload', 'created_at')
