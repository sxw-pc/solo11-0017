"""Link checker reuse, fallback, redirect, and anchor tests."""

from linkpatrol.budget import AttemptBudget, BUDGET_EXHAUSTED_ERROR
from linkpatrol.checker import check_links
from linkpatrol.crawler import crawl_site
from linkpatrol.fetcher import Fetcher

def prepare(reset_mock, make_config):
    config = make_config(reset_mock.base_url)
    crawl = crawl_site(config, Fetcher(timeout_s=2, retries=2, backoff_s=0))
    checker_fetcher = Fetcher(timeout_s=2, retries=2, backoff_s=0)
    calls = []
    original = checker_fetcher.fetch

    def tracked(url, method="GET", max_body_bytes=None):
        calls.append((url, method))
        return original(url, method=method, max_body_bytes=max_body_bytes)

    checker_fetcher.fetch = tracked
    report = check_links(config, crawl, checker_fetcher)
    return reset_mock, report, calls

def test_internal_pages_are_reused(reset_mock, make_config):
    site, report, calls = prepare(reset_mock, make_config)
    about = site.base_url + "/about"
    assert report.checks[about].status_code == 200
    assert not any(url == about for url, _method in calls)

def test_head_405_falls_back_to_get(reset_mock, make_config, ext_site):
    _site, report, calls = prepare(reset_mock, make_config)
    target = ext_site.base_url + "/head-only"
    assert report.checks[target].status_code == 200 and report.checks[target].ok
    assert (target, "HEAD") in calls and (target, "GET") in calls

def test_anchor_presence_and_absence(reset_mock, make_config):
    site, report, _calls = prepare(reset_mock, make_config)
    missing = report.checks[site.base_url + "/anchor-page#absent"]
    present = report.checks[site.base_url + "/anchor-page#present"]
    assert not missing.anchor_ok and not missing.ok and missing.error == "anchor_missing"
    assert present.anchor_ok and present.ok

def test_external_404_and_redirect_loop_are_broken(reset_mock, make_config, ext_site):
    site, report, _calls = prepare(reset_mock, make_config)
    external = report.checks[ext_site.base_url + "/missing"]
    assert external.status_code == 404 and not external.ok
    loop = report.checks[site.base_url + "/loop-a"]
    assert loop.error == "redirect_loop" and not loop.ok

def test_long_redirects_are_warnings_and_5xx_is_broken(reset_mock, make_config):
    site, report, _calls = prepare(reset_mock, make_config)
    redirect = report.checks[site.base_url + "/hop-1"]
    assert redirect.ok and redirect.warning and redirect.verdict == "warning"
    assert len(redirect.redirect_chain) == 3
    error = report.checks[site.base_url + "/server-error"]
    assert error.status_code == 500 and not error.ok

def test_non_http_links_are_skipped(reset_mock, make_config):
    _site, report, _calls = prepare(reset_mock, make_config)
    assert report.skipped >= 3
    assert not any(url.startswith("mailto:") for url in report.checks)


def test_exhausted_budget_marks_remaining_targets_skipped(reset_mock, make_config):
    config = make_config(reset_mock.base_url)
    crawl = crawl_site(config, Fetcher(timeout_s=2, retries=0, backoff_s=0))
    checker_fetcher = Fetcher(timeout_s=2, retries=0, backoff_s=0)
    budget = AttemptBudget(0)  # crawl already finished; checker gets nothing
    report = check_links(config, crawl, checker_fetcher, budget=budget)
    assert report.skipped_budget >= 1
    budget_checks = [c for c in report.checks.values() if c.error == BUDGET_EXHAUSTED_ERROR]
    assert budget_checks and all(not c.ok and c.status_code is None for c in budget_checks)
    # No network attempts were made in the check phase.
    assert checker_fetcher.request_count == 0
    details = {d.target_url: d for d in report.attempt_details if d.phase == "check"}
    target = next(c.url for c in budget_checks)
    base = target.split("#", 1)[0]
    assert details[base].budget_skipped and details[base].attempts == 0


def test_check_phase_failure_attribution_for_5xx(reset_mock, make_config):
    config = make_config(reset_mock.base_url, limits={"retries": 2})
    crawl = crawl_site(config, Fetcher(timeout_s=2, retries=2, backoff_s=0))
    checker_fetcher = Fetcher(timeout_s=2, retries=2, backoff_s=0)
    report = check_links(config, crawl, checker_fetcher)
    # /server-error is reused from the crawl, so the check phase owns the
    # network probes for the check-only targets (assets and external links).
    asset = reset_mock.base_url + "/assets/missing.png"
    detail = next(d for d in report.attempt_details if d.target_url == asset)
    assert detail.phase == "check"
    assert detail.attempts == 1 and detail.retry_count == 0
    assert detail.first_failure_code == detail.final_failure_code == 404
    # The crawled 5xx target keeps its crawl-phase attribution untouched.
    crawled = next(
        d for d in crawl.attempt_details
        if d.target_url == reset_mock.base_url + "/server-error"
    )
    assert crawled.phase == "crawl" and crawled.attempts == 3
    assert crawled.final_failure_code == 500


def test_in_flight_budget_denial_stops_retries_without_overshoot(
        reset_mock, make_config):
    config = make_config(reset_mock.base_url, limits={"retries": 2})
    crawl = crawl_site(config, Fetcher(timeout_s=2, retries=0, backoff_s=0))
    budget = AttemptBudget(1)
    checker_fetcher = Fetcher(timeout_s=2, retries=2, backoff_s=0, budget=budget)
    report = check_links(config, crawl, checker_fetcher, budget=budget)
    assert budget.used <= 1
    assert report.skipped_budget >= 1
    assert any(d.budget_skipped for d in report.attempt_details)
    # Every denied target maps to a broken check rather than a hard crash.
    assert any(c.error == BUDGET_EXHAUSTED_ERROR for c in report.checks.values())
