"""Link status, redirect, and fragment-anchor verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import unquote

from .crawler import CrawlResult, PageRecord
from .extractor import ExtractedLink, extract_links
from .fetcher import FetchResult, Fetcher
from .urlnorm import is_internal, normalize_url, strip_fragment

@dataclass
class CheckRecord:
    """One unique target URL and its verification outcome."""

    url: str
    final_url: str
    status_code: int | None
    redirect_chain: list[dict]
    anchor_ok: bool
    ok: bool
    error: str | None
    checked_at: str
    kind: str = "page"
    scope: str = "internal"
    warning: bool = False

    @property
    def verdict(self) -> str:
        if self.ok and self.warning:
            return "warning"
        return "ok" if self.ok else "broken"

@dataclass
class CheckReport:
    """All checks from one run keyed by the exact link target."""

    checks: dict[str, CheckRecord] = field(default_factory=dict)
    skipped: int = 0
    requests: int = 0

    @property
    def broken(self) -> list[CheckRecord]:
        return [record for record in self.checks.values() if not record.ok]

    @property
    def warnings(self) -> list[CheckRecord]:
        return [record for record in self.checks.values() if record.warning]

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

class _Probe:
    def __init__(self, result: FetchResult, page: PageRecord | None = None) -> None:
        self.result = result
        self.page = page
        self.anchors: set[str] | None = page.anchors if page and page.is_ok and page.is_html else None

    @property
    def status(self) -> int | None:
        return self.page.status_code if self.page else self.result.status_code

    @property
    def final_url(self) -> str:
        return self.page.final_url if self.page else (self.result.final_url or self.result.requested_url)

    @property
    def chain(self) -> list[dict]:
        return self.page.redirect_chain if self.page else self.result.redirect_chain

    @property
    def error(self) -> str | None:
        return self.page.error if self.page else self.result.error

    @property
    def body_text(self) -> str:
        return self.page.html if self.page else self.result.text

    @property
    def is_html_ok(self) -> bool:
        if self.page:
            return self.page.is_ok and self.page.is_html
        return self.result.is_ok and self.result.is_html

class Checker:
    """Check unique links while reusing crawled in-scope pages."""

    def __init__(self, config: dict, crawl: CrawlResult, fetcher: Fetcher | None = None) -> None:
        self.config = config
        self.crawl = crawl
        limits = config["limits"]
        self.seed = config["seed"]
        self.allow_subdomains = config["scope"]["allow_subdomains"]
        self.check_external = config["external"]["check"]
        self.fetcher = fetcher or Fetcher(
            timeout_s=limits["timeout_s"],
            retries=limits["retries"],
            user_agent=config["robots"]["user_agent"],
            max_redirects=limits["max_redirects"],
        )
        self._base_cache: dict[str, _Probe] = {}

    def run(self) -> CheckReport:
        report = CheckReport()
        seen: set[str] = set()
        for link in self._candidates():
            if link.target_url in seen:
                continue
            seen.add(link.target_url)
            if not self._should_check(link):
                report.skipped += 1
                continue
            report.checks[link.target_url] = self._check_link(link)
        report.requests = len(self._base_cache)
        return report

    def _candidates(self) -> Iterable:
        """Unique check candidates from the crawl's link graph.

        The graph already deduplicates ``(target_url, kind)`` across source
        pages, so each target is probed at most once.  Crawl results built
        without a graph (older callers, hand-built fixtures) fall back to
        scanning page links directly.
        """
        graph = getattr(self.crawl, "link_graph", None)
        if graph is not None and len(graph):
            return graph.edges()
        return (link for _source, link in self.crawl.iter_links())

    def _should_check(self, link: ExtractedLink) -> bool:
        if link.scope in {"mailto", "tel", "other"}:
            return False
        if link.scope == "external" and not self.check_external:
            return False
        return True

    def _check_link(self, link: ExtractedLink) -> CheckRecord:
        base = strip_fragment(link.target_url)
        internal = is_internal(base, self.seed, self.allow_subdomains)
        probe = self._probe(base, need_anchors=bool(link.anchor))
        anchor_ok = True
        error = probe.error
        if link.anchor and internal and probe.is_html_ok:
            anchor_ok = self._anchor_present(probe, link.anchor)
            if not anchor_ok:
                error = "anchor_missing"
        status = probe.status
        ok = error is None and status is not None and 200 <= status < 300 and anchor_ok
        warning = bool(ok and len(probe.chain) >= 3)
        if warning and not error:
            error = "long_redirect_chain"
        return CheckRecord(
            url=link.target_url,
            final_url=probe.final_url,
            status_code=status,
            redirect_chain=list(probe.chain),
            anchor_ok=anchor_ok,
            ok=ok,
            error=error,
            checked_at=_now(),
            kind=link.kind,
            scope=link.scope,
            warning=warning,
        )

    def _probe(self, base: str, need_anchors: bool = False) -> _Probe:
        if base in self._base_cache:
            cached = self._base_cache[base]
            if need_anchors and cached.anchors is None and cached.status and 200 <= cached.status < 300:
                refreshed = self._probe_network(base, force_get=True)
                self._base_cache[base] = refreshed
                return refreshed
            return cached
        page = self.crawl.pages.get(base)
        if page is not None:
            probe = _Probe(
                FetchResult(
                    requested_url=base,
                    final_url=page.final_url,
                    status_code=page.status_code,
                    body=page.html.encode("utf-8", errors="replace"),
                    elapsed_ms=page.duration_ms,
                    redirect_chain=page.redirect_chain,
                    error=page.error,
                ),
                page=page,
            )
            self._base_cache[base] = probe
            return probe
        probe = self._probe_network(base, force_get=need_anchors)
        self._base_cache[base] = probe
        return probe

    def _probe_network(self, base: str, force_get: bool = False) -> _Probe:
        if force_get:
            result = self.fetcher.fetch(base, method="GET", max_body_bytes=64 * 1024)
            return _Probe(result)
        head = self.fetcher.fetch(base, method="HEAD")
        if head.status_code in {405, 501}:
            fallback = self.fetcher.fetch(base, method="GET", max_body_bytes=64 * 1024)
            result = fallback
        else:
            result = head
        if result.is_ok and result.is_html and force_get is False:
            # A HEAD response has no document to validate fragments against.
            return _Probe(result)
        return _Probe(result)

    def _anchor_present(self, probe: _Probe, fragment: str) -> bool:
        if probe.anchors is None:
            text = probe.body_text
            if not text:
                return True
            probe.anchors = extract_links(
                text, probe.final_url, allow_subdomains=self.allow_subdomains
            ).anchors
        return unquote(fragment) in probe.anchors

def check_links(config: dict, crawl: CrawlResult, fetcher: Fetcher | None = None) -> CheckReport:
    """Convenience wrapper used by the CLI and tests."""
    return Checker(config, crawl, fetcher=fetcher).run()
