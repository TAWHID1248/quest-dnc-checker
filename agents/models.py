from django.conf import settings
from django.db import models


class AgentPromoCode(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        EXPIRED = 'expired', 'Expired'
        USED = 'used', 'Used'

    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='promo_codes',
    )
    code = models.CharField(max_length=20, unique=True, db_index=True)
    sequence = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_by = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='used_promo_code',
    )
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('agent', 'sequence')]
        indexes = [
            models.Index(fields=['agent', 'status']),
            models.Index(fields=['status', 'expires_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.code} ({self.get_status_display()})"
