from django.contrib import admin
from .models import SupportTicket


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ('ticket_id', 'user', 'subject', 'priority', 'status', 'created_at')
    list_filter = ('status', 'priority', 'created_at')
    search_fields = ('ticket_id', 'user__email', 'subject')
    readonly_fields = ('ticket_id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
