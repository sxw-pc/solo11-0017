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
