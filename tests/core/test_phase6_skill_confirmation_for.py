"""High #5 — a skill's declared requires_confirmation_for was DROPPED at selection.

SelectedSkillDecision.from_skill copied every safety field EXCEPT requires_confirmation_for,
and build_skill_execution_pack then derived the safety envelope from the safety_class ALONE
(safety_envelope honors requires_confirmation_for, but was never given it). For a command_safe
skill (whose class does NOT force confirmation) that declared requires_confirmation_for, the
'requires_confirmation' flag silently became False — and build_qwen_skill_dispatch copies
pack.safety verbatim, so the guard never demanded confirmation. The declaration must survive
selection -> pack -> dispatch.
"""

from __future__ import annotations

from schema.execution_skill import ExecutionSkill
from schema.selected_skill import SelectedSkillDecision
from schema.skill_execution_pack import build_skill_execution_pack

_LIMITS = {"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": 10}


def _skill(requires_confirmation_for=("run_command",)):
    return ExecutionSkill(
        name="cmd-skill",
        skill_id="5.01.qwenskill-cmd-skill",
        kind="execution_skill",
        version=1,
        enabled=True,
        description="cmd",
        skill_class="repair_skill",
        intents=("USE_SKILL",),
        execution_kinds=("APPLY",),
        domains=("python",),
        allowed_tools=("run_command", "read_files"),
        forbidden_tools=(),
        safety_class="command_safe",
        requires_confirmation_for=tuple(requires_confirmation_for),
        limits=dict(_LIMITS),
        evidence_required=("summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="# Skill\nbody",
        source_path="/skills/cmd-skill/SKILL.md",
    )


def test_decision_carries_requires_confirmation_for():
    d = SelectedSkillDecision.from_skill(_skill(), score=1.0, execution_kind="APPLY", reason="t")
    assert d.requires_confirmation_for == ("run_command",)
    assert d.to_dict()["requires_confirmation_for"] == ["run_command"]


def test_pack_requires_confirmation_when_skill_declares_it():
    d = SelectedSkillDecision.from_skill(_skill(), score=1.0, execution_kind="APPLY", reason="t")
    pack = build_skill_execution_pack(
        episode_id="ep_1", objective="run the build", decision=d, source_action_goal={}
    )
    assert pack.safety["requires_confirmation"] is True


def test_command_safe_without_declaration_keeps_class_behavior():
    # control: a command_safe skill that declares NOTHING keeps class behavior (no forced confirm)
    d = SelectedSkillDecision.from_skill(
        _skill(requires_confirmation_for=()), score=1.0, execution_kind="APPLY", reason="t"
    )
    pack = build_skill_execution_pack(
        episode_id="ep_1", objective="x", decision=d, source_action_goal={}
    )
    assert pack.safety["requires_confirmation"] is False
