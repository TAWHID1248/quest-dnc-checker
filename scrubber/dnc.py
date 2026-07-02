"""
DNC scrubbing engine — checks numbers against the local dnc_master_numbers table.

Numbers are stored in Postgres as BIGINT with a composite PRIMARY KEY (number, list_type).
A single SQL ANY(%s) query checks an entire batch in one round-trip — no HTTP API calls,
no rate limits, no external dependency.
"""

import logging
import threading
from dataclasses import dataclass, field

from django.db import connection

from dnc_master.models import LIST_TYPE_INT

logger = logging.getLogger(__name__)


@dataclass
class BatchResult:
    clean:       list = field(default_factory=list)
    dnc_numbers: list = field(default_factory=list)
    unchecked:   list = field(default_factory=list)

    @property
    def clean_count(self) -> int:
        return len(self.clean)

    @property
    def dnc_count(self) -> int:
        return len(self.dnc_numbers)


def run_checks(
    numbers: list[str],
    scrub_types: list[str],
    stop_event: threading.Event | None = None,
) -> BatchResult:
    """
    Check a batch of normalised 10-digit phone strings against the local DNC database.

    Args:
        numbers:     Normalised 10-digit phone strings (e.g. '2125551234').
        scrub_types: List of list types to check, e.g. ['federal_dnc', 'state_dnc'].
        stop_event:  When set before the query runs, returns all numbers as unchecked.

    Returns:
        BatchResult with clean, dnc_numbers, and unchecked lists populated.
    """
    if not numbers:
        return BatchResult()

    if stop_event and stop_event.is_set():
        return BatchResult(unchecked=list(numbers))

    list_type_ints = [LIST_TYPE_INT[t] for t in scrub_types if t in LIST_TYPE_INT]
    if not list_type_ints:
        logger.warning("No valid scrub_types in %s — treating all numbers as clean", scrub_types)
        return BatchResult(clean=list(numbers))

    number_ints = [int(n) for n in numbers]

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT number FROM dnc_master_numbers "
                "WHERE number = ANY(%s) AND list_type = ANY(%s)",
                [number_ints, list_type_ints],
            )
            dnc_set = {f"{row[0]:010d}" for row in cursor.fetchall()}
    except Exception as exc:
        logger.exception("DNC database lookup failed: %s — marking batch as DNC", exc)
        return BatchResult(dnc_numbers=list(numbers))

    clean       = [n for n in numbers if n not in dnc_set]
    dnc_numbers = [n for n in numbers if n in dnc_set]

    logger.debug(
        "run_checks: %d numbers → %d clean, %d dnc (types=%s)",
        len(numbers), len(clean), len(dnc_numbers), scrub_types,
    )

    return BatchResult(clean=clean, dnc_numbers=dnc_numbers)
