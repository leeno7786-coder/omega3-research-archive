"""Meta B consistency phase — assemble input + packet consistency check."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class MetaBInput:
    episode_id: str
    pressure: dict
    source_action_goal: dict
    execution_pack: dict
    auto_outcome: dict
    # WO-06: the objective's contract, for the criteria checks. ``None`` when the episode
    # carries no (non-shadow) motor program — an objective with nothing recorded to check.
    motor_program: Optional[dict] = None
    # AUTO's declared normalization of the source plan, never of its execution echo.
    expected_execution_pack: Optional[dict] = None


def _pack_stable_keys() -> Tuple[str, ...]:
    from pressure_loop.policy import meta_b_pack_stable_fields

    # These fields determine WHAT runs, where it runs, and which memories may
    # inform it. Their integrity cannot depend on a telemetry policy omitting them.
    contract_fields = (
        "source_action_goal",
        "execution_inputs", "execution_constraints", "memory_required",
        "memory_answer_required", "memory_reason", "memory_scope", "memory_query",
        "memory_delivery_mode", "memory_tools",
    )
    return tuple(dict.fromkeys((*meta_b_pack_stable_fields(), *contract_fields)))


def _stable_subset(data: Mapping[str, Any], keys: Tuple[str, ...]) -> dict:
    return {k: data.get(k) for k in keys}


def meta_b_input_from_episode(episode) -> Optional[MetaBInput]:
    """Assemble MetaBInput; None if episode.auto missing."""
    folded = getattr(episode, "auto", None)
    if not folded:
        return None

    auto_dict = dict(folded)
    global_ = dict(getattr(episode, "global_", None) or {})
    meta = dict(getattr(episode, "meta", None) or {})
    action_goal = dict(
        global_.get("intent_decision")
        or global_.get("action_goal")
        or meta.get("execution_action_pack", {}).get("source_action_goal")
        or {}
    )
    execution_pack = dict(meta.get("execution_action_pack") or {})
    pressure_raw = dict(getattr(episode, "pressure", None) or {})
    opened_by = str(pressure_raw.get("opened_by") or getattr(episode, "opened_by", "") or "")
    contributors = list(pressure_raw.get("contributors") or [])
    aggregate_pressure = pressure_raw.get("aggregate_pressure", 0.0)
    neuromod_state = dict(pressure_raw.get("neuromod_state") or {})

    eid = str(
        getattr(episode, "episode_id", "")
        or auto_dict.get("episode_id")
        or action_goal.get("episode_id")
        or ""
    )
    auto_echo_pack = {}
    embedded = auto_dict.get("execution_pack")
    if isinstance(embedded, Mapping):
        auto_echo_pack = dict(embedded)
    auto_outcome = {
        "episode_id": eid,
        "source_action_goal": dict(auto_dict.get("source_action_goal") or {}),
        "execution_pack": auto_echo_pack,
        "auto": auto_dict,
    }
    program = meta.get("motor_program")
    from auto_loop.intake.run import normalized_execution_pack

    expected_pack, _ = normalized_execution_pack(
        execution_pack,
        episode_id=eid,
        contributors=contributors,
        motor_program=program,
    )
    return MetaBInput(
        episode_id=eid,
        pressure={
            "reason": opened_by,
            "contributors": contributors,
            "aggregate_pressure": aggregate_pressure,
            "neuromod_state": neuromod_state,
        },
        source_action_goal=action_goal,
        execution_pack=execution_pack,
        auto_outcome=auto_outcome,
        motor_program=(
            dict(program)
            if isinstance(program, Mapping) and program.get("shadow") is not True
            else None
        ),
        expected_execution_pack=expected_pack,
    )


def _auto_payload(auto_outcome: Mapping[str, Any]) -> dict:
    inner = auto_outcome.get("auto")
    if isinstance(inner, Mapping):
        return dict(inner)
    return dict(auto_outcome)


def run(mbi: MetaBInput) -> Tuple[dict, bool]:
    """Return (consistency dict, critical mismatch flag)."""
    reasons: list[str] = []
    goal = dict(mbi.source_action_goal)
    pack = dict(mbi.execution_pack)
    outcome = dict(mbi.auto_outcome)
    auto_goal = dict(outcome.get("source_action_goal") or {})
    auto_pack = dict(outcome.get("execution_pack") or {})
    auto_inner = _auto_payload(outcome)
    pack_goal = dict(pack.get("source_action_goal") or {})
    auto_pack_goal = dict(auto_pack.get("source_action_goal") or {})

    eids = {
        str(mbi.episode_id or ""),
        str(goal.get("episode_id") or ""),
        str(pack.get("episode_id") or ""),
        str(outcome.get("episode_id") or ""),
        str(auto_goal.get("episode_id") or ""),
        str(auto_pack.get("episode_id") or ""),
        str(auto_inner.get("episode_id") or ""),
        str(pack_goal.get("episode_id") or ""),
        str(auto_pack_goal.get("episode_id") or ""),
    }
    eids.discard("")
    episode_id_mismatch = len(eids) > 1

    expected_pack = mbi.expected_execution_pack if mbi.expected_execution_pack is not None else pack
    pack_stable_src = _stable_subset(expected_pack, _pack_stable_keys())
    pack_stable_auto = _stable_subset(auto_pack, _pack_stable_keys())
    pack_matches = pack_stable_src == pack_stable_auto
    if not pack_matches:
        reasons.append("execution_pack stable fields differ from auto_outcome")
    source_mismatch = goal != auto_goal
    if source_mismatch:
        reasons.append("Global A source differs from auto_outcome")

    action_src = str(pack.get("action") or "")
    action_auto = str(auto_pack.get("action") or auto_inner.get("action") or "")
    tool_src = pack.get("tool")
    tool_auto = auto_pack.get("tool")
    if tool_auto is None:
        tool_auto = auto_inner.get("tool")

    action_mismatch = bool(action_src or action_auto) and action_src != action_auto
    tool_mismatch = (tool_src is not None or tool_auto is not None) and tool_src != tool_auto

    if episode_id_mismatch:
        reasons.append("episode_id mismatch across packets")
    if action_mismatch:
        reasons.append("action mismatch between execution_pack and auto_outcome")
    if tool_mismatch:
        reasons.append("tool mismatch between execution_pack and auto_outcome")

    from schema.omega_control_decision import OMEGA_LANES

    lanes = [row.get("omega_lane", "chat") for row in (
        goal, auto_goal, dict(pack.get("source_action_goal") or goal),
        dict(auto_pack.get("source_action_goal") or auto_goal))]
    lane_mismatch = any(lane not in OMEGA_LANES for lane in lanes) or len(set(lanes)) > 1
    if lane_mismatch:
        reasons.append("omega lane mismatch across packets")

    # The stable pack fields are the execution contract. Any drift means AUTO did
    # not execute the plan Meta A produced, even when action/tool happen to match.
    critical = (not pack_matches or source_mismatch or episode_id_mismatch
                or action_mismatch or tool_mismatch or lane_mismatch)
    if critical:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "meta",
            "consistency",
            status="fail",
            reason="critical_mismatch",
            ok=False,
        )
    consistency = {
        "pack_matches": pack_matches and not source_mismatch and not episode_id_mismatch,
        "mismatch_reasons": reasons,
    }
    return consistency, critical
