"""Slice 4 B4 — Global B compat status, grounding, summary, followups."""

from __future__ import annotations

import pytest

from global_loop.global_b import (
    FOLLOWUP_TYPES,
    GLOBAL_INTENTS,
    followups_for,
    ground,
    summary_for,
)


def test_ground_lists_only_present_fields():
    auto = {
        "auto": {
            "outputs": {"tool_result": {"x": 1}},
        }
    }
    meta = {"execution": {"disposition": "completed"}}
    grounded, basis = ground(auto, meta)
    assert grounded is True
    assert basis == ["auto.outputs.tool_result", "meta.execution.disposition"]


def test_ground_no_fabrication_when_outputs_empty():
    auto = {"auto": {"outputs": {}}}
    meta = {"execution": {"disposition": "failed"}}
    grounded, basis = ground(auto, meta)
    assert basis == ["meta.execution.disposition"]
    assert grounded is True


@pytest.mark.parametrize(
    "compat_status,action,expected",
    [
        ("completed", "USE_TOOL", "Tool execution completed successfully."),
        ("completed", "RESPONSE", "Response generated successfully."),
        ("completed", "PROACTIVE_RESPONSE", "Proactive response generated successfully."),
        ("completed", "IMAGE_CREATION", "Image creation completed successfully."),
        ("failed", "USE_TOOL", "Execution failed: tool_error."),
        ("degraded", "USE_TOOL", "Execution partially completed with degradation."),
        ("skipped", "IDLE", "No execution was performed."),
        ("idle", "IDLE", "Chose idle with a recorded reason."),
        ("invalid", "USE_TOOL", "Outcome invalid due to packet inconsistency or missing trace."),
    ],
)
def test_summary_templates(compat_status, action, expected):
    fk = "tool_error" if compat_status == "failed" else None
    assert summary_for(compat_status, action, fk) == expected


def test_followups_completed_none():
    fu = followups_for("completed", None, "USE_TOOL")
    assert fu == [{"type": "none", "reason": "", "suggested_intent": None}]
    assert fu[0]["type"] in FOLLOWUP_TYPES


def test_followups_invalid_needs_clarification():
    fu = followups_for("invalid", None, "USE_TOOL")
    assert fu[0]["type"] == "needs_clarification"
    assert fu[0]["suggested_intent"] in GLOBAL_INTENTS


def test_followups_failed_clarification():
    fu = followups_for("failed", "missing_params", "USE_TOOL")
    assert fu[0]["type"] == "needs_clarification"
    assert fu[0]["suggested_intent"] == "RESPOND"


def test_followups_failed_retry():
    fu = followups_for("failed", "tool_error", "USE_TOOL")
    assert fu[0]["type"] == "needs_retry"
    assert fu[0]["suggested_intent"] == "USE_TOOL"
