"""SQLite schema shared by the store and tests."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    depth INTEGER NOT NULL,
    status_code INTEGER,
    content_hash TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    error TEXT,
    UNIQUE(run_id, url)
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    target_url TEXT NOT NULL,
    raw_href TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    anchor TEXT NOT NULL DEFAULT '',
    UNIQUE(run_id, source_url, target_url, kind)
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    final_url TEXT,
    status_code INTEGER,
    redirect_chain_json TEXT NOT NULL DEFAULT '[]',
    anchor_ok INTEGER NOT NULL DEFAULT 1,
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    checked_at TEXT NOT NULL,
    UNIQUE(run_id, url)
);
CREATE TABLE IF NOT EXISTS baselines (
    name TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target_url TEXT NOT NULL,
    phase TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    first_failure_code INTEGER,
    final_failure_code INTEGER,
    budget_skipped INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    UNIQUE(run_id, target_url, phase)
);
CREATE INDEX IF NOT EXISTS idx_pages_run ON pages(run_id);
CREATE INDEX IF NOT EXISTS idx_links_run_source ON links(run_id, source_url);
CREATE INDEX IF NOT EXISTS idx_checks_run_ok ON checks(run_id, ok);
CREATE INDEX IF NOT EXISTS idx_attempt_details_run ON attempt_details(run_id);
"""
