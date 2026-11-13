"""IDLE episodes carry reflectable reason (NO_TOPIC) — not bare skipped."""

from __future__ import annotations

from copy import deepcopy

from meta_loop.meta_b import run_meta_b_front
from pressure_loop.episode_journal import compact_episode_for_reflection

NO_TOPIC_REASON = "NO_TOPIC_AVAILABLE — no resolvable learning target; aborting LEARN"


def _idle_execution_pack(*, eid: str = "ep-idle") -> dict:
    return {
        "episode_id": eid,
        "action": "IDLE",
        "tool": None,
        "tool_reason": NO_TOPIC_REASON,
        "memory_required": False,
        "memory_reason": None,
        "memory_scope": [],
        "memory_query": None,
        "memory_delivery_mode": None,
        "memory_tools": [],
        "execution_intent": "SKIP_NO_TOPIC",
        "execution_inputs": {},
        "execution_constraints": {
            "planning_only": True,
            "no_inference_in_meta": True,
            "no_execution_in_meta": True,
            "no_model_fields": True,
        },
        "source_action_goal": {
            "episode_id": eid,
            "intent": "LEARN",
            "target": "",
            "confidence": 0.5,
            "reason": "no resolvable topic",
        },
    }


def _idle_episode(*, cycle_kind: str = "idle", eid: str = "ep-idle"):
    pack = _idle_execution_pack(eid=eid)

    class Ep:
        episode_id = eid
        pressure = {"opened_by": "learning_debt", "contributors": []}
        global_ = {
            "action_goal": {
                "episode_id": eid,
                "intent": "LEARN",
                "target": "",
                "confidence": 0.5,
                "reason": "no resolvable topic",
            }
        }
        meta = {"execution_action_pack": pack}
        auto = {
            "episode_id": eid,
            "action": "IDLE",
            "source_action_goal": deepcopy(global_["action_goal"]),
            "execution_pack": deepcopy(pack),
            "status": cycle_kind,
            "cycle_kind": cycle_kind,
            "telemetry": {"cycle_kind": cycle_kind, "runtime_ms": 0, "warnings": []},
            "outputs": {},
            "memory": {"memory_status": "skipped"},
        }

    return Ep()


def test_idle_meta_reflection_carries_reason_and_idle_disposition():
    ep = _idle_episode()
    pack = run_meta_b_front(ep)
    assert pack is not None

    execution = pack.execution
    reason = execution.get("reason")
    assert reason is not None and reason != ""
    assert "NO_TOPIC_AVAILABLE" in str(reason)
    assert execution.get("execution_intent") == "SKIP_NO_TOPIC"
    assert execution.get("disposition") == "idle"
    assert execution.get("status") == "idle"
    assert execution.get("action") == "IDLE"


def test_compact_episode_for_reflection_carries_idle_reason():
    ep = _idle_episode()
    body = {
        "episode_id": ep.episode_id,
        "intent": "LEARN",
        "action": "IDLE",
        "outcome": {"status": "idle"},
        "execution": {
            "execution_action_pack": ep.meta["execution_action_pack"],
            "state": "idle",
        },
        "pressure": ep.pressure,
        "cascade_status": "completed",
    }
    compact = compact_episode_for_reflection(body)
    execution = compact.get("execution") or {}
    reason = execution.get("reason")
    assert reason is not None and reason != ""
    assert "NO_TOPIC_AVAILABLE" in str(reason)
    assert execution.get("action") == "IDLE"
