"""Offline inspection-trend analysis across the historical runs of one seed.

The module is strictly read-only with respect to the scan database: it opens
the SQLite file through a ``file:`` URI in ``mode=ro`` and never imports the
write-capable :class:`~linkpatrol.store.Store`.  Its output is a deterministic
report payload that can be persisted repeatedly without changing identity.

Identity model
--------------
A URL observed in a run has two keys:

* ``exact``  -- origin + path + query, i.e. the URL as the run's own
  ``normalize.drop_params`` rules canonicalised it.  This is the strongest
  identity but breaks when normalisation rules change between runs.
* ``structural`` -- origin + path only.  Stable across query/normalisation
  changes, but ambiguous when a run genuinely serves several pages that only
  differ by query string.

A structural key is merged into one :class:`TrendEntity` only when it maps to
a single exact URL across the whole selected run set.  When two distinct
exact URLs coexist under one structural key inside the same run the merge is
refused: each exact URL keeps its own entity (``alignment == "ambiguous"``)
rather than risking a wrong identity merge.  Anchor entities use
``(structural page key, fragment)`` and attach only to aligned page entities,
so an anchor that vanishes with a page restructure is reported instead of
being silently re-homed.

See ``docs/trends.md`` for the full metric and anomaly definitions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .urlnorm import is_http_url

SCHEMA_VERSION = 1

# Severity levels used by the deterioration / flapping classifier.
SEV_OK = 0
SEV_MINOR = 1
SEV_MAJOR = 2
SEV_CRITICAL = 3


class TrendError(ValueError):
    """Raised for invalid trend parameters or unusable run selections."""


# ---------------------------------------------------------------------------
# Read-only view of the scan database
# ---------------------------------------------------------------------------

class ScanArchive:
    """Read-only accessor for the pages/links/checks tables of a scan DB."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        if not path.is_file():
            raise FileNotFoundError(f"scan database not found: {path}")
        uri = f"file:{path.resolve()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ScanArchive":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def select_runs(
        self,
        seed: str,
        since: str | None = None,
        until: str | None = None,
        run_ids: set[int] | None = None,
        include_incomplete: bool = False,
    ) -> list[dict]:
        """Return the runs of one seed ordered by start time, then run id."""
        clauses = ["seed = ?"]
        params: list[object] = [seed]
        if not include_incomplete:
            clauses.append("status = 'completed'")
        if run_ids is not None:
            clauses.append(f"id IN ({', '.join('?' for _ in sorted(run_ids))})")
            params.extend(sorted(run_ids))
        rows = self.conn.execute(
            f"SELECT * FROM runs WHERE {' AND '.join(clauses)} "
            "ORDER BY started_at, id",
            params,
        ).fetchall()
        runs = [self._decode_run(row) for row in rows]
        if since is not None:
            runs = [run for run in runs if run["started_at"] >= since]
        if until is not None:
            runs = [run for run in runs if run["started_at"] <= until]
        return runs

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["config"] = json.loads(data.pop("config_json") or "{}")
        data["stats"] = json.loads(data.pop("stats_json") or "{}")
        return data

    def pages_map(self, run_id: int) -> dict[str, dict]:
        return {row["url"]: dict(row) for row in self.conn.execute(
            "SELECT * FROM pages WHERE run_id = ?", (run_id,))}

    def checks_map(self, run_id: int) -> dict[str, dict]:
        return {row["url"]: self._decode_check(row) for row in self.conn.execute(
            "SELECT * FROM checks WHERE run_id = ?", (run_id,))}

    @staticmethod
    def _decode_check(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["redirect_chain"] = json.loads(data.pop("redirect_chain_json") or "[]")
        data["anchor_ok"] = bool(data["anchor_ok"])
        data["ok"] = bool(data["ok"])
        return data

    def external_failures_by_source(self, run_id: int) -> dict[str, int]:
        """Count distinct broken external targets attributed to each source."""
        rows = self.conn.execute(
            "SELECT l.source_url AS source_url, "
            "COUNT(DISTINCT l.target_url) AS n "
            "FROM links l JOIN checks c "
            "ON c.run_id = l.run_id AND c.url = l.target_url "
            "WHERE l.run_id = ? AND l.scope = 'external' AND c.ok = 0 "
            "GROUP BY l.source_url",
            (run_id,),
        ).fetchall()
        return {row["source_url"]: int(row["n"]) for row in rows}

    def fragment_checks(self, run_id: int) -> list[dict]:
        """Checks whose URL carries a fragment (anchor verifications)."""
        rows = self.conn.execute(
            "SELECT * FROM checks WHERE run_id = ? AND url LIKE '%#%' ORDER BY url",
            (run_id,),
        ).fetchall()
        return [self._decode_check(row) for row in rows]


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def structural_key(url: str) -> str:
    """Return origin + path, dropping query and fragment.

    The input is already a URL normalised by the producing run, so only the
    query/fragment stripping is needed here.
    """
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def exact_key(url: str) -> str:
    """Return origin + path + query, dropping any fragment."""
    parsed = urlsplit(url)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def split_fragment(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    base = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    return base, parsed.fragment


def is_truncated(run: dict) -> bool:
    """A run is truncated when its own crawl limits left URLs unvisited."""
    crawl = run.get("stats", {}).get("crawl", {})
    return int(crawl.get("skipped_by_limits", 0) or 0) > 0


# ---------------------------------------------------------------------------
# Per-run observations
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """Every metric available for one exact URL in one run."""

    run_id: int
    url: str
    status_code: int | None
    status_class: str
    verdict: str
    severity: int
    redirect_chain_length: int | None
    anchor_verdict: str
    external_failures: int
    content_hash: str | None
    scope: str
    observed: bool

    def as_point(self) -> dict:
        return {
            "run_id": self.run_id,
            "observed": self.observed,
            "coverage": "observed",
            "status_code": self.status_code,
            "status_class": self.status_class,
            "verdict": self.verdict,
            "severity": self.severity,
            "redirect_chain_length": self.redirect_chain_length,
            "anchor_verdict": self.anchor_verdict,
            "external_failures": self.external_failures,
            "content_hash": self.content_hash,
        }


def status_class_of(status_code: int | None, error: str | None) -> str:
    """Classify a fetch outcome into the documented status classes."""
    if status_code is None:
        return "network_error" if error else "unknown"
    if 200 <= status_code < 300:
        return "2xx"
    if 300 <= status_code < 400:
        return "3xx"
    if 400 <= status_code < 500:
        return "4xx"
    if 500 <= status_code < 600:
        return "5xx"
    return "other"


def http_severity(status_code: int | None, error: str | None) -> int:
    cls = status_class_of(status_code, error)
    if cls in {"5xx", "network_error"}:
        return SEV_CRITICAL
    if cls == "4xx":
        return SEV_MAJOR
    return SEV_OK


def _page_scope(pages: dict[str, dict], checks: dict[str, dict], url: str) -> str:
    # Every internal URL is visited by the crawler and therefore has a page
    # row; URLs known only to checks are external link targets (the checks
    # table itself does not persist the scope column).
    if url in pages:
        return "internal"
    if url in checks:
        return "external"
    return "unknown"


def build_run_observations(archive: ScanArchive, run: dict) -> dict[str, Observation]:
    """Merge pages and checks of one run into per-exact-URL observations.

    A URL present in either table is observed.  Fragment-bearing checks
    describe an anchor on their base page, so they are folded into the base
    URL's observation rather than given a page identity; the anchor verdict
    itself is tracked separately by :func:`build_anchor_entities`.

    When both tables know a URL the check row wins for verdict/status/
    redirect (it is the verification result), while the page row supplies
    ``content_hash`` and reconciles missing check fields -- checks and pages
    can disagree because of retries and redirects, and neither table is
    assumed authoritative on its own.
    """
    run_id = run["run_id"] if "run_id" in run else run["id"]
    pages = archive.pages_map(run_id)
    checks = archive.checks_map(run_id)
    ext_fail = archive.external_failures_by_source(run_id)
    base_urls: set[str] = set()
    for url in set(pages) | set(checks):
        base_urls.add(split_fragment(url)[0])
    observations: dict[str, Observation] = {}
    for url in sorted(base_urls):
        page = pages.get(url)
        # A base-URL check plus any fragment checks of the same page share
        # one identity; the base check (if present) carries the verdict.
        check = checks.get(url)
        error = (check or {}).get("error") or (page or {}).get("error")
        status_code = None
        if check is not None and check.get("status_code") is not None:
            status_code = check["status_code"]
        elif page is not None and page.get("status_code") is not None:
            status_code = page["status_code"]
        if check is not None:
            verdict = "ok" if check["ok"] else "broken"
            # anchor_ok defaults to True: a 4xx/5xx/network failure leaves
            # anchors unevaluated, not broken.  Only an explicit
            # anchor_missing failure counts as an anchor verdict failure.
            if check["anchor_ok"] or not check["ok"]:
                anchor_verdict = "not_applicable"
            else:
                anchor_verdict = "fail"
            chain_len = len(check.get("redirect_chain") or [])
        else:
            ok = error is None and status_code is not None and 200 <= status_code < 300
            verdict = "ok" if ok else "broken"
            anchor_verdict = "not_applicable"
            chain_len = len(page.get("redirect_chain") or []) if page else 0
        severity = http_severity(status_code, error)
        if anchor_verdict == "fail":
            severity = max(severity, SEV_MAJOR)
        n_ext = ext_fail.get(url, 0)
        if n_ext:
            severity = max(severity, SEV_MINOR)
        content_hash = page.get("content_hash") if page else None
        observations[url] = Observation(
            run_id=run_id,
            url=url,
            status_code=status_code,
            status_class=status_class_of(status_code, error),
            verdict=verdict,
            severity=severity,
            redirect_chain_length=chain_len,
            anchor_verdict=anchor_verdict,
            external_failures=n_ext,
            content_hash=content_hash,
            scope=_page_scope(pages, checks, url),
            observed=True,
        )
    return observations


# ---------------------------------------------------------------------------
# Entity alignment
# ---------------------------------------------------------------------------

@dataclass
class TrendEntity:
    """One aligned URL identity with its per-run timeline."""

    key: str
    structural: str
    exact_urls: list[str]
    aliases: list[str]
    alignment: str  # "exact" | "structural" | "ambiguous"
    scope: str
    series: dict[int, dict] = field(default_factory=dict)
    first_seen_run: int | None = None
    last_seen_run: int | None = None
    observed_runs: int = 0
    eligible: bool = False
    classification: str = "unclassified"
    reasons: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    # Only populated for anchor entities.
    fragment: str = ""
    page_key: str = ""


def align_entities(
    run_observations: list[tuple[dict, dict[str, Observation]]],
) -> dict[str, TrendEntity]:
    """Align exact URLs of every run into stable trend entities.

    ``run_observations`` pairs each run row with its observations, in run
    order.  The returned mapping is keyed by the stable entity key.
    """
    # structural -> exact URL -> set of run ids where both were observed.
    groups: dict[str, dict[str, set[int]]] = {}
    scopes: dict[str, str] = {}
    for run, observations in run_observations:
        rid = run["id"]
        for url, obs in observations.items():
            struct = structural_key(url)
            groups.setdefault(struct, {}).setdefault(url, set()).add(rid)
            scopes.setdefault(url, obs.scope)

    entities: dict[str, TrendEntity] = {}
    for struct in sorted(groups):
        exact_urls = sorted(groups[struct])
        if len(exact_urls) == 1:
            url = exact_urls[0]
            key = struct
            alignment = "exact" if "?" not in url else "structural"
            entities[key] = TrendEntity(
                key=key,
                structural=struct,
                exact_urls=[url],
                aliases=sorted({url, struct}),
                alignment=alignment,
                scope=scopes.get(url, "unknown"),
            )
            continue
        # Several exact URLs share the structural key.  They may be merged
        # only when they never coexist in a single run (query-rule changes).
        coexist = False
        url_runs = [groups[struct][url] for url in exact_urls]
        for i in range(len(url_runs)):
            for j in range(i + 1, len(url_runs)):
                if url_runs[i] & url_runs[j]:
                    coexist = True
                    break
            if coexist:
                break
        if not coexist:
            entities[struct] = TrendEntity(
                key=struct,
                structural=struct,
                exact_urls=exact_urls,
                aliases=sorted(set(exact_urls) | {struct}),
                alignment="structural",
                scope=_dominant_scope(exact_urls, scopes),
            )
        else:
            # Ambiguous: keep separate exact-URL entities, never merge.
            for url in exact_urls:
                entities[exact_key(url)] = TrendEntity(
                    key=exact_key(url),
                    structural=struct,
                    exact_urls=[url],
                    aliases=[url],
                    alignment="ambiguous",
                    scope=scopes.get(url, "unknown"),
                )
    return entities


def _dominant_scope(exact_urls: list[str], scopes: dict[str, str]) -> str:
    counts: dict[str, int] = {}
    for url in exact_urls:
        scope = scopes.get(url, "unknown")
        counts[scope] = counts.get(scope, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def fill_timelines(
    entities: dict[str, TrendEntity],
    runs: list[dict],
    run_observations: list[tuple[dict, dict[str, Observation]]],
    min_occurrences: int,
) -> None:
    """Attach a series point to every entity for every run, filling gaps.

    Coverage of an unobserved entity in a run is decided precisely:

    * ``unscanned`` -- the run was truncated or interrupted *and* the
      entity's structural path was not among the URLs the run actually
      observed.  A truncated run still fully covered everything it fetched.
    * ``missing`` -- a complete run did not know the URL, or a truncated run
      had already observed another page under the same structural key (so
      the absence is meaningful rather than a cap artifact).
    """
    truncated = {run["id"]: is_truncated(run) for run in runs}
    incomplete = {run["id"]: run["status"] != "completed" for run in runs}
    obs_by_run = {run["id"]: observations for run, observations in run_observations}
    observed_structs = {
        rid: {structural_key(url) for url in observations}
        for rid, observations in obs_by_run.items()
    }

    def coverage_for(entity: TrendEntity, rid: int) -> str:
        if not (truncated[rid] or incomplete[rid]):
            return "missing"
        if entity.structural in observed_structs[rid]:
            return "missing"
        return "unscanned"

    def entity_urls_for_run(entity: TrendEntity, rid: int) -> list[str]:
        return [url for url in entity.exact_urls if url in obs_by_run[rid]]

    for entity in entities.values():
        seen = 0
        first_run: int | None = None
        last_run: int | None = None
        for run in runs:
            rid = run["id"]
            matched = entity_urls_for_run(entity, rid)
            if matched:
                # Deterministic choice if an ambiguous group ever collapsed
                # here: take the lexicographically smallest observed URL.
                url = sorted(matched)[0]
                point = obs_by_run[rid][url].as_point()
                entity.series[rid] = point
                seen += 1
                first_run = rid if first_run is None else first_run
                last_run = rid
            else:
                coverage = coverage_for(entity, rid)
                entity.series[rid] = {
                    "run_id": rid,
                    "observed": False,
                    "coverage": coverage,
                    "status_code": None,
                    "status_class": None,
                    "verdict": None,
                    "severity": None,
                    "redirect_chain_length": None,
                    "anchor_verdict": "unscanned" if coverage == "unscanned" else "missing",
                    "external_failures": 0,
                    "content_hash": None,
                }
        entity.observed_runs = seen
        entity.first_seen_run = first_run
        entity.last_seen_run = last_run
        entity.eligible = seen >= max(1, min_occurrences)


# ---------------------------------------------------------------------------
# Anchor entities
# ---------------------------------------------------------------------------

def build_anchor_entities(
    runs: list[dict],
    page_entities: dict[str, TrendEntity],
    archive: ScanArchive,
    min_occurrences: int,
) -> dict[str, TrendEntity]:
    """Align ``page#fragment`` checks across runs under their page entity."""
    truncated = {run["id"]: is_truncated(run) for run in runs}
    incomplete = {run["id"]: run["status"] != "completed" for run in runs}
    # (page structural key, fragment) -> run ids / states
    seen_runs: dict[tuple[str, str], set[int]] = {}
    states: dict[tuple[str, str], dict[int, str]] = {}
    for run in runs:
        rid = run["id"]
        for check in archive.fragment_checks(rid):
            base, fragment = split_fragment(check["url"])
            if not fragment:
                continue
            page_key = structural_key(base)
            if page_key not in page_entities:
                # The page itself could not be aligned; do not re-home the
                # anchor onto a different identity.
                page_key = f"unaligned:{page_key}"
            identity = (page_key, fragment)
            seen_runs.setdefault(identity, set()).add(rid)
            if check["ok"]:
                # A 2xx page: the fragment was explicitly verified.
                state = "ok" if check["anchor_ok"] else "fail"
            elif check.get("error") == "anchor_missing":
                # The page loaded fine; only the fragment target is absent.
                state = "fail"
            else:
                # The fetch itself failed, so the anchor could not be tested.
                state = "unevaluated"
            states.setdefault(identity, {})[rid] = state

    anchors: dict[str, TrendEntity] = {}
    for (page_key, fragment) in sorted(seen_runs):
        key = f"{page_key}#{fragment}"
        page_entity = page_entities.get(page_key)
        entity = TrendEntity(
            key=key,
            structural=page_key,
            exact_urls=[],
            aliases=[key],
            alignment="anchor",
            scope="anchor",
            fragment=fragment,
            page_key=page_key,
        )
        observed = 0
        for run in runs:
            rid = run["id"]
            if rid in states[(page_key, fragment)]:
                state = states[(page_key, fragment)][rid]
                entity.series[rid] = {
                    "run_id": rid,
                    "observed": True,
                    "coverage": "observed",
                    "anchor_verdict": state,
                }
                observed += 1
            elif page_entity is not None and page_entity.series[rid]["observed"]:
                # The page was fetched but no link to this fragment exists
                # in this run: the anchor target effectively disappeared.
                entity.series[rid] = {
                    "run_id": rid,
                    "observed": False,
                    "coverage": "missing",
                    "anchor_verdict": "missing",
                }
            else:
                # Mirror the page-level coverage so a truncated run never
                # masquerades as anchor removal.
                page_coverage = (
                    page_entity.series[rid]["coverage"]
                    if page_entity is not None and rid in page_entity.series
                    else ("unscanned" if (truncated[rid] or incomplete[rid]) else "missing")
                )
                entity.series[rid] = {
                    "run_id": rid,
                    "observed": False,
                    "coverage": page_coverage,
                    "anchor_verdict": "unscanned" if page_coverage == "unscanned" else "missing",
                }
        entity.observed_runs = observed
        entity.eligible = observed >= max(1, min_occurrences)
        anchors[key] = entity
    return anchors

# ---------------------------------------------------------------------------
# Classification and events
# ---------------------------------------------------------------------------

def classify_entity(entity: TrendEntity, ordered_run_ids: list[int]) -> None:
    """Assign one of the documented anomaly labels to an eligible entity.

    Flapping means the entity genuinely recovered to a healthy severity
    (zero) and then broke again.  Movement that stays in unhealthy territory
    (e.g. 5xx -> 4xx) is still deterioration, not oscillation.
    """
    if not entity.eligible:
        entity.classification = "below_threshold"
        return
    points = [entity.series[rid] for rid in ordered_run_ids if entity.series[rid]["observed"]]
    severities = [point["severity"] for point in points]
    transitions = list(zip(severities, severities[1:]))
    worsen = sum(1 for prev, cur in transitions if cur > prev)
    improve = sum(1 for prev, cur in transitions if cur < prev)
    recovered_to_healthy = any(
        prev > SEV_OK and cur == SEV_OK for prev, cur in transitions)
    first_sev, last_sev = severities[0], severities[-1]

    if recovered_to_healthy and worsen:
        entity.classification = "flapping"
        entity.reasons.append(
            "recovered to healthy at least once and then worsened again "
            f"({worsen} worsening / {improve} improving step(s))")
        return
    if worsen >= 1 and last_sev > first_sev and last_sev >= SEV_MINOR:
        # Any movement within unhealthy territory (e.g. 5xx -> 4xx) does not
        # undo the deterioration relative to the healthy baseline.
        entity.classification = "deteriorating"
        entity.reasons.append(
            f"severity rose from {first_sev} to {last_sev} without a healthy "
            f"recovery ({worsen} worsening step(s))")
        return
    if last_sev >= SEV_MINOR:
        entity.classification = "stable_unhealthy"
        entity.reasons.append(f"severity held at {last_sev} without recovery")
        return
    if improve and not worsen:
        entity.classification = "improving"
        entity.reasons.append(f"severity fell from {first_sev} to {last_sev}")
        return
    entity.classification = "stable_healthy"
    entity.reasons.append("no severity change across observed runs")


def classify_anchor(entity: TrendEntity, ordered_run_ids: list[int]) -> None:
    """Classify an anchor entity on ok/fail transitions alone."""
    if not entity.eligible:
        entity.classification = "below_threshold"
        return
    states = [
        entity.series[rid]["anchor_verdict"]
        for rid in ordered_run_ids
        if entity.series[rid]["observed"]
        and entity.series[rid]["anchor_verdict"] in {"ok", "fail"}
    ]
    if not states:
        entity.classification = "unclassified"
        entity.reasons.append("anchor was never evaluable in the selected runs")
        return
    numeric = [SEV_MAJOR if state == "fail" else SEV_OK for state in states]
    transitions = list(zip(numeric, numeric[1:]))
    worsen = sum(1 for prev, cur in transitions if cur > prev)
    improve = sum(1 for prev, cur in transitions if cur < prev)
    recovered = any(prev > SEV_OK and cur == SEV_OK for prev, cur in transitions)
    if recovered and worsen:
        entity.classification = "flapping"
        entity.reasons.append("anchor verdict recovered to ok then failed again")
    elif worsen and numeric[-1] >= SEV_MINOR:
        entity.classification = "deteriorating"
        entity.reasons.append("anchor verdict ended in fail after earlier ok")
    elif numeric[-1] >= SEV_MINOR:
        entity.classification = "stable_unhealthy"
        entity.reasons.append("anchor remained missing/failing")
    elif improve:
        entity.classification = "improving"
        entity.reasons.append("anchor verdict recovered to ok")
    else:
        entity.classification = "stable_healthy"
        entity.reasons.append("anchor verdict stayed ok")


def build_events(
    entity: TrendEntity,
    ordered_run_ids: list[int],
) -> list[dict]:
    """Derive appearance / disappearance / return events from coverage.

    An entity observed in the first selected run is treated as baseline and
    emits no ``appeared`` event -- appearance means "new relative to an
    earlier observation gap".  ``unscanned`` gaps (truncated/interrupted
    runs) never count as disappearance.
    """
    events: list[dict] = []
    previously_observed = False
    previously_missing = False
    first_id = ordered_run_ids[0] if ordered_run_ids else None
    for rid in ordered_run_ids:
        point = entity.series[rid]
        if point["observed"]:
            if not previously_observed and rid != first_id:
                events.append({
                    "type": "appeared" if not previously_missing else "returned",
                    "run_id": rid,
                    "entity_key": entity.key,
                })
            previously_observed = True
            previously_missing = False
        elif point["coverage"] == "missing":
            if previously_observed:
                events.append({
                    "type": "disappeared",
                    "run_id": rid,
                    "entity_key": entity.key,
                    "truncated": False,
                })
            previously_missing = True
            previously_observed = False
        # coverage == "unscanned" changes nothing: the URL was not visited,
        # so its presence/absence cannot be asserted.
    return events


def build_anchor_events(
    anchor: TrendEntity, ordered_run_ids: list[int]
) -> list[dict]:
    """Anchor-specific events, including a fragment going away entirely."""
    events: list[dict] = []
    last_state: str | None = None
    first_id = ordered_run_ids[0] if ordered_run_ids else None
    for rid in ordered_run_ids:
        point = anchor.series[rid]
        state = point.get("anchor_verdict")
        if state == "ok":
            if last_state == "fail":
                events.append({"type": "anchor_fixed", "run_id": rid,
                               "entity_key": anchor.key, "fragment": anchor.fragment})
        elif state == "fail":
            # A fail in the first observed run is the baseline condition,
            # not a breakage event.
            if last_state not in {None, "fail"} and rid != first_id:
                events.append({"type": "anchor_broke", "run_id": rid,
                               "entity_key": anchor.key, "fragment": anchor.fragment})
        elif state == "missing" and last_state in {"ok", "fail"}:
            events.append({"type": "anchor_gone", "run_id": rid,
                           "entity_key": anchor.key, "fragment": anchor.fragment})
        # "unscanned" and "unevaluated" never close the previous state.
        if state in {"ok", "fail", "missing"}:
            last_state = state
    return events


def redirect_growth(entity: TrendEntity, ordered_run_ids: list[int]) -> bool:
    """Flag a redirect chain that ends strictly longer than it started."""
    lengths = [
        entity.series[rid]["redirect_chain_length"]
        for rid in ordered_run_ids
        if entity.series[rid]["observed"]
        and entity.series[rid]["redirect_chain_length"] is not None
    ]
    return len(lengths) >= 2 and lengths[-1] > lengths[0]


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

@dataclass
class TrendConfig:
    seed: str
    since: str | None = None
    until: str | None = None
    run_ids: set[int] | None = None
    include_incomplete: bool = False
    min_occurrences: int = 2


def _entity_payload(entity: TrendEntity, ordered_run_ids: list[int]) -> dict:
    hashes = {
        point["content_hash"]
        for point in entity.series.values()
        if point.get("content_hash")
    }
    return {
        "key": entity.key,
        "structural_key": entity.structural,
        "exact_urls": entity.exact_urls,
        "aliases": entity.aliases,
        "alignment": entity.alignment,
        "scope": entity.scope,
        "fragment": entity.fragment,
        "page_key": entity.page_key,
        "first_seen_run": entity.first_seen_run,
        "last_seen_run": entity.last_seen_run,
        "observed_runs": entity.observed_runs,
        "eligible": entity.eligible,
        "classification": entity.classification,
        "reasons": entity.reasons,
        "redirect_chain_grew": redirect_growth(entity, ordered_run_ids),
        "content_changed": len(hashes) > 1,
        "events": entity.events,
        "series": [entity.series[rid] for rid in ordered_run_ids],
    }


def _anchor_payload(entity: TrendEntity, ordered_run_ids: list[int]) -> dict:
    """Anchor series only carry anchor verdicts, so page-only fields are omitted."""
    return {
        "key": entity.key,
        "structural_key": entity.structural,
        "exact_urls": [],
        "aliases": entity.aliases,
        "alignment": "anchor",
        "scope": "anchor",
        "fragment": entity.fragment,
        "page_key": entity.page_key,
        "first_seen_run": entity.first_seen_run,
        "last_seen_run": entity.last_seen_run,
        "observed_runs": entity.observed_runs,
        "eligible": entity.eligible,
        "classification": entity.classification,
        "reasons": entity.reasons,
        "events": entity.events,
        "series": [
            {
                "run_id": rid,
                "observed": entity.series[rid]["observed"],
                "coverage": entity.series[rid]["coverage"],
                "anchor_verdict": entity.series[rid]["anchor_verdict"],
            }
            for rid in ordered_run_ids
        ],
    }


def canonical_payload(payload: dict) -> bytes:
    """Deterministic serialisation used for identity hashing and JSON output."""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def identity_hash(payload: dict) -> str:
    """Hash everything that defines the result, excluding volatile metadata."""
    stable = {key: value for key, value in payload.items()
              if key not in {"generated_at", "report_id"}}
    return hashlib.sha256(canonical_payload(stable)).hexdigest()


def build_trend_report(archive: ScanArchive, config: TrendConfig) -> dict:
    """Build the full, deterministic trend report for one seed's runs."""
    if not is_http_url(config.seed):
        raise TrendError("seed must be an absolute http(s) URL")
    if config.min_occurrences < 1:
        raise TrendError("min_occurrences must be >= 1")
    if config.run_ids is not None and not config.run_ids:
        raise TrendError("run filter is empty")
    runs = archive.select_runs(
        config.seed,
        since=config.since,
        until=config.until,
        run_ids=config.run_ids,
        include_incomplete=config.include_incomplete,
    )
    if config.run_ids is not None:
        missing = sorted(config.run_ids - {run["id"] for run in runs})
        if missing:
            raise TrendError(
                f"runs not found for this seed/selection: {', '.join(map(str, missing))}")
    if len(runs) < 2:
        raise TrendError(
            "trend analysis needs at least two matching runs; "
            f"found {len(runs)}")

    run_observations = [(run, build_run_observations(archive, run)) for run in runs]
    entities = align_entities(run_observations)
    fill_timelines(entities, runs, run_observations, config.min_occurrences)
    anchors = build_anchor_entities(runs, entities, archive, config.min_occurrences)

    ordered_run_ids = [run["id"] for run in runs]
    truncated = {run["id"]: is_truncated(run) for run in runs}
    all_events: list[dict] = []
    for entity in entities.values():
        classify_entity(entity, ordered_run_ids)
        entity.events = build_events(entity, ordered_run_ids)
        all_events.extend(entity.events)
    anchor_events: list[dict] = []
    for anchor in anchors.values():
        classify_anchor(anchor, ordered_run_ids)
        anchor.events = build_anchor_events(anchor, ordered_run_ids)
        anchor_events.extend(anchor.events)

    appeared, disappeared = [], []
    for event in sorted(all_events, key=lambda item: (item["run_id"], item["entity_key"])):
        if event["type"] in {"appeared", "returned"}:
            appeared.append(event)
        elif event["type"] == "disappeared":
            disappeared.append(event)

    run_records = []
    drop_param_generations: dict[str, list[int]] = {}
    obs_counts = {run["id"]: len(obs) for run, obs in run_observations}
    for run in runs:
        drop = tuple(run.get("config", {}).get("normalize", {}).get("drop_params", []))
        label = ",".join(drop)
        drop_param_generations.setdefault(label, []).append(run["id"])
        run_records.append({
            "run_id": run["id"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "status": run["status"],
            "truncated": is_truncated(run),
            "drop_params": list(drop),
            "observed_urls": obs_counts[run["id"]],
        })

    entity_payloads = sorted(
        (_entity_payload(entity, ordered_run_ids) for entity in entities.values()),
        key=lambda item: item["key"],
    )
    anchor_payloads = sorted(
        (_anchor_payload(anchor, ordered_run_ids) for anchor in anchors.values()),
        key=lambda item: item["key"],
    )

    def count(label: str) -> int:
        return sum(1 for item in entity_payloads if item["classification"] == label)

    summary = {
        "runs_total": len(runs),
        "runs_completed": sum(1 for run in runs if run["status"] == "completed"),
        "runs_incomplete": sum(1 for run in runs if run["status"] != "completed"),
        "runs_truncated": sum(1 for flag in truncated.values() if flag),
        "entities_total": len(entity_payloads),
        "entities_eligible": sum(1 for item in entity_payloads if item["eligible"]),
        "anchor_entities_total": len(anchor_payloads),
        "deteriorating": count("deteriorating"),
        "flapping": count("flapping"),
        "stable_unhealthy": count("stable_unhealthy"),
        "improving": count("improving"),
        "stable_healthy": count("stable_healthy"),
        "below_threshold": count("below_threshold"),
        "appeared": len(appeared),
        "disappeared": len(disappeared),
        "ambiguous_entities": sum(
            1 for item in entity_payloads if item["alignment"] == "ambiguous"),
        "drop_param_generations": {
            label or "<none>": ids for label, ids in sorted(drop_param_generations.items())
        },
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "seed": config.seed,
        "generated_at": None,  # filled by the caller; excluded from hashing
        "parameters": {
            "since": config.since,
            "until": config.until,
            "run_ids": sorted(config.run_ids) if config.run_ids else None,
            "include_incomplete": config.include_incomplete,
            "min_occurrences": config.min_occurrences,
        },
        "summary": summary,
        "runs": run_records,
        "entities": entity_payloads,
        "anchors": anchor_payloads,
        "events": {
            "appeared": sorted(appeared, key=lambda item: (item["run_id"], item["entity_key"])),
            "disappeared": sorted(
                disappeared, key=lambda item: (item["run_id"], item["entity_key"])),
            "anchor": sorted(
                anchor_events, key=lambda item: (item["run_id"], item["entity_key"])),
        },
    }
    payload["content_hash"] = identity_hash(payload)
    return payload
