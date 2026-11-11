"""Fetching policy tests against the local mock server."""

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
