"""Deterministic execution contract for the current attempt.

Meta A selects the capability; this host projection turns that validated selection
into the canonical MotorProgram that AUTO, the execution foldback, attempt memory,
the loop guard and DeepSleep's verified-mastery gate read. No model authors it and
no inference runs between Meta A and AUTO. A carried agent program is rebound
verbatim. A pack whose declared terms cannot form a valid contract runs
uncontracted and says so in telemetry, rather than inventing checks it never asked for.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from pressure_loop.policy import motor_programs_eligible_actions, motor_programs_enabled
from schema.motor_program import MotorProgram, evidence_lane

log = logging.getLogger("omega.meta_loop.execution_contract")

_WEB_RESEARCH_TOOLS = frozenset({"web_fetch", "web_search"})


class ExecutionContractUnavailable(ValueError):
    """The selected capability's declared terms cannot form a valid contract."""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _texts(values: Any) -> list[str]:
    if isinstance(values, str):
        values = (values,)
    return [text for value in values or () if (text := str(value).strip())]


def _tool_objective(inputs: Mapping[str, Any], pressure: Mapping[str, Any]) -> str:
    proposed = _mapping(pressure.get("proposed_action"))
    for value in (
        proposed.get("intended_transformation"),
        pressure.get("gap_description"),
        inputs.get("query"),
        inputs.get("objective"),
        inputs.get("question"),
        inputs.get("message"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping) and value:
            return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return ""


def _identity(episode_id: str, inputs: Mapping[str, Any], objective: str, created_at: str) -> dict:
    """Host-assigned identity, unchanged from the retired planner so lineage stays continuous."""
    pressure = _mapping(inputs.get("emergent_pressure"))
    failed = _texts(pressure.get("failed_program_ids"))
    attempt = int(inputs.get("attempt_number") or (len(failed) + 1 if failed else 1))
    seed = objective or episode_id
    return {
        "program_id": f"mp_{episode_id}_{attempt:02d}",
        "parent_program_id": str(inputs.get("parent_program_id") or (failed[-1] if failed else "")),
        "outcome_id": str(
            pressure.get("outcome_id") or hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        ),
        "episode_id": episode_id,
        "attempt_number": attempt,
        "created_at": created_at,
    }


def execution_contract_for(episode: Any) -> MotorProgram | None:
    """Project the episode's validated execution pack into its contract, or None when ineligible.

    Raises plain ValueError for substrate mismatches and ExecutionContractUnavailable when
    the selected capability's own terms cannot form a valid contract.
    """
    meta = _mapping(getattr(episode, "meta", None))
    pack = _mapping(meta.get("execution_action_pack"))
    action = str(pack.get("action") or "").strip()
    if action not in motor_programs_eligible_actions():
        return None
    episode_id = str(getattr(episode, "episode_id", "") or "").strip()
    if not episode_id or str(pack.get("episode_id") or "").strip() != episode_id:
        raise ValueError("execution contract pack episode_id mismatch")
    created_at = str(getattr(episode, "opened_at", "") or "").strip()
    if not created_at:
        raise ValueError("execution contract requires the episode's opened_at timestamp")

    inputs = _mapping(pack.get("execution_inputs"))
    pressure = _mapping(inputs.get("emergent_pressure"))
    transport = str(inputs.get("motor_transport") or "") if action == "EXECUTE_MOTOR_PROGRAM" else action
    if transport == "USE_SKILL":
        skill = _mapping(inputs.get("skill_execution_pack"))
        if not skill:
            return None  # skill selection failed; AUTO reports that failure itself
        selected = _mapping(skill.get("selected_skill"))
        kind = "skill"
        name = str(selected.get("skill_name") or selected.get("skill_id") or "").strip()
        objective = str(skill.get("objective") or "").strip()
        capability = str(selected.get("skill_class") or selected.get("execution_kind") or "").strip()
        declared = _texts(skill.get("evidence_required"))
        criteria = _texts(skill.get("completion_criteria"))
        rationale = str(selected.get("reason") or "").strip()
    elif transport == "USE_TOOL":
        kind = "tool"
        name = str(pack.get("tool") or "").strip()
        if not name:
            return None
        objective = _tool_objective(inputs, pressure)
        capability = "web_research" if name in _WEB_RESEARCH_TOOLS else name
        declared = ["fetch"] if capability == "web_research" else ["content_present"]
        criteria = []
        rationale = ""
    else:
        return None
    if not objective:
        raise ExecutionContractUnavailable(f"{kind} {name!r} has no objective to contract")

    criteria = criteria or _texts(pressure.get("resolution_conditions")) or [
        f"factual evidence shows: {objective}"
    ]
    lanes: dict[str, list[str]] = {"check": [], "source_proof": [], "artifact": []}
    for requirement in declared:
        lanes[evidence_lane(requirement)].append(requirement)
    identity = _identity(episode_id, inputs, objective, created_at)
    candidate = {
        **identity,
        "outcome_contract": {"objective": objective, "acceptance_criteria": criteria},
        "pathway": {
            "pathway_id": f"{kind}:{name}",
            "revision": 1,
            "name": name,
            "method": f"execute the {name} {kind} Meta A selected for this objective",
            "operational_rationale": rationale
            or str(pack.get("tool_reason") or pack.get("execution_intent") or f"Meta A selected {name}"),
            "failed_predecessor_id": identity["parent_program_id"],
        },
        "execution_plan": {
            "capability_class": capability,
            "operations": [objective],
            "required_capabilities": _texts(_mapping(pressure.get("proposed_action")).get("required_capabilities")),
        },
        "evidence_contract": {
            "required_artifacts": lanes["artifact"],
            "required_source_proof": lanes["source_proof"],
            "required_checks": lanes["check"],
            "completion_signals": criteria,
        },
    }
    try:
        return MotorProgram.from_dict(candidate)
    except (TypeError, ValueError) as exc:
        raise ExecutionContractUnavailable(str(exc)) from exc


def _report_uncontracted(episode: Any, reason: str) -> None:
    episode_id = str(getattr(episode, "episode_id", "") or "")
    log.warning("episode %s runs without an execution contract: %s", episode_id, reason)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "meta",
            "execution_contract_unavailable",
            episode_id=episode_id,
            summary=reason[:200],
            status="degraded",
            ok=False,
        )
    except Exception:
        log.debug("execution contract telemetry failed", exc_info=True)


