from django.contrib import admin

from .models import AgentPromoCode


@admin.register(AgentPromoCode)
class AgentPromoCodeAdmin(admin.ModelAdmin):
    list_display = ('code', 'agent', 'sequence', 'status', 'created_at', 'expires_at', 'used_by', 'used_at')
    list_filter = ('status',)
    search_fields = ('code', 'agent__email', 'agent__name')
    readonly_fields = ('code', 'sequence', 'created_at', 'used_by', 'used_at')
    raw_id_fields = ('agent',)
    ordering = ('-created_at',)
