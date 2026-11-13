"""Execution-contract attachment and the motor-program policy knobs."""

from __future__ import annotations

import logging
from copy import deepcopy
from types import SimpleNamespace

from omega_loop.motor.run import attach_motor_program
from pressure_loop.policy import (
    fetched_comprehension_timeout_seconds,
    motor_programs_distinct_failure_limit,
    motor_programs_enabled,
    motor_programs_equivalent_no_progress_limit,
    motor_programs_eligible_actions,
    motor_programs_max_parse_attempts,
    motor_programs_shadow_mode,
    motor_programs_timeout_seconds,
)
from schema.motor_program import MotorProgram


def _episode(action: str = "USE_TOOL") -> SimpleNamespace:
    return SimpleNamespace(
        episode_id="ep1",
        opened_at="2026-08-03T18:00:00+00:00",
        opened_by="mastery_gap",
        pressure={"opened_by": "mastery_gap", "contributors": []},
        global_={"intent_decision": {"intent": "LEARN", "confidence": 0.9}},
        meta={
            "execution_action_pack": {
                "episode_id": "ep1",
                "action": action,
                "tool": "web_fetch",
                "execution_inputs": {
                    "query": "backpropagation",
                    "domain": "machine-learning",
                },
                "source_action_goal": {"intent": "LEARN"},
            },
        },
    )


def _program(episode_id: str = "ep1") -> MotorProgram:
    return MotorProgram.from_dict(
        {
            "program_id": f"mp_{episode_id}_01",
            "parent_program_id": "",
            "outcome_id": "out-backprop",
            "episode_id": episode_id,
            "attempt_number": 1,
            "created_at": "2026-08-03T18:00:00+00:00",
            "outcome_contract": {
                "objective": "understand and demonstrate backpropagation",
                "acceptance_criteria": ["derive chain rule", "verify gradients"],
            },
            "pathway": {
                "pathway_id": "tool:web_fetch",
                "revision": 1,
                "name": "web_fetch",
                "method": "execute the web_fetch tool Meta A selected",
                "operational_rationale": "Default external information acquisition",
            },
            "execution_plan": {
                "capability_class": "web_research",
                "operations": ["backpropagation"],
            },
            "evidence_contract": {
                "required_checks": ["fetch"],
                "completion_signals": ["derivation verified"],
            },
        }
    )


def test_shadow_attachment_preserves_execution_inputs_byte_for_byte(monkeypatch) -> None:
    episode = _episode()
    before = deepcopy(episode.meta["execution_action_pack"]["execution_inputs"])
    program = _program()
    monkeypatch.setattr("omega_loop.motor.run.motor_programs_shadow_mode", lambda: True)

    attached = attach_motor_program(episode, program)

    assert "motor_program" not in episode.meta
    assert episode.meta["shadow_motor_program"] == attached
    assert attached["shadow"] is True
    assert episode.meta["execution_action_pack"]["execution_inputs"] == before
    assert attached["fingerprint"] == program.fingerprint()


def test_non_shadow_attachment_uses_exact_attached_dict(monkeypatch) -> None:
    episode = _episode()
    monkeypatch.setattr("omega_loop.motor.run.motor_programs_shadow_mode", lambda: False)

    attached = attach_motor_program(episode, _program())

    transported = episode.meta["execution_action_pack"]["execution_inputs"]["motor_program"]
    assert transported is not attached
    assert transported is not episode.meta["motor_program"]
    assert transported == episode.meta["motor_program"]
    assert attached["shadow"] is False


def test_motor_program_telemetry_has_exact_allowed_envelope(monkeypatch) -> None:
    captured = {}

    def emit_data(system: str, event: str, **fields) -> None:
        captured.update({"system": system, "event": event, "fields": fields})

    monkeypatch.setattr("guardian.terminal_feedback.emit_data", emit_data)

    attach_motor_program(_episode(), _program())

    assert captured["system"] == "omega"
    assert captured["event"] == "motor_program"
    assert set(captured["fields"]) == {
        "program_id",
        "parent_program_id",
        "outcome_id",
        "episode_id",
        "pathway_id",
        "pathway_revision",
        "fingerprint",
        "transport",
        "shadow",
    }


def test_motor_program_telemetry_failure_is_diagnostic_and_failure_soft(
    monkeypatch, caplog
) -> None:
    program = _program()

    def fail_emit(*_args, **_kwargs) -> None:
        raise OSError("telemetry offline")

    monkeypatch.setattr("guardian.terminal_feedback.emit_data", fail_emit)

    with caplog.at_level(logging.DEBUG, logger="omega.omega_loop.motor"):
        attached = attach_motor_program(_episode(), program)

    assert attached["program_id"] == program.program_id
    assert "motor program telemetry failed" in caplog.text


def test_motor_program_policy_defaults_are_typed() -> None:
    empty_policy = {"cognition_chain": {}}

    assert motor_programs_enabled(empty_policy) is True
    assert motor_programs_shadow_mode(empty_policy) is True
    assert motor_programs_eligible_actions(empty_policy) == frozenset(
        {"USE_TOOL", "USE_SKILL", "EXECUTE_MOTOR_PROGRAM"}
    )
    assert motor_programs_max_parse_attempts(empty_policy) == 2
    assert motor_programs_timeout_seconds(empty_policy) == 600
    assert motor_programs_equivalent_no_progress_limit(empty_policy) == 2
    assert motor_programs_distinct_failure_limit(empty_policy) == 3


def test_motor_program_timeout_override_is_bounded() -> None:
    def policy(value: int) -> dict:
        return {"cognition_chain": {"motor_programs": {"timeout_seconds": value}}}

    assert motor_programs_timeout_seconds(policy(600)) == 600
    assert motor_programs_timeout_seconds(policy(59)) == 600
    assert motor_programs_timeout_seconds(policy(901)) == 600


def test_fetched_comprehension_timeout_override_is_bounded() -> None:
    def policy(value) -> dict:
        return {"cognition_chain": {"fetched_comprehension": {"timeout_seconds": value}}}

    assert fetched_comprehension_timeout_seconds(policy(600)) == 600
    assert fetched_comprehension_timeout_seconds(policy(59)) == 600
    assert fetched_comprehension_timeout_seconds(policy(901)) == 600
    assert fetched_comprehension_timeout_seconds(policy("bad")) == 600


def test_motor_program_action_override_cannot_admit_response() -> None:
    policy = {
        "cognition_chain": {
            "motor_programs": {"eligible_actions": ["RESPONSE"]},
        }
    }

    assert motor_programs_eligible_actions(policy) == frozenset()
