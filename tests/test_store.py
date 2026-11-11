"""SQLite schema, uniqueness, rollback, and baseline tests."""

import pytest

from linkpatrol.store import BaselineExistsError

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
