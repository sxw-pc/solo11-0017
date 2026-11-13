"""Dense-site end-to-end tests: dedup, checker reuse, baseline, legacy DB."""

import json
import sqlite3
from pathlib import Path

from linkpatrol.checker import check_links
from linkpatrol.cli import main
from linkpatrol.crawler import crawl_site
from linkpatrol.fetcher import Fetcher
from linkpatrol.store import Store
from tests.densesite import HUBS, SHARED
from tests.test_linkgraph import make_legacy_db

# home→hubs + hubs→(shared, missing, ext, img) + shared→home
EXPECTED_PAGES = 1 + HUBS + SHARED + 1
EXPECTED_EDGES = HUBS + SHARED + 1 + 1 + 1 + 1
EXPECTED_REFS = HUBS + HUBS * (SHARED + 3) + SHARED
BROKEN_TARGET = "/missing-shared"

def tracked_fetcher():
    fetcher = Fetcher(timeout_s=2, retries=1, backoff_s=0)
    calls = []
    original = fetcher.fetch

    def tracked(url, method="GET", max_body_bytes=None):
        calls.append((url, method))
        return original(url, method=method, max_body_bytes=max_body_bytes)

    fetcher.fetch = tracked
    return fetcher, calls

def test_dedup_shrinks_link_rows_and_checker_requests(reset_dense, ext_site,
                                                      make_config, tmp_path):
    config = make_config(reset_dense.base_url, limits={"concurrency": 2})
    crawl = crawl_site(config, Fetcher(timeout_s=2, retries=1, backoff_s=0))
    assert len(crawl.pages) == EXPECTED_PAGES
    assert len(crawl.link_graph) == EXPECTED_EDGES
    assert crawl.link_graph.reference_count == EXPECTED_REFS

    checker_fetcher, calls = tracked_fetcher()
    report = check_links(config, crawl, checker_fetcher)
    # Every checkable target is unique; only non-crawled targets hit the wire.
    assert len(report.checks) == EXPECTED_EDGES
    assert sorted(calls) == sorted([
        (reset_dense.base_url + "/assets/pixel.png", "HEAD"),
        (ext_site.base_url + "/ok", "HEAD"),
    ])
    assert len(calls) == len(set(calls))  # no target is probed twice

    store = Store(tmp_path / "dense.db")
    try:
        run_id = store.create_run(config["seed"], config)
        store.save_link_graph(run_id, crawl.link_graph)
        rows = store.get_links(run_id)
        assert len(rows) == EXPECTED_EDGES
        assert sum(row["sources"] for row in rows) == EXPECTED_REFS
        counts = store.inlink_counts(run_id)
        assert counts[reset_dense.base_url + "/shared/0"] == HUBS
        assert counts[reset_dense.base_url + "/"] == SHARED
    finally:
        store.close()

def test_concurrent_crawl_keeps_exact_source_counts(reset_dense, make_config):
    config = make_config(reset_dense.base_url, limits={"concurrency": 4})
    crawl = crawl_site(config, Fetcher(timeout_s=2, retries=1, backoff_s=0))
    assert len(crawl.link_graph) == EXPECTED_EDGES
    assert crawl.link_graph.reference_count == EXPECTED_REFS
    for n in range(SHARED):
        sources = crawl.link_graph.sources_for(
            reset_dense.base_url + f"/shared/{n}")
        assert sorted(sources) == sorted(
            reset_dense.base_url + f"/hub/{i}" for i in range(HUBS)
        )

def write_dense_config(tmp_path: Path, seed: str) -> Path:
    path = tmp_path / "dense.toml"
    disk = tmp_path.as_posix()
    path.write_text(f"""
seed = "{seed}/"
[scope]
exclude = []
[limits]
max_depth = 3
max_pages = 100
timeout_s = 2.0
retries = 1
delay_ms = 0
concurrency = 2
max_redirects = 5
[robots]
respect = true
user_agent = "linkpatrol-dense-test/0.1"
[external]
check = true
[normalize]
drop_params = []
[output]
dir = "{disk}/reports"
database = "{disk}/out/linkpatrol.db"
sitemap = "{disk}/out/sitemap.xml"
""", encoding="utf-8")
    return path

def test_cli_scan_baseline_and_regression(reset_dense, tmp_path, capsys):
    config_path = write_dense_config(tmp_path, reset_dense.base_url)
    database = tmp_path / "out" / "linkpatrol.db"
    assert main(["scan", "--config", str(config_path)]) == 0

    store = Store(database)
    try:
        rows = store.get_links(1)
        assert len(rows) == EXPECTED_EDGES
        assert sum(row["sources"] for row in rows) == EXPECTED_REFS
        assert len(store.get_checks(1)) == EXPECTED_EDGES
        # The always-broken target keeps all of its source pages.
        broken = store.broken_identities(1)
        assert len(broken) == HUBS
        assert {row["target_url"] for row in broken} == {
            reset_dense.base_url + BROKEN_TARGET}
    finally:
        store.close()
    assert main(["baseline", "set", "--name", "prod", "--run", "1",
                 "--db", str(database)]) == 0

    # A heavily linked shared page regresses to 404.
    reset_dense.app.config["BROKEN_SHARED"].add("/shared/3")
    assert main(["scan", "--config", str(config_path)]) == 0
    capsys.readouterr()
    code = main(["diff", "--baseline", "prod", "--run", "2",
                 "--db", str(database), "--fail-on", "new-broken"])
    assert code == 2
    diff = json.loads(capsys.readouterr().out)
    new_targets = {item["target_url"] for item in diff["new_broken"]}
    assert new_targets == {reset_dense.base_url + "/shared/3"}
    # Deduplication must not swallow sources: every hub shows up.
    assert sorted(item["source_url"] for item in diff["new_broken"]) == sorted(
        reset_dense.base_url + f"/hub/{i}" for i in range(HUBS))
    assert diff["summary"]["persistent"] == HUBS

    assert main(["report", "--db", str(database), "--run", "2",
                 "--out", str(tmp_path / "reports2")]) == 0
    report = json.loads((tmp_path / "reports2" / "run_2.json").read_text())
    assert report["summary"]["links"] == EXPECTED_EDGES
    # /shared/3 now 404s, so its own outgoing home link is gone (-1 ref).
    assert report["summary"]["link_refs"] == EXPECTED_REFS - 1
    assert report["summary"]["broken"] == 2 * HUBS
    inlinks = {item["target_url"]: item["sources"] for item in report["inlinks"]}
    assert inlinks[reset_dense.base_url + "/shared/3"] == HUBS
    assert inlinks[reset_dense.base_url + BROKEN_TARGET] == HUBS

def test_report_command_reads_legacy_database(tmp_path):
    database = tmp_path / "legacy.db"
    make_legacy_db(database)
    out_dir = tmp_path / "legacy_reports"
    assert main(["report", "--db", str(database), "--run", "1",
                 "--out", str(out_dir)]) == 0
    payload = json.loads((out_dir / "run_1.json").read_text(encoding="utf-8"))
    assert payload["summary"]["links"] == 3
    assert payload["summary"]["broken"] == 3
    assert payload["inlinks"] == [{"target_url": "http://site/gone", "sources": 3}]
    assert [item["source_url"] for item in payload["broken"]] == [
        "http://site/p0", "http://site/p1", "http://site/p2"]
    # The historical database file itself is only extended, never rebuilt.
    conn = sqlite3.connect(database)
    try:
        count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        sources = conn.execute("SELECT DISTINCT sources FROM links").fetchall()
    finally:
        conn.close()
    assert count == 3 and [tuple(row) for row in sources] == [(1,)]
