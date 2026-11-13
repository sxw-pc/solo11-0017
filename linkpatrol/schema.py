"""SQLite schema shared by the store and tests."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT NOT NULL DEFAULT '{}',
    batch_id INTEGER,
    site_name TEXT
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
CREATE TABLE IF NOT EXISTS batch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    max_concurrency INTEGER NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS batch_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batch_runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    config_path TEXT NOT NULL,
    host TEXT,
    status TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    error TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(batch_id, name)
);
CREATE INDEX IF NOT EXISTS idx_pages_run ON pages(run_id);
CREATE INDEX IF NOT EXISTS idx_links_run_source ON links(run_id, source_url);
CREATE INDEX IF NOT EXISTS idx_checks_run_ok ON checks(run_id, ok);
"""

# Additive migrations for databases created before batch support. Each entry
# is (table, column DDL fragment); columns already present are skipped, so old
# databases upgrade in place and legacy rows simply read back NULL.
RUNS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("runs", "batch_id INTEGER"),
    ("runs", "site_name TEXT"),
)

# Indexes that rely on the added columns. Created after the column migration
# so they work on both freshly created and upgraded databases.
BATCH_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_sites_batch ON batch_sites(batch_id)",
)
