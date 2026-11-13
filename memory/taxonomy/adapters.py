"""Phase 7E — cascade event adapters.

Convert a closed Omega episode's cascade slots into MemoryEvents. PURE functions:
adapters never mutate the episode, never write the graph, never write the ledger —
they only PRODUCE MemoryEvents (the capture hook, 7F, appends them). The event payload
carries the full slot dict so the graph can be rebuilt with no information loss
(esp. the qwen skill result's tool_calls / files_changed / tests_run / diff_summary /
failure_class / guard_status).

An adapter returns None when its slot is absent (e.g. a non-skill episode has no
skill_result), so the same aggregator handles skill and non-skill episodes.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional

from memory.taxonomy.normalize import canonical_json
from memory.taxonomy.hashing import event_id_for
from memory.taxonomy.schemas import MEMORY_EVENT_SCHEMA_VERSION, MemoryEvent


# ── slot accessors (tolerate both "global" and dataclass "global_") ─────────────
# A plugin result is provider data, and every recalled byte is a token the
# operator pays for on a frontier model. The recallable projection is therefore
# deliberately small: enough to answer "what did the plugin say", not a copy of
# the response. The complete body stays recoverable from the episode archive,
# and the digest below proves the excerpt came from it.
_MAX_PLUGIN_RESULT_CHARS = 1200


def canonical_event_view(ep: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize raw and enriched closed-episode shapes for event adapters.

    The lobe-9 archive intentionally stores the enriched episode projection,
    while older adapters consume the carrier's raw ``auto/global/meta`` slots.
    Keep that storage distinction explicit and give every adapter one stable
    view instead of making each event kind guess its input shape.
    """
    view = dict(ep)
    if not isinstance(view.get("auto"), Mapping):
        outputs = view.get("outputs")
        view["auto"] = {
            "action": view.get("action"),
            "status": view.get("cascade_status") or view.get("status"),
            "failure_kind": view.get("failure_kind"),
            "failure_reason": view.get("failure_reason"),
            "outputs": dict(outputs) if isinstance(outputs, Mapping) else {},
        }
    if not isinstance(view.get("global") or view.get("global_"), Mapping):
        global_view: dict[str, Any] = {}
        outcome = view.get("outcome")
        if isinstance(outcome, Mapping):
            global_view["outcome"] = dict(outcome)
        omega_result = view.get("omega_result")
        if isinstance(omega_result, Mapping):
            global_view["omega_result"] = dict(omega_result)
        intent = str(view.get("intent") or "").strip()
        if intent:
            global_view["intent_decision"] = {"intent": intent}
        view["global"] = global_view
    if not isinstance(view.get("meta"), Mapping):
        execution = view.get("execution")
        meta_view: dict[str, Any] = {}
        if isinstance(execution, Mapping):
            pack = execution.get("execution_action_pack")
            if isinstance(pack, Mapping):
                meta_view["execution_action_pack"] = dict(pack)
        view["meta"] = meta_view
    return view


def _eid(ep: Mapping[str, Any]) -> str:
    return str(ep.get("episode_id") or "")


def _global(ep: Mapping[str, Any]) -> dict:
    return dict(ep.get("global") or ep.get("global_") or {})


def _meta(ep: Mapping[str, Any]) -> dict:
    return dict(ep.get("meta") or {})


def _auto(ep: Mapping[str, Any]) -> dict:
    return dict(ep.get("auto") or {})


def _auto_outputs(ep: Mapping[str, Any]) -> dict:
    return dict(_auto(ep).get("outputs") or {})


def _resolve_created_at(ep: Mapping[str, Any], created_at: Optional[str]) -> str:
    if created_at:
        return created_at
    return str(ep.get("closed_at") or ep.get("created_at") or ep.get("opened_at") or "")


def _build_event(
    *, event_kind: str, source: str, episode_id: str, payload: Any, created_at: str
) -> MemoryEvent:
    body = payload if isinstance(payload, dict) else {"value": payload}
    content_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    eid = event_id_for(event_kind, episode_id, source, body, MEMORY_EVENT_SCHEMA_VERSION)
    return MemoryEvent(
        event_id=eid,
        event_kind=event_kind,
        episode_id=episode_id,
        source=source,
        created_at=created_at,
        content_hash=content_hash,
        payload=dict(body),
    )


