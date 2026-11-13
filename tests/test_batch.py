"""Batch scheduling: manifest parsing, global limiter, dedup, isolation."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from linkpatrol.batch import (
    BoundedFetcher,
    GlobalLimiter,
    load_manifest,
)
from linkpatrol.config import ConfigError, default_config
from linkpatrol.fetcher import Fetcher


def write_site_config(tmp_path: Path, name: str, seed: str, **limits) -> Path:
    config = default_config(seed)
    config["limits"].update({"max_pages": 10, "timeout_s": 2.0, "retries": 0})
    config["limits"].update(limits)
    config["output"]["database"] = str(tmp_path / "shared.db")
    config["output"]["dir"] = str(tmp_path / "reports")
    config["output"]["sitemap"] = str(tmp_path / f"{name}.xml")
    path = tmp_path / f"{name}.toml"
    lines = [
        f'seed = "{seed}"',
        "",
        "[limits]",
        f'max_depth = {config["limits"]["max_depth"]}',
        f'max_pages = {config["limits"]["max_pages"]}',
        f'timeout_s = {config["limits"]["timeout_s"]}',
        f'retries = {config["limits"]["retries"]}',
        f'delay_ms = {config["limits"]["delay_ms"]}',
        f'concurrency = {config["limits"]["concurrency"]}',
        f'max_redirects = {config["limits"]["max_redirects"]}',
        "",
        "[robots]",
        f'respect = {str(config["robots"]["respect"]).lower()}',
        f'user_agent = "{config["robots"]["user_agent"]}"',
        "",
        "[output]",
        f'dir = "{config["output"]["dir"]}"',
        f'database = "{config["output"]["database"]}"',
        f'sitemap = "{config["output"]["sitemap"]}"',
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "batch.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# GlobalLimiter / BoundedFetcher
# --------------------------------------------------------------------------


def test_global_limiter_bounds_inflight_and_records_peak():
    limiter = GlobalLimiter(2)
    inflight = 0
    peak = 0
    lock = threading.Lock()

    def worker():
        nonlocal inflight, peak
        limiter.acquire()
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        limiter.release()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 2
    assert limiter.peak_inflight == 2


def test_global_limiter_rejects_non_positive():
    with pytest.raises(ConfigError):
        GlobalLimiter(0)


class _RecordingFetcher(Fetcher):
    """Fetcher that sleeps inside fetch so concurrency is observable."""

    def __init__(self):
        super().__init__(timeout_s=2, retries=0, backoff_s=0)
        self.inflight = 0
        self.peak = 0
        self.lock = threading.Lock()

    def fetch(self, url, method="GET", max_body_bytes=None):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(0.04)
        with self.lock:
            self.inflight -= 1
        from linkpatrol.fetcher import FetchResult

        return FetchResult(requested_url=url, final_url=url, status_code=200)


def test_bounded_fetcher_funnels_every_request_through_limiter():
    limiter = GlobalLimiter(3)
    recorder = _RecordingFetcher()
    bounded = BoundedFetcher(recorder, limiter)
    assert isinstance(bounded, Fetcher)  # Crawler isinstance contract holds

    def worker():
        bounded.fetch("http://127.0.0.1:1/x")

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert limiter.peak_inflight <= 3
    assert recorder.peak <= 3


# --------------------------------------------------------------------------
# Manifest parsing and validation
# --------------------------------------------------------------------------


def test_manifest_loads_sites_and_merges_duplicate_hosts(tmp_path, mocksite, ext_site):
    write_site_config(tmp_path, "alpha", mocksite.base_url + "/")
    write_site_config(tmp_path, "alpha_dup", mocksite.base_url + "/about")
    write_site_config(tmp_path, "beta", ext_site.base_url + "/")
    manifest = write_manifest(
        tmp_path,
        f"""
[batch]
max_concurrency = 3
database = "{tmp_path / 'shared.db'}"

[[sites]]
name = "alpha"
config = "alpha.toml"

[[sites]]
name = "alpha_dup"
config = "alpha_dup.toml"

[[sites]]
name = "beta"
config = "beta.toml"
""",
    )
    loaded = load_manifest(manifest)
    assert loaded.max_concurrency == 3
    by_name = {entry.name: entry for entry in loaded.entries}
    assert by_name["alpha"].status == "pending"
    assert by_name["alpha_dup"].status == "skipped"
    assert by_name["alpha_dup"].merged_into == "alpha"
    # Same host key regardless of path.
    assert by_name["alpha"].host == by_name["alpha_dup"].host
    assert by_name["beta"].status == "pending"
    assert by_name["beta"].host != by_name["alpha"].host
    assert len(loaded.runnable) == 2


def test_manifest_duplicate_host_distinguishes_ports(tmp_path, mocksite, ext_site):
    write_site_config(tmp_path, "a", mocksite.base_url + "/")
    write_site_config(tmp_path, "b", ext_site.base_url + "/")
    manifest = write_manifest(
        tmp_path,
        """
[[sites]]
name = "a"
config = "a.toml"
[[sites]]
name = "b"
config = "b.toml"
""",
    )
    loaded = load_manifest(manifest)
    assert {entry.name for entry in loaded.runnable} == {"a", "b"}


def test_manifest_isolates_invalid_site_config(tmp_path, mocksite):
    write_site_config(tmp_path, "good", mocksite.base_url + "/")
    bad = tmp_path / "bad.toml"
    bad.write_text('seed = "ftp://127.0.0.1/nope"\n', encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        """
