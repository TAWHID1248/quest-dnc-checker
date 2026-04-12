"""
DNC checking engine.

Each check_* function accepts a list of normalised 10-digit phone numbers and
returns the subset that matched that list.

PLACEHOLDER IMPLEMENTATION
--------------------------
Real implementations will call the FTC / state DNC APIs or compare against a
locally-cached database snapshot.  The placeholder uses a deterministic hash
so results are stable across runs (same number always produces the same outcome
in tests) while still yielding realistic hit-rates for demo purposes.

To swap in a real implementation, replace the body of each check_* function
without changing its signature.  The task in tasks.py calls this module through
the single public entry-point  `run_checks()` only.
"""

import hashlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Hit-rate configuration (placeholder only) ──────────────────────────────

_RATES = {
    'federal_dnc': 0.14,   # ~14 % of numbers on the federal list
    'state_dnc':   0.07,   # ~7 % additional state-only hits
    'litigator':   0.02,   # ~2 % known TCPA litigators
}


def _hash_hit(number: str, salt: str, rate: float) -> bool:
    """Deterministic pseudo-random hit based on number + salt."""
    digest = hashlib.md5(f"{salt}:{number}".encode()).hexdigest()
    # Use first 8 hex chars as a 32-bit unsigned int, compare to rate threshold
    return int(digest[:8], 16) < int(rate * 0xFFFFFFFF)


# ── Individual check functions ──────────────────────────────────────────────

def check_federal_dnc(numbers: list[str]) -> set[str]:
    """
    Return the subset of `numbers` found on the Federal Do Not Call Registry.

    Real implementation: query FTC DNC database or a licensed data provider.
    """
    return {n for n in numbers if _hash_hit(n, 'federal', _RATES['federal_dnc'])}


def check_state_dnc(numbers: list[str]) -> set[str]:
    """
    Return the subset of `numbers` found on any state DNC list.

    Real implementation: query per-state registries (varies by state).
    Numbers already flagged as federal_dnc are excluded here to avoid
    double-counting in result stats.
    """
    return {n for n in numbers if _hash_hit(n, 'state', _RATES['state_dnc'])}


def check_litigator(numbers: list[str]) -> set[str]:
    """
    Return the subset of `numbers` known to belong to TCPA litigators.

    Real implementation: compare against a licensed litigator database.
    """
    return {n for n in numbers if _hash_hit(n, 'litigator', _RATES['litigator'])}


# ── Public result container ─────────────────────────────────────────────────

@dataclass
class BatchResult:
    """Holds categorised results for one batch of numbers."""
    clean:     list[str] = field(default_factory=list)
    federal:   set[str]  = field(default_factory=set)
    state:     set[str]  = field(default_factory=set)
    litigator: set[str]  = field(default_factory=set)

    @property
    def dnc_count(self) -> int:
        return len(self.federal)

    @property
    def state_count(self) -> int:
        return len(self.state)

    @property
    def litigator_count(self) -> int:
        return len(self.litigator)

    @property
    def clean_count(self) -> int:
        return len(self.clean)


# ── Main entry-point ────────────────────────────────────────────────────────

def run_checks(numbers: list[str], scrub_types: list[str]) -> BatchResult:
    """
    Run all requested checks against `numbers` and return a BatchResult.

    Args:
        numbers:     List of normalised 10-digit phone numbers.
        scrub_types: Subset of ['federal_dnc', 'state_dnc', 'litigator'].

    Returns:
        BatchResult with numbers partitioned into clean / flagged buckets.

    The priority order for categorisation when a number matches multiple
    lists is: litigator > federal_dnc > state_dnc.  A number is placed in
    `clean` only if it matches none of the requested lists.
    """
    if not numbers:
        return BatchResult()

    scrub_set = set(scrub_types)
    flagged: set[str] = set()

    federal:   set[str] = set()
    state:     set[str] = set()
    litigator: set[str] = set()

    # Run only the checks the user requested
    if 'litigator' in scrub_set:
        litigator = check_litigator(numbers)
        flagged |= litigator
        logger.debug("Litigator check: %d hits out of %d", len(litigator), len(numbers))

    if 'federal_dnc' in scrub_set:
        # Don't double-flag litigators as federal DNC
        remaining = [n for n in numbers if n not in flagged]
        federal = check_federal_dnc(remaining)
        flagged |= federal
        logger.debug("Federal DNC check: %d hits out of %d", len(federal), len(remaining))

    if 'state_dnc' in scrub_set:
        remaining = [n for n in numbers if n not in flagged]
        state = check_state_dnc(remaining)
        flagged |= state
        logger.debug("State DNC check: %d hits out of %d", len(state), len(remaining))

    clean = [n for n in numbers if n not in flagged]

    return BatchResult(
        clean=clean,
        federal=federal,
        state=state,
        litigator=litigator,
    )
