import uuid
from django.conf import settings
from django.db import models


def _generate_scrub_id():
    return f"SCR-{uuid.uuid4().hex[:8].upper()}"


class ScrubJob(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        QUEUED = 'queued', 'Queued'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    job_id = models.CharField(max_length=20, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='scrub_jobs',
    )
    filename = models.CharField(max_length=255, help_text='Original uploaded filename')
    file = models.FileField(upload_to='scrub_uploads/%Y/%m/', blank=True)

    # Scrub configuration
    scrub_types = models.JSONField(
        default=list,
        help_text='List of scrub types: federal_dnc, state_dnc',
    )

    # Result counts
    total = models.PositiveIntegerField(default=0, help_text='Total numbers submitted')
    clean = models.PositiveIntegerField(default=0, help_text='Numbers that passed all scrubs')
    dnc = models.PositiveIntegerField(default=0, help_text='Federal DNC matches')
    state_dnc = models.PositiveIntegerField(default=0, help_text='State DNC matches')

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True, help_text='Set when status=failed')
    result_file = models.FileField(upload_to='scrub_results/%Y/%m/', blank=True, null=True)
    result_file_dnc = models.FileField(upload_to='scrub_results/%Y/%m/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['job_id']),
            models.Index(fields=['created_at']),
            models.Index(fields=['status', 'created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.job_id} — {self.filename} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if not self.job_id:
            self.job_id = _generate_scrub_id()
        super().save(*args, **kwargs)

    @property
    def removed(self):
        return self.total - self.clean

    @property
    def completion_pct(self):
        if self.total == 0:
            return 0
        return round((self.clean / self.total) * 100, 1)
