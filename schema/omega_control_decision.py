"""Strict, immutable post-episode monitoring and semantic control contract."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Mapping

OMEGA_LANES = ("chat", "agent", "browser", "window")
ALLOWED_PRESSURE_SETPOINTS = frozenset({
    "application_debt_delta", "synthesis_debt_delta", "execution_debt_delta",
    "mastery_debt_delta", "clarification_debt_delta", "verification_debt_delta",
    "confidence_debt_delta",
})
# The intents Omega may choose to continue with. PURSUE_OUTCOME is deliberately absent: it
# needs an emergent-pressure payload the control envelope does not carry, and Meta A fails
# hard without one (live 2026-09-15: a window reflection chose it and the tick died).
PRESSURE_INTENTS = frozenset({
    "BROWSE", "USE_SKILL", "USE_TOOL", "LEARN", "ASK_OPERATOR",
})
_AGENT_INTENTS = frozenset({
    "USE_SKILL", "USE_TOOL", "PURSUE_OUTCOME", "PLUGIN_OPERATION", "IMAGE_CREATE", "LEARN",
})


def omega_lane_for_intent(intent: str) -> str:
    """Global A's intent-to-foldback-lane rule for episodes the operator router did not route.

    Routed operator turns take their lane from the route (global_loop.intent.route);
    successors reuse this rule verbatim. Document creation is a chat-lane episode.
    """
    if intent == "BROWSE":
        return "browser"
    if intent == "REFLECT_WINDOW":
        return "window"
    return "agent" if intent in _AGENT_INTENTS else "chat"


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if type(value) is not dict:
        raise ValueError("Expected a JSON object")
    if not required <= value.keys() or value.keys() - required - (optional or set()):
        raise ValueError(f"Expected required fields {sorted(required)} and optional {sorted(optional or [])}")
    return value


def _text(value: Any, name: str, limit: int = 4000, *, empty: bool = False) -> str:
    if type(value) is not str or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"{name} must be {'possibly empty' if empty else 'nonempty'} text <= {limit}")
    return value


def _unit(value: Any, name: str) -> None:
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number in [0,1], not a boolean")


def _texts(value: Any, name: str, maximum: int, minimum: int = 0) -> tuple[str, ...]:
    if type(value) not in (list, tuple) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} requires {minimum}..{maximum} text items")
    return tuple(_text(item, name) for item in value)


@dataclass(frozen=True)
class OmegaAssessment:
    who: str
    what: str
    when: str
    where: str
    why: str
    how: str
    desired_state: str
    observed_state: str
    gap: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name != "evidence":
                _text(getattr(self, field.name), field.name, empty=field.name == "gap")
        object.__setattr__(self, "evidence", _texts(self.evidence, "evidence", 32))

    @classmethod
    def from_dict(cls, value: Any) -> OmegaAssessment:
        data = _keys(value, {field.name for field in fields(cls)})
        if type(data["evidence"]) is not list:
            raise ValueError("evidence must be a JSON array")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {field.name: list(self.evidence) if field.name == "evidence"
                else getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True)
class OmegaPressureDecision:
    intent: str
    confidence: float
    reason: str
    setpoints: Mapping[str, float]
    desired_state: str
    observed_state: str
    resolution_conditions: tuple[str, ...]
    domain: str | None = None
    topic: str | None = None

    def __post_init__(self) -> None:
        if type(self.intent) is not str or self.intent not in PRESSURE_INTENTS:
            raise ValueError("Unknown pressure intent")
        _unit(self.confidence, "confidence")
        for name in ("reason", "desired_state", "observed_state"):
            _text(getattr(self, name), name)
        for name in ("domain", "topic"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name, 1000)
        if type(self.setpoints) not in (dict, MappingProxyType):
            raise ValueError("setpoints must be an object")
        if self.setpoints.keys() - ALLOWED_PRESSURE_SETPOINTS:
            raise ValueError("Unknown pressure setpoint")
        for name, value in self.setpoints.items():
            _unit(value, name)
        object.__setattr__(self, "setpoints", MappingProxyType(dict(self.setpoints)))
        object.__setattr__(self, "resolution_conditions",
                           _texts(self.resolution_conditions, "resolution_conditions", 16, 1))

    @classmethod
    def from_dict(cls, value: Any) -> OmegaPressureDecision:
        required = {"intent", "confidence", "reason", "setpoints", "desired_state",
                    "observed_state", "resolution_conditions"}
        optional = {"domain", "topic"}
        data = _keys(value, required, optional)
        if type(data["resolution_conditions"]) is not list:
            raise ValueError("resolution_conditions must be a JSON array")
        if type(data["setpoints"]) is not dict:
            raise ValueError("setpoints must be a JSON object")
        for name in optional & data.keys():
            _text(data[name], name, 1000)
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        result = {field.name: getattr(self, field.name) for field in fields(self)
                  if getattr(self, field.name) is not None}
        result["setpoints"] = dict(self.setpoints)
        result["resolution_conditions"] = list(self.resolution_conditions)
        return result


@dataclass(frozen=True)
class OmegaControlDecision:
    assessment: OmegaAssessment
    decision: str
    pressure: OmegaPressureDecision | None
    synthesis: str

    def __post_init__(self) -> None:
        if type(self.assessment) is not OmegaAssessment:
            raise ValueError("assessment must be OmegaAssessment")
        if type(self.decision) is not str or self.decision not in ("DONE", "PRESSURE", "ASK_OPERATOR"):
            raise ValueError("Unknown Omega control decision")
        _text(self.synthesis, "synthesis", 8000)
        if self.decision == "DONE":
            if self.assessment.gap != "" or self.pressure is not None:
                raise ValueError("DONE requires an empty gap and null pressure")
        else:
            if not self.assessment.gap.strip() or type(self.pressure) is not OmegaPressureDecision:
                raise ValueError("Unresolved decisions require a gap and validated pressure")
            if self.decision == "ASK_OPERATOR" and self.pressure.intent != "ASK_OPERATOR":
                raise ValueError("ASK_OPERATOR requires clarification pressure")

    @classmethod
    def from_dict(cls, value: Any) -> OmegaControlDecision:
        data = _keys(value, {"assessment", "decision", "pressure", "synthesis"})
        return cls(OmegaAssessment.from_dict(data["assessment"]), data["decision"],
                   None if data["pressure"] is None else OmegaPressureDecision.from_dict(data["pressure"]),
                   data["synthesis"])

    def to_dict(self) -> dict[str, Any]:
        return {"assessment": self.assessment.to_dict(), "decision": self.decision,
                "pressure": None if self.pressure is None else self.pressure.to_dict(),
                "synthesis": self.synthesis}
