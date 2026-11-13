"""Omega's window lane folds Global B's locked-window view through the shared control spine."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from organ.promptorgan.packs.foldback import foldback_spec_for
from tests.omega_loop.test_foldback_prompt_dispatch import WINDOW_VIEW, packet_fixture
from tests.omega_loop.test_omega_prompt_contracts import done_control

REFLECTION = {
    "reflection": {
        "summary": "Two lookups circling one gap.",
        "reasoning": {
            "what": "looked things up",
            "why": "the gap recurred",
            "identity": "prefers verified answers",
        },
        "emergent_observations": {
            "repeated_behaviors": ["reaches for recall first"],
            "stable_patterns": ["short answers"],
        },
        "trajectory": {
            "decision": "adjust",
            "target_domain": "python",
            "target_capability": "async",
            "reason": "the gap recurred",
        },
    },
    "trajectory_update": {
        "target_domain": "python",
        "target_capability": "async",
        "confidence": 0.6,
        "reason": "the gap recurred",
    },
}


def window_packet(**changes):
    return replace(packet_fixture("window"), **changes)


def test_window_lane_orients_the_locked_window_through_the_shared_spine():
    packet = window_packet()
    system, user = foldback_spec_for(packet).build(packet)
    for term in ("Nelson", "5W1H", "WINDOW MONITOR", "ACROSS",
                 "Trajectory and task pressure are separate", '"reflection"', '"trajectory_update"'):
        assert term in system
    assert f'"window_id": "{WINDOW_VIEW["window_id"]}"' in user
    assert "ep-2" in user


def test_a_window_may_change_trajectory_while_deciding_done():
    packet = window_packet()
    result = foldback_spec_for(packet).parse({"control": done_control(), **REFLECTION}, packet)

    assert result.control_decision == done_control()
    assert (result.pressure_params, result.intent_decision) == ({}, None)
    outcome = result.window_reflection
    assert (outcome["status"], outcome["window_id"], outcome["reflection_source"]) == (
        "reflected", WINDOW_VIEW["window_id"], "model")
    assert outcome["trajectory_update"]["target_domain"] == "python"
    assert outcome["trajectory_update"]["supporting_episode"] == WINDOW_VIEW["window_id"]
    assert outcome["reflection"]["reasoning"]["identity"] == "prefers verified answers"


def test_window_task_pressure_travels_only_in_control():
    control = done_control()
    control["assessment"]["gap"] = "the async repair was never verified"
    control.update(decision="PRESSURE", pressure={
        "intent": "USE_SKILL", "confidence": 0.7, "reason": "verify the async repair",
        "setpoints": {"execution_debt_delta": 0.6}, "desired_state": "async repair verified",
        "observed_state": "two unverified attempts",
        "resolution_conditions": ["focused async tests pass"]})
    packet = window_packet()

    result = foldback_spec_for(packet).parse({"control": control, **REFLECTION}, packet)

    assert result.intent_decision["intent"] == "USE_SKILL"
    assert result.pressure_params == {"execution_debt_delta": 0.6}
    assert result.window_reflection["trajectory_update"]["target_capability"] == "async"


@pytest.mark.parametrize("payload,rejection", [
    ({}, "'reflection' object"),
    ({"reflection": {"reasoning": "sure thing"}}, "reflection.reasoning must be an object"),
    ({**REFLECTION, "trajectory_update": {"target_capability": "async"}}, "cannot be recorded"),
])
def test_the_reflection_payload_is_required_and_strictly_validated(payload, rejection):
    packet = window_packet()
    with pytest.raises(ValueError, match=re.escape(rejection)):
        foldback_spec_for(packet).parse({"control": done_control(), **payload}, packet)


def test_a_window_without_episodes_is_not_reflected_by_the_model():
    packet = window_packet(window_context={**WINDOW_VIEW, "episodes": []})
    with pytest.raises(ValueError, match="no episode bodies"):
        foldback_spec_for(packet).parse({"control": done_control(), **REFLECTION}, packet)


def test_window_context_belongs_to_the_window_lane_and_round_trips():
    from omega_loop.episode.run import _packet_from_episode

    with pytest.raises(ValueError, match="window context"):
        replace(packet_fixture("chat"), window_context=WINDOW_VIEW)
    with pytest.raises(ValueError, match="window context"):
        window_packet(window_context=None)
    packet = window_packet()
    episode = SimpleNamespace(global_={"omega_packet": packet.to_dict()}, auto=None)
    assert _packet_from_episode(episode).window_context == WINDOW_VIEW


def test_the_runner_repairs_a_window_reflection_contract_violation(monkeypatch):
    from omega_loop.episode import run

    packet = window_packet()
    monkeypatch.setattr(run, "_with_current_identity", lambda value: value)
    attempts: list[str] = []

    def reason(system, user):
        attempts.append(user)
        body = {"control": done_control()}
        if len(attempts) == 2:
            body.update(REFLECTION)
        return json.dumps(body)

    episode = SimpleNamespace(episode_id=packet.episode_id,
                              global_={"omega_packet": packet.to_dict()}, temporal={})
    result = run.run_omega_for_episode(episode, reason_fn=reason)

    assert result.window_reflection["reflection"]["summary"] == "Two lookups circling one gap."
    assert len(attempts) == 2
    assert "'reflection' object" in attempts[1]
