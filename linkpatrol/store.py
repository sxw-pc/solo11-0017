"""SQLite persistence: schema, idempotent writes, and report queries."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import SCHEMA

class BaselineExistsError(ValueError):
    """Raised when setting an existing baseline without force."""

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

class Store:
    """A small repository around one SQLite database file."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.write_errors: list[str] = []

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _write(self, sql: str, params: Iterable[Any]) -> bool:
        savepoint = "row_write"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self.conn.execute(sql, tuple(params))
            self.conn.execute(f"RELEASE {savepoint}")
            self.conn.commit()
            return True
        except sqlite3.Error as exc:
            self.conn.execute(f"ROLLBACK TO {savepoint}")
            self.conn.execute(f"RELEASE {savepoint}")
            self.conn.commit()
            self.write_errors.append(str(exc))
            return False

    def create_run(self, seed: str, config: dict) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs(seed, config_json, started_at, status, stats_json) "
            "VALUES (?, ?, ?, 'running', '{}')",
            (seed, json.dumps(config, sort_keys=True), _now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str = "completed",
                   stats: dict | None = None) -> None:
        fields = ["status = ?", "finished_at = ?"]
        params: list[Any] = [status, _now()]
        if stats is not None:
            fields.append("stats_json = ?")
            params.append(json.dumps(stats, sort_keys=True))
        params.append(run_id)
        self.conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", params)
        self.conn.commit()

    def merge_stats(self, run_id: int, updates: dict) -> dict:
        run = self.get_run(run_id)
        stats = run["stats"] if run else {}
        stats.update(updates)
        self.conn.execute(
            "UPDATE runs SET stats_json = ? WHERE id = ?",
            (json.dumps(stats, sort_keys=True), run_id),
        )
        self.conn.commit()
        return stats

    def insert_page(self, run_id: int, page: Any) -> bool:
        if isinstance(page, dict):
            values = page
        else:
            values = {
                "url": page.url,
                "depth": page.depth,
                "status_code": page.status_code,
                "content_hash": page.content_hash,
                "duration_ms": page.duration_ms,
                "error": page.error,
            }
        return self._write(
            "INSERT INTO pages(run_id, url, depth, status_code, content_hash, "
            "duration_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                values["url"],
                values["depth"],
                values.get("status_code"),
                values["content_hash"],
                values["duration_ms"],
                values.get("error"),
            ),
        )

    def insert_link(self, run_id: int, source_url: str, link: Any) -> bool:
        values = link if isinstance(link, dict) else link.__dict__
        return self._write(
            "INSERT INTO links(run_id, source_url, target_url, raw_href, kind, "
            "scope, line_no, anchor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                source_url,
                values["target_url"],
                values["raw_href"],
                values["kind"],
                values["scope"],
                values["line_no"],
                values.get("anchor", ""),
            ),
        )

    def insert_check(self, run_id: int, check: Any) -> bool:
        values = check if isinstance(check, dict) else check.__dict__
        chain = values.get("redirect_chain", [])
        return self._write(
            "INSERT INTO checks(run_id, url, final_url, status_code, "
            "redirect_chain_json, anchor_ok, ok, error, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                values["url"],
                values.get("final_url"),
                values.get("status_code"),
                json.dumps(chain, sort_keys=True),
                int(bool(values.get("anchor_ok", True))),
                int(bool(values.get("ok", False))),
                values.get("error"),
                values["checked_at"],
            ),
        )

    def save_crawl(self, run_id: int, pages: Iterable[Any]) -> None:
        for page in pages:
            self.insert_page(run_id, page)

    def save_links(self, run_id: int, pairs: Iterable[tuple[str, Any]]) -> None:
        for source_url, link in pairs:
            self.insert_link(run_id, source_url, link)

    def save_checks(self, run_id: int, checks: Iterable[Any]) -> None:
        for check in checks:
            self.insert_check(run_id, check)

    def get_run(self, run_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._decode_run(row) if row else None

    def list_runs(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
        return [self._decode_run(row) for row in rows]

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["config"] = json.loads(data.pop("config_json") or "{}")
        data["stats"] = json.loads(data.pop("stats_json") or "{}")
        return data

    def get_pages(self, run_id: int, status: int | None = None) -> list[dict]:
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM pages WHERE run_id = ? ORDER BY depth, url", (run_id,)
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM pages WHERE run_id = ? AND status_code = ? "
                "ORDER BY depth, url",
                (run_id, status),
            )
        return [dict(row) for row in rows]

    def get_page(self, page_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return dict(row) if row else None

    def get_links(self, run_id: int, source_url: str | None = None,
                  kind: str | None = None) -> list[dict]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if source_url is not None:
            clauses.append("source_url = ?")
            params.append(source_url)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        rows = self.conn.execute(
            f"SELECT * FROM links WHERE {' AND '.join(clauses)} "
            "ORDER BY source_url, line_no, id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_checks(self, run_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM checks WHERE run_id = ? ORDER BY url", (run_id,)
        ).fetchall()
        return [self._decode_check(row) for row in rows]

    def get_checks_map(self, run_id: int) -> dict[str, dict]:
        return {row["url"]: row for row in self.get_checks(run_id)}

    @staticmethod
    def _decode_check(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["redirect_chain"] = json.loads(data.pop("redirect_chain_json") or "[]")
        data["anchor_ok"] = bool(data["anchor_ok"])
        data["ok"] = bool(data["ok"])
        return data

    def broken_identities(self, run_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT DISTINCT l.source_url, l.target_url, c.status_code, c.error, "
            "c.anchor_ok FROM links l JOIN checks c "
            "ON c.run_id = l.run_id AND c.url = l.target_url "
            "WHERE l.run_id = ? AND c.ok = 0 "
            "ORDER BY l.source_url, l.target_url",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def redirect_records(self, run_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM checks WHERE run_id = ? AND redirect_chain_json != '[]' "
            "ORDER BY url",
            (run_id,),
        ).fetchall()
        return [self._decode_check(row) for row in rows]

    def set_baseline(self, name: str, run_id: int, force: bool = False) -> None:
        existing = self.get_baseline(name)
        if existing and not force:
            raise BaselineExistsError(f"baseline already exists: {name}")
        if self.get_run(run_id) is None:
            raise ValueError(f"run not found: {run_id}")
        self.conn.execute(
            "INSERT INTO baselines(name, run_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET run_id = excluded.run_id, "
            "created_at = excluded.created_at",
            (name, run_id, _now()),
        )
        self.conn.commit()

    def get_baseline(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM baselines WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def list_baselines(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM baselines ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def delete_baseline(self, name: str) -> bool:
        cursor = self.conn.execute("DELETE FROM baselines WHERE name = ?", (name,))
        self.conn.commit()
        return cursor.rowcount > 0

    # ---- archive / timeline -------------------------------------------------

    def begin_immediate(self) -> None:
        """Start one explicit write transaction owned by the caller."""
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def protected_run_ids(self) -> list[int]:
        """Every run id currently referenced by a named baseline."""
        rows = self.conn.execute(
            "SELECT DISTINCT run_id FROM baselines ORDER BY run_id"
        ).fetchall()
        return [int(row["run_id"]) for row in rows]

    def previous_run_id(self, run_id: int, seed: str | None = None) -> int | None:
        """The previous completed run, optionally restricted to one seed.

        Failed or still-running scans are skipped because their conclusions
        are not comparable: treating them as "no broken links" would fabricate
        fixes, and treating them as "all broken" would fabricate regressions.
        Archived runs participate through their timeline record, so a run
        archived earlier can still serve as the comparison baseline.
        """
        live_clause = "id < ? AND status = 'completed'"
        timeline_clause = "run_id < ? AND status = 'completed'"
        params: list[Any] = [run_id]
        if seed is not None:
            live_clause += " AND seed = ?"
            timeline_clause += " AND seed = ?"
            params.append(seed)
        rows = self.conn.execute(
            f"SELECT id FROM runs WHERE {live_clause} "
            f"UNION SELECT run_id FROM timeline_runs WHERE {timeline_clause} "
            "ORDER BY id DESC LIMIT 1",
            [*params, *params],
        ).fetchall()
        return int(rows[0][0]) if rows else None

    def broken_records_for_run(self, run_id: int) -> list[dict]:
        """One broken record per target URL for a live or archived run.

        A URL can be linked from many sources in one run; the first source
        (sorted order) is kept so the timeline stays one row per URL per run
        and results are identical before and after archiving.
        """
        if self.get_run(run_id) is not None:
            rows = self.broken_identities(run_id)
        else:
            rows = self.get_timeline_broken(run_id=run_id)
        by_target: dict[str, dict] = {}
        for row in sorted(rows, key=lambda item: (item["target_url"], item["source_url"])):
            url = str(row["target_url"])
            if url not in by_target:
                by_target[url] = {
                    "source_url": row["source_url"],
                    "target_url": url,
                    "status_code": row.get("status_code"),
                    "error": row.get("error"),
                }
        return [by_target[key] for key in sorted(by_target)]

    def run_detail_counts(self, run_id: int) -> dict:
        pages = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        links = self.conn.execute(
            "SELECT COUNT(*) FROM links WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        checks = self.conn.execute(
            "SELECT COUNT(*) FROM checks WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        return {"pages": int(pages), "links": int(links), "checks": int(checks)}

    def write_archive(self, entries: list[dict], run_ids: list[int]) -> None:
        """Insert timeline rows and delete run details inside an open transaction.

        The caller must wrap this in :meth:`begin_immediate` / :meth:`commit`
        (or :meth:`rollback`) so database changes are all-or-nothing.
        """
        for entry in entries:
            run_id = entry["run_id"]
            self.conn.execute(
                "INSERT OR REPLACE INTO timeline_runs("
                "run_id, seed, started_at, finished_at, status, checks_total, "
                "broken_total, new_broken_total, fixed_total, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    entry["seed"],
                    entry["started_at"],
                    entry.get("finished_at"),
                    entry["status"],
                    entry["checks_total"],
                    entry["broken_total"],
                    entry["new_broken_total"],
                    entry["fixed_total"],
                    entry["archived_at"],
                ),
            )
            self.conn.execute(
                "DELETE FROM timeline_broken WHERE run_id = ?", (run_id,)
            )
            self.conn.execute(
                "DELETE FROM timeline_fixed WHERE run_id = ?", (run_id,)
            )
            for row in entry["broken"]:
                self.conn.execute(
                    "INSERT OR IGNORE INTO timeline_broken("
                    "run_id, target_url, source_url, status_code, error, is_new) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        row["target_url"],
                        row["source_url"],
                        row.get("status_code"),
                        row.get("error"),
                        int(bool(row.get("is_new", False))),
                    ),
                )
            for row in entry["fixed"]:
                self.conn.execute(
                    "INSERT OR IGNORE INTO timeline_fixed("
                    "run_id, target_url, source_url) VALUES (?, ?, ?)",
                    (run_id, row["target_url"], row["source_url"]),
                )
        for run_id in run_ids:
            self.conn.execute("DELETE FROM pages WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM links WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM checks WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    def list_timeline_runs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM timeline_runs ORDER BY run_id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_all_run_records(self) -> list[dict]:
        """Live and archived run metadata ordered oldest first.

        Archived runs are those whose detail rows were removed but whose
        timeline record remains.
        """
        records: list[dict] = []
        live_ids: set[int] = set()
        for row in self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall():
            data = self._decode_run(row)
            live_ids.add(int(data["id"]))
            records.append(
                {
                    "run_id": int(data["id"]),
                    "seed": data["seed"],
                    "started_at": data["started_at"],
                    "finished_at": data.get("finished_at"),
                    "status": data["status"],
                    "checks_total": self.run_detail_counts(int(data["id"]))["checks"],
                    "broken_total": len(self.broken_urls_for_run(int(data["id"]))),
                    "archived": False,
                }
            )
        for row in self.conn.execute(
            "SELECT * FROM timeline_runs ORDER BY run_id"
        ).fetchall():
            data = dict(row)
            if int(data["run_id"]) in live_ids:
                continue
            records.append(
                {
                    "run_id": int(data["run_id"]),
                    "seed": data["seed"],
                    "started_at": data["started_at"],
                    "finished_at": data.get("finished_at"),
                    "status": data["status"],
                    "checks_total": int(data["checks_total"]),
                    "broken_total": int(data["broken_total"]),
                    "archived": True,
                }
            )
        records.sort(key=lambda item: item["run_id"])
        return records

    def get_timeline_run(self, run_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM timeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_timeline_broken(
        self, run_id: int | None = None, target_url: str | None = None,
        source_url: str | None = None,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if target_url is not None:
            clauses.append("target_url = ?")
            params.append(target_url)
        if source_url is not None:
            clauses.append("source_url = ?")
            params.append(source_url)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM timeline_broken{where} ORDER BY run_id, target_url",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_timeline_fixed(
        self, run_id: int | None = None, target_url: str | None = None,
        source_url: str | None = None,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if target_url is not None:
            clauses.append("target_url = ?")
            params.append(target_url)
        if source_url is not None:
            clauses.append("source_url = ?")
            params.append(source_url)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM timeline_fixed{where} ORDER BY run_id, target_url",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def broken_urls_for_run(self, run_id: int) -> set[str]:
        """Distinct broken target URLs for a live or archived run."""
        return {row["target_url"] for row in self.broken_records_for_run(run_id)}

