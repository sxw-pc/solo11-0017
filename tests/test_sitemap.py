"""Sitemap generation, parsing, recursion, and limits."""

import pytest

from linkpatrol import sitemap as sitemap_module
from linkpatrol.sitemap import SitemapError, generate_sitemap, parse_sitemap, parse_sitemap_file

URL_LIST = ["http://127.0.0.1:5001/", "http://127.0.0.1:5001/about",
            "http://127.0.0.1:5001/docs"]

def test_roundtrip_dedupe_sort_and_file(tmp_path):
    xml = generate_sitemap(URL_LIST[::-1] + [URL_LIST[0]], today="2026-02-03")
    assert parse_sitemap(xml) == sorted(URL_LIST) and "2026-02-03" in xml
    target = tmp_path / "out" / "sitemap.xml"
    generate_sitemap(URL_LIST, target)
    assert target.is_file() and parse_sitemap_file(target) == URL_LIST

def test_over_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(sitemap_module, "MAX_URLS", 2)
    with pytest.raises(SitemapError):
        generate_sitemap(URL_LIST)

def test_sitemapindex_is_followed():
    index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             '<sitemap><loc>http://127.0.0.1:5001/a.xml</loc></sitemap>'
             '<sitemap><loc>http://127.0.0.1:5001/b.xml</loc></sitemap>'
             '</sitemapindex>')
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    children = {
        "http://127.0.0.1:5001/a.xml": f'<urlset xmlns="{ns}"><url><loc>http://127.0.0.1:5001/a</loc></url></urlset>',
        "http://127.0.0.1:5001/b.xml": f'<urlset xmlns="{ns}"><url><loc>http://127.0.0.1:5001/b</loc></url></urlset>',
    }
    assert parse_sitemap(index, fetch=children.__getitem__) == [
        "http://127.0.0.1:5001/a", "http://127.0.0.1:5001/b"]

def test_index_without_fetch_invalid_xml_and_bad_root():
    index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             '<sitemap><loc>http://127.0.0.1:5001/a.xml</loc></sitemap></sitemapindex>')
    with pytest.raises(SitemapError, match="fetch"):
        parse_sitemap(index)
    with pytest.raises(SitemapError, match="invalid sitemap XML"):
        parse_sitemap("<not-closed")
    with pytest.raises(SitemapError, match="root"):
        parse_sitemap("<html></html>")
