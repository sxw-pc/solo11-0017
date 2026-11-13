"""Command-line interface for demo, scan, report, serve, sitemap, and baseline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .baseline import BaselineError, IgnoreRules, diff_runs, exit_code_for, summary_text
from .batch import (
    GlobalLimiter,
    format_summary,
    load_manifest,
    run_batch,
)
from .config import ConfigError, load_config
from .fetcher import Fetcher
from .orphans import load_url_list
from .report import write_reports
from .sitemap import SitemapError, generate_sitemap, parse_sitemap, parse_sitemap_file
from .store import BaselineExistsError, Store
from .urlnorm import origin

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linkpatrol")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the built-in demonstration site")
    demo.add_argument("--port", type=int, default=5001)

    scan = sub.add_parser("scan", help="crawl and check a configured site")
    scan.add_argument("--config", required=True)
    scan.add_argument("--urls-file", help="listed URLs for orphan comparison")
    scan.add_argument("--sitemap-url", help="remote sitemap used for orphan comparison")

    batch_scan = sub.add_parser(
        "batch-scan",
        help="crawl every site listed in a batch manifest in one command",
    )
    batch_scan.add_argument("--manifest", required=True)
    batch_scan.add_argument(
        "--max-concurrency",
        type=int,
        help="override the manifest's global in-flight request limit",
    )

    report = sub.add_parser("report", help="regenerate reports for a run")
    report.add_argument("--db", default="out/linkpatrol.db")
    report.add_argument("--run", type=int, required=True)
    report.add_argument("--out", default="reports")

    serve = sub.add_parser("serve", help="browse stored runs in a local web UI")
    serve.add_argument("--db", default="out/linkpatrol.db")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5002)

    sitemap = sub.add_parser("sitemap", help="generate or parse a sitemap")
    sitemap.add_argument("--db", default="out/linkpatrol.db")
    sitemap.add_argument("--run", type=int)
    sitemap.add_argument("--out", help="output XML path for generation")
    sitemap.add_argument("--parse-file")
    sitemap.add_argument("--parse-url")
    sitemap.add_argument("--urls-out")

    base = sub.add_parser("baseline", help="manage named baselines")
    base_sub = base.add_subparsers(dest="baseline_command", required=True)
    bset = base_sub.add_parser("set")
    bset.add_argument("--name", required=True)
    bset.add_argument("--run", type=int, required=True)
    bset.add_argument("--db", default="out/linkpatrol.db")
    bset.add_argument("--force", action="store_true")
    blist = base_sub.add_parser("list")
    blist.add_argument("--db", default="out/linkpatrol.db")
    bdel = base_sub.add_parser("delete")
    bdel.add_argument("--name", required=True)
    bdel.add_argument("--db", default="out/linkpatrol.db")

    diff = sub.add_parser("diff", help="diff a run against a baseline")
    diff.add_argument("--baseline", required=True)
    diff.add_argument("--run", type=int, required=True)
    diff.add_argument("--db", default="out/linkpatrol.db")
    diff.add_argument("--ignore")
    diff.add_argument("--fail-on", choices=["new-broken", "none"], default="none")
    diff.add_argument("--json-out")
    return parser

def _fetch_sitemap_text(fetcher: Fetcher, url: str) -> str:
    result = fetcher.fetch(url, method="GET", max_body_bytes=2 * 1024 * 1024)
    if result.status_code != 200 or result.error:
        raise SitemapError(f"cannot fetch sitemap {url}: HTTP {result.status_code}")
    return result.text

def _listed_urls(args, config: dict, fetcher: Fetcher) -> list[str] | None:
    if args.urls_file:
        return load_url_list(args.urls_file)
    url = args.sitemap_url
    if not url:
        candidate = f"{origin(config['seed'])}/sitemap.xml"
        result = fetcher.fetch(candidate, method="GET", max_body_bytes=2 * 1024 * 1024)
        if result.status_code != 200 or result.error:
            return None
        url = candidate
        text = result.text
    else:
        text = _fetch_sitemap_text(fetcher, url)
    try:
        return parse_sitemap(
            text,
            fetch=lambda child: _fetch_sitemap_text(fetcher, child),
            base_url=url,
        )
    except SitemapError as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return None

def _scan(args) -> int:
    from .batch import execute_scan

    config = load_config(args.config)
    limits = config["limits"]
    store = Store(config["output"]["database"])
    run_id = store.create_run(config["seed"], config)
    fetcher = Fetcher(
        timeout_s=limits["timeout_s"],
        retries=limits["retries"],
        user_agent=config["robots"]["user_agent"],
        max_redirects=limits["max_redirects"],
    )
    try:
        summary = execute_scan(
            config,
            store,
            fetcher,
            run_id,
            listed_urls_provider=lambda f: _listed_urls(args, config, f),
        )
        print(
            f"run {run_id}: pages={summary['pages']} checks={summary['checks']} "
            f"broken={summary['broken']} reports={summary['json_report']},"
            f"{summary['html_report']}"
        )
        return 0
    except Exception:
        store.finish_run(run_id, "failed", {"error": "scan aborted"})
        raise
    finally:
        store.close()

def _auto_sitemap_provider(config: dict):
    """Build the orphan-comparison provider for a batch site config.

    Batch sites do not carry --urls-file/--sitemap-url flags, so they keep the
    single-site default: probe ``<origin>/sitemap.xml`` and skip orphan
    comparison when it is absent.
    """

    def provider(fetcher: Fetcher) -> list[str] | None:
        candidate = f"{origin(config['seed'])}/sitemap.xml"
        result = fetcher.fetch(candidate, method="GET", max_body_bytes=2 * 1024 * 1024)
        if result.status_code != 200 or result.error:
            return None
        try:
            return parse_sitemap(
                result.text,
                fetch=lambda child: _fetch_sitemap_text(fetcher, child),
                base_url=candidate,
            )
        except SitemapError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            return None

    return provider

def _batch_scan(args) -> int:
    manifest = load_manifest(args.manifest)
    if args.max_concurrency is not None:
        if isinstance(args.max_concurrency, bool) or not 1 <= args.max_concurrency <= 64:
            raise ConfigError("--max-concurrency must be between 1 and 64")
        manifest.max_concurrency = args.max_concurrency

    # Resolve the shared database first so a disagreement across sites yields
    # exit code 3 before any crawling starts.
    from .batch import _resolve_database

    database = _resolve_database(manifest)
    limiter = GlobalLimiter(manifest.max_concurrency)
    store = Store(database)
    try:
        result = run_batch(
            manifest,
            store,
            limiter,
            provider_factory=_auto_sitemap_provider,
        )
    finally:
        store.close()
    print(format_summary(result))
    return result.exit_code

def _report(args) -> int:
    store = Store(args.db)
    try:
        json_path, html_path = write_reports(store, args.run, args.out)
    finally:
        store.close()
    print(f"wrote {json_path} and {html_path}")
    return 0

def _serve(args) -> int:
    if not Path(args.db).is_file():
        raise FileNotFoundError(f"database not found: {args.db}")
    from .server import create_app

    app = create_app(args.db)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0

def _sitemap(args) -> int:
    if args.parse_file or args.parse_url:
        fetcher = Fetcher()
        try:
            if args.parse_file:
                urls = parse_sitemap_file(
                    args.parse_file,
                    fetch=lambda child: _fetch_sitemap_text(fetcher, child),
                )
            else:
                text = _fetch_sitemap_text(fetcher, args.parse_url)
                urls = parse_sitemap(
                    text,
                    fetch=lambda child: _fetch_sitemap_text(fetcher, child),
                    base_url=args.parse_url,
                )
        except SitemapError as exc:
            print(f"sitemap error: {exc}", file=sys.stderr)
            return 1
        if args.urls_out:
            Path(args.urls_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.urls_out).write_text("\n".join(urls) + "\n", encoding="utf-8")
        for url in urls:
            print(url)
        return 0
    if args.run is None:
        print("sitemap requires --run or --parse-file/--parse-url", file=sys.stderr)
        return 1
    store = Store(args.db)
    try:
        pages = [p for p in store.get_pages(args.run) if p["status_code"] and 200 <= p["status_code"] < 300]
    finally:
        store.close()
    output = args.out or "out/sitemap.xml"
    try:
        xml = generate_sitemap(pages, output)
    except SitemapError as exc:
        print(f"sitemap error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {output} ({len(pages)} URLs, {len(xml)} bytes)")
    return 0

def _baseline(args) -> int:
    store = Store(args.db)
    try:
        if args.baseline_command == "set":
            store.set_baseline(args.name, args.run, force=args.force)
            print(f"baseline {args.name!r} -> run {args.run}")
        elif args.baseline_command == "list":
            for row in store.list_baselines():
                print(f"{row['name']}\trun={row['run_id']}\t{row['created_at']}")
        else:
            print("deleted" if store.delete_baseline(args.name) else "not found")
    finally:
        store.close()
    return 0

def _diff(args) -> int:
    store = Store(args.db)
    try:
        ignore = IgnoreRules.load(args.ignore)
        diff = diff_runs(store, args.baseline, args.run, ignore)
        store.merge_stats(
            args.run,
            {
                "last_diff": {
                    "baseline": args.baseline,
                    "baseline_run": diff["baseline_run"],
                    "summary": diff["summary"],
                }
            },
        )
    finally:
        store.close()
    rendered = json.dumps(diff, indent=2, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    print(summary_text(diff), file=sys.stderr)
    return exit_code_for(diff, args.fail_on)

def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch, translating expected errors to exit codes."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "demo":
            from .demoapp import create_demo_app

            create_demo_app().run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
            return 0
        if args.command == "scan":
            return _scan(args)
        if args.command == "batch-scan":
            return _batch_scan(args)
        if args.command == "report":
            return _report(args)
        if args.command == "serve":
            return _serve(args)
        if args.command == "sitemap":
            return _sitemap(args)
        if args.command == "baseline":
            return _baseline(args)
        if args.command == "diff":
            return _diff(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 3
    except (BaselineError, BaselineExistsError) as exc:
        print(f"baseline error: {exc}", file=sys.stderr)
        return 1
    except (SitemapError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1
