"""Bounded crawling behaviour."""

from linkpatrol.crawler import Crawler
from linkpatrol.fetcher import Fetcher

def crawl(config, fetcher=None):
    return Crawler(config, fetcher=fetcher).run()

def test_depth_and_page_limits(make_config, mocksite):
    depth = crawl(make_config(mocksite.base_url + "/depth/1", limits={"max_depth": 1}))
    assert set(depth.pages) == {
        mocksite.base_url + "/depth/1", mocksite.base_url + "/depth/2",
        mocksite.base_url + "/"}
    assert depth.stats.skipped_by_limits >= 1
    limited = crawl(make_config(mocksite.base_url, limits={"max_pages": 3, "max_depth": 5}))
    assert len(limited.pages) == 3 and limited.stats.skipped_by_limits > 0

def test_exclude_and_include_regexes(make_config, mocksite):
    excluded = crawl(make_config(mocksite.base_url, scope={"exclude": [r"^/section/"]}))
    assert not any("/section/" in url for url in excluded.pages)
    assert excluded.stats.excluded_by_scope >= 1
    included = crawl(make_config(
        mocksite.base_url, scope={"include": [r"^/$", r"^/about$"]},
        limits={"max_depth": 3}))
    assert set(included.pages) == {mocksite.base_url + "/", mocksite.base_url + "/about"}

def test_robots_skip_and_non_html_recording(reset_mock, make_config):
    reset_mock.app.config["COUNTERS"].clear()
    result = crawl(make_config(reset_mock.base_url))
    assert result.stats.skipped_by_robots >= 1
    assert reset_mock.app.config["COUNTERS"]["private"] == 0
    page = result.pages[reset_mock.base_url + "/non-html"]
    assert page.status_code == 200 and page.links == [] and not page.is_html
    assert result.stats.non_html == 1

def test_external_pages_are_not_crawled(make_config, mocksite, ext_site):
    result = crawl(make_config(mocksite.base_url))
    assert not any(url.startswith(ext_site.base_url) for url in result.pages)

def test_serial_crawls_are_deterministic_and_concurrency_runs(
        reset_mock, make_config):
    config = make_config(reset_mock.base_url, limits={"max_pages": 25})
    first = crawl(config, Fetcher(timeout_s=2, retries=2, backoff_s=0))
    second = crawl(config, Fetcher(timeout_s=2, retries=2, backoff_s=0))
    assert set(first.pages) == set(second.pages)
    assert first.stats.excluded_by_scope == second.stats.excluded_by_scope
    concurrent = crawl(make_config(
        reset_mock.base_url, limits={"max_pages": 8, "concurrency": 3}))
    assert len(concurrent.pages) == 8
