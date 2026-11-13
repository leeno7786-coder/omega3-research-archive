"""Slice 3 B2 — PHASE 0 pack validation."""

from __future__ import annotations

from auto_loop.auto import validate_for_execution
from meta_loop.meta_a import execution_action_pack_to_dict, build_execution_action_pack, MetaAInput


def _valid_pack_dict():
    mi = MetaAInput(
        episode_id="ep_v",
        pressure={"reason": "operator_command", "contributors": []},
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep_v",
            "intent": "LEARN",
            "target": "Python",
            "confidence": 1.0,
            "reason": "r",
        },
    )
    return execution_action_pack_to_dict(build_execution_action_pack(mi))


def test_valid_pack_ok():
    ok, fk, reason = validate_for_execution(_valid_pack_dict())
    assert ok is True
    assert fk is None


def test_banned_model_key_fails():
    pack = _valid_pack_dict()
    pack["model"] = "gpt-4"
    ok, fk, _ = validate_for_execution(pack)
    assert ok is False
    assert fk == "invalid_execution_pack"


def test_tool_without_tool_reason_fails():
    pack = _valid_pack_dict()
    pack["tool_reason"] = None
    ok, fk, _ = validate_for_execution(pack)
    assert ok is False
    assert fk == "invalid_execution_pack"


def test_memory_required_without_tools_fails():
    pack = _valid_pack_dict()
    pack["memory_required"] = True
    pack["memory_reason"] = "need memory"
    pack["memory_query"] = "what did I say"
    pack["memory_tools"] = []
    ok, fk, _ = validate_for_execution(pack)
    assert ok is False
    assert fk == "invalid_execution_pack"
