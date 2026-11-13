"""Versioned ignore rules: fingerprints, run bindings, per-version diffs, migration."""

import json
import sqlite3

import pytest

from linkpatrol.baseline import (
    BaselineError, BrokenLink, IgnoreRule, IgnoreRules, compute_diff, diff_runs,
    register_ignore_version,
)
from linkpatrol.schema import IMPLICIT_FINGERPRINT, IMPLICIT_VERSION_ID
from linkpatrol.store import Store

SOURCE = "http://127.0.0.1:5001/"


def link(target, status=404, source=SOURCE):
    return BrokenLink(source_url=source, target_url=target, status_code=status)


def write_ignore(path, body):
    path.write_text(body, encoding="utf-8")
    return path


def add_broken(store, run_id, target, status=404):
    store.insert_link(run_id, SOURCE, {
        "target_url": target, "raw_href": target, "kind": "page",
        "scope": "internal", "line_no": 1, "anchor": ""})
    store.insert_check(run_id, {
        "url": target, "final_url": target, "status_code": status,
        "redirect_chain": [], "anchor_ok": True, "ok": False, "error": None,
        "checked_at": "2026-01-01T00:00:00+00:00"})


# --- fingerprints ---------------------------------------------------------

def test_fingerprint_order_insensitive_and_dedup(tmp_path):
    first = write_ignore(tmp_path / "a.toml", """
[[ignore]]
url_regex = "/legacy/"
status = [404, 410]
[[ignore]]
url_regex = "^http://example/private/"
""")
    # Same rules, reversed order, statuses in a different order, one duplicated entry.
    second = write_ignore(tmp_path / "b.toml", """
[[ignore]]
url_regex = "^http://example/private/"
[[ignore]]
url_regex = "/legacy/"
status = [410, 404]
[[ignore]]
url_regex = "/legacy/"
status = [410, 404]
""")
    rules_a = IgnoreRules.load(first)
    rules_b = IgnoreRules.load(second)
    assert rules_a.fingerprint() == rules_b.fingerprint()
    assert rules_a.canonical_entries() == rules_b.canonical_entries()


def test_different_semantics_get_different_fingerprints(tmp_path):
    first = write_ignore(tmp_path / "a.toml",
                         '[[ignore]]\nurl_regex = "/legacy/"\n')
    second = write_ignore(tmp_path / "b.toml",
                          '[[ignore]]\nurl_regex = "/legacy/"\nstatus = [410]\n')
    assert IgnoreRules.load(first).fingerprint() != IgnoreRules.load(second).fingerprint()


def test_empty_rules_have_stable_fingerprint():
    assert IgnoreRules().fingerprint() == IgnoreRules([]).fingerprint()


# --- version persistence ---------------------------------------------------

def test_register_version_reuses_row_and_round_trips(store):
    rules = IgnoreRules([
        IgnoreRule("/legacy/", (404, 410)),
        IgnoreRule("^http://example/private/"),
    ])
    version_id = register_ignore_version(store, rules)
    # Re-registering the same semantics (even reordered) creates no new version.
    reordered = IgnoreRules(list(reversed(rules.rules)))
    assert register_ignore_version(store, reordered) == version_id
    version = store.get_ignore_version(version_id)
    assert version["rule_count"] == 2
    restored = IgnoreRules.from_canonical(version["rules"])
    assert restored.canonical_entries() == rules.canonical_entries()
    assert restored.ignored(link("http://example/legacy/x", status=404))
    assert not restored.ignored(link("http://example/legacy/x", status=500))


def test_new_semantics_create_new_version(store):
    v1 = register_ignore_version(store, IgnoreRules([IgnoreRule("a")]))
    v2 = register_ignore_version(store, IgnoreRules([IgnoreRule("b")]))
    assert v2 != v1
    assert store.get_ignore_version(v1)["fingerprint"] != \
        store.get_ignore_version(v2)["fingerprint"]


def test_run_binds_ignore_version(store):
    version_id = register_ignore_version(store, IgnoreRules([IgnoreRule("a")]))
    run_id = store.create_run(SOURCE, {}, ignore_version_id=version_id)
    assert store.get_run_ignore_version_id(run_id) == version_id
    assert store.get_run(run_id)["ignore_version_id"] == version_id


# --- per-version diffs -----------------------------------------------------

def _two_runs_with_versions(store):
    ignore_v1 = register_ignore_version(
        store, IgnoreRules([IgnoreRule("/ignored")]))
    ignore_v2 = register_ignore_version(
        store, IgnoreRules([IgnoreRule("/ignored"), IgnoreRule("/also-ignored")]))
    first = store.create_run(SOURCE, {}, ignore_version_id=ignore_v1)
    second = store.create_run(SOURCE, {}, ignore_version_id=ignore_v2)
    add_broken(store, first, "http://127.0.0.1:5001/old")
    add_broken(store, second, "http://127.0.0.1:5001/new")
    add_broken(store, second, "http://127.0.0.1:5001/also-ignored")
    store.set_baseline("prod", first)
    return first, second, ignore_v1, ignore_v2


