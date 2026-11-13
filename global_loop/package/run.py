"""Global B phase — assemble GlobalOutcomePack and write episode.global_['outcome']."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Mapping

from omega_loop.episode.types import bounded_browser_evidence, bounded_browser_run

from global_loop.assemble import GlobalBInput, auto_inner
from auto_loop.fold.run import (
    plugin_execution_envelope,
    plugin_permission_is_pending,
    screen_plugin_outputs,
)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return parsed


def _clamp(value, lo: float, hi: float, default: float = 0.0) -> float:
    parsed = _safe_float(value, default)
    if parsed < lo:
        return lo
    if parsed > hi:
        return hi
    return parsed


@dataclass(frozen=True)
class GlobalOutcomePack:
    episode_id: str
    outcome: dict
    observations: dict
    artifacts: dict
    followups: list
    consistency: dict
    temporal: dict = None
    learning_state: dict = None
    chemical_state: dict = None
    # WO-06: the post-action phase evidence this pack was derived from, bound verbatim so the
    # nested producer semantics survive (Meta B's execution verdict and criteria checks, AUTO's
    # cycle and failure cause). Each is an Observation, so context Global B never received is
    # ``not_captured`` with a reason and cannot pass for a phase that found nothing.
    phase_evidence: dict = None
    # WO-06: the exact cross-lane mail admitted to this episode's Omega fold. Written once the
    # fold's mail is bound (global_loop.loop.run_global_b), which happens after packaging.
    cross_lane_mail: dict = None


def global_outcome_pack_to_dict(pack: GlobalOutcomePack) -> dict:
    return asdict(pack)


_AUTO_EVIDENCE_KEYS = ("cycle_kind", "status", "failure_kind", "failure_reason", "memory_status")


def phase_evidence(gbi: GlobalBInput, auto_inner_dict: Mapping) -> dict:
    """Meta B's and AUTO's outcome evidence as received, never flattened into one status."""
    from schema.continuity_json import Observation

    reflection = dict(gbi.meta_reflection or {})
    execution = reflection.get("execution")
    if isinstance(execution, Mapping):
        meta_b = Observation.observed({
            "execution": dict(execution),
            "checks": reflection.get("checks"),
            "consistency": reflection.get("consistency"),
        })
    else:
        meta_b = Observation.missing(
            "not_captured",
            reason="Global B assembled this pack without a Meta B reflection, so the execution "
            "verdict and criteria checks it would carry were never received",
        )
    if auto_inner_dict:
        auto = Observation.observed({key: auto_inner_dict.get(key) for key in _AUTO_EVIDENCE_KEYS})
    else:
        auto = Observation.missing("not_captured", reason="AUTO produced no outcome for Global B")
    return {"meta_b": meta_b.to_dict(), "auto": auto.to_dict()}


def run(
    gbi: GlobalBInput,
    *,
    intent: str,
    trajectory_alignment: str,
    divergence_score: float,
    execution_outcome: dict,
    compat_status: str,
    grounded: bool,
    grounding_basis: List[str],
    summary: str,
    followups: List[dict],
    episode_temporal: dict = None,
    episode_learning_state: dict = None,
) -> GlobalOutcomePack:
    auto_inner_dict = auto_inner(gbi.auto_outcome)
    outputs = screen_plugin_outputs(
        dict(auto_inner_dict.get("outputs") or {}),
        preserve_receipts=True,
    )
    if gbi.omega_lane == "browser":
        outputs = {key: (bounded_browser_run(value) if key == "browser_run" else bounded_browser_evidence(value))
                   for key, value in outputs.items() if key in {"browser_run", "tool_result"}}
    tele = dict(auto_inner_dict.get("telemetry") or {})
    exec_block = dict(gbi.meta_reflection.get("execution") or {})
    failure_kind = auto_inner_dict.get("failure_kind")
    failure_reason = auto_inner_dict.get("failure_reason")
    plugin_result = outputs.get("plugin_result")
    plugin_summary = outputs.get("tool_result_summary")
    if not plugin_summary and isinstance(plugin_result, Mapping):
        plugin_summary = plugin_result.get("summary") or plugin_result.get("receipt_summary")
    if not plugin_summary:
        for step in outputs.get("plugin_step_results") or ():
            if isinstance(step, Mapping) and step.get("summary"):
                plugin_summary = step["summary"]
                break
    plugin_execution = plugin_execution_envelope(outputs)
    plugin_permission_pending = plugin_permission_is_pending(auto_inner_dict, outputs)
    if plugin_permission_pending:
        compat_status = "pending_input"
        execution_outcome = dict(execution_outcome or {})
        execution_outcome.update({"executed": False, "status": "pending_input"})
    plugin_artifacts = {
        key: outputs.get(key)
        for key in (
            "plugin_plan",
            "plugin_step_results",
            "plugin_permission_request",
            "plugin_failure",
            "plugin_screen_receipts",
        )
        if outputs.get(key) is not None
    }

    mem_status = exec_block.get("memory_status") or auto_inner_dict.get("memory_status")
    if mem_status is None:
        mem = auto_inner_dict.get("memory")
        if isinstance(mem, Mapping):
            mem_status = mem.get("memory_status")

    from pressure_loop.policy import global_b_memory_success_status

    memory_success = global_b_memory_success_status()
    cycle_kind = str(auto_inner_dict.get("cycle_kind") or "").lower()
    outcome_failed = bool(
        failure_kind
        and cycle_kind not in {"partial", "skipped", "pending_input", "idle"}
        and compat_status not in {"partial", "skipped", "pending_input", "idle"}
    )

    # Compact temporal summary for GLOBAL_B threading (DEFERRED_INTEGRATION.md §1)
    temporal_summary = {}
    if episode_temporal:
        from core.episode_temporal import compact_temporal_summary

        temporal_summary = compact_temporal_summary(episode_temporal)

    # Compact learning state summary for OMEGA threading (DEFERRED_INTEGRATION.md §2)
    learning_state_summary = {}
    if episode_learning_state:
        from pressure_loop.symphony_learning_state import compact_learning_state_summary

        learning_state_summary = compact_learning_state_summary(episode_learning_state)

    # Chemical State for Omega reasoning
    auto_neuromod = (
        auto_inner_dict.get("neuromod")
        if isinstance(auto_inner_dict.get("neuromod"), Mapping)
        else {}
    )
    stress = _clamp(auto_neuromod.get("stress", 0.0), 0.0, 1.0)
    serotonin = _clamp(auto_neuromod.get("serotonin", 0.0), 0.0, 1.0)
    dopamine = _clamp(auto_neuromod.get("dopamine", 0.0), -1.0, 1.0)
    plasticity = _clamp(auto_neuromod.get("plasticity", 0.0), 0.0, 1.0)

    # Apply Meta B neuromod adjustments
    meta_adjustments = dict(gbi.meta_reflection.get("neuromod_adjustments") or {})
    dopamine = _clamp(
        dopamine + _safe_float(meta_adjustments.get("dopamine", 0.0)),
        -1.0,
        1.0,
    )
    plasticity = _clamp(
        plasticity + _safe_float(meta_adjustments.get("plasticity", 0.0)),
        0.0,
        1.0,
    )

    # Noradrenaline (Explore vs Exploit)
    # Driven by divergence score (new trajectories/ideas) and stress
    divergence = _clamp(divergence_score, 0.0, 1.0)
    noradrenaline = _clamp(divergence * 0.8 + stress * 0.2, 0.0, 1.0)

    chemical_state_summary = {
        "dopamine": dopamine,
        "stress": stress,
        "serotonin": serotonin,
        "noradrenaline": noradrenaline,
        "plasticity": plasticity,
    }

    pack = GlobalOutcomePack(
        episode_id=gbi.episode_id,
        # Episode outcome: intent chosen vs trajectory vs execution (no goal, no goal_state).
        outcome={
            "omega_lane": gbi.omega_lane,
            "intent": intent,
            "trajectory_alignment": trajectory_alignment,
            "divergence_score": divergence_score,
            "execution_outcome": execution_outcome,
            "status": compat_status,
            "grounded": grounded,
            "grounding_basis": list(grounding_basis or []),
            "summary": summary,
            "failure_kind": failure_kind,
            "failure_reason": failure_reason,
        },
        observations={
            "response_text_present": outputs.get("response_text") is not None,
            "tool_result_present": (
                outputs.get("tool_result") is not None or plugin_result is not None
            ),
            "image_artifact_present": outputs.get("image_artifact") is not None,
            "document_artifact_present": outputs.get("document_artifact") is not None,
            "plugin_evidence_present": plugin_execution is not None,
            "plugin_permission_pending": plugin_permission_pending,
            "plugin_failure_present": outputs.get("plugin_failure") is not None,
            "memory_used": str(mem_status or "") == memory_success,
            "warnings": list(tele.get("warnings") or exec_block.get("warnings") or []),
        },
        artifacts={
            "original_objective": str(gbi.request_context.get("text") or ""),
            "browser_perception": (
                bounded_browser_evidence((gbi.execution_pack.get("execution_inputs") or {}).get("browser_perception"))
            ),
            "browser_run": outputs.get("browser_run"),
            "agent_run": outputs.get("agent_run"),
            "window_view": gbi.window_view,
            "response_text": outputs.get("response_text"),
            "tool_result_summary": plugin_summary,
            "tool_result": (
                outputs.get("tool_result")
                if outputs.get("tool_result") is not None
                else plugin_result
            ),
            "image_artifact": outputs.get("image_artifact"),
            "document_artifact": outputs.get("document_artifact"),
            "plugin_execution": plugin_execution,
            **plugin_artifacts,
        },
        followups=followups,
        consistency=dict(gbi.meta_reflection.get("consistency") or {}),
        temporal=temporal_summary,
        learning_state=learning_state_summary,
        chemical_state=chemical_state_summary,
        phase_evidence=phase_evidence(gbi, auto_inner_dict),
    )

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "global",
            "global_outcome_pack",
            episode_id=gbi.episode_id,
            summary=summary[:120] if summary else "",
            grounded=grounded,
            followup_count=len(followups),
            status=compat_status,
            ok=(compat_status not in ("failed", "fail", "error") and not outcome_failed),
            failure_kind=failure_kind,
            failure_reason=failure_reason,
            trajectory_alignment=trajectory_alignment,
            divergence_score=divergence_score,
        )
    except Exception:
        pass

    return pack


def write_outcome(episode, pack: GlobalOutcomePack) -> None:
    global_ = dict(getattr(episode, "global_", None) or {})
    global_["outcome"] = global_outcome_pack_to_dict(pack)
    episode.global_ = global_
