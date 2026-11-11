"""End-to-end CLI scenario: scan, report, baseline, and CI exit code."""

import json
from pathlib import Path

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
