#!/usr/bin/env python3
"""Offline trend demo: run several scans of the built-in demo site and
build a trend report that exhibits deterioration, flapping, identity
alignment, truncation, and appearance/disappearance events.

The script starts the demo Flask app on an ephemeral local port, performs
four scans while changing injected faults and normalisation rules between
them, then generates the trend artifact.  Everything stays on 127.0.0.1 and
no existing scan write path is modified -- the scan command itself is used.

Usage (from the repository root, with dependencies installed)::

    .venv/bin/python scripts/demo_trends.py
    .venv/bin/python scripts/demo_trends.py --keep-db   # leave out/ behind
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from werkzeug.serving import make_server  # noqa: E402

from linkpatrol.cli import main  # noqa: E402
from linkpatrol.demoapp import create_demo_app  # noqa: E402

# (label, injected faults as a JSON-encoded env value, drop_params, max_pages)
PHASES = [
    # Healthy baseline.
    ("baseline", None, ["utm_*", "fbclid", "campaign"], 60),
    # /docs starts failing (500): first worsening step, later it recovers,
    # which makes /docs a flapping entity.
    ("docs-500", json.dumps({"/docs": 500}), ["utm_*", "fbclid", "campaign"], 60),
    # /docs recovers to 404 then back to healthy below; /about starts a
    # persistent 404 that never recovers -> deteriorating entity.
    ("about-404-docs-flap", json.dumps({"/docs": 404, "/about": 404}),
     ["utm_*", "fbclid", "campaign"], 60),
    # Normalisation rule change: campaign is no longer dropped, so the
    # /landing?campaign=home and /docs?campaign=footer links get new exact
    # identities; structural alignment must merge the landing entity while
    # the docs group stays ambiguous (both query variants coexist).
    # The run is also truncated by max_pages, so unvisited URLs must be
    # recorded as unscanned rather than disappeared.
    ("rule-change-truncated", json.dumps({"/about": 404, "/blog/post-1": 500}),
     ["utm_*", "fbclid"], 6),
]


def write_config(path: Path, seed: str, database: Path, drop_params: list[str],
                 max_pages: int) -> Path:
    path.write_text(
        f'''
seed = "{seed}/"
[scope]
exclude = ["^/search", "^/private/"]
[limits]
max_depth = 3
max_pages = {max_pages}
timeout_s = 3.0
retries = 2
delay_ms = 0
concurrency = 1
max_redirects = 5
[robots]
respect = true
user_agent = "linkpatrol-demo/0.1"
[external]
check = true
[normalize]
drop_params = {json.dumps(drop_params)}
[output]
dir = "{database.parent / 'reports'}"
database = "{database}"
sitemap = "{database.parent / 'sitemap.xml'}"
'''.lstrip(),
        encoding="utf-8",
    )
    return path


def start_demo() -> tuple[object, str]:
    server = make_server("127.0.0.1", 0, create_demo_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-db", action="store_true",
                        help="keep artifacts under out/trend-demo/ instead of a temp dir")
    args = parser.parse_args()

    if args.keep_db:
        work = REPO_ROOT / "out" / "trend-demo"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        cleanup = False
    else:
        work = Path(tempfile.mkdtemp(prefix="linkpatrol-trend-demo-"))
        cleanup = True

    database = work / "linkpatrol.db"
    trend_db = work / "trends.db"
    json_out = work / "trend.json"
    server, seed = start_demo()
    previous_faults = os.environ.get("LINKPATROL_DEMO_FAULTS")
    try:
        for index, (label, faults, drop_params, max_pages) in enumerate(PHASES, start=1):
            if faults is None:
                os.environ.pop("LINKPATROL_DEMO_FAULTS", None)
            else:
                os.environ["LINKPATROL_DEMO_FAULTS"] = faults
            config_path = write_config(
                work / f"scan-{index}.toml", seed, database, drop_params, max_pages)
            code = main(["scan", "--config", str(config_path)])
            if code != 0:
                print(f"scan phase {label} failed with exit code {code}", file=sys.stderr)
                return code
            print(f"--- phase {index} ({label}) complete ---")
        os.environ.pop("LINKPATROL_DEMO_FAULTS", None)
        code = main([
            "trend", "generate",
            "--db", str(database),
            "--trend-db", str(trend_db),
            "--seed", seed + "/",
            "--min-occurrences", "2",
            "--json-out", str(json_out),
        ])
        if code != 0:
            return code
        print(f"trend database: {trend_db}")
        print(f"trend JSON:     {json_out}")
        print("inspect with: python -m linkpatrol trend list --trend-db", trend_db)
    finally:
        server.shutdown()
        server.server_close()
        if previous_faults is not None:
            os.environ["LINKPATROL_DEMO_FAULTS"] = previous_faults
        else:
            os.environ.pop("LINKPATROL_DEMO_FAULTS", None)
        if cleanup:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