#: WO-04 reads this off the episode to record WHY an attempt is contracted or not. It is a
#: plain outcome note written by the owner that made the decision — not a capture call, so this
#: module stays free of memory concerns and its own unit tests need no episode SRAM owner.
CONTRACT_OUTCOME_KEY = "execution_contract_outcome"


def _note_outcome(episode: Any, *, status: str, reason: str = "") -> None:
    meta = dict(getattr(episode, "meta", None) or {})
    meta[CONTRACT_OUTCOME_KEY] = {"status": str(status), "reason": str(reason or "")}
    episode.meta = meta


def bind_execution_contract(episode: Any) -> dict[str, Any] | None:
    """Attach the attempt's execution contract; return the attached dict, or None when uncontracted."""
    from omega_loop.motor.run import attach_motor_program
    from pressure_loop import agent_lane

    carried = agent_lane.carried_program(episode)
    if carried is not None:
        # A durable agent row names the exact program its parent bound. Installing the
        # dispatched pack first lets attachment synchronize that same contract onto it.
        agent_lane.restore_carried_pack(episode)
        attached = attach_motor_program(episode, carried)
        _note_outcome(episode, status="carried")
        return attached
    if not motor_programs_enabled():
        _note_outcome(episode, status="uncontracted",
                      reason="motor programs are disabled for this profile")
        return None
    try:
        program = execution_contract_for(episode)
    except ExecutionContractUnavailable as exc:
        _report_uncontracted(episode, str(exc))
        _note_outcome(episode, status="blocked", reason=str(exc))
        return None
    if program is None:
        _note_outcome(episode, status="uncontracted",
                      reason="the selected action is not contract-eligible")
        return None
    attached = attach_motor_program(episode, program)
    _note_outcome(episode, status="contracted")
    return attached
