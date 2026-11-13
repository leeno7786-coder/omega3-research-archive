"""SkillConsolidationRecord + CapabilitySignal — Omega/DeepSleep consolidation of a skill
episode (Phase 6L).

Omega records the full execution provenance (selected skill, what it did, the outcome, and
the capability reinforcement/weakening signal). The CapabilitySignal encodes WHICH learning
action the result earns, following the 6L rules — reinforce only on grounded success; pending
approval is neutral; blocked → safety memory; malformed → bridge reliability (NOT the skill);
forbidden-tool → weaken the skill; failure/timeout → failure memory. Additive + flag-gated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SKILL_CONSOLIDATION_SCHEMA_VERSION = "omega.skill_consolidation_record.v1"

CAPABILITY_SIGNALS = frozenset(
    {
        "reinforce",  # grounded success
        "reinforce_partial",  # partial success that materially advanced the objective
        "neutral",  # pending approval / ungrounded success — neither reward nor punish
        "safety_memory",  # blocked by safety — safety memory, not a capability failure
        "weaken_bridge",  # malformed/bridge result — weaken bridge reliability, NOT the skill
        "weaken_skill",  # forbidden-tool attempt — weaken skill/tool reliability
        "failure_memory",  # failure / timeout / invalid — failure memory (+ maybe repair pressure)
    }
)

# operator confirmation provenance
OPERATOR_CONFIRMATION_STATES = frozenset({"not_required", "pending", "confirmed"})


@dataclass(frozen=True)
class CapabilitySignal:
    signal: str
    skill_name: str
    skill_id: str
    delta: float
    target: str  # "skill_capability" | "bridge_reliability" | "safety" | "failure" | "none"
    reason: str = ""
    creates_repair_pressure: bool = False
    type: str = "capability_signal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "signal": self.signal,
            "skill_name": self.skill_name,
            "skill_id": self.skill_id,
            "delta": self.delta,
            "target": self.target,
            "reason": self.reason,
            "creates_repair_pressure": self.creates_repair_pressure,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.signal not in CAPABILITY_SIGNALS:
            errors.append(f"unknown capability signal: {self.signal!r}")
        if self.signal in ("reinforce", "reinforce_partial") and self.delta <= 0:
            errors.append("reinforce signals require a positive delta")
        if self.signal == "weaken_skill" and self.delta >= 0:
            errors.append("weaken_skill requires a negative delta")
        if self.signal == "weaken_bridge" and self.delta >= 0:
            errors.append("weaken_bridge requires a negative delta")
        # a malformed/bridge failure must never touch the skill's capability strength
        if self.signal == "weaken_bridge" and self.target == "skill_capability":
            errors.append("weaken_bridge must not target the skill capability")
        return errors


@dataclass(frozen=True)
class SkillConsolidationRecord:
    episode_id: str
    dispatch_id: str
    skill_name: str
    skill_id: str
    execution_kind: str
    objective: str
    outcome: str  # the Meta B skill outcome_kind
    tool_calls: tuple = ()
    files_changed: tuple = ()
    commands_run: tuple = ()
    tests_run: tuple = ()
    artifacts: tuple = ()
    failure_class: str | None = None
    unresolved_blockers: tuple = ()
    evidence: dict = field(default_factory=dict)
    operator_confirmation_status: str = "not_required"
    capability_signal: dict = field(default_factory=dict)
    workflow_memory_candidate: dict | None = None
    summary: str = ""
    type: str = "skill_consolidation_record"
    schema_version: str = SKILL_CONSOLIDATION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "dispatch_id": self.dispatch_id,
            "skill_name": self.skill_name,
            "skill_id": self.skill_id,
            "execution_kind": self.execution_kind,
            "objective": self.objective,
            "outcome": self.outcome,
            "tool_calls": [dict(t) if isinstance(t, dict) else t for t in self.tool_calls],
            "files_changed": list(self.files_changed),
            "commands_run": list(self.commands_run),
            "tests_run": list(self.tests_run),
            "artifacts": list(self.artifacts),
            "failure_class": self.failure_class,
            "unresolved_blockers": list(self.unresolved_blockers),
            "evidence": dict(self.evidence),
            "operator_confirmation_status": self.operator_confirmation_status,
            "capability_signal": dict(self.capability_signal),
            "workflow_memory_candidate": dict(self.workflow_memory_candidate)
            if self.workflow_memory_candidate
            else None,
            "summary": self.summary,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.dispatch_id:
            errors.append("missing dispatch_id")
        if not self.skill_id:
            errors.append("missing skill_id")
        if self.operator_confirmation_status not in OPERATOR_CONFIRMATION_STATES:
            errors.append(
                f"bad operator_confirmation_status: {self.operator_confirmation_status!r}"
            )
        return errors
