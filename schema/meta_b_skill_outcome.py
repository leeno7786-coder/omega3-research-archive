"""MetaBSkillOutcome — Meta B's classification of a qwen skill-execution result (6K).

The skill path is additive + flag-gated (OMEGA_USE_SKILL_ROUTING): a USE_SKILL episode's
QwenSkillExecutionResult is classified into exactly one of nine skill_* outcome kinds, sitting
ALONGSIDE the live USE_TOOL/APPLY MetaReflectionPack (which is left byte-identical). This is
the data contract; the classification logic lives in meta_loop/skill/foldback.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

META_B_SKILL_SCHEMA_VERSION = "omega.meta_b_skill_outcome.v1"

ALLOWED_META_B_SKILL_KINDS = frozenset(
    {
        "skill_success",
        "skill_partial",
        "skill_yielded",  # fold-back checkpoint — progress intact, Omega decides continue|finish
        "skill_failure",
        "skill_blocked",
        "skill_timeout",
        "skill_requires_confirmation",
        "skill_invalid_dispatch",
        "skill_malformed_result",
        "skill_bridge_unavailable",
    }
)

# QwenSkillExecutionResult.status (6J) -> Meta B skill outcome kind. Total + deterministic;
# an unknown status falls through to skill_malformed_result (fail closed) in the classifier.
STATUS_TO_META_B_KIND = {
    "success": "skill_success",
    "partial": "skill_partial",
    "yielded": "skill_yielded",
    "failure": "skill_failure",
    "blocked": "skill_blocked",
    "timeout": "skill_timeout",
    "requires_confirmation": "skill_requires_confirmation",
    "invalid_dispatch": "skill_invalid_dispatch",
    "malformed_result": "skill_malformed_result",
    "bridge_unavailable": "skill_bridge_unavailable",
}

# kinds that MUST carry a failure_class (Meta B rule: failure -> failure_class required)
FAILURE_KINDS = frozenset(
    {
        "skill_failure",
        "skill_timeout",
        "skill_invalid_dispatch",
        "skill_malformed_result",
        "skill_bridge_unavailable",
    }
)


@dataclass(frozen=True)
class MetaBSkillOutcome:
    episode_id: str
    dispatch_id: str
    skill_name: str
    skill_id: str
    execution_kind: str
    outcome_kind: str
    satisfied_dispatch: bool
    failure_class: str | None = None
    safety_blocker: str | None = None
    requires_operator_input: bool = False
    unresolved_blockers: tuple = ()
    summary: str = ""
    evidence: dict = field(default_factory=dict)
    guard_status: dict = field(default_factory=dict)
    confidence: float = 0.0
    type: str = "meta_b_skill_outcome"
    schema_version: str = META_B_SKILL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "dispatch_id": self.dispatch_id,
            "skill_name": self.skill_name,
            "skill_id": self.skill_id,
            "execution_kind": self.execution_kind,
            "outcome_kind": self.outcome_kind,
            "satisfied_dispatch": self.satisfied_dispatch,
            "failure_class": self.failure_class,
            "safety_blocker": self.safety_blocker,
            "requires_operator_input": self.requires_operator_input,
            "unresolved_blockers": list(self.unresolved_blockers),
            "summary": self.summary,
            "evidence": dict(self.evidence),
            "guard_status": dict(self.guard_status),
            "confidence": self.confidence,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.outcome_kind not in ALLOWED_META_B_SKILL_KINDS:
            errors.append(f"unknown outcome_kind: {self.outcome_kind!r}")
        if not self.dispatch_id:
            errors.append("missing dispatch_id")
        if not self.episode_id:
            errors.append("missing episode_id")
        if self.outcome_kind in FAILURE_KINDS and not self.failure_class:
            errors.append(f"{self.outcome_kind} requires a failure_class")
        if self.outcome_kind == "skill_blocked" and not self.safety_blocker:
            errors.append("skill_blocked must record a safety_blocker")
        if self.outcome_kind == "skill_requires_confirmation" and not self.requires_operator_input:
            errors.append("skill_requires_confirmation must set requires_operator_input")
        if self.outcome_kind == "skill_success" and not self.satisfied_dispatch:
            errors.append("skill_success must satisfy the dispatch")
        return errors
