"""Archive retention, baseline protection, atomic rollback, and timeline tests."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from linkpatrol.archive import (
    ArchiveError, archive_runs, plan_archive, summarize_run,
)
from linkpatrol.store import Store
from linkpatrol.timeline import run_timeline, url_history

SOURCE = "http://127.0.0.1:5001/"
OTHER = "http://127.0.0.1:5001/about"

def add_link(store, run_id, source, target):
    store.insert_link(run_id, source, {
        "target_url": target, "raw_href": target, "kind": "page",
        "scope": "internal", "line_no": 1, "anchor": ""})

def add_check(store, run_id, target, ok=True, status=200):
    store.insert_check(run_id, {
        "url": target, "final_url": target, "status_code": status,
        "redirect_chain": [], "anchor_ok": True, "ok": ok, "error": None,
        "checked_at": "2026-01-01T00:00:00+00:00"})

def add_page(store, run_id, url):
    store.insert_page(run_id, {
        "url": url, "depth": 0, "status_code": 200, "content_hash": "h",
        "duration_ms": 1, "error": None})

def make_run(store, broken=(), fixed_pages=("http://127.0.0.1:5001/",)):
    """Create a run with one healthy page per URL plus the given broken links."""
    run_id = store.create_run(SOURCE, {})
    for url in fixed_pages:
        add_page(store, run_id, url)
    for target in broken:
        add_link(store, run_id, SOURCE, target)
        add_check(store, run_id, target, ok=False, status=404)
    store.finish_run(run_id, "completed", {"broken": len(broken)})
    return run_id

def count(store, table, run_id):
    return store.conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
    ).fetchone()[0]

def test_plan_keep_and_retention_window():
    store = Store(":memory:")
    now = datetime.now(timezone.utc)
    ids = []
    for days in (30, 20, 10, 5):
        run_id = store.create_run(SOURCE, {})
        store.finish_run(run_id, "completed")
        store.conn.execute(
            "UPDATE runs SET finished_at = ? WHERE id = ?",
            ((now - timedelta(days=days)).isoformat(), run_id),
        )
        ids.append(run_id)
    store.conn.commit()
    plan = plan_archive(store, keep_runs=1, retain_days=7, now=now)
    assert plan.archived == [ids[0], ids[1], ids[2]]  # 30, 20, 10 days old
    assert plan.kept == [ids[3]]  # 5 days old and the newest, kept twice over
    with pytest.raises(ArchiveError):
        plan_archive(store)
    with pytest.raises(ArchiveError):
        plan_archive(store, keep_runs=-1)

def test_plan_rejects_negative_values():
    store = Store(":memory:")
    with pytest.raises(ArchiveError):
        plan_archive(store, keep_runs=-1)
    with pytest.raises(ArchiveError):
        plan_archive(store, retain_days=-1)

def test_archive_removes_details_but_keeps_timeline():
    store = Store(":memory:")
    first = make_run(store, broken=["http://127.0.0.1:5001/old"])
    second = make_run(store, broken=["http://127.0.0.1:5001/old"])
    result = archive_runs(store, [first])
    assert result["archived"] == [first] and result["protected"] == []
    assert store.get_run(first) is None
    for table in ("pages", "links", "checks"):
        assert count(store, table, first) == 0
    assert count(store, "pages", second) == 1
    timeline = store.get_timeline_run(first)
    assert timeline["checks_total"] == 1
    assert timeline["broken_total"] == 1
    assert timeline["new_broken_total"] == 1
    rows = store.get_timeline_broken(run_id=first)
    assert [(row["target_url"], row["is_new"]) for row in rows] == [
        ("http://127.0.0.1:5001/old", 1)]
    assert store.get_run(second) is not None

def test_archive_protects_baseline_runs_and_reports_them():
    store = Store(":memory:")
    protected = make_run(store, broken=["http://127.0.0.1:5001/pinned"])
    other = make_run(store, broken=["http://127.0.0.1:5001/old"])
    store.set_baseline("prod", protected)
    result = archive_runs(store, [protected, other])
    assert result["protected"] == [protected]
    assert result["archived"] == [other]
    assert store.get_run(protected) is not None
    assert count(store, "links", protected) == 1
    assert count(store, "checks", protected) == 1
    assert count(store, "pages", protected) == 1
    plan = plan_archive(store, keep_runs=0, retain_days=0)
    assert protected in plan.protected and protected not in plan.archived

def test_archive_stage_failure_rolls_back_database_and_files(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    run_id = make_run(store, broken=["http://127.0.0.1:5001/old"])
    json_path = report_dir / f"run_{run_id}.json"
    html_path = report_dir / f"run_{run_id}.html"
    json_path.write_text("{}", encoding="utf-8")
    html_path.write_text("<html></html>", encoding="utf-8")

    def fail(stage):
        if stage == "after_artifacts":
            raise RuntimeError("injected failure")

    with pytest.raises(ArchiveError):
        archive_runs(store, [run_id], report_dir=report_dir, failure_hook=fail)
    assert store.get_run(run_id) is not None
    assert count(store, "pages", run_id) == 1
    assert count(store, "links", run_id) == 1
    assert count(store, "checks", run_id) == 1
    assert store.get_timeline_run(run_id) is None
    assert json_path.is_file() and html_path.is_file()
    assert not (report_dir / f".archive-tmp-{__import__('os').getpid()}").exists()
    for table in ("pages", "links", "checks"):
        orphan = store.conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE run_id NOT IN (SELECT id FROM runs)"
        ).fetchone()[0]
        assert orphan == 0
    assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

def test_archive_db_write_failure_rolls_back(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    run_id = make_run(store, broken=["http://127.0.0.1:5001/old"])

    def fail(stage):
        if stage == "after_db_write":
            raise RuntimeError("boom")

    with pytest.raises(ArchiveError):
        archive_runs(store, [run_id], report_dir=tmp_path / "reports", failure_hook=fail)
    assert store.get_run(run_id) is not None
    assert count(store, "checks", run_id) == 1
    assert store.get_timeline_run(run_id) is None

def test_archive_removes_report_artifacts(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    run_id = make_run(store, broken=["http://127.0.0.1:5001/old"])
    for suffix in (".json", ".html"):
        (report_dir / f"run_{run_id}{suffix}").write_text("x", encoding="utf-8")
    survivor = report_dir / "run_999.json"
    survivor.write_text("keep", encoding="utf-8")
    archive_runs(store, [run_id], report_dir=report_dir)
    assert not (report_dir / f"run_{run_id}.json").exists()
    assert not (report_dir / f"run_{run_id}.html").exists()
    assert survivor.read_text(encoding="utf-8") == "keep"

def test_plan_keeps_unfinished_runs():
    store = Store(":memory:")
    running = store.create_run(SOURCE, {})
    failed = store.create_run(SOURCE, {})
    store.finish_run(failed, "failed")
    plan = plan_archive(store, keep_runs=0)
    assert plan.archived == []
    assert plan.reason[running] == "not_completed"
    assert plan.reason[failed] == "not_completed"
    with pytest.raises(ArchiveError):
        archive_runs(store, [failed])


def test_archive_missing_run_raises_and_changes_nothing():
    store = Store(":memory:")
    with pytest.raises(ArchiveError):
        archive_runs(store, [42])


def test_timeline_answers_first_broken_and_fix_across_archives():
    store = Store(":memory:")
    target = "http://127.0.0.1:5001/old"
    r1 = make_run(store, broken=[])
    r2 = make_run(store, broken=[target])       # becomes broken
    r3 = make_run(store, broken=[target])       # still broken
    r4 = make_run(store, broken=[])             # fixed
    archive_runs(store, [r1, r2, r3])
    history = url_history(store, target)
    assert history["first_broken_run"] == r2
    assert history["fix_runs"] == [r4]
    assert history["last_fixed_run"] == r4
    assert history["currently_broken"] is False
    assert len(history["spans"]) == 1
    span = history["spans"][0]
    assert span["run_ids"] == [r2, r3]
    assert span["fixed_run"] == r4
    assert [entry["run_id"] for entry in history["entries"]] == [r1, r2, r3, r4]
    assert history["entries"][1]["first_broken"] is True
    assert history["entries"][3]["fixed_run"] == r4

def test_timeline_dedupes_repeated_target_within_one_run():
    store = Store(":memory:")
    target = "http://127.0.0.1:5001/dup"
    run_id = make_run(store, broken=[])
    for source in (SOURCE, OTHER):
        add_link(store, run_id, source, target)
    add_check(store, run_id, target, ok=False, status=404)
    store.finish_run(run_id, "completed")
    summary = summarize_run(store, run_id)
    assert summary["broken_total"] == 1  # two link rows, one URL
    assert len(summary["broken"]) == 1
    archive_runs(store, [run_id])
    assert len(store.get_timeline_broken(run_id=run_id)) == 1
    assert url_history(store, target)["spans"][0]["run_ids"] == [run_id]

def test_timeline_still_open_and_run_listing():
    store = Store(":memory:")
    target = "http://127.0.0.1:5001/old"
    r1 = make_run(store, broken=[])
    r2 = make_run(store, broken=[target])
    archive_runs(store, [r1, r2])
    history = url_history(store, target)
    assert history["still_open"] is True
    assert history["spans"][0]["fixed_run"] is None
    rows = run_timeline(store)
    assert [row["run_id"] for row in rows] == [r1, r2]
    assert all(row["archived"] for row in rows)
    assert rows[1]["new_broken_total"] == 1 and rows[1]["fixed_total"] == 0

def test_failed_run_is_not_a_diff_baseline():
    store = Store(":memory:")
    target = "http://127.0.0.1:5001/old"
    good = make_run(store, broken=[target])
    failed = store.create_run(SOURCE, {})
    store.finish_run(failed, "failed", {"error": "scan aborted"})
    after = make_run(store, broken=[target])
    summary = summarize_run(store, after)
    assert summary["new_broken_total"] == 0  # compared with the completed run
    assert summary["fixed_total"] == 0


def test_archiving_in_one_batch_keeps_diffs_against_earlier_archived_runs():
    store = Store(":memory:")
    target = "http://127.0.0.1:5001/old"
    r1 = make_run(store, broken=[target])
    r2 = make_run(store, broken=[])
    r3 = make_run(store, broken=[target])
    entries = [summarize_run(store, run_id) for run_id in (r1, r2, r3)]
    assert entries[1]["fixed_total"] == 1
    assert entries[2]["new_broken_total"] == 1
    archive_runs(store, [r1, r2, r3])
    rows = sorted(store.list_timeline_runs(), key=lambda row: row["run_id"])
    assert [row["fixed_total"] for row in rows] == [0, 1, 0]
    assert [row["new_broken_total"] for row in rows] == [1, 0, 1]


def test_failed_run_appears_in_history_but_does_not_break_spans():
    store = Store(":memory:")
    target = "http://127.0.0.1:5001/old"
    r1 = make_run(store, broken=[target])
    failed = store.create_run(SOURCE, {})
    store.finish_run(failed, "failed", {"error": "scan aborted"})
    r2 = make_run(store, broken=[])
    history = url_history(store, target)
    assert history["first_broken_run"] == r1
    assert history["fix_runs"] == [r2]
    assert history["spans"][0]["run_ids"] == [r1]
    failed_entry = next(e for e in history["entries"] if e["run_id"] == failed)
    assert failed_entry["broken"] is None
    assert failed_entry["ignored"] == "not_completed"


def test_live_run_history_merges_with_archived():
    store = Store(":memory:")
    target = "http://127.0.0.1:5001/old"
    r1 = make_run(store, broken=[])
    r2 = make_run(store, broken=[target])
    archive_runs(store, [r1])
    history = url_history(store, target)
    assert history["first_broken_run"] == r2
    assert history["entries"][0]["archived"] is True
    assert history["entries"][1]["archived"] is False
    assert history["currently_broken"] is True
