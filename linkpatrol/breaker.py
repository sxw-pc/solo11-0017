"""Per-host failure circuit breaking shared across fetcher workers.

The breaker observes completed fetch outcomes (after the fetcher's own
retries have settled) and keeps a consecutive-failure streak per host inside
a sliding time window.  Once the streak reaches the configured threshold the
host is "open": the checker then degrades further external links to that
host into skip records instead of real requests.

The state lives on the shared :class:`~linkpatrol.fetcher.Fetcher` instance,
so concurrent crawler workers and the checker record into the same place.
Each host has its own lock; the global lock only guards dictionary insertion,
so recording never becomes a global bottleneck.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

CIRCUIT_OPEN_ERROR = "circuit_open"

@dataclass(frozen=True)
class BreakerConfig:
    """Tunable circuit-breaker policy; mirrors the ``[circuit]`` config table."""

    enabled: bool = False
    threshold: int = 5
    window_s: float = 300.0
    count_only_server_errors: bool = True
    probe_allowance: int = 0

@dataclass(frozen=True)
class GateDecision:
    """The breaker's answer for one host at one moment."""

    allowed: bool
    failures: int
    probing: bool = False

@dataclass
class _HostState:
    failures: deque[float] = field(default_factory=deque)
    probes_used: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

class CircuitBreaker:
    """Thread-safe per-host failure streaks inside a sliding window."""

    def __init__(self, config: BreakerConfig | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config or BreakerConfig()
        self._clock = clock
        self._states: dict[str, _HostState] = {}
        self._states_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def counts_as_failure(self, status_code: int | None, error: str | None) -> bool:
        """Classify one completed fetch outcome for streak accounting.

        Network errors and 5xx always count.  4xx responses count only when
        ``count_only_server_errors`` is false; they still never trigger the
        fetcher's retry path.  Anything else proves the host answered and
        therefore resets the streak.
        """
        if error is not None or status_code is None:
            return True
        if status_code >= 500:
            return True
        if self.config.count_only_server_errors:
            return False
        return status_code >= 400

    def record_result(self, host: str, status_code: int | None,
                      error: str | None) -> None:
        """Record one completed (post-retry) outcome for ``host``."""
        if not self.config.enabled or not host:
            return
        state = self._state_for(host)
        with state.lock:
            if self.counts_as_failure(status_code, error):
                now = self._clock()
                self._prune(state, now)
                state.failures.append(now)
            else:
                # Any answered request proves the host is alive and ends the
                # consecutive-failure streak, reopening probe allowance.
                state.failures.clear()
                state.probes_used = 0

    def decide(self, host: str) -> GateDecision:
        """Return whether a request to ``host`` may proceed right now.

        A host at or above the failure threshold blocks further requests,
        except that up to ``probe_allowance`` probe requests per open episode
        are still let through; the probe slot is reserved atomically here so
        concurrent callers cannot overshoot the allowance.
        """
        if not self.config.enabled:
            return GateDecision(allowed=True, failures=0)
        state = self._state_for(host)
        with state.lock:
            now = self._clock()
            self._prune(state, now)
            failures = len(state.failures)
            if failures < self.config.threshold:
                return GateDecision(allowed=True, failures=failures)
            if state.probes_used < self.config.probe_allowance:
                state.probes_used += 1
                return GateDecision(allowed=True, failures=failures, probing=True)
            return GateDecision(allowed=False, failures=failures)

    def failure_count(self, host: str) -> int:
        """Current consecutive-failure streak length for ``host``."""
        if not self.config.enabled:
            return 0
        state = self._states.get(host)
        if state is None:
            return 0
        with state.lock:
            self._prune(state, self._clock())
            return len(state.failures)

    def is_open(self, host: str) -> bool:
        """Return True when ``host`` has reached the failure threshold."""
        return self.config.enabled and self.failure_count(host) >= self.config.threshold

    def snapshot(self) -> dict[str, int]:
        """Current streak length per host, for tests and diagnostics."""
        if not self.config.enabled:
            return {}
        now = self._clock()
        with self._states_lock:
            items = list(self._states.items())
        result: dict[str, int] = {}
        for host, state in items:
            with state.lock:
                self._prune(state, now)
                if state.failures:
                    result[host] = len(state.failures)
        return result

    def _state_for(self, host: str) -> _HostState:
        with self._states_lock:
            state = self._states.get(host)
            if state is None:
                state = _HostState()
                self._states[host] = state
            return state

    def _prune(self, state: _HostState, now: float) -> None:
        cutoff = now - self.config.window_s
        while state.failures and state.failures[0] < cutoff:
            state.failures.popleft()
        if not state.failures:
            state.probes_used = 0

def breaker_from_config(config: dict) -> CircuitBreaker:
    """Build a breaker from a validated scan configuration."""
    section = config.get("circuit") or {}
    return CircuitBreaker(BreakerConfig(
        enabled=bool(section.get("enabled", False)),
        threshold=int(section.get("threshold", 5)),
        window_s=float(section.get("window_s", 300.0)),
        count_only_server_errors=bool(section.get("count_only_server_errors", True)),
        probe_allowance=int(section.get("probe_allowance", 0)),
    ))
