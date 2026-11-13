"""Phase 7G — deterministic entity + claim extraction from MemoryEvents.

MODEL-FREE: every entity/claim comes from a fixed template over fields already present
on the event payload — no LLM, no inference. Repeated extraction yields identical IDs
(content-derived), so the graph compiler is idempotent. Every entity/claim cites the
source event_id as evidence.
"""

from __future__ import annotations

from typing import Any, Iterable

from memory.taxonomy.claims import claim
from memory.taxonomy.entities import entity
from memory.taxonomy.schemas import MemoryClaim, MemoryEntity, MemoryEvent


def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _from_qwen_skill_result(ev: MemoryEvent) -> tuple[list, list]:
    p = ev.payload
    sev = ev.event_id
    skill = _str(p.get("skill_name"))
    dispatch = _str(p.get("dispatch_id"))
    episode = _str(p.get("episode_id")) or _str(ev.episode_id)
    status = _str(p.get("status"))
    ents: list[MemoryEntity] = []
    claims: list[MemoryClaim] = []
    if skill:
        ents.append(entity("skill", skill, sev))
    if dispatch:
        ents.append(entity("dispatch", dispatch, sev))
    if episode:
        ents.append(entity("episode", episode, sev))
    if skill and status:
        claims.append(claim(skill, "produced_status", status, sev))
    for tc in p.get("tool_calls") or []:
        tool = _str((tc or {}).get("tool_name"))
        if tool:
            ents.append(entity("tool", tool, sev))
            if skill:
                claims.append(claim(skill, "used_tool", tool, sev))
    for path in p.get("files_changed") or []:
        path = _str(path)
        if path:
            ents.append(entity("file", path, sev))
            if skill:
                claims.append(claim(skill, "changed_file", path, sev))
    for test in p.get("tests_run") or []:
        test = _str(test)
        if test and dispatch:
            claims.append(claim(dispatch, "verified_by", test, sev))
    failure_class = _str(p.get("failure_class"))
    if failure_class:
        ents.append(entity("failure", failure_class, sev))
        if dispatch:
            claims.append(claim(dispatch, "failed_because", failure_class, sev))
    return ents, claims


def _from_skill_execution_pack(ev: MemoryEvent) -> tuple[list, list]:
    p = ev.payload
    sev = ev.event_id
    skill = _str(p.get("skill_name"))
    dispatch = _str(p.get("dispatch_id"))
    objective = _str(p.get("objective"))
    ents: list[MemoryEntity] = []
    claims: list[MemoryClaim] = []
    if skill:
        ents.append(entity("skill", skill, sev))
    if dispatch:
        ents.append(entity("dispatch", dispatch, sev))
    if skill and objective:
        claims.append(claim(skill, "selected_for", objective, sev))
    return ents, claims


def _from_failure(ev: MemoryEvent) -> tuple[list, list]:
    p = ev.payload
    sev = ev.event_id
    dispatch = _str(p.get("dispatch_id"))
    failure_class = _str(p.get("failure_class")) or _str(p.get("kind"))
    ents: list[MemoryEntity] = []
    claims: list[MemoryClaim] = []
    if failure_class:
        ents.append(entity("failure", failure_class, sev))
        subject = dispatch or _str(p.get("episode_id")) or _str(ev.episode_id)
        if subject:
            claims.append(claim(subject, "failed_because", failure_class, sev))
    return ents, claims


def _from_global_b_skill(ev: MemoryEvent) -> tuple[list, list]:
    p = ev.payload
    sev = ev.event_id
    episode = _str(p.get("episode_id")) or _str(ev.episode_id)
    claims: list[MemoryClaim] = []
    ents: list[MemoryEntity] = []
    if episode:
        ents.append(entity("episode", episode, sev))
        satisfied = "true" if p.get("intent_satisfied") else "false"
        claims.append(claim(episode, "intent_satisfied", satisfied, sev))
        reason = ""
        pressure = p.get("continuation_pressure")
        if isinstance(pressure, dict):
            reason = _str(pressure.get("reason"))
        if not reason:
            signals = p.get("continuation_signals") or []
            reason = _str(signals[0]) if signals else ""
        if reason:
            claims.append(claim(episode, "created_pressure", reason, sev))
    return ents, claims


def _from_capability(ev: MemoryEvent) -> tuple[list, list]:
    p = ev.payload
    sev = ev.event_id
    skill = _str(p.get("skill_name"))
    signal = _str(p.get("signal"))
    ents: list[MemoryEntity] = []
    claims: list[MemoryClaim] = []
    if skill:
        ents.append(entity("skill", skill, sev))
        if signal:
            claims.append(claim(skill, "capability_signal", signal, sev))
    return ents, claims


def _from_causal_edge(ev: MemoryEvent) -> tuple[list, list]:
    """A host-recorded causal fact (spec section 9, T11): two entities named by the host's
    prefixed references, one claim whose predicate is the edge kind and whose EVIDENCE is the
    episode's own event — never this record's."""
    from memory.taxonomy.causal import EDGE_KINDS, REFERENCE_KINDS, reference_kind

    p = ev.payload
    kind, source, target = _str(p.get("kind")), _str(p.get("source")), _str(p.get("target"))
    evidence = _str(p.get("evidence_event_id")) or ev.event_id
    source_kind, _, source_id = source.partition(":")
    target_kind, _, target_id = target.partition(":")
    if (
        kind not in EDGE_KINDS
        or source_kind not in REFERENCE_KINDS
        or target_kind not in REFERENCE_KINDS
        or not source_id.strip()
        or not target_id.strip()
    ):
        return [], []
    return ([entity(reference_kind(source), source, evidence),
             entity(reference_kind(target), target, evidence)],
            [claim(source, kind, target, evidence)])


# event_kind -> extractor
_EXTRACTORS = {
    "qwen_skill_result": _from_qwen_skill_result,
    "skill_execution_pack": _from_skill_execution_pack,
    "skill_failure": _from_failure,
    "tool_failure": _from_failure,
    "bridge_failure": _from_failure,
    "safety_block": _from_failure,
    "global_b_skill_update": _from_global_b_skill,
    "capability_signal": _from_capability,
    "capability_reinforced": _from_capability,
    "capability_weakened": _from_capability,
    "causal_edge": _from_causal_edge,
}


def extract_from_event(event: MemoryEvent) -> tuple[list[MemoryEntity], list[MemoryClaim]]:
    """Extract (entities, claims) from one event via its deterministic template.
    Events with no template yield ([], [])."""
    extractor = _EXTRACTORS.get(event.event_kind)
    if extractor is None:
        return [], []
    return extractor(event)


def extract_from_events(
    events: Iterable[MemoryEvent],
) -> tuple[list[MemoryEntity], list[MemoryClaim]]:
    """Extract from many events, deduping entities by entity_id and claims by claim_id
    (stable insertion order preserved)."""
    seen_e: dict[str, MemoryEntity] = {}
    seen_c: dict[str, MemoryClaim] = {}
    for event in events:
        ents, claims = extract_from_event(event)
        for e in ents:
            seen_e.setdefault(e.entity_id, e)
        for c in claims:
            seen_c.setdefault(c.claim_id, c)
    return list(seen_e.values()), list(seen_c.values())
