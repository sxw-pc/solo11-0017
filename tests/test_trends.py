"""Trend analysis: identity alignment, coverage semantics, classification."""

from __future__ import annotations

import sqlite3

import pytest

from linkpatrol.store import Store
from linkpatrol.trends import (
    SEV_CRITICAL,
    SEV_MAJOR,
    SEV_MINOR,
    SEV_OK,
    ScanArchive,
    TrendConfig,
    TrendError,
    build_trend_report,
    structural_key,
)

SEED = "http://127.0.0.1:9000/"


def make_run(store: Store, run_id: int, status: str = "completed",
             truncated: bool = False, drop_params: list[str] | None = None):
    rid = store.create_run(SEED, {"seed": SEED,
                                  "normalize": {"drop_params": drop_params or []}})
    assert rid == run_id
    stats = {"crawl": {"skipped_by_limits": 3 if truncated else 0}}
    store.finish_run(rid, status, stats)
    return rid


def page(url: str, status: int = 200, error: str | None = None,
         chain: list[dict] | None = None, content_hash: str = "h"):
    return {
        "url": url, "depth": 0, "status_code": status, "content_hash": content_hash,
        "duration_ms": 5, "error": error,
    }


def check(url: str, status: int = 200, ok: bool = True, anchor_ok: bool = True,
          chain: list[dict] | None = None, error: str | None = None,
          scope: str = "internal", checked_at: str = "2026-01-01T00:00:00+00:00"):
    return {
        "url": url, "final_url": url.split("#", 1)[0], "status_code": status,
        "redirect_chain": chain or [], "anchor_ok": anchor_ok, "ok": ok,
        "error": error, "checked_at": checked_at, "scope": scope,
    }


def entity_map(payload: dict) -> dict[str, dict]:
    return {item["key"]: item for item in payload["entities"]}


def series_of(payload: dict, key: str) -> list[dict]:
    return entity_map(payload)[key]["series"]


# ---------------------------------------------------------------------------
# Identity alignment
# ---------------------------------------------------------------------------

