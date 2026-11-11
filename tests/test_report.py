"""JSON/HTML report and read-only Flask server tests."""

import json

import pytest

from linkpatrol.report import build_report, write_reports
from linkpatrol.server import create_app

ROOT = "http://127.0.0.1:5001/"
ABOUT, MISSING = ROOT + "about", ROOT + "missing"
ANCHOR = ABOUT + "#gone"

def add_page(store, run_id, url, status=200, depth=0):
    store.insert_page(run_id, {"url": url, "depth": depth, "status_code": status,
                               "content_hash": "hash", "duration_ms": 3, "error": None})

def add_link_check(store, run_id, target, status=200, ok=True, error=None):
    store.insert_link(run_id, ROOT, {
        "target_url": target, "raw_href": target, "kind": "page",
        "scope": "internal", "line_no": 4,
        "anchor": target.split("#", 1)[1] if "#" in target else ""})
    chain = [{"url": ROOT, "status": 301, "location": target}] if target == ABOUT else []
    store.insert_check(run_id, {
        "url": target, "final_url": target, "status_code": status,
        "redirect_chain": chain, "anchor_ok": "#" not in target, "ok": ok,
        "error": error, "checked_at": "2026-01-01T00:00:00+00:00"})

@pytest.fixture
def populated(store):
    run_id = store.create_run(ROOT, {"seed": ROOT})
    add_page(store, run_id, ROOT)
    add_page(store, run_id, ABOUT)
    add_page(store, run_id, MISSING, status=404)
    add_link_check(store, run_id, ABOUT)
    add_link_check(store, run_id, MISSING, status=404, ok=False)
    add_link_check(store, run_id, ANCHOR, ok=False, error="anchor_missing")
    store.finish_run(run_id, "completed", {"checks": 3, "broken": 2, "findings": {
        "orphan": [ROOT + "orphan"], "unlisted": [ABOUT], "dead_end": [MISSING]}})
    return store, run_id

def test_report_required_sections(populated):
    store, run_id = populated
    data = build_report(store, run_id)
    for key in ("summary", "broken", "redirects", "anchors", "orphans", "dead_ends"):
        assert key in data
    assert data["summary"]["broken"] == 2
    assert data["summary"]["anchor_failures"] == 1
    assert data["orphans"]["orphan"] == [ROOT + "orphan"]
    assert data["dead_ends"] == [MISSING]
    with pytest.raises(ValueError):
        build_report(store, 999)

def test_write_json_and_html(populated, tmp_path):
    store, run_id = populated
    json_path, html_path = write_reports(store, run_id, tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["run"]["id"] == run_id
    html = html_path.read_text(encoding="utf-8")
    assert MISSING in html and "Missing anchors" in html
    assert "<script src=" not in html and "fonts.googleapis.com" not in html
    assert "<style>" in html

def test_flask_server_routes(populated):
    store, run_id = populated
    app = create_app(store.path)
    client = app.test_client()
    assert f"#{run_id}".encode() in client.get("/").data
    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200 and b"/missing" in detail.data
    assert client.get(f"/runs/{run_id}?status=404&kind=page").status_code == 200
    page_id = store.get_pages(run_id)[0]["id"]
    page = client.get(f"/pages/{page_id}")
    assert page.status_code == 200 and ABOUT.encode() in page.data
    assert client.get("/runs/9999").status_code == 404
    assert client.get("/pages/9999").status_code == 404
