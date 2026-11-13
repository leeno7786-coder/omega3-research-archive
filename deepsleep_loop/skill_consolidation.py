"""Skill consolidation — make a qwen skill episode become memory + capability learning (6L).

derive_capability_signal encodes the 6L learning rules; apply_capability_signal applies them
through the live machinery (capability reinforce/weaken + failure memory) via injectable hooks
(tests inject spies — no disk). build_skill_consolidation_record is the Omega provenance
record; build_deepsleep_skill_summary is the recallable "what skill was used and why".
Additive + flag-gated; the live consolidation phases are untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from memory.capability_catalog import CAPABILITY_REINFORCE_DELTA, CAPABILITY_WEAKEN_DELTA
from schema.skill_consolidation_record import CapabilitySignal, SkillConsolidationRecord

log = logging.getLogger("omega.deepsleep.skill_consolidation")

_PARTIAL_DELTA = 0.02  # smaller credit for a partial that materially advanced the objective


class SkillConsolidationError(RuntimeError):
    """Raised when a skill consolidation record/signal fails validation."""


def _grounded(result: Mapping[str, Any]) -> bool:
    """A success is grounded when backed by real evidence — not a hollow 'success'."""
    return bool(
        result.get("files_changed")
        or result.get("commands_run")
        or result.get("tests_run")
        or result.get("tool_calls")
        or result.get("evidence")
    )


# ── the 6L learning rules ─────────────────────────────────────────────────────
def derive_capability_signal(
    meta_b: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    objective_materially_advanced: bool = False,
    objective_complete: bool = True,
) -> CapabilitySignal:
    kind = meta_b["outcome_kind"]
    name = meta_b["skill_name"]
    sid = meta_b["skill_id"]
    fc = (meta_b.get("failure_class") or "").lower()

    if kind == "skill_success":
        # Meta B's Global B update is the completion authority. A grounded bridge
        # success with unresolved verification blockers is not a proven capability.
        if not objective_complete:
            return CapabilitySignal(
                "neutral",
                name,
                sid,
                0.0,
                "none",
                "success reported but objective is not complete",
                creates_repair_pressure=True,
            )
        if _grounded(result):
            return CapabilitySignal(
                "reinforce",
                name,
                sid,
                CAPABILITY_REINFORCE_DELTA,
                "skill_capability",
                "grounded success",
            )
        return CapabilitySignal("neutral", name, sid, 0.0, "none", "ungrounded success")

    if kind == "skill_partial":
        if objective_materially_advanced:
            return CapabilitySignal(
                "reinforce_partial",
                name,
                sid,
                _PARTIAL_DELTA,
                "skill_capability",
                "partial materially advanced",
                creates_repair_pressure=True,
            )
        return CapabilitySignal(
            "neutral",
            name,
            sid,
            0.0,
            "none",
            "partial, no material advance",
            creates_repair_pressure=True,
        )

    if kind == "skill_requires_confirmation":
        # pending approval — neither reinforce nor punish
        return CapabilitySignal("neutral", name, sid, 0.0, "none", "pending operator approval")

    if kind == "skill_blocked":
        # blocked by safety becomes safety memory, NOT a capability failure
        return CapabilitySignal(
            "safety_memory",
            name,
            sid,
            0.0,
            "safety",
            meta_b.get("safety_blocker") or "blocked by safety",
        )

    if kind in ("skill_malformed_result", "skill_bridge_unavailable"):
        # weakens BRIDGE reliability, not the selected skill
        return CapabilitySignal(
            "weaken_bridge",
            name,
            sid,
            -CAPABILITY_WEAKEN_DELTA,
            "bridge_reliability",
            kind.removeprefix("skill_"),
            creates_repair_pressure=True,
        )

    if "forbidden_tool" in fc:
        # qwen forbidden-tool attempt weakens skill/tool reliability
        return CapabilitySignal(
            "weaken_skill",
            name,
            sid,
            -CAPABILITY_WEAKEN_DELTA,
            "skill_capability",
            "forbidden tool attempt",
            creates_repair_pressure=True,
        )

    # skill_failure / skill_timeout / skill_invalid_dispatch -> failure memory (+ repair pressure)
    return CapabilitySignal(
        "failure_memory",
        name,
        sid,
        0.0,
        "failure",
        fc or kind.removeprefix("skill_"),
        creates_repair_pressure=True,
    )


# ── live bridges (failure-soft) ───────────────────────────────────────────────
def _default_reinforce(name: str, delta: float, domain: str, scope: str) -> Optional[float]:
    from memory.capability_catalog import reinforce_capability

    return reinforce_capability(name, delta=abs(delta), domain=domain, scope=scope)


def _default_weaken(
    name: str, delta: float, domain: str, scope: str, reason: str
) -> Optional[float]:
    from memory.capability_catalog import weaken_capability

    return weaken_capability(name, delta=abs(delta), domain=domain, scope=scope, reason=reason)


def _default_record_failure(kind: str, context: str, episode_id: str = "") -> bool:
    """Record/increment a FailurePattern. Failure-soft (the live upsert is fail-hard; the
    skill path must not crash the cascade on a consolidation write hiccup)."""
    try:
        from deepsleep_loop._deepsleep_state import FailurePattern
        from deepsleep_loop.failure_store import failure_key, load_failures, upsert_failure
        from deepsleep_loop.store_locks import FAILURE, store_transaction

        # Cut 13 review: read-and-increment is one transaction — the same
        # frequency race the consolidation phase has, reached from the skill
        # path instead.
        with store_transaction(FAILURE):
            existing = load_failures()
            prev = existing.get(failure_key(kind, context))
            freq = (prev.frequency if prev else 0) + 1
            upsert_failure(
                FailurePattern(kind=kind, context=context, frequency=freq, last_episode=episode_id)
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("skill failure-memory record failed (%s::%s): %s", kind, context, exc)
        return False


def apply_capability_signal(
    signal: Mapping[str, Any],
    *,
    episode_id: str = "",
    domain: str = "",
    scope: str = "dev",
    reinforce_fn: Callable = None,
    weaken_fn: Callable = None,
    record_failure_fn: Callable = None,
) -> dict:
    """Apply a derived CapabilitySignal through the live machinery. Returns a report of what
    was applied. The hooks are injectable so tests prove the routing without disk."""
    sig = signal["signal"]
    name = signal["skill_name"]
    delta = signal["delta"]
    reason = signal.get("reason", "")
    applied = {
        "signal": sig,
        "reinforced": False,
        "weakened": False,
        "failure_recorded": False,
        "safety_recorded": False,
        "bridge_recorded": False,
        "new_strength": None,
        "repair_pressure": bool(signal.get("creates_repair_pressure")),
    }
    try:
        if sig in ("reinforce", "reinforce_partial"):
            applied["new_strength"] = (reinforce_fn or _default_reinforce)(
                name, abs(delta), domain, scope
            )
            applied["reinforced"] = True
        elif sig == "weaken_skill":
            applied["new_strength"] = (weaken_fn or _default_weaken)(
                name, abs(delta), domain, scope, reason
            )
            applied["weakened"] = True
            (record_failure_fn or _default_record_failure)(
                "forbidden_tool", name or "skill", episode_id
            )
            applied["failure_recorded"] = True
        elif sig == "weaken_bridge":
            # weaken bridge reliability only — DO NOT touch the skill's capability strength
            (record_failure_fn or _default_record_failure)("bridge", reason or "bridge", episode_id)
            applied["bridge_recorded"] = True
        elif sig == "safety_memory":
            (record_failure_fn or _default_record_failure)(
                "safety", reason or "blocked", episode_id
            )
            applied["safety_recorded"] = True
        elif sig == "failure_memory":
            (record_failure_fn or _default_record_failure)(
                "skill_failure", reason or "failure", episode_id
            )
            applied["failure_recorded"] = True
        # neutral -> nothing applied (pending approval / ungrounded success)
    except Exception as exc:  # noqa: BLE001
        applied["error"] = str(exc)
    return applied


# ── records + summary ─────────────────────────────────────────────────────────
def _operator_confirmation_status(meta_b: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    if meta_b["outcome_kind"] == "skill_requires_confirmation":
        return "pending"
    gs = result.get("guard_status") or meta_b.get("guard_status") or {}
    if gs.get("confirmation_required"):
        return "confirmed"  # it executed despite needing confirmation = operator confirmed
    return "not_required"


def _workflow_memory_candidate(
    meta_b: Mapping[str, Any], result: Mapping[str, Any], objective: str
) -> Optional[dict]:
    if (
        meta_b["outcome_kind"] != "skill_success"
        or not _grounded(result)
        or not bool(meta_b.get("objective_complete"))
    ):
        return None
    tool_seq = [
        tc.get("tool_name") for tc in (result.get("tool_calls") or []) if isinstance(tc, dict)
    ]
    return {
        "skill_name": meta_b["skill_name"],
        "skill_id": meta_b["skill_id"],
        "execution_kind": meta_b["execution_kind"],
        "objective_pattern": objective,
        "tool_sequence": tool_seq,
        "files_changed": list(result.get("files_changed") or ()),
    }


def build_skill_consolidation_record(
    *,
    meta_b: Mapping[str, Any],
    result: Mapping[str, Any],
    objective: str,
    capability_signal: Mapping[str, Any],
    workflow_memory_candidate: Optional[dict] = None,
) -> SkillConsolidationRecord:
    return SkillConsolidationRecord(
        episode_id=str(meta_b.get("episode_id") or ""),
        dispatch_id=str(meta_b.get("dispatch_id") or ""),
        skill_name=str(meta_b.get("skill_name") or ""),
        skill_id=str(meta_b.get("skill_id") or ""),
        execution_kind=str(meta_b.get("execution_kind") or ""),
        objective=objective,
        outcome=str(meta_b.get("outcome_kind") or ""),
        tool_calls=tuple(result.get("tool_calls") or ()),
        files_changed=tuple(result.get("files_changed") or ()),
        commands_run=tuple(result.get("commands_run") or ()),
        tests_run=tuple(result.get("tests_run") or ()),
        artifacts=tuple(result.get("artifacts") or ()),
        failure_class=meta_b.get("failure_class"),
        unresolved_blockers=tuple(
            meta_b.get("unresolved_blockers") or result.get("unresolved_blockers") or ()
        ),
        evidence=dict(result.get("evidence") or {}),
        operator_confirmation_status=_operator_confirmation_status(meta_b, result),
        capability_signal=dict(capability_signal),
        workflow_memory_candidate=workflow_memory_candidate,
        summary=str(meta_b.get("summary") or result.get("summary") or ""),
    )


def build_deepsleep_skill_summary(
    record: SkillConsolidationRecord, global_b: Mapping[str, Any], applied: Mapping[str, Any]
) -> dict:
    return {
        "type": "deepsleep_skill_summary",
        "episode_id": record.episode_id,
        "dispatch_id": record.dispatch_id,
        "selected_skill": record.skill_name,
        "skill_id": record.skill_id,
        "execution_kind": record.execution_kind,
        "objective": record.objective,
        "outcome": record.outcome,
        "result_summary": record.summary,
        "files_changed": list(record.files_changed),
        "tests_run": list(record.tests_run),
        "capability_change": dict(applied),
        "workflow_pattern": record.workflow_memory_candidate,
        "failure_memory_recorded": bool(
            applied.get("failure_recorded")
            or applied.get("safety_recorded")
            or applied.get("bridge_recorded")
        ),
        "next_pressure": global_b.get("continuation_pressure"),
        "closure": "closed" if global_b.get("close_episode") else "open",
        "why": f"used skill '{record.skill_name}' to: {record.objective}",
    }


# ── orchestrator ──────────────────────────────────────────────────────────────
def consolidate_skill_episode(
    folded: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    objective: str,
    objective_materially_advanced: bool = False,
    domain: str = "",
    scope: str = "dev",
    reinforce_fn: Callable = None,
    weaken_fn: Callable = None,
    record_failure_fn: Callable = None,
) -> dict:
    """Derive the capability signal, apply it, build the Omega record + DeepSleep summary."""
    meta_b = folded["meta_b"]
    global_b = folded["global_b"]

    objective_complete = bool(
        global_b.get("intent_satisfied")
        and global_b.get("objective_complete")
        and not global_b.get("more_work_needed")
        and not (global_b.get("unresolved_blockers") or ())
    )
    signal = derive_capability_signal(
        meta_b,
        result,
        objective_materially_advanced=objective_materially_advanced,
        objective_complete=objective_complete,
    )
    meta_b = dict(meta_b)
    meta_b["objective_complete"] = objective_complete
    serrs = signal.validate()
    if serrs:
        raise SkillConsolidationError("invalid capability signal: " + "; ".join(serrs))

    applied = apply_capability_signal(
        signal.to_dict(),
        episode_id=str(meta_b.get("episode_id") or ""),
        domain=domain,
        scope=scope,
        reinforce_fn=reinforce_fn,
        weaken_fn=weaken_fn,
        record_failure_fn=record_failure_fn,
    )
    wmc = _workflow_memory_candidate(meta_b, result, objective)
    record = build_skill_consolidation_record(
        meta_b=meta_b,
        result=result,
        objective=objective,
        capability_signal=signal.to_dict(),
        workflow_memory_candidate=wmc,
    )
    rerrs = record.validate()
    if rerrs:
        raise SkillConsolidationError("invalid consolidation record: " + "; ".join(rerrs))
    summary = build_deepsleep_skill_summary(record, global_b, applied)
    return {"record": record.to_dict(), "applied": applied, "summary": summary}
