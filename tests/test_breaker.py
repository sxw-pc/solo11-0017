"""Per-host circuit breaker: unit, integration, and CLI acceptance tests."""

import json
import threading
from urllib.parse import urlsplit

import pytest

from linkpatrol.baseline import diff_runs
from linkpatrol.breaker import (
    CIRCUIT_OPEN_ERROR,
    BreakerConfig,
    CircuitBreaker,
    breaker_from_config,
)
from linkpatrol.checker import check_links
from linkpatrol.cli import main
from linkpatrol.config import ConfigError, default_config, validate_config
from linkpatrol.crawler import crawl_site
from linkpatrol.fetcher import Fetcher
from linkpatrol.report import build_report, render_static_html
from linkpatrol.store import Store
from linkpatrol.urlnorm import host_key

HOST = "ext.example"

class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

def make_breaker(clock=None, **kwargs) -> CircuitBreaker:
    return CircuitBreaker(BreakerConfig(enabled=True, **kwargs),
                          clock=clock or FakeClock())

def fail(breaker: CircuitBreaker, host: str = HOST, times: int = 1) -> None:
    for _ in range(times):
        breaker.record_result(host, 500, None)

# --- configuration -------------------------------------------------------

def test_circuit_config_defaults_and_disabled_breaker():
    config = default_config("http://127.0.0.1:5001/")
    assert config["circuit"] == {
        "enabled": False,
        "threshold": 5,
        "window_s": 300.0,
        "count_only_server_errors": True,
        "probe_allowance": 0,
    }
    assert not breaker_from_config(config).enabled

def test_circuit_config_validation():
    def invalid(**update):
        config = default_config("http://127.0.0.1:5001/")
        config["circuit"].update(update)
        return config

    for update in (
        {"enabled": "yes"},
        {"threshold": 0},
        {"threshold": 1.5},
        {"window_s": 0},
        {"count_only_server_errors": 1},
        {"probe_allowance": -1},
    ):
        with pytest.raises(ConfigError):
            validate_config(invalid(**update))
    config = validate_config(invalid(enabled=True, threshold=2, window_s=30,
                                     probe_allowance=1))
    breaker = breaker_from_config(config)
    assert breaker.enabled and breaker.config.threshold == 2
    assert breaker.config.window_s == 30.0 and breaker.config.probe_allowance == 1

# --- breaker state machine ----------------------------------------------

def test_threshold_blocks_and_reports_failure_count():
    breaker = make_breaker(threshold=3)
    fail(breaker, times=2)
    decision = breaker.decide(HOST)
    assert decision.allowed and decision.failures == 2
    fail(breaker)
    decision = breaker.decide(HOST)
    assert not decision.allowed and decision.failures == 3
    assert breaker.is_open(HOST)

def test_success_resets_the_streak():
    breaker = make_breaker(threshold=2)
    fail(breaker, times=2)
    breaker.record_result(HOST, 200, None)
    assert breaker.failure_count(HOST) == 0
    assert breaker.decide(HOST).allowed

def test_window_expiry_prunes_old_failures():
    clock = FakeClock()
    breaker = make_breaker(clock=clock, threshold=2, window_s=10.0)
    fail(breaker)
    clock.now += 20.0
    fail(breaker)
    assert breaker.failure_count(HOST) == 1
    assert breaker.decide(HOST).allowed

def test_4xx_counting_is_configurable():
    strict = make_breaker(threshold=1)
    strict.record_result(HOST, 404, None)
    assert strict.failure_count(HOST) == 0
    lax = make_breaker(threshold=1, count_only_server_errors=False)
    lax.record_result(HOST, 404, None)
    assert lax.failure_count(HOST) == 1
    assert not lax.decide(HOST).allowed

def test_network_errors_always_count():
    breaker = make_breaker(threshold=1)
    breaker.record_result(HOST, None, "URLError: connection refused")
    assert breaker.is_open(HOST)

def test_probe_allowance_and_recovery():
    breaker = make_breaker(threshold=1, probe_allowance=2)
    fail(breaker)
    first, second, third = (breaker.decide(HOST) for _ in range(3))
    assert first.allowed and first.probing
    assert second.allowed and second.probing
    assert not third.allowed
    # A successful probe closes the episode and restores the allowance.
    breaker.record_result(HOST, 200, None)
    fail(breaker)
    assert breaker.decide(HOST).probing

