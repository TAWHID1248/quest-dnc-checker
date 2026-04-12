import uuid
from django.conf import settings
from django.db import models


def _generate_id(prefix, length=8):
    """Generate a prefixed ID like PAY-A1B2C3D4."""
    return f"{prefix}-{uuid.uuid4().hex[:length].upper()}"


class PaymentMethod(models.Model):
    class CardType(models.TextChoices):
        VISA = 'visa', 'Visa'
        MASTERCARD = 'mastercard', 'Mastercard'
        AMEX = 'amex', 'American Express'
        DISCOVER = 'discover', 'Discover'
        OTHER = 'other', 'Other'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='payment_methods',
    )
    card_type = models.CharField(max_length=20, choices=CardType.choices, default=CardType.OTHER)
    last4 = models.CharField(max_length=4)
    exp_date = models.CharField(max_length=7, help_text='MM/YYYY')
    is_default = models.BooleanField(default=False)
    stripe_pm_id = models.CharField(max_length=255, unique=True)

    class Meta:
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['user', 'is_default']),
        ]
        ordering = ['-is_default', 'id']

    def __str__(self):
        return f"{self.get_card_type_display()} ****{self.last4} ({self.user.email})"

    def save(self, *args, **kwargs):
        # Ensure only one default per user
        if self.is_default:
            PaymentMethod.objects.filter(user=self.user, is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class CreditTransaction(models.Model):
    class Type(models.TextChoices):
        PURCHASE = 'purchase', 'Purchase'
        USAGE = 'usage', 'Usage'
        REFUND = 'refund', 'Refund'
        ADJUSTMENT = 'adjustment', 'Adjustment'

    transaction_id = models.CharField(max_length=20, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='credit_transactions',
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2, help_text='Credits added (positive) or consumed (negative)')
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text='USD amount paid, if applicable')
    payment_method = models.ForeignKey(
        PaymentMethod,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transactions',
    )
    scrub_job = models.ForeignKey(
        'scrubber.ScrubJob',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='credit_transactions',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['user', 'type']),
            models.Index(fields=['created_at']),
            models.Index(fields=['transaction_id']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.transaction_id} — {self.get_type_display()} {self.amount} credits ({self.user.email})"

    def save(self, *args, **kwargs):
        if not self.transaction_id:
            self.transaction_id = _generate_id('TXN')
        super().save(*args, **kwargs)


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        REFUNDED = 'refunded', 'Refunded'

    payment_id = models.CharField(max_length=20, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='payments',
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, help_text='Amount in USD')
    credits = models.DecimalField(max_digits=10, decimal_places=2, help_text='Credits purchased')
    method = models.ForeignKey(
        PaymentMethod,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='payments',
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    stripe_pi_id = models.CharField(max_length=255, blank=True, help_text='Stripe PaymentIntent ID')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['created_at']),
            models.Index(fields=['payment_id']),
            models.Index(fields=['stripe_pi_id']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.payment_id} — ${self.amount} / {self.credits} credits ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if not self.payment_id:
            self.payment_id = _generate_id('PAY')
        super().save(*args, **kwargs)
