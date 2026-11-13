"""Global B reads goal_state/ground/summarize/followups config via policy."""

from __future__ import annotations

from pressure_loop.policy import (
    clear_policy_cache,
    global_b_followup_clarification_kinds,
    global_b_summarize_completed_by_action,
)
from global_loop.global_b import followups_for, ground, summary_for


def test_global_b_summarize_templates_from_config():
    clear_policy_cache()
    completed = global_b_summarize_completed_by_action()
    assert summary_for("completed", "USE_TOOL", None) == completed["USE_TOOL"]


def test_global_b_ground_paths_from_config():
    clear_policy_cache()
    auto = {"auto": {"outputs": {"tool_result": {"x": 1}}}}
    meta = {"execution": {"disposition": "completed"}}
    grounded, basis = ground(auto, meta)
    assert grounded is True
    assert "auto.outputs.tool_result" in basis


def test_global_b_followup_clarification_kinds_from_config():
    clear_policy_cache()
    kinds = global_b_followup_clarification_kinds()
    fu = followups_for("failed", "missing_params", "USE_TOOL")
    assert fu[0]["type"] == "needs_clarification"
    assert "missing_params" in kinds