[[sites]]
name = "good"
config = "good.toml"
[[sites]]
name = "bad"
config = "bad.toml"
""",
    )
    loaded = load_manifest(manifest)
    by_name = {entry.name: entry for entry in loaded.entries}
    assert by_name["good"].status == "pending"
    assert by_name["bad"].status == "invalid"
    assert "seed" in by_name["bad"].error


def test_manifest_rejects_bad_structure(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError):
        load_manifest(missing)

    bad_toml = write_manifest(tmp_path, "this is = = not toml")
    with pytest.raises(ConfigError):
        load_manifest(bad_toml)

    unknown_key = write_manifest(
        tmp_path,
        """
[batch]
bogus = 1
[[sites]]
name = "x"
config = "x.toml"
""",
    )
    with pytest.raises(ConfigError):
        load_manifest(unknown_key)

    no_sites = write_manifest(tmp_path, '[batch]\nmax_concurrency = 2\n')
    with pytest.raises(ConfigError):
        load_manifest(no_sites)


def test_manifest_rejects_malformed_entries_and_types(tmp_path, mocksite):
    write_site_config(tmp_path, "a", mocksite.base_url + "/")
    cases = {
        "non_int_limit": '[batch]\nmax_concurrency = "two"\n[[sites]]\nname="a"\nconfig="a.toml"\n',
        "bool_limit": '[batch]\nmax_concurrency = true\n[[sites]]\nname="a"\nconfig="a.toml"\n',
        "unknown_top": 'bogus = 1\n[[sites]]\nname="a"\nconfig="a.toml"\n',
        "unknown_batch_key": '[batch]\nmax_concurrency = 2\nnope = 1\n[[sites]]\nname="a"\nconfig="a.toml"\n',
        "empty_database": '[batch]\ndatabase = ""\n[[sites]]\nname="a"\nconfig="a.toml"\n',
        "site_unknown_key": '[[sites]]\nname="a"\nconfig="a.toml"\nextra=1\n',
        "missing_name": '[[sites]]\nconfig="a.toml"\n',
        "missing_config": '[[sites]]\nname="a"\n',
    }
    for label, body in cases.items():
        path = tmp_path / f"{label}.toml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_manifest(path)


def test_manifest_rejects_duplicate_names_and_bad_concurrency(tmp_path, mocksite):
    write_site_config(tmp_path, "a", mocksite.base_url + "/")
    dup_name = write_manifest(
        tmp_path,
        """
[[sites]]
name = "a"
config = "a.toml"
[[sites]]
name = "a"
config = "a.toml"
""",
    )
    with pytest.raises(ConfigError):
        load_manifest(dup_name)

    bad_limit = write_manifest(
        tmp_path,
        """
[batch]
max_concurrency = 0
[[sites]]
name = "a"
config = "a.toml"
""",
    )
    with pytest.raises(ConfigError):
        load_manifest(bad_limit)


def test_manifest_config_paths_resolve_relative_to_manifest(tmp_path, mocksite):
    sub = tmp_path / "configs"
    sub.mkdir()
    cfg = write_site_config(sub, "a", mocksite.base_url + "/")
    manifest = tmp_path / "batch.toml"
    manifest.write_text(
        """
[[sites]]
name = "a"
config = "configs/a.toml"
""",
        encoding="utf-8",
    )
    loaded = load_manifest(manifest)
    assert loaded.entries[0].config_path == str(cfg.resolve())


def test_resolve_database_requires_agreement_without_override(tmp_path, mocksite, ext_site):
    from linkpatrol.batch import _resolve_database

    # Distinct per-site databases and no batch.database -> must refuse.
    write_site_config(tmp_path, "a", mocksite.base_url + "/")
    b_path = tmp_path / "b.toml"
    b_path.write_text(
        f'seed = "{ext_site.base_url}/"\n[output]\n'
        f'dir = "{tmp_path / "r"}"\ndatabase = "{tmp_path / "other.db"}"\n'
        f'sitemap = "{tmp_path / "b.xml"}"\n',
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path,
        """
[[sites]]
name = "a"
config = "a.toml"
[[sites]]
name = "b"
config = "b.toml"
""",
    )
    loaded = load_manifest(manifest)
    assert loaded.database is None
    with pytest.raises(ConfigError):
        _resolve_database(loaded)

    # When all sites agree, that single database is returned.
    b_path.write_text(
        f'seed = "{ext_site.base_url}/"\n[output]\n'
        f'dir = "{tmp_path / "r"}"\ndatabase = "{tmp_path / "shared.db"}"\n'
        f'sitemap = "{tmp_path / "b.xml"}"\n',
        encoding="utf-8",
    )
    agreed = load_manifest(manifest)
    assert _resolve_database(agreed) == str(tmp_path / "shared.db")


def test_format_summary_lists_every_site():
    from linkpatrol.batch import BatchResult, format_summary

    result = BatchResult(
        batch_id=7,
        status="partial",
        exit_code=4,
        counts={"total": 3, "completed": 1, "failed": 1, "skipped": 1, "invalid": 0},
        sites=[
            {"name": "ok", "status": "completed", "run_id": 11, "host": "h:1", "error": None},
            {"name": "bad", "status": "failed", "run_id": 12, "host": "h:2", "error": "boom"},
            {"name": "dup", "status": "skipped", "run_id": None, "host": "h:1", "error": "dup"},
        ],
        peak_inflight=2,
        max_concurrency=3,
    )
    text = format_summary(result)
    assert "batch 7: partial" in text
    assert "peak_inflight=2" in text
    assert "ok" in text and "bad" in text and "dup" in text
    assert "run=-" in text  # skipped site has no run id