def test_structural_merge_when_query_identity_changes(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1 = make_run(store, 1, drop_params=["campaign"])
    r2 = make_run(store, 2, drop_params=[])
    store.insert_page(r1, page("http://127.0.0.1:9000/landing"))
    store.insert_page(r2, page("http://127.0.0.1:9000/landing?campaign=x"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    landing = entity_map(payload)["http://127.0.0.1:9000/landing"]
    assert landing["alignment"] == "structural"
    assert landing["observed_runs"] == 2
    assert "http://127.0.0.1:9000/landing?campaign=x" in landing["aliases"]
    assert all(point["observed"] for point in landing["series"])


def test_ambiguous_coexisting_query_variants_are_never_merged(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2 = make_run(store, 1), make_run(store, 2)
    for rid in (r1, r2):
        store.insert_page(rid, page("http://127.0.0.1:9000/docs"))
        store.insert_page(rid, page("http://127.0.0.1:9000/docs?campaign=footer"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(
            archive, TrendConfig(seed=SEED, min_occurrences=1))
    keys = entity_map(payload)
    assert "http://127.0.0.1:9000/docs" in keys
    assert "http://127.0.0.1:9000/docs?campaign=footer" in keys
    assert all(item["alignment"] == "ambiguous"
               for key, item in keys.items()
               if key.startswith("http://127.0.0.1:9000/docs"))
    assert payload["summary"]["ambiguous_entities"] == 2


def test_fragment_checks_fold_into_base_page_identity(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1 = make_run(store, 1)
    r2 = make_run(store, 2)
    for rid in (r1, r2):
        store.insert_page(rid, page("http://127.0.0.1:9000/about"))
        store.insert_check(
            rid, check("http://127.0.0.1:9000/about#section", ok=False,
                       anchor_ok=False, error="anchor_missing"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    keys = entity_map(payload)
    assert "http://127.0.0.1:9000/about" in keys
    assert "http://127.0.0.1:9000/about#section" not in keys
    anchor = {a["key"]: a for a in payload["anchors"]}[
        "http://127.0.0.1:9000/about#section"]
    assert all(point["anchor_verdict"] == "fail" for point in anchor["series"])


# ---------------------------------------------------------------------------
# Coverage: missing vs unscanned
# ---------------------------------------------------------------------------

def test_truncated_run_marks_unvisited_urls_unscanned_not_disappeared(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1 = make_run(store, 1)
    r2 = make_run(store, 2, truncated=True)
    r3 = make_run(store, 3)
    for rid in (r1, r2, r3):
        store.insert_page(rid, page("http://127.0.0.1:9000/"))
    store.insert_page(r1, page("http://127.0.0.1:9000/late"))
    store.insert_page(r3, page("http://127.0.0.1:9000/late"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    late = series_of(payload, "http://127.0.0.1:9000/late")
    assert [point["coverage"] for point in late] == ["observed", "unscanned", "observed"]
    # No disappearance event may be attributed to the truncated run.
    assert payload["events"]["disappeared"] == []


def test_complete_run_absence_is_real_disappearance(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1 = make_run(store, 1)
    r2 = make_run(store, 2)
    store.insert_page(r1, page("http://127.0.0.1:9000/vanish"))
    store.insert_page(r2, page("http://127.0.0.1:9000/"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    vanish = series_of(payload, "http://127.0.0.1:9000/vanish")
    assert [point["coverage"] for point in vanish] == ["observed", "missing"]
    gone = payload["events"]["disappeared"]
    assert len(gone) == 1 and gone[0]["entity_key"].endswith("/vanish")


def test_truncated_run_distinguishes_covered_and_uncovered_structural_paths(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1 = make_run(store, 1)
    r2 = make_run(store, 2, truncated=True)
    # Two distinct structural paths.  In the truncated run the crawler
    # reached /covered but never reached /unvisited: the former is observed
    # normally while the latter must be unscanned (never a disappearance).
    covered = "http://127.0.0.1:9000/covered"
    unvisited = "http://127.0.0.1:9000/unvisited"
    store.insert_page(r1, page(covered))
    store.insert_page(r1, page(unvisited))
    store.insert_page(r2, page(covered))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(
            archive, TrendConfig(seed=SEED, min_occurrences=1))
    entities = entity_map(payload)
    assert entities[covered]["series"][1]["coverage"] == "observed"
    assert entities[unvisited]["series"][1]["coverage"] == "unscanned"
    assert payload["events"]["disappeared"] == []


def test_query_variant_absence_under_fetched_path_is_missing_not_unscanned(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1 = make_run(store, 1)
    r2 = make_run(store, 2, truncated=True)
    plain = "http://127.0.0.1:9000/docs"
    variant = "http://127.0.0.1:9000/docs?campaign=footer"
    store.insert_page(r1, page(variant))
    # The truncated run did fetch the /docs structural path, so the missing
    # query variant is a real absence rather than a cap artifact.
    store.insert_page(r2, page(plain))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(
            archive, TrendConfig(seed=SEED, min_occurrences=1))
    docs = entity_map(payload)["http://127.0.0.1:9000/docs"]
    assert docs["alignment"] == "structural"
    assert docs["series"][1]["coverage"] == "observed"
    assert variant in docs["aliases"]


# ---------------------------------------------------------------------------
# Anomaly classification
# ---------------------------------------------------------------------------

def _status_run(store: Store, rid: int, url: str, status: int | None,
                error: str | None = None):
    store.insert_page(rid, page(url, status=status if status else 200, error=error))
    store.insert_check(rid, check(
        url, status=status if status is not None else 200,
        ok=status is not None and 200 <= status < 300 and error is None,
        error=error))


def test_deteriorating_classification_requires_monotonic_worsening(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2, r3 = make_run(store, 1), make_run(store, 2), make_run(store, 3)
    url = "http://127.0.0.1:9000/about"
    _status_run(store, r1, url, 200)
    _status_run(store, r2, url, 500)
    _status_run(store, r3, url, 404)
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    entity = entity_map(payload)[url]
    assert entity["classification"] == "deteriorating"
    assert [point["severity"] for point in entity["series"]] == [
        SEV_OK, SEV_CRITICAL, SEV_MAJOR]


def test_flapping_requires_direction_changes_both_ways(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2, r3, r4 = (make_run(store, i) for i in (1, 2, 3, 4))
    url = "http://127.0.0.1:9000/docs"
    for rid, status in ((r1, 200), (r2, 500), (r3, 200), (r4, 500)):
        _status_run(store, rid, url, status)
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    assert entity_map(payload)[url]["classification"] == "flapping"


def test_stable_unhealthy_and_stable_healthy(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2 = make_run(store, 1), make_run(store, 2)
    _status_run(store, r1, "http://127.0.0.1:9000/missing", 404)
    _status_run(store, r2, "http://127.0.0.1:9000/missing", 404)
    _status_run(store, r1, "http://127.0.0.1:9000/ok", 200)
    _status_run(store, r2, "http://127.0.0.1:9000/ok", 200)
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    entities = entity_map(payload)
    assert entities["http://127.0.0.1:9000/missing"]["classification"] == "stable_unhealthy"
    assert entities["http://127.0.0.1:9000/ok"]["classification"] == "stable_healthy"


def test_min_occurrences_excludes_sparse_entities_from_classification(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2, r3 = make_run(store, 1), make_run(store, 2), make_run(store, 3)
    store.insert_page(r1, page("http://127.0.0.1:9000/once"))
    for rid in (r1, r2, r3):
        store.insert_page(rid, page("http://127.0.0.1:9000/always"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(
            archive, TrendConfig(seed=SEED, min_occurrences=2))
    entities = entity_map(payload)
    sparse = entities["http://127.0.0.1:9000/once"]
    assert sparse["eligible"] is False
    assert sparse["classification"] == "below_threshold"
    # It still appears in the detailed series.
    assert len(sparse["series"]) == 3
    assert entities["http://127.0.0.1:9000/always"]["eligible"] is True


# ---------------------------------------------------------------------------
# Anchors, external failures, redirects
# ---------------------------------------------------------------------------

def test_anchor_gone_when_page_structure_drops_fragment(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2 = make_run(store, 1), make_run(store, 2)
    for rid in (r1, r2):
        store.insert_page(rid, page("http://127.0.0.1:9000/docs"))
    store.insert_check(r1, check("http://127.0.0.1:9000/docs#install"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    anchor = {a["key"]: a for a in payload["anchors"]}[
        "http://127.0.0.1:9000/docs#install"]
    assert [point["anchor_verdict"] for point in anchor["series"]] == ["ok", "missing"]
    assert any(event["type"] == "anchor_gone" for event in anchor["events"])


def test_failed_fetch_marks_anchor_unevaluated_not_failed(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2 = make_run(store, 1), make_run(store, 2)
    for rid in (r1, r2):
        store.insert_page(rid, page("http://127.0.0.1:9000/docs"))
    store.insert_check(r1, check("http://127.0.0.1:9000/docs#install"))
    store.insert_check(r2, check("http://127.0.0.1:9000/docs#install",
                                 status=500, ok=False, anchor_ok=False,
                                 error="HTTPError: 500"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    anchor = {a["key"]: a for a in payload["anchors"]}[
        "http://127.0.0.1:9000/docs#install"]
    assert [point["anchor_verdict"] for point in anchor["series"]] == [
        "ok", "unevaluated"]


def test_external_failure_count_feeds_page_severity(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2 = make_run(store, 1), make_run(store, 2)
    for rid in (r1, r2):
        store.insert_page(rid, page("http://127.0.0.1:9000/"))
        store.insert_link(rid, "http://127.0.0.1:9000/", {
            "target_url": "http://ext.example/down", "raw_href": "http://ext.example/down",
            "kind": "page", "scope": "external", "line_no": 1, "anchor": ""})
        store.insert_check(rid, check("http://ext.example/down", status=502, ok=False,
                                     error="HTTPError: 502", scope="external"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(
            archive, TrendConfig(seed=SEED, min_occurrences=1))
    home = entity_map(payload)["http://127.0.0.1:9000/"]
    point = home["series"][0]
    assert point["external_failures"] == 1
    assert point["severity"] == SEV_MINOR


def test_redirect_chain_length_is_recorded_per_run(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1, r2 = make_run(store, 1), make_run(store, 2)
    chain = [{"url": "http://127.0.0.1:9000/hop", "status": 302,
              "location": "http://127.0.0.1:9000/landing"}]
    for rid in (r1, r2):
        store.insert_page(rid, page("http://127.0.0.1:9000/hop"))
        store.insert_check(rid, check("http://127.0.0.1:9000/hop", chain=chain))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(archive, TrendConfig(seed=SEED))
    hop = entity_map(payload)["http://127.0.0.1:9000/hop"]
    assert all(point["redirect_chain_length"] == 1 for point in hop["series"])


# ---------------------------------------------------------------------------
# Run selection, incomplete runs, and validation
# ---------------------------------------------------------------------------

def test_incomplete_runs_excluded_by_default_and_annotated_when_included(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    r1 = make_run(store, 1)
    r2 = make_run(store, 2, status="failed")
    for rid in (r1, r2):
        store.insert_page(rid, page("http://127.0.0.1:9000/"))
    store.close()

    with ScanArchive(db) as archive:
        with pytest.raises(TrendError):
            build_trend_report(archive, TrendConfig(seed=SEED))
        payload = build_trend_report(
            archive, TrendConfig(seed=SEED, include_incomplete=True,
                                 min_occurrences=1))
    assert payload["summary"]["runs_incomplete"] == 1
    assert payload["runs"][1]["status"] == "failed"


def test_time_window_and_run_id_filters(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    for i in range(3):
        rid = make_run(store, i + 1)
        store.insert_page(rid, page("http://127.0.0.1:9000/"))
    store.close()

    with ScanArchive(db) as archive:
        by_ids = build_trend_report(
            archive, TrendConfig(seed=SEED, run_ids={1, 3}, min_occurrences=1))
        assert [run["run_id"] for run in by_ids["runs"]] == [1, 3]
        with pytest.raises(TrendError):
            build_trend_report(
                archive, TrendConfig(seed=SEED,
                                     since="2099-01-01T00:00:00+00:00",
                                     min_occurrences=1))


def test_unknown_run_id_is_rejected(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    make_run(store, 1)
    store.close()
    with ScanArchive(db) as archive:
        with pytest.raises(TrendError):
            build_trend_report(
                archive, TrendConfig(seed=SEED, run_ids={99}, min_occurrences=1))


def test_seed_filtering_isolates_different_sites(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    rid_a1 = store.create_run("http://127.0.0.1:9000/", {})
    rid_a2 = store.create_run("http://127.0.0.1:9000/", {})
    rid_b1 = store.create_run("http://127.0.0.1:9001/", {})
    rid_b2 = store.create_run("http://127.0.0.1:9001/", {})
    for rid in (rid_a1, rid_a2, rid_b1, rid_b2):
        store.finish_run(rid, "completed", {})
        store.insert_page(rid, page("http://127.0.0.1:9000/"))
    store.close()

    with ScanArchive(db) as archive:
        payload = build_trend_report(
            archive, TrendConfig(seed="http://127.0.0.1:9001/", min_occurrences=1))
    assert len(payload["entities"]) == 1
    assert payload["entities"][0]["key"].startswith("http://127.0.0.1:9000/")
    assert [run["status"] for run in payload["runs"]] == ["completed", "completed"]
    assert len(payload["runs"]) == 2


def test_requires_two_runs(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    rid = make_run(store, 1)
    store.insert_page(rid, page("http://127.0.0.1:9000/"))
    store.close()
    with ScanArchive(db) as archive:
        with pytest.raises(TrendError):
            build_trend_report(archive, TrendConfig(seed=SEED))


def test_rejects_invalid_seed_and_threshold(tmp_path):
    db = tmp_path / "scan.db"
    Store(db).close()
    with ScanArchive(db) as archive:
        with pytest.raises(TrendError):
            build_trend_report(
                archive, TrendConfig(seed="ftp://127.0.0.1/"))
        with pytest.raises(TrendError):
            build_trend_report(
                archive, TrendConfig(seed=SEED, min_occurrences=0))
        with pytest.raises(TrendError):
            build_trend_report(
                archive, TrendConfig(seed=SEED, run_ids=set()))


def test_scan_database_is_opened_read_only(tmp_path):
    db = tmp_path / "scan.db"
    store = Store(db)
    rid = make_run(store, 1)
    store.insert_page(rid, page("http://127.0.0.1:9000/"))
    store.close()
    archive = ScanArchive(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            archive.conn.execute("CREATE TABLE attempted_write(id INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            archive.conn.execute(
                "INSERT INTO runs(seed, config_json, started_at, status) "
                "VALUES ('x', '{}', '2026-01-01', 'running')")
    finally:
        archive.close()


def test_structural_key_helper():
    assert structural_key("http://h:8080/a/b?x=1#frag") == "http://h:8080/a/b"
