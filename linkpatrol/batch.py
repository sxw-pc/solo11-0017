"""Batch inspection: site manifests, global concurrency, and per-site scheduling.

A batch run reads a manifest listing several sites, each backed by an ordinary
single-site ``scan.toml``. Sites run in parallel subject to two independent
limits:

* each site keeps its own ``limits.concurrency`` worker pool and ``delay_ms``
  throttle (see :class:`linkpatrol.crawler.Crawler`);
* a process-wide :class:`GlobalLimiter` semaphore bounds the *total* number of
  HTTP requests in flight across every site, so neither the local machine nor
  any remote peer is overwhelmed.

Per-site failures (invalid configuration, timeouts, robots/download problems
that abort the scan) are recorded on the ``batch_sites`` row and never abort the
remaining sites. Repeated hosts are merged: only the first manifest entry for a
given host key is crawled, later ones are stored with status ``skipped``.
"""

from __future__ import annotations

import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .checker import check_links
from .config import ConfigError, load_config
from .crawler import crawl_site
from .fetcher import Fetcher
from .orphans import compute_findings
from .report import write_reports
from .sitemap import generate_sitemap
from .store import Store
from .urlnorm import host_key, normalize_url

# Exit codes. 0/1/3 match the single-site CLI convention; 4 is batch-specific.
EXIT_OK = 0
EXIT_ALL_FAILED = 1
EXIT_PARTIAL = 4
EXIT_CONFIG = 3

VALID_SITE_STATUSES = {"completed", "failed", "skipped"}


# ---------------------------------------------------------------------------
# Global concurrency control
# ---------------------------------------------------------------------------


class GlobalLimiter:
    """A counting semaphore plus a high-water mark of concurrent acquisitions.

    The peak gauge is only used for reporting and verification; the semaphore
    itself is the hard bound on total in-flight HTTP requests.
    """

    def __init__(self, max_inflight: int) -> None:
        if max_inflight < 1:
            raise ConfigError("max_concurrency must be >= 1")
        self.max_inflight = max_inflight
        self._semaphore = threading.BoundedSemaphore(max_inflight)
        self._inflight = 0
        self._peak = 0
        self._gauge_lock = threading.Lock()

    @property
    def peak_inflight(self) -> int:
        return self._peak

    def acquire(self) -> None:
        self._semaphore.acquire()
        with self._gauge_lock:
            self._inflight += 1
            if self._inflight > self._peak:
                self._peak = self._inflight

    def release(self) -> None:
        with self._gauge_lock:
            self._inflight -= 1
        self._semaphore.release()


class BoundedFetcher(Fetcher):
    """Fetcher wrapper that funnels every request through a GlobalLimiter.

    Subclassing :class:`Fetcher` keeps the ``isinstance`` contract used by
    :class:`linkpatrol.crawler.Crawler`. Crawler pages, link checks, robots.txt
    downloads and sitemap probes all call :meth:`fetch`, so wrapping this one
    method bounds every request a site makes. Per-site delay throttling happens
    inside the Crawler before this layer, so a site waiting out its delay does
    not hold a global permit.
    """

    def __init__(self, base: Fetcher, limiter: GlobalLimiter) -> None:
        super().__init__(
            timeout_s=base.timeout_s,
            retries=base.retries,
            user_agent=base.user_agent,
            max_redirects=base.max_redirects,
            max_body_bytes=base.max_body_bytes,
            backoff_s=base.backoff_s,
        )
        self.wrapped = base
        self.limiter = limiter

    def fetch(self, url: str, method: str = "GET", max_body_bytes: int | None = None):
        self.limiter.acquire()
        try:
            return self.wrapped.fetch(url, method=method, max_body_bytes=max_body_bytes)
        finally:
            self.limiter.release()


# ---------------------------------------------------------------------------
# Manifest model and loading
# ---------------------------------------------------------------------------


@dataclass
class ManifestEntry:
    """One [[sites]] row after validation and host de-duplication."""

    name: str
    config_path: str
    host: str | None = None
    config: dict | None = None
    status: str = "pending"  # pending | skipped | invalid
    error: str | None = None
    merged_into: str | None = None


