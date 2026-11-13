"""GlobalBSkillUpdate — Global B's integration of a skill outcome (6K).

Global B decides whether the qwen skill result satisfied the original Global intent, whether
the objective is complete, whether blockers remain, whether more work is needed, what
continuation signal(s) apply, whether to create continuation pressure, whether to close the
episode, and whether to ask the operator. Global B RECORDS these — it does NOT execute and
does NOT enqueue pressure itself (the continuation_pressure is a recorded intent the pressure
loop consumes). Additive + flag-gated, alongside the live GlobalOutcomePack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GLOBAL_B_SKILL_SCHEMA_VERSION = "omega.global_b_skill_update.v1"

CONTINUATION_SIGNALS = frozenset(
    {
        "verification_needed",
        "repair_needed",
        "missing_context",
        "requires_operator_input",
        "test_failed",
        "partial_completion",
        "build_in_progress",  # fold-back checkpoint — Omega decides continue|finish
        "blocked_by_safety",
        "skill_gap",
    }
)


@dataclass(frozen=True)
class GlobalBSkillUpdate:
    episode_id: str
    dispatch_id: str
    intent_satisfied: bool
    objective_complete: bool
    more_work_needed: bool
    unresolved_blockers: tuple = ()
    continuation_signals: tuple = ()
    continuation_pressure: dict | None = None
    close_episode: bool = False
    ask_operator: bool = False
    summary: str = ""
    type: str = "global_b_skill_update"
    schema_version: str = GLOBAL_B_SKILL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "dispatch_id": self.dispatch_id,
            "intent_satisfied": self.intent_satisfied,
            "objective_complete": self.objective_complete,
            "more_work_needed": self.more_work_needed,
            "unresolved_blockers": list(self.unresolved_blockers),
            "continuation_signals": list(self.continuation_signals),
            "continuation_pressure": dict(self.continuation_pressure)
            if self.continuation_pressure
            else None,
            "close_episode": self.close_episode,
            "ask_operator": self.ask_operator,
            "summary": self.summary,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.dispatch_id:
            errors.append("missing dispatch_id")
        if not self.episode_id:
            errors.append("missing episode_id")
        bad = set(self.continuation_signals) - CONTINUATION_SIGNALS
        if bad:
            errors.append(f"unknown continuation signals: {sorted(bad)}")
        if self.close_episode and self.more_work_needed:
            errors.append("cannot close_episode while more_work_needed")
        if self.intent_satisfied and self.unresolved_blockers:
            errors.append("intent_satisfied cannot hold with unresolved_blockers")
        if self.close_episode and not self.intent_satisfied:
            errors.append("close_episode requires intent_satisfied")
        return errors
