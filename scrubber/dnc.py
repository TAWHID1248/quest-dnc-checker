"""
DNC scrubbing engine — checks numbers against the live DNC API.

Each number is checked via GET /api/v1/check/. A ThreadPoolExecutor runs
checks concurrently for acceptable throughput on large batches.  Each
worker thread gets its own requests.Session so TCP connections are reused
within the thread (significant speedup for large batches).

Result caching
--------------
Confirmed API results (success=true responses) are cached in Redis for
DNC_RESULT_CACHE_TTL seconds (default 7 days).  Before hitting the API,
run_checks() does a single bulk Redis MGET for all numbers in the batch;
only the cache-miss subset goes to the API.  This eliminates redundant
calls for numbers that appear across multiple jobs, dramatically reducing
processing time for large datasets.

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

On any API error the number is conservatively placed in the DNC bucket
but the result is NOT cached (so a clean retry is possible).
"""

import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

DNC_API_URL = 'https://donotcalldnc.com/api/v1/check/'

# Cache confirmed API results for 7 days; configurable via settings.
DNC_RESULT_CACHE_TTL = getattr(settings, 'DNC_RESULT_CACHE_TTL', 7 * 24 * 3600)

# Thread-local storage: one persistent Session per worker thread.
_local = threading.local()


def _get_session() -> requests.Session:
    """Return (or lazily create) a per-thread Session with connection pooling."""
    if not hasattr(_local, 'session'):
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=1,
            pool_maxsize=1,
            max_retries=Retry(total=0),  # no retries — treat errors as DNC and move on
        )
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        _local.session = session
    return _local.session


def _cache_key(number: str) -> str:
    return f'dnc_result:{number}'


def _bulk_cache_lookup(numbers: list[str]) -> tuple[dict[str, bool], list[str]]:
    """
    Single Redis MGET for all numbers.
    Returns (cached_results, uncached_numbers).
    cached_results maps number -> is_dnc for cache hits.
    Falls back to (empty, all numbers) on any Redis error so the job
    continues without caching rather than failing.
    """
    try:
        keys = [_cache_key(n) for n in numbers]
        values = cache.get_many(keys)
    except Exception as exc:
        logger.warning("DNC result cache unavailable (lookup): %s — skipping cache", exc)
        return {}, list(numbers)

    cached: dict[str, bool] = {}
    uncached: list[str] = []
    for number in numbers:
        val = values.get(_cache_key(number))
        if val is not None:
            cached[number] = bool(val)
        else:
            uncached.append(number)
    return cached, uncached


def _bulk_cache_store(results: dict[str, bool]) -> None:
    """Store confirmed API results in Redis with a single MSET call.
    Failures are logged and ignored — caching is best-effort."""
    if not results:
        return
    try:
        cache.set_many(
            {_cache_key(n): is_dnc for n, is_dnc in results.items()},
            DNC_RESULT_CACHE_TTL,
        )
    except Exception as exc:
        logger.warning("DNC result cache unavailable (store): %s — skipping cache", exc)


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
) -> tuple[str, bool, bool] | None:
    """
    Check a single number against the API.

    Returns (number, is_dnc, confirmed) or None if stop_event was set
    before the call started.  confirmed=True means the API returned
    success=true and the result can be cached; False means it was an
    error (conservative DNC, do not cache).
    """
    if stop_event and stop_event.is_set():
        return None  # skipped — will appear in BatchResult.unchecked
    try:
        session = _get_session()
        resp = session.get(
            DNC_API_URL,
            params={'phone': number},
            headers={'X-API-KEY': api_key},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get('success'):
                return number, bool(data.get('is_dnc', True)), True
            logger.warning("DNC API non-success body for %s: %s", number, data)
        else:
            logger.warning("DNC API HTTP %s for %s: %s", resp.status_code, number, resp.text[:200])
    except requests.Timeout:
        logger.warning("DNC API timeout for %s", number)
    except Exception as exc:
        logger.exception("DNC API error for %s: %s", number, exc)
    return number, True, False  # fail-safe: treat as DNC, not cacheable


def run_checks(
    numbers: list[str],
    scrub_types: list[str],
    stop_event: threading.Event | None = None,
) -> BatchResult:
    """
    Check each number against the live DNC API, using Redis as a result cache.

    Flow:
      1. Bulk MGET from Redis — cache hits skip the API entirely.
      2. Remaining numbers go to the API via a thread pool.
      3. Confirmed API results are stored back to Redis in one MSET.

    Args:
        numbers:     List of normalised 10-digit phone strings.
        scrub_types: Kept for interface compatibility.
        stop_event:  When set, queued (not yet started) API calls return None
                     and are collected in BatchResult.unchecked.

    Returns:
        BatchResult with clean, dnc_numbers, and unchecked lists.
    """
    if not numbers:
        return BatchResult()

    api_key = getattr(settings, 'DNC_API_KEY', '')
    if not api_key:
        logger.error("DNC_API_KEY not configured — treating all numbers as DNC")
        return BatchResult(dnc_numbers=list(numbers))

    # ── 1. Bulk cache lookup ──────────────────────────────────────────────────
    cached_results, uncached = _bulk_cache_lookup(numbers)

    clean: list[str] = [n for n, is_dnc in cached_results.items() if not is_dnc]
    dnc_numbers: list[str] = [n for n, is_dnc in cached_results.items() if is_dnc]

    if not uncached:
        logger.debug("run_checks: all %d numbers served from cache", len(numbers))
        return BatchResult(clean=clean, dnc_numbers=dnc_numbers)

    logger.debug(
        "run_checks: %d cached, %d need API check",
        len(cached_results), len(uncached),
    )

    # ── 2. API check for cache misses ─────────────────────────────────────────
    max_workers = getattr(settings, 'DNC_API_CONCURRENCY', 50)
    confirmed_results: dict[str, bool] = {}
    api_checked: set[str] = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_check_one, n, api_key, stop_event): n
            for n in uncached
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is None:
                continue  # skipped due to stop_event
            number, is_dnc, confirmed = result
            api_checked.add(number)
            if confirmed:
                confirmed_results[number] = is_dnc
            if is_dnc:
                dnc_numbers.append(number)
            else:
                clean.append(number)

    # ── 3. Store confirmed results in cache ───────────────────────────────────
    _bulk_cache_store(confirmed_results)

    unchecked = [n for n in uncached if n not in api_checked]
    return BatchResult(clean=clean, dnc_numbers=dnc_numbers, unchecked=unchecked)
