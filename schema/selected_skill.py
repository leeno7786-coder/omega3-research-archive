"""SelectedSkillDecision — Meta A's choice of the exact ExecutionSkill (Phase 6F).

Meta A selects; it never executes, never calls qwen, never writes a qwen prompt. The
decision carries everything Auto needs to build the bounded qwen dispatch (6G/6H):
allowed/forbidden tools, limits, evidence, completion criteria, output schema. A
failed selection carries a structured failure_reason (no silent drop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SELECTED_SKILL_SCHEMA_VERSION = "omega.selected_skill.v1"

ALLOWED_FAILURE_REASONS = frozenset(
    {"no_matching_skill", "unsafe_skill_required", "skill_catalog_unavailable"}
)


@dataclass(frozen=True)
class SelectedSkillDecision:
    selected: bool
    skill_name: str = ""
    skill_id: str = ""
    skill_class: str = ""
    execution_kind: str = ""
    source_path: str = ""  # where Auto loads the SKILL.md body from (6H)
    score: float = 0.0
    reason: str = ""
    matched_terms: tuple = ()
    safety_class: str = ""
    allowed_tools: tuple = ()
    forbidden_tools: tuple = ()
    requires_confirmation_for: tuple = ()
    limits: dict = field(default_factory=dict)
    evidence_required: tuple = ()
    completion_criteria: tuple = ()
    output_schema: dict = field(default_factory=dict)
    failure_reason: str | None = None
    schema_version: str = SELECTED_SKILL_SCHEMA_VERSION

    @classmethod
    def from_skill(
        cls,
        skill,
        *,
        score: float,
        execution_kind: str,
        reason: str,
        matched_terms: tuple = (),
    ) -> "SelectedSkillDecision":
        return cls(
            selected=True,
            skill_name=skill.name,
            skill_id=skill.skill_id,
            skill_class=skill.skill_class,
            execution_kind=execution_kind,
            source_path=skill.source_path,
            score=float(score),
            reason=reason,
            matched_terms=tuple(matched_terms),
            safety_class=skill.safety_class,
            allowed_tools=tuple(skill.allowed_tools),
            forbidden_tools=tuple(skill.forbidden_tools),
            requires_confirmation_for=tuple(skill.requires_confirmation_for),
            limits=dict(skill.limits),
            evidence_required=tuple(skill.evidence_required),
            completion_criteria=tuple(skill.completion_criteria),
            output_schema=dict(skill.output_schema),
        )

    @classmethod
    def failed(cls, failure_reason: str) -> "SelectedSkillDecision":
        return cls(selected=False, failure_reason=str(failure_reason))

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "skill_name": self.skill_name,
            "skill_id": self.skill_id,
            "skill_class": self.skill_class,
            "execution_kind": self.execution_kind,
            "source_path": self.source_path,
            "score": self.score,
            "reason": self.reason,
            "matched_terms": list(self.matched_terms),
            "safety_class": self.safety_class,
            "allowed_tools": list(self.allowed_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "requires_confirmation_for": list(self.requires_confirmation_for),
            "limits": dict(self.limits),
            "evidence_required": list(self.evidence_required),
            "completion_criteria": list(self.completion_criteria),
            "output_schema": dict(self.output_schema),
            "failure_reason": self.failure_reason,
            "schema_version": self.schema_version,
        }
