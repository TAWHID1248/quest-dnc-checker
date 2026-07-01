import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name='agents.tasks.expire_promo_codes')
def expire_promo_codes():
    """
    Hourly task: mark expired active codes and auto-generate a replacement
    for each affected agent so they always have one active code available.
    """
    from .models import AgentPromoCode
    from .utils import generate_next_promo_code

    now = timezone.now()
    expired_qs = (
        AgentPromoCode.objects
        .filter(status=AgentPromoCode.Status.ACTIVE, expires_at__lt=now)
        .select_related('agent')
    )

    count = 0
    for code in expired_qs:
        code.status = AgentPromoCode.Status.EXPIRED
        code.save(update_fields=['status'])
        generate_next_promo_code(code.agent)
        count += 1

    if count:
        logger.info("expire_promo_codes: expired %d codes, generated %d replacements", count, count)
    return count
