"""Phase 6I — general skill-execution guard (per-safety-class policy + structured status)."""

from __future__ import annotations

from auto_loop.execute.skill_execution_guards import skill_execution_guard
from auto_loop.skill.run import dispatch_skill_pack
from schema.execution_skill import ExecutionSkill
from schema.qwen_skill_dispatch import QwenSkillExecutionDispatch
from schema.selected_skill import SelectedSkillDecision
from schema.skill_execution_pack import build_skill_execution_pack, safety_envelope

_LIMITS = {"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": 10}


def _dispatch(
    safety_class="write_safe",
    *,
    allowed=("read_files", "patch_file"),
    forbidden=("run_command",),
    limits=None,
    requires_confirmation=False,
):
    safety = safety_envelope(safety_class)
    if requires_confirmation:
        safety["requires_confirmation"] = True
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
        safety=safety,
        limits=limits if limits is not None else dict(_LIMITS),
        evidence_required=(),
        completion_criteria=(),
        expected_output_schema={},
        source_action_goal={},
        source_skill_execution_pack={},
    )


# ── per-class policy ──────────────────────────────────────────────────────────
def test_read_only_no_checkpoint_no_confirmation():
    g = skill_execution_guard(
        _dispatch("read_only", allowed=("read_files",), forbidden=("write_file", "patch_file"))
    )
    assert g.allowed and g.status == "allowed"
    assert g.checkpoint_required is False and g.confirmation_required is False


def test_write_skill_requires_checkpoint_and_confirmation():
    g = skill_execution_guard(_dispatch("write_safe"))
    assert g.checkpoint_required is True
    assert g.status == "requires_confirmation" and not g.allowed  # confirmation required, not given
    g2 = skill_execution_guard(_dispatch("write_safe"), confirmed=True, workspace_root="/ws")
    assert g2.allowed and g2.checkpoint_required is True


def test_destructive_requires_confirmation_and_command_policy():
    # destructive needs a command policy
    g = skill_execution_guard(_dispatch("destructive", allowed=("run_command",), forbidden=()))
    assert not g.allowed and "command policy" in g.reason
    # with a command policy but no confirmation -> requires_confirmation
    g2 = skill_execution_guard(
        _dispatch("destructive", allowed=("run_command",), forbidden=()),
        command_policy=lambda c: True,
    )
    assert g2.status == "requires_confirmation"
    # with both -> allowed
    g3 = skill_execution_guard(
        _dispatch("destructive", allowed=("run_command",), forbidden=()),
        command_policy=lambda c: True,
        confirmed=True,
        workspace_root="/ws",
    )
    assert g3.allowed and g3.checkpoint_required is True


def test_forbidden_tool_in_allowed_is_blocked():
    g = skill_execution_guard(
        _dispatch("write_safe", allowed=("read_files", "run_command"), forbidden=("run_command",))
    )
    assert not g.allowed and "forbidden tool" in g.reason
    assert "run_command" in g.blocked_tools


def test_self_modification_blocked_by_default():
    g = skill_execution_guard(_dispatch("self_modification", forbidden=()))
    assert not g.allowed and "self_modification" in g.reason
    # authorized but STILL needs confirmation (highest-risk class)
    g2 = skill_execution_guard(
        _dispatch("self_modification", forbidden=()), allow_self_modification=True
    )
    assert g2.status == "requires_confirmation"
    g3 = skill_execution_guard(
        _dispatch("self_modification", forbidden=()), allow_self_modification=True, confirmed=True
    )
    assert g3.allowed


def test_command_safe_requires_command_policy():
    g = skill_execution_guard(_dispatch("command_safe", allowed=("run_tests",), forbidden=()))
    assert not g.allowed and "command policy" in g.reason
    g2 = skill_execution_guard(
        _dispatch("command_safe", allowed=("run_tests",), forbidden=()),
        command_policy=lambda c: True,
        workspace_root="/ws",
    )
    assert g2.allowed  # command_safe needs a command policy + a workspace root (no confirmation)


