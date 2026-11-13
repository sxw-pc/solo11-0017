"""SQLite schema shared by the store and tests."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS ignore_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    rules_json TEXT NOT NULL,
    rule_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT NOT NULL DEFAULT '{}',
    ignore_version_id INTEGER NOT NULL DEFAULT 0
        REFERENCES ignore_versions(id) ON DELETE RESTRICT
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
CREATE INDEX IF NOT EXISTS idx_pages_run ON pages(run_id);
CREATE INDEX IF NOT EXISTS idx_links_run_source ON links(run_id, source_url);
CREATE INDEX IF NOT EXISTS idx_checks_run_ok ON checks(run_id, ok);
"""

# The implicit version every pre-versioning run is attributed to during migration.
# It carries an empty rule set: historical runs predate persisted ignore rules, so
# nothing can be reconstructed for them and every broken link remains reportable.
IMPLICIT_VERSION_ID = 0
IMPLICIT_FINGERPRINT = "v0-implicit-empty"
