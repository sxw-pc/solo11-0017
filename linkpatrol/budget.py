"""Run-level request attempt budget and per-target failure attribution.

The per-request retry policy in :mod:`linkpatrol.fetcher` only bounds how many
times one individual request is retried.  A whole run can still spend an
unbounded number of attempts when many targets keep failing.  This module adds
a run-wide ceiling shared by the crawler and the checker: every HTTP attempt
reserves one slot through a single thread-safe counter, so concurrent workers
can never overshoot the budget.  Targets that no longer have a slot are marked
``skipped_budget`` instead of being hammered.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# Error marker used when a fetch never reached the network because the run
# budget was already exhausted.
BUDGET_EXHAUSTED_ERROR = "budget_exhausted"


@dataclass
class AttemptDetail:
    """Failure/retry attribution recorded for one target URL in one phase.

    Only targets that retried, failed, or were cut off by the budget get a
    row; healthy one-attempt targets are omitted to keep the table small.
    """

    target_url: str
    phase: str
    attempts: int = 0
    retry_count: int = 0
    first_failure_code: int | None = None
    final_failure_code: int | None = None
    budget_skipped: bool = False
    error: str | None = None


class AttemptBudget:
    """A thread-safe ceiling on the total HTTP attempts of one run.

    ``reserve`` is the only place the counter moves, and it moves under a
    lock, so with N workers the number of accepted reservations can never
    exceed ``limit``.
    """

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self._used = 0
        self._denied = 0
        self._lock = threading.Lock()

    def reserve(self) -> bool:
        """Atomically consume one attempt slot.

        Returns ``True`` when the caller may perform the HTTP request and
        ``False`` when the run budget is already exhausted.
        """
        if self.limit is None:
            return True
        with self._lock:
            if self._used >= self.limit:
                self._denied += 1
                return False
            self._used += 1
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def denied(self) -> int:
        with self._lock:
            return self._denied

    @property
    def exhausted(self) -> bool:
        if self.limit is None:
            return False
        with self._lock:
            return self._used >= self.limit

    def snapshot(self) -> dict:
        """Plain-object view embedded in run stats and JSON reports."""
        with self._lock:
            used, denied, limit = self._used, self._denied, self.limit
        remaining = None if limit is None else max(0, limit - used)
        return {
            "limit": limit,
            "used": used,
            "denied": denied,
            "remaining": remaining,
            "exhausted": limit is not None and used >= limit,
        }


def default_attempt_limit(max_pages: int, retries: int) -> int:
    """Derive a reasonable run-wide ceiling from the other limits.

    A worst-case target costs ``retries + 1`` attempts.  Crawling can touch
    up to ``max_pages`` targets and the checker independently probes the
    unique link targets, so two phases of ``max_pages`` targets are budgeted.
    A fixed cushion covers robots.txt, sitemap fetching, and HEAD->GET
    fallbacks.
    """
    per_target = max(1, retries + 1)
    cushion = 10
    return max_pages * 2 * per_target + cushion


def resolve_attempt_limit(config: dict) -> int | None:
    """Return the configured override or the derived default for a run."""
    limits = config["limits"]
    configured = limits.get("max_attempts")
    if configured is not None:
        return int(configured)
    return default_attempt_limit(limits["max_pages"], limits["retries"])


def merge_attempt_detail(existing: AttemptDetail, other: AttemptDetail) -> None:
    """Merge two attribution rows for the same target in the same phase.

    The checker may probe one base URL more than once (HEAD then GET, or a
    GET refresh for anchor validation); their telemetry is combined here.
    """
    existing.attempts += other.attempts
    existing.retry_count += other.retry_count
    existing.budget_skipped = existing.budget_skipped or other.budget_skipped
    if existing.first_failure_code is None and other.first_failure_code is not None:
        existing.first_failure_code = other.first_failure_code
    # The later probe is the latest observed state; None means recovery.
    existing.final_failure_code = other.final_failure_code
    if other.error is not None:
        existing.error = other.error


def detail_from_fetch(target_url: str, phase: str, fetch_result: object) -> AttemptDetail:
    """Build an :class:`AttemptDetail` from a fetcher result's telemetry."""
    return AttemptDetail(
        target_url=target_url,
        phase=phase,
        attempts=fetch_result.attempts,
        retry_count=max(0, fetch_result.attempts - 1),
        first_failure_code=fetch_result.first_failure_code,
        final_failure_code=fetch_result.final_failure_code,
        budget_skipped=fetch_result.budget_skipped,
        error=fetch_result.error,
    )


def is_significant(detail: AttemptDetail) -> bool:
    """Whether a target deserves a persisted attribution row."""
    return (
        detail.attempts > 1
        or detail.first_failure_code is not None
        or detail.final_failure_code is not None
        or detail.budget_skipped
        or detail.error is not None
    )
