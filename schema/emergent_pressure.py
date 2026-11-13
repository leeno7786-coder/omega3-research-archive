"""Immutable executable pressure hypothesis authored by Omega."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from schema.motor_program import _freeze, _required_string_tuple, _thaw


def _clamp_unit(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(text for item in value if (text := str(item).strip()))


@dataclass(frozen=True)
class EmergentPressure:
    pressure_id: str
    name: str
    source_episode_id: str
    outcome_id: str
    desired_state: Any
    observed_state: Any
    gap_description: str
    evidence: tuple[str, ...]
    failed_program_ids: tuple[str, ...]
    urgency: float
    persistence: int
    proposed_action: dict[str, Any]
    activation_conditions: tuple[str, ...]
    resolution_conditions: tuple[str, ...]
    supersedes_pressure_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "desired_state", _freeze(self.desired_state))
        object.__setattr__(self, "observed_state", _freeze(self.observed_state))
        object.__setattr__(self, "evidence", _strings(self.evidence))
        object.__setattr__(self, "failed_program_ids", _strings(self.failed_program_ids))
        object.__setattr__(self, "urgency", _clamp_unit(self.urgency))
        object.__setattr__(self, "persistence", max(0, int(self.persistence)))
        object.__setattr__(
            self,
            "activation_conditions",
            _required_string_tuple(self.activation_conditions, "activation_conditions"),
        )
        object.__setattr__(
            self,
            "resolution_conditions",
            _required_string_tuple(self.resolution_conditions, "resolution_conditions"),
        )
        if not all(
            (
                self.pressure_id.strip(),
                self.name.strip(),
                self.source_episode_id.strip(),
                self.outcome_id.strip(),
            )
        ):
            raise ValueError("emergent pressure identity fields are required")
        if not self.gap_description.strip():
            raise ValueError("gap_description is required")
        action = dict(self.proposed_action)
        intended_transformation = str(action.get("intended_transformation") or "").strip()
        candidate_pathway = str(action.get("candidate_pathway") or "").strip()
        try:
            required_capabilities = _required_string_tuple(
                action.get("required_capabilities"), "required_capabilities"
            )
        except ValueError as exc:
            raise ValueError("proposed_action must define executable semantics") from exc
        if not intended_transformation or not candidate_pathway:
            raise ValueError("proposed_action must define executable semantics")
        action["intended_transformation"] = intended_transformation
        action["candidate_pathway"] = candidate_pathway
        action["required_capabilities"] = required_capabilities
        object.__setattr__(self, "proposed_action", _freeze(action))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EmergentPressure:
        return cls(
            pressure_id=str(value.get("pressure_id") or ""),
            name=str(value.get("name") or ""),
            source_episode_id=str(value.get("source_episode_id") or ""),
            outcome_id=str(value.get("outcome_id") or ""),
            desired_state=value.get("desired_state"),
            observed_state=value.get("observed_state"),
            gap_description=str(value.get("gap_description") or ""),
            evidence=_strings(value.get("evidence")),
            failed_program_ids=_strings(value.get("failed_program_ids")),
            urgency=float(value.get("urgency") or 0.0),
            persistence=int(value.get("persistence") or 0),
            proposed_action=dict(value.get("proposed_action") or {}),
            activation_conditions=_strings(value.get("activation_conditions")),
            resolution_conditions=_strings(value.get("resolution_conditions")),
            supersedes_pressure_id=str(value.get("supersedes_pressure_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pressure_id": self.pressure_id,
            "name": self.name,
            "source_episode_id": self.source_episode_id,
            "outcome_id": self.outcome_id,
            "desired_state": _thaw(self.desired_state),
            "observed_state": _thaw(self.observed_state),
            "gap_description": self.gap_description,
            "evidence": list(self.evidence),
            "failed_program_ids": list(self.failed_program_ids),
            "urgency": self.urgency,
            "persistence": self.persistence,
            "proposed_action": _thaw(self.proposed_action),
            "activation_conditions": list(self.activation_conditions),
            "resolution_conditions": list(self.resolution_conditions),
            "supersedes_pressure_id": self.supersedes_pressure_id,
        }
