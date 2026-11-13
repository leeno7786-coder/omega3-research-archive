"""Immutable per-attempt pathway mastery and credit outcome."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping

from schema.motor_program import _freeze, _thaw


def _clamp(value: Any, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


_MASTERY_ALIASES = {
    "knowledge": "knowledge",
    "knowledge_mastery": "knowledge",
    "application": "application",
    "application_mastery": "application",
}


def _canonical_mastery(state: Mapping[str, Any]) -> dict[str, float]:
    unknown = [str(key) for key in state if str(key) not in _MASTERY_ALIASES]
    if unknown:
        raise ValueError(f"unknown mastery dimension: {unknown[0]}")

    canonical: dict[str, float] = {}
    for dimension in ("knowledge", "application"):
        aliases = (dimension, f"{dimension}_mastery")
        present = [alias for alias in aliases if alias in state]
        if len(present) > 1:
            raise ValueError(f"duplicate mastery aliases for {dimension}")
        raw = state[present[0]] if present else 0.0
        canonical[dimension] = _clamp(raw, 0.0, 1.0)
    return canonical


@dataclass(frozen=True)
class PathwayOutcome:
    pathway_id: str
    revision: int
    task_context: str
    domain: str
    program_id: str
    pre_mastery: dict[str, float]
    post_mastery: dict[str, float]
    knowledge_delta: float
    application_delta: float
    verification_verdict: str
    progress_efficiency: float
    failure_fingerprint: str
    neuromodulator_response: dict[str, float]
    episode_id: str = ""
    outcome_id: str = ""
    pathway_method: str = ""
    material_signature: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            (
                self.pathway_id.strip(),
                self.task_context.strip(),
                self.domain.strip(),
                self.program_id.strip(),
            )
        ):
            raise ValueError("pathway outcome identity fields are required")
        if self.revision < 1:
            raise ValueError("revision must be >= 1")
        pre_mastery = _canonical_mastery(self.pre_mastery)
        post_mastery = _canonical_mastery(self.post_mastery)
        knowledge_delta = _clamp(self.knowledge_delta, -1.0, 1.0)
        application_delta = _clamp(self.application_delta, -1.0, 1.0)
        object.__setattr__(self, "pre_mastery", _freeze(pre_mastery))
        object.__setattr__(self, "post_mastery", _freeze(post_mastery))
        object.__setattr__(self, "knowledge_delta", knowledge_delta)
        object.__setattr__(self, "application_delta", application_delta)
        object.__setattr__(self, "progress_efficiency", _clamp(self.progress_efficiency, 0.0, 1.0))
        credit = {
            str(key): _clamp(value, -1.0 if str(key) == "dopamine" else 0.0, 1.0)
            for key, value in self.neuromodulator_response.items()
        }
        object.__setattr__(self, "neuromodulator_response", _freeze(credit))
        object.__setattr__(self, "material_signature", _freeze(self.material_signature))
        if not self.verification_verdict.strip():
            raise ValueError("verification_verdict is required")
        for dimension, claimed_delta in (
            ("knowledge", knowledge_delta),
            ("application", application_delta),
        ):
            derived_delta = post_mastery[dimension] - pre_mastery[dimension]
            positive_transition = derived_delta > 1e-12
            if self.verification_verdict != "verified" and (
                positive_transition or claimed_delta > 0.0
            ):
                raise ValueError("positive mastery transitions require a verified verdict")
            if not math.isclose(claimed_delta, derived_delta, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"{dimension}_delta must match pre/post mastery")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PathwayOutcome:
        return cls(
            pathway_id=str(value.get("pathway_id") or ""),
            revision=int(value.get("revision") or 0),
            task_context=str(value.get("task_context") or ""),
            domain=str(value.get("domain") or ""),
            program_id=str(value.get("program_id") or ""),
            pre_mastery=dict(value.get("pre_mastery") or {}),
            post_mastery=dict(value.get("post_mastery") or {}),
            knowledge_delta=float(value.get("knowledge_delta") or 0.0),
            application_delta=float(value.get("application_delta") or 0.0),
            verification_verdict=str(value.get("verification_verdict") or ""),
            progress_efficiency=float(value.get("progress_efficiency") or 0.0),
            failure_fingerprint=str(value.get("failure_fingerprint") or ""),
            neuromodulator_response=dict(value.get("neuromodulator_response") or {}),
            episode_id=str(value.get("episode_id") or ""),
            outcome_id=str(value.get("outcome_id") or ""),
            pathway_method=str(value.get("pathway_method") or ""),
            material_signature=dict(value.get("material_signature") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pathway_id": self.pathway_id,
            "revision": self.revision,
            "task_context": self.task_context,
            "domain": self.domain,
            "program_id": self.program_id,
            "pre_mastery": _thaw(self.pre_mastery),
            "post_mastery": _thaw(self.post_mastery),
            "knowledge_delta": self.knowledge_delta,
            "application_delta": self.application_delta,
            "verification_verdict": self.verification_verdict,
            "progress_efficiency": self.progress_efficiency,
            "failure_fingerprint": self.failure_fingerprint,
            "neuromodulator_response": _thaw(self.neuromodulator_response),
            "episode_id": self.episode_id,
            "outcome_id": self.outcome_id,
            "pathway_method": self.pathway_method,
            "material_signature": _thaw(self.material_signature),
        }
