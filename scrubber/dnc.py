"""
DNC scrubbing engine — checks numbers against the live DNC API.

Each number is checked via GET /api/v1/check/. A ThreadPoolExecutor runs
checks concurrently for acceptable throughput on large batches.

Semantics:
    is_dnc = true  →  DNC   (removed from clean list)
    is_dnc = false →  CLEAN

On any API error the number is conservatively placed in the DNC bucket.
"""

import concurrent.futures
import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DNC_API_URL = 'https://donotcalldnc.com/api/v1/check/'


@dataclass
class BatchResult:
    """Categorised results for one batch of numbers."""
    clean:       list = field(default_factory=list)
    dnc_numbers: list = field(default_factory=list)

    @property
    def clean_count(self) -> int:
        return len(self.clean)

    @property
    def dnc_count(self) -> int:
        return len(self.dnc_numbers)


def _check_one(number: str, api_key: str) -> tuple[str, bool]:
    """Check a single number. Returns (number, is_dnc). Treats errors as DNC."""
    try:
        resp = requests.get(
            DNC_API_URL,
            params={'phone': number},
            headers={'X-API-KEY': api_key},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get('success'):
                return number, bool(data.get('is_dnc', True))
            logger.warning("DNC API non-success body for %s: %s", number, data)
        else:
            logger.warning("DNC API HTTP %s for %s: %s", resp.status_code, number, resp.text[:200])
    except requests.Timeout:
        logger.warning("DNC API timeout for %s", number)
    except Exception as exc:
        logger.exception("DNC API error for %s: %s", number, exc)
    return number, True  # fail-safe: treat as DNC on any error


def run_checks(numbers: list[str], scrub_types: list[str]) -> BatchResult:
    """
    Check each number against the live DNC API.

    Args:
        numbers:     List of normalised 10-digit phone strings.
        scrub_types: Kept for interface compatibility — the external API performs
                     a unified DNC check regardless of list type.

    Returns:
        BatchResult where `clean` = not-on-DNC, `dnc_numbers` = DNC matches.
    """
    if not numbers:
        return BatchResult()

    api_key = getattr(settings, 'DNC_API_KEY', '')
    if not api_key:
        logger.error("DNC_API_KEY not configured — treating all numbers as DNC")
        return BatchResult(dnc_numbers=list(numbers))

    max_workers = getattr(settings, 'DNC_API_CONCURRENCY', 50)

    clean: list = []
    dnc_numbers: list = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_check_one, n, api_key): n for n in numbers}
        for future in concurrent.futures.as_completed(futures):
            number, is_dnc = future.result()
            if is_dnc:
                dnc_numbers.append(number)
            else:
                clean.append(number)

    return BatchResult(clean=clean, dnc_numbers=dnc_numbers)