def test_diff_uses_each_runs_own_rule_version(store):
    first, second, v1, v2 = _two_runs_with_versions(store)
    result = diff_runs(store, "prod", second)
    targets = {item["target_url"] for item in result["new_broken"]}
    # /also-ignored is suppressed by the current run's v2 rules.
    assert targets == {"http://127.0.0.1:5001/new"}
    assert result["baseline_ignore_version"]["id"] == v1
    assert result["current_ignore_version"]["id"] == v2
    assert any("differ" in note for note in result["notes"])
    assert result["summary"]["ignored_new"] == 1


def test_diff_same_version_has_no_mismatch_note(store):
    version_id = register_ignore_version(store, IgnoreRules())
    first = store.create_run(SOURCE, {}, ignore_version_id=version_id)
    second = store.create_run(SOURCE, {}, ignore_version_id=version_id)
    add_broken(store, first, "http://127.0.0.1:5001/old")
    add_broken(store, second, "http://127.0.0.1:5001/new")
    store.set_baseline("prod", first)
    result = diff_runs(store, "prod", second)
    assert result["notes"] == []
    assert [item["target_url"] for item in result["new_broken"]] == [
        "http://127.0.0.1:5001/new"]


def test_changing_ignore_file_keeps_old_broken_reportable(store):
    """Acceptance: a newly ignored link leaves new_broken; links broken under the
    old version that were reportable then still surface on that side."""
    broad = register_ignore_version(store, IgnoreRules([IgnoreRule("/new")]))
    first = store.create_run(SOURCE, {}, ignore_version_id=IMPLICIT_VERSION_ID)
    second = store.create_run(SOURCE, {}, ignore_version_id=broad)
    add_broken(store, first, "http://127.0.0.1:5001/old")
    add_broken(store, second, "http://127.0.0.1:5001/new")
    store.set_baseline("prod", first)
    result = diff_runs(store, "prod", second)
    assert result["new_broken"] == []
    # The baseline-side link is unaffected by the newer rules.
    assert [item["target_url"] for item in result["fixed"]] == [
        "http://127.0.0.1:5001/old"]
    assert any("implicit v0" in note for note in result["notes"])


def test_diff_explicit_ignore_overrides_current_version(store):
    first, second, _v1, _v2 = _two_runs_with_versions(store)
    # The override replaces the run's stored rules for this diff entirely.
    override = IgnoreRules([IgnoreRule("/new"), IgnoreRule("/also-ignored")])
    result = diff_runs(store, "prod", second, override)
    assert result["new_broken"] == []
    assert any("override" in note for note in result["notes"])


def test_compute_diff_legacy_single_ignore_still_applies_both_sides():
    rules = IgnoreRules([IgnoreRule("ignored")])
    result = compute_diff(
        [link("http://127.0.0.1:5001/ignored")],
        [link("http://127.0.0.1:5001/ignored"), link("http://127.0.0.1:5001/new")],
        ignore=rules,
    )
    assert result["summary"] == {
        "new_broken": 1, "fixed": 0, "persistent": 0, "ignored_new": 1}


# --- migration from a pre-versioning database ------------------------------

