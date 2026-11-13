"""Command-line interface for demo, scan, report, serve, sitemap, and baseline."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .baseline import BaselineError, IgnoreRules, diff_runs, exit_code_for, summary_text
from .checker import check_links
from .config import ConfigError, load_config
from .crawler import crawl_site
from .fetcher import Fetcher
from .orphans import compute_findings, load_url_list
from .report import write_reports
from .sitemap import SitemapError, generate_sitemap, parse_sitemap, parse_sitemap_file
from .store import BaselineExistsError, Store
from .trends import ScanArchive, TrendConfig, TrendError, build_trend_report
from .trendstore import TrendStore
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

    trend = sub.add_parser("trend", help="offline multi-run trend analysis")
    trend_sub = trend.add_subparsers(dest="trend_command", required=True)
    tgen = trend_sub.add_parser("generate", help="build a trend report from historical runs")
    tgen.add_argument("--db", default="out/linkpatrol.db", help="scan database (read-only)")
    tgen.add_argument("--trend-db", default="out/trends.db", help="trend artifact database")
    tgen.add_argument("--seed", required=True)
    tgen.add_argument("--since", help="ISO-8601 lower bound on runs.started_at (inclusive)")
    tgen.add_argument("--until", help="ISO-8601 upper bound on runs.started_at (inclusive)")
    tgen.add_argument("--runs", help="comma-separated run ids; mutually exclusive with the time window")
    tgen.add_argument("--min-occurrences", type=int, default=2,
                      help="minimum observed runs for an entity to join anomaly classification")
    tgen.add_argument("--include-incomplete", action="store_true",
                      help="include runs whose status is not 'completed'")
    tgen.add_argument("--json-out", help="also write the full report payload to this JSON file")
    tlist = trend_sub.add_parser("list", help="list stored trend reports")
    tlist.add_argument("--trend-db", default="out/trends.db")
    tlist.add_argument("--seed")
    tshow = trend_sub.add_parser("show", help="print one stored trend report as JSON")
    tshow.add_argument("--trend-db", default="out/trends.db")
    tshow.add_argument("--report", type=int, required=True)
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
        listed = _listed_urls(args, config, fetcher)
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
        print(
            f"run {run_id}: pages={len(crawl.pages)} checks={len(checks.checks)} "
            f"broken={len(checks.broken)} reports={json_path},{html_path}"
        )
        return 0
    except Exception:
        store.finish_run(run_id, "failed", {"error": "scan aborted"})
        raise
    finally:
        store.close()

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

def _parse_run_ids(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    ids: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError as exc:
            raise TrendError(f"invalid run id: {token!r}") from exc
    if not ids:
        raise TrendError("--runs must contain at least one id")
    return ids


def _trend(args) -> int:
    if args.trend_command == "list":
        with TrendStore(args.trend_db) as trend_store:
            reports = trend_store.list_reports(seed=args.seed)
        for report in reports:
            summary = report["summary"]
            print(
                f"report {report['id']}\tseed={report['seed']}\t"
                f"runs={len(report['run_ids'])}\t"
                f"deteriorating={summary['deteriorating']} "
                f"flapping={summary['flapping']} "
                f"appeared={summary['appeared']} "
                f"disappeared={summary['disappeared']}\t"
                f"hash={report['content_hash'][:12]}\t{report['generated_at']}"
            )
        return 0

    if args.trend_command == "show":
        with TrendStore(args.trend_db) as trend_store:
            report = trend_store.get_report(args.report)
        if report is None:
            raise TrendError(f"trend report not found: {args.report}")
        print(json.dumps(report["payload"], indent=2, ensure_ascii=False))
        return 0

    run_ids = _parse_run_ids(args.runs)
    if run_ids is not None and (args.since or args.until):
        raise TrendError("--runs cannot be combined with --since/--until")
    config = TrendConfig(
        seed=args.seed,
        since=args.since,
        until=args.until,
        run_ids=run_ids,
        include_incomplete=args.include_incomplete,
        min_occurrences=args.min_occurrences,
    )
    with ScanArchive(args.db) as archive:
        payload = build_trend_report(archive, config)
    payload["generated_at"] = _utc_now_iso()
    with TrendStore(args.trend_db) as trend_store:
        report_id, created = trend_store.save_report(payload)
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    print(rendered)
    action = "created" if created else "unchanged (same content hash)"
    summary = payload["summary"]
    print(
        f"trend report {report_id} {action}: runs={summary['runs_total']} "
        f"deteriorating={summary['deteriorating']} flapping={summary['flapping']} "
        f"appeared={summary['appeared']} disappeared={summary['disappeared']}",
        file=sys.stderr,
    )
    return 0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
        if args.command == "trend":
            return _trend(args)
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
