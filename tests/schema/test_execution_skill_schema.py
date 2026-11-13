"""Phase 6B — execution-skill frontmatter schema. Covers all 14 validation rules."""

from __future__ import annotations

import pytest

from schema.execution_skill import (
    ExecutionSkillSchemaError,
    is_selectable,
    parse_execution_skill_frontmatter,
    validate_execution_skill_frontmatter,
)


def _valid(**over) -> dict:
    base = {
        "name": "codex-grade-coding",
        "kind": "execution_skill",
        "version": 1,
        "enabled": True,
        "description": "Repair code under guard.",
        "skill_class": "repair_skill",
        "intents": ["USE_SKILL"],
        "execution_kinds": ["INSPECT", "PATCH", "TEST", "VERIFY", "REPAIR"],
        "domains": ["python"],
        "allowed_tools": ["read_files", "patch_file", "run_tests"],
        "forbidden_tools": ["run_command"],
        "safety_class": "write_safe",
        "requires_confirmation_for": [],
        "max_steps": 20,
        "max_duration_ms": 600000,
        "max_files_changed": 10,
        "evidence_required": ["diff_summary", "tests_run"],
        "completion_criteria": ["tests_pass"],
        "failure_classes": ["test_failure", "patch_conflict"],
        "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
    }
    base.update(over)
    return base


def _valid_read_only(**over) -> dict:
    ro = {
        "skill_class": "inspection_skill",
        "execution_kinds": ["INSPECT", "SEARCH", "REPORT"],
        "allowed_tools": ["read_files", "search_repo"],
        "forbidden_tools": ["write_file", "patch_file", "run_command"],
        "safety_class": "read_only",
        "max_files_changed": 0,
    }
    ro.update(over)  # caller overrides win (no duplicate-kwarg collision)
    return _valid(**ro)


# ── acceptance: valid passes ──────────────────────────────────────────────────
def test_valid_frontmatter_passes():
    assert validate_execution_skill_frontmatter(_valid()) == []
    assert parse_execution_skill_frontmatter(_valid())["name"] == "codex-grade-coding"


def test_valid_read_only_inspection_passes():
    assert validate_execution_skill_frontmatter(_valid_read_only()) == []


# ── rule 1: missing required field ────────────────────────────────────────────
@pytest.mark.parametrize(
    "field", ["name", "kind", "safety_class", "max_steps", "output_schema", "completion_criteria"]
)
def test_rule1_missing_required_field_fails(field):
    d = _valid()
    del d[field]
    errs = validate_execution_skill_frontmatter(d)
    assert any(field in e for e in errs)


# ── rule 2-5: enum validity ───────────────────────────────────────────────────
def test_rule2_unknown_kind_fails():
    assert any(
        "kind" in e for e in validate_execution_skill_frontmatter(_valid(kind="prompt_skill"))
    )


def test_rule3_unknown_skill_class_fails():
    assert any(
        "skill_class" in e
        for e in validate_execution_skill_frontmatter(_valid(skill_class="wizard_skill"))
    )


def test_rule4_unknown_execution_kind_fails():
    assert any(
        "execution_kind" in e
        for e in validate_execution_skill_frontmatter(
            _valid(execution_kinds=["INSPECT", "TELEPORT"])
        )
    )


def test_rule5_unknown_safety_class_fails():
    assert any(
        "safety_class" in e
        for e in validate_execution_skill_frontmatter(_valid(safety_class="yolo"))
    )


# ── rule 6: empty allowed_tools ───────────────────────────────────────────────
def test_rule6_empty_allowed_tools_fails_for_normal_skill():
    assert any(
        "allowed_tools is empty" in e
        for e in validate_execution_skill_frontmatter(_valid(allowed_tools=[]))
    )


def test_rule6_empty_allowed_tools_ok_for_readonly_planning_skill():
    d = _valid(
        skill_class="planning_skill",
        execution_kinds=["PLAN", "INSPECT", "REPORT"],
        allowed_tools=[],
        safety_class="read_only",
        max_files_changed=0,
    )
    assert validate_execution_skill_frontmatter(d) == []


