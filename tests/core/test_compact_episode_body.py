"""Fix-2: compact_episode_for_reflection schema and trajectory_diff preservation."""

from __future__ import annotations

from pressure_loop.episode_journal import compact_episode_for_reflection


def _bloated_body(episode_id: str = "ep-0") -> dict:
    """Realistic enriched body with prompt-bloat fields."""
    turn = {
        "cycle_id": f"{episode_id}-turn-1",
        "ingress_class": "operator_message",
        "outcome_hint": "respond",
        "action_kind": "RESPOND",
        "ingress_confidence": 0.91,
        "pressure_contribution": 0.42,
        "response_summary": "A" * 400,
        "memory_operation": "recall_identity",
        "tool_or_memory_context": {"lobe": 3, "subdomain": "persona"},
        "store_receipt": {"written": True, "path": "/memory/600_journal/x.json"},
    }
    return {
        "episode_id": episode_id,
        "intent": "RESPOND",
        "action": "RESPOND",
        "outcome": {"status": "success"},
        "trajectory_update": {"target_domain": "python"},
        "pressure": {
            "aggregate_pressure": 0.75,
            "opened_by": "curiosity_gap",
            "contributors": [{"type": "curiosity_gap", "weight": 0.75}] * 5,
            "dominant_contributor": "curiosity_gap",
        },
        "execution": {
            "execution_action_pack": {"action": "RESPOND", "tool": "none"},
            "state": "completed",
        },
        "brain_turns": [dict(turn, cycle_id=f"{episode_id}-turn-{i}") for i in range(6)],
        "topic_history": {"topic": "decorators", "domain": "python", "source": "mastery"},
        "open_query_state": [
            {
                "question_id": f"q-{j}",
                "question_text": "What is the capital of France?" + ("?" * 80),
                "status": "open",
            }
            for j in range(8)
        ],
        "cascade_status": "completed",
        "neuromod": {"dopamine": 0.3, "curiosity": 0.8},
    }


def test_compact_episode_for_reflection_schema():
    """B2: compact body drops bloat, keeps reflection-critical fields."""
    compact = compact_episode_for_reflection(_bloated_body())

    assert "brain_turns" not in compact
    assert "open_query_state" not in compact
    assert compact.get("brain_turn_count") == 6
    assert compact.get("intent") == "RESPOND"
    assert compact.get("outcome") == {"status": "success"}
    assert compact.get("topic_history") == {
        "topic": "decorators",
        "domain": "python",
        "source": "mastery",
    }
    assert compact.get("trajectory_update") == {"target_domain": "python"}
    assert compact.get("cascade_status") == "completed"
    assert compact.get("neuromod") == {"dopamine": 0.3, "curiosity": 0.8}

    pressure = compact.get("pressure") or {}
    assert set(pressure.keys()) <= {"aggregate_pressure", "opened_by"}
    assert pressure.get("aggregate_pressure") == 0.75
    assert pressure.get("opened_by") == "curiosity_gap"

    execution = compact.get("execution") or {}
    assert set(execution.keys()) <= {"action", "state", "reason"}
    assert execution.get("action") == "RESPOND"
    assert execution.get("state") == "completed"


def test_compact_caps_raw_tool_result_keeps_summary_and_temporal():
    """Window-prompt bloat fix: a full web page in outcome.artifacts.tool_result
    (29.5k+ chars / 85% of the prompt in the live incident) is capped in favor of
    tool_result_summary, while reflection-critical outcome.temporal /
    learning_state / observations survive the trim."""
    import json

    big_page = "PAGE " * 12000  # ~60k chars, the unbounded raw fetch
    body = {
        "episode_id": "ep-big",
        "intent": "LEARN",
        "outcome": {
            "outcome": "success",
            "artifacts": {
                "tool_result": big_page,
                "tool_result_summary": "concise grounded summary of the page",
                "response_text": None,
            },
            "temporal": {"phase": "pressure", "ts": "2026"},
            "learning_state": {"mastery": 0.3},
            "observations": ["the page covered ETL"],
            "consistency": {"ok": True},
        },
    }
    compact = compact_episode_for_reflection(body)
    oc = compact["outcome"]

    assert len(json.dumps(compact)) < 12000, "raw page must no longer bloat the prompt"
    raw = oc["artifacts"].get("tool_result")
    assert raw is None or len(str(raw)) <= 6200
    # the SIGNAL survives — only the raw page is shed
    assert oc["artifacts"]["tool_result_summary"] == "concise grounded summary of the page"
    assert oc["temporal"] == {"phase": "pressure", "ts": "2026"}
    assert oc["learning_state"] == {"mastery": 0.3}
    assert oc["observations"] == ["the page covered ETL"]
    assert oc["consistency"] == {"ok": True}


def test_compact_small_outcome_passed_through_intact():
    body = {"episode_id": "ep", "intent": "LEARN", "outcome": {"status": "success"}}
    assert compact_episode_for_reflection(body)["outcome"] == {"status": "success"}
