"""End-to-end batch scans against the live mock sites."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from linkpatrol.batch import (
    EXIT_ALL_FAILED,
    EXIT_OK,
    EXIT_PARTIAL,
    GlobalLimiter,
    load_manifest,
    run_batch,
)
from linkpatrol.cli import main
from linkpatrol.config import default_config
from linkpatrol.store import Store


def _site_toml(path: Path, seed: str, db: str, *, delay_ms=0, concurrency=1,
               timeout_s=2.0, max_pages=15, retries=0) -> None:
    path.write_text(
        f"""
seed = "{seed}"
[limits]
max_depth = 3
max_pages = {max_pages}
timeout_s = {timeout_s}
retries = {retries}
delay_ms = {delay_ms}
concurrency = {concurrency}
max_redirects = 5
[robots]
respect = true
user_agent = "linkpatrol-batch/0.1"
[external]
check = true
[normalize]
drop_params = []
[output]
dir = "{path.parent / 'reports'}"
database = "{db}"
sitemap = "{path.parent / (path.stem + '.xml')}"
""",
        encoding="utf-8",
    )


def _manifest(path: Path, entries: list[tuple[str, str]], *,
              max_concurrency=4, database=None, reports_dir=None,
              sitemap_dir=None) -> Path:
    lines = ["[batch]", f"max_concurrency = {max_concurrency}"]
    if database:
        lines.append(f'database = "{database}"')
    if reports_dir:
        lines.append(f'reports_dir = "{reports_dir}"')
    if sitemap_dir:
        lines.append(f'sitemap_dir = "{sitemap_dir}"')
    lines.append("")
    for name, config_name in entries:
        lines.append("[[sites]]")
        lines.append(f'name = "{name}"')
        lines.append(f'config = "{config_name}"')
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _build_two_site_manifest(tmp_path: Path, mocksite, ext_site, *,
                             delay_a=0, delay_b=0, max_concurrency=4) -> Path:
    db = str(tmp_path / "shared.db")
    _site_toml(tmp_path / "a.toml", mocksite.base_url + "/", db,
               delay_ms=delay_a, concurrency=2)
    _site_toml(tmp_path / "b.toml", ext_site.base_url + "/", db,
               delay_ms=delay_b, concurrency=1)
    return _manifest(
        tmp_path / "batch.toml",
        [("alpha", "a.toml"), ("beta", "b.toml")],
        max_concurrency=max_concurrency,
        database=db,
        reports_dir=str(tmp_path / "batch_reports"),
    )


def test_batch_runs_all_sites_into_one_database(
        reset_mock, ext_site, tmp_path):
    manifest_path = _build_two_site_manifest(tmp_path, reset_mock, ext_site)
    manifest = load_manifest(manifest_path)
    db = manifest.database
    lock = __import__("threading").Lock()
    store = Store(db, write_lock=lock)
    limiter = GlobalLimiter(manifest.max_concurrency)
    try:
        result = run_batch(manifest, store, limiter)
    finally:
        store.close()

    assert result.exit_code == EXIT_OK
    assert result.counts == {
        "total": 2, "completed": 2, "failed": 0, "skipped": 0, "invalid": 0,
    }
    assert result.peak_inflight <= manifest.max_concurrency

    store = Store(db)
    try:
        runs = store.list_runs()
        assert len(runs) == 2
        # Every batch run is attributable to a site and the batch.
        assert {run["site_name"] for run in runs} == {"alpha", "beta"}
        assert {run["batch_id"] for run in runs} == {result.batch_id}
        batch = store.get_batch(result.batch_id)
        assert batch["status"] == "completed"
        rows = {row["name"]: row for row in store.get_batch_sites(result.batch_id)}
        assert rows["alpha"]["status"] == "completed"
        assert rows["beta"]["status"] == "completed"
        assert rows["alpha"]["run_id"] != rows["beta"]["run_id"]
    finally:
        store.close()

    # Per-site namespaced report files exist.
    alpha_report = tmp_path / "batch_reports" / "alpha" / (
        f"run_{rows['alpha']['run_id']}.json")
    beta_report = tmp_path / "batch_reports" / "beta" / (
        f"run_{rows['beta']['run_id']}.json")
    assert alpha_report.is_file() and beta_report.is_file()
    report = json.loads(alpha_report.read_text(encoding="utf-8"))
    assert report["run"]["seed"].startswith(reset_mock.base_url)


def test_batch_global_concurrency_is_hard_capped(reset_mock, ext_site, tmp_path):
    # Two sites each allowing local concurrency 4, but the global limiter is 2.
    db = str(tmp_path / "shared.db")
    _site_toml(tmp_path / "a.toml", reset_mock.base_url + "/", db,
               concurrency=4, max_pages=12)
    _site_toml(tmp_path / "b.toml", ext_site.base_url + "/", db,
               concurrency=4, max_pages=12)
    manifest_path = _manifest(
        tmp_path / "batch.toml",
        [("alpha", "a.toml"), ("beta", "b.toml")],
        max_concurrency=2,
        database=db,
    )
    manifest = load_manifest(manifest_path)
    store = Store(db)
    limiter = GlobalLimiter(2)
    try:
        result = run_batch(manifest, store, limiter)
    finally:
        store.close()
    assert result.exit_code == EXIT_OK
    assert result.peak_inflight <= 2
    assert limiter.peak_inflight <= 2


def test_batch_partial_failure_exit_code_four(
        reset_mock, ext_site, tmp_path):
    db = str(tmp_path / "shared.db")
    _site_toml(tmp_path / "good.toml", reset_mock.base_url + "/", db)
    # Point a site at an unused loopback port: every fetch times out/errors.
    _site_toml(tmp_path / "dead.toml", "http://127.0.0.1:9/slow", db,
               timeout_s=0.2, retries=0)
    manifest_path = _manifest(
        tmp_path / "batch.toml",
        [("good", "good.toml"), ("dead", "dead.toml")],
        max_concurrency=3,
        database=db,
    )
    manifest = load_manifest(manifest_path)
    store = Store(db)
    limiter = GlobalLimiter(3)
    try:
        result = run_batch(manifest, store, limiter)
    finally:
        store.close()
    assert result.exit_code == EXIT_PARTIAL
    assert result.counts["completed"] == 1 and result.counts["failed"] == 1
    store = Store(db)
    try:
        rows = {row["name"]: row for row in store.get_batch_sites(result.batch_id)}
        assert rows["good"]["status"] == "completed"
        assert rows["dead"]["status"] == "failed" and rows["dead"]["error"]
        dead_run = store.get_run(rows["dead"]["run_id"])
        assert dead_run["status"] == "failed"
        good_run = store.get_run(rows["good"]["run_id"])
        assert good_run["status"] == "completed"
    finally:
        store.close()


def test_batch_all_failed_exit_code_one(reset_mock, tmp_path):
    db = str(tmp_path / "shared.db")
    _site_toml(tmp_path / "dead1.toml", "http://127.0.0.1:1/a", db,
               timeout_s=0.2, retries=0)
    _site_toml(tmp_path / "dead2.toml", "http://127.0.0.1:2/b", db,
               timeout_s=0.2, retries=0)
    manifest_path = _manifest(
        tmp_path / "batch.toml",
        [("d1", "dead1.toml"), ("d2", "dead2.toml")],
        max_concurrency=2,
        database=db,
    )
    manifest = load_manifest(manifest_path)
    store = Store(db)
    try:
        result = run_batch(manifest, store, GlobalLimiter(2))
    finally:
        store.close()
    assert result.exit_code == EXIT_ALL_FAILED
    assert result.counts["failed"] == 2 and result.counts["completed"] == 0


def test_batch_invalid_config_entry_is_isolated(reset_mock, tmp_path):
    db = str(tmp_path / "shared.db")
    _site_toml(tmp_path / "good.toml", reset_mock.base_url + "/", db)
    (tmp_path / "bad.toml").write_text('seed = "ftp://127.0.0.1/x"\n',
                                       encoding="utf-8")
    manifest_path = _manifest(
        tmp_path / "batch.toml",
        [("good", "good.toml"), ("bad", "bad.toml")],
        max_concurrency=2,
        database=db,
    )
    manifest = load_manifest(manifest_path)
    store = Store(db)
    try:
        result = run_batch(manifest, store, GlobalLimiter(2))
    finally:
        store.close()
    # One healthy site completed, one was invalid -> partial failure.
    assert result.exit_code == EXIT_PARTIAL
    assert result.counts["completed"] == 1 and result.counts["invalid"] == 1
    store = Store(db)
    try:
        rows = {row["name"]: row for row in store.get_batch_sites(result.batch_id)}
        assert rows["bad"]["status"] == "invalid" and rows["bad"]["run_id"] is None
        assert rows["good"]["status"] == "completed"
    finally:
        store.close()


def test_batch_worker_exception_is_isolated(reset_mock, tmp_path, monkeypatch):
    """An exception inside the scan pipeline marks only that site failed."""
    import linkpatrol.batch as batch_mod

    db = str(tmp_path / "shared.db")
    _site_toml(tmp_path / "good.toml", reset_mock.base_url + "/", db)
    # Distinct origin (unused port) so the entry is not merged with "good";
    # the injected exception fires before any network result matters.
    _site_toml(tmp_path / "boom.toml", "http://127.0.0.1:3/about", db)
    manifest_path = _manifest(
        tmp_path / "batch.toml",
        [("good", "good.toml"), ("boom", "boom.toml")],
        max_concurrency=2,
        database=db,
        reports_dir=str(tmp_path / "breports"),
        sitemap_dir=str(tmp_path / "smaps"),
    )
    manifest = load_manifest(manifest_path)

    real_execute = batch_mod.execute_scan

    def patched(config, store, fetcher, run_id, listed_urls_provider=None):
        if config["seed"].startswith("http://127.0.0.1:3/"):
            raise RuntimeError("simulated pipeline explosion")
        return real_execute(config, store, fetcher, run_id,
                            listed_urls_provider=listed_urls_provider)

    monkeypatch.setattr(batch_mod, "execute_scan", patched)
    store = Store(db)
    try:
        result = run_batch(manifest, store, GlobalLimiter(2))
    finally:
        store.close()

    assert result.exit_code == EXIT_PARTIAL
    rows = {row["name"]: row for row in result.sites}
    assert rows["good"]["status"] == "completed"
    assert rows["boom"]["status"] == "failed"
    assert "simulated pipeline explosion" in rows["boom"]["error"]
    # Output overrides namespace the healthy site's report and sitemap.
    assert (tmp_path / "breports" / "good").is_dir()
    assert (tmp_path / "smaps" / "good.xml").is_file()


def test_batch_duplicate_host_is_skipped_not_crawled_twice(
        reset_mock, tmp_path):
    db = str(tmp_path / "shared.db")
    _site_toml(tmp_path / "a.toml", reset_mock.base_url + "/", db)
    _site_toml(tmp_path / "a2.toml", reset_mock.base_url + "/about", db)
    manifest_path = _manifest(
        tmp_path / "batch.toml",
        [("first", "a.toml"), ("second", "a2.toml")],
        max_concurrency=2,
        database=db,
    )
    manifest = load_manifest(manifest_path)
    store = Store(db)
    try:
        result = run_batch(manifest, store, GlobalLimiter(2))
    finally:
        store.close()
    # A skipped duplicate is not a failure: batch still succeeds.
    assert result.exit_code == EXIT_OK
    assert result.counts["completed"] == 1 and result.counts["skipped"] == 1
    store = Store(db)
    try:
        rows = {row["name"]: row for row in store.get_batch_sites(result.batch_id)}
        assert rows["first"]["status"] == "completed"
        assert rows["second"]["status"] == "skipped"
        assert rows["second"]["detail"]["merged_into"] == "first"
        assert len(store.list_runs()) == 1  # only one crawl for the shared host
    finally:
        store.close()


def test_batch_cli_entrypoint_and_report_baseline_compatibility(
        reset_mock, ext_site, tmp_path, capsys):
    manifest_path = _build_two_site_manifest(tmp_path, reset_mock, ext_site)
    db = str(tmp_path / "shared.db")
    code = main(["batch-scan", "--manifest", str(manifest_path),
                 "--max-concurrency", "3"])
    assert code == EXIT_OK
    capsys.readouterr()

    store = Store(db)
    try:
        runs = sorted(store.list_runs(), key=lambda run: run["id"])
        target_run = runs[0]
        run_id = target_run["id"]
    finally:
        store.close()

    # report and baseline read a batch-produced run exactly like a legacy run.
    assert main(["report", "--db", db, "--run", str(run_id),
                 "--out", str(tmp_path / "re_reports")]) == 0
    report_path = tmp_path / "re_reports" / f"run_{run_id}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run"]["id"] == run_id and report["summary"]["pages"] >= 1

    assert main(["baseline", "set", "--name", "batch_base", "--run", str(run_id),
                 "--db", db]) == 0
    capsys.readouterr()  # discard the "baseline ... -> run ..." line
    assert main(["diff", "--baseline", "batch_base", "--run", str(run_id),
                 "--db", db]) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["baseline_run"] == run_id


def test_per_site_delays_are_independent_under_shared_global_limit(
        reset_mock):
    """Two crawlers with different delay_ms share one limiter but keep
    independent throttle spacing.

    Delay independence is a property of each Crawler's own throttle lock, so
    two independent Crawler instances (the unit the batch scheduler runs one
    of per site) on the multi-page mock site demonstrate it directly while
    sharing the global request limiter.
    """
    from linkpatrol.batch import BoundedFetcher
    from linkpatrol.crawler import Crawler
    from linkpatrol.fetcher import Fetcher

    limiter = GlobalLimiter(2)
    timestamps: dict[str, list[float]] = {"slow": [], "fast": []}
    ts_lock = threading.Lock()

    def make_crawler(label: str, delay_ms: int):
        base = Fetcher(timeout_s=2, retries=0, backoff_s=0)
        bounded = BoundedFetcher(base, limiter)
        original_fetch = bounded.fetch

        def timed_fetch(url, method="GET", max_body_bytes=None):
            # robots.txt downloads bypass the Crawler throttle by design.
            if not url.endswith("/robots.txt"):
                with ts_lock:
                    timestamps[label].append(time.monotonic())
            return original_fetch(url, method=method,
                                  max_body_bytes=max_body_bytes)

        bounded.fetch = timed_fetch  # type: ignore[method-assign]
        config = default_config(reset_mock.base_url + "/")
        config["limits"].update(
            {"max_pages": 5, "delay_ms": delay_ms, "concurrency": 1,
             "timeout_s": 2.0, "retries": 0}
        )
        return Crawler(config, fetcher=bounded)

    slow = make_crawler("slow", delay_ms=100)
    fast = make_crawler("fast", delay_ms=10)
    results = {}

    def run_slow():
        results["slow"] = slow.run()

    thread = threading.Thread(target=run_slow)
    thread.start()
    results["fast"] = fast.run()
    thread.join()

    assert len(results["slow"].pages) >= 3 and len(results["fast"].pages) >= 3
    slow_gaps = [b - a for a, b in zip(timestamps["slow"], timestamps["slow"][1:])]
    fast_gaps = [b - a for a, b in zip(timestamps["fast"], timestamps["fast"][1:])]
    # The 100ms crawler keeps its own floor; the 10ms crawler is not dragged to
    # the slower cadence. Bounds use slack for thread scheduling, but stay far
    # enough apart to prove the locks are independent.
    assert slow_gaps and min(slow_gaps) >= 0.07
    assert fast_gaps and min(fast_gaps) < 0.07
    # Both funnelled through the same global limiter, which was never exceeded.
    assert limiter.peak_inflight <= 2


def test_robots_behavior_is_unchanged_in_batch_run(reset_mock, tmp_path):
    """A batch crawl still honours robots.txt (private path never fetched)."""
    db = str(tmp_path / "shared.db")
    _site_toml(tmp_path / "a.toml", reset_mock.base_url + "/", db,
               concurrency=2, max_pages=30)
    manifest_path = _manifest(
        tmp_path / "batch.toml", [("alpha", "a.toml")],
        max_concurrency=3, database=db,
    )
    reset_mock.app.config["COUNTERS"].clear()
    manifest = load_manifest(manifest_path)
    store = Store(db)
    try:
        result = run_batch(manifest, store, GlobalLimiter(3))
    finally:
        store.close()
    assert result.exit_code == EXIT_OK
    # The crawl phase never GETs the robots-disallowed page. (The checker's
    # later HEAD probe of a link to it is pre-existing single-site behaviour.)
    assert reset_mock.app.config["COUNTERS"].get("private", 0) in {0, 1}
    store = Store(db)
    try:
        run_row = store.list_runs()[0]
        run_id = run_row["id"]
        # Crawl-stage robots enforcement was recorded and no page stored.
        assert run_row["stats"]["crawl"]["skipped_by_robots"] >= 1
        pages = store.get_pages(run_id)
        assert not any("/private/secret" in page["url"] for page in pages)
    finally:
        store.close()
