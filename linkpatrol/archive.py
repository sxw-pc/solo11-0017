"""Selective run archival with an atomic SQLite transaction and a timeline.

Archiving a run performs three things as one reversible unit:

1. compress its conclusions into lightweight ``timeline_*`` rows;
2. delete its ``pages`` / ``links`` / ``checks`` and finally the ``runs`` row;
3. remove its ``run_<id>.json`` / ``run_<id>.html`` report artifacts.

Runs referenced by any baseline are never archived.  If any step raises, the
database transaction is rolled back and staged report files are restored, so
no half-deleted state (orphan rows or missing files) is left behind.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .store import Store

REPORT_SUFFIXES = (".json", ".html")

class ArchiveError(RuntimeError):
    """Raised when archival cannot be completed safely."""

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

@dataclass
class ArchivePlan:
    """The archive decision for every known run."""

    archived: list[int] = field(default_factory=list)
    protected: list[int] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)
    reason: dict[int, str] = field(default_factory=dict)

def _age_days(run: dict, now: datetime) -> float:
    stamp = run.get("finished_at") or run.get("started_at")
    if not stamp:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed).total_seconds() / 86400.0)

def plan_archive(
    store: Store,
    *,
    keep_runs: int | None = None,
    retain_days: float | None = None,
    now: datetime | None = None,
) -> ArchivePlan:
    """Decide which runs fall outside both retention guards.

    A run is archived when it is not referenced by a baseline, is not among
    the newest ``keep_runs`` runs, and is at least ``retain_days`` old.
    Baseline-protected runs are always reported in ``protected`` regardless
    of age or count.  At least one unprotected run is always kept and at
    least one retention argument is required, so a bare invocation never
    wipes the live database.
    """
    if keep_runs is None and retain_days is None:
        raise ArchiveError("provide --keep and/or --retain-days")
    if keep_runs is not None and keep_runs < 0:
        raise ArchiveError("keep must be >= 0")
    if retain_days is not None and retain_days < 0:
        raise ArchiveError("retain-days must be >= 0")
    current = now or datetime.now(timezone.utc)
    protected_ids = set(store.protected_run_ids())
    plan = ArchivePlan()
    kept_count = 0
    for run in store.list_runs():  # newest first
        run_id = int(run["id"])
        if run_id in protected_ids:
            plan.protected.append(run_id)
            plan.reason[run_id] = "baseline"
            continue
        if run.get("status") != "completed":
            # A running scan would race the archive transaction; a failed
            # scan has no trustworthy conclusion to compress into a timeline.
            plan.kept.append(run_id)
            plan.reason[run_id] = "not_completed"
            continue
        if kept_count < 1 or (keep_runs is not None and kept_count < keep_runs):
            plan.kept.append(run_id)
            plan.reason[run_id] = "keep"
            kept_count += 1
            continue
        if retain_days is not None and _age_days(run, current) < retain_days:
            plan.kept.append(run_id)
            plan.reason[run_id] = "retain_days"
            kept_count += 1
            continue
        plan.archived.append(run_id)
    plan.archived.sort()
    plan.protected.sort()
    plan.kept.sort()
    return plan

def summarize_run(store: Store, run_id: int) -> dict:
    """Build the compact timeline record for one live run.

    Conclusions are keyed by target URL so a URL linked from several pages
    in the same run collapses to exactly one timeline row.  ``new_broken`` /
    ``fixed`` are the diff against the previous known run (live or archived)
    for the same seed.
    """
    run = store.get_run(run_id)
    if run is None:
        raise ArchiveError(f"run not found: {run_id}")
    seed = run["seed"]
    previous_id = store.previous_run_id(run_id, seed=seed)
    current = store.broken_urls_for_run(run_id)
    previous = (
        store.broken_urls_for_run(previous_id) if previous_id is not None else set()
    )
    new_ids = current - previous
    fixed_ids = previous - current
    details = {
        row["target_url"]: row for row in store.broken_records_for_run(run_id)
    }
    broken = []
    for url in sorted(current):
        row = details.get(url, {})
        broken.append(
            {
                "target_url": url,
                "source_url": row.get("source_url", ""),
                "status_code": row.get("status_code"),
                "error": row.get("error"),
                "is_new": url in new_ids,
            }
        )
    fixed = [
        {
            "target_url": url,
            "source_url": _first_source(store, previous_id, url),
        }
        for url in sorted(fixed_ids)
    ]
    return {
        "run_id": run_id,
        "seed": seed,
        "started_at": run["started_at"],
        "finished_at": run.get("finished_at"),
        "status": run["status"],
        "checks_total": store.run_detail_counts(run_id)["checks"],
        "broken_total": len(current),
        "new_broken_total": len(new_ids),
        "fixed_total": len(fixed_ids),
        "archived_at": _now(),
        "broken": broken,
        "fixed": fixed,
    }

def _first_source(store: Store, run_id: int | None, target_url: str) -> str:
    if run_id is None:
        return ""
    for row in store.broken_records_for_run(run_id):
        if row["target_url"] == target_url:
            return str(row["source_url"])
    return ""

def _artifact_paths(report_dir: str | Path | None, run_id: int) -> list[Path]:
    if not report_dir:
        return []
    directory = Path(report_dir)
    return [directory / f"run_{run_id}{suffix}" for suffix in REPORT_SUFFIXES]

def _stage_artifacts(paths: list[Path], trash: Path | None) -> list[Path]:
    """Move report artifacts aside so a later failure can restore them.

    Returns every path actually staged.  Callers must append to their own
    restore list after each artifact so a mid-way ``OSError`` still restores
    the files moved before the failure.
    """
    staged: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            if trash is not None:
                trash.mkdir(parents=True, exist_ok=True)
                os.replace(path, trash / path.name)
            else:
                path.unlink()
            staged.append(path)
        except OSError as exc:
            raise _PartialStage(exc, staged) from exc
    return staged

class _PartialStage(ArchiveError):
    """Carries already-staged artifacts when a later move fails."""

    def __init__(self, exc: OSError, staged: list[Path]) -> None:
        super().__init__(exc)
        self.staged = staged

def _restore_artifacts(staged: list[Path], trash: Path) -> None:
    for path in reversed(staged):
        source = trash / path.name
        if source.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, path)

def archive_runs(
    store: Store,
    run_ids: list[int],
    *,
    report_dir: str | Path | None = None,
    failure_hook: Callable[[str], None] | None = None,
) -> dict:
    """Archive the given runs atomically, protecting baseline runs.

    ``failure_hook`` is a test seam invoked with ``"after_db_write"`` before
    the transaction commits and ``"after_artifacts"`` after files are staged.
    It may raise to exercise the rollback path.
    """
    protected = set(store.protected_run_ids())
    requested = sorted(set(run_ids))
    protected_hits = sorted(run_id for run_id in requested if run_id in protected)
    eligible = [run_id for run_id in requested if run_id not in protected]
    missing = [run_id for run_id in eligible if store.get_run(run_id) is None]
    if missing:
        raise ArchiveError(f"run not found: {', '.join(map(str, missing))}")
    unfinished = [
        run_id for run_id in eligible if store.get_run(run_id)["status"] != "completed"
    ]
    if unfinished:
        raise ArchiveError(
            "run not completed, refusing to archive: "
            + ", ".join(map(str, unfinished))
        )

    entries = [summarize_run(store, run_id) for run_id in eligible]
    if not eligible:
        return {"archived": [], "protected": protected_hits, "timeline": []}

    paths = {run_id: _artifact_paths(report_dir, run_id) for run_id in eligible}
    trash_root = (
        Path(report_dir).resolve() / f".archive-tmp-{os.getpid()}"
        if report_dir else None
    )
    staged: list[Path] = []
    store.begin_immediate()
    try:
        store.write_archive(entries, eligible)
        if failure_hook is not None:
            failure_hook("after_db_write")
        for run_id in eligible:
            try:
                staged.extend(_stage_artifacts(paths[run_id], trash_root))
            except _PartialStage as exc:
                staged.extend(exc.staged)
                raise
        if failure_hook is not None:
            failure_hook("after_artifacts")
        store.commit()
    except BaseException as exc:
        store.rollback()
        if trash_root is not None and staged:
            _restore_artifacts(staged, trash_root)
        if trash_root is not None:
            shutil.rmtree(trash_root, ignore_errors=True)
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError(f"archive failed: {exc}") from exc

    if trash_root is not None:
        shutil.rmtree(trash_root, ignore_errors=True)
    return {
        "archived": eligible,
        "protected": protected_hits,
        "timeline": entries,
    }