def test_missing_or_bad_limits_blocked():
    g = skill_execution_guard(
        _dispatch(
            "read_only",
            allowed=("read_files",),
            forbidden=(),
            limits={"max_steps": 0, "max_duration_ms": 0, "max_files_changed": 0},
        )
    )
    assert not g.allowed and "limits" in g.reason


# ── declared runtime policy ───────────────────────────────────────────────────
def test_timeout_and_fail_closed_declared():
    g = skill_execution_guard(_dispatch("read_only", allowed=("read_files",), forbidden=()))
    assert (
        g.timeout_ms == _LIMITS["max_duration_ms"]
    )  # the timeout the dispatcher enforces (kills qwen)
    assert g.fail_closed_on_missing_done is True  # a missing bridge 'done' is a failure
    # honest signal: read_only with no workspace_root has no validated jail root in force, so
    # workspace_jail must NOT claim True. It is True only with a validated disjoint root (see
    # test_phase6i_jail_root_disjoint.py).
    assert g.workspace_jail is False


def test_guard_status_is_structured():
    g = skill_execution_guard(_dispatch("write_safe"), confirmed=True, workspace_root="/ws")
    d = g.to_dict()
    assert set(d) >= {
        "allowed",
        "status",
        "checkpoint_required",
        "confirmation_required",
        "timeout_ms",
        "fail_closed_on_missing_done",
        "workspace_jail",
        "policy",
    }


# ── guard status appears in the Auto result ───────────────────────────────────
def _mk_skill(
    name="ro-skill",
    *,
    skill_class="inspection_skill",
    safety_class="read_only",
    execution_kinds=("INSPECT",),
    allowed=("read_files",),
    forbidden=("write_file", "patch_file", "run_command"),
):
    return ExecutionSkill(
        name=name,
        skill_id=f"5.01.qwenskill-{name}",
        kind="execution_skill",
        version=1,
        enabled=True,
        description=name,
        skill_class=skill_class,
        intents=("USE_SKILL",),
        execution_kinds=tuple(execution_kinds),
        domains=("python",),
        allowed_tools=tuple(allowed),
        forbidden_tools=tuple(forbidden),
        safety_class=safety_class,
        requires_confirmation_for=(),
        limits=dict(_LIMITS),
        evidence_required=("summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="# Skill\nbody",
        source_path=f"/skills/{name}/SKILL.md",
    )


def _pack_for(skill, objective="inspect the repo"):
    decision = SelectedSkillDecision.from_skill(
        skill, score=1.0, execution_kind=skill.execution_kinds[0], reason="t"
    )
    return build_skill_execution_pack(
        episode_id="ep_1", objective=objective, decision=decision, source_action_goal={}
    )


def test_guard_status_appears_in_auto_result_executed():
    skill = _mk_skill()  # read_only -> guard allows, no confirmation
    out = dispatch_skill_pack(
        _pack_for(skill),
        catalog=[skill],
        invoker=lambda directive, d: {"ok": True},
        guard=skill_execution_guard,
    )
    assert out["status"] == "executed"
    assert (
        out["guard_status"]["status"] == "allowed"
        and out["guard_status"]["checkpoint_required"] is False
    )


def test_guard_status_appears_in_auto_result_requires_confirmation():
    skill = _mk_skill(
        "wr-skill",
        skill_class="repair_skill",
        safety_class="write_safe",
        execution_kinds=("REPAIR",),
        allowed=("read_files", "patch_file"),
        forbidden=("run_command",),
    )
    inv_calls = []
    out = dispatch_skill_pack(
        _pack_for(skill, "fix the bug"),
        catalog=[skill],
        invoker=lambda directive, d: inv_calls.append(1) or {"ok": True},
        guard=skill_execution_guard,
    )
    assert out["status"] == "requires_confirmation"  # write_safe needs confirmation
    assert out["guard_status"]["checkpoint_required"] is True
    assert inv_calls == []  # qwen NOT called when confirmation is pending
