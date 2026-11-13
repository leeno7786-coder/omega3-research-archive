"""SkillExecutionPack — Meta A's bounded hand-off to Auto (Phase 6G).

The pack is the Meta -> Auto contract for a USE_SKILL episode. It carries the selected
skill, the tools it may/may not use, the safety envelope, the hard limits, the evidence
required, and an EXACT echo of the source action goal. It deliberately carries NO prompt
and NO qwen/model settings — Auto builds the qwen directive from the SKILL.md body in 6H.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SKILL_PACK_SCHEMA_VERSION = "omega.skill_execution_pack.v1"


class SkillPackError(ValueError):
    """Raised when a SkillExecutionPack cannot be built or fails validation."""


def _dispatch_id(episode_id: str, skill_id: str, objective: str) -> str:
    blob = json.dumps([episode_id, skill_id, objective], sort_keys=True, ensure_ascii=False)
    return "dispatch_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def safety_envelope(safety_class: str, *, requires_confirmation_for: tuple = ()) -> dict:
    """Derive the boolean capability envelope from a skill's safety_class + its declared
    confirmation requirements. Conservative: a capability is allowed only when the class
    grants it."""
    sc = safety_class
    requires_confirmation = bool(requires_confirmation_for) or sc in (
        "destructive",
        "desktop_control",
        "self_modification",
    )
    return {
        "safety_class": sc,
        "write_allowed": sc
        in ("write_safe", "command_safe", "destructive", "desktop_control", "self_modification"),
        "network_allowed": sc == "network_read",
        "command_allowed": sc in ("command_safe", "destructive", "desktop_control"),
        "desktop_allowed": sc == "desktop_control",
        "destructive_allowed": sc == "destructive",
        "requires_confirmation": requires_confirmation,
    }


@dataclass(frozen=True)
class SkillExecutionPack:
    episode_id: str
    dispatch_id: str
    objective: str
    selected_skill: dict
    allowed_tools: tuple
    forbidden_tools: tuple
    safety: dict
    limits: dict
    evidence_required: tuple
    completion_criteria: tuple
    expected_output_schema: dict
    source_action_goal: dict
    action: str = "USE_SKILL"
    type: str = "skill_execution_pack"
    schema_version: str = SKILL_PACK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "dispatch_id": self.dispatch_id,
            "action": self.action,
            "objective": self.objective,
            "selected_skill": dict(self.selected_skill),
            "allowed_tools": list(self.allowed_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "safety": dict(self.safety),
            "limits": dict(self.limits),
            "evidence_required": list(self.evidence_required),
            "completion_criteria": list(self.completion_criteria),
            "expected_output_schema": dict(self.expected_output_schema),
            "source_action_goal": dict(self.source_action_goal),
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.dispatch_id:
            errors.append("missing dispatch_id")
        if not self.episode_id:
            errors.append("missing episode_id")
        if self.action != "USE_SKILL":
            errors.append(f"action must be USE_SKILL, got {self.action!r}")
        if not self.objective:
            errors.append("missing objective")
        for key in ("skill_name", "skill_id", "skill_class", "execution_kind", "source_path"):
            if not self.selected_skill.get(key):
                errors.append(f"selected_skill missing {key}")
        if "safety_class" not in self.safety:
            errors.append("safety missing safety_class")
        # the pack must NOT carry a prompt or qwen/model settings (that is Auto's 6H job)
        forbidden_keys = {
            "prompt",
            "skill_markdown",
            "model",
            "model_settings",
            "temperature",
            "messages",
        }
        leaked = forbidden_keys & set(self.to_dict())
        if leaked:
            errors.append(f"pack must not carry prompt/model fields: {sorted(leaked)}")
        return errors


def rebind_skill_pack_to_episode(pack: dict, episode_id: str) -> dict:
    """Bind an already-built pack dict to a different episode. IDENTITY ONLY.

    Phase 4 risk 9 (ruling 2026-08-11). The agent lane carries the pack its
    parent chat episode dispatched, and the executor's readers all require the
    pack to name the ACTIVE episode (``auto_loop.intake.run`` checks four
    episode identities and raises when any disagrees). This is the pack's
    counterpart to ``MotorProgram``'s rebind, and it changes exactly the same
    class of field: who is executing, never what is being executed.

    ``dispatch_id`` is RECOMPUTED rather than carried, because it is derived
    from ``(episode_id, skill_id, objective)`` and the foldback validators
    compare it against the executor result produced from THIS pack
    (``omega_loop.episode.packet``, ``cognition_chain``). A carried dispatch_id
    would name a hash of an episode that is not running, so every foldback it
    reached would raise on a mismatch it could never resolve.

    The selection itself — skill, tools, safety envelope, limits, evidence and
    completion terms — is returned verbatim. That is the whole point: those are
    the decision the operator was told was under way.
    """
    ep_id = str(episode_id or "")
    if not ep_id:
        raise SkillPackError("episode_id is required to rebind a skill execution pack")
    if not isinstance(pack, dict):
        raise SkillPackError("a skill execution pack must be an object to rebind")
    rebound = json.loads(json.dumps(pack, ensure_ascii=False, sort_keys=False, default=str))
    rebound["episode_id"] = ep_id
    source = rebound.get("source_action_goal")
    if isinstance(source, dict):
        source["episode_id"] = ep_id
    skill_id = str(dict(rebound.get("selected_skill") or {}).get("skill_id") or "")
    rebound["dispatch_id"] = _dispatch_id(ep_id, skill_id, str(rebound.get("objective") or ""))
    return rebound


def build_skill_execution_pack(
    *,
    episode_id: str,
    objective: str,
    decision,
    source_action_goal: dict,
    global_forbidden_tools: tuple = (),
) -> SkillExecutionPack:
    """Build + validate a SkillExecutionPack from a SelectedSkillDecision.

    Raises SkillPackError on a failed selection (the failed_to_initiate path) or if the
    assembled pack fails validation. allowed_tools come ONLY from the selected skill;
    forbidden_tools = the skill's forbidden + global policy forbidden.
    """
    if not getattr(decision, "selected", False):
        raise SkillPackError(
            f"failed_to_initiate: no selected skill ({getattr(decision, 'failure_reason', None)})"
        )
    if not episode_id:
        raise SkillPackError("episode_id is required")
    if not objective:
        raise SkillPackError("objective is required")

    dispatch_id = _dispatch_id(episode_id, decision.skill_id, objective)
    forbidden = tuple(sorted(set(decision.forbidden_tools) | set(global_forbidden_tools)))
    # Carry the skill's declared requires_confirmation_for through to the safety envelope so a
    # skill that demands confirmation — even in a class that does NOT force it (e.g. command_safe)
    # — is not silently bypassed. build_qwen_skill_dispatch copies pack.safety to the guard verbatim.
    safety = safety_envelope(
        decision.safety_class,
        requires_confirmation_for=tuple(getattr(decision, "requires_confirmation_for", ()) or ()),
    )

    pack = SkillExecutionPack(
        episode_id=str(episode_id),
        dispatch_id=dispatch_id,
        objective=str(objective),
        selected_skill={
            "skill_name": decision.skill_name,
            "skill_id": decision.skill_id,
            "skill_class": decision.skill_class,
            "execution_kind": decision.execution_kind,
            "source_path": decision.source_path,
        },
        allowed_tools=tuple(decision.allowed_tools),
        forbidden_tools=forbidden,
        safety=safety,
        limits=dict(decision.limits),
        evidence_required=tuple(decision.evidence_required),
        completion_criteria=tuple(decision.completion_criteria),
        expected_output_schema=dict(decision.output_schema),
        source_action_goal=dict(source_action_goal or {}),
    )
    errors = pack.validate()
    if errors:
        raise SkillPackError("; ".join(errors))
    return pack
