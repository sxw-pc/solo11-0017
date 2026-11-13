"""Baseline diffing, versioned ignore rules, and CI-oriented exit decisions."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .schema import IMPLICIT_FINGERPRINT, IMPLICIT_VERSION_ID
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

@dataclass(frozen=True)
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

    def canonical(self) -> dict:
        """A plain-data, order-stable representation suitable for hashing/storage."""
        entry = {"url_regex": self.url_regex}
        if self.statuses is not None:
            entry["status"] = sorted(self.statuses)
        return entry

    @classmethod
    def from_canonical(cls, entry: dict) -> "IgnoreRule":
        statuses = entry.get("status")
        return cls(
            url_regex=entry["url_regex"],
            statuses=tuple(statuses) if statuses is not None else None,
        )

class IgnoreRules:
    """A list of ignore rules loaded from TOML or reconstructed from a stored version."""

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
                    isinstance(item, int) and not isinstance(item, bool)
                    for item in statuses
                ):
                    raise BaselineError("ignore status must be a list of integers")
                statuses = tuple(statuses)
            rules.append(IgnoreRule(url_regex=raw["url_regex"], statuses=statuses))
        return cls(rules)

    @classmethod
    def from_canonical(cls, entries: list[dict]) -> "IgnoreRules":
        return cls([IgnoreRule.from_canonical(entry) for entry in entries])

    def canonical_entries(self) -> list[dict]:
        """Semantically normalised rules: duplicate entries collapsed, then sorted.

        Two files with the same rules in a different order produce the same
        canonical list, so their fingerprints match and no new version is created.
        """
        unique = sorted(
            {json.dumps(rule.canonical(), sort_keys=True) for rule in self.rules}
        )
        return [json.loads(item) for item in unique]

    def fingerprint(self) -> str:
        """Stable sha256 of the canonical rule set (order-insensitive)."""
        payload = json.dumps(self.canonical_entries(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ignored(self, link: BrokenLink) -> bool:
        return any(rule.matches(link) for rule in self.rules)

def register_ignore_version(store: Store, rules: IgnoreRules) -> int:
    """Persist ``rules`` as a version (if new) and return its version id."""
    entries = rules.canonical_entries()
    return store.ensure_ignore_version(rules.fingerprint(), entries)

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

def _version_info(version: dict | None) -> dict:
    if version is None:
        return {
            "id": None,
            "fingerprint": None,
            "rule_count": 0,
            "implicit": True,
        }
    return {
        "id": version["id"],
        "fingerprint": version["fingerprint"],
        "rule_count": version["rule_count"],
        "implicit": version["id"] == IMPLICIT_VERSION_ID,
    }

def _resolve_rules(store: Store, run_id: int) -> tuple[IgnoreRules, dict]:
    """Load the ignore rules and metadata bound to a run, tolerating old data."""
    version_id = store.get_run_ignore_version_id(run_id)
    version = store.get_ignore_version(version_id)
    if version is None:
        # A run older than the ignore_versions table with no migrated pointer.
        return IgnoreRules(), {
            "id": None,
            "fingerprint": IMPLICIT_FINGERPRINT,
            "rule_count": 0,
            "implicit": True,
        }
    return IgnoreRules.from_canonical(version["rules"]), _version_info(version)

def compute_diff(
    baseline_links: Iterable[BrokenLink],
    current_links: Iterable[BrokenLink],
    ignore: IgnoreRules | None = None,
    baseline_ignore: IgnoreRules | None = None,
    current_ignore: IgnoreRules | None = None,
) -> dict:
    """Classify broken-link identities into new, fixed, and persistent.

    Each side is filtered with the ignore rules that were in effect when its run
    was created. The legacy single-``ignore`` argument applies the same rules to
    both sides and stays supported for callers without versioned data.
    """
    before_ignore = baseline_ignore if baseline_ignore is not None else (
        ignore or IgnoreRules()
    )
    after_ignore = current_ignore if current_ignore is not None else (
        ignore or IgnoreRules()
    )
    before = _by_identity(baseline_links)
    after = _by_identity(current_links)
    kept_before = {
        identity: link
        for identity, link in before.items()
        if not before_ignore.ignored(link)
    }
    kept_after = {
        identity: link
        for identity, link in after.items()
        if not after_ignore.ignored(link)
    }
    new_candidates = [
        link for identity, link in kept_after.items() if identity not in kept_before
    ]
    new = sorted(new_candidates, key=lambda link: link.identity)
    # Broken links in the current run that its own ignore rules suppress.
    ignored_new = sum(1 for link in after.values() if after_ignore.ignored(link))
    fixed = sorted(
        (link for identity, link in kept_before.items() if identity not in kept_after),
        key=lambda link: link.identity,
    )
    persistent = sorted(
        (link for identity, link in kept_after.items() if identity in kept_before),
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
            "ignored_new": ignored_new,
        },
    }

def diff_runs(
    store: Store,
    baseline_name: str,
    run_id: int,
    ignore: IgnoreRules | None = None,
) -> dict:
    """Compare one run against a named baseline using each run's rule version."""
    baseline = store.get_baseline(baseline_name)
    if not baseline:
        raise BaselineError(f"baseline not found: {baseline_name}")
    baseline_run_id = baseline["run_id"]
    baseline_rules, baseline_version = _resolve_rules(store, baseline_run_id)
    current_rules, current_version = _resolve_rules(store, run_id)
    notes: list[str] = []
    override = ignore is not None
    if override:
        current_rules = ignore
        notes.append(
            "current run evaluated with the --ignore override instead of its "
            f"stored ignore version {current_version['id']}"
        )
    if baseline_version["id"] != current_version["id"]:
        notes.append(
            "ignore rule versions differ: baseline run "
            f"{baseline_run_id} uses version {baseline_version['id']} "
            f"({baseline_version['rule_count']} rules), current run {run_id} uses "
            f"version {current_version['id']} ({current_version['rule_count']} rules); "
            "each side was judged with its own version"
        )
    if baseline_version["implicit"] or (current_version["implicit"] and not override):
        notes.append(
            "one side predates persisted ignore rules and uses the implicit v0 "
            "empty rule set, so all of its broken links remain reportable"
        )
    baseline_links = broken_from_store(store, baseline_run_id)
    current_links = broken_from_store(store, run_id)
    result = compute_diff(
        baseline_links,
        current_links,
        baseline_ignore=baseline_rules,
        current_ignore=current_rules,
    )
    result["baseline"] = baseline_name
    result["baseline_run"] = baseline_run_id
    result["run"] = run_id
    result["baseline_ignore_version"] = baseline_version
    result["current_ignore_version"] = current_version
    result["notes"] = notes
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
