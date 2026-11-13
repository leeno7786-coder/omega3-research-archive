"""Slice 4 B5 — Global B episode outcome (intent x trajectory x execution).

Doctrine: Global B reflects the episode = intent chosen vs trajectory vs Meta B's
execution data -> one EpisodeOutcome for Omega. NO goal, NO goal_state.
"""

from __future__ import annotations

import dataclasses

from global_loop.global_b import run_global_b_front
from global_loop.package.run import GlobalOutcomePack


def _episode(auto_payload, *, intent="RESPOND", action="RESPONSE", domain="", trajectory=None):
    class Ep:
        episode_id = "ep_gb"
        pressure = {
            "opened_by": "op",
            "contributors": [],
            "trajectory_snapshot": trajectory or {},
        }
        global_ = {
            "intent_decision": {
                "episode_id": "ep_gb",
                "intent": intent,
                "confidence": 0.7,
                "reason": "answer",
            }
        }
        meta = {
            "execution_action_pack": {
                "episode_id": "ep_gb",
                "action": action,
                "tool": None,
                "execution_intent": "respond",
                "execution_inputs": {"domain": domain},
            }
        }
        auto = None

    ep = Ep()
    ep.auto = auto_payload
    return ep


def test_episode_outcome_shape_no_goal_state_no_target():
    reflection = {
        "episode_id": "ep_gb",
        "consistency": {"pack_matches": True, "mismatch_reasons": []},
        "execution": {
            "executed": True,
            "result": "success",
            "action": "RESPONSE",
            "tool": None,
            "status": "success",
            "disposition": "completed",
            "failure_kind": None,
            "failure_reason": None,
            "memory_status": "success",
            "warnings": [],
        },
        "trace_summary": {},
    }
    ep = _episode(
        {
            "cycle_kind": "success",
            "memory_status": "success",
            "outputs": {"response_text": "Hello"},
            "telemetry": {"warnings": []},
        }
    )
    pack = run_global_b_front(ep, reflection)
    assert pack is not None
    # no goal anywhere — the abstraction is gone
    assert "goal" not in {f.name for f in dataclasses.fields(GlobalOutcomePack)}
    assert "goal_state" not in pack.outcome
    # the episode outcome: intent x trajectory x execution
    assert pack.outcome["intent"] == "RESPOND"
    assert pack.outcome["execution_outcome"]["executed"] is True
    assert pack.outcome["execution_outcome"]["status"] == "success"
    assert pack.outcome["execution_outcome"]["failure_reason"] is None
    # compat key for legacy window/deepsleep readers (== old goal_state vocab)
    assert pack.outcome["status"] == "completed"
    assert pack.artifacts["response_text"] == "Hello"
    # written to episode under global_['outcome']['outcome']
    assert "goal" not in ep.global_["outcome"]
    assert ep.global_["outcome"]["outcome"]["intent"] == "RESPOND"


def test_episode_outcome_failure_carries_reason():
    reflection = {
        "consistency": {},
        "execution": {
            "executed": True,
            "result": "failure",
            "action": "USE_TOOL",
            "status": "failed",
            "disposition": "failed",
            "failure_kind": "tool_error",
            "failure_reason": "boom",
            "memory_status": "skipped",
            "warnings": [],
        },
        "trace_summary": {},
    }
    ep = _episode(
        {
            "cycle_kind": "fail",
            "failure_kind": "tool_error",
            "failure_reason": "boom",
            "outputs": {},
            "telemetry": {},
        },
        intent="USE_TOOL",
        action="USE_TOOL",
    )
    pack = run_global_b_front(ep, reflection)
    eo = pack.outcome["execution_outcome"]
    assert eo["status"] == "failure"
    assert eo["failure_kind"] == "tool_error"
    assert eo["failure_reason"] == "boom"  # the reason-it-failed reaches Omega
    assert pack.outcome["status"] == "failed"


def test_idle_episode_outcome_not_executed_status_none():
    reflection = {
        "consistency": {},
        "execution": {
            "executed": False,
            "result": "none",
            "action": "IDLE",
            "status": "idle",
            "disposition": "idle",
            "memory_status": "skipped",
            "warnings": [],
        },
        "trace_summary": {},
    }
    ep = _episode(
        {"cycle_kind": "idle", "outputs": {}, "telemetry": {}}, intent="IDLE", action="IDLE"
    )
    pack = run_global_b_front(ep, reflection)
    eo = pack.outcome["execution_outcome"]
    assert eo["executed"] is False
    assert eo["status"] == "none"  # not a failure
    assert pack.outcome["status"] == "idle"


def test_partial_episode_outcome_preserves_progress_status():
    reflection = {
        "consistency": {},
        "execution": {
            "executed": True,
            "result": "partial",
            "action": "USE_SKILL",
            "status": "partial",
            "disposition": "degraded",
            "failure_kind": "skill_yielded",
            "failure_reason": "checkpoint reached",
            "memory_status": "skipped",
            "warnings": [],
        },
        "trace_summary": {},
    }
    ep = _episode(
        {
            "cycle_kind": "partial",
            "status": "partial",
            "failure_kind": "skill_yielded",
            "failure_reason": "checkpoint reached",
            "outputs": {},
            "telemetry": {},
        },
        intent="USE_SKILL",
        action="USE_SKILL",
    )
    pack = run_global_b_front(ep, reflection)
    eo = pack.outcome["execution_outcome"]
    assert eo["status"] == "partial"
    assert eo["failure_kind"] == "skill_yielded"
    assert eo["failure_reason"] is None


def test_global_b_chemical_state_is_never_raise_and_clamped():
    reflection = {
        "consistency": {},
        "execution": {
            "executed": True,
            "result": "success",
            "action": "RESPONSE",
            "status": "success",
            "disposition": "completed",
            "memory_status": "success",
            "warnings": [],
        },
        "trace_summary": {},
        "neuromod_adjustments": {"dopamine": "bad", "plasticity": 99},
    }
    ep = _episode(
        {
            "cycle_kind": "success",
            "memory_status": "success",
            "neuromod": {
                "dopamine": "bad",
                "stress": 2,
                "serotonin": "nan",
                "plasticity": -1,
            },
            "outputs": {"response_text": "Hello"},
            "telemetry": {"warnings": []},
        }
    )
    pack = run_global_b_front(ep, reflection)
    assert pack.chemical_state == {
        "dopamine": 0.0,
        "stress": 1.0,
        "serotonin": 0.0,
        "noradrenaline": 0.2,
        "plasticity": 1.0,
    }
