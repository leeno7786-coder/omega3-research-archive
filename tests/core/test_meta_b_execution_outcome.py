"""Meta B reflects over the AUTONOMOUS EXECUTION — and folds success/failure up.

Operator doctrine (soft gate): Meta B reflects over execution ONLY IF execution
ran. A non-executing episode (IDLE / skipped) is marked executed=False with
result='none' — NOT a fabricated failure. When execution ran, the success-vs-
failure verdict AND the reason-it-failed flow up to Global B.
"""

from __future__ import annotations

from meta_loop.consistency.run import MetaBInput
from meta_loop.consistency.run import run as run_consistency
from meta_loop.disposition.run import run as run_disposition
from meta_loop.reflect.run import run as run_reflect


def _reflect(auto_inner: dict, *, action: str = "USE_TOOL", tool: str = "web_fetch") -> dict:
    pack = {"episode_id": "ep1", "action": action, "tool": tool}
    goal = {"episode_id": "ep1", "intent": action}
    mbi = MetaBInput(
        episode_id="ep1",
        pressure={"reason": "x", "contributors": []},
        source_action_goal=dict(goal),
        execution_pack=dict(pack),
        # AUTO's fold echoes the Global A source; a missing echo is a critical mismatch.
        auto_outcome={
            "episode_id": "ep1",
            "source_action_goal": dict(goal),
            "execution_pack": dict(pack),
            "auto": auto_inner,
        },
    )
    consistency, critical = run_consistency(mbi)
    disposition, status = run_disposition(mbi, critical)
    return run_reflect(mbi, consistency, disposition, status).execution


def test_meta_b_idle_not_executed_no_failure():
    ex = _reflect({"cycle_kind": "idle", "status": "idle"}, action="IDLE", tool="")
    assert ex["executed"] is False
    # not executed -> NOT a failure; 'none' result, no failure reason fabricated
    assert ex["result"] == "none"
    assert ex["failure_reason"] is None


def test_meta_b_failed_tool_marks_failure_and_propagates_reason():
    ex = _reflect(
        {
            "cycle_kind": "fail",
            "status": "failed",
            "failure_kind": "tool_error",
            "failure_reason": "boom",
        }
    )
    assert ex["executed"] is True
    assert ex["result"] == "failure"
    # the reason-it-failed flows UP to Global B
    assert ex["failure_reason"] == "boom"


def test_meta_b_success_executed():
    ex = _reflect({"cycle_kind": "success", "status": "success"})
    assert ex["executed"] is True
    assert ex["result"] == "success"


def test_meta_b_partial_preserves_progress_without_marking_failure():
    ex = _reflect(
        {
            "cycle_kind": "partial",
            "status": "partial",
            "failure_kind": "skill_yielded",
            "failure_reason": "checkpoint reached",
        }
    )
    assert ex["executed"] is True
    assert ex["result"] == "partial"
    assert ex["failure_kind"] == "skill_yielded"
    assert ex["failure_reason"] == "checkpoint reached"


def test_meta_b_invalid_not_executed_no_fabricated_failure():
    ex = _reflect({"cycle_kind": "invalid", "status": "invalid"})
    assert ex["executed"] is False
    assert ex["result"] == "none"
    assert ex["failure_reason"] is None
