"""Global B phase — deterministic summary template."""

from __future__ import annotations

from typing import Optional


def run(goal_state: str, action: str, failure_kind: Optional[str]) -> str:
    from pressure_loop.policy import (
        global_b_summarize_completed_by_action,
        global_b_summarize_template,
    )

    gs = str(goal_state or "")
    act = str(action or "")
    completed = global_b_summarize_completed_by_action()
    if gs == "completed":
        if act in completed:
            return completed[act]
        return completed.get("default", "Execution completed successfully.")
    if gs == "failed":
        fk = failure_kind or "unknown"
        template = global_b_summarize_template("failed_template")
        return template.format(failure_kind=fk)
    if gs == "degraded":
        return global_b_summarize_template("degraded")
    if gs == "skipped":
        return global_b_summarize_template("skipped")
    if gs == "idle":
        return global_b_summarize_template("idle")
    if gs == "invalid":
        return global_b_summarize_template("invalid")
    return global_b_summarize_template(
        "invalid",
        default="Outcome invalid due to packet inconsistency or missing trace.",
    )
