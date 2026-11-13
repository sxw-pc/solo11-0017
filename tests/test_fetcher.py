"""Fetching policy tests against the local mock server."""

from linkpatrol.budget import AttemptBudget, BUDGET_EXHAUSTED_ERROR
from linkpatrol.fetcher import Fetcher

def test_retry_404_timeout_and_content_helpers(reset_mock):
    fetcher = Fetcher(timeout_s=2, retries=2, backoff_s=0)
    assert fetcher.fetch(reset_mock.base_url + "/unstable").status_code == 200
    assert fetcher.request_count == 3
    missing = Fetcher(timeout_s=2, retries=3, backoff_s=0)
    assert missing.fetch(reset_mock.base_url + "/missing").status_code == 404
    assert missing.request_count == 1
    slow = Fetcher(timeout_s=0.05, retries=0, backoff_s=0)
    assert slow.fetch(reset_mock.base_url + "/slow").error is not None
    page = Fetcher(timeout_s=2, backoff_s=0).fetch(reset_mock.base_url + "/about")
    assert page.is_ok and page.is_html and "About" in page.text

def test_redirect_chain_recording(reset_mock):
    fetcher = Fetcher(timeout_s=2, retries=0, max_redirects=5, backoff_s=0)
    result = fetcher.fetch(reset_mock.base_url + "/hop-1")
    assert result.status_code == 200 and result.final_url.endswith("/landing")
    assert [hop["status"] for hop in result.redirect_chain] == [302, 301, 307]
    assert [hop["location"].rsplit("/", 1)[-1] for hop in result.redirect_chain] == [
        "hop-2", "hop-3", "landing"]

def test_redirect_loop_and_hop_limit(reset_mock):
    loop = Fetcher(timeout_s=2, retries=0, max_redirects=8, backoff_s=0)
    assert loop.fetch(reset_mock.base_url + "/loop-a").error == "redirect_loop"
    limited = Fetcher(timeout_s=2, retries=0, max_redirects=1, backoff_s=0)
    assert limited.fetch(reset_mock.base_url + "/hop-1").error == "too_many_redirects"


def test_budget_denied_before_first_attempt_makes_synthetic_result(reset_mock):
    fetcher = Fetcher(timeout_s=2, retries=2, backoff_s=0,
                      budget=AttemptBudget(0))
    result = fetcher.fetch(reset_mock.base_url + "/about")
    assert result.error == BUDGET_EXHAUSTED_ERROR
    assert result.budget_skipped is True
    assert result.attempts == 0 and fetcher.request_count == 0
    assert result.first_failure_code is None and result.final_failure_code is None


def test_budget_cuts_retries_midway_and_never_overshoots(reset_mock):
    # Only one slot: the first 500 is observed, the retry has no budget.
    fetcher = Fetcher(timeout_s=2, retries=2, backoff_s=0,
                      budget=AttemptBudget(1))
    result = fetcher.fetch(reset_mock.base_url + "/server-error")
    assert result.status_code == 500
    assert result.budget_skipped is True
    assert result.attempts == 1
    assert result.first_failure_code == result.final_failure_code == 500
    assert fetcher.budget.used == 1 and fetcher.request_count == 1
    # A second fetch gets nothing through and performs no network IO.
    again = fetcher.fetch(reset_mock.base_url + "/about")
    assert again.error == BUDGET_EXHAUSTED_ERROR and again.attempts == 0
    assert fetcher.budget.used == 1


def test_telemetry_records_recovered_retry_and_4xx(reset_mock):
    recovered = Fetcher(timeout_s=2, retries=2, backoff_s=0)
    result = recovered.fetch(reset_mock.base_url + "/unstable")
    assert result.status_code == 200 and result.attempts == 3
    assert result.first_failure_code == 500
    assert result.final_failure_code is None  # recovered
    missing = Fetcher(timeout_s=2, retries=2, backoff_s=0)
    not_found = missing.fetch(reset_mock.base_url + "/missing")
    assert not_found.attempts == 1
    assert not_found.first_failure_code == not_found.final_failure_code == 404


def test_network_error_uses_null_failure_codes(reset_mock):
    fetcher = Fetcher(timeout_s=0.05, retries=1, backoff_s=0)
    result = fetcher.fetch(reset_mock.base_url + "/slow")
    assert result.error is not None and result.attempts == 2
    assert result.first_failure_code is None and result.final_failure_code is None
