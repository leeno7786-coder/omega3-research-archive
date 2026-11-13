"""Fold-back build loop - end-to-end contract flow across the seams.

The current brain executor emits a structured yielded result directly. This
test validates that it survives normalization and foldback as a non-failure,
more-work-owed checkpoint carrying the brain-derived digest.
"""

from __future__ import annotations

from meta_loop.skill.foldback import fold_skill_result
from schema.qwen_skill_dispatch import QwenSkillExecutionDispatch
from schema.qwen_skill_result import normalize_qwen_result


def _dispatch(forbidden=("run_command",)):
    return QwenSkillExecutionDispatch(
        episode_id="ep1",
        dispatch_id="d1",
        skill_name="builder",
        skill_id="i",
        skill_class="execution_skill",
        execution_kind="WRITE",
        objective="build the framework",
        skill_markdown="# b",
        allowed_tools=("write_file",),
        forbidden_tools=tuple(forbidden),
        safety={},
        limits={},
        evidence_required=(),
        completion_criteria=(),
        expected_output_schema={},
        source_action_goal={},
        source_skill_execution_pack={},
    )


def test_bridge_yield_flows_to_global_b_build_in_progress():
    # 1) the brain executor emits a structured yield with observed tool evidence.
    raw = {
        "ok": True,
        "status": "yielded",
        "summary": "wrote core types and the memory store",
        "tool_calls": [
            {"tool_name": "write_file", "args": {"path": "src/core/types.ts"}},
            {"tool_name": "write_file", "args": {"path": "src/memory/index.ts"}},
        ],
        "ledger": {
            "touched_files": [
                {"path": "src/core/types.ts"},
                {"path": "src/memory/index.ts"},
            ]
        },
        "rounds_used": 16,
        "progress_digest": "wrote core types and the memory store",
    }
    assert raw["status"] == "yielded" and raw["progress_digest"]
    assert raw["ok"] is True  # a yield is progress, never an error

    # 2) normalize -> yielded is NOT a failure; the digest, rounds, and files survive
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert r.status == "yielded" and r.failure_class is None
    assert r.rounds_used == 16
    assert r.progress_digest
    assert set(r.files_changed) == {"src/core/types.ts", "src/memory/index.ts"}

    # 3) foldback -> Meta B skill_yielded; Global B owes more work, signals build_in_progress,
    #    and the episode stays OPEN so the mind decides continue|finish (no auto-close, no failure)
    folded = fold_skill_result(r.to_dict(), objective="build the framework")
    assert folded["meta_b"]["outcome_kind"] == "skill_yielded"
    gb = folded["global_b"]
    assert gb["more_work_needed"] is True
    assert gb["close_episode"] is False
    assert gb["intent_satisfied"] is False
    assert "build_in_progress" in gb["continuation_signals"]


def test_yield_that_escaped_allowed_tools_is_a_failure_not_a_clean_checkpoint():
    # security: a forbidden-tool escape during a yielded run downgrades to failure
    raw = {
        "status": "yielded",
        "summary": "the run yielded",
        "tool_calls": [
            {"tool_name": "run_command", "args": {"command": "rm -rf /"}},
        ],
        "rounds_used": 4,
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch(forbidden=("run_command",)))
    assert r.status == "failure"
    assert r.failure_class and "forbidden_tool_use" in r.failure_class
