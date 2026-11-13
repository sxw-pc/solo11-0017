"""CLI-level acceptance tests for versioned ignore rules (scan + diff)."""

import json

from linkpatrol.cli import main
from linkpatrol.store import Store


def _ignore_file(path, regexes):
    # TOML literal strings keep backslashes verbatim (no \r-style escapes).
    body = "".join(
        f"[[ignore]]\nurl_regex = '{regex}'\n" for regex in regexes
    )
    path.write_text(body, encoding="utf-8")
    return path


def _config(tmp_path: str, seed: str) -> str:
    disk = tmp_path.as_posix() if hasattr(tmp_path, "as_posix") else str(tmp_path)
    path = f"{disk}/scan.toml"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"""
seed = "{seed}/"
[scope]
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
drop_params = []
[output]
dir = "{disk}/reports"
database = "{disk}/out/linkpatrol.db"
sitemap = "{disk}/out/sitemap.xml"
""")
    return path


def test_scan_binds_ignore_version_and_reorder_reuses_it(reset_mock, tmp_path):
    config_path = _config(tmp_path, reset_mock.base_url)
    db = tmp_path / "out" / "linkpatrol.db"
    ignore_a = _ignore_file(tmp_path / "a.toml", ["alpha", "beta"])
    ignore_b = _ignore_file(tmp_path / "b.toml", ["beta", "alpha"])
    ignore_c = _ignore_file(tmp_path / "c.toml", ["alpha", "beta", "gamma"])

    assert main(["scan", "--config", config_path, "--ignore", str(ignore_a)]) == 0
    assert main(["scan", "--config", config_path, "--ignore", str(ignore_b)]) == 0
    assert main(["scan", "--config", config_path, "--ignore", str(ignore_c)]) == 0

    store = Store(db)
    try:
        runs = {row["id"]: row["ignore_version_id"] for row in store.list_runs()}
        versions = store.conn.execute(
            "SELECT id, rule_count FROM ignore_versions ORDER BY id"
        ).fetchall()
        version_rows = [dict(row) for row in versions]
    finally:
        store.close()

    # Reordered but semantically identical lists share one version; the third
    # scan changed semantics and created exactly one additional version.
    real_versions = [v for v in version_rows if v["id"] != 0]
    assert len(real_versions) == 2
    assert runs[1] == runs[2]
    assert runs[3] != runs[1]


def test_changed_ignore_file_changes_diff_without_breaking_exit_codes(
    reset_mock, tmp_path, capsys
):
    config_path = _config(tmp_path, reset_mock.base_url)
    db = tmp_path / "out" / "linkpatrol.db"
    empty_ignore = _ignore_file(tmp_path / "empty.toml", [])

    # Baseline scan: no ignores.
    assert main(["scan", "--config", config_path,
                 "--ignore", str(empty_ignore)]) == 0
    assert main(["baseline", "set", "--name", "prod", "--run", "1",
                 "--db", str(db)]) == 0

    reset_mock.app.config["REGRESSION_BROKEN"] = True
    # Second scan still without an ignore for the regression URL.
    assert main(["scan", "--config", config_path,
                 "--ignore", str(empty_ignore)]) == 0
    capsys.readouterr()
    code = main(["diff", "--baseline", "prod", "--run", "2", "--db", str(db),
                 "--fail-on", "new-broken"])
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    regression = reset_mock.base_url + "/regression"
    assert regression in [item["target_url"] for item in output["new_broken"]]

    # New ignore file covers the regression; a third scan binds that version.
    new_ignore = _ignore_file(tmp_path / "new.toml", ["/regression"])
    assert main(["scan", "--config", config_path,
                 "--ignore", str(new_ignore)]) == 0
    capsys.readouterr()
    code = main(["diff", "--baseline", "prod", "--run", "3", "--db", str(db),
                 "--fail-on", "new-broken"])
    assert code == 0  # acceptance: newly ignored link is not new_broken anymore
    output = json.loads(capsys.readouterr().out)
    assert regression not in [item["target_url"] for item in output["new_broken"]]
    assert output["baseline_ignore_version"]["id"] != \
        output["current_ignore_version"]["id"]
    assert output["notes"]  # mixed versions produce an explanation, not an error
