"""Link checker reuse, fallback, redirect, and anchor tests."""

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
