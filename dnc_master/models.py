from django.conf import settings
from django.db import models

LIST_TYPES = [
    ('federal_dnc', 'Federal DNC'),
    ('state_dnc', 'State DNC'),
]

# Maps list_type string to the SMALLINT stored in dnc_master_numbers
LIST_TYPE_INT = {
    'federal_dnc': 1,
    'state_dnc':   2,
}

LIST_TYPE_LABEL = {v: k for k, v in LIST_TYPE_INT.items()}


class DncMasterList(models.Model):
    """One row per list type — tracks aggregate stats for the master database."""
    list_type = models.CharField(max_length=20, choices=LIST_TYPES, unique=True)
    record_count = models.BigIntegerField(default=0)
    last_updated = models.DateTimeField(null=True, blank=True)
    last_uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    is_loading = models.BooleanField(default=False)

    class Meta:
        ordering = ['list_type']

    def __str__(self):
        return f"{self.get_list_type_display()} ({self.record_count:,} records)"


class DncUploadJob(models.Model):
    """Tracks each admin CSV upload attempt."""

    class Status(models.TextChoices):
        PENDING    = 'pending',    'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED  = 'completed',  'Completed'
        FAILED     = 'failed',     'Failed'

    class Mode(models.TextChoices):
        REPLACE = 'replace', 'Replace'
        APPEND  = 'append',  'Append'

    list_type         = models.CharField(max_length=20, choices=LIST_TYPES)
    mode              = models.CharField(max_length=10, choices=Mode.choices, default=Mode.REPLACE)
    file              = models.FileField(upload_to='dnc_uploads/%Y/%m/')
    original_filename = models.CharField(max_length=255, blank=True)
    status            = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    total_rows        = models.BigIntegerField(default=0)
    records_loaded    = models.BigIntegerField(default=0)
    error_message     = models.TextField(blank=True)
    uploaded_by       = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='dnc_uploads',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_list_type_display()} — {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def progress_pct(self):
        if not self.total_rows:
            return 0
        return min(round(self.records_loaded / self.total_rows * 100, 1), 100)
