"""Read-only skills keep a distinct, terminal analysis route end to end."""

from __future__ import annotations

from meta_loop.skill.foldback import fold_skill_result
from schema.execution_skill import ExecutionSkill
from schema.qwen_skill_dispatch import build_directive, build_qwen_skill_dispatch
from schema.qwen_skill_result import normalize_qwen_result
from schema.selected_skill import SelectedSkillDecision
from schema.skill_execution_pack import build_skill_execution_pack


def _skill(*, safety_class: str = "read_only") -> ExecutionSkill:
    read_only = safety_class == "read_only"
    return ExecutionSkill(
        name="review-route",
        skill_id="5.01.qwenskill-review-route",
        kind="execution_skill",
        version=1,
        enabled=True,
        description="Review the requested code and report evidence-backed findings.",
        skill_class="review_skill" if read_only else "repair_skill",
        intents=("USE_SKILL",),
        execution_kinds=("REVIEW",) if read_only else ("REPAIR",),
        domains=("engineering",),
        allowed_tools=("read_files", "search_repo", "inspect_git_diff")
        if read_only
        else ("read_files", "patch_file"),
        forbidden_tools=("edit_file", "write_file", "run_command", "run_tests")
        if read_only
        else ("web_fetch",),
        safety_class=safety_class,
        requires_confirmation_for=(),
        limits={
            "max_steps": 20,
            "max_duration_ms": 600_000,
            "max_files_changed": 0 if read_only else 10,
        },
        evidence_required=("findings", "summary") if read_only else ("files_changed",),
        completion_criteria=("review_complete",) if read_only else ("repair_complete",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="# Review route\nUse factual tool evidence.",
        source_path="/skills/review-route/SKILL.md",
    )


def _dispatch(skill: ExecutionSkill):
    decision = SelectedSkillDecision.from_skill(
        skill,
        score=1.0,
        execution_kind=skill.execution_kinds[0],
        reason="test route",
    )
    pack = build_skill_execution_pack(
        episode_id="ep-read-route",
        objective="review the cognition pipeline",
        decision=decision,
        source_action_goal={"goal_kind": "operator_task"},
    )
    return pack, build_qwen_skill_dispatch(pack, skill)


def test_read_only_route_preserves_tools_and_completes_from_report_evidence():
    skill = _skill()
    pack, dispatch = _dispatch(skill)

    assert dispatch.execution_route == "read_only"
    assert dispatch.allowed_tools == skill.allowed_tools
    assert set(dispatch.forbidden_tools) == set(skill.forbidden_tools)

    directive = build_directive(dispatch)
    assert "READ-ONLY ANALYSIS ROUTE" in directive
    assert "evidence-backed report completes the objective" in directive
    assert "all tests passing is the goal" not in directive

    normalized = normalize_qwen_result(
        {
            "status": "success",
            "summary": "Found two contract gaps.",
            "tool_calls": [
                {
                    "tool_call_id": "read-1",
                    "tool_name": "read_files",
                    "args": {"path": "cognition_chain.py"},
                    "status": "success",
                    "stdout": "two evidence-backed findings",
                    "result": {"content": "two evidence-backed findings"},
                }
            ],
        },
        dispatch=dispatch,
    )
    assert normalized.execution_route == "read_only"

    folded = fold_skill_result(
        normalized.to_dict(),
        objective=pack.objective,
        evidence_required=pack.evidence_required,
    )
    assert folded["global_b"]["intent_satisfied"] is True
    assert folded["global_b"]["objective_complete"] is True
    assert folded["global_b"]["close_episode"] is True
    assert folded["global_b"]["continuation_pressure"] is None


def test_write_safe_route_excludes_arbitrary_command_and_restore_tools():
    _pack, dispatch = _dispatch(_skill(safety_class="write_safe"))

    assert dispatch.execution_route == "development"
    assert {"read_files", "edit_file", "write_file"} <= set(dispatch.allowed_tools)
    assert "run_tests" in dispatch.allowed_tools
    assert not {"run_command", "restore_project_from_memory"} & set(dispatch.allowed_tools)
