"""AUTO intake phase — episode → AutoInput."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Optional, Sequence

from auto_loop._auto_state import AutoInput


def normalized_execution_pack(
    execution_pack: Mapping[str, Any],
    *,
    episode_id: str,
    contributors: Sequence[Mapping[str, Any]] = (),
    motor_program: Optional[dict] = None,
) -> tuple[dict, Optional[dict]]:
    """Apply AUTO's declared intake transformations to an independent plan copy.

    Meta B uses this same pure projection for its EXPECTED contract. The actual
    execution echo must never pass through it: normalizing an executed value
    could hide a changed argument, program or memory requirement.
    """
    pack = deepcopy(dict(execution_pack))
    execution_inputs = pack.get("execution_inputs")
    if execution_inputs is not None and not isinstance(execution_inputs, dict):
        raise TypeError("execution_inputs must be an object")
    execution_inputs = dict(execution_inputs or {})
    skill_pack = execution_inputs.get("skill_execution_pack")
    contributor_invocations = []
    for row in contributors:
        if not isinstance(row, dict):
            continue
        detail = row.get("metadata") or row.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        candidate = detail.get("job_invocation")
        if isinstance(candidate, dict):
            contributor_invocations.append(deepcopy(candidate))
    if contributor_invocations:
        admitted_invocation = contributor_invocations[0]
        if any(candidate != admitted_invocation for candidate in contributor_invocations):
            raise ValueError("conflicting job_invocation contributor metadata")
        pack_invocation = execution_inputs.get("job_invocation")
        if pack_invocation is None:
            execution_inputs["job_invocation"] = deepcopy(admitted_invocation)
            pack["execution_inputs"] = execution_inputs
        elif not isinstance(pack_invocation, dict) or pack_invocation != admitted_invocation:
            raise ValueError("execution pack job_invocation differs from admitted contributor")
    canonical_program = None
    raw_program = motor_program
    if isinstance(raw_program, dict) and raw_program.get("shadow") is True:
        raw_program = None
        execution_inputs = dict(pack.get("execution_inputs") or {})
        transported = execution_inputs.get("motor_program")
        if isinstance(transported, dict) and transported.get("shadow") is True:
            execution_inputs.pop("motor_program", None)
            pack["execution_inputs"] = execution_inputs
    if isinstance(raw_program, dict):
        from pressure_loop.motor_loop_guard import (
            material_pathway_fingerprint,
            material_pathway_signature,
        )
        from schema.motor_program import MotorProgram

        parsed = MotorProgram.from_dict(raw_program)
        if parsed.episode_id != episode_id:
            raise ValueError("motor program episode_id does not match the active episode")
        fingerprint = parsed.fingerprint()
        material_fingerprint = material_pathway_fingerprint(parsed)
        if raw_program.get("fingerprint") not in (None, "", fingerprint):
            raise ValueError("attached motor_program fingerprint is stale or corrupt")
        if raw_program.get("material_fingerprint") not in (
            None,
            "",
            material_fingerprint,
        ):
            raise ValueError("attached motor_program material fingerprint is stale or corrupt")
        canonical_program = {
            **parsed.to_dict(),
            "fingerprint": fingerprint,
            "material_fingerprint": material_fingerprint,
            "material_signature": material_pathway_signature(parsed),
            "shadow": False,
        }
        transported = dict(pack.get("execution_inputs") or {}).get("motor_program")
        if transported is not None:
            if not isinstance(transported, dict):
                raise TypeError("transported motor_program must be an object")
            transported_program = MotorProgram.from_dict(transported)
            if transported_program.to_dict() != parsed.to_dict():
                raise ValueError("transported motor_program conflicts with episode canonical")
            pack["execution_inputs"]["motor_program"] = deepcopy(canonical_program)
        if isinstance(skill_pack, dict):
            if str(skill_pack.get("objective") or "").strip() != parsed.outcome_contract.objective:
                raise ValueError("skill execution pack objective conflicts with motor program")

    from auto_loop.mril_tools import is_browser_route

    if is_browser_route(pack):
        pack.update(
            memory_required=False,
            memory_answer_required=False,
            memory_reason=None,
            memory_query=None,
            memory_scope=[],
            memory_tools=[],
        )
    return pack, canonical_program


def auto_input_from_episode(episode) -> Optional[AutoInput]:
    """Assemble AutoInput from episode; None if no execution_action_pack."""
    meta = dict(getattr(episode, "meta", None) or {})
    pack = dict(meta.get("execution_action_pack") or {})
    if not pack:
        return None

    global_ = dict(getattr(episode, "global_", None) or {})
    action_goal = dict(
        global_.get("intent_decision")
        or global_.get("action_goal")
        or pack.get("source_action_goal")
        or {}
    )
    episode_id = str(getattr(episode, "episode_id", "") or "").strip()
    pack_episode_id = str(pack.get("episode_id") or "").strip()
    pack_source = pack.get("source_action_goal")
    pack_source_episode_id = str(
        pack_source.get("episode_id") if isinstance(pack_source, dict) else ""
    ).strip()
    action_goal_episode_id = str(action_goal.get("episode_id") or "").strip()
    identities = (episode_id, pack_episode_id, pack_source_episode_id, action_goal_episode_id)
    if any(not identity for identity in identities) or len(set(identities)) != 1:
        raise ValueError("execution pack episode identity does not match the active episode")
    execution_inputs = pack.get("execution_inputs")
    if execution_inputs is not None and not isinstance(execution_inputs, dict):
        raise TypeError("execution_inputs must be an object")
    skill_pack = dict(execution_inputs or {}).get("skill_execution_pack")
    if skill_pack is not None:
        if not isinstance(skill_pack, dict):
            raise TypeError("skill_execution_pack must be an object")
        skill_source = skill_pack.get("source_action_goal")
        skill_episode_id = str(skill_pack.get("episode_id") or "").strip()
        skill_source_episode_id = str(
            skill_source.get("episode_id") if isinstance(skill_source, dict) else ""
        ).strip()
        if (
            not skill_episode_id
            or not skill_source_episode_id
            or skill_episode_id != episode_id
            or skill_source_episode_id != episode_id
        ):
            raise ValueError(
                "skill execution pack episode identity does not match the active episode"
            )

    pressure_raw = dict(getattr(episode, "pressure", None) or {})
    opened_by = str(pressure_raw.get("opened_by") or getattr(episode, "opened_by", "") or "")
    contributors = list(pressure_raw.get("contributors") or [])
    pack, canonical_program = normalized_execution_pack(
        pack,
        episode_id=episode_id,
        contributors=contributors,
        motor_program=meta.get("motor_program"),
    )
    traj = pressure_raw.get("trajectory_snapshot")
    if not isinstance(traj, dict):
        orient = dict(global_.get("orient") or {})
        traj_candidate = orient.get("trajectory")
        traj = dict(traj_candidate) if isinstance(traj_candidate, dict) else {}

    try:
        from guardian.terminal_feedback import emit_data, emit_guardian_event

        emit_data(
            "auto", "intake", episode_id=episode_id or None,
            summary=f"action={pack.get('action')}", action=pack.get("action"), opened_by=opened_by,
        )
        emit_guardian_event(
            "omega.auto", "intake", episode_id=episode_id,
            action=pack.get("action"), opened_by=opened_by,
        )
    except Exception:
        pass

    return AutoInput(
        episode_id=episode_id,
        pressure={"reason": opened_by, "contributors": contributors},
        source_action_goal=deepcopy(action_goal),
        execution_pack=pack,
        trajectory_snapshot=dict(traj),
        motor_program=deepcopy(canonical_program),
        temporal=dict(getattr(episode, "temporal", None) or {}) or None,
    )


def run(episode) -> Optional[AutoInput]:
    return auto_input_from_episode(episode)
