"""The dispatch directive must FORCE qwen to do the work, not narrate it.

Root cause of the fabricated-build finding (operator 2026-06-27): a "build a todo app" dispatch
printed a report — "4 tests passed", a file tree — and wrote ZERO files. build_directive handed qwen
the expected output schema + evidence list but never told it to ACTUALLY perform the work or ground
the evidence to real tool calls, so qwen produced the JSON and invented its values. These statements
close that gap at the source (the evidence-gate catches what still slips through).
"""

from __future__ import annotations

from schema.qwen_skill_dispatch import build_directive, QwenSkillExecutionDispatch


def _dispatch(objective="build a python cli todo app"):
    return QwenSkillExecutionDispatch(
        episode_id="e",
        dispatch_id="d",
        skill_name="python",
        skill_id="i",
        skill_class="artifact_skill",
        execution_kind="WRITE",
        objective=objective,
        skill_markdown="# body",
        allowed_tools=("write_file", "run_tests"),
        forbidden_tools=(),
        safety={},
        limits={"max_steps": 40},
        evidence_required=("summary", "test_results"),
        completion_criteria=("tests_pass",),
        expected_output_schema={"type": "object"},
        source_action_goal={},
        source_skill_execution_pack={},
    )


def test_directive_demands_real_work_not_narration():
    text = build_directive(_dispatch()).lower()
    assert "actually perform the work" in text  # DO the work...
    assert "do not narrate" in text  # ...not narrate it
    assert "write the files" in text


def test_directive_grounds_evidence_to_real_tool_results():
    text = build_directive(_dispatch()).lower()
    assert "fabrication" in text  # a report without tool calls is rejected
    assert "never invent" in text  # no invented file names / test counts
    assert "real tool" in text  # evidence must reflect real tool results
