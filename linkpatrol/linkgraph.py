"""Per-run link graph: deduplicated edges with per-source provenance.

The crawler feeds every extracted reference into a :class:`LinkGraph`.  The
graph keeps exactly one edge per ``(target_url, kind)`` pair, remembers the
first occurrence as the representative record, and accumulates every distinct
source page so inlink counts and baseline diffs keep full source information.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Iterable

@dataclass(frozen=True)
class LinkSource:
    """One source page occurrence of an edge."""

    source_url: str
    raw_href: str
    line_no: int
    anchor: str = ""

    def as_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "raw_href": self.raw_href,
            "line_no": self.line_no,
            "anchor": self.anchor,
        }

@dataclass
class LinkEdge:
    """One deduplicated ``(target_url, kind)`` edge with all of its sources."""

    target_url: str
    kind: str
    scope: str
    raw_href: str
    line_no: int
    anchor: str = ""
    sources: list[LinkSource] = field(default_factory=list)
    _seen_sources: set[str] = field(default_factory=set, repr=False)

    @property
    def source_url(self) -> str:
        """The representative (first seen) source page."""
        return self.sources[0].source_url if self.sources else ""

    @property
    def source_count(self) -> int:
        return len(self.sources)

    def add_source(self, source_url: str, link) -> None:
        """Record ``source_url`` once; the first occurrence per page wins."""
        if source_url in self._seen_sources:
            return
        self._seen_sources.add(source_url)
        self.sources.append(
            LinkSource(
                source_url=source_url,
                raw_href=getattr(link, "raw_href", ""),
                line_no=int(getattr(link, "line_no", 0) or 0),
                anchor=getattr(link, "anchor", "") or "",
            )
        )

class LinkGraph:
    """Thread-safe collection of :class:`LinkEdge` keyed by (target, kind)."""

    def __init__(self) -> None:
        self._edges: dict[tuple[str, str], LinkEdge] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._edges)

    def add(self, source_url: str, link) -> None:
        """Add one extracted reference; safe to call from worker threads."""
        key = (link.target_url, link.kind)
        with self._lock:
            edge = self._edges.get(key)
            if edge is None:
                edge = LinkEdge(
                    target_url=link.target_url,
                    kind=link.kind,
                    scope=link.scope,
                    raw_href=link.raw_href,
                    line_no=link.line_no,
                    anchor=getattr(link, "anchor", "") or "",
                )
                self._edges[key] = edge
            edge.add_source(source_url, link)

    def edges(self) -> list[LinkEdge]:
        """Snapshot of edges in first-seen order."""
        with self._lock:
            return list(self._edges.values())

    @property
    def reference_count(self) -> int:
        """Total source references across every edge."""
        return sum(edge.source_count for edge in self.edges())

    def sources_for(self, target_url: str) -> list[str]:
        """All source pages linking to ``target_url`` (any kind)."""
        urls: list[str] = []
        for edge in self.edges():
            if edge.target_url == target_url:
                urls.extend(source.source_url for source in edge.sources)
        return urls

def build_graph(pairs: Iterable[tuple[str, object]]) -> LinkGraph:
    """Build a graph from ``(source_url, link)`` pairs."""
    graph = LinkGraph()
    for source_url, link in pairs:
        graph.add(source_url, link)
    return graph
