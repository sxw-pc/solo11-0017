"""Orphan page, unlisted page, and dead-end discovery."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Iterable

from .crawler import PageRecord
from .urlnorm import in_path_scope, normalize_url, strip_fragment

def load_url_list(path: str | Path) -> list[str]:
    """Load one URL per line, ignoring blanks and ``#`` comments."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"URL list not found: {source}")
    urls: list[str] = []
    seen: set[str] = set()
    for line in source.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        normalised = normalize_url(candidate)
        if normalised not in seen:
            seen.add(normalised)
            urls.append(normalised)
    return urls

def _view(page) -> tuple[str, bool, bool, bool]:
    """Return (url, is_2xx_html, has_internal_link, is_html)."""
    if isinstance(page, PageRecord):
        url = page.url
        is_ok = page.is_ok
        is_html = page.is_html
        internal = any(link.scope == "internal" for link in page.links)
    else:
        url = page.get("url", "")
        status = page.get("status_code")
        is_ok = not page.get("error") and status is not None and 200 <= status < 300
        content_type = page.get("content_type", "text/html")
        is_html = content_type in {"text/html", "application/xhtml+xml"}
        links = page.get("links", [])
        internal = any(
            (getattr(item, "scope", None) or item.get("scope")) == "internal"
            for item in links
        )
    normalised = strip_fragment(normalize_url(url)) if url else ""
    return normalised, is_ok and is_html, internal, is_ok and is_html

def compute_findings(listed_urls: Iterable[str], pages: Iterable) -> dict:
    """Compare a listed URL set with reachable pages from one crawl.

    The returned mapping contains ``orphan`` (listed but unreachable),
    ``unlisted`` (reachable but absent from the list), and ``dead_end``
    (reachable HTML pages with no internal outgoing references).
    """
    listed: set[str] = set()
    for url in listed_urls:
        if url and str(url).strip():
            listed.add(strip_fragment(normalize_url(str(url).strip())))
    reachable: set[str] = set()
    dead_ends: list[str] = []
    for page in pages:
        url, reachable_html, has_internal, _is_html = _view(page)
        if not url:
            continue
        if reachable_html:
            reachable.add(url)
            if not has_internal:
                dead_ends.append(url)
    return {
        "orphan": sorted(listed - reachable),
        "unlisted": sorted(reachable - listed),
        "dead_end": sorted(set(dead_ends)),
    }
