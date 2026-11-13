"""Slice 2 B5 — acceptance tests + invariants (spec)."""

from __future__ import annotations

from meta_loop.meta_a import (
    MetaAInput,
    build_execution_action_pack,
    validate_execution_action_pack,
    execution_action_pack_to_dict,
)


def _goal(intent: str, target, message: str = ""):
    # Global A no longer carries a target — topic reaches Meta A via the contributor
    # message. Map a string target onto the message for these acceptance fixtures.
    eff_message = message or (target if isinstance(target, str) else "")
    contributors = []
    if eff_message:
        contributors.append(
            {
                "source": "operator",
                "type": "operator_command",
                "weight": 1.0,
                "metadata": {"message": eff_message},
            }
        )
    return MetaAInput(
        episode_id="ep_1",
        pressure={"reason": "operator_command", "contributors": contributors},
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep_1",
            "intent": intent,
            "confidence": 0.9,
            "reason": "acceptance",
        },
    )


def test_t1_learn_python():
    pack = build_execution_action_pack(_goal("LEARN", "Python"))
    d = execution_action_pack_to_dict(pack)
    assert d["action"] == "USE_TOOL"
    assert d["tool"] == "web_fetch"
    assert d["tool_reason"]
    assert d["memory_required"] is True
    assert d["memory_query"] == "Python"
    assert [t["tool"] for t in d["memory_tools"]] == ["remember_code"]
    assert d["execution_inputs"].get("query")
    assert d["execution_constraints"]["planning_only"] is True
    assert d["source_action_goal"]["intent"] == "LEARN"
    validate_execution_action_pack(d)
    for banned in ("model", "llm", "prompt", "temperature"):
        assert banned not in d
        assert banned not in (d.get("execution_inputs") or {})


def test_t2_what_did_i_tell_respond_memory():
    pack = build_execution_action_pack(
        _goal(
            "RESPOND",
            {"question": "What did I tell Omega about Python?"},
            "What did I tell Omega about Python?",
        )
    )
    d = execution_action_pack_to_dict(pack)
    assert d["action"] == "RESPONSE"
    assert d["memory_required"] is True
    assert d["memory_reason"]
    assert d["memory_query"]
    assert len(d["memory_tools"]) > 0
    assert len(d["memory_scope"]) > 0
    assert "response" not in (d.get("execution_inputs") or {})
    validate_execution_action_pack(d)


def test_t3_image_create():
    pack = build_execution_action_pack(
        _goal(
            "IMAGE_CREATE",
            "omega cognition graph",
            "Generate an image of an omega cognition graph",
        )
    )
    d = execution_action_pack_to_dict(pack)
    assert d["action"] == "IMAGE_CREATION"
    assert d["tool"] == "image_tool"
    assert d["tool_reason"]
    assert d["execution_inputs"].get("image_spec")
    validate_execution_action_pack(d)


def test_t4_idle():
    pack = build_execution_action_pack(_goal("IDLE", None))
    d = execution_action_pack_to_dict(pack)
    assert d["action"] == "IDLE"
    assert d["tool"] is None
    assert d["memory_required"] is False
    validate_execution_action_pack(d)


def test_invariants_intent_unchanged_and_single_pack():
    ag = {
        "episode_id": "ep_1",
        "intent": "LEARN",
        "target": "Rust",
        "confidence": 1.0,
        "reason": "r",
    }
    mi = MetaAInput(
        episode_id="ep_1",
        pressure={"reason": "operator_command", "contributors": []},
        trajectory_snapshot={},
        action_goal=ag,
    )
    pack = build_execution_action_pack(mi)
    # Meta A echoes Global A's foldback lane verbatim; this goal names none.
    assert pack.source_action_goal == {
        "episode_id": "ep_1",
        "intent": "LEARN",
        "confidence": 1.0,
        "reason": "r",
        "omega_lane": "",
    }
    assert pack.source_action_goal["intent"] == "LEARN"
