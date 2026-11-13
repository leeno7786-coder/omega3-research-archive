"""Phase 6G — Meta A emits a SkillExecutionPack consumable by Auto."""

from __future__ import annotations

import pytest

from meta_loop.skill.run import select_skill_for_intent
from runtime.execution_skills import selectable_execution_skills
from runtime.qwen_skills import BUNDLED_SKILL_DIRS
from schema.selected_skill import SelectedSkillDecision
from schema.skill_execution_pack import (
    SkillExecutionPack,
    SkillPackError,
    build_skill_execution_pack,
    safety_envelope,
)

_BUNDLED = BUNDLED_SKILL_DIRS[0]


@pytest.fixture(scope="module")
def catalog():
    return selectable_execution_skills(roots=[_BUNDLED], config={})


def _pack(catalog, objective="fix this bug", goal=None, global_forbidden=()):
    decision = select_skill_for_intent(objective=objective, catalog=catalog)
    return build_skill_execution_pack(
        episode_id="ep_1",
        objective=objective,
        decision=decision,
        source_action_goal=goal
        if goal is not None
        else {"goal_kind": "operator_task", "desired_outcome": objective},
        global_forbidden_tools=global_forbidden,
    ), decision


# ── valid pack ────────────────────────────────────────────────────────────────
def test_valid_pack_emitted_for_use_skill(catalog):
    pack, decision = _pack(catalog)
    assert pack.validate() == []
    assert pack.action == "USE_SKILL" and pack.type == "skill_execution_pack"
    assert pack.dispatch_id.startswith("dispatch_") and pack.episode_id == "ep_1"
    ss = pack.selected_skill
    assert ss["skill_name"] == decision.skill_name and ss["skill_id"] == decision.skill_id
    assert ss["source_path"] and ss["execution_kind"]


def test_pack_dispatch_id_is_deterministic(catalog):
    a, _ = _pack(catalog)
    b, _ = _pack(catalog)
    assert a.dispatch_id == b.dispatch_id


# ── failed_to_initiate ────────────────────────────────────────────────────────
def test_failed_selection_raises_failed_to_initiate():
    with pytest.raises(SkillPackError, match="failed_to_initiate"):
        build_skill_execution_pack(
            episode_id="ep_1",
            objective="x",
            decision=SelectedSkillDecision.failed("no_matching_skill"),
            source_action_goal={},
        )


def test_missing_episode_or_objective_raises(catalog):
    decision = select_skill_for_intent(objective="fix this bug", catalog=catalog)
    with pytest.raises(SkillPackError):
        build_skill_execution_pack(
            episode_id="", objective="fix", decision=decision, source_action_goal={}
        )
    with pytest.raises(SkillPackError):
        build_skill_execution_pack(
            episode_id="ep_1", objective="", decision=decision, source_action_goal={}
        )


# ── source_action_goal exact echo ─────────────────────────────────────────────
def test_pack_echoes_source_action_goal_exactly(catalog):
    goal = {
        "goal_kind": "operator_task",
        "desired_outcome": "fix the parser bug",
        "constraints": ["bounded"],
    }
    pack, _ = _pack(catalog, goal=goal)
    assert pack.source_action_goal == goal


# ── no prompt / qwen-model fields ─────────────────────────────────────────────
def test_pack_has_no_prompt_or_model_fields(catalog):
    pack, _ = _pack(catalog)
    keys = set(pack.to_dict())
    assert not (
        keys & {"prompt", "skill_markdown", "model", "model_settings", "temperature", "messages"}
    )


# ── allowed only from skill; forbidden = skill + global ───────────────────────
def test_allowed_tools_only_from_selected_skill(catalog):
    pack, decision = _pack(catalog)
    assert set(pack.allowed_tools) == set(decision.allowed_tools)


def test_forbidden_tools_includes_skill_and_global(catalog):
    pack, decision = _pack(catalog, global_forbidden=("desktop_control_tool",))
    assert set(decision.forbidden_tools).issubset(set(pack.forbidden_tools))
    assert "desktop_control_tool" in pack.forbidden_tools


# ── safety derived from class ─────────────────────────────────────────────────
def test_safety_envelope_read_only():
    env = safety_envelope("read_only")
    assert env["write_allowed"] is False and env["command_allowed"] is False
    assert env["destructive_allowed"] is False and env["requires_confirmation"] is False


def test_safety_envelope_destructive_requires_confirmation():
    env = safety_envelope("destructive")
    assert env["write_allowed"] and env["command_allowed"] and env["destructive_allowed"]
    assert env["requires_confirmation"] is True


def test_pack_safety_derived_from_selected_skill(catalog):
    # codex-grade-coding is write_safe -> write allowed, no command/destructive
    pack, _ = _pack(catalog, objective="fix this bug")
    assert pack.safety["safety_class"] in ("write_safe", "command_safe", "destructive")
    assert pack.safety["write_allowed"] is True
    assert isinstance(pack, SkillExecutionPack)
