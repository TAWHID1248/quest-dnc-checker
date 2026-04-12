"""
Phone number normalization utilities.

All numbers are normalised to bare 10-digit US format (NPANXXXXXX) before
any DNC checking occurs.  This module has zero external dependencies beyond
the stdlib so it is safe to import anywhere.
"""

import re
from typing import Iterator

# Pre-compiled once at import time
_DIGIT_RE   = re.compile(r'\D')
_VALID_RE   = re.compile(r'^[2-9]\d{2}[2-9]\d{6}$')  # NANP: NPA & NXX can't start with 0 or 1


def normalize(raw: str) -> str | None:
    """
    Strip a raw phone string down to 10 clean US digits.

    Returns the 10-digit string on success, or None if the number is
    structurally invalid (wrong length, bad area code, etc.).
    """
    digits = _DIGIT_RE.sub('', raw)

    # Strip leading country code  (+1 / 1)
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]

    if len(digits) != 10:
        return None

    if not _VALID_RE.match(digits):
        return None

    return digits


def iter_numbers(file_obj, encoding: str = 'utf-8') -> Iterator[tuple[int, str, str | None]]:
    """
    Iterate over a file-like object line by line.

    Yields: (line_number, raw_value, normalized_or_None)

    Handles CSV files where the phone number is the first column, as well as
    plain TXT files with one number per line.  BOM and mixed line-endings are
    handled transparently.
    """
    # Read as text; fall back to latin-1 if the file has encoding issues
    try:
        content = file_obj.read().decode(encoding)
    except (UnicodeDecodeError, AttributeError):
        try:
            file_obj.seek(0)
            content = file_obj.read().decode('latin-1')
        except Exception:
            return

    content = content.lstrip('\ufeff')  # strip UTF-8 BOM if present

    for lineno, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        # Take first comma-separated token (handles CSV with headers / extra cols)
        raw = line.split(',')[0].strip().strip('"').strip("'")

        if not raw:
            continue

        # Skip obvious header rows
        if re.match(r'^[a-zA-Z]', raw) and not re.search(r'\d', raw):
            continue

        yield lineno, raw, normalize(raw)


def extract_unique_numbers(file_obj) -> tuple[list[str], int, int]:
    """
    Fully parse a file and return:
        (unique_valid_numbers, total_lines_read, invalid_count)

    Deduplication is done here so the task only pays the DNC check cost once
    per unique number.
    """
    seen:    set[str] = set()
    valid:   list[str] = []
    invalid: int = 0
    total:   int = 0

    for _lineno, _raw, normalised in iter_numbers(file_obj):
        total += 1
        if normalised is None:
            invalid += 1
            continue
        if normalised not in seen:
            seen.add(normalised)
            valid.append(normalised)

    return valid, total, invalid
