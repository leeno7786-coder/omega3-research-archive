"""Attachment and telemetry for the attempt's execution contract.

The contract itself is projected deterministically by ``meta_loop.execution_contract``;
no model authors motor programs and no inference runs between Meta A and AUTO.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from copy import deepcopy
from typing import Any

from pressure_loop.motor_loop_guard import (
    material_pathway_fingerprint,
    material_pathway_signature,
)
from pressure_loop.policy import motor_programs_shadow_mode
from schema.motor_program import MotorProgram

log = logging.getLogger("omega.omega_loop.motor")


def attach_motor_program(episode: Any, program: MotorProgram) -> dict[str, Any]:
    """Attach one exact serialized program and optionally expose it to AUTO."""
    shadow = motor_programs_shadow_mode()
    meta = getattr(episode, "meta", None)
    if not isinstance(meta, MutableMapping):
        meta = {}
        setattr(episode, "meta", meta)

    attached = {
        **program.to_dict(),
        "fingerprint": program.fingerprint(),
        "material_fingerprint": material_pathway_fingerprint(program),
        "material_signature": material_pathway_signature(program),
        "shadow": shadow,
    }
    execution_pack = meta.get("execution_action_pack")
    transport = ""
    if isinstance(execution_pack, MutableMapping):
        transport = str(execution_pack.get("action") or "")
    if shadow:
        # Shadow plans are observability only.  Keeping them under a distinct key
        # prevents AUTO intake, the Omega packet, journal/DeepSleep, and pathway
        # credit from mistaking an unexecuted proposal for a real attempt.
        meta.pop("motor_program", None)
        meta["shadow_motor_program"] = deepcopy(attached)
    else:
        meta.pop("shadow_motor_program", None)
        meta["motor_program"] = deepcopy(attached)
        if isinstance(execution_pack, MutableMapping):
            execution_inputs = execution_pack.get("execution_inputs")
            if not isinstance(execution_inputs, MutableMapping):
                execution_inputs = {}
                execution_pack["execution_inputs"] = execution_inputs
            skill_pack = execution_inputs.get("skill_execution_pack")
            if isinstance(skill_pack, MutableMapping):
                # Meta A selects the execution skill; once its canonical contract is
                # bound, the contract's objective/evidence/completion terms are
                # authoritative all the way through AUTO, agent dispatch, and foldback.
                from schema.qwen_skill_dispatch import motor_authority

                objective, evidence_required, completion_criteria, _ = motor_authority(attached)
                synchronized_skill_pack = {
                    **deepcopy(dict(skill_pack)),
                    "objective": objective,
                    "evidence_required": list(evidence_required),
                    "completion_criteria": list(completion_criteria),
                }
                execution_inputs["skill_execution_pack"] = deepcopy(synchronized_skill_pack)
                meta["skill_execution_pack"] = deepcopy(synchronized_skill_pack)
            # Canonical episode state and executor transport never share mutable
            # identity; both were rebuilt from the frozen MotorProgram above.
            execution_inputs["motor_program"] = deepcopy(attached)

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "omega",
            "motor_program",
            program_id=program.program_id,
            parent_program_id=program.parent_program_id,
            outcome_id=program.outcome_id,
            episode_id=program.episode_id,
            pathway_id=program.pathway.pathway_id,
            pathway_revision=program.pathway.revision,
            fingerprint=attached["fingerprint"],
            transport=transport,
            shadow=shadow,
        )
    except Exception:
        log.debug("motor program telemetry failed", exc_info=True)
    return deepcopy(attached)
