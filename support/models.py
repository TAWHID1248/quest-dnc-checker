import uuid
from django.conf import settings
from django.db import models


def _generate_ticket_id():
    return f"TKT-{uuid.uuid4().hex[:6].upper()}"


class SupportTicket(models.Model):
    class Priority(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
        URGENT = 'urgent', 'Urgent'

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        IN_PROGRESS = 'in_progress', 'In Progress'
        WAITING = 'waiting', 'Waiting on Customer'
        RESOLVED = 'resolved', 'Resolved'
        CLOSED = 'closed', 'Closed'

    ticket_id = models.CharField(max_length=15, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='support_tickets',
    )
    subject = models.CharField(max_length=255)
    description = models.TextField()
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['status']),
            models.Index(fields=['priority']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['ticket_id']),
            models.Index(fields=['created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.ticket_id} — {self.subject} [{self.get_status_display()}]"

    def save(self, *args, **kwargs):
        if not self.ticket_id:
            self.ticket_id = _generate_ticket_id()
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status in (self.Status.OPEN, self.Status.IN_PROGRESS, self.Status.WAITING)
