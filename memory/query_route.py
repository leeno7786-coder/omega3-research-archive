"""Route a QUESTION to a Dewey lobe, so recall can use the plane we already write.

Every record is routed to a coordinate at ingest, and recall then ranked with
BM25 and discarded the address. Measured on eac54add: a `procedure` record at
coord [5,0,0] scored 26.89 -- four times everything else -- for a question
asking what happened at a TIME (lobe 9), and steered the answer wrong.

The router is deterministic and already query-shaped, so this is a reconnection,
not a new classifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class QueryRoute:
    """A query's Dewey lobe, or None when the router declined to classify."""

    lobe: int | None
    source: str


def route_query(query: str) -> QueryRoute:
    text = str(query or "").strip()
    if not text:
        return QueryRoute(lobe=None, source="empty")
    try:
        from auto_loop.mril_tools.router import route_memory_tool

        entry = route_memory_tool(text)
    except Exception:
        # Recall must never fail because routing is unavailable; an unrouted
        # query simply gets no affinity signal.
        return QueryRoute(lobe=None, source="unavailable")
    lobe = entry.get("lobe") if isinstance(entry, dict) else None
    if isinstance(lobe, bool) or not isinstance(lobe, int):
        return QueryRoute(lobe=None, source="unrouted")
    return QueryRoute(lobe=lobe, source="router")


def coordinate_affinity(route: QueryRoute, coord: object) -> float:
    """1.0 when the record sits in the query's lobe, else 0.0.

    Deliberately a REWARD and never a penalty: a wrong route must degrade
    ranking, never erase a lane. NON_RECALLABLE_LOBES remains the only veto.
    """
    if route.lobe is None:
        return 0.0
    if not isinstance(coord, Sequence) or isinstance(coord, (str, bytes)):
        return 0.0
    if not coord:
        return 0.0
    head = coord[0]
    if isinstance(head, bool) or not isinstance(head, int):
        return 0.0
    return 1.0 if head == route.lobe else 0.0
