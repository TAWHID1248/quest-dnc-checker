"""
DNC scrubbing engine — checks numbers against the live DNC API.

Each number is checked via GET /api/v1/check/. A ThreadPoolExecutor runs
checks concurrently for acceptable throughput on large batches.  Each
worker thread gets its own requests.Session so TCP connections are reused
within the thread (significant speedup for large batches).

Pause / cancel support
----------------------
An optional threading.Event (stop_event) can be passed to run_checks().
When the event is set:
  • _check_one returns None for calls that haven't started yet
  • already in-flight HTTP calls complete normally (HTTP can't be cancelled)
  • BatchResult.unchecked contains all numbers that were skipped

Semantics:
    is_dnc = true  →  DNC   (removed from clean list)
    is_dnc = false →  CLEAN

On any API error the number is conservatively placed in the DNC bucket.
"""

import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.conf import settings

logger = logging.getLogger(__name__)

DNC_API_URL = 'https://donotcalldnc.com/api/v1/check/'

# Thread-local storage: one persistent Session per worker thread.
_local = threading.local()


def _get_session() -> requests.Session:
    """Return (or lazily create) a per-thread Session with connection pooling."""
    if not hasattr(_local, 'session'):
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=1,
            pool_maxsize=2,
            max_retries=Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503]),
        )
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        _local.session = session
    return _local.session


@dataclass
class BatchResult:
    """Categorised results for one batch of numbers."""
    clean:       list = field(default_factory=list)
    dnc_numbers: list = field(default_factory=list)
    unchecked:   list = field(default_factory=list)  # skipped due to stop_event

    @property
    def clean_count(self) -> int:
        return len(self.clean)

    @property
    def dnc_count(self) -> int:
        return len(self.dnc_numbers)


def _check_one(
    number: str,
    api_key: str,
    stop_event: threading.Event | None = None,
) -> tuple[str, bool] | None:
    """
    Check a single number.
    Returns (number, is_dnc), or None if stop_event was already set
    (meaning this number was never sent to the API).
    """
    if stop_event and stop_event.is_set():
        return None  # skipped — will appear in BatchResult.unchecked
    try:
        session = _get_session()
        resp = session.get(
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


def run_checks(
    numbers: list[str],
    scrub_types: list[str],
    stop_event: threading.Event | None = None,
) -> BatchResult:
    """
    Check each number against the live DNC API.

    Args:
        numbers:     List of normalised 10-digit phone strings.
        scrub_types: Kept for interface compatibility.
        stop_event:  When set, queued (not yet started) calls return None and
                     are collected in BatchResult.unchecked.

    Returns:
        BatchResult with clean, dnc_numbers, and unchecked lists.
    """
    if not numbers:
        return BatchResult()

    api_key = getattr(settings, 'DNC_API_KEY', '')
    if not api_key:
        logger.error("DNC_API_KEY not configured — treating all numbers as DNC")
        return BatchResult(dnc_numbers=list(numbers))

    max_workers = getattr(settings, 'DNC_API_CONCURRENCY', 150)

    clean:       list = []
    dnc_numbers: list = []
    checked:     set  = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_check_one, n, api_key, stop_event): n for n in numbers}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is None:
                continue  # this number was skipped; will appear in unchecked below
            number, is_dnc = result
            checked.add(number)
            if is_dnc:
                dnc_numbers.append(number)
            else:
                clean.append(number)

    # Preserve original order for unchecked so resume is deterministic
    unchecked = [n for n in numbers if n not in checked]
    return BatchResult(clean=clean, dnc_numbers=dnc_numbers, unchecked=unchecked)
