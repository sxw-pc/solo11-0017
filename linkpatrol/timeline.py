"""Timeline queries that survive detail archival.

The ``timeline_*`` tables keep one compact record per run and per target
URL, so questions such as "in which run did this URL first become broken?"
and "which runs did this broken URL span before it was fixed?" still work
after the heavy ``pages`` / ``links`` / ``checks`` rows are deleted.

Live runs are answered from their detail tables; archived runs from the
timeline tables.  Both paths deduplicate by target URL, so a target linked
from several pages in one run yields exactly one timeline entry.
"""

from __future__ import annotations

from .store import Store

def url_history(
    store: Store,
    target_url: str,
    *,
    seed: str | None = None,
) -> dict:
    """First broken run, each fix run, and the spans for one target URL.

    ``entries`` has one row per known run (live or archived) in run order.
    ``spans`` groups consecutive broken runs; each span records the run ids
    and the run in which the URL was fixed (``None`` when still open).

    Runs whose status is not ``completed`` carry no trustworthy conclusion;
    they are listed in ``entries`` but do not open, extend, or close a span.
    """
    records = store.list_all_run_records()
    if seed is not None:
        records = [record for record in records if record["seed"] == seed]
    entries: list[dict] = []
    spans: list[dict] = []
    open_span: dict | None = None
    previous_broken = False
    last_valid_broken = False
    first_broken_run: int | None = None
    fix_runs: list[int] = []

    for record in records:
        run_id = record["run_id"]
        valid = record["status"] == "completed"
        if not valid:
            entries.append(
                {
                    "run_id": run_id,
                    "started_at": record["started_at"],
                    "archived": record["archived"],
                    "status": record["status"],
                    "broken": None,
                    "ignored": "not_completed",
                }
            )
            continue
        if record["archived"]:
            rows = store.get_timeline_broken(run_id=run_id, target_url=target_url)
        else:
            rows = [
                row
                for row in store.broken_records_for_run(run_id)
                if row["target_url"] == target_url
            ]
        broken = bool(rows)
        entry = {
            "run_id": run_id,
            "started_at": record["started_at"],
            "archived": record["archived"],
            "broken": broken,
            "is_new": bool(broken and not previous_broken),
        }
        if broken:
            row = rows[0]
            if first_broken_run is None:
                first_broken_run = run_id
            entry.update(
                {
                    "first_broken": first_broken_run == run_id,
                    "source_url": row.get("source_url"),
                    "status_code": row.get("status_code"),
                    "error": row.get("error"),
                }
            )
            if not previous_broken:
                open_span = {
                    "first_broken_run": run_id,
                    "last_broken_run": run_id,
                    "fixed_run": None,
                    "run_ids": [run_id],
                }
                spans.append(open_span)
            else:
                open_span["last_broken_run"] = run_id
                open_span["run_ids"].append(run_id)
        elif previous_broken:
            entry["fixed_run"] = run_id
            fix_runs.append(run_id)
            if open_span is not None:
                open_span["fixed_run"] = run_id
        entries.append(entry)
        previous_broken = broken
        last_valid_broken = broken

    return {
        "target_url": target_url,
        "seed": seed,
        "first_broken_run": first_broken_run,
        "fix_runs": fix_runs,
        "last_fixed_run": fix_runs[-1] if fix_runs else None,
        "currently_broken": last_valid_broken,
        "still_open": bool(open_span and open_span["fixed_run"] is None),
        "spans": spans,
        "entries": entries,
        "last_run_archived": bool(records and records[-1]["archived"]),
    }

def _live_diff_counts(store: Store, run_id: int, seed: str) -> dict:
    current = store.broken_urls_for_run(run_id)
    previous_id = store.previous_run_id(run_id, seed=seed)
    previous = (
        store.broken_urls_for_run(previous_id) if previous_id is not None else set()
    )
    return {
        "new_broken_total": len(current - previous),
        "fixed_total": len(previous - current),
    }

def run_timeline(store: Store, *, seed: str | None = None) -> list[dict]:
    """One summary row per known run, oldest first."""
    records = store.list_all_run_records()
    if seed is not None:
        records = [record for record in records if record["seed"] == seed]
    rows: list[dict] = []
    for record in records:
        run_id = record["run_id"]
        timeline = store.get_timeline_run(run_id)
        row = {
            "run_id": run_id,
            "seed": record["seed"],
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "status": record["status"],
            "archived": record["archived"],
            "checks_total": record["checks_total"],
            "broken_total": record["broken_total"],
            "new_broken_total": None,
            "fixed_total": None,
        }
        if timeline is not None:
            row.update(
                {
                    "checks_total": timeline["checks_total"],
                    "broken_total": timeline["broken_total"],
                    "new_broken_total": timeline["new_broken_total"],
                    "fixed_total": timeline["fixed_total"],
                }
            )
        elif record["status"] == "completed":
            row.update(_live_diff_counts(store, run_id, record["seed"]))
        rows.append(row)
    return rows
