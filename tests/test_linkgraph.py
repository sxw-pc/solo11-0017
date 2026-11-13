"""Link graph dedup, persistence, migration, and legacy-database tests."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from linkpatrol.linkgraph import LinkGraph, build_graph
from linkpatrol.report import build_report
from linkpatrol.schema import SCHEMA
from linkpatrol.store import Store

@dataclass
class FakeLink:
    target_url: str
    raw_href: str
    kind: str = "page"
    scope: str = "internal"
    line_no: int = 1
    anchor: str = ""

def link(target, kind="page", line_no=1):
    return FakeLink(target_url=target, raw_href=target, kind=kind, line_no=line_no)

def test_same_target_and_kind_dedupes_to_one_edge():
    graph = LinkGraph()
    for n in range(10):
        graph.add(f"http://s/{n}", link("http://t/page"))
    assert len(graph) == 1
    edge = graph.edges()[0]
    assert edge.source_count == 10
    assert edge.source_url == "http://s/0"  # first occurrence is representative
    assert [s.source_url for s in edge.sources] == [f"http://s/{n}" for n in range(10)]
    assert graph.reference_count == 10

def test_kind_and_source_page_semantics():
    graph = build_graph([
        ("http://s/0", link("http://t/x", kind="page")),
        ("http://s/0", link("http://t/x", kind="asset")),
        ("http://s/0", link("http://t/x", kind="page")),  # same page twice
        ("http://s/1", link("http://t/x", kind="page")),
    ])
    assert len(graph) == 2
    page_edge = graph.edges()[0]
    assert page_edge.kind == "page" and page_edge.source_count == 2
    asset_edge = graph.edges()[1]
    assert asset_edge.kind == "asset" and asset_edge.source_count == 1
    assert graph.sources_for("http://t/x") == ["http://s/0", "http://s/1", "http://s/0"]

def test_concurrent_adds_never_double_count():
    graph = LinkGraph()

    def worker(n):
        for i in range(200):
            graph.add(f"http://src/{n}", link(f"http://t/{i % 50}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    assert len(graph) == 50
    # 50 unique targets, each referenced once per worker (repeats from the
    # same source page collapse, matching the old per-row uniqueness).
    assert graph.reference_count == 50 * 8
    for edge in graph.edges():
        assert edge.source_count == 8
        assert sorted(s.source_url for s in edge.sources) == [
            f"http://src/{n}" for n in range(8)
        ]

def make_run(store):
    return store.create_run("http://127.0.0.1:5001/", {"seed": "x"})

def add_check(store, run_id, url, ok=True, status=200):
    store.insert_check(run_id, {
        "url": url, "final_url": url, "status_code": status,
        "redirect_chain": [], "anchor_ok": True, "ok": ok,
        "error": None if ok else "gone",
        "checked_at": "2026-01-01T00:00:00+00:00"})

def test_save_link_graph_writes_one_row_per_edge(store):
    run_id = make_run(store)
    graph = build_graph([
        (f"http://s/{n}", link("http://t/shared")) for n in range(5)
    ] + [("http://s/0", link("http://t/other"))])
    store.save_link_graph(run_id, graph)
    rows = store.get_links(run_id)
    assert len(rows) == 2
    shared = next(row for row in rows if row["target_url"] == "http://t/shared")
    assert shared["source_url"] == "http://s/0"
    assert shared["sources"] == 5
    assert [s["source_url"] for s in shared["source_list"]] == [
        f"http://s/{n}" for n in range(5)
    ]
    assert json.loads(shared["sources_json"])[0]["raw_href"] == "http://t/shared"
    assert store.inlink_counts(run_id) == {"http://t/shared": 5, "http://t/other": 1}
    assert not store.write_errors

def test_failed_edge_does_not_abort_remaining_batch(store):
    run_id = make_run(store)
    # Pre-insert a row that collides with the first edge's representative.
    store.insert_link(run_id, "http://s/0", {
        "target_url": "http://t/shared", "raw_href": "http://t/shared",
        "kind": "page", "scope": "internal", "line_no": 1, "anchor": ""})
    graph = build_graph([
        ("http://s/0", link("http://t/shared")),
        ("http://s/1", link("http://t/shared")),
        ("http://s/0", link("http://t/other")),
    ])
    store.save_link_graph(run_id, graph)
    assert store.write_errors  # the colliding edge failed alone
    targets = {row["target_url"] for row in store.get_links(run_id)}
    assert targets == {"http://t/shared", "http://t/other"}

def test_broken_identities_expand_all_sources(store):
    run_id = make_run(store)
    graph = build_graph([
        (f"http://s/{n}", link("http://t/broken")) for n in range(3)
    ])
    store.save_link_graph(run_id, graph)
    add_check(store, run_id, "http://t/broken", ok=False, status=404)
    identities = store.broken_identities(run_id)
    assert [row["source_url"] for row in identities] == [
        "http://s/0", "http://s/1", "http://s/2"]
    assert all(row["target_url"] == "http://t/broken" for row in identities)

LEGACY_SCHEMA = (
    SCHEMA
    .replace("    sources INTEGER NOT NULL DEFAULT 1,\n", "")
    .replace("    sources_json TEXT NOT NULL DEFAULT '[]',\n", "")
)

def make_legacy_db(path):
    """Create a database exactly as the pre-graph schema did."""
    assert "sources" not in LEGACY_SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO runs(id, seed, config_json, started_at, status, stats_json) "
        "VALUES (1, 'http://site/', '{}', '2026-01-01T00:00:00+00:00', 'completed', '{}')")
    conn.execute(
        "INSERT INTO pages(run_id, url, depth, status_code, content_hash, "
        "duration_ms, error) VALUES (1, 'http://site/', 0, 200, 'h', 5, NULL)")
    for n in range(3):  # same broken target linked from three pages
        conn.execute(
            "INSERT INTO links(run_id, source_url, target_url, raw_href, kind, "
            "scope, line_no, anchor) VALUES (1, ?, 'http://site/gone', '/gone', "
            "'page', 'internal', ?, '')",
            (f"http://site/p{n}", n + 2))
    conn.execute(
        "INSERT INTO checks(run_id, url, final_url, status_code, "
        "redirect_chain_json, anchor_ok, ok, error, checked_at) "
        "VALUES (1, 'http://site/gone', 'http://site/gone', 404, '[]', 1, 0, "
        "NULL, '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

def test_legacy_database_migrates_and_stays_readable(tmp_path):
    db_path = tmp_path / "legacy.db"
    make_legacy_db(db_path)
    store = Store(db_path)
    try:
        columns = {
            row[1] for row in store.conn.execute("PRAGMA table_info(links)")
        }
        assert {"sources", "sources_json"} <= columns
        rows = store.get_links(1)
        assert len(rows) == 3  # historical rows are not collapsed
        assert all(row["sources"] == 1 for row in rows)
        assert rows[0]["source_list"] == [{
            "source_url": rows[0]["source_url"], "raw_href": "/gone",
            "line_no": rows[0]["line_no"], "anchor": ""}]
        # Inlink counts and broken stats still computable from old data.
        assert store.inlink_counts(1) == {"http://site/gone": 3}
        identities = store.broken_identities(1)
        assert [row["source_url"] for row in identities] == [
            "http://site/p0", "http://site/p1", "http://site/p2"]
    finally:
        store.close()

def test_legacy_database_report_output_is_unchanged(tmp_path):
    db_path = tmp_path / "legacy.db"
    make_legacy_db(db_path)
    store = Store(db_path)
    try:
        data = build_report(store, 1)
    finally:
        store.close()
    assert data["summary"]["links"] == 3
    assert data["summary"]["link_refs"] == 3
    assert data["summary"]["broken"] == 3
    assert data["inlinks"] == [{"target_url": "http://site/gone", "sources": 3}]
    expected = [
        {"source_url": f"http://site/p{n}", "target_url": "http://site/gone",
         "raw_href": "/gone", "kind": "page", "scope": "internal",
         "line_no": n + 2, "status_code": 404, "error": None,
         "final_url": "http://site/gone"}
        for n in range(3)
    ]
    for got, want in zip(data["broken"], expected):
        assert all(got[key] == value for key, value in want.items())
        assert got["sources"] == 1  # new field carries a safe default

def test_report_expands_deduped_sources(tmp_path):
    store = Store(tmp_path / "store.db")
    try:
        run_id = make_run(store)
        graph = build_graph([
            (f"http://s/{n}", link("http://t/broken", line_no=n + 10))
            for n in range(3)
        ] + [("http://s/0", link("http://t/ok"))])
        store.save_link_graph(run_id, graph)
        add_check(store, run_id, "http://t/broken", ok=False, status=404)
        add_check(store, run_id, "http://t/ok")
        store.finish_run(run_id, "completed", {})
        data = build_report(store, run_id)
    finally:
        store.close()
    assert data["summary"]["links"] == 2
    assert data["summary"]["link_refs"] == 4
    assert data["summary"]["broken"] == 3
    assert [item["source_url"] for item in data["broken"]] == [
        "http://s/0", "http://s/1", "http://s/2"]
    assert [item["line_no"] for item in data["broken"]] == [10, 11, 12]
    assert all(item["sources"] == 3 for item in data["broken"])
    assert data["inlinks"][0] == {"target_url": "http://t/broken", "sources": 3}

def test_insert_link_defaults_keep_old_callers_working(store):
    run_id = make_run(store)
    assert store.insert_link(run_id, "http://s/0", link("http://t/x"))
    row = store.get_links(run_id)[0]
    assert row["sources"] == 1 and row["source_list"][0]["source_url"] == "http://s/0"

def test_report_requires_existing_run(store):
    with pytest.raises(ValueError):
        build_report(store, 4242)
