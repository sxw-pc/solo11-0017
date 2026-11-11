"""robots.txt parsing and a per-origin policy cache."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from .urlnorm import origin

@dataclass(frozen=True)
class RobotsRule:
    """One Allow or Disallow directive."""

    allow: bool
    pattern: str
    regex: re.Pattern[str]

@dataclass
class RobotsPolicy:
    """The rules applicable to our user agent."""

    rules: list[RobotsRule] = field(default_factory=list)

    def allowed(self, url_path: str) -> bool:
        """Return True when the longest matching rule permits the path.

        An Allow rule wins an equal-length tie. With no matching rule the URL
        is allowed. An empty Disallow value is not a rule and therefore allows
        everything.
        """
        path = url_path or "/"
        best: RobotsRule | None = None
        for rule in self.rules:
            if not rule.regex.match(path):
                continue
            if best is None:
                best = rule
                continue
            if len(rule.pattern) > len(best.pattern):
                best = rule
            elif len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow:
                best = rule
        return True if best is None else best.allow

def _compile_pattern(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = re.escape(body).replace(r"\*", ".*")
    expression = "^" + expression + ("$" if anchored else ".*")
    return re.compile(expression)

def _groups(text: str) -> list[tuple[list[str], list[tuple[bool, str]]]]:
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, value = line.split(":", 1)
        field_name = field_name.strip().lower()
        value = value.strip()
        if field_name == "user-agent":
            if rules and agents:
                groups.append((agents, rules))
                agents = []
                rules = []
            if value:
                agents.append(value.lower())
        elif field_name in {"allow", "disallow"}:
            if not agents:
                continue
            # Empty Disallow means "allow everything" and is not recorded.
            if field_name == "disallow" and value == "":
                continue
            if field_name == "allow" and value == "":
                continue
            rules.append((field_name == "allow", value))
    if agents:
        groups.append((agents, rules))
    return groups

def parse_robots(text: str, user_agent: str) -> RobotsPolicy:
    """Parse robots.txt and select the most specific matching user-agent."""
    groups = _groups(text or "")
    ua = (user_agent or "").lower()
    wildcard: list[tuple[bool, str]] = []
    specific: dict[str, list[tuple[bool, str]]] = {}
    for agents, rules in groups:
        for agent in agents:
            if agent == "*":
                wildcard.extend(rules)
            elif agent and agent in ua:
                specific.setdefault(agent, []).extend(rules)
    chosen: list[tuple[bool, str]]
    if specific:
        best_length = max(len(agent) for agent in specific)
        chosen = []
        for agent, rules in specific.items():
            if len(agent) == best_length:
                chosen.extend(rules)
    else:
        chosen = wildcard
    compiled = [
        RobotsRule(allow=allow, pattern=pattern, regex=_compile_pattern(pattern))
        for allow, pattern in chosen
    ]
    return RobotsPolicy(rules=compiled)

class RobotsCache:
    """Fetch and parse robots.txt once per origin."""

    def __init__(self, fetcher, respect: bool = True, user_agent: str = "linkpatrol/0.1"):
        self.fetcher = fetcher
        self.respect = respect
        self.user_agent = user_agent
        self._cache: dict[str, RobotsPolicy] = {}
        self._lock = threading.Lock()

    def policy_for(self, url: str) -> RobotsPolicy:
        if not self.respect:
            return RobotsPolicy()
        key = origin(url)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        policy = self._download(key)
        with self._lock:
            self._cache[key] = policy
        return policy

    def _download(self, site_origin: str) -> RobotsPolicy:
        robots_url = f"{site_origin}/robots.txt"
        try:
            result = self.fetcher.fetch(
                robots_url, method="GET", max_body_bytes=512 * 1024
            )
        except Exception:
            return RobotsPolicy()
        if result.status_code != 200 or result.error:
            return RobotsPolicy()
        text = result.body.decode("utf-8", errors="replace")
        return parse_robots(text, self.user_agent)

    def clear(self) -> None:
        """Clear cached policies; useful for long-lived processes and tests."""
        with self._lock:
            self._cache.clear()
