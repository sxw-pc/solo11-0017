"""Persistence and idempotency of trend artifacts in the separate trend DB."""

from __future__ import annotations

import json

import pytest

from linkpatrol.store import Store
from linkpatrol.trends import ScanArchive, TrendConfig, build_trend_report
from linkpatrol.trendstore import TrendStore

SEED = "http://127.0.0.1:9000/"


def _populate(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    for rid, status in ((1, 200), (2, 500), (3, 404)):
        run_id = store.create_run(SEED, {"seed": SEED, "normalize": {"drop_params": []}})
        assert run_id == rid
        store.finish_run(run_id, "completed", {"crawl": {"skipped_by_limits": 0}})
        store.insert_page(run_id, {
            "url": "http://127.0.0.1:9000/about", "depth": 0,
            "status_code": status, "content_hash": f"h{rid}", "duration_ms": 5,
            "error": None if status < 400 else f"HTTP {status}",
        })
        store.insert_check(run_id, {
            "url": "http://127.0.0.1:9000/about",
            "final_url": "http://127.0.0.1:9000/about", "status_code": status,
            "redirect_chain": [], "anchor_ok": True,
            "ok": 200 <= status < 300,
            "error": None if status < 400 else f"HTTP {status}",
            "checked_at": "2026-01-01T00:00:00+00:00",
        })
    store.close()
    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    return payload


def test_save_is_idempotent_on_content_hash(tmp_path):
    payload = _populate(tmp_path)
    trend_db = tmp_path / "trends.db"
    with TrendStore(trend_db) as trend_store:
        report_id, created = trend_store.save_report(payload)
        assert created is True
        again_id, again_created = trend_store.save_report(payload)
        assert again_created is False
        assert again_id == report_id
        rows = trend_store.conn.execute(
            "SELECT COUNT(*) FROM trend_reports").fetchone()[0]
        assert rows == 1


def test_different_run_sets_create_distinct_reports(tmp_path):
    payload = _populate(tmp_path)
    with TrendStore(tmp_path / "trends.db") as trend_store:
        first_id, _ = trend_store.save_report(payload)
        changed = json.loads(json.dumps(payload))
        changed["parameters"]["min_occurrences"] = 99
        # Recompute the identity hash the same way the builder would.
        from linkpatrol.trends import identity_hash

        changed["content_hash"] = identity_hash(changed)
        second_id, created = trend_store.save_report(changed)
        assert created is True and second_id != first_id


def test_series_entities_and_events_are_queryable(tmp_path):
    payload = _populate(tmp_path)
    with TrendStore(tmp_path / "trends.db") as trend_store:
        report_id, _ = trend_store.save_report(payload)
        entities = trend_store.get_entities(report_id, kind="page")
        assert len(entities) == 1
        deteriorating = trend_store.get_entities(
            report_id, classification="deteriorating")
        assert len(deteriorating) == 1
        rows = trend_store.conn.execute(
            "SELECT coverage, severity FROM trend_series "
            "WHERE report_id = ? ORDER BY run_id", (report_id,)).fetchall()
        assert [row["coverage"] for row in rows] == [
            "observed", "observed", "observed"]
        assert [row["severity"] for row in rows] == [0, 3, 2]
        stored = trend_store.get_report(report_id)
        assert stored["summary"]["runs_total"] == 3
        assert stored["content_hash"] == payload["content_hash"]


def test_list_reports_filters_by_seed(tmp_path):
    payload = _populate(tmp_path)
    with TrendStore(tmp_path / "trends.db") as trend_store:
        trend_store.save_report(payload)
        assert len(trend_store.list_reports()) == 1
        assert trend_store.list_reports(seed="http://other.example/") == []
        assert len(trend_store.list_reports(seed=SEED)) == 1


def test_get_missing_report_returns_none(tmp_path):
    with TrendStore(tmp_path / "trends.db") as trend_store:
        assert trend_store.get_report(42) is None
