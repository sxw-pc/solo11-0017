"""Orphan, unlisted, and dead-end classification."""

from linkpatrol.crawler import PageRecord
from linkpatrol.extractor import ExtractedLink
from linkpatrol.orphans import compute_findings, load_url_list

ROOT = "http://127.0.0.1:5001/"
ABOUT = "http://127.0.0.1:5001/about"
DEAD = "http://127.0.0.1:5001/dead"
ORPHAN = "http://127.0.0.1:5001/orphan"

def page(url, links=None):
    return PageRecord(url=url, depth=0, status_code=200, content_hash="x",
                      duration_ms=1, content_type="text/html", links=links or [])

def internal_link(target):
    return ExtractedLink(target_url=target, raw_href=target, kind="page",
                         scope="internal", line_no=1)

def test_three_finding_classes_and_normalisation():
    pages = [page(ROOT, [internal_link(ABOUT)]), page(ABOUT, [internal_link(ROOT)]),
             page(DEAD)]
    findings = compute_findings(
        [ROOT, ORPHAN, DEAD + "/", "http://127.0.0.1:5001/#top"], pages)
    assert findings["orphan"] == [ORPHAN]
    assert findings["unlisted"] == [ABOUT]
    assert findings["dead_end"] == [DEAD]

def test_empty_list_and_dict_pages():
    assert compute_findings([], [page(ROOT, [internal_link(ABOUT)])])["unlisted"] == [ROOT]
    findings = compute_findings([ROOT], [{"url": ROOT, "status_code": 200, "links": []}])
    assert findings["dead_end"] == [ROOT]

def test_load_url_list_ignores_comments(tmp_path):
    source = tmp_path / "urls.txt"
    source.write_text("# comment\nhttp://127.0.0.1:5001/\n\nhttp://127.0.0.1:5001/a/\n")
    assert load_url_list(source) == ["http://127.0.0.1:5001/", "http://127.0.0.1:5001/a"]