LEGACY_SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url TEXT NOT NULL, depth INTEGER NOT NULL, status_code INTEGER,
    content_hash TEXT NOT NULL, duration_ms INTEGER NOT NULL, error TEXT,
    UNIQUE(run_id, url)
);
CREATE TABLE links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL, target_url TEXT NOT NULL, raw_href TEXT NOT NULL,
    kind TEXT NOT NULL, scope TEXT NOT NULL, line_no INTEGER NOT NULL,
    anchor TEXT NOT NULL DEFAULT '',
    UNIQUE(run_id, source_url, target_url, kind)
);
CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url TEXT NOT NULL, final_url TEXT, status_code INTEGER,
    redirect_chain_json TEXT NOT NULL DEFAULT '[]',
    anchor_ok INTEGER NOT NULL DEFAULT 1, ok INTEGER NOT NULL DEFAULT 0,
    error TEXT, checked_at TEXT NOT NULL,
    UNIQUE(run_id, url)
);
CREATE TABLE baselines (
    name TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
"""


def _build_legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO runs(seed, config_json, started_at, status, stats_json) "
        "VALUES (?, '{}', '2026-01-01T00:00:00+00:00', 'completed', '{}')",
        (SOURCE,))
    conn.execute(
        "INSERT INTO links(run_id, source_url, target_url, raw_href, kind, scope, "
        "line_no, anchor) VALUES (1, ?, ?, ?, 'page', 'internal', 1, '')",
        (SOURCE, "http://127.0.0.1:5001/legacy-broken",
         "/legacy-broken"))
    conn.execute(
        "INSERT INTO checks(run_id, url, final_url, status_code, redirect_chain_json, "
        "anchor_ok, ok, error, checked_at) VALUES "
        "(1, 'http://127.0.0.1:5001/legacy-broken', "
        "'http://127.0.0.1:5001/legacy-broken', 404, '[]', 1, 0, NULL, "
        "'2026-01-01T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO baselines(name, run_id, created_at) VALUES "
        "('prod', 1, '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()


def test_legacy_db_migrates_without_losing_history(tmp_path):
    db_path = tmp_path / "legacy.db"
    _build_legacy_db(db_path)

    store = Store(db_path)  # migration runs on open
    try:
        # Historical data is intact.
        run = store.get_run(1)
        assert run is not None and run["status"] == "completed"
        assert store.get_baseline("prod")["run_id"] == 1
        # Every legacy run points at the implicit v0 empty version.
        assert store.get_run_ignore_version_id(1) == IMPLICIT_VERSION_ID
        implicit = store.get_ignore_version(IMPLICIT_VERSION_ID)
        assert implicit["fingerprint"] == IMPLICIT_FINGERPRINT
        assert implicit["rules"] == [] and implicit["rule_count"] == 0

        # A new scan-style run can be created against a real version.
        version_id = register_ignore_version(
            store, IgnoreRules([IgnoreRule("/legacy/")]))
        new_run = store.create_run(SOURCE, {}, ignore_version_id=version_id)
        add_broken(store, new_run, "http://127.0.0.1:5001/legacy-broken")
        add_broken(store, new_run, "http://127.0.0.1:5001/fresh-broken")

        # Mixed old/new diff runs and explains itself.
        result = diff_runs(store, "prod", new_run)
        targets = {item["target_url"] for item in result["new_broken"]}
        assert targets == {"http://127.0.0.1:5001/fresh-broken"}
        assert any("implicit v0" in note for note in result["notes"])
        assert any("differ" in note for note in result["notes"])
    finally:
        store.close()

    # Reopening an already-migrated database is idempotent.
    again = Store(db_path)
    try:
        assert again.get_run_ignore_version_id(1) == IMPLICIT_VERSION_ID
        assert len(again.list_runs()) == 2
    finally:
        again.close()


# --- edge cases ------------------------------------------------------------

def test_load_rejects_invalid_files_and_entries(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(BaselineError, match="not found"):
        IgnoreRules.load(missing)
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("ignore = 42\n")
    with pytest.raises(BaselineError, match=r"\[\[ignore\]\]"):
        IgnoreRules.load(bad_toml)
    broken = tmp_path / "broken.toml"
    broken.write_text('[[ignore]]\nurl_regex = "("\n')
    with pytest.raises(BaselineError, match="invalid ignore regex"):
        IgnoreRules.load(broken)
    no_regex = tmp_path / "noregex.toml"
    no_regex.write_text('[[ignore]]\nstatus = [404]\n')
    with pytest.raises(BaselineError, match="url_regex"):
        IgnoreRules.load(no_regex)
    bad_status = tmp_path / "badstatus.toml"
    bad_status.write_text('[[ignore]]\nurl_regex = "x"\nstatus = [true]\n')
    with pytest.raises(BaselineError, match="list of integers"):
        IgnoreRules.load(bad_status)
    assert IgnoreRules.load(None).rules == []


def test_get_ignore_version_missing_and_run_lookup(store):
    assert store.get_ignore_version(9999) is None
    with pytest.raises(ValueError, match="run not found"):
        store.get_run_ignore_version_id(9999)


def test_dangling_version_pointer_falls_back_to_empty(tmp_path):
    """A run pointing at a version row that no longer exists still diffs."""
    db_path = tmp_path / "dangling.db"
    store = Store(db_path)
    try:
        run_id = store.create_run(SOURCE, {})
        # Bypass the foreign key to simulate externally corrupted legacy data.
        store.conn.execute("PRAGMA foreign_keys = OFF")
        store.conn.execute(
            "UPDATE runs SET ignore_version_id = 77 WHERE id = ?", (run_id,))
        store.conn.commit()
        add_broken(store, run_id, "http://127.0.0.1:5001/orphan-broken")
        other = store.create_run(SOURCE, {})
        add_broken(store, other, "http://127.0.0.1:5001/orphan-broken")
        store.set_baseline("b", other)
        result = diff_runs(store, "b", run_id)
        assert result["current_ignore_version"]["implicit"] is True
        assert any("implicit v0" in note for note in result["notes"])
    finally:
        store.close()


def test_store_context_manager_closes_connection(tmp_path):
    with Store(tmp_path / "ctx.db") as store:
        run_id = store.create_run(SOURCE, {})
    with pytest.raises(sqlite3.ProgrammingError):
        store.get_run(run_id)
