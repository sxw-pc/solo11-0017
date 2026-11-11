"""Machine-readable JSON and fully inlined static HTML reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .store import Store

TEMPLATE_DIR = Path(__file__).with_name("templates")

def build_report(store: Store, run_id: int) -> dict:
    """Assemble report data from one completed run."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    pages = store.get_pages(run_id)
    links = store.get_links(run_id)
    checks = store.get_checks_map(run_id)
    broken: list[dict] = []
    anchors: list[dict] = []
    sources_with_internal: set[str] = set()
    for link in links:
        if link["scope"] == "internal":
            sources_with_internal.add(link["source_url"])
        check = checks.get(link["target_url"])
        if check is None:
            continue
        if not check["ok"]:
            broken.append(
                {
                    "source_url": link["source_url"],
                    "target_url": link["target_url"],
                    "raw_href": link["raw_href"],
                    "kind": link["kind"],
                    "scope": link["scope"],
                    "line_no": link["line_no"],
                    "status_code": check["status_code"],
                    "error": check["error"],
                    "final_url": check["final_url"],
                }
            )
        if not check["anchor_ok"]:
            anchors.append(
                {
                    "source_url": link["source_url"],
                    "target_url": link["target_url"],
                    "anchor": link["anchor"],
                    "line_no": link["line_no"],
                }
            )
    redirects = []
    for check in checks.values():
        if check["redirect_chain"]:
            redirects.append(
                {
                    "url": check["url"],
                    "final_url": check["final_url"],
                    "status_code": check["status_code"],
                    "chain": check["redirect_chain"],
                    "chain_length": len(check["redirect_chain"]),
                    "warning": len(check["redirect_chain"]) >= 3,
                }
            )
    redirects.sort(key=lambda item: item["url"])
    broken.sort(key=lambda item: (item["source_url"], item["target_url"]))
    anchors.sort(key=lambda item: (item["source_url"], item["target_url"]))
    findings = run["stats"].get("findings", {})
    reachable = [p for p in pages if p["status_code"] and 200 <= p["status_code"] < 300]
    dead_ends = findings.get("dead_end")
    if dead_ends is None:
        dead_ends = [
            page["url"]
            for page in reachable
            if page["url"] not in sources_with_internal
        ]
    orphan_report = {
        "orphan": findings.get("orphan", []),
        "unlisted": findings.get("unlisted", []),
    }
    warnings = [check for check in checks.values() if check["ok"] and check["error"]]
    data = {
        "run": {
            "id": run["id"],
            "seed": run["seed"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        },
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "summary": {
            "pages": len(pages),
            "reachable_pages": len(reachable),
            "links": len(links),
            "checks": len(checks),
            "broken": len(broken),
            "redirects": len(redirects),
            "anchor_failures": len(anchors),
            "warnings": len(warnings),
            "orphan": len(orphan_report["orphan"]),
            "unlisted": len(orphan_report["unlisted"]),
            "dead_ends": len(dead_ends),
            "crawl": run["stats"].get("crawl", {}),
        },
        "broken": broken,
        "redirects": redirects,
        "anchors": anchors,
        "orphans": orphan_report,
        "dead_ends": dead_ends,
    }
    return data

def write_json_report(data: dict, output_dir: str | Path) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run_{data['run']['id']}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path

def render_static_html(data: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report_static.html")
    return template.render(**data)

def write_html_report(data: dict, output_dir: str | Path) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run_{data['run']['id']}.html"
    path.write_text(render_static_html(data), encoding="utf-8")
    return path

def write_reports(store: Store, run_id: int, output_dir: str | Path) -> tuple[Path, Path]:
    """Write both report formats and return their paths."""
    data = build_report(store, run_id)
    return write_json_report(data, output_dir), write_html_report(data, output_dir)