# ── episode-slot adapters ───────────────────────────────────────────────────────
def event_from_pressure_ignition(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    p = ep.get("pressure")
    if not p:
        return None
    return _build_event(
        event_kind="pressure_ignition",
        source="pressure_loop.ignition",
        episode_id=_eid(ep),
        payload=dict(p),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_global_a(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    g = _global(ep)
    d = g.get("intent_decision") or g.get("action_goal")
    if not d:
        return None
    return _build_event(
        event_kind="global_a_intent",
        source="global_loop.intent_a",
        episode_id=_eid(ep),
        payload=dict(d),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_meta_a_pack(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    pack = _meta(ep).get("execution_action_pack")
    if not pack:
        return None
    return _build_event(
        event_kind="meta_a_action_pack",
        source="meta_loop.action_a",
        episode_id=_eid(ep),
        payload=dict(pack),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_skill_execution_pack(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    pack = _meta(ep).get("skill_execution_pack")
    if not pack:
        return None
    return _build_event(
        event_kind="skill_execution_pack",
        source="meta_loop.skill_selection",
        episode_id=_eid(ep),
        payload=dict(pack),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_qwen_skill_result(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    # The live auto.outputs.skill_result is cleared by store.close() before capture runs, so
    # the foldback also snapshots the normalized result into meta.skill_result_snapshot (which
    # survives close). Read auto.outputs first (kept for any pre-close caller), fall back to the
    # meta snapshot — this is the SOLE source of the workflow_memory node, so it must not miss.
    r = _auto_outputs(ep).get("skill_result") or _meta(ep).get("skill_result_snapshot")
    if not r:
        return None
    return _build_event(
        event_kind="qwen_skill_result",
        source="auto_loop.skill_result",
        episode_id=_eid(ep),
        payload=dict(r),
        created_at=_resolve_created_at(ep, created_at),
    )


def _plugin_result_title(payload: Mapping[str, Any]) -> str:
    """A title a recall hit can be read from, never a bare identifier."""
    capability = str(payload.get("capability") or "").strip()
    plugin_id = str(payload.get("plugin_id") or "").strip()
    if capability and plugin_id:
        return f"{plugin_id} {capability} result"
    return f"{plugin_id or capability or 'plugin'} result"


def event_from_plugin_result(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    """One recallable record per plugin tool result.

    Without this the only trace of what a plugin returned is an opaque JSON
    fragment inside the whole-episode archive: recall can reach the episode
    but never the fact the plugin actually supplied. The payload keeps the
    provider attribution (so a later answer can cite it), the operation that
    produced it, and a bounded projection of the data itself -- the same
    guardian-screened projection AUTO already showed the model, never the raw
    provider response.
    """
    result = _auto_outputs(ep).get("plugin_result")
    if not isinstance(result, Mapping) or not result:
        return None
    data = result.get("data")
    encoded = canonical_json(data) if data is not None else ""
    truncated = len(encoded) > _MAX_PLUGIN_RESULT_CHARS
    payload = {
        "plugin_id": str(result.get("plugin_id") or ""),
        "app_id": str(result.get("app_id") or ""),
        "capability": str(result.get("capability") or ""),
        "capability_kind": str(result.get("capability_kind") or ""),
        "operation": str(result.get("operation") or ""),
        "status": str(result.get("status") or ""),
        "trust": str(result.get("trust") or ""),
        "data": encoded[:_MAX_PLUGIN_RESULT_CHARS],
        "data_truncated": truncated,
        "data_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest() if encoded else "",
    }
    payload["title"] = _plugin_result_title(payload)
    status = payload["status"] or "completed"
    payload["summary"] = f"{payload['title']} ({status})"
    return _build_event(
        event_kind="tool_result",
        source="auto_loop.plugin",
        episode_id=_eid(ep),
        payload=payload,
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_auto_outcome(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    a = _auto(ep)
    if not a:
        return None
    payload = {
        "status": a.get("status", ""),
        "action": a.get("action", ""),
        "failure_kind": a.get("failure_kind", ""),
        "failure_reason": a.get("failure_reason", ""),
    }
    return _build_event(
        event_kind="auto_outcome",
        source="auto_loop.fold",
        episode_id=_eid(ep),
        payload=payload,
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_meta_b(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    m = _meta(ep).get("reflection")
    if not m:
        return None
    return _build_event(
        event_kind="meta_b_outcome",
        source="meta_loop.reflection_b",
        episode_id=_eid(ep),
        payload=dict(m),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_skill_meta_b(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    m = _meta(ep).get("skill_reflection")
    if not m:
        return None
    return _build_event(
        event_kind="meta_b_skill_outcome",
        source="meta_loop.skill_foldback",
        episode_id=_eid(ep),
        payload=dict(m),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_global_b(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    o = _global(ep).get("outcome")
    if not o:
        return None
    return _build_event(
        event_kind="global_b_update",
        source="global_loop.outcome_b",
        episode_id=_eid(ep),
        payload=dict(o),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_skill_global_b(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    o = _global(ep).get("skill_outcome")
    if not o:
        return None
    return _build_event(
        event_kind="global_b_skill_update",
        source="global_loop.skill_outcome_b",
        episode_id=_eid(ep),
        payload=dict(o),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_skill_consolidation(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    c = _meta(ep).get("skill_consolidation")
    if not c:
        return None
    return _build_event(
        event_kind="deepsleep_skill_summary",
        source="deepsleep.skill_consolidation",
        episode_id=_eid(ep),
        payload=dict(c),
        created_at=_resolve_created_at(ep, created_at),
    )


def event_from_deepsleep_summary(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> Optional[MemoryEvent]:
    s = ep.get("deepsleep_summary") or _global(ep).get("omega_result")
    if not s:
        return None
    return _build_event(
        event_kind="deepsleep_summary",
        source="deepsleep.consolidate",
        episode_id=_eid(ep),
        payload=dict(s),
        created_at=_resolve_created_at(ep, created_at),
    )


# ── record / query / packet adapters ────────────────────────────────────────────
def event_from_capability_signal(
    record: Mapping[str, Any], *, episode_id: str, created_at: str
) -> Optional[MemoryEvent]:
    if not record:
        return None
    signal = str(record.get("signal") or "").lower()
    if "reinforce" in signal:
        kind = "capability_reinforced"
    elif "weaken" in signal:
        kind = "capability_weakened"
    else:
        kind = "capability_signal"
    return _build_event(
        event_kind=kind,
        source="deepsleep.capability",
        episode_id=episode_id,
        payload=dict(record),
        created_at=created_at,
    )


def event_from_failure_record(
    record: Mapping[str, Any], *, episode_id: str, created_at: str
) -> Optional[MemoryEvent]:
    if not record:
        return None
    kind = str(record.get("kind") or "").lower()
    if "bridge" in kind:
        event_kind = "bridge_failure"
    elif "safety" in kind or "block" in kind:
        event_kind = "safety_block"
    elif "tool" in kind:
        event_kind = "tool_failure"
    else:
        event_kind = "skill_failure"
    return _build_event(
        event_kind=event_kind,
        source="deepsleep.failure_store",
        episode_id=episode_id,
        payload=dict(record),
        created_at=created_at,
    )


def event_from_recall_query(
    query: Mapping[str, Any], *, episode_id: str, created_at: str
) -> Optional[MemoryEvent]:
    if not query:
        return None
    return _build_event(
        event_kind="recall_query",
        source="memory.taxonomy.recall",
        episode_id=episode_id,
        payload=dict(query),
        created_at=created_at,
    )


def event_from_recall_result(
    packet: Mapping[str, Any], *, episode_id: str, created_at: str
) -> Optional[MemoryEvent]:
    if not packet:
        return None
    return _build_event(
        event_kind="recall_result",
        source="memory.taxonomy.recall",
        episode_id=episode_id,
        payload=dict(packet),
        created_at=created_at,
    )


# failure / block markers that promote a skill cycle to a dedicated failure event
_FAILURE_MARKERS = ("fail", "block", "error")


def event_from_episode_failure(ep: Mapping[str, Any], created_at: str) -> Optional[MemoryEvent]:
    """Promote a FAILED / BLOCKED skill cycle to a dedicated failure event
    (skill_failure | tool_failure | bridge_failure | safety_block).

    Failure is also recorded INSIDE qwen_skill_result (its failure_class), but that yields
    only a ``produced_status`` claim — NOT the 7K failure topology (WEAKENS the skill, CREATE
    repair pressure) or the 7M failure clusters. This adapter makes failure graph-native on
    the live capture path. Returns None for any non-failed episode, so success / idle
    episodes are untouched. Classification reads auto.failure_kind, the skill_reflection
    outcome_kind / safety_blocker, and the skill_result status — the signals a closed
    episode actually carries.
    """
    auto = _auto(ep)
    meta = _meta(ep)
    reflection = dict(meta.get("skill_reflection") or {})
    skill_result = dict(_auto_outputs(ep).get("skill_result") or {})
    pack = dict(meta.get("skill_execution_pack") or {})

    status = str(auto.get("status") or "").lower()
    failure_kind = str(auto.get("failure_kind") or "").lower()
    outcome_kind = str(reflection.get("outcome_kind") or "").lower()
    result_status = str(skill_result.get("status") or "").lower()
    blocked = bool(reflection.get("safety_blocker"))

    failed = (
        status == "failure"
        or result_status == "failure"
        or blocked
        or any(m in failure_kind for m in _FAILURE_MARKERS)
        or any(m in outcome_kind for m in _FAILURE_MARKERS)
    )
    if not failed:
        return None

    blob = f"{failure_kind} {outcome_kind}{' safety_block' if blocked else ''}"
    if "bridge" in blob:
        event_kind, kind = "bridge_failure", "bridge"
    elif "block" in blob or "safety" in blob:
        event_kind, kind = "safety_block", "safety"
    elif "tool" in blob:
        event_kind, kind = "tool_failure", "tool"
    else:
        event_kind, kind = "skill_failure", "skill"

    failure_class = (
        str(
            skill_result.get("failure_class")
            or auto.get("failure_reason")
            or outcome_kind
            or "unknown"
        ).strip()
        or "unknown"
    )
    payload = {
        "kind": kind,
        "failure_class": failure_class,
        "skill_name": str(skill_result.get("skill_name") or pack.get("skill_name") or ""),
        "dispatch_id": str(skill_result.get("dispatch_id") or pack.get("dispatch_id") or ""),
        "episode_id": _eid(ep),
        "outcome_kind": outcome_kind,
    }
    return _build_event(
        event_kind=event_kind,
        source="cognition.episode_failure",
        episode_id=_eid(ep),
        payload=payload,
        created_at=created_at,
    )


# ── aggregate ────────────────────────────────────────────────────────────────────
_EPISODE_ADAPTERS = (
    event_from_pressure_ignition,
    event_from_global_a,
    event_from_meta_a_pack,
    event_from_skill_execution_pack,
    event_from_qwen_skill_result,
    event_from_auto_outcome,
    event_from_plugin_result,
    event_from_meta_b,
    event_from_skill_meta_b,
    event_from_global_b,
    event_from_skill_global_b,
    event_from_skill_consolidation,
    event_from_episode_failure,
    event_from_deepsleep_summary,
)


def events_from_episode(
    ep: Mapping[str, Any], created_at: Optional[str] = None
) -> list[MemoryEvent]:
    """Run every episode-slot adapter; return the non-None events in cascade order.
    Pure — does not mutate ``ep``. The capture hook (7F) is the writer."""
    resolved = _resolve_created_at(ep, created_at)
    out: list[MemoryEvent] = []
    for adapt in _EPISODE_ADAPTERS:
        ev = adapt(ep, resolved)
        if ev is not None:
            out.append(ev)
    return out
