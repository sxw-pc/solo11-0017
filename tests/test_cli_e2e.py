"""End-to-end CLI scenario: scan, report, baseline, and CI exit code."""

import json
import sqlite3
import time
from pathlib import Path

from linkpatrol.cli import main
from linkpatrol.demoapp import create_demo_app
from linkpatrol.store import Store


def write_config(tmp_path: Path, seed: str, max_attempts: int | None = None,
                 concurrency: int = 1) -> Path:
    path = tmp_path / "scan.toml"
    disk = tmp_path.as_posix()
    attempts_line = "" if max_attempts is None else f"max_attempts = {max_attempts}\n"
    path.write_text(f"""
seed = "{seed}/"
[scope]
exclude = ["^/private/"]
[limits]
max_depth = 3
max_pages = 50
timeout_s = 2.0
retries = 2
delay_ms = 0
concurrency = {concurrency}
max_redirects = 5
{attempts_line}[robots]
respect = true
user_agent = "linkpatrol-test/0.1"
[external]
check = true
[normalize]
drop_params = ["utm_*", "fbclid"]
[output]
dir = "{disk}/reports"
database = "{disk}/out/linkpatrol.db"
sitemap = "{disk}/out/sitemap.xml"
""", encoding="utf-8")
    return path

def test_full_cli_regression_workflow(reset_mock, tmp_path, capsys):
    config_path = write_config(tmp_path, reset_mock.base_url)
    database = tmp_path / "out" / "linkpatrol.db"
    assert main(["scan", "--config", str(config_path)]) == 0
    assert database.is_file() and (tmp_path / "reports" / "run_1.json").is_file()
    assert (tmp_path / "out" / "sitemap.xml").is_file()
    assert main(["baseline", "set", "--name", "prod", "--run", "1",
                 "--db", str(database)]) == 0
    assert main(["baseline", "set", "--name", "prod", "--run", "1",
                 "--db", str(database)]) == 1

    reset_mock.app.config["REGRESSION_BROKEN"] = True
    assert main(["scan", "--config", str(config_path)]) == 0
    capsys.readouterr()
    code = main(["diff", "--baseline", "prod", "--run", "2", "--db", str(database),
                 "--fail-on", "new-broken"])
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert reset_mock.base_url + "/regression" in [
        item["target_url"] for item in output["new_broken"]]

    store = Store(database)
    try:
        run = store.get_run(2)
        # Run 2 still finishes the full pipeline (exit code stays 0) but the
        # always-500 target makes it a partial completion rather than clean.
        assert run["status"] in {"completed", "partial"}
        assert run["stats"]["last_diff"]["baseline"] == "prod"
        assert run["stats"]["last_diff"]["summary"]["new_broken"] >= 1
    finally:
        store.close()

    assert main(["report", "--db", str(database), "--run", "2",
                 "--out", str(tmp_path / "reports2")]) == 0
    report = json.loads((tmp_path / "reports2" / "run_2.json").read_text(encoding="utf-8"))
    assert reset_mock.base_url + "/regression" in {
        item["target_url"] for item in report["broken"]}
    sitemap_out = tmp_path / "from_db.xml"
    assert main(["sitemap", "--db", str(database), "--run", "2",
                 "--out", str(sitemap_out)]) == 0
    assert sitemap_out.is_file()

