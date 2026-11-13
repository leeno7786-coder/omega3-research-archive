"""Stage 3 — canonical-record candidate loading + graph traversal for the
unified recall engine.

load_canonical_candidates walks ONE coord_index snapshot and loads the live
record metas, applying lobe/subdomain filters (write-Y==read-Y; denied wins
over allowed). expand_via_graph follows compiled STRUCTURAL graph edges
(MENTIONS / SUPERSEDES / co-occurrence / persona relations; the dense similarity
class is retired since compiler v7) from seed ids, bypassing allowed_lobes but
enforcing denied_lobes on EVERY hop. Both are deterministic + Guardian-free.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from memory.dewey_map import NON_RECALLABLE_LOBES
from memory.engine.walker import coord_index_snapshot, load_coord_index, load_meta_by_path
from memory.taxonomy import graph_store as G

log = logging.getLogger("omega.memory.taxonomy.recall_graph")

# Polite DEFAULT (callers may override 11/dev with an explicit allow); the
# STRUCTURAL boundary is dewey_map.NON_RECALLABLE_LOBES (6 journal, 7 vision) —
# unioned into the denied set inside every loader below, so NO caller argument
# can reach those lobes (the recall-lane rule, operator 2026-07-18).
DENIED_LOBES_DEFAULT = (6, 11)


def _lobe(coord: Sequence[int]) -> Optional[int]:
    try:
        return int((coord or [])[0])
    except (TypeError, ValueError, IndexError):
        return None


def _subdomain(coord: Sequence[int]) -> Optional[int]:
    try:
        return int((coord or [])[1])
    except (TypeError, ValueError, IndexError):
        return None


RECALL_BLOCKED_STATUSES = frozenset({"quarantined", "rejected", "superseded"})


def _graph_blocked_memory_ids(*, root: Path | str | None = None) -> frozenset[str]:
    """Return memory_record ids whose compiled graph leaf is not recallable."""
    from memory.projection.generation import GenerationIntegrityError

    blocked: set[str] = set()
    try:
        conn = G.connect_for_read(root)
    except GenerationIntegrityError:
        raise  # Cut 13 (D-13-3): an unavailable generation is loud, never "nothing blocked"
    except Exception as exc:
        log.debug("graph blocked lookup connect failed (%s)", exc)
        return frozenset()
    try:
        rows = conn.execute(
            "SELECT node_id, payload_json FROM nodes WHERE node_type = ?",
            ("memory_record",),
        ).fetchall()
    except Exception as exc:
        log.debug("graph blocked lookup query failed (%s)", exc)
        return frozenset()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            continue
        if str(payload.get("type") or "") != "memory_leaf_v2":
            continue
        status = str(payload.get("status") or "active").strip().lower()
        if status in RECALL_BLOCKED_STATUSES:
            blocked.add(str(payload.get("memory_id") or row["node_id"] or ""))
    return frozenset(item for item in blocked if item)


def _is_recallable(meta: dict[str, Any]) -> bool:
    if meta.get("superseded_by"):
        return False
    enrichment = meta.get("enrichment") if isinstance(meta.get("enrichment"), dict) else {}
    status = str(meta.get("status") or enrichment.get("status") or "active").strip().lower()
    if status in RECALL_BLOCKED_STATUSES:
        return False
    flags = meta.get("flags") if isinstance(meta.get("flags"), dict) else {}
    return not flags.get("quarantined")


def _has_recall_projection(entry: dict[str, Any] | None) -> bool:
    """True when coord_index entry carries the recall projection fields."""
    if not isinstance(entry, dict):
        return False
    if entry.get("recall_proj"):
        return True
    # Legacy enriched entries may have summary without the version marker.
    return "summary" in entry and "status" in entry


def meta_from_coord_entry(
    mid: str,
    entry: dict[str, Any],
    *,
    root: Path | str | None = None,
) -> Dict[str, Any]:
    """Resolve a candidate meta from the index projection, falling back to disk.

    Rich ``coord_index`` entries (built by ``extract_minimal``) avoid an O(N)
    ``load_meta_by_path`` during taxonomy recall. Legacy sparse entries still
    open the canonical JSON once.
    """
    if _has_recall_projection(entry):
        flags = {
            "canonical": bool(entry.get("canonical")),
            "private": bool(entry.get("private")),
            "quarantined": bool(entry.get("quarantined")),
        }
        status = str(entry.get("status") or "active").strip().lower() or "active"
        return {
            "id": mid,
            "coord": list(entry.get("coord") or []),
            "path": entry.get("path"),
            "title": entry.get("title") or "",
            "type": entry.get("type"),
            "domain": entry.get("domain") or "",
            "subtopic": entry.get("subtopic") or "",
            "summary": entry.get("summary") or "",
            "tags": list(entry.get("tags") or []),
            "text": entry.get("text_prefix") or "",
            "status": status,
            "superseded_by": entry.get("superseded_by") or None,
            "flags": flags,
            "enrichment": {
                "canonical": flags["canonical"],
                "status": status,
            },
        }

    path = entry.get("path")
    if not path:
        return {}
    p = Path(path)
    if not p.is_absolute():
        from core.paths import OMEGA_ROOT

        base = Path(root) if root is not None else OMEGA_ROOT
        p = base / p
    meta = load_meta_by_path(p)
    if not meta:
        return {}
    meta.setdefault("id", mid)
    meta.setdefault("coord", list(entry.get("coord") or []))
    return meta


def _passes(coord, allowed_set, denied_set, allowed_subs) -> bool:
    x = _lobe(coord)
    if x is None:
        return False
    if denied_set and x in denied_set:
        return False
    if allowed_set is not None and x not in allowed_set:
        return False
    if allowed_subs is not None:
        y = _subdomain(coord)
        if y is None or y not in allowed_subs:
            return False
    return True


def load_canonical_candidates(
    *,
    allowed_lobes: Optional[Iterable[int]] = None,
    denied_lobes: Iterable[int] = DENIED_LOBES_DEFAULT,
    allowed_subdomains: Optional[Iterable[int]] = None,
    root: Path | str | None = None,
) -> List[Dict[str, Any]]:
    """Load live canonical record metas honoring lobe/subdomain lane discipline.

    Deny takes precedence over allow. Superseded + private records are dropped.
    """
    allowed_set = set(allowed_lobes) if allowed_lobes is not None else None
    # the structural boundary: journal/vision are never query-recallable,
    # whatever the caller allowed or forgot to deny
    denied_set = set(denied_lobes or ()) | set(NON_RECALLABLE_LOBES)
    allowed_subs = set(allowed_subdomains) if allowed_subdomains is not None else None
    graph_blocked = _graph_blocked_memory_ids(root=root)

    # Ensure the index is populated before reading its snapshot. An unavailable
    # index propagates as CoordIndexUnavailable; an empty valid index returns an
    # empty candidate set. Those states must never collapse into one another.
    load_coord_index()

    out: List[Dict[str, Any]] = []
    for mid, entry in coord_index_snapshot().items():
        if mid in graph_blocked:
            continue
        coord = entry.get("coord")
        if not _passes(coord, allowed_set, denied_set, allowed_subs):
            continue
        if not entry.get("path"):
            continue
        meta = meta_from_coord_entry(mid, entry, root=root)
        if not meta:
            continue
        if not _is_recallable(meta):
            continue
        if meta.get("flags", {}).get("private"):
            continue
        if meta.get("superseded_by"):
            continue
        meta.setdefault("id", mid)
        meta.setdefault("coord", list(coord or []))
        out.append(meta)
    # Stable order for determinism.
    out.sort(key=lambda m: str(m.get("id") or ""))
    return out


# Graph edge types that participate in via_link recall expansion.
_VIA_LINK_EDGE_TYPES = (
    "MENTIONS",
    "SUPERSEDES",
    "CO_LOCATED",
    "SHARES_DOMAIN",
    "SHARES_TAG",
    # (SEMANTIC_SIMILARITY retired at compiler v7 — memory-organ Cut 5A; the via-link
    #  whitelist never walks it, even from a stale artifact)
    # Semantic relation edges (extract_persona): the recall plane now walks 'Noah OWNS
    # Omega AI' / 'Noah FAMILY_OF Abel', not just co-occurrence.
    "FAMILY_OF",
    "OWNS",
    "STUDIES",
    "WORKS_AT",
    "WORKS_AS",
    "LIVES_IN",
    "LIKES",
    # entity-resolution alias so a NAME query reaches the pivot node the relations
    # hang off ("Noah" -SAME_AS-> operator -OWNS-> ...).
    "SAME_AS",
)


# Upstreamed 2026-08-07 from the omega-memory-mcp eval tree (now the canonical
# home of this bulk scan). recall.py seeds expand_via_graph with
# the ENTIRE canonical corpus, so the per-node walk decoded two payload
# JSONs per neighbor row across every record node -- ~4.5M decodes per
# recall on the LME-V2 enterprise graph (21,346 records / 1,131,732 edges),
# 507s per warm query. The walk consumes only node_id + edge_type +
# confidence, all plain columns.
def _bulk_edge_adjacency(
    conn: Any, edge_types: Sequence[str]
) -> Dict[str, List[tuple]]:
    """One streamed id-only edge scan → node_id -> [(other, etype, conf)].

    Replicates ``graph_store.neighbors(node, edge_types=...)`` exactly for
    the fields expand_via_graph consumes: a row is visible from one endpoint
    only when the OTHER endpoint has a node row (the JOIN), per-node order
    is all outbound rows then all inbound rows (the UNION ALL), duplicates
    and self-loops preserved. ``graph_store.neighbors()`` itself is
    untouched -- its per-node semantics still serve the live recall path.
    """
    wanted_types = set(edge_types)
    node_ids = {str(r[0]) for r in conn.execute("SELECT node_id FROM nodes")}
    outbound: Dict[str, List[tuple]] = {}
    inbound: Dict[str, List[tuple]] = {}
    for src, dst, etype, conf in conn.execute(
        "SELECT source_id, target_id, edge_type, confidence FROM edges"
    ):
        if etype not in wanted_types:
            continue
        if dst in node_ids:
            outbound.setdefault(src, []).append((dst, etype, conf))
        if src in node_ids:
            inbound.setdefault(dst, []).append((src, etype, conf))
    adjacency: Dict[str, List[tuple]] = {}
    for node in outbound.keys() | inbound.keys():
        adjacency[node] = outbound.get(node, []) + inbound.get(node, [])
    return adjacency


def expand_via_graph(
    seed_ids: Iterable[str],
    *,
    denied_lobes: Iterable[int] = DENIED_LOBES_DEFAULT,
    root: Path | str | None = None,
    edge_types: Optional[Sequence[str]] = None,
    depth: int = 1,
) -> List[Dict[str, Any]]:
    """Follow compiled graph edges from seeds. Bypasses allowed_lobes (an
    explicit edge IS the relevance signal) but enforces denied_lobes on
    EVERY hop, including cross-lobe hops. Returns loaded
    record metas marked via_link=True; never raises into recall."""
    denied_set = set(denied_lobes or ()) | set(NON_RECALLABLE_LOBES)  # structural
    edge_types = tuple(edge_types) if edge_types is not None else _VIA_LINK_EDGE_TYPES
    snapshot = coord_index_snapshot()
    graph_blocked = _graph_blocked_memory_ids(root=root)

    from memory.projection.generation import GenerationIntegrityError

    expanded: Dict[str, Dict[str, Any]] = {}
    visited = set(seed_ids)
    frontier = list(seed_ids)
    try:
        conn = G.connect_for_read(root)
    except GenerationIntegrityError:
        raise  # Cut 13 (D-13-3): an unavailable generation is loud, never "no expansion"
    except Exception as exc:
        log.debug("expand_via_graph: graph connect failed (%s)", exc)
        return []
    try:
        # LOCAL DIVERGENCE (see _bulk_edge_adjacency above): one id-only scan
        # replaces the per-node neighbors() calls; the loop below is unchanged.
        try:
            adjacency = _bulk_edge_adjacency(conn, edge_types)
        except Exception as exc:
            log.debug("expand_via_graph: bulk edge scan failed (%s)", exc)
            return []
        for _ in range(max(1, depth)):
            next_frontier: List[str] = []
            for node_id in frontier:
                for target_id, via_edge_type, via_confidence in adjacency.get(node_id, ()):
                    if not target_id or target_id in visited or target_id in expanded:
                        continue
                    if target_id in graph_blocked:
                        continue
                    entry = snapshot.get(target_id)
                    if not entry:
                        continue
                    coord = entry.get("coord")
                    x = _lobe(coord)
                    if x is None or (denied_set and x in denied_set):
                        continue  # deny on EVERY hop
                    if not entry.get("path"):
                        continue
                    meta = meta_from_coord_entry(target_id, entry, root=root)
                    if not meta or not _is_recallable(meta):
                        continue
                    if meta.get("flags", {}).get("private"):
                        continue
                    if meta.get("superseded_by"):
                        continue
                    meta.setdefault("id", target_id)
                    meta.setdefault("coord", list(coord or []))
                    meta["via_link"] = True
                    # Carry the edge TYPE (diagnostic / receipt): every via-link hop is
                    # structural since compiler v7 and must earn its place by query
                    # relevance in recall's gate (no per-type exemption remains).
                    meta["_via_edge_type"] = str(via_edge_type or "")
                    # Carry the edge confidence (1.0 for structural edges) — the +conf
                    # bonus in recall stays as-is for structural hops.
                    _conf = via_confidence
                    try:
                        meta["_via_confidence"] = float(_conf) if _conf is not None else 1.0
                    except (TypeError, ValueError):
                        meta["_via_confidence"] = 1.0
                    expanded[target_id] = meta
                    next_frontier.append(target_id)
                    visited.add(target_id)
            frontier = next_frontier
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [expanded[k] for k in sorted(expanded)]