def test_disabled_breaker_allows_and_records_nothing():
    breaker = CircuitBreaker()
    breaker.record_result(HOST, 500, None)
    assert breaker.decide(HOST).allowed
    assert breaker.failure_count(HOST) == 0
    assert breaker.snapshot() == {}

def test_concurrent_recording_is_thread_safe():
    breaker = make_breaker(threshold=10_000, window_s=3600.0)

    def worker():
        for _ in range(200):
            breaker.record_result(HOST, 500, None)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert breaker.failure_count(HOST) == 1600

# --- fetcher recording ---------------------------------------------------

def test_fetcher_records_each_settled_fetch_once(reset_mock):
    breaker = CircuitBreaker(BreakerConfig(enabled=True, threshold=10))
    fetcher = Fetcher(timeout_s=2, retries=2, backoff_s=0, breaker=breaker)
    host = host_key(reset_mock.base_url)
    fetcher.fetch(reset_mock.base_url + "/server-error")
    # retries=2 means three attempts, but only one settled outcome is recorded.
    assert fetcher.request_count == 3
    assert breaker.failure_count(host) == 1
    # A 404 proves the host answers: not counted, and the streak resets.
    fetcher.fetch(reset_mock.base_url + "/missing")
    assert breaker.failure_count(host) == 0

def test_fetcher_records_network_errors():
    breaker = CircuitBreaker(BreakerConfig(enabled=True, threshold=1))
    fetcher = Fetcher(timeout_s=0.2, retries=0, backoff_s=0, breaker=breaker)
    result = fetcher.fetch("http://127.0.0.1:9/unreachable")
    assert result.error is not None
    assert breaker.is_open("127.0.0.1:9")

def test_default_fetcher_breaker_is_inert(reset_mock):
    fetcher = Fetcher(timeout_s=2, retries=0, backoff_s=0)
    fetcher.fetch(reset_mock.base_url + "/server-error")
    assert not fetcher.breaker.enabled
    assert fetcher.breaker.snapshot() == {}

# --- checker integration against the external link farm ------------------

def farm_config(make_config, mocksite, **circuit):
    overrides = {"enabled": True, "threshold": 3}
    overrides.update(circuit)
    return make_config(
        mocksite.base_url + "/ext-farm", limits={"retries": 0}, circuit=overrides
    )

def ext_host(ext_site) -> str:
    return host_key(ext_site.base_url)

def test_external_links_trip_the_breaker(mocksite, ext_site, make_config):
    ext_site.app.config["COUNTERS"].clear()
    config = farm_config(make_config, mocksite)
    report = check_links(config, crawl_site(config))
    skipped = report.circuit_skipped
    assert len(skipped) == 9
    for record in skipped:
        assert record.verdict == "skipped"
        assert record.error == CIRCUIT_OPEN_ERROR
        assert record.status_code is None and not record.ok
        assert record.skip_detail == {
            "reason": CIRCUIT_OPEN_ERROR,
            "host": ext_host(ext_site),
            "failures": 3,
        }
    broken = report.broken
    assert len(broken) == 3 and all(r.status_code == 500 for r in broken)
    # Request count converges to the threshold instead of 12 links.
    assert ext_site.app.config["COUNTERS"]["fail"] == 3

def test_request_count_converges_with_threshold_and_retries(
        mocksite, ext_site, make_config):
    ext_site.app.config["COUNTERS"].clear()
    config = farm_config(make_config, mocksite)
    config["limits"]["retries"] = 2
    report = check_links(config, crawl_site(config))
    assert len(report.circuit_skipped) == 9
    # 3 probed links x (1 initial + 2 retried) HEAD requests each.
    assert ext_site.app.config["COUNTERS"]["fail"] == 9

def test_probe_allowance_lets_a_few_requests_through(mocksite, ext_site, make_config):
    ext_site.app.config["COUNTERS"].clear()
    config = farm_config(make_config, mocksite, probe_allowance=2)
    report = check_links(config, crawl_site(config))
    assert ext_site.app.config["COUNTERS"]["fail"] == 5  # 3 to trip + 2 probes
    skipped = report.circuit_skipped
    assert len(skipped) == 7
    assert {record.skip_detail["failures"] for record in skipped} == {5}

def test_internal_links_are_never_skipped(reset_mock, make_config):
    config = make_config(
        reset_mock.base_url,
        limits={"retries": 0},
        circuit={"enabled": True, "threshold": 1},
    )
    report = check_links(config, crawl_site(config))
    assert report.circuit_skipped == []
    record = report.checks[reset_mock.base_url + "/server-error"]
    assert record.status_code == 500 and not record.skipped

