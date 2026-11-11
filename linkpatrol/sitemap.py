"""Sitemap 0.9 generation and sitemapindex parsing."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_URLS = 50000

class SitemapError(ValueError):
    """Raised for invalid sitemap input or an over-limit generation request."""

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

def _lastmod(value) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10]

def generate_sitemap(
    entries,
    output_path: str | Path | None = None,
    max_urls: int | None = None,
    today: str | date | None = None,
) -> str:
    """Generate a deterministic urlset XML document.

    ``entries`` may contain URL strings or mappings with ``url`` and optional
    ``lastmod`` keys. More than ``max_urls`` (default 50,000) is rejected.
    """
    by_url: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, str):
            url, lastmod = entry, None
        else:
            url = entry.get("url") or entry.get("loc")
            lastmod = entry.get("lastmod")
        if not url:
            continue
        by_url[str(url)] = _lastmod(lastmod if lastmod is not None else today)
    limit = MAX_URLS if max_urls is None else max_urls
    if len(by_url) > limit:
        raise SitemapError(
            f"sitemap contains {len(by_url)} URLs, exceeding the limit of {limit}"
        )
    ET.register_namespace("", SITEMAP_NS)
    root = ET.Element(f"{{{SITEMAP_NS}}}urlset")
    for url in sorted(by_url):
        node = ET.SubElement(root, f"{{{SITEMAP_NS}}}url")
        ET.SubElement(node, f"{{{SITEMAP_NS}}}loc").text = url
        ET.SubElement(node, f"{{{SITEMAP_NS}}}lastmod").text = by_url[url]
    xml = ET.tostring(root, encoding="unicode", xml_declaration=True)
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(xml + "\n", encoding="utf-8")
    return xml

def _parse_xml(xml_text: str) -> ET.Element:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SitemapError(f"invalid sitemap XML: {exc}") from exc
    root_name = _local(root.tag)
    if root_name not in {"urlset", "sitemapindex"}:
        raise SitemapError(f"unexpected sitemap root element: {root_name}")
    return root

def parse_sitemap(
    xml_text: str,
    fetch=None,
    base_url: str = "",
    depth: int = 0,
    max_depth: int = 2,
) -> list[str]:
    """Parse a sitemap or sitemapindex into a deduplicated URL list.

    Child sitemaps in an index are loaded through ``fetch`` (a callable taking
    a URL and returning XML text). At most ``max_depth`` index levels are
    followed. A missing fetch callable produces an explicit error.
    """
    root = _parse_xml(xml_text)
    locs: list[str] = []
    for node in root:
        if _local(node.tag) == "url":
            for child in node:
                if _local(child.tag) == "loc" and child.text:
                    locs.append(urljoin(base_url, child.text.strip()))
        elif _local(node.tag) == "sitemap":
            for child in node:
                if _local(child.tag) == "loc" and child.text:
                    locs.append(urljoin(base_url, child.text.strip()))
    if _local(root.tag) == "urlset":
        return _dedupe(locs)
    if fetch is None:
        raise SitemapError("cannot follow sitemapindex without a fetch callable")
    if depth >= max_depth:
        raise SitemapError(f"sitemapindex nesting exceeds max_depth={max_depth}")
    parsed: list[str] = []
    for child_url in locs:
        try:
            child_text = fetch(child_url)
        except Exception as exc:
            raise SitemapError(f"failed to fetch child sitemap {child_url}: {exc}") from exc
        parsed.extend(parse_sitemap(child_text, fetch, child_url, depth + 1, max_depth))
    return _dedupe(parsed)

def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered

def parse_sitemap_file(path: str | Path, fetch=None) -> list[str]:
    """Read and parse a local sitemap file."""
    sitemap_path = Path(path)
    if not sitemap_path.is_file():
        raise SitemapError(f"sitemap file not found: {sitemap_path}")
    try:
        text = sitemap_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SitemapError(f"cannot read sitemap file {sitemap_path}: {exc}") from exc
    return parse_sitemap(text, fetch=fetch, base_url=sitemap_path.as_uri())
