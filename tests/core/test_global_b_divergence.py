"""Global B computes a first-class numeric intent-vs-trajectory divergence.

Pins the EXACT computed score (not just type/range), including an intermediate
case so a degenerate always-0/always-1 implementation cannot pass. Divergence is
sourced from the RESOLVED topic domain (execution_inputs['domain']), not the raw
intent word — else every autonomous LEARN scores divergent against a trajectory.
"""

from __future__ import annotations

import pytest

from global_loop.global_b import run_global_b_front

_REFLECTION = {
    "consistency": {},
    "execution": {
        "executed": True,
        "result": "success",
        "action": "USE_TOOL",
        "status": "success",
        "disposition": "completed",
        "warnings": [],
    },
    "trace_summary": {},
}


def _outcome(domain, trajectory):
    class Ep:
        episode_id = "ep_div"
        pressure = {"opened_by": "op", "contributors": [], "trajectory_snapshot": trajectory}
        global_ = {
            "intent_decision": {
                "episode_id": "ep_div",
                "intent": "LEARN",
                "confidence": 0.7,
                "reason": "r",
            }
        }
        meta = {
            "execution_action_pack": {
                "episode_id": "ep_div",
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "execution_intent": "learn",
                "execution_inputs": {"domain": domain},
            }
        }
        auto = {"cycle_kind": "success", "status": "success", "outputs": {}, "telemetry": {}}

    return run_global_b_front(Ep(), dict(_REFLECTION)).outcome


def test_on_trajectory_zero_divergence():
    o = _outcome("coding", {"target_domain": "coding", "target_capability": ""})
    assert o["trajectory_alignment"] == "on-trajectory"
    assert o["divergence_score"] == 0.0


def test_divergent_full_divergence():
    o = _outcome("photography", {"target_domain": "coding", "target_capability": ""})
    assert o["trajectory_alignment"] == "divergent"
    assert o["divergence_score"] == 1.0


def test_no_trajectory_is_zero():
    o = _outcome("coding", {})
    assert o["trajectory_alignment"] == "no-trajectory"
    assert o["divergence_score"] == 0.0


def test_respond_empty_domain_not_divergent():
    """Non-LEARN episodes with empty domain are not trajectory drift."""

    class Ep:
        episode_id = "ep_resp"
        pressure = {
            "opened_by": "op",
            "contributors": [],
            "trajectory_snapshot": {"target_domain": "coding", "target_capability": ""},
        }
        global_ = {
            "intent_decision": {
                "episode_id": "ep_resp",
                "intent": "RESPOND",
                "confidence": 0.7,
                "reason": "r",
            }
        }
        meta = {
            "execution_action_pack": {
                "episode_id": "ep_resp",
                "action": "RESPONSE",
                "tool": None,
                "execution_intent": "respond",
                "execution_inputs": {"domain": ""},
            }
        }
        auto = {"cycle_kind": "success", "status": "success", "outputs": {}, "telemetry": {}}

    o = run_global_b_front(Ep(), dict(_REFLECTION)).outcome
    assert o["trajectory_alignment"] == "no-trajectory"
    assert o["divergence_score"] == 0.0


def test_intermediate_divergence_is_the_computed_number():
    # subject {machine, learning} vs target {learning, theory}:
    # overlap {learning}=1, union {machine, learning, theory}=3 -> J=1/3 -> 1-J=0.667
    o = _outcome("machine learning", {"target_domain": "learning theory", "target_capability": ""})
    assert o["divergence_score"] == pytest.approx(0.667, abs=0.001)
    assert o["trajectory_alignment"] == "divergent"
