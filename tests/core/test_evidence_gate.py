"""The retired EVIDENCE-GATE, and what replaced it.

WAS: a fixed ledger rule at the execute() seam. A mutating-kind skill that wrote
nothing, or a TEST-kind skill where no test ran, was failed CLOSED as
skill_no_evidence. It existed for a measured reason (operator 2026-06-27): qwen
NARRATED a finished "todo app" — reporting "4 tests passed" and 5 files — having
written ZERO of them.

NOW (operator 2026-07-31): the rule was judging a BUILD by a SKILL property, and
the agent was never told the bar it would be measured against, so an agent could
finish in good faith and be recorded as a failure — live episode b06b85ac, 138
seconds spent and nothing banked. Two changes:

  * The bar is part of the MIND'S OUTPUT — stated per objective from what Omega
    has learned, rendered into the agent's brief before it starts
    (auto_loop.execute.success_criteria.render_brief). It sets a bar and tries
    to reach it, rather than being graded on a constant it never saw.
  * A skill's execution_kind is a PERMISSION axis (may this skill write?), not a
    verdict. Skills "should become modifiers/enhancers not law for a build, they
    should never affect a build's outcome."

Falling short is now a REPORT the mind decides on — retry, change approach, or
stop — instead of a hard failure at this seam or an unbounded retry loop.

⚠️ The fabricated-done protection now rests on the mind's authored criteria
being specific enough to expose an empty run. That is a deliberate trade; see
auto_loop/execute/success_criteria.py.

Drives the REAL execute() USE_SKILL branch (not a helper), so what is asserted
here is proven at the seam.
"""

from __future__ import annotations

import auto_loop.skill.run as skill_run
from auto_loop.auto import MemoryEnvelope, PromptPack, execute


def _mem() -> MemoryEnvelope:
    return MemoryEnvelope(
        memory_required=False,
        memory_status="skipped",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )


def _pp() -> PromptPack:
    return PromptPack(
        prompt_type="RESPONSE_REASONING",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="build the app",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="reasoning",
        tool_call_spec=None,
        image_spec=None,
    )


def _use_skill_pack(*, execution_kind: str, max_files_changed: int = 60):
    return {
        "action": "USE_SKILL",
        "tool": None,
        "source_action_goal": {},
        "execution_inputs": {
            "skill_execution_pack": {
                "dispatch_id": "d1",
                "episode_id": "ep_ev",
                "objective": "build the app",
                "selected_skill": {"name": "python", "execution_kind": execution_kind},
                "limits": {"max_files_changed": max_files_changed},
            }
        },
    }


def _fake_dispatch(*, files_changed=(), tests_run=()):
    def _dispatch(*_args, **_kwargs):
        return {
            "status": "executed",
            "dispatch_id": "d1",
            "reason": None,
            "guard_status": {},
            "qwen_result": {
                "status": "success",
                "files_changed": list(files_changed),
                "tests_run": list(tests_run),
                "summary": "I built the app and 4 tests pass",
            },
        }

    return _dispatch


def test_write_kind_zero_files_is_no_longer_failed_by_a_hardcoded_rule(monkeypatch):
    """RETIRED GATE (operator 2026-07-31).

    A WRITE run that wrote nothing used to be failed here by a fixed rule the
    agent was never told, so an agent could finish in good faith and be recorded
    as skill_no_evidence (live episode b06b85ac: 138s spent, nothing banked).
    The bar now comes from the MIND'S OUTPUT and is handed to the agent up
    front; falling short is a REPORT the mind decides on, not a hardcoded
    verdict at this seam.
    """
    monkeypatch.setattr(skill_run, "dispatch_skill_pack", _fake_dispatch(files_changed=()))
    res = execute(
        _pp(),
        None,
        _use_skill_pack(execution_kind="WRITE"),
        _mem(),
        "ep_ev",
        runtime_client=object(),
    )
    assert res["failure_kind"] != "skill_no_evidence"


def test_test_kind_no_tests_but_wrote_files_is_credited(monkeypatch):
    # A TEST skill whose tests_run didn't propagate but that WROTE real files still did
    # substantiated work — files on the ledger are evidence, not the fabricated-done the gate
    # guards against. Operator call 2026-07-01: credit real artifacts so a genuine build routed
    # to a TEST skill (with tests_run lost in transit) is not discarded as skill_no_evidence.
    monkeypatch.setattr(
        skill_run, "dispatch_skill_pack", _fake_dispatch(files_changed=["t.py"], tests_run=())
    )
    res = execute(
        _pp(),
        None,
        _use_skill_pack(execution_kind="TEST"),
        _mem(),
        "ep_ev2",
        runtime_client=object(),
    )
    assert res["outputs"]["skill_status"] != "failure"
    assert res["failure_kind"] != "skill_no_evidence"


def test_test_kind_empty_run_is_no_longer_failed_by_a_hardcoded_rule(monkeypatch):
    """Same retirement, TEST kind. The skill's execution_kind is a PERMISSION
    axis (may this skill write?), not a verdict on whether the build succeeded —
    operator 2026-07-31: skills "should become modifiers/enhancers not law for a
    build, they should never affect a build's outcome"."""
    monkeypatch.setattr(
        skill_run, "dispatch_skill_pack", _fake_dispatch(files_changed=(), tests_run=())
    )
    res = execute(
        _pp(),
        None,
        _use_skill_pack(execution_kind="TEST"),
        _mem(),
        "ep_ev2b",
        runtime_client=object(),
    )
    assert res["failure_kind"] != "skill_no_evidence"


def test_write_kind_with_real_files_is_credited(monkeypatch):
    # the gate must NOT false-positive: a build that actually wrote files is a real success
    monkeypatch.setattr(
        skill_run,
        "dispatch_skill_pack",
        _fake_dispatch(files_changed=["todo_app/main.py", "tests/test_todo.py"]),
    )
    res = execute(
        _pp(),
        None,
        _use_skill_pack(execution_kind="WRITE"),
        _mem(),
        "ep_ev3",
        runtime_client=object(),
    )
    assert res["outputs"]["skill_status"] == "success"
    assert res["model_id"] == "brain_runtime"
    assert res["failure_kind"] is None


def test_inspection_kind_zero_files_still_credited(monkeypatch):
    # a read-only/INSPECT skill legitimately writes nothing — its evidence is findings, not files,
    # so the gate must leave it alone
    monkeypatch.setattr(skill_run, "dispatch_skill_pack", _fake_dispatch(files_changed=()))
    res = execute(
        _pp(),
        None,
        _use_skill_pack(execution_kind="INSPECT", max_files_changed=0),
        _mem(),
        "ep_ev4",
        runtime_client=object(),
    )
    assert res["outputs"]["skill_status"] == "success"
    assert res["model_id"] == "brain_runtime"
    assert res["failure_kind"] is None
