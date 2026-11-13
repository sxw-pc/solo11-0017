"""Bounded breadth-first crawling and page record construction."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable

from .config import ConfigError
from .extractor import ExtractedLink, extract_links
from .fetcher import FetchResult, Fetcher
from .linkgraph import LinkGraph
from .robots import RobotsCache
from .urlnorm import (
    in_path_scope,
    is_http_url,
    is_internal,
    normalize_url,
    path_of,
    strip_fragment,
)

@dataclass
class PageRecord:
    """One fetched page (or failed fetch) in a crawl."""

    url: str
    depth: int
    status_code: int | None
    content_hash: str
    duration_ms: int
    error: str | None = None
    content_type: str = ""
    final_url: str = ""
    redirect_chain: list[dict] = field(default_factory=list)
    html: str = ""
    anchors: set[str] = field(default_factory=set)
    links: list[ExtractedLink] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300

    @property
    def is_html(self) -> bool:
        return self.content_type in {"text/html", "application/xhtml+xml"}

@dataclass
class CrawlStats:
    discovered: int = 0
    fetched: int = 0
    skipped_by_robots: int = 0
    skipped_by_limits: int = 0
    excluded_by_scope: int = 0
    non_html: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "fetched": self.fetched,
            "skipped_by_robots": self.skipped_by_robots,
            "skipped_by_limits": self.skipped_by_limits,
            "excluded_by_scope": self.excluded_by_scope,
            "non_html": self.non_html,
            "errors": self.errors,
        }

@dataclass
class CrawlResult:
    seed: str
    pages: dict[str, PageRecord] = field(default_factory=dict)
    stats: CrawlStats = field(default_factory=CrawlStats)
    link_graph: LinkGraph = field(default_factory=LinkGraph)

    def iter_links(self) -> Iterable[tuple[str, ExtractedLink]]:
        for page in self.pages.values():
            for link in page.links:
                yield page.url, link

class Crawler:
    """Polite, bounded BFS crawler for one site."""

    def __init__(self, config: dict, fetcher: Fetcher | None = None,
                 robots_cache: RobotsCache | None = None) -> None:
        self.config = config
        limits = config["limits"]
        self.seed = normalize_url(config["seed"], drop_params=self.drop_params)
        self.allow_subdomains = bool(config["scope"]["allow_subdomains"])
        self.include = config["scope"]["include"]
        self.exclude = config["scope"]["exclude"]
        self.max_depth = limits["max_depth"]
        self.max_pages = limits["max_pages"]
        self.concurrency = limits["concurrency"]
        self.delay_s = limits["delay_ms"] / 1000.0
        self.fetcher = fetcher or Fetcher(
            timeout_s=limits["timeout_s"],
            retries=limits["retries"],
            user_agent=config["robots"]["user_agent"],
            max_redirects=limits["max_redirects"],
        )
        if not isinstance(self.fetcher, Fetcher):
            raise ConfigError("fetcher must be a Fetcher instance")
        self.robots = robots_cache or RobotsCache(
            self.fetcher,
            respect=config["robots"]["respect"],
            user_agent=config["robots"]["user_agent"],
        )
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0

    @property
    def drop_params(self) -> list[str]:
        return self.config["normalize"]["drop_params"]

    def run(self) -> CrawlResult:
        result = CrawlResult(seed=self.seed)
        if not is_http_url(self.seed):
            raise ConfigError("seed must be an absolute http(s) URL")
        queue: deque[tuple[str, int]] = deque([(self.seed, 0)])
        seen = {self.seed}
        result.stats.discovered = 1
        with self._executor() as executor:
            while queue:
                if len(result.pages) >= self.max_pages:
                    result.stats.skipped_by_limits += len(queue)
                    queue.clear()
                    break
                batch: list[tuple[str, int]] = []
                while queue and len(batch) < self.concurrency:
                    if len(result.pages) + len(batch) >= self.max_pages:
                        result.stats.skipped_by_limits += len(queue)
                        queue.clear()
                        break
                    batch.append(queue.popleft())
                prepared: list[tuple[str, int]] = []
                for url, depth in batch:
                    policy = self.robots.policy_for(url)
                    if not policy.allowed(path_of(url)):
                        result.stats.skipped_by_robots += 1
                        continue
                    prepared.append((url, depth))
                if not prepared:
                    continue
                if self.concurrency > 1:
                    fetched = list(executor.map(self._fetch, [u for u, _ in prepared]))
                else:
                    fetched = [self._fetch(url) for url, _ in prepared]
                for (url, depth), fetch in zip(prepared, fetched):
                    self._record(url, depth, fetch, result, seen, queue)
        return result

    def _executor(self):
        if self.concurrency <= 1:
            return _NullExecutor()
        return ThreadPoolExecutor(max_workers=self.concurrency)

    def _throttle(self) -> None:
        if self.delay_s <= 0:
            return
        with self._throttle_lock:
            wait = self.delay_s - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _fetch(self, url: str) -> FetchResult:
        self._throttle()
        return self.fetcher.fetch(url)

    def _record(self, url: str, depth: int, fetch: FetchResult, result: CrawlResult,
                seen: set[str], queue: deque[tuple[str, int]]) -> None:
        raw_html = ""
        anchors: set[str] = set()
        links: list[ExtractedLink] = []
        content_type = fetch.content_type
        if fetch.is_ok and fetch.is_html:
            raw_html = fetch.text
            extracted = extract_links(
                raw_html,
                fetch.final_url or url,
                drop_params=self.drop_params,
                allow_subdomains=self.allow_subdomains,
                scope_base=self.seed,
            )
            anchors = extracted.anchors
            links = extracted.links
        elif fetch.status_code is not None and fetch.status_code < 300:
            result.stats.non_html += 1
        if fetch.error:
            result.stats.errors += 1
        digest = hashlib.sha256(fetch.body).hexdigest()
        page = PageRecord(
            url=url,
            depth=depth,
            status_code=fetch.status_code,
            content_hash=digest,
            duration_ms=fetch.elapsed_ms,
            error=fetch.error,
            content_type=content_type,
            final_url=fetch.final_url or url,
            redirect_chain=list(fetch.redirect_chain),
            html=raw_html,
            anchors=anchors,
            links=links,
        )
        result.pages[url] = page
        result.stats.fetched = len(result.pages)
        for link in links:
            result.link_graph.add(url, link)
        for link in links:
            if link.kind != "page" or link.scope != "internal":
                continue
            target = strip_fragment(
                normalize_url(link.target_url, drop_params=self.drop_params)
            )
            if target in seen:
                continue
            if not is_internal(target, self.seed, self.allow_subdomains):
                continue
            if not in_path_scope(target, self.include, self.exclude):
                result.stats.excluded_by_scope += 1
                seen.add(target)
                continue
            seen.add(target)
            result.stats.discovered += 1
            if depth + 1 > self.max_depth:
                result.stats.skipped_by_limits += 1
                continue
            queue.append((target, depth + 1))

class _NullExecutor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

def crawl_site(config: dict, fetcher: Fetcher | None = None) -> CrawlResult:
    """Convenience wrapper used by the CLI and tests."""
    return Crawler(config, fetcher=fetcher).run()
