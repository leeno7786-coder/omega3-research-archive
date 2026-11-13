"""Slice 4 B3 — Meta B pack + write surface."""

from __future__ import annotations

from copy import deepcopy

import pytest

from meta_loop.meta_b import run_meta_b_front


def _episode(auto_payload, *, eid="ep_pack"):
    class Ep:
        episode_id = eid
        pressure = {"opened_by": "op", "contributors": []}
        global_ = {
            "action_goal": {
                "episode_id": eid,
                "intent": "USE_TOOL",
                "target": "x",
                "confidence": 0.8,
                "reason": "r",
            }
        }
        meta = {
            "execution_action_pack": {
                "episode_id": eid,
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "execution_intent": "learn",
                "source_action_goal": {
                    "episode_id": eid,
                    "intent": "USE_TOOL",
                    "target": "x",
                    "confidence": 0.8,
                    "reason": "r",
                },
            }
        }
        auto = None

    ep = Ep()
    # AUTO carries independent source and plan echoes, including on a failed
    # action. Meta B must not reconstruct these from the expected plan.
    ep.auto = {
        "episode_id": eid,
        "action": ep.meta["execution_action_pack"]["action"],
        "source_action_goal": deepcopy(ep.global_["action_goal"]),
        "execution_pack": deepcopy(ep.meta["execution_action_pack"]),
        **deepcopy(auto_payload),
    }
    return ep


def test_success_reflection_and_trace_summary():
    ep = _episode(
        {
            "episode_id": "ep_pack",
            "status": "success",
            "cycle_kind": "success",
            "telemetry": {"cycle_kind": "success", "runtime_ms": 42, "warnings": ["w1"]},
            "outputs": {
                "response_text": "hi",
                "tool_result": {"a": 1},
                "image_artifact": {"b": 2},
            },
            "memory": {"memory_status": "success"},
        }
    )
    auto_before = dict(ep.auto)
    global_before = dict(ep.global_)
    pack = run_meta_b_front(ep)
    assert pack is not None
    assert pack.execution["disposition"] == "completed"
    assert pack.execution["status"] == "success"
    assert pack.trace_summary["has_response_text"] is True
    assert pack.trace_summary["has_tool_result"] is True
    assert pack.trace_summary["has_image_artifact"] is True
    assert pack.trace_summary["runtime_ms"] == 42
    assert pack.execution["warnings"] == ["w1"]
    assert ep.meta["reflection"]["execution"]["disposition"] == "completed"
    assert ep.auto == auto_before
    assert ep.global_ == global_before


@pytest.mark.parametrize(
    "cycle_kind,disposition,status",
    [
        ("success", "completed", "success"),
        ("partial", "degraded", "partial"),
        ("fail", "failed", "failed"),
        ("skipped", "skipped", "skipped"),
        ("idle", "idle", "idle"),
    ],
)
def test_disposition_per_cycle_kind(cycle_kind, disposition, status):
    ep = _episode(
        {
            "cycle_kind": cycle_kind,
            "telemetry": {"warnings": []},
            "outputs": {},
        }
    )
    pack = run_meta_b_front(ep)
    assert pack.execution["disposition"] == disposition
    assert pack.execution["status"] == status


def test_critical_mismatch_forces_invalid():
    ep = _episode(
        {
            "cycle_kind": "success",
            "telemetry": {"warnings": []},
            "outputs": {},
        }
    )
    ep.meta["execution_action_pack"]["tool"] = "wrong_tool"
    ep.auto["execution_pack"] = dict(ep.meta["execution_action_pack"])
    ep.auto["execution_pack"]["tool"] = "web_fetch"
    pack = run_meta_b_front(ep)
    assert pack.execution["status"] == "invalid"
    assert pack.execution["disposition"] == "invalid"
