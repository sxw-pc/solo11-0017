"""Read-only Flask browser for stored linkpatrol runs."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, g, render_template, request

from .store import Store

def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None

def create_app(db_path: str | Path) -> Flask:
    """Build the report browser application."""
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)
    app.config["STORE_FACTORY"] = lambda: Store(app.config["DB_PATH"])

    @app.before_request
    def open_store() -> None:
        g.store = app.config["STORE_FACTORY"]()

    @app.teardown_request
    def close_store(_exc) -> None:
        store = g.pop("store", None)
        if store is not None:
            store.close()

    @app.route("/")
    def index():
        runs = g.store.list_runs()
        for run in runs:
            run["stats"] = run.get("stats") or {}
        return render_template("runs.html", runs=runs)

    @app.route("/runs/<int:run_id>")
    def run_detail(run_id: int):
        run = g.store.get_run(run_id)
        if run is None:
            abort(404)
        status = _parse_int(request.args.get("status"))
        kind = request.args.get("kind") or ""
        query = (request.args.get("q") or "").strip().lower()
        pages = g.store.get_pages(run_id, status=status)
        checks = g.store.get_checks_map(run_id)
        links = g.store.get_links(run_id, kind=kind or None)
        rows = []
        for link in links:
            if query and query not in link["target_url"].lower() and query not in link["source_url"].lower():
                continue
            check = checks.get(link["target_url"], {})
            if status is not None and check.get("status_code") != status:
                continue
            row = dict(link)
            row["check"] = check
            rows.append(row)
        all_checks = list(checks.values())
        broken_count = sum(1 for check in all_checks if not check["ok"])
        return render_template(
            "run_detail.html",
            run=run,
            pages=pages,
            links=rows,
            broken_count=broken_count,
            filters={"status": status, "kind": kind, "q": query},
        )

    @app.route("/pages/<int:page_id>")
    def page_detail(page_id: int):
        page = g.store.get_page(page_id)
        if page is None:
            abort(404)
        run = g.store.get_run(page["run_id"])
        checks = g.store.get_checks_map(page["run_id"])
        rows = []
        for link in g.store.get_links(page["run_id"], source_url=page["url"]):
            row = dict(link)
            row["check"] = checks.get(link["target_url"], {})
            rows.append(row)
        return render_template("page_detail.html", page=page, run=run, links=rows)

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("base.html", title="Not found", body="That record does not exist."), 404

    return app
