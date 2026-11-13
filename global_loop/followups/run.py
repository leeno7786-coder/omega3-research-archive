"""Global B phase — non-executable followups."""

from __future__ import annotations

from typing import List, Optional


def run(
    goal_state: str,
    failure_kind: Optional[str],
    action: str,
) -> List[dict]:
    from pressure_loop.policy import (
        global_b_followup_clarification_kinds,
        global_b_followup_rules,
        global_b_followup_tool_kinds,
    )

    gs = str(goal_state or "")
    fk = str(failure_kind or "").lower()
    rules = global_b_followup_rules()
    clarify_kinds = global_b_followup_clarification_kinds()
    tool_kinds = global_b_followup_tool_kinds()

    if gs == "completed":
        rule = rules.get("completed") or {}
        return [
            {
                "type": rule.get("type", "none"),
                "reason": rule.get("reason", ""),
                "suggested_intent": rule.get("suggested_intent"),
            }
        ]

    if gs == "invalid":
        rule = rules.get("invalid") or {}
        return [
            {
                "type": rule.get("type", "needs_clarification"),
                "reason": rule.get("reason", "packet inconsistency or missing trace"),
                "suggested_intent": rule.get("suggested_intent", "IDLE"),
            }
        ]

    if gs == "failed":
        if fk in clarify_kinds:
            rule = rules.get("failed_clarification") or {}
            return [
                {
                    "type": rule.get("type", "needs_clarification"),
                    "reason": fk or rule.get("reason", "execution failed"),
                    "suggested_intent": rule.get("suggested_intent", "RESPOND"),
                }
            ]
        if fk in tool_kinds:
            rule = rules.get("failed_default") or {}
            return [
                {
                    "type": rule.get("type", "needs_retry"),
                    "reason": fk or rule.get("reason", "tool failure"),
                    "suggested_intent": rule.get("suggested_intent", "USE_TOOL"),
                }
            ]
        rule = rules.get("failed_default") or {}
        return [
            {
                "type": rule.get("type", "needs_retry"),
                "reason": fk or rule.get("reason", "execution failed"),
                "suggested_intent": rule.get("suggested_intent", "USE_TOOL"),
            }
        ]

    if gs == "degraded":
        rule = rules.get("degraded") or {}
        by_action = rule.get("suggested_intent_from_action") or {}
        suggested = by_action.get(action) or by_action.get("default", "RESPOND")
        return [
            {
                "type": rule.get("type", "needs_retry"),
                "reason": rule.get("reason", "partial completion"),
                "suggested_intent": suggested,
            }
        ]

    if gs == "skipped":
        return [{"type": "none", "reason": "", "suggested_intent": None}]

    if gs == "idle":
        return [{"type": "none", "reason": "", "suggested_intent": None}]

    return [{"type": "none", "reason": "", "suggested_intent": None}]
