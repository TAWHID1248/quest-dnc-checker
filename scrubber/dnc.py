"""
DNC checking engine — real database lookup against dnc_master_numbers.

Cross-checks each batch of normalised 10-digit phone numbers against the
master table using a PostgreSQL TEMP TABLE + JOIN for maximum throughput.

If the master table is empty (not yet loaded by admin) all numbers are
returned as clean and a warning is logged.
"""

import io
import logging
from dataclasses import dataclass, field

from django.db import connection

logger = logging.getLogger(__name__)

# list_type string → SMALLINT stored in dnc_master_numbers
LIST_TYPE_INT = {
    'federal_dnc': 1,
    'state_dnc':   2,
}

# SMALLINT → human label used in DNC output CSV
LIST_INT_LABEL = {
    1: 'Federal DNC',
    2: 'State DNC',
}


@dataclass
class BatchResult:
    """Holds categorised results for one batch of numbers."""
    clean:   list = field(default_factory=list)
    federal: set  = field(default_factory=set)
    state:   set  = field(default_factory=set)
    # number (10-digit str) → (human_label, state_str)
    dnc_details: dict = field(default_factory=dict)

    @property
    def dnc_count(self) -> int:
        return len(self.federal)

    @property
    def state_count(self) -> int:
        return len(self.state)

    @property
    def clean_count(self) -> int:
        return len(self.clean)


def run_checks(numbers: list[str], scrub_types: list[str]) -> BatchResult:
    """
    Cross-check `numbers` against dnc_master_numbers for the requested
    scrub_types.  Returns a BatchResult with clean + flagged buckets.

    Args:
        numbers:     List of normalised 10-digit phone strings.
        scrub_types: Subset of ['federal_dnc', 'state_dnc'].
    """
    if not numbers:
        return BatchResult()

    type_values = [LIST_TYPE_INT[t] for t in scrub_types if t in LIST_TYPE_INT]
    if not type_values:
        return BatchResult(clean=list(numbers))

    # Map integer → original string so we can reconstruct results
    int_to_str: dict[int, str] = {}
    for n in numbers:
        try:
            int_to_str[int(n)] = n
        except (ValueError, TypeError):
            pass

    if not int_to_str:
        return BatchResult(clean=list(numbers))

    federal:   set = set()
    state_set: set = set()
    dnc_details: dict = {}

    try:
        with connection.cursor() as cursor:
            # Temp table for this batch — dropped at transaction end
            cursor.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _scrub_input (number BIGINT)"
            )
            cursor.execute("TRUNCATE _scrub_input")

            # Bulk-load user numbers via COPY
            buf = io.StringIO()
            for n in int_to_str:
                buf.write(f"{n}\n")
            buf.seek(0)
            cursor.copy_from(buf, '_scrub_input', columns=('number',))

            # Single JOIN — uses the PRIMARY KEY index on dnc_master_numbers
            placeholders = ','.join(['%s'] * len(type_values))
            cursor.execute(
                f"""
                SELECT i.number, d.list_type, d.state
                FROM _scrub_input i
                JOIN dnc_master_numbers d ON d.number = i.number
                WHERE d.list_type IN ({placeholders})
                """,
                type_values,
            )
            rows = cursor.fetchall()

    except Exception as exc:
        logger.exception("DNC lookup failed, returning all numbers as clean: %s", exc)
        return BatchResult(clean=list(numbers))

    matched_ints: set = set()

    for number_int, list_type_val, st in rows:
        n = int_to_str.get(number_int)
        if n is None:
            continue
        matched_ints.add(number_int)
        label = LIST_INT_LABEL.get(list_type_val, 'DNC')
        state_str = str(st).strip() if st else ''
        dnc_details[n] = (label, state_str)

        if list_type_val == 1:
            federal.add(n)
        elif list_type_val == 2:
            state_set.add(n)

    if not rows:
        logger.debug(
            "No DNC matches found for %d numbers (master table may be empty)",
            len(numbers),
        )

    clean = [n for n in numbers if int(n) not in matched_ints]

    return BatchResult(
        clean=clean,
        federal=federal,
        state=state_set,
        dnc_details=dnc_details,
    )
