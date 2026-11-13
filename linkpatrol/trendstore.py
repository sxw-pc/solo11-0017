"""Persistent storage for offline trend reports in a separate SQLite file.

Nothing in this module ever opens the scan database for writing; the scan DB
is only read through :class:`~linkpatrol.trends.ScanArchive`.  Re-generating a
report for the same set of runs produces the same ``content_hash`` and
updates the existing row instead of creating a duplicate, so repeated runs
are idempotent and historical conclusions keep their identity.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TREND_SCHEMA = """
CREATE TABLE IF NOT EXISTS trend_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    seed TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    run_ids_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trend_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES trend_reports(id) ON DELETE CASCADE,
    entity_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    classification TEXT NOT NULL,
    alignment TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    observed_runs INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(report_id, entity_key, kind)
);
CREATE TABLE IF NOT EXISTS trend_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES trend_reports(id) ON DELETE CASCADE,
    entity_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    observed INTEGER NOT NULL,
    coverage TEXT NOT NULL,
    status_class TEXT,
    verdict TEXT,
    severity INTEGER,
    redirect_chain_length INTEGER,
    anchor_verdict TEXT,
    external_failures INTEGER,
    UNIQUE(report_id, entity_key, kind, run_id)
);
CREATE TABLE IF NOT EXISTS trend_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES trend_reports(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    entity_key TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_trend_entities_report ON trend_entities(report_id);
CREATE INDEX IF NOT EXISTS idx_trend_series_lookup
    ON trend_series(report_id, entity_key, kind);
CREATE INDEX IF NOT EXISTS idx_trend_events_report ON trend_events(report_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TrendStore:
    """Repository for trend artifacts, isolated from the scan Store."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(TREND_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TrendStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def find_by_hash(self, content_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM trend_reports WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        return self._decode_report(row) if row else None

    def save_report(self, payload: dict) -> tuple[int, bool]:
        """Insert or refresh a report.

        Returns ``(report_id, created)``; ``created`` is False when an
        identical report (same content hash) already existed and only its
        ``generated_at`` timestamp was refreshed.
        """
        content_hash = payload["content_hash"]
        existing = self.find_by_hash(content_hash)
        timestamp = _now()
        run_ids = [run["run_id"] for run in payload["runs"]]
        if existing is not None:
            self.conn.execute(
                "UPDATE trend_reports SET generated_at = ? WHERE id = ?",
                (timestamp, existing["id"]),
            )
            self.conn.commit()
            return existing["id"], False

        cursor = self.conn.execute(
            "INSERT INTO trend_reports(content_hash, seed, generated_at, "
            "parameters_json, run_ids_json, summary_json, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                content_hash,
                payload["seed"],
                timestamp,
                json.dumps(payload["parameters"], sort_keys=True),
                json.dumps(run_ids, sort_keys=True),
                json.dumps(payload["summary"], sort_keys=True),
                json.dumps(payload, sort_keys=True, ensure_ascii=False),
            ),
        )
        report_id = int(cursor.lastrowid)
        self._save_entities(report_id, payload)
        self._save_events(report_id, payload)
        self.conn.commit()
        return report_id, True

    def _save_entities(self, report_id: int, payload: dict) -> None:
        for kind, key in (("page", "entities"), ("anchor", "anchors")):
            for entity in payload[key]:
                self.conn.execute(
                    "INSERT INTO trend_entities(report_id, entity_key, kind, "
                    "classification, alignment, eligible, observed_runs, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        report_id,
                        entity["key"],
                        kind,
                        entity["classification"],
                        entity["alignment"],
                        int(entity["eligible"]),
                        entity["observed_runs"],
                        json.dumps(entity, sort_keys=True, ensure_ascii=False),
                    ),
                )
                for point in entity["series"]:
                    self.conn.execute(
                        "INSERT INTO trend_series(report_id, entity_key, kind, "
                        "run_id, observed, coverage, status_class, verdict, "
                        "severity, redirect_chain_length, anchor_verdict, "
                        "external_failures) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            report_id,
                            entity["key"],
                            kind,
                            point["run_id"],
                            int(point["observed"]),
                            point["coverage"],
                            point.get("status_class"),
                            point.get("verdict"),
                            point.get("severity"),
                            point.get("redirect_chain_length"),
                            point.get("anchor_verdict"),
                            point.get("external_failures", 0),
                        ),
                    )

    def _save_events(self, report_id: int, payload: dict) -> None:
        for bucket in ("appeared", "disappeared"):
            for event in payload["events"][bucket]:
                self.conn.execute(
                    "INSERT INTO trend_events(report_id, event_type, run_id, "
                    "entity_key, detail_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        report_id,
                        event["type"],
                        event["run_id"],
                        event["entity_key"],
                        json.dumps(event, sort_keys=True),
                    ),
                )
        for event in payload["events"]["anchor"]:
            self.conn.execute(
                "INSERT INTO trend_events(report_id, event_type, run_id, "
                "entity_key, detail_json) VALUES (?, ?, ?, ?, ?)",
                (
                    report_id,
                    event["type"],
                    event["run_id"],
                    event["entity_key"],
                    json.dumps(event, sort_keys=True),
                ),
            )

    def list_reports(self, seed: str | None = None) -> list[dict]:
        if seed is None:
            rows = self.conn.execute(
                "SELECT * FROM trend_reports ORDER BY id DESC").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trend_reports WHERE seed = ? ORDER BY id DESC",
                (seed,),
            ).fetchall()
        return [self._decode_report(row, include_payload=False) for row in rows]

    def get_report(self, report_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM trend_reports WHERE id = ?", (report_id,)).fetchone()
        return self._decode_report(row) if row else None

    def get_entities(self, report_id: int, kind: str | None = None,
                     classification: str | None = None) -> list[dict]:
        clauses = ["report_id = ?"]
        params: list[object] = [report_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if classification is not None:
            clauses.append("classification = ?")
            params.append(classification)
        rows = self.conn.execute(
            f"SELECT payload_json FROM trend_entities "
            f"WHERE {' AND '.join(clauses)} ORDER BY entity_key",
            params,
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_events(self, report_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT detail_json FROM trend_events WHERE report_id = ? "
            "ORDER BY run_id, entity_key",
            (report_id,),
        ).fetchall()
        return [json.loads(row["detail_json"]) for row in rows]

    @staticmethod
    def _decode_report(row: sqlite3.Row, include_payload: bool = True) -> dict:
        data = {
            "id": row["id"],
            "content_hash": row["content_hash"],
            "seed": row["seed"],
            "generated_at": row["generated_at"],
            "parameters": json.loads(row["parameters_json"]),
            "run_ids": json.loads(row["run_ids_json"]),
            "summary": json.loads(row["summary_json"]),
        }
        if include_payload:
            data["payload"] = json.loads(row["payload_json"])
        return data
