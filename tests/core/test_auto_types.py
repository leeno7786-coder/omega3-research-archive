"""Slice 3 B1 — AUTO types + AutoInput assembly."""

from __future__ import annotations

import pytest

from auto_loop.auto import (
    MODEL_PROFILES,
    PROMPT_TYPES,
    RESPONSE_MODES,
    AutoInput,
    auto_input_from_episode,
)
from meta_loop.meta_a import run_meta_a_front


def test_prompt_and_model_contracts_cover_production_routes():
    assert {
        "RESPONSE_REASONING",
        "RESPONSE_QUESTION",
        "RESPONSE_ACK",
        "RESPONSE_FREEFORM",
        "RESPONSE_TEMPORAL",
        "PROACTIVE_REASONING",
        "PROACTIVE_ACK",
        "TOOL_CALL_SYSTEM",
        "TOOL_RESULT_SYNTHESIS",
        "SKILL_BUILD_SYSTEM",
        "IMAGE_SYSTEM",
        "DOCUMENT_SYSTEM",
        "IDLE_SYSTEM",
    }.issubset(PROMPT_TYPES)
    assert len(MODEL_PROFILES) == 4
    assert "TEXT_REASONING" in MODEL_PROFILES
    assert RESPONSE_MODES == ("reasoning", "question", "ack", "freeform", "summary")


def test_auto_input_from_slice2_episode():
    class Ep:
        episode_id = "ep_slice3"
        opened_by = "operator_command"
        pressure = {
            "opened_by": "operator_command",
            "contributors": [
                {
                    "source": "operator",
                    "type": "operator_command",
                    "weight": 1.0,
                    "metadata": {"message": "Learn Python"},
                }
            ],
            "trajectory_snapshot": {"t": 1},
        }
        global_ = {
            "action_goal": {
                "episode_id": "ep_slice3",
                "intent": "LEARN",
                "target": "Python",
                "confidence": 0.9,
                "reason": "test",
            }
        }
        meta = {}

    run_meta_a_front(Ep)
    ai = auto_input_from_episode(Ep)
    assert ai is not None
    assert ai.episode_id == "ep_slice3"
    assert ai.execution_pack.get("action") == "USE_TOOL"
    assert ai.source_action_goal.get("intent") == "LEARN"
    assert ai.pressure["reason"] == "operator_command"


def test_auto_input_rejects_pack_bound_to_different_episode():
    class Ep:
        episode_id = "ep_active"
        opened_by = "operator_command"
        pressure = {"opened_by": "operator_command", "contributors": []}
        global_ = {
            "action_goal": {
                "episode_id": "ep_active",
                "intent": "IDLE",
                "target": None,
                "confidence": 0.5,
                "reason": "test",
            }
        }
        meta = {}

    run_meta_a_front(Ep)
    Ep.meta["execution_action_pack"]["episode_id"] = "ep_stale"
    with pytest.raises(ValueError, match="episode identity"):
        auto_input_from_episode(Ep)


def test_auto_input_none_without_execution_pack():
    class Ep:
        episode_id = "ep_empty"
        pressure = {}
        global_ = {}
        meta = {}

    assert auto_input_from_episode(Ep) is None
