"""SQLite schema, uniqueness, rollback, and baseline tests."""

import sqlite3

import pytest

from linkpatrol.budget import AttemptDetail
from linkpatrol.report import build_report
from linkpatrol.store import BaselineExistsError, Store

PAGE = {"url": "http://127.0.0.1:5001/", "depth": 0, "status_code": 200,
        "content_hash": "abc", "duration_ms": 12, "error": None}
LINK = {"target_url": "http://127.0.0.1:5001/about", "raw_href": "/about",
        "kind": "page", "scope": "internal", "line_no": 3, "anchor": ""}
CHECK = {"url": "http://127.0.0.1:5001/about",
         "final_url": "http://127.0.0.1:5001/about", "status_code": 200,
         "redirect_chain": [], "anchor_ok": True, "ok": True, "error": None,
         "checked_at": "2026-01-01T00:00:00+00:00"}

def make_run(store):
    return store.create_run("http://127.0.0.1:5001/", {"seed": "x"})

def test_run_lifecycle_and_stats_merge(store):
    run_id = make_run(store)
    store.finish_run(run_id, "completed", {"broken": 2})
    store.merge_stats(run_id, {"last_diff": {"new_broken": 1}})
    run = store.get_run(run_id)
    assert run["status"] == "completed" and run["finished_at"]
    assert run["config"] == {"seed": "x"}
    assert run["stats"] == {"broken": 2, "last_diff": {"new_broken": 1}}

def test_page_uniqueness_and_new_runs(store):
    first, second = make_run(store), make_run(store)
    assert store.insert_page(first, PAGE) and not store.insert_page(first, PAGE)
    assert store.insert_page(second, PAGE)
    assert len(store.get_pages(first)) == 1 and len(store.get_pages(second)) == 1
    assert store.write_errors

def test_foreign_key_failure_rolls_back_only_that_row(store):
    run_id = make_run(store)
    assert store.insert_page(run_id, PAGE) and not store.insert_page(9999, PAGE)
    assert len(store.get_pages(run_id)) == 1

def test_link_and_check_uniqueness(store):
    run_id = make_run(store)
    assert store.insert_link(run_id, PAGE["url"], LINK)
    assert not store.insert_link(run_id, PAGE["url"], LINK)
    assert store.insert_check(run_id, CHECK) and not store.insert_check(run_id, CHECK)
    assert len(store.get_links(run_id)) == 1 and len(store.get_checks(run_id)) == 1

def test_broken_identity_join(store):
    run_id = make_run(store)
    target = "http://127.0.0.1:5001/missing"
    store.insert_link(run_id, PAGE["url"], dict(LINK, target_url=target))
    store.insert_check(run_id, dict(CHECK, url=target, status_code=404, ok=False))
    assert store.broken_identities(run_id) == [{
        "source_url": PAGE["url"], "target_url": target, "status_code": 404,
        "error": None, "anchor_ok": 1}]

def test_baseline_set_force_and_delete(store):
    first, second = make_run(store), make_run(store)
    store.set_baseline("main", first)
    with pytest.raises(BaselineExistsError):
        store.set_baseline("main", second)
    store.set_baseline("main", second, force=True)
    assert store.get_baseline("main")["run_id"] == second
    assert store.delete_baseline("main") and not store.delete_baseline("main")


def test_attempt_detail_round_trip_and_uniqueness(store):
    run_id = make_run(store)
    detail = AttemptDetail(
        target_url="http://127.0.0.1:5001/server-error", phase="crawl",
        attempts=3, retry_count=2, first_failure_code=500,
        final_failure_code=500, budget_skipped=False, error=None,
    )
    assert store.insert_attempt_detail(run_id, detail)
    assert not store.insert_attempt_detail(run_id, detail)
    rows = store.get_attempt_details(run_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["target_url"].endswith("/server-error") and row["phase"] == "crawl"
    assert row["attempts"] == 3 and row["retry_count"] == 2
    assert row["first_failure_code"] == row["final_failure_code"] == 500
    assert row["budget_skipped"] is False
    # Dict-shaped rows are accepted too.
    store.insert_attempt_detail(run_id, {
        "target_url": "http://127.0.0.1:5001/other", "phase": "check",
        "attempts": 0, "budget_skipped": True, "error": "budget_exhausted"})
    assert len(store.get_attempt_details(run_id)) == 2


def test_legacy_database_without_attempt_details_table(tmp_path):
    """Opening an old-schema DB adds the table; reports degrade to empty."""
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
        INSERT INTO runs(seed, config_json, started_at, status)
        VALUES ('http://127.0.0.1:5001/', '{}', '2026-01-01T00:00:00+00:00',
                'completed');
        """
    )
    conn.commit()
    conn.close()

    store = Store(legacy)
    try:
        assert store.get_attempt_details(1) == []
        data = build_report(store, 1)
        distribution = data["failure_distribution"]
        assert distribution["by_code"] == {}
        assert distribution["by_target"] == []
        assert distribution["skipped_budget"] == 0
        assert distribution["total_retries"] == 0
        assert distribution["budget"]["exhausted"] is False
        assert data["summary"]["skipped_budget"] == 0
    finally:
        store.close()
