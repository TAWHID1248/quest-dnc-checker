import logging
import re
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

PROMO_CODE_EXPIRY_DAYS = 7
PROMO_CODE_MAX_SEQUENCE = 10_000


def _lastname_prefix(agent) -> str:
    """
    Derive the 4-letter uppercase prefix from the agent's last name.
    Strips non-alpha chars, pads short names with 'X'.
    E.g. "Md. Rahman" -> "RAHM", "Lee" -> "LEEX"
    """
    name = agent.name.strip()
    parts = name.split()
    last = parts[-1] if parts else name
    clean = re.sub(r'[^A-Za-z]', '', last).upper()
    clean = clean.ljust(4, 'X')
    return clean[:4]


def generate_next_promo_code(agent):
    """
    Generate the next sequential promo code for the agent.
    Returns the AgentPromoCode instance, or None if the 10,000-code limit is reached.
    """
    from .models import AgentPromoCode

    last = (
        AgentPromoCode.objects
        .filter(agent=agent)
        .order_by('-sequence')
        .first()
    )
    next_seq = (last.sequence + 1) if last else 1

    if next_seq > PROMO_CODE_MAX_SEQUENCE:
        _alert_admin_limit_reached(agent)
        return None

    prefix = _lastname_prefix(agent)
    code = f"{prefix}DNC26{next_seq:04d}"
    now = timezone.now()

    promo = AgentPromoCode.objects.create(
        agent=agent,
        code=code,
        sequence=next_seq,
        status=AgentPromoCode.Status.ACTIVE,
        expires_at=now + timedelta(days=PROMO_CODE_EXPIRY_DAYS),
    )
    logger.info("Generated promo code %s for agent %s", code, agent.email)
    return promo


def _alert_admin_limit_reached(agent) -> None:
    from django.core.mail import mail_admins
    logger.warning("Promo code limit reached for agent %s", agent.email)
    mail_admins(
        f'Promo code limit reached — {agent.email}',
        (
            f"Agent {agent.name} ({agent.email}) has used all {PROMO_CODE_MAX_SEQUENCE} "
            f"available promo codes. Auto-generation is stopped. "
            f"Manual intervention required."
        ),
        fail_silently=True,
    )
