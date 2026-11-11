"""Link, asset, and anchor extraction built on :mod:`html.parser`."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .urlnorm import is_http_url, is_internal, normalize_url, strip_fragment, with_fragment

LINK_KINDS = {
    "a": ("href", "page"),
    "img": ("src", "asset"),
    "script": ("src", "asset"),
    "link": ("href", "asset"),
    "form": ("action", "form"),
}

@dataclass(frozen=True)
class ExtractedLink:
    """A deduplicated outgoing reference from one HTML document."""

    target_url: str
    raw_href: str
    kind: str
    scope: str
    line_no: int
    anchor: str = ""

@dataclass
class ExtractResult:
    """All references and fragment anchors found in one document."""

    links: list[ExtractedLink] = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)
    ignored_counts: dict[str, int] = field(default_factory=dict)

def classify_scope(url: str, base_url: str, allow_subdomains: bool = False) -> str:
    """Classify a resolved URL as internal/external/other protocol family."""
    raw = url.strip()
    if raw.startswith("#"):
        return "anchor"
    scheme = urlsplit(raw).scheme.lower()
    if scheme in {"mailto", "tel"}:
        return scheme
    if scheme and scheme not in {"http", "https"}:
        return "other"
    if not raw:
        return "anchor"
    if is_http_url(raw):
        return "internal" if is_internal(raw, base_url, allow_subdomains) else "external"
    return "other"

class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: set[str] = set()
        self.records: list[tuple[str, str, str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        if "id" in attr_map and attr_map["id"]:
            self.anchors.add(attr_map["id"])
        if tag.lower() == "a" and attr_map.get("name"):
            self.anchors.add(attr_map["name"])
        spec = LINK_KINDS.get(tag.lower())
        if spec is None:
            return
        attribute, kind = spec
        if attribute not in attr_map:
            return
        self.records.append((tag.lower(), kind, attr_map[attribute], self.getpos()[0]))

    handle_startendtag = handle_starttag

def _resolve(raw: str, base_url: str, drop_params: tuple[str, ...]) -> str:
    stripped = raw.strip()
    if not stripped:
        return strip_fragment(normalize_url(base_url, drop_params=drop_params))
    if stripped.startswith("#"):
        base = strip_fragment(normalize_url(base_url, drop_params=drop_params))
        return with_fragment(base, stripped[1:])
    try:
        return normalize_url(stripped, base=base_url, drop_params=drop_params, keep_fragment=True)
    except ValueError:
        return urljoin(base_url, stripped)

def extract_links(
    html: str,
    base_url: str,
    drop_params: list[str] | tuple[str, ...] | None = None,
    allow_subdomains: bool = False,
    scope_base: str | None = None,
) -> ExtractResult:
    """Extract page/asset/form references and collect every fragment anchor.

    The same target+kind pair is retained only once per document; the first
    source line wins. Non-HTTP schemes are counted and classified but never
    normalised as HTTP URLs.
    """
    parser = _LinkParser()
    parser.feed(html or "")
    parser.close()
    drops = tuple(drop_params or ())
    scope_url = scope_base or base_url
    seen: set[tuple[str, str]] = set()
    result = ExtractResult(anchors=set(parser.anchors))
    for _tag, kind, raw, line_no in parser.records:
        stripped = raw.strip()
        if not stripped:
            # An empty href/action means the current document.
            target = strip_fragment(normalize_url(base_url, drop_params=drops))
            scope = "anchor"
            anchor = ""
        elif stripped.startswith("#"):
            target = _resolve(stripped, base_url, drops)
            scope = "anchor"
            anchor = urlsplit(target).fragment
        elif _is_non_http(stripped):
            scheme = urlsplit(stripped).scheme.lower()
            scope = classify_scope(stripped, scope_url, allow_subdomains)
            decoded = urljoin(base_url, stripped)
            anchor = urlsplit(decoded).fragment
            target = decoded
            if scope == "other":
                result.ignored_counts[scheme or "unknown"] = (
                    result.ignored_counts.get(scheme or "unknown", 0) + 1
                )
        else:
            target = _resolve(stripped, base_url, drops)
            parsed = urlsplit(target)
            anchor = parsed.fragment
            scope = (
                "internal"
                if is_http_url(target) and is_internal(target, scope_url, allow_subdomains)
                else "external"
            )
        key = (target, kind)
        if key in seen:
            continue
        seen.add(key)
        result.links.append(
            ExtractedLink(
                target_url=target,
                raw_href=stripped,
                kind=kind,
                scope=scope,
                line_no=line_no,
                anchor=anchor,
            )
        )
    return result

def _is_non_http(raw: str) -> bool:
    scheme = urlsplit(raw).scheme.lower()
    return bool(scheme) and scheme not in {"http", "https"} and not raw.startswith("#")

def page_links(result: ExtractResult) -> list[ExtractedLink]:
    """Return only navigational links that BFS may enqueue."""
    return [
        link
        for link in result.links
        if link.kind == "page" and link.scope in {"internal", "external"}
    ]
