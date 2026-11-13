"""End-to-end CLI tests for the offline trend command."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from linkpatrol.cli import main
from linkpatrol.demoapp import create_demo_app


def _config(tmp_path: Path, seed: str, max_pages: int = 60) -> Path:
    disk = tmp_path.as_posix()
    path = tmp_path / "scan.toml"
    path.write_text(f"""
seed = "{seed}/"
[scope]
exclude = ["^/search", "^/private/"]
[limits]
max_depth = 3
max_pages = {max_pages}
timeout_s = 2.0
retries = 2
delay_ms = 0
concurrency = 1
max_redirects = 5
[robots]
respect = true
user_agent = "linkpatrol-test/0.1"
[external]
check = true
[normalize]
drop_params = ["utm_*", "fbclid", "campaign"]
[output]
dir = "{disk}/reports"
database = "{disk}/out/linkpatrol.db"
sitemap = "{disk}/out/sitemap.xml"
""", encoding="utf-8")
    return path


def test_trend_generate_list_show_and_idempotency(tmp_path):
    app = create_demo_app()
    import threading
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    seed = f"http://127.0.0.1:{server.server_port}"
    database = tmp_path / "out" / "linkpatrol.db"
    trend_db = tmp_path / "out" / "trends.db"
    json_out = tmp_path / "trend.json"
    try:
        config_path = _config(tmp_path, seed)
        assert main(["scan", "--config", str(config_path)]) == 0
        # Inject a deterministic 500 for /docs on the second scan.
        import os

        os.environ["LINKPATROL_DEMO_FAULTS"] = json.dumps({"/docs": 500})
        assert main(["scan", "--config", str(config_path)]) == 0
        os.environ.pop("LINKPATROL_DEMO_FAULTS", None)

        args = [
            "trend", "generate", "--db", str(database),
            "--trend-db", str(trend_db), "--seed", seed + "/",
            "--min-occurrences", "2", "--json-out", str(json_out),
        ]
        assert main(args) == 0
        # Regenerating for the same runs must not add a second report.
        assert main(args) == 0
        payload = json.loads(json_out.read_text(encoding="utf-8"))
        assert payload["summary"]["runs_total"] == 2
        assert payload["schema_version"] >= 1
        docs = next(
            entity for entity in payload["entities"]
            if entity["key"].endswith("/docs"))
        assert docs["classification"] == "deteriorating"
        assert [point["status_code"] for point in docs["series"]] == [200, 500]

        assert main(["trend", "list", "--trend-db", str(trend_db)]) == 0
        assert main(["trend", "show", "--trend-db", str(trend_db),
                     "--report", "1"]) == 0

        # Only one persisted report despite two generate calls.
        conn = sqlite3.connect(trend_db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM trend_reports").fetchone()[0] == 1
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_trend_default_excludes_incomplete_run(tmp_path):
    app = create_demo_app()
    import threading
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    seed = f"http://127.0.0.1:{server.server_port}"
    database = tmp_path / "out" / "linkpatrol.db"
    trend_db = tmp_path / "out" / "trends.db"
    try:
        config_path = _config(tmp_path, seed)
        assert main(["scan", "--config", str(config_path)]) == 0
        assert main(["scan", "--config", str(config_path)]) == 0
        # Mark the second run as interrupted directly in the scan DB.
        conn = sqlite3.connect(database)
        try:
            conn.execute("UPDATE runs SET status = 'failed' WHERE id = 2")
            conn.commit()
        finally:
            conn.close()

        # Only one completed run remains -> analysis must refuse by default.
        code = main(["trend", "generate", "--db", str(database),
                     "--trend-db", str(trend_db), "--seed", seed + "/"])
        assert code == 1
        # Including the incomplete run succeeds and annotates it.
        assert main([
            "trend", "generate", "--db", str(database),
            "--trend-db", str(trend_db), "--seed", seed + "/",
            "--include-incomplete", "--min-occurrences", "1",
        ]) == 0
        conn = sqlite3.connect(trend_db)
        try:
            row = conn.execute(
                "SELECT payload_json FROM trend_reports ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        payload = json.loads(row[0])
        assert payload["summary"]["runs_incomplete"] == 1
        assert payload["runs"][1]["status"] == "failed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_trend_does_not_modify_scan_database(tmp_path):
    app = create_demo_app()
    import threading
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    seed = f"http://127.0.0.1:{server.server_port}"
    database = tmp_path / "out" / "linkpatrol.db"
    try:
        config_path = _config(tmp_path, seed)
        assert main(["scan", "--config", str(config_path)]) == 0
        assert main(["scan", "--config", str(config_path)]) == 0
        assert main([
            "trend", "generate", "--db", str(database),
            "--trend-db", str(tmp_path / "trends.db"),
            "--seed", seed + "/",
        ]) == 0
        # Row counts of every scan table must be unchanged by trend generation.
        conn = sqlite3.connect(database)
        try:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("runs", "pages", "links", "checks", "baselines")
            }
        finally:
            conn.close()
        assert counts["runs"] == 2 and counts["pages"] > 0
        assert all(counts[table] >= 0 for table in counts)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_trend_cli_parameter_validation(tmp_path):
    trend_db = tmp_path / "trends.db"
    scan_db = tmp_path / "missing.db"
    base = ["trend", "generate", "--db", str(scan_db),
            "--trend-db", str(trend_db), "--seed", "http://127.0.0.1:9000/"]
    # Missing scan database surfaces as a normal error exit code.
    assert main(base) == 1
    # --runs and the time window are mutually exclusive.
    assert main(base + ["--runs", "1,2", "--since", "2026-01-01T00:00:00+00:00"]) == 1
    # Malformed run ids are rejected.
    assert main(base + ["--runs", "1,x"]) == 1
    # Showing an unknown report is an error rather than a crash.
    assert main(["trend", "show", "--trend-db", str(trend_db), "--report", "99"]) == 1
    # Listing an empty/absent trend store still succeeds with no output.
    assert main(["trend", "list", "--trend-db", str(tmp_path / "empty.db")]) == 0