@dataclass
class BatchManifest:
    """The parsed batch manifest."""

    path: Path
    max_concurrency: int
    database: str | None
    reports_dir: str | None
    sitemap_dir: str | None
    entries: list[ManifestEntry] = field(default_factory=list)

    @property
    def runnable(self) -> list[ManifestEntry]:
        return [entry for entry in self.entries if entry.status == "pending"]


def _as_positive_int(value: object, key: str, high: int = 64) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"batch.{key} must be an integer")
    if not 1 <= value <= high:
        raise ConfigError(f"batch.{key} must be between 1 and {high}")
    return value


def load_manifest(path: str | Path) -> BatchManifest:
    """Parse and validate a batch manifest TOML file.

    Every referenced per-site config is loaded through the standard
    :func:`linkpatrol.config.load_config`, so site-level validation is exactly
    the same as for ``scan --config``. Invalid site files are captured on the
    entry (status ``invalid``) rather than raised, letting the batch isolate
    them and continue with the healthy entries.
    """
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise ConfigError(f"batch manifest not found: {manifest_path}")
    try:
        with manifest_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {manifest_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("batch manifest root must be a TOML table")

    unknown = set(data) - {"batch", "sites"}
    if unknown:
        raise ConfigError(f"unknown batch manifest key(s): {sorted(unknown)}")

    section = data.get("batch", {})
    if not isinstance(section, dict):
        raise ConfigError("[batch] must be a TOML table")
    batch_unknown = set(section) - {
        "max_concurrency", "database", "reports_dir", "sitemap_dir",
    }
    if batch_unknown:
        raise ConfigError(f"unknown [batch] key(s): {sorted(batch_unknown)}")
    max_concurrency = _as_positive_int(
        section.get("max_concurrency", 4), "max_concurrency"
    )
    database = section.get("database")
    reports_dir = section.get("reports_dir")
    sitemap_dir = section.get("sitemap_dir")
    for key, value in (
        ("database", database),
        ("reports_dir", reports_dir),
        ("sitemap_dir", sitemap_dir),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"batch.{key} must be a non-empty path")

    raw_sites = data.get("sites")
    if not isinstance(raw_sites, list) or not raw_sites:
        raise ConfigError("manifest must contain at least one [[sites]] entry")

    base_dir = manifest_path.parent
    entries: list[ManifestEntry] = []
    seen_names: set[str] = set()
    seen_hosts: dict[str, str] = {}
    for index, raw in enumerate(raw_sites):
        if not isinstance(raw, dict):
            raise ConfigError(f"[[sites]] entry {index + 1} must be a table")
        site_unknown = set(raw) - {"name", "config"}
        if site_unknown:
            raise ConfigError(
                f"unknown [[sites]] key(s) on entry {index + 1}: {sorted(site_unknown)}"
            )
        name = raw.get("name")
        rel_config = raw.get("config")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"[[sites]] entry {index + 1} requires a string name")
        if not isinstance(rel_config, str) or not rel_config.strip():
            raise ConfigError(f"site {name!r} requires a string config path")
        if name in seen_names:
            raise ConfigError(f"duplicate site name in manifest: {name!r}")
        seen_names.add(name)
        config_path = (base_dir / rel_config).resolve()

        entry = ManifestEntry(name=name, config_path=str(config_path))
        try:
            config = load_config(config_path)
            normalised_seed = normalize_url(config["seed"])
            host = host_key(normalised_seed)
            if not host:
                raise ConfigError("seed has no usable host")
        except ConfigError as exc:
            entry.status = "invalid"
            entry.error = str(exc)
            entries.append(entry)
            continue
        entry.config = config
        entry.host = host
        if host in seen_hosts:
            # Same host appears more than once: merge instead of crawling twice.
            entry.status = "skipped"
            entry.merged_into = seen_hosts[host]
            entry.error = f"duplicate host {host!r}; merged into {seen_hosts[host]!r}"
        else:
            seen_hosts[host] = name
        entries.append(entry)

    return BatchManifest(
        path=manifest_path,
        max_concurrency=max_concurrency,
        database=database,
        reports_dir=reports_dir,
        sitemap_dir=sitemap_dir,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Shared single-site scan execution
# ---------------------------------------------------------------------------


def execute_scan(
    config: dict,
    store: Store,
    fetcher: Fetcher,
    run_id: int,
    listed_urls_provider: Callable[[Fetcher], list[str] | None] | None = None,
) -> dict:
    """Run the crawl/check/report pipeline for one site on an open run.

    This is the same pipeline the single-site ``scan`` command uses; batch mode
    only changes how the config, fetcher and store are constructed. Raising on
    failure lets the caller (CLI or batch worker) own run-status bookkeeping.
    """
    crawl = crawl_site(config, fetcher=fetcher)
    store.save_crawl(run_id, crawl.pages.values())
    store.save_links(run_id, crawl.iter_links())
    checks = check_links(config, crawl, fetcher=fetcher)
    store.save_checks(run_id, checks.checks.values())
    entries = [
        {"url": page.url, "lastmod": None}
        for page in crawl.pages.values()
        if page.is_ok and page.is_html
    ]
    generate_sitemap(entries, config["output"]["sitemap"])
    if listed_urls_provider is None:
        listed = None
    else:
        listed = listed_urls_provider(fetcher)
    if listed is None:
        findings = compute_findings([], crawl.pages.values())
        findings["orphan"] = []
        findings["unlisted"] = []
    else:
        findings = compute_findings(listed, crawl.pages.values())
    stats = crawl.stats.as_dict()
    stats.update(
        {
            "crawl": crawl.stats.as_dict(),
            "checks": len(checks.checks),
            "broken": len(checks.broken),
            "warnings": len(checks.warnings),
            "findings": findings,
            "write_errors": len(store.write_errors),
            "sitemap_urls": len(entries),
        }
    )
    store.finish_run(run_id, "completed", stats)
    json_path, html_path = write_reports(store, run_id, config["output"]["dir"])
    return {
        "pages": len(crawl.pages),
        "checks": len(checks.checks),
        "broken": len(checks.broken),
        "json_report": str(json_path),
        "html_report": str(html_path),
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------


def _apply_output_overrides(
    manifest: BatchManifest, entry: ManifestEntry
) -> dict:
    """Point the site's outputs at batch-namespaced paths when overridden."""
    config = entry.config
    assert config is not None
    if manifest.database:
        config["output"]["database"] = manifest.database
    if manifest.reports_dir:
        config["output"]["dir"] = str(Path(manifest.reports_dir) / entry.name)
    if manifest.sitemap_dir:
        path = Path(manifest.sitemap_dir)
        config["output"]["sitemap"] = str(path / f"{entry.name}.xml")
    return config


def _resolve_database(manifest: BatchManifest) -> str:
    if manifest.database:
        return manifest.database
    databases = {
        entry.config["output"]["database"]
        for entry in manifest.runnable
        if entry.config is not None
    }
    if len(databases) != 1:
        raise ConfigError(
            "sites disagree on output.database and batch.database is not set; "
            "refusing to split a batch across multiple databases"
        )
    return next(iter(databases))


@dataclass
class BatchResult:
    """Aggregate outcome of a batch run."""

    batch_id: int
    status: str
    exit_code: int
    counts: dict[str, int]
    sites: list[dict] = field(default_factory=list)
    peak_inflight: int = 0
    max_concurrency: int = 0


def run_batch(
    manifest: BatchManifest,
    store: Store,
    limiter: GlobalLimiter,
    provider_factory: Callable[[dict], Callable[[Fetcher], list[str] | None] | None]
    | None = None,
    print_line: Callable[[str], None] = print,
) -> BatchResult:
    """Execute every runnable manifest entry and persist per-site status.

    ``provider_factory`` maps a site config to the listed-URL provider used for
    orphan comparison; returning ``None`` keeps the pipeline's default of
    skipping orphan comparison.
    """
    batch_id = store.create_batch(str(manifest.path), manifest.max_concurrency)

    # Record invalid and duplicate-skipped entries up front; they never crawl.
    for entry in manifest.entries:
        if entry.status in {"invalid", "skipped"}:
            store.add_batch_site(
                batch_id,
                entry.name,
                entry.config_path,
                host=entry.host,
                status=entry.status,
                error=entry.error,
                detail=(
                    {"merged_into": entry.merged_into}
                    if entry.merged_into else None
                ),
            )
            print_line(f"site {entry.name}: {entry.status} ({entry.error})")

    runnable = manifest.runnable
    for entry in runnable:
        _apply_output_overrides(manifest, entry)
        store.add_batch_site(
            batch_id,
            entry.name,
            entry.config_path,
            host=entry.host,
            status="running",
            started=True,
        )

    def worker(entry: ManifestEntry) -> dict[str, str]:
        config = entry.config
        assert config is not None
        limits = config["limits"]
        run_id = store.create_run(
            config["seed"], config, batch_id=batch_id, site_name=entry.name
        )
        base_fetcher = Fetcher(
            timeout_s=limits["timeout_s"],
            retries=limits["retries"],
            user_agent=config["robots"]["user_agent"],
            max_redirects=limits["max_redirects"],
        )
        bounded = BoundedFetcher(base_fetcher, limiter)
        provider = provider_factory(config) if provider_factory else None
        try:
            summary = execute_scan(
                config, store, bounded, run_id,
                listed_urls_provider=provider,
            )
        except Exception as exc:  # isolation: one site must not kill the batch
            store.finish_run(run_id, "failed", {"error": "scan aborted"})
            store.update_batch_site(
                batch_id, entry.name, "failed",
                run_id=run_id, error=f"{type(exc).__name__}: {exc}",
            )
            print_line(f"site {entry.name}: failed ({type(exc).__name__}: {exc})")
            return {"name": entry.name, "status": "failed", "run_id": run_id}
        # A site whose every fetch failed is operationally a failed inspection,
        # even though the pipeline itself completed (errors are recorded pages).
        reachable = summary["stats"]["crawl"].get("fetched", 0)
        failures = summary["stats"]["crawl"].get("errors", 0)
        if reachable > 0 and reachable == failures:
            message = "no page could be fetched successfully (host unreachable or all requests errored)"
            store.finish_run(run_id, "failed", {"error": message})
            store.update_batch_site(
                batch_id, entry.name, "failed",
                run_id=run_id, error=message, detail=summary,
            )
            print_line(f"site {entry.name}: failed ({message})")
            return {"name": entry.name, "status": "failed", "run_id": run_id}
        store.update_batch_site(
            batch_id, entry.name, "completed",
            run_id=run_id, detail=summary,
        )
        print_line(
            f"site {entry.name}: completed run={run_id} "
            f"pages={summary['pages']} broken={summary['broken']}"
        )
        return {"name": entry.name, "status": "completed", "run_id": run_id}

    # Site-level parallelism. Each site additionally owns its own Crawler pool,
    # while total in-flight requests stay bounded by the shared limiter.
    max_workers = min(len(runnable), manifest.max_concurrency) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(worker, entry) for entry in runnable]
        for future in as_completed(futures):
            future.result()

    site_rows = store.get_batch_sites(batch_id)
    counts = {
        "total": len(manifest.entries),
        "completed": sum(1 for row in site_rows if row["status"] == "completed"),
        "failed": sum(1 for row in site_rows if row["status"] == "failed"),
        "skipped": sum(1 for row in site_rows if row["status"] == "skipped"),
        "invalid": sum(1 for row in site_rows if row["status"] == "invalid"),
    }
    if counts["completed"] == 0:
        status, exit_code = "failed", EXIT_ALL_FAILED
    elif counts["failed"] or counts["invalid"]:
        status, exit_code = "partial", EXIT_PARTIAL
    else:
        status, exit_code = "completed", EXIT_OK
    stats = {**counts, "peak_inflight": limiter.peak_inflight}
    store.finish_batch(batch_id, status, stats)
    return BatchResult(
        batch_id=batch_id,
        status=status,
        exit_code=exit_code,
        counts=counts,
        sites=site_rows,
        peak_inflight=limiter.peak_inflight,
        max_concurrency=manifest.max_concurrency,
    )


def format_summary(result: BatchResult) -> str:
    """Render the final per-site summary table printed by the CLI."""
    lines = [
        f"batch {result.batch_id}: {result.status} "
        f"(total={result.counts['total']} completed={result.counts['completed']} "
        f"failed={result.counts['failed']} skipped={result.counts['skipped']} "
        f"invalid={result.counts['invalid']}) "
        f"global_max_concurrency={result.max_concurrency} "
        f"peak_inflight={result.peak_inflight}"
    ]
    for site in result.sites:
        run_id = site["run_id"]
        run_part = f"run={run_id}" if run_id is not None else "run=-"
        lines.append(
            f"  {site['name']:<20} {site['status']:<10} {run_part} "
            f"host={site['host'] or '-'} "
            f"error={site['error'] or '-'}"
        )
    return "\n".join(lines)
