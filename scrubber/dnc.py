"""
Scrubbing engine — cross-checks user numbers against dnc_master_numbers.

Semantics (whitelist mode):
    matched in master   →  CLEAN
    not matched         →  DNC

Uses a PostgreSQL TEMP TABLE + JOIN for maximum throughput.  If the master
table is empty (not yet loaded by admin) every submitted number falls into
the DNC bucket (nothing matched) and a warning is logged.
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


@dataclass
class BatchResult:
    """Categorised results for one batch of numbers."""
    clean:       list = field(default_factory=list)  # matched → clean
    dnc_numbers: list = field(default_factory=list)  # unmatched → DNC

    @property
    def clean_count(self) -> int:
        return len(self.clean)

    @property
    def dnc_count(self) -> int:
        return len(self.dnc_numbers)


def run_checks(numbers: list[str], scrub_types: list[str]) -> BatchResult:
    """
    Cross-check `numbers` against dnc_master_numbers for the requested
    scrub_types.

    Args:
        numbers:     List of normalised 10-digit phone strings.
        scrub_types: Subset of ['federal_dnc', 'state_dnc']. Determines
                     which master list types to match against.

    Returns:
        BatchResult where `clean` = matched numbers, `dnc_numbers` = unmatched.
    """
    if not numbers:
        return BatchResult()

    type_values = [LIST_TYPE_INT[t] for t in scrub_types if t in LIST_TYPE_INT]
    if not type_values:
        # No master types selected → nothing can match → all DNC
        return BatchResult(dnc_numbers=list(numbers))

    # Map integer → original string for result reconstruction
    int_to_str: dict[int, str] = {}
    for n in numbers:
        try:
            int_to_str[int(n)] = n
        except (ValueError, TypeError):
            pass

    if not int_to_str:
        return BatchResult(dnc_numbers=list(numbers))

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _scrub_input (number BIGINT)"
            )
            cursor.execute("TRUNCATE _scrub_input")

            buf = io.StringIO()
            for n in int_to_str:
                buf.write(f"{n}\n")
            buf.seek(0)
            cursor.copy_from(buf, '_scrub_input', columns=('number',))

            placeholders = ','.join(['%s'] * len(type_values))
            cursor.execute(
                f"""
                SELECT DISTINCT i.number
                FROM _scrub_input i
                JOIN dnc_master_numbers d ON d.number = i.number
                WHERE d.list_type IN ({placeholders})
                """,
                type_values,
            )
            matched_rows = cursor.fetchall()

    except Exception as exc:
        logger.exception("Master lookup failed, returning all numbers as DNC: %s", exc)
        return BatchResult(dnc_numbers=list(numbers))

    matched_ints: set = {row[0] for row in matched_rows}

    if not matched_ints:
        logger.debug(
            "No master matches found for %d numbers (master table may be empty)",
            len(numbers),
        )

    # Inverted: matched = clean, unmatched = DNC
    clean:       list = []
    dnc_numbers: list = []
    for n in numbers:
        try:
            n_int = int(n)
        except (ValueError, TypeError):
            dnc_numbers.append(n)
            continue
        if n_int in matched_ints:
            clean.append(n)
        else:
            dnc_numbers.append(n)

    return BatchResult(clean=clean, dnc_numbers=dnc_numbers)
