from django.contrib import admin
from .models import ScrubJob


@admin.register(ScrubJob)
class ScrubJobAdmin(admin.ModelAdmin):
    list_display = ('job_id', 'user', 'filename', 'status', 'total', 'clean', 'dnc', 'litigator', 'state_dnc', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('job_id', 'user__email', 'filename')
    readonly_fields = ('job_id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
