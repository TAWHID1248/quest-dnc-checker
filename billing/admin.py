from django.contrib import admin
from .models import PaymentMethod, CreditTransaction, Payment


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ('user', 'card_type', 'last4', 'exp_date', 'is_default', 'stripe_pm_id')
    list_filter = ('card_type', 'is_default')
    search_fields = ('user__email', 'last4', 'stripe_pm_id')


@admin.register(CreditTransaction)
class CreditTransactionAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'user', 'type', 'amount', 'price', 'scrub_job', 'created_at')
    list_filter = ('type', 'created_at')
    search_fields = ('transaction_id', 'user__email')
    readonly_fields = ('transaction_id', 'created_at')
    ordering = ('-created_at',)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('payment_id', 'user', 'amount', 'credits', 'status', 'stripe_pi_id', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('payment_id', 'user__email', 'stripe_pi_id')
    readonly_fields = ('payment_id', 'created_at')
    ordering = ('-created_at',)
