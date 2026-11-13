"""Slice 4 B7 — acceptance tests + invariants."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

from global_loop.global_b import run_global_b_front
from meta_loop.meta_b import run_meta_b_front


def _ep(
    auto_payload, *, pack_tool="web_fetch", action="USE_TOOL", intent="USE_TOOL", eid="ep_acc",
):
    class Ep:
        episode_id = eid
        pressure = {"opened_by": "op", "contributors": []}
        global_ = {
            "intent_decision": {
                "episode_id": eid,
                "intent": intent,
                "confidence": 0.9,
                "reason": "acc",
            }
        }
        meta = {
            "execution_action_pack": {
                "episode_id": eid,
                "action": action,
                "tool": pack_tool,
                "execution_intent": "learn",
                "source_action_goal": {
                    "episode_id": eid,
                    "intent": intent,
                    "confidence": 0.9,
                    "reason": "acc",
                },
            }
        }
        auto = None

    ep = Ep()
    ep.auto = {
        "episode_id": eid,
        "action": action,
        "source_action_goal": deepcopy(ep.global_["intent_decision"]),
        "execution_pack": deepcopy(ep.meta["execution_action_pack"]),
        **deepcopy(auto_payload),
    }
    return ep


def test_t1_tool_success():
    ep = _ep(
        {
            "cycle_kind": "success",
            "memory_status": "skipped",
            "outputs": {"tool_result": {"results": ["doc"]}},
            "telemetry": {"warnings": []},
        }
    )
    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    ref = ep.meta["reflection"]
    out = ep.global_["outcome"]
    assert ref["execution"]["disposition"] == "completed"
    assert ref["execution"]["status"] == "success"
    assert out["outcome"]["status"] == "completed"
    assert out["outcome"]["grounded"] is True
    assert "auto.outputs.tool_result" in out["outcome"]["grounding_basis"]
    assert "meta.execution.disposition" in out["outcome"]["grounding_basis"]
    assert out["artifacts"]["tool_result"] == {"results": ["doc"]}


def test_t2_response_memory_success():
    ep = _ep(
        {
            "cycle_kind": "success",
            "memory_status": "success",
            "outputs": {"response_text": "You said Python"},
            "telemetry": {"warnings": []},
        },
        pack_tool=None,
        action="RESPONSE",
        intent="RESPOND",
    )

    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    ref = ep.meta["reflection"]
    out = ep.global_["outcome"]
    assert ref["execution"]["disposition"] == "completed"
    assert out["outcome"]["status"] == "completed"
    assert out["observations"]["memory_used"] is True
    assert out["artifacts"]["response_text"] == "You said Python"


def test_t3_failure():
    ep = _ep(
        {
            "cycle_kind": "fail",
            "failure_kind": "tool_error",
            "failure_reason": "timeout",
            "outputs": {},
            "telemetry": {"warnings": []},
        }
    )
    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    ref = ep.meta["reflection"]
    out = ep.global_["outcome"]
    assert ref["execution"]["disposition"] == "failed"
    assert out["outcome"]["status"] == "failed"
    assert out["outcome"]["summary"] == "Execution failed: tool_error."
    assert out["followups"][0]["type"] in ("needs_retry", "needs_clarification")
    assert out["outcome"]["grounded"] is True
    assert "meta.execution.disposition" in out["outcome"]["grounding_basis"]


def test_t4_invalid_tool_mismatch():
    ep = _ep(
        {
            "cycle_kind": "success",
            "outputs": {},
            "telemetry": {"warnings": []},
        },
        pack_tool="web_fetch",
    )
    ep.meta["execution_action_pack"]["tool"] = "ocr_tool"
    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    ref = ep.meta["reflection"]
    out = ep.global_["outcome"]
    assert ref["execution"]["status"] == "invalid"
    assert ref["execution"]["disposition"] == "invalid"
    assert out["outcome"]["status"] == "invalid"
    assert "invalid" in out["outcome"]["summary"].lower()
    assert out["followups"][0]["type"] == "needs_clarification"
    assert out["followups"][0]["suggested_intent"] in ("IDLE", "RESPOND")


def test_invariant_no_models_in_meta_b_global_b():
    root = Path(__file__).resolve().parents[2]
    banned = (
        "meta_b_loop",
        "global_b_loop",
        "run_meta_b_loop",
        "run_global_b_loop",
        "build_global_action_pack",
        "run_audited_loop",
        "metacognitive_review",
        "get_runtime_client",
        "generate_completion",
    )
    for rel in ("meta_loop/meta_b.py", "global_loop/global_b.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for name in banned:
            assert name not in text, f"{rel} must not reference {name}"


def test_invariant_disposition_only_from_cycle_kind():
    ep = _ep({"cycle_kind": "partial", "outputs": {}, "telemetry": {}})
    pack = run_meta_b_front(ep)
    assert pack.execution["disposition"] == "degraded"


def test_invariant_goal_state_only_from_disposition():
    ep = _ep({"cycle_kind": "skipped", "outputs": {}, "telemetry": {}})
    ref = run_meta_b_front(ep)
    out = run_global_b_front(ep, ep.meta["reflection"])
    assert ref.execution["disposition"] == "skipped"
    assert out.outcome["status"] == "skipped"


def test_invariant_intent_decision_not_mutated():
    ep = _ep({"cycle_kind": "success", "outputs": {"tool_result": {}}, "telemetry": {}})
    intent_before = dict(ep.global_["intent_decision"])
    pack_before = dict(ep.meta["execution_action_pack"])
    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    assert ep.global_["intent_decision"] == intent_before
    assert ep.meta["execution_action_pack"] == pack_before


def test_invariant_grounding_basis_actual_auto_fields():
    ep = _ep(
        {
            "cycle_kind": "success",
            "outputs": {"tool_result_summary": "ok"},
            "telemetry": {},
        }
    )
    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    basis = ep.global_["outcome"]["outcome"]["grounding_basis"]
    assert "auto.outputs.tool_result_summary" in basis
    assert "auto.outputs.tool_result" not in basis


def test_invariant_summaries_are_templates():
    ep = _ep({"cycle_kind": "success", "outputs": {"tool_result": {}}, "telemetry": {}})
    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    assert ep.global_["outcome"]["outcome"]["summary"] == "Tool execution completed successfully."
