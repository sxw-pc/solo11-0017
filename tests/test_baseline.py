"""Baseline classification, ignore rules, and exit-code tests."""

import pytest

from linkpatrol.baseline import (
    BaselineError, BrokenLink, IgnoreRule, IgnoreRules, compute_diff, diff_runs,
    exit_code_for, summary_text,
)

SOURCE = "http://127.0.0.1:5001/"

def link(target, status=404, source=SOURCE):
    return BrokenLink(source_url=source, target_url=target, status_code=status)

def test_new_fixed_and_persistent_classification():
    baseline = [link("http://127.0.0.1:5001/old"), link("http://127.0.0.1:5001/common")]
    current = [link("http://127.0.0.1:5001/new"), link("http://127.0.0.1:5001/common")]
    result = compute_diff(baseline, current)
    assert [item["target_url"] for item in result["new_broken"]] == [
        "http://127.0.0.1:5001/new"]
    assert [item["target_url"] for item in result["fixed"]] == [
        "http://127.0.0.1:5001/old"]
    assert [item["target_url"] for item in result["persistent"]] == [
        "http://127.0.0.1:5001/common"]

def test_ignore_rules_and_status_filter(tmp_path):
    path = tmp_path / "ignore.toml"
    path.write_text('[[ignore]]\nurl_regex = "ignored"\nstatus = [404, 410]\n')
    rules = IgnoreRules.load(path)
    result = compute_diff(
        [link("http://127.0.0.1:5001/common")],
        [link("http://127.0.0.1:5001/ignored"), link("http://127.0.0.1:5001/new"),
         link("http://127.0.0.1:5001/common")], rules)
    assert result["summary"]["new_broken"] == 1
    assert result["summary"]["ignored_new"] == 1 and len(result["persistent"]) == 1
    mismatch = IgnoreRules([IgnoreRule(url_regex="ignored", statuses=(500,))])
    assert compute_diff([], [link("http://127.0.0.1:5001/ignored")], mismatch)[
        "summary"]["new_broken"] == 1

def test_exit_codes_and_summary_text():
    diff = {"new_broken": [{"target_url": "x"}],
            "summary": {"new_broken": 2, "fixed": 1, "persistent": 3, "ignored_new": 0}}
    assert exit_code_for(diff, "new-broken") == 2
    assert exit_code_for(diff, "none") == 0
    assert exit_code_for({"new_broken": []}, "new-broken") == 0
    assert "new_broken=2" in summary_text(diff) and "persistent=3" in summary_text(diff)

def add_broken(store, run_id, target):
    store.insert_link(run_id, SOURCE, {
        "target_url": target, "raw_href": target, "kind": "page",
        "scope": "internal", "line_no": 1, "anchor": ""})
    store.insert_check(run_id, {
        "url": target, "final_url": target, "status_code": 404,
        "redirect_chain": [], "anchor_ok": True, "ok": False, "error": None,
        "checked_at": "2026-01-01T00:00:00+00:00"})

def test_diff_runs_and_missing_baseline(store):
    first, second = store.create_run(SOURCE, {}), store.create_run(SOURCE, {})
    add_broken(store, first, "http://127.0.0.1:5001/old")
    add_broken(store, second, "http://127.0.0.1:5001/new")
    store.set_baseline("prod", first)
    result = diff_runs(store, "prod", second)
    assert result["baseline_run"] == first
    assert [item["target_url"] for item in result["new_broken"]] == [
        "http://127.0.0.1:5001/new"]
    assert exit_code_for(result, "new-broken") == 2
    with pytest.raises(BaselineError):
        diff_runs(store, "absent", second)
