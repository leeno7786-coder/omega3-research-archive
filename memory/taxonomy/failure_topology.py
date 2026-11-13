"""Phase 7K — failure topology (failures become graph-native).

Deterministic edge/entity templates that turn failure events into graph structure, with
the Phase-6L distinctions preserved:
  - skill_failure  -> WEAKENS the skill capability  + CREATED_PRESSURE repair_needed
  - tool_failure   -> WEAKENS tool_reliability
  - bridge_failure -> WEAKENS qwen_bridge_reliability, NEVER the selected skill
  - safety_block   -> SUPPORTS safety_policy + CREATED_PRESSURE requires_operator_input,
                      NOT a skill failure (no WEAKENS -> skill)

Model-free; consumed by the compiler (7I). Synthetic concept nodes (repair_needed,
tool_reliability, qwen_bridge_reliability, safety_policy, requires_operator_input) use
existing entity kinds (component / policy / invariant).
"""

from __future__ import annotations

from typing import Iterable

from memory.taxonomy.entities import entity
from memory.taxonomy.hashing import edge_id_for
from memory.taxonomy.schemas import (
    MEMORY_EDGE_SCHEMA_VERSION,
    MemoryEdge,
    MemoryEntity,
    MemoryEvent,
)

_FAILURE_KINDS = frozenset({"tool_failure", "skill_failure", "bridge_failure", "safety_block"})


def _edge(source_id: str, edge_type: str, target_id: str, sev: str) -> MemoryEdge:
    return MemoryEdge(
        edge_id=edge_id_for(source_id, edge_type, target_id, sev, MEMORY_EDGE_SCHEMA_VERSION),
        source_id=source_id,
        target_id=target_id,
        edge_type=edge_type,
        evidence_event_id=sev,
    )


def failure_topology(
    events: Iterable[MemoryEvent],
) -> tuple[list[MemoryEntity], list[MemoryEdge]]:
    """Build (entities, edges) for the failure topology of all failure events."""
    ents: dict[str, MemoryEntity] = {}
    edges: dict[str, MemoryEdge] = {}

    def add_ent(e: MemoryEntity) -> str:
        ents.setdefault(e.entity_id, e)
        return e.entity_id

    def add_edge(g: MemoryEdge) -> None:
        edges.setdefault(g.edge_id, g)

    for ev in events:
        if ev.event_kind not in _FAILURE_KINDS:
            continue
        p = ev.payload
        sev = ev.event_id
        failure_class = str(p.get("failure_class") or p.get("kind") or "").strip()
        episode = str(p.get("episode_id") or ev.episode_id or "").strip()
        dispatch = str(p.get("dispatch_id") or "").strip()
        skill = str(p.get("skill_name") or "").strip()

        fid = add_ent(entity("failure", failure_class, sev)) if failure_class else ""
        eid = add_ent(entity("episode", episode, sev)) if episode else ""
        did = add_ent(entity("dispatch", dispatch, sev)) if dispatch else ""
        sid = add_ent(entity("skill", skill, sev)) if skill else ""

        if did and fid:
            add_edge(_edge(did, "FAILED_BECAUSE", fid, sev))
        if did and eid:
            add_edge(_edge(did, "OCCURRED_IN", eid, sev))

        if ev.event_kind == "skill_failure":
            if fid and sid:
                add_edge(_edge(fid, "WEAKENS", sid, sev))  # weakens the skill capability
            rid = add_ent(entity("invariant", f"repair_needed:{skill or dispatch or episode}", sev))
            if eid:
                add_edge(_edge(eid, "CREATED_PRESSURE", rid, sev))
        elif ev.event_kind == "tool_failure":
            tid = add_ent(entity("component", "tool_reliability", sev))
            if fid:
                add_edge(_edge(fid, "WEAKENS", tid, sev))
            rid = add_ent(entity("invariant", f"repair_needed:{dispatch or episode}", sev))
            if eid:
                add_edge(_edge(eid, "CREATED_PRESSURE", rid, sev))
        elif ev.event_kind == "bridge_failure":
            bid = add_ent(entity("component", "qwen_bridge_reliability", sev))
            if fid:
                add_edge(_edge(fid, "WEAKENS", bid, sev))
            # NOTE: a bridge failure NEVER weakens the selected skill.
        elif ev.event_kind == "safety_block":
            pid = add_ent(entity("policy", "safety_policy", sev))
            oid = add_ent(entity("invariant", "requires_operator_input", sev))
            source = did or eid
            if source:
                add_edge(_edge(source, "SUPPORTS", pid, sev))
            if eid:
                add_edge(_edge(eid, "CREATED_PRESSURE", oid, sev))
            # NOTE: a safety block records safety memory, NOT a skill failure.

    return list(ents.values()), list(edges.values())
