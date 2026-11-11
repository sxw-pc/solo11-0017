"""Baseline diffing, ignore rules, and CI-oriented exit decisions."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .store import Store

class BaselineError(ValueError):
    """Raised for invalid ignore rules or a missing baseline."""

@dataclass(frozen=True)
class BrokenLink:
    """The stable identity of a broken link."""

    source_url: str
    target_url: str
    status_code: int | None = None
    error: str | None = None
    anchor_ok: bool = True

    @property
    def identity(self) -> tuple[str, str]:
        return self.source_url, self.target_url

    def as_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "target_url": self.target_url,
            "status_code": self.status_code,
            "error": self.error,
            "anchor_ok": self.anchor_ok,
        }

@dataclass
class IgnoreRule:
    """One [[ignore]] TOML entry."""

    url_regex: str
    statuses: tuple[int, ...] | None = None

    def matches(self, link: BrokenLink) -> bool:
        if self.statuses is not None and link.status_code not in self.statuses:
            return False
        return bool(
            re.search(self.url_regex, link.source_url)
            or re.search(self.url_regex, link.target_url)
        )

class IgnoreRules:
    """A list of ignore rules loaded from TOML."""

    def __init__(self, rules: list[IgnoreRule] | None = None) -> None:
        self.rules = rules or []

    @classmethod
    def load(cls, path: str | Path | None) -> "IgnoreRules":
        if path is None:
            return cls()
        rule_path = Path(path)
        if not rule_path.is_file():
            raise BaselineError(f"ignore file not found: {rule_path}")
        try:
            with rule_path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise BaselineError(f"invalid ignore TOML: {exc}") from exc
        raw_rules = data.get("ignore", [])
        if not isinstance(raw_rules, list):
            raise BaselineError("ignore file must contain [[ignore]] entries")
        rules: list[IgnoreRule] = []
        for raw in raw_rules:
            if not isinstance(raw, dict) or not isinstance(raw.get("url_regex"), str):
                raise BaselineError("each ignore entry requires a string url_regex")
            try:
                re.compile(raw["url_regex"])
            except re.error as exc:
                raise BaselineError(f"invalid ignore regex: {raw['url_regex']}: {exc}") from exc
            statuses = raw.get("status")
            if statuses is not None:
                if not isinstance(statuses, list) or not all(
                    isinstance(item, int) for item in statuses
                ):
                    raise BaselineError("ignore status must be a list of integers")
                statuses = tuple(statuses)
            rules.append(IgnoreRule(url_regex=raw["url_regex"], statuses=statuses))
        return cls(rules)

    def ignored(self, link: BrokenLink) -> bool:
        return any(rule.matches(link) for rule in self.rules)

def broken_from_store(store: Store, run_id: int) -> list[BrokenLink]:
    """Load broken links from the joined ``links``/``checks`` tables."""
    return [
        BrokenLink(
            source_url=row["source_url"],
            target_url=row["target_url"],
            status_code=row["status_code"],
            error=row["error"],
            anchor_ok=bool(row["anchor_ok"]),
        )
        for row in store.broken_identities(run_id)
    ]

def _by_identity(links: Iterable[BrokenLink]) -> dict[tuple[str, str], BrokenLink]:
    return {link.identity: link for link in links}

def compute_diff(
    baseline_links: Iterable[BrokenLink],
    current_links: Iterable[BrokenLink],
    ignore: IgnoreRules | None = None,
) -> dict:
    """Classify broken-link identities into new, fixed, and persistent."""
    ignore = ignore or IgnoreRules()
    before = _by_identity(baseline_links)
    after = _by_identity(current_links)
    new_candidates = [
        link for identity, link in after.items() if identity not in before
    ]
    new = sorted(
        (link for link in new_candidates if not ignore.ignored(link)),
        key=lambda link: link.identity,
    )
    fixed = sorted(
        (link for identity, link in before.items() if identity not in after),
        key=lambda link: link.identity,
    )
    persistent = sorted(
        (link for identity, link in after.items() if identity in before),
        key=lambda link: link.identity,
    )
    return {
        "new_broken": [link.as_dict() for link in new],
        "fixed": [link.as_dict() for link in fixed],
        "persistent": [link.as_dict() for link in persistent],
        "summary": {
            "new_broken": len(new),
            "fixed": len(fixed),
            "persistent": len(persistent),
            "ignored_new": len(new_candidates) - len(new),
        },
    }

def diff_runs(
    store: Store,
    baseline_name: str,
    run_id: int,
    ignore: IgnoreRules | None = None,
) -> dict:
    """Compare one run against a named baseline stored in SQLite."""
    baseline = store.get_baseline(baseline_name)
    if not baseline:
        raise BaselineError(f"baseline not found: {baseline_name}")
    baseline_links = broken_from_store(store, baseline["run_id"])
    current_links = broken_from_store(store, run_id)
    result = compute_diff(baseline_links, current_links, ignore)
    result["baseline"] = baseline_name
    result["baseline_run"] = baseline["run_id"]
    result["run"] = run_id
    return result

def exit_code_for(diff: dict, fail_on: str | None) -> int:
    """Return 2 when new breakage is present and requested, else 0."""
    if fail_on == "new-broken" and diff.get("new_broken"):
        return 2
    return 0

def summary_text(diff: dict) -> str:
    """One-line CI-friendly summary of a diff."""
    summary = diff.get("summary", {})
    return (
        f"new_broken={summary.get('new_broken', 0)} "
        f"fixed={summary.get('fixed', 0)} "
        f"persistent={summary.get('persistent', 0)} "
        f"ignored_new={summary.get('ignored_new', 0)}"
    )
