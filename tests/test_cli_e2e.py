"""End-to-end CLI scenario: scan, report, baseline, and CI exit code."""

import json
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from linkpatrol.cli import main
from linkpatrol.demoapp import create_demo_app
from linkpatrol.store import Store

def write_config(tmp_path: Path, seed: str) -> Path:
    path = tmp_path / "scan.toml"
    disk = tmp_path.as_posix()
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
concurrency = 1
max_redirects = 5
[robots]
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
        assert run["status"] == "completed"
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

def test_archive_uses_retention_config(reset_mock, tmp_path, capsys):
    config_path = write_config(tmp_path, reset_mock.base_url)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n[retention]\nkeep_runs = 1\n",
        encoding="utf-8",
    )
    database = tmp_path / "out" / "linkpatrol.db"
    for _ in range(3):
        assert main(["scan", "--config", str(config_path)]) == 0
    capsys.readouterr()
    assert main(["archive", "--config", str(config_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["archived"] == [1, 2] and result["kept"] == [3]
    assert (tmp_path / "reports" / "run_1.json").is_file() is False
    assert (tmp_path / "reports" / "run_3.json").is_file() is True


def test_config_error_returns_three(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('seed = "ftp://127.0.0.1/resource"\n')
    assert main(["scan", "--config", str(path)]) == 3


def test_archive_and_timeline_cli_workflow(reset_mock, tmp_path, capsys):
    config_path = write_config(tmp_path, reset_mock.base_url)
    database = tmp_path / "out" / "linkpatrol.db"
    reports = tmp_path / "reports"
    target = reset_mock.base_url + "/regression"

    # Run 1: healthy. Run 2/3: the target becomes broken. Run 4: it is fixed.
    assert main(["scan", "--config", str(config_path)]) == 0
    reset_mock.app.config["REGRESSION_BROKEN"] = True
    assert main(["scan", "--config", str(config_path)]) == 0
    assert main(["scan", "--config", str(config_path)]) == 0
    reset_mock.app.config["REGRESSION_BROKEN"] = False
    assert main(["scan", "--config", str(config_path)]) == 0

    # Pin run 1 to a baseline; it must survive archiving untouched.
    assert main(["baseline", "set", "--name", "prod", "--run", "1",
                 "--db", str(database)]) == 0

    capsys.readouterr()
    assert main(["archive", "--db", str(database), "--reports", str(reports),
                 "--keep", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["protected"] == [1]
    assert result["archived"] == [2, 3]
    assert (reports / "run_1.json").is_file() and (reports / "run_1.html").is_file()
    assert not (reports / "run_2.json").exists()
    assert not (reports / "run_3.json").exists()
    assert (reports / "run_4.json").is_file()

    store = Store(database)
    try:
        for run_id in (2, 3):
            assert store.get_run(run_id) is None
            assert store.run_detail_counts(run_id) == {
                "pages": 0, "links": 0, "checks": 0}
        assert store.get_run(1) is not None
        assert store.run_detail_counts(1)["checks"] > 0
        assert store.get_baseline("prod")["run_id"] == 1
    finally:
        store.close()

    capsys.readouterr()
    assert main(["timeline", "--db", str(database), "--url", target]) == 0
    history = json.loads(capsys.readouterr().out)
    assert history["first_broken_run"] == 2
    assert history["fix_runs"] == [4]
    assert history["spans"][0]["run_ids"] == [2, 3]
    assert history["spans"][0]["fixed_run"] == 4

    capsys.readouterr()
    assert main(["timeline", "--db", str(database), "--runs"]) == 0
    runs = json.loads(capsys.readouterr().out)["runs"]
    assert [row["run_id"] for row in runs] == [1, 2, 3, 4]
    assert [row["archived"] for row in runs] == [False, True, True, False]


@pytest.fixture
def demo_site():
    """The built-in demo site on a random local port, with reset helpers."""
    app = create_demo_app()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_demo_site_archive_timeline_acceptance(demo_site, tmp_path, capsys):
    """Acceptance: broken-then-fixed history survives archival intact."""
    config_path = write_config(tmp_path, demo_site)
    database = tmp_path / "out" / "linkpatrol.db"
    reports = tmp_path / "reports"
    target = demo_site + "/regression"

    # Runs 1-2: the probe is broken. Run 3: it is fixed.
    for _ in range(3):
        assert main(["scan", "--config", str(config_path)]) == 0
    assert main(["baseline", "set", "--name", "pinned", "--run", "2",
                 "--db", str(database)]) == 0

    capsys.readouterr()
    assert main(["archive", "--db", str(database), "--reports", str(reports),
                 "--keep", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["archived"] == [1]
    assert result["protected"] == [2]
    assert (reports / "run_2.json").is_file()
    assert not (reports / "run_1.json").exists()
    assert (reports / "run_3.json").is_file()

    store = Store(database)
    try:
        assert store.get_run(1) is None
        assert store.run_detail_counts(1) == {"pages": 0, "links": 0, "checks": 0}
        assert store.get_run(2) is not None
        assert store.run_detail_counts(2)["checks"] > 0
        assert store.get_baseline("pinned")["run_id"] == 2
    finally:
        store.close()

    capsys.readouterr()
    assert main(["timeline", "--db", str(database), "--url", target]) == 0
    history = json.loads(capsys.readouterr().out)
    assert history["first_broken_run"] == 1
    assert history["fix_runs"] == [3]
    assert history["spans"][0]["run_ids"] == [1, 2]
    assert history["spans"][0]["fixed_run"] == 3
    assert history["currently_broken"] is False
    # The archived run 1 and the still-live run 2 answer the same timeline.
    assert [entry["archived"] for entry in history["entries"]] == [True, False, False]


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
