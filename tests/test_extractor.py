"""HTML extraction tests for all supported tags and classifications."""

from linkpatrol.extractor import classify_scope, extract_links, page_links

BASE = "http://127.0.0.1:5001/dir/page"
HTML = """<!doctype html>
<html><body>
<h1>Links</h1>
<a href="/one">one</a>
<img src="pic.png">
<script src="/app.js"></script>
<link rel="stylesheet" href="/style.css">
<form action="/search"></form>
<div id="main"></div>
<a name="legacy">legacy</a>
<a href="#main">main anchor</a>
<a href="mailto:test@127.0.0.1">mail</a>
<a href="tel:+10000000000">phone</a>
<a href="javascript:void(0)">script</a>
<a href="https://other.example/x">external</a>
<a href="/one">duplicate</a>
</body></html>"""

def test_all_tag_types_and_relative_assets():
    result = extract_links(HTML, BASE)
    kinds = {(link.target_url, link.kind) for link in result.links}
    assert ("http://127.0.0.1:5001/one", "page") in kinds
    assert ("http://127.0.0.1:5001/dir/pic.png", "asset") in kinds
    assert ("http://127.0.0.1:5001/app.js", "asset") in kinds
    assert ("http://127.0.0.1:5001/style.css", "asset") in kinds
    assert ("http://127.0.0.1:5001/search", "form") in kinds
    image = next(link for link in result.links if link.raw_href == "pic.png")
    assert image.target_url == "http://127.0.0.1:5001/dir/pic.png"

def test_line_numbers_anchors_and_deduplication():
    result = extract_links(HTML, BASE)
    one = next(link for link in result.links if link.target_url.endswith("/one"))
    image = next(link for link in result.links if link.kind == "asset")
    assert one.line_no == 4 and image.line_no == 5
    assert result.anchors == {"main", "legacy"}
    assert len([link for link in result.links if link.target_url.endswith("/one")]) == 1

def test_scope_classification_and_ignored_counts():
    result = extract_links(HTML, BASE)
    scopes = {link.raw_href: link.scope for link in result.links}
    assert scopes["#main"] == "anchor"
    assert scopes["mailto:test@127.0.0.1"] == "mailto"
    assert scopes["tel:+10000000000"] == "tel"
    assert scopes["javascript:void(0)"] == "other"
    assert scopes["https://other.example/x"] == "external"
    assert scopes["/one"] == "internal"
    assert result.ignored_counts == {"javascript": 1}

def test_pure_fragment_and_empty_href():
    result = extract_links(HTML, BASE)
    anchor = next(link for link in result.links if link.raw_href == "#main")
    assert anchor.target_url == "http://127.0.0.1:5001/dir/page#main"
    assert anchor.anchor == "main"
    empty = extract_links('<a href="">self</a>', BASE).links[0]
    assert empty.target_url == BASE and empty.scope == "anchor"


def test_classification_helpers_and_credential_fallback():
    assert classify_scope("#part", BASE) == "anchor"
    assert classify_scope("", BASE) == "anchor"
    assert classify_scope("mailto:a@127.0.0.1", BASE) == "mailto"
    assert classify_scope("ftp://127.0.0.1/file", BASE) == "other"
    assert classify_scope("https://example.org/x", BASE) == "external"
    result = extract_links('<a href="http://user:pass@example.org/x">x</a>', BASE)
    assert result.links[0].scope == "external"
    extracted = extract_links('<a href="/a">a</a><img src="/x.png">', BASE)
    assert [link.kind for link in page_links(extracted)] == ["page"]
