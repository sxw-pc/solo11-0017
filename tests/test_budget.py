"""Run-level attempt budget, default derivation, and attribution merging."""

import threading

import pytest

from linkpatrol.budget import (
    AttemptBudget,
    AttemptDetail,
    default_attempt_limit,
    detail_from_fetch,
    is_significant,
    merge_attempt_detail,
    resolve_attempt_limit,
)
from linkpatrol.config import ConfigError, default_config, validate_config
from linkpatrol.fetcher import FetchResult


def test_reservation_never_overshoots_limit_under_contention():
    budget = AttemptBudget(100)
    accepted = []
    denied = []
    lock = threading.Lock()

    def worker():
        local_accepted = 0
        local_denied = 0
        for _ in range(200):
            if budget.reserve():
                local_accepted += 1
            else:
                local_denied += 1
        with lock:
            accepted.append(local_accepted)
            denied.append(local_denied)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert budget.used == 100
    assert sum(accepted) == 100
    assert budget.denied == sum(denied) == 8 * 200 - 100
    assert budget.exhausted is True
    assert budget.snapshot() == {
        "limit": 100, "used": 100, "denied": 1500,
        "remaining": 0, "exhausted": True,
    }


def test_unlimited_budget_always_allows():
    budget = AttemptBudget(None)
    assert all(budget.reserve() for _ in range(1000))
    assert budget.exhausted is False
    assert budget.snapshot()["limit"] is None
    assert budget.snapshot()["remaining"] is None


def test_default_limit_scales_with_pages_and_retries():
    assert default_attempt_limit(100, 2) == 100 * 2 * 3 + 10
    assert default_attempt_limit(10, 0) == 10 * 2 * 1 + 10
    # retries + 1 attempts per target, worst case
    assert default_attempt_limit(5, 4) == 5 * 2 * 5 + 10


def test_resolve_uses_override_otherwise_derives():
    config = default_config("http://127.0.0.1:5001/")
    derived = resolve_attempt_limit(config)
    assert derived == default_attempt_limit(
        config["limits"]["max_pages"], config["limits"]["retries"]
    )
    config["limits"]["max_attempts"] = 42
    assert resolve_attempt_limit(config) == 42


def test_max_attempts_validation():
    config = default_config("http://127.0.0.1:5001/")
    validate_config(config)  # None default is accepted
    config["limits"]["max_attempts"] = 0
    with pytest.raises(ConfigError):
        validate_config(config)
    config["limits"]["max_attempts"] = "nope"
    with pytest.raises(ConfigError):
        validate_config(config)


def _fetch_result(**overrides):
    base = FetchResult(
        requested_url="http://x/t", final_url="http://x/t", status_code=500,
        error=None, attempts=3, first_failure_code=500,
        final_failure_code=500, budget_skipped=False,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_detail_from_fetch_and_significance():
    detail = detail_from_fetch("http://x/t", "crawl", _fetch_result())
    assert detail.attempts == 3 and detail.retry_count == 2
    assert detail.first_failure_code == detail.final_failure_code == 500
    assert is_significant(detail)
    healthy = detail_from_fetch(
        "http://x/ok", "check",
        _fetch_result(status_code=200, attempts=1, first_failure_code=None,
                      final_failure_code=None),
    )
    assert not is_significant(healthy)
    denied = detail_from_fetch(
        "http://x/skip", "check",
        _fetch_result(status_code=None, attempts=0, first_failure_code=None,
                      final_failure_code=None, budget_skipped=True,
                      error="budget_exhausted"),
    )
    assert is_significant(denied) and denied.retry_count == 0


def test_merge_keeps_first_failure_and_accumulates_attempts():
    head = AttemptDetail(
        target_url="http://x/t", phase="check", attempts=1, retry_count=0,
        first_failure_code=405, final_failure_code=405,
    )
    get = AttemptDetail(
        target_url="http://x/t", phase="check", attempts=2, retry_count=1,
        first_failure_code=500, final_failure_code=None, error=None,
    )
    merge_attempt_detail(head, get)
    assert head.attempts == 3 and head.retry_count == 1
    assert head.first_failure_code == 405  # earliest probe wins
    assert head.final_failure_code is None


def test_merge_fills_first_failure_and_error_from_later_probe():
    first = AttemptDetail(
        target_url="http://x/t", phase="check", attempts=1,
    )
    second = AttemptDetail(
        target_url="http://x/t", phase="check", attempts=1,
        first_failure_code=503, final_failure_code=503, error="HTTPError: 503",
    )
    merge_attempt_detail(first, second)
    assert first.first_failure_code == 503
    assert first.final_failure_code == 503
    assert first.error == "HTTPError: 503"
