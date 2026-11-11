"""URL normalisation, fragment helpers, and in-scope host decisions."""

from __future__ import annotations

import fnmatch
import posixpath
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}

def is_http_url(url: str) -> bool:
    """Return True only for absolute HTTP(S) URLs."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)

def origin(url: str) -> str:
    """Return the lower-cased scheme://host[:port] origin of a URL."""
    parsed = urlsplit(url)
    return f"{parsed.scheme.lower()}://{_netloc(parsed)}"

def _netloc(parsed) -> str:
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port
    if port is None or DEFAULT_PORTS.get(parsed.scheme.lower()) == port:
        return host
    return f"{host}:{port}"

def _normal_path(path: str) -> str:
    if not path:
        return "/"
    normalised = posixpath.normpath(path)
    if path.startswith("/") and not normalised.startswith("/"):
        normalised = "/" + normalised
    if normalised in {"", "."}:
        normalised = "/"
    if normalised != "/" and normalised.endswith("/"):
        normalised = normalised.rstrip("/")
    return normalised

def _kept_query(query: str, drop_params: list[str] | tuple[str, ...]) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [
        (key, value)
        for key, value in pairs
        if not any(fnmatch.fnmatchcase(key, pattern) for pattern in drop_params)
    ]
    # Sorting by key then value gives a stable, idempotent representation.
    kept.sort(key=lambda pair: (pair[0], pair[1]))
    return urlencode(kept, doseq=True)

def normalize_url(
    url: str,
    base: str | None = None,
    drop_params: list[str] | tuple[str, ...] | None = None,
    keep_fragment: bool = False,
) -> str:
    """Resolve and canonicalise an HTTP(S) URL.

    Scheme/host are lower-cased, default ports and dot segments are removed,
    non-root trailing slashes are removed, query parameters are sorted and
    optionally filtered, and fragments are discarded unless ``keep_fragment``
    is true.
    """
    raw = url.strip()
    absolute = urljoin(base, raw) if base else raw
    parsed = urlsplit(absolute)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        return absolute
    if parsed.username or parsed.password:
        # Credentials are not supported anywhere in this project.
        raise ValueError("URLs with embedded credentials are not supported")
    netloc = _netloc(parsed)
    path = _normal_path(parsed.path)
    query = _kept_query(parsed.query, list(drop_params or ()))
    fragment = parsed.fragment if keep_fragment else ""
    return urlunsplit((scheme, netloc, path, query, fragment))

def strip_fragment(url: str) -> str:
    """Return the URL without its fragment."""
    return url.split("#", 1)[0]

def get_fragment(url: str) -> str:
    """Return the fragment without the leading ``#``, or an empty string."""
    parsed = urlsplit(url)
    return parsed.fragment

def with_fragment(url: str, fragment: str) -> str:
    """Attach a fragment to a URL that has none."""
    base = strip_fragment(url)
    return f"{base}#{fragment}" if fragment else base

def path_of(url: str) -> str:
    """Return the path component of a URL."""
    return urlsplit(url).path or "/"

def host_key(url: str) -> str:
    """Return hostname plus non-default port, the unit used for scoping."""
    parsed = urlsplit(url)
    return _netloc(parsed)

def hostname_of(url: str) -> str:
    """Return the lower-cased hostname without a port."""
    return (urlsplit(url).hostname or "").lower()

def same_origin(first: str, second: str) -> bool:
    """Compare two URLs by scheme, host, and effective port."""
    return origin(first) == origin(second)

def is_internal(url: str, seed_url: str, allow_subdomains: bool = False) -> bool:
    """Return whether ``url`` belongs to the seed host scope.

    Subdomains are accepted only when ``allow_subdomains`` is true. The port
    and scheme must always match, so a second local server on another port is
    correctly classified as external.
    """
    if not is_http_url(url):
        return False
    candidate = urlsplit(url)
    seed = urlsplit(seed_url)
    candidate_port = candidate.port or DEFAULT_PORTS.get(candidate.scheme.lower())
    seed_port = seed.port or DEFAULT_PORTS.get(seed.scheme.lower())
    if candidate.scheme.lower() != seed.scheme.lower() or candidate_port != seed_port:
        return False
    candidate_host = (candidate.hostname or "").lower()
    seed_host = (seed.hostname or "").lower()
    if candidate_host == seed_host:
        return True
    if allow_subdomains and candidate_host.endswith("." + seed_host):
        return True
    return False

def in_path_scope(
    url: str,
    include: list[str] | tuple[str, ...],
    exclude: list[str] | tuple[str, ...],
) -> bool:
    """Apply include/exclude regular expressions to a URL path."""
    import re

    path = path_of(url)
    if exclude and any(re.search(pattern, path) for pattern in exclude):
        return False
    if include and not any(re.search(pattern, path) for pattern in include):
        return False
    return True
