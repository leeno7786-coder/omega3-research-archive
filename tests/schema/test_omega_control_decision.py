from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from schema.omega_control_decision import (
    OmegaAssessment, OmegaControlDecision, OmegaPressureDecision,
)


def done():
    return {"assessment": {"who": "operator and Omega", "what": "answered",
        "when": "this episode", "where": "chat", "why": "requested",
        "how": "response", "desired_state": "answered", "observed_state": "answered",
        "gap": "", "evidence": ["response present"]},
        "decision": "DONE", "pressure": None, "synthesis": "Satisfied."}


def pressure(intent="BROWSE", decision="PRESSURE"):
    value = done()
    value["decision"] = decision
    value["assessment"]["gap"] = "Evidence still needed"
    value["pressure"] = {"intent": intent, "confidence": 0.9, "reason": "Need evidence",
        "setpoints": {"execution_debt_delta": 1.0}, "desired_state": "verified",
        "observed_state": "unverified", "resolution_conditions": ["Evidence recorded"]}
    return value


@pytest.mark.parametrize("value", [done(), pressure(), pressure("ASK_OPERATOR", "ASK_OPERATOR"),
    pressure("USE_SKILL"), pressure("USE_TOOL"), pressure("LEARN")])
def test_roundtrip(value):
    parsed = OmegaControlDecision.from_dict(value)
    assert parsed.to_dict() == value
    assert OmegaControlDecision.from_dict(parsed.to_dict()) == parsed
    assert OmegaAssessment.from_dict(value["assessment"]).to_dict() == value["assessment"]
    if value["pressure"]:
        assert OmegaPressureDecision.from_dict(value["pressure"]).to_dict() == value["pressure"]


def test_browser_pressure_carries_no_plan_cursor_and_optional_topic():
    value = pressure()
    value["pressure"].update(domain="research", topic="models")
    assert OmegaControlDecision.from_dict(value).to_dict() == value
    for legacy in ("plan_id", "step_id", "step_transition"):
        with pytest.raises(ValueError):
            OmegaControlDecision.from_dict({**value, "pressure": {**value["pressure"], legacy: "x"}})


@pytest.mark.parametrize("path,new", [
    (("decision",), "APPLY"), (("decision",), []), (("extra",), 1),
    (("assessment", "gap"), "remaining"), (("pressure",), {}),
    (("assessment", "who"), ""), (("assessment", "extra"), "unknown"),
    (("assessment", "evidence"), "not a list"), (("assessment", "evidence"), [1]),
    (("assessment", "evidence"), ["e"] * 33), (("synthesis",), "x" * 8001),
    (("assessment", "what"), "x" * 4001),
])
def test_invalid_done(path, new):
    value = done()
    node = value
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = new
    with pytest.raises(ValueError):
        OmegaControlDecision.from_dict(value)


@pytest.mark.parametrize("key", ["who", "what", "when", "where", "why", "how",
    "desired_state", "observed_state", "gap", "evidence"])
def test_missing_assessment_field(key):
    value = done()
    del value["assessment"][key]
    with pytest.raises(ValueError):
        OmegaControlDecision.from_dict(value)


@pytest.mark.parametrize("key,new", [
    ("confidence", True), ("confidence", float("nan")), ("confidence", float("inf")),
    ("confidence", -0.1), ("confidence", 1.1), ("confidence", "0.9"),
    ("intent", "APPLY"), ("intent", []), ("setpoints", {"unknown": 1}),
    ("setpoints", {"execution_debt_delta": True}), ("setpoints", {"execution_debt_delta": 2}),
    ("setpoints", {"execution_debt_delta": float("nan")}),
    ("resolution_conditions", []), ("resolution_conditions", ["x"] * 17),
    ("resolution_conditions", ["x" * 4001]), ("reason", " "), ("topic", {}),
])
def test_invalid_pressure(key, new):
    value = pressure()
    value["pressure"][key] = new
    with pytest.raises(ValueError):
        OmegaControlDecision.from_dict(value)


@pytest.mark.parametrize("key", ["selector", "click", "x", "y", "url", "keys",
    "browser_action", "tool", "arguments", "tool_args"])
def test_browser_mechanics_rejected(key):
    value = pressure()
    value["pressure"][key] = "forbidden"
    with pytest.raises(ValueError):
        OmegaControlDecision.from_dict(value)


@pytest.mark.parametrize("key", ["intent", "confidence",
    "reason", "setpoints", "desired_state", "observed_state", "resolution_conditions"])
def test_missing_pressure_field(key):
    value = pressure()
    del value["pressure"][key]
    with pytest.raises(ValueError):
        OmegaControlDecision.from_dict(value)


@pytest.mark.parametrize("decision", ["PRESSURE", "ASK_OPERATOR"])
@pytest.mark.parametrize("bad", [None, {}, [], "pressure"])
def test_required_pressure(decision, bad):
    value = pressure(decision=decision)
    value["pressure"] = bad
    with pytest.raises(ValueError):
        OmegaControlDecision.from_dict(value)


def test_gap_and_operator_intent_consistency():
    value = pressure()
    value["assessment"]["gap"] = ""
    with pytest.raises(ValueError):
        OmegaControlDecision.from_dict(value)
    value = pressure(decision="ASK_OPERATOR")
    with pytest.raises(ValueError):
        OmegaControlDecision.from_dict(value)
    # PURSUE_OUTCOME is no longer an intent Omega may choose (2026-09-15).
    value = pressure("PURSUE_OUTCOME")
    with pytest.raises(ValueError, match="intent"):
        OmegaControlDecision.from_dict(value)


def test_frozen_detached_and_constructor_validation():
    value = pressure()
    parsed = OmegaControlDecision.from_dict(value)
    before = deepcopy(value)
    value["pressure"]["setpoints"]["execution_debt_delta"] = 0
    value["assessment"]["evidence"].append("changed")
    assert parsed.to_dict() == before
    with pytest.raises(FrozenInstanceError):
        parsed.decision = "DONE"
    with pytest.raises(TypeError):
        parsed.pressure.setpoints["execution_debt_delta"] = 0
    with pytest.raises(ValueError):
        replace(parsed, decision="DONE")
    with pytest.raises(ValueError):
        replace(parsed.pressure, confidence=True)


def test_episode_result_defaults_and_transport():
    from omega_loop.episode.types import OmegaEpisodeResult
    result = OmegaEpisodeResult("ep", True, (), (), {}, None, None, None)
    assert result.control_decision is None
    value = done()
    assert replace(result, control_decision=value).to_dict()["control_decision"] == value


def test_existing_prompt_parser_shares_setpoint_authority():
    from omega_loop.episode.prompts import _ALLOWED_PRESSURE_PARAMS
    from schema.omega_control_decision import ALLOWED_PRESSURE_SETPOINTS
    assert _ALLOWED_PRESSURE_PARAMS is ALLOWED_PRESSURE_SETPOINTS
