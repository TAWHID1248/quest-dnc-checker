from django.conf import settings
from django.db import models


class AppSumoLicense(models.Model):
    """
    One AppSumo license key. Created/updated exclusively by AppSumo webhooks;
    linked to a user when the buyer completes the OAuth redemption flow.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVE = 'active', 'Active'
        DEACTIVATED = 'deactivated', 'Deactivated'

    license_key = models.CharField(max_length=64, unique=True)
    prev_license_key = models.CharField(
        max_length=64, blank=True,
        help_text='Previous license key (set on upgrade/downgrade events)',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='appsumo_licenses',
    )
    tier = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    credits_granted = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text='Total credits granted to the user for this license',
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['status']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        who = self.user.email if self.user else 'unlinked'
        return f"{self.license_key} (tier {self.tier}, {self.status}, {who})"


class AppSumoWebhookEvent(models.Model):
    """Raw log of every webhook received from AppSumo (audit/debug trail)."""

    license_key = models.CharField(max_length=64, blank=True)
    event = models.CharField(max_length=30)
    test = models.BooleanField(default=False)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['license_key']),
            models.Index(fields=['created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.event} — {self.license_key or 'no key'}"
