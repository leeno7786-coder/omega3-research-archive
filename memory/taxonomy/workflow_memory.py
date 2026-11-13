"""Phase 7L — skill + workflow memory.

A GROUNDED skill execution (it actually did something: tool_calls | files_changed |
commands_run | tests_run) becomes a recallable ``workflow_memory`` node — a reusable
"how this kind of objective was accomplished" pattern that Meta can later recall as
selection context (7N).

Two outcomes are captured:
  - ``outcome=success``     — a finished, satisfied build. Reinforces the skill (STRENGTHENS)
    and becomes a ``prior_builds`` "already_built" recall.
  - ``outcome=in_progress`` — a fold-back YIELD: the build hit its round/watchdog budget and
    stopped at a clean boundary with progress intact. It carries the brain-derived
    ``progress_digest`` (the context-compactor that re-grounds qwen on resume) and does NOT
    STRENGTHEN the skill (unfinished work must not reinforce it as proven). Recalled as
    ``build_in_progress`` so Omega's episode can decide continue|finish.

Topology:
    [episode] PRODUCED [workflow_memory]
    [workflow_memory] SELECTED_SKILL [skill]
    [workflow_memory] STRENGTHENS  [skill]
    [workflow_memory] USED_TOOL     [tool]   (per tool in the sequence)
    [workflow_memory] RESULTED_IN   [outcome]

Model-free; consumed by the compiler (7I). The objective / allowed_tools come from the
correlated skill_execution_pack and the completion flag from the correlated global_b.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from memory.taxonomy.entities import entity
from memory.taxonomy.hashing import edge_id_for
from memory.taxonomy.normalize import canonical_json
from memory.taxonomy.schemas import (
    MEMORY_EDGE_SCHEMA_VERSION,
    MemoryEdge,
    MemoryEntity,
    MemoryEvent,
)


def _wf_id(skill_id: str, objective: str, source_event_id: str) -> str:
    blob = canonical_json([skill_id, objective, source_event_id])
    return "wf_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _edge(source_id: str, edge_type: str, target_id: str, sev: str) -> MemoryEdge:
    return MemoryEdge(
        edge_id=edge_id_for(source_id, edge_type, target_id, sev, MEMORY_EDGE_SCHEMA_VERSION),
        source_id=source_id,
        target_id=target_id,
        edge_type=edge_type,
        evidence_event_id=sev,
    )


def _grounded(payload: dict) -> bool:
    return bool(
        payload.get("files_changed")
        or payload.get("commands_run")
        or payload.get("tests_run")
        or payload.get("tool_calls")
    )


def _project_key(objective: str, files: Iterable[str]) -> str:
    """Derive a stable project identity from artifact paths before free-form wording."""
    for raw in files:
        parts = [part for part in str(raw or "").replace("\\", "/").split("/") if part]
        if len(parts) >= 2 and parts[0] not in {".", ".."}:
            return re.sub(r"[^a-z0-9_-]+", "-", parts[0].lower()).strip("-_")
    normalized = re.sub(r"[^a-z0-9]+", "-", str(objective or "").lower()).strip("-")
    return normalized[:120]


def workflow_topology(
    events: Iterable[MemoryEvent],
) -> tuple[list[MemoryEntity], list[dict], list[MemoryEdge]]:
    """Build (entities, workflow_memory nodes, edges) for grounded successful skills."""
    events = list(events)
    packs = {}
    gbs = {}
    for ev in events:
        if ev.event_kind == "skill_execution_pack":
            d = str(ev.payload.get("dispatch_id") or "")
            if d:
                packs[d] = ev
        elif ev.event_kind == "global_b_skill_update":
            d = str(ev.payload.get("dispatch_id") or "")
            if d:
                gbs[d] = ev

    ents: dict[str, MemoryEntity] = {}
    edges: dict[str, MemoryEdge] = {}
    nodes: list[dict] = []

    def add_ent(e: MemoryEntity) -> str:
        ents.setdefault(e.entity_id, e)
        return e.entity_id

    def add_edge(g: MemoryEdge) -> None:
        edges.setdefault(g.edge_id, g)

    for ev in events:
        if ev.event_kind != "qwen_skill_result":
            continue
        p = ev.payload
        status = str(p.get("status"))
        # success AND yielded both ground a workflow node — a yield is a real, recallable
        # checkpoint (progress intact), just an UNFINISHED one.
        if status not in ("success", "yielded") or not _grounded(p):
            continue
        sev = ev.event_id
        skill = str(p.get("skill_name") or "").strip()
        skill_id = str(p.get("skill_id") or "").strip()
        dispatch = str(p.get("dispatch_id") or "").strip()
        episode = str(p.get("episode_id") or ev.episode_id or "").strip()
        pack = packs.get(dispatch)
        gb = gbs.get(dispatch)
        # The bridge status is not completion evidence. Global B is the authoritative,
        # verification-aware outcome and is required before a workflow becomes recallable.
        if gb is None:
            continue
        gb_payload = dict(gb.payload or {})
        blockers = list(gb_payload.get("unresolved_blockers") or ())
        complete = bool(
            gb_payload.get("intent_satisfied")
            and gb_payload.get("objective_complete")
            and not gb_payload.get("more_work_needed")
            and not blockers
        )
        if complete and status == "success":
            outcome = "success"
        elif bool(gb_payload.get("more_work_needed")) or not complete:
            outcome = "in_progress"
        else:
            continue
        in_progress = outcome == "in_progress"
        objective = str((pack.payload.get("objective") if pack else "") or "").strip()
        tool_sequence = [
            str((tc or {}).get("tool_name") or "").strip()
            for tc in (p.get("tool_calls") or [])
            if (tc or {}).get("tool_name")
        ]
        files = [str(f).strip() for f in (p.get("files_changed") or []) if f]
        tests = [str(t).strip() for t in (p.get("tests_run") or []) if t]
        allowed = list((pack.payload.get("allowed_tools") if pack else []) or [])
        evidence = [sev] + ([pack.event_id] if pack else []) + ([gb.event_id] if gb else [])

        candidate = {
            "type": "workflow_memory",
            "skill_name": skill,
            "skill_id": skill_id,
            "execution_kind": str(p.get("execution_kind") or ""),
            "objective_pattern": objective,
            # WHAT was built — the executor's own description of the artifact. This is the
            # recallable "I made X" detail; without it the node only knew a skill succeeded,
            # never what it produced (operator 2026-06-25). Bounded to keep the node lean.
            "summary": str(p.get("summary") or "").strip()[:1200],
            # the progress digest — LEADS with the model's own forced end-of-run account
            # (operator 2026-07-01), derived facts follow; the context-compactor that re-grounds
            # qwen on resume. 2400 (was 1500) so the account survives persistence.
            "progress_digest": str(p.get("progress_digest") or "").strip()[:2400],
            "rounds_used": int(p.get("rounds_used") or 0),
            "allowed_tools": allowed,
            "tool_sequence": tool_sequence,
            "files_changed": files,
            "tests_run": tests,
            "outcome": outcome,
            "completion_satisfied": bool(gb_payload.get("intent_satisfied")),
            "source_episode_id": episode,
            "evidence_event_ids": evidence,
            "created_at": str(ev.created_at or ""),
            "source_event_id": sev,
            "project_key": _project_key(objective, files),
            "objective_complete": bool(gb_payload.get("objective_complete")),
            "intent_satisfied": bool(gb_payload.get("intent_satisfied")),
            "unresolved_blockers": blockers,
        }
        wf_id = _wf_id(skill_id, objective, sev)
        nodes.append(
            {
                "node_id": wf_id,
                "node_type": "workflow_memory",
                "label": f"{skill}: {objective}".strip(),
                "payload": candidate,
                "source_event_id": sev,
            }
        )

        skill_eid = add_ent(entity("skill", skill, sev)) if skill else ""
        episode_eid = add_ent(entity("episode", episode, sev)) if episode else ""
        outcome_eid = add_ent(entity("outcome", f"{skill} {outcome}".strip(), sev))

        if episode_eid:
            add_edge(_edge(episode_eid, "PRODUCED", wf_id, sev))
        if skill_eid:
            add_edge(_edge(wf_id, "SELECTED_SKILL", skill_eid, sev))
            # Only FINISHED work reinforces the skill. A yield is unproven — STRENGTHENS it
            # later, when (and if) the resumed build completes.
            if not in_progress:
                add_edge(_edge(wf_id, "STRENGTHENS", skill_eid, sev))
        add_edge(_edge(wf_id, "RESULTED_IN", outcome_eid, sev))
        for tool in tool_sequence:
            tool_eid = add_ent(entity("tool", tool, sev))
            add_edge(_edge(wf_id, "USED_TOOL", tool_eid, sev))

    return list(ents.values()), nodes, list(edges.values())