def test_rule6_empty_allowed_tools_ok_for_readonly_review_skill():
    d = _valid(
        skill_class="review_skill",
        execution_kinds=["REVIEW", "INSPECT", "REPORT"],
        allowed_tools=[],
        safety_class="read_only",
        max_files_changed=0,
    )
    assert validate_execution_skill_frontmatter(d) == []


# ── rule 7: write tools with read_only safety (write/read mismatch) ───────────
def test_rule7_write_tool_with_readonly_fails():
    d = _valid_read_only(allowed_tools=["read_files", "patch_file"])
    assert any("write tools" in e for e in validate_execution_skill_frontmatter(d))


def test_rule7_write_execution_kind_with_readonly_fails():
    d = _valid_read_only(execution_kinds=["INSPECT", "PATCH"])
    assert any(
        "world-mutating execution_kinds" in e for e in validate_execution_skill_frontmatter(d)
    )


# ── rule 8: destructive without confirmation ─────────────────────────────────
def test_rule8_destructive_without_confirmation_fails():
    d = _valid(
        safety_class="destructive", allowed_tools=["run_command"], requires_confirmation_for=[]
    )
    assert any("requires_confirmation_for" in e for e in validate_execution_skill_frontmatter(d))


def test_rule8_destructive_with_confirmation_passes():
    d = _valid(
        skill_class="devops_skill",
        safety_class="destructive",
        execution_kinds=["RUN"],
        allowed_tools=["run_command"],
        requires_confirmation_for=["command"],
        max_files_changed=0,
    )
    assert validate_execution_skill_frontmatter(d) == []


# ── rule 9: self_modification denied by default ───────────────────────────────
def test_rule9_self_modification_denied_by_default():
    d = _valid(safety_class="self_modification")
    assert any("self_modification" in e for e in validate_execution_skill_frontmatter(d))


def test_rule9_self_modification_allowed_with_policy_flag():
    d = _valid(safety_class="self_modification")
    errs = validate_execution_skill_frontmatter(d, allow_self_modification=True)
    assert not any("self_modification" in e for e in errs)


# ── rule 10: disabled skill loads but is not selectable ───────────────────────
def test_rule10_disabled_skill_is_valid_but_not_selectable():
    d = _valid(enabled=False)
    assert validate_execution_skill_frontmatter(d) == []  # valid (loads)
    assert is_selectable(d) is False  # but not selectable
    assert is_selectable(_valid(enabled=True)) is True


def test_rule10_invalid_enabled_skill_is_not_selectable():
    assert is_selectable(_valid(enabled=True, safety_class="yolo")) is False


# ── rule 11: malformed output_schema ──────────────────────────────────────────
def test_rule11_non_dict_output_schema_fails():
    assert any(
        "output_schema" in e
        for e in validate_execution_skill_frontmatter(_valid(output_schema="a string"))
    )


def test_rule11_malformed_json_schema_fails():
    # type must be a string/array of strings; an integer is a malformed JSON Schema
    assert any(
        "output_schema" in e
        for e in validate_execution_skill_frontmatter(_valid(output_schema={"type": 123}))
    )


# ── rule 12/13: positive limits ───────────────────────────────────────────────
@pytest.mark.parametrize("bad", [0, -1, "20", 1.5, True])
def test_rule12_max_steps_must_be_positive_int(bad):
    assert any(
        "max_steps" in e for e in validate_execution_skill_frontmatter(_valid(max_steps=bad))
    )


@pytest.mark.parametrize("bad", [0, -1, "x"])
def test_rule13_max_duration_must_be_positive_int(bad):
    assert any(
        "max_duration_ms" in e
        for e in validate_execution_skill_frontmatter(_valid(max_duration_ms=bad))
    )


# ── rule 14: read_only => max_files_changed == 0 ──────────────────────────────
def test_rule14_readonly_nonzero_files_changed_fails():
    d = _valid_read_only(max_files_changed=3)
    assert any("max_files_changed" in e for e in validate_execution_skill_frontmatter(d))


def test_parse_raises_on_invalid():
    with pytest.raises(ExecutionSkillSchemaError):
        parse_execution_skill_frontmatter(_valid(safety_class="yolo"))