def test_crawler_fetches_are_never_blocked(reset_mock, make_config):
    plain = crawl_site(make_config(reset_mock.base_url, limits={"retries": 0}))
    config = make_config(
        reset_mock.base_url,
        limits={"retries": 0},
        circuit={"enabled": True, "threshold": 1},
    )
    tripped = crawl_site(config)
    # The breaker only observes crawler traffic; robots and fetching are
    # untouched, so the page sets are identical.
    assert set(tripped.pages) == set(plain.pages)
    assert tripped.stats.skipped_by_robots == plain.stats.skipped_by_robots >= 1
    assert reset_mock.app.config["COUNTERS"]["private"] == 0

def test_checker_shares_the_fetchers_breaker_state(mocksite, ext_site, make_config):
    ext_site.app.config["COUNTERS"].clear()
    config = farm_config(make_config, mocksite)
    fetcher = Fetcher(timeout_s=2, retries=0, backoff_s=0,
                      breaker=breaker_from_config(config))
    crawl = crawl_site(config, fetcher=fetcher)
    # Two failures already recorded against the external host (e.g. from
    # redirect hops during the crawl) count toward the checker's decisions.
    fetcher.breaker.record_result(ext_host(ext_site), 500, None)
    fetcher.breaker.record_result(ext_host(ext_site), 500, None)
    report = check_links(config, crawl, fetcher=fetcher)
    assert ext_site.app.config["COUNTERS"]["fail"] == 1  # one more trip the 3rd
    assert len(report.circuit_skipped) == 11

def test_explicit_fetcher_without_breaker_stays_disabled(
        mocksite, ext_site, make_config):
    config = farm_config(make_config, mocksite)
    crawl = crawl_site(config)
    report = check_links(config, crawl, Fetcher(timeout_s=2, retries=0, backoff_s=0))
    assert report.circuit_skipped == []
    assert len(report.broken) == 12

def scan_once(config) -> dict:
    fetcher = Fetcher(timeout_s=config["limits"]["timeout_s"],
                      retries=config["limits"]["retries"], backoff_s=0,
                      breaker=breaker_from_config(config))
    crawl = crawl_site(config, fetcher=fetcher)
    report = check_links(config, crawl, fetcher=fetcher)
    return {record.url: record.skip_detail["failures"]
            for record in report.circuit_skipped}

def test_serial_and_concurrent_scans_trip_identically(mocksite, ext_site, make_config):
    outcomes = []
    for concurrency in (1, 4):
        config = farm_config(make_config, mocksite)
        config["limits"]["concurrency"] = concurrency
        outcomes.append(scan_once(config))
    assert len(outcomes[0]) == 9
    assert outcomes[0] == outcomes[1]

# --- storage -------------------------------------------------------------

SOURCE = "http://127.0.0.1:5001/"

def skipped_check(url: str, failures: int = 3) -> dict:
    return {
        "url": url,
        "final_url": url,
        "status_code": None,
        "redirect_chain": [],
        "anchor_ok": True,
        "ok": False,
        "error": CIRCUIT_OPEN_ERROR,
        "checked_at": "2026-01-01T00:00:00+00:00",
        "skipped": True,
        "skip_detail": {"reason": CIRCUIT_OPEN_ERROR, "host": HOST,
                        "failures": failures},
    }

def broken_check(url: str) -> dict:
    return {
        "url": url,
        "final_url": url,
        "status_code": 500,
        "redirect_chain": [],
        "anchor_ok": True,
        "ok": False,
        "error": None,
        "checked_at": "2026-01-01T00:00:00+00:00",
    }

def add_link(store, run_id: int, target: str, line_no: int = 1) -> None:
    store.insert_link(run_id, SOURCE, {
        "target_url": target, "raw_href": target, "kind": "page",
        "scope": "external", "line_no": line_no, "anchor": ""})

def test_skipped_check_roundtrip_and_uniqueness(store):
    run_id = store.create_run(SOURCE, {})
    check = skipped_check("http://ext.example/fail/0")
    assert store.insert_check(run_id, check)
    # The UNIQUE(run_id, url) constraint still applies to skipped rows.
    assert not store.insert_check(run_id, check)
    assert store.write_errors
    row = store.get_checks(run_id)[0]
    assert row["skipped"] is True
    assert row["error"] == CIRCUIT_OPEN_ERROR and row["status_code"] is None
    assert row["skip_detail"] == {
        "reason": CIRCUIT_OPEN_ERROR, "host": HOST, "failures": 3}

