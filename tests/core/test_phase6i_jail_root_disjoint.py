"""High #4 / #26 — the skill-execution guard must VALIDATE the workspace jail root is
disjoint from the Omega tree, and report ``workspace_jail`` HONESTLY.

The guard previously checked only that a write-capable class had *some* truthy workspace_root
(presence), never that the root lies OUTSIDE ``OMEGA_ROOT``. An operator who points
``OMEGA_OPERATOR_WORKSPACE`` at Omega's own tree (or a parent of it) would pass the guard and
jail qwen *into* Omega source — the exact thing invariant 2 forbids. And ``workspace_jail`` was
a hardcoded ``True`` (a fabricated signal) regardless of whether a jail was actually in force.
"""

from __future__ import annotations

from pathlib import Path

from auto_loop.execute.skill_execution_guards import skill_execution_guard
from core.paths import OMEGA_ROOT
from schema.qwen_skill_dispatch import QwenSkillExecutionDispatch
from schema.skill_execution_pack import safety_envelope

_LIMITS = {"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": 10}


def _dispatch(
    safety_class="write_safe", *, allowed=("read_files", "patch_file"), forbidden=("run_command",)
):
    return QwenSkillExecutionDispatch(
        episode_id="ep_1",
        dispatch_id="dispatch_x",
        skill_name="s",
        skill_id="5.01.qwenskill-s",
        skill_class="repair_skill",
        execution_kind="REPAIR",
        objective="obj",
        skill_markdown="body",
        allowed_tools=tuple(allowed),
        forbidden_tools=tuple(forbidden),
        safety=safety_envelope(safety_class),
        limits=dict(_LIMITS),
        evidence_required=(),
        completion_criteria=(),
        expected_output_schema={},
        source_action_goal={},
        source_skill_execution_pack={},
    )


def test_write_skill_jailed_into_omega_tree_is_blocked():
    inside = str(Path(OMEGA_ROOT) / "operator_ws")
    g = skill_execution_guard(_dispatch("write_safe"), confirmed=True, workspace_root=inside)
    assert not g.allowed
    assert "outside" in g.reason.lower() or "omega" in g.reason.lower()


def test_write_skill_jailed_at_omega_root_itself_is_blocked():
    g = skill_execution_guard(
        _dispatch("write_safe"), confirmed=True, workspace_root=str(OMEGA_ROOT)
    )
    assert not g.allowed


def test_write_skill_jailed_above_omega_root_is_blocked():
    # a parent of OMEGA_ROOT CONTAINS the Omega tree — not a valid jail
    parent = str(Path(OMEGA_ROOT).resolve().parent)
    g = skill_execution_guard(_dispatch("write_safe"), confirmed=True, workspace_root=parent)
    assert not g.allowed


def test_write_skill_with_disjoint_root_is_allowed_and_jailed(tmp_path):
    g = skill_execution_guard(_dispatch("write_safe"), confirmed=True, workspace_root=str(tmp_path))
    assert g.allowed
    assert g.workspace_jail is True


def test_read_only_without_root_is_not_falsely_reported_as_jailed():
    g = skill_execution_guard(
        _dispatch("read_only", allowed=("read_files",), forbidden=("write_file", "patch_file"))
    )
    assert g.allowed
    # honest signal: no validated jail root in force, so workspace_jail must NOT claim True
    assert g.workspace_jail is False