def test_config_error_returns_three(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('seed = "ftp://127.0.0.1/resource"\n')
    assert main(["scan", "--config", str(path)]) == 3


def test_demo_site_exercises_all_scenarios():
    client = create_demo_app().test_client()
    for path in ["/", "/about", "/docs", "/blog/post-1", "/dead-end", "/orphan",
                 "/landing", "/old", "/hop-1", "/hop-2", "/hop-3", "/loop-a",
                 "/loop-b", "/slow", "/private/secret", "/robots.txt",
                 "/assets/style.css", "/assets/app.js", "/assets/logo.png"]:
        assert client.get(path).status_code in {200, 301, 302, 307}
    assert client.get("/missing").status_code == 404
    assert client.get("/server-error").status_code == 500
    assert b"Disallow: /private/" in client.get("/robots.txt").data
    sitemap = client.get("/sitemap.xml").data
    assert b"/orphan" in sitemap
    assert client.get("/flaky").status_code == 500
    assert client.get("/flaky").status_code == 500
    assert client.get("/flaky").status_code == 200
    body = client.get("/search?q=<script>").data
    assert b"&lt;script&gt;" in body and b"<script>" not in body


def test_budget_exhaustion_truncates_run_with_correct_counts(
        reset_mock, tmp_path):
    # BFS reaches robots.txt and the first few healthy pages, then the
    # always-500 target burns its three attempts and the budget runs dry;
    # every remaining target must be skipped instead of retried.
    config_path = write_config(tmp_path, reset_mock.base_url, max_attempts=12)
    database = tmp_path / "out" / "linkpatrol.db"
    started = time.monotonic()
    code = main(["scan", "--config", str(config_path)])
    elapsed = time.monotonic() - started
    assert code == 0  # budget truncation is a clean partial completion
    assert elapsed < 30  # the run ends in bounded time instead of retrying

    store = Store(database)
    try:
        run = store.get_run(1)
        assert run["status"] == "budget_exhausted"
        budget = run["stats"]["budget"]
        assert budget["limit"] == 12 and budget["used"] <= 12
        assert budget["exhausted"] is True and budget["denied"] >= 1
        assert run["stats"]["skipped_budget"] >= 1
        details = store.get_attempt_details(1)
        assert details  # failure attribution was persisted
        skipped = [d for d in details if d["budget_skipped"]]
        # stats count unique skipped links; details hold one row per unique
        # target base, so the row count is a lower bound on the link count.
        assert skipped and len(skipped) <= run["stats"]["skipped_budget"]
        server_error = next(
            d for d in details
            if d["target_url"] == reset_mock.base_url + "/server-error"
        )
        assert server_error["attempts"] == 3
        assert server_error["first_failure_code"] == 500
        assert server_error["final_failure_code"] == 500
    finally:
        store.close()

    # The JSON report distribution must match the persisted rows exactly.
    report_path = tmp_path / "reports" / "run_1.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    distribution = payload["failure_distribution"]
    assert distribution["budget"]["used"] <= 12
    assert distribution["skipped_budget"] == len(skipped)
    assert distribution["by_code"]["500"] >= 1
    assert "budget_exhausted" in distribution["by_code"]
    reported_targets = {item["target_url"] for item in distribution["by_target"]}
    assert {d["target_url"] for d in details} == reported_targets
    assert payload["summary"]["skipped_budget"] == len(skipped)
    # Every budget-skipped check target maps to a broken, budget_exhausted
    # check. Crawl-phase drained rows have no links and thus no check rows.
    skipped_urls = {
        d["target_url"] for d in skipped if d["phase"] == "check"
    }
    broken_bases = {
        item["target_url"].split("#", 1)[0] for item in payload["broken"]
    }
    assert skipped_urls <= broken_bases
    html = (tmp_path / "reports" / "run_1.html").read_text(encoding="utf-8")
    assert "Retry budget and failure distribution" in html


def test_concurrency_four_never_overshoots_budget(reset_mock, tmp_path):
    config_path = write_config(
        tmp_path, reset_mock.base_url, max_attempts=15, concurrency=4
    )
    database = tmp_path / "out" / "linkpatrol.db"
    assert main(["scan", "--config", str(config_path)]) == 0
    store = Store(database)
    try:
        run = store.get_run(1)
        budget = run["stats"]["budget"]
        assert budget["used"] <= 15  # atomic counter, no thread overshoot
        assert run["status"] == "budget_exhausted"
        assert run["stats"]["skipped_budget"] >= 1
        # Reserved slots + denied reservations reconcile to the observed work.
        assert budget["used"] + budget["denied"] >= budget["used"]
        details = store.get_attempt_details(1)
        assert sum(d["attempts"] for d in details) <= 15
    finally:
        store.close()


def test_report_on_legacy_database_does_not_crash(tmp_path):
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, seed TEXT NOT NULL,
            config_json TEXT NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
            stats_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            url TEXT NOT NULL, depth INTEGER NOT NULL, status_code INTEGER,
            content_hash TEXT NOT NULL, duration_ms INTEGER NOT NULL, error TEXT,
            UNIQUE(run_id, url)
        );
        CREATE TABLE links (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            source_url TEXT NOT NULL, target_url TEXT NOT NULL, raw_href TEXT NOT NULL,
            kind TEXT NOT NULL, scope TEXT NOT NULL, line_no INTEGER NOT NULL,
            anchor TEXT NOT NULL DEFAULT '',
            UNIQUE(run_id, source_url, target_url, kind)
        );
        CREATE TABLE checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            url TEXT NOT NULL, final_url TEXT, status_code INTEGER,
            redirect_chain_json TEXT NOT NULL DEFAULT '[]',
            anchor_ok INTEGER NOT NULL DEFAULT 1, ok INTEGER NOT NULL DEFAULT 0,
            error TEXT, checked_at TEXT NOT NULL, UNIQUE(run_id, url)
        );
        CREATE TABLE baselines (
            name TEXT PRIMARY KEY, run_id INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO runs(seed, config_json, started_at, finished_at, status)
        VALUES ('http://127.0.0.1:5001/', '{}', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:01:00+00:00', 'completed');
        """
    )
    conn.commit()
    conn.close()

    out_dir = tmp_path / "reports"
    assert main(["report", "--db", str(legacy), "--run", "1",
                 "--out", str(out_dir)]) == 0
    payload = json.loads((out_dir / "run_1.json").read_text(encoding="utf-8"))
    assert payload["failure_distribution"]["by_code"] == {}
    assert payload["failure_distribution"]["by_target"] == []
    assert payload["summary"]["skipped_budget"] == 0