def test_broken_identities_excludes_skips(store):
    run_id = store.create_run(SOURCE, {})
    target = "http://ext.example/fail/0"
    add_link(store, run_id, target)
    store.insert_check(run_id, skipped_check(target))
    assert store.broken_identities(run_id) == []

def test_checks_table_migrates_from_old_schema(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE checks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_id INTEGER NOT NULL, url TEXT NOT NULL, final_url TEXT, "
        "status_code INTEGER, redirect_chain_json TEXT NOT NULL DEFAULT '[]', "
        "anchor_ok INTEGER NOT NULL DEFAULT 1, ok INTEGER NOT NULL DEFAULT 0, "
        "error TEXT, checked_at TEXT NOT NULL, UNIQUE(run_id, url))"
    )
    conn.execute(
        "INSERT INTO checks(run_id, url, checked_at) VALUES (9, 'http://x/', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        row = store.get_checks(9)[0]
        assert row["skipped"] is False and row["skip_detail"] == {}
        run_id = store.create_run(SOURCE, {})
        assert store.insert_check(run_id, skipped_check("http://ext.example/a"))
        assert store.get_checks(run_id)[0]["skipped"] is True
    finally:
        store.close()

# --- baseline diff -------------------------------------------------------

def test_baseline_diff_never_counts_skips_as_new_broken(store):
    baseline_run = store.create_run(SOURCE, {})
    current_run = store.create_run(SOURCE, {})
    for index in range(4):
        target = f"http://ext.example/fail/{index}"
        add_link(store, baseline_run, target, line_no=index)
        add_link(store, current_run, target, line_no=index)
        store.insert_check(baseline_run, broken_check(target))
        if index == 0:
            store.insert_check(current_run, broken_check(target))
        else:
            store.insert_check(current_run, skipped_check(target))
    store.set_baseline("prod", baseline_run)
    diff = diff_runs(store, "prod", current_run)
    assert diff["summary"]["new_broken"] == 0
    assert diff["summary"]["persistent"] == 1
    assert diff["summary"]["fixed"] == 3
    assert all(item["error"] != CIRCUIT_OPEN_ERROR for item in diff["persistent"])

# --- report ---------------------------------------------------------------

def test_report_separates_and_groups_circuit_skips(store):
    run_id = store.create_run(SOURCE, {})
    store.insert_page(run_id, {"url": SOURCE, "depth": 0, "status_code": 200,
                               "content_hash": "h", "duration_ms": 1, "error": None})
    real_error = "http://ext.example/timeout"
    add_link(store, run_id, real_error)
    store.insert_check(run_id, {
        "url": real_error, "final_url": real_error, "status_code": None,
        "redirect_chain": [], "anchor_ok": True, "ok": False,
        "error": "URLError: timed out", "checked_at": "2026-01-01T00:00:00+00:00"})
    for index in range(2):
        target = f"http://ext.example/fail/{index}"
        add_link(store, run_id, target, line_no=index + 2)
        store.insert_check(run_id, skipped_check(target))
    data = build_report(store, run_id)
    assert data["summary"]["broken"] == 1
    assert data["summary"]["circuit_skips"] == 2
    assert [item["target_url"] for item in data["broken"]] == [real_error]
    assert data["circuit"]["hosts"] == [
        {"host": HOST, "skipped": 2, "max_failures": 3}]
    assert {item["target_url"] for item in data["circuit"]["skips"]} == {
        "http://ext.example/fail/0", "http://ext.example/fail/1"}
    html = render_static_html(data)
    assert "Circuit-breaker skips" in html
    assert 'class="pill warn">skipped · circuit_open' in html
    assert "URLError: timed out" in html

def test_report_without_skips_has_no_circuit_fields(store):
    run_id = store.create_run(SOURCE, {})
    store.insert_page(run_id, {"url": SOURCE, "depth": 0, "status_code": 200,
                               "content_hash": "h", "duration_ms": 1, "error": None})
    target = SOURCE + "about"
    store.insert_link(run_id, SOURCE, {
        "target_url": target, "raw_href": "/about", "kind": "page",
        "scope": "internal", "line_no": 1, "anchor": ""})
    store.insert_check(run_id, {
        "url": target, "final_url": target, "status_code": 200,
        "redirect_chain": [], "anchor_ok": True, "ok": True, "error": None,
        "checked_at": "2026-01-01T00:00:00+00:00"})
    data = build_report(store, run_id)
    assert "circuit" not in data
    assert "circuit_skips" not in data["summary"]
    assert "Circuit-breaker" not in render_static_html(data)

# --- CLI end to end -------------------------------------------------------

def write_scan_config(tmp_path, name: str, seed: str, circuit_section: str):
    disk = tmp_path.as_posix()
    path = tmp_path / name
    path.write_text(f"""
seed = "{seed}"
[scope]
exclude = ["^/private/"]
[limits]
max_depth = 3
max_pages = 50
timeout_s = 2.0
retries = 0
delay_ms = 0
concurrency = 1
max_redirects = 5
[robots]
respect = true
user_agent = "linkpatrol-test/0.1"
[external]
check = true
{circuit_section}
[output]
dir = "{disk}/reports"
database = "{disk}/out/linkpatrol.db"
sitemap = "{disk}/out/sitemap.xml"
""", encoding="utf-8")
    return path

CIRCUIT_ON = """[circuit]
enabled = true
threshold = 3
window_s = 300.0
count_only_server_errors = true
probe_allowance = 0
"""

def test_cli_circuit_breaker_end_to_end(mocksite, ext_site, tmp_path, capsys):
    seed = mocksite.base_url + "/ext-farm"
    off = write_scan_config(tmp_path, "scan_off.toml", seed,
                            "[circuit]\nenabled = false\n")
    on = write_scan_config(tmp_path, "scan_on.toml", seed, CIRCUIT_ON)
    database = tmp_path / "out" / "linkpatrol.db"

    # Run 1: breaker off — every external link is really requested and the
    # results are field-identical to the original implementation.
    ext_site.app.config["COUNTERS"].clear()
    assert main(["scan", "--config", str(off)]) == 0
    store = Store(database)
    try:
        run1 = store.get_checks(1)
        assert len(run1) == 12
        assert all(not row["skipped"] for row in run1)
        assert all(row["status_code"] == 500 and not row["ok"] for row in run1)
        assert all(row["error"] is None for row in run1)
        assert "circuit_skips" not in store.get_run(1)["stats"]
    finally:
        store.close()
    assert ext_site.app.config["COUNTERS"]["fail"] == 12
    report1 = json.loads((tmp_path / "reports" / "run_1.json").read_text(encoding="utf-8"))
    assert "circuit" not in report1
    assert "circuit_skips" not in report1["summary"]
    assert main(["baseline", "set", "--name", "prod", "--run", "1",
                 "--db", str(database)]) == 0

    # Run 2: breaker on — requests converge to the threshold, the rest skip.
    ext_site.app.config["COUNTERS"].clear()
    assert main(["scan", "--config", str(on)]) == 0
    host = f"127.0.0.1:{urlsplit(ext_site.base_url).port}"
    store = Store(database)
    try:
        run2 = store.get_checks(2)
        assert len(run2) == 12
        skipped = [row for row in run2 if row["skipped"]]
        broken = [row for row in run2 if not row["skipped"]]
        assert len(skipped) == 9 and len(broken) == 3
        for row in skipped:
            assert row["error"] == CIRCUIT_OPEN_ERROR
            assert row["status_code"] is None and not row["ok"]
            assert row["skip_detail"] == {
                "reason": CIRCUIT_OPEN_ERROR, "host": host, "failures": 3}
        assert store.get_run(2)["stats"]["circuit_skips"] == 9
    finally:
        store.close()
    assert ext_site.app.config["COUNTERS"]["fail"] == 3

    # The report separates skips from real network errors, grouped by host.
    report2 = json.loads((tmp_path / "reports" / "run_2.json").read_text(encoding="utf-8"))
    assert report2["summary"]["broken"] == 3
    assert report2["summary"]["circuit_skips"] == 9
    assert report2["circuit"]["hosts"] == [
        {"host": host, "skipped": 9, "max_failures": 3}]
    html = (tmp_path / "reports" / "run_2.html").read_text(encoding="utf-8")
    assert "Circuit-breaker skips" in html
    assert "skipped · circuit_open" in html

    # The baseline diff never treats skips as new breakage.
    capsys.readouterr()
    code = main(["diff", "--baseline", "prod", "--run", "2", "--db", str(database),
                 "--fail-on", "new-broken"])
    assert code == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["summary"]["new_broken"] == 0
    assert diff["summary"]["persistent"] == 3
