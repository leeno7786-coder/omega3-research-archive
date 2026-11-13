"""A "build a script" objective must route to a WRITE skill, not a read_only planner.

Operator live finding 2026-06-28: "Build a small Python script at fib.py ... then run it" was
classified INSPECT — the WRITE markers matched 'build the' but not 'build a' — so a read_only
planning skill (omega-planner-executor, max_files_changed=0) was selected for a BUILD task. qwen's
real fib.py write then tripped skill_files_changed_exceeded (1 file > 0 limit), failing a build
whose work actually succeeded. Secondary: files_changed counted write OPERATIONS, not unique
files, so re-editing one file could also breach the limit.
"""

from __future__ import annotations

import pytest

from meta_loop.skill.run import _WRITE_KINDS, _needed_kind, select_skill_for_intent
from schema.execution_skill import ExecutionSkill
from schema.qwen_skill_result import _files_from_ledger


def _mk(name, *, skill_class, execution_kinds, safety_class, allowed_tools, max_files_changed=10):
    return ExecutionSkill(
        name=name,
        skill_id=f"5.01.qwenskill-{name}",
        kind="execution_skill",
        version=1,
        enabled=True,
        description=name,
        skill_class=skill_class,
        intents=("USE_SKILL",),
        execution_kinds=tuple(execution_kinds),
        domains=("python",),
        allowed_tools=tuple(allowed_tools),
        forbidden_tools=(),
        safety_class=safety_class,
        requires_confirmation_for=(),
        limits={"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": max_files_changed},
        evidence_required=("summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="body",
        source_path="syn",
    )


@pytest.mark.parametrize(
    "objective",
    [
        "Build a small Python script at fib.py that prints the first 15 Fibonacci numbers, then run it to verify.",
        "build a python script",
        "create a new parser module",
        "make a helper function for parsing",
        "write a script that prints hello",
        "generate a config loader",
        "scaffold a new package",
        # build objectives that ALSO name tests are still WRITE — the deliverable is the code, the
        # tests are part of it (operator live finding 2026-07-01: this routed to TEST/testing-strategy
        # and the whole build failed the TEST evidence gate on empty tests_run).
        "Build a small Python todo app: create main.py and test_main.py with pytest tests. Write the files.",
        "create a parser module and write unit tests for it",
        "implement a config loader with a pytest test suite",
        # "rebuild" is a build verb too — whole-word matching means it never hit "build" (operator
        # live finding 2026-07-02: this exact objective routed to TEST/testing-strategy, whose
        # evidence gate then blocked the completion notice on a build that succeeded on disk).
        "Rebuild the csv_stats project from scratch in the workspace: delete the old files first, "
        "then csv_stats.py (row count, column count, per-column empty-cell counts), a pytest test "
        "file with at least 4 tests, and a README with usage examples. Run the tests and confirm "
        "they all pass.",
        "rebuild the parser module and run the tests",
    ],
)
def test_build_verbs_route_to_write(objective):
    assert _needed_kind(objective) in _WRITE_KINDS, f"{objective!r} -> {_needed_kind(objective)}"


def test_run_tests_still_routes_to_test_when_no_build_verb():
    # the override is narrow: only a BUILD verb flips a TEST-noun match. A pure "run tests" ask
    # (no build verb) must still route to TEST.
    assert _needed_kind("run the pytest suite") == "TEST"
    assert _needed_kind("run the tests and report failures") == "TEST"


@pytest.mark.parametrize(
    "objective,kind",
    [
        ("inspect this repo and find the schema", "INSPECT"),
        ("review this code for quality", "REVIEW"),
        ("run the tests", "TEST"),
        ("plan the architecture", "PLAN"),
        ("fix this bug in the parser", "REPAIR"),
    ],
)
def test_non_build_objectives_unchanged(objective, kind):
    assert _needed_kind(objective) == kind


def test_build_task_selects_write_skill_not_read_only():
    # The exact live failure: a read_only planner (max_files_changed=0) vs a write skill. Before the
    # fix the build objective is INSPECT, so the read_only planner is NOT filtered and (being the
    # only INSPECT-capable one) wins. After: the build is WRITE, the planner is filtered, python wins.
    ro = _mk(
        "planner",
        skill_class="planning_skill",
        execution_kinds=("INSPECT",),
        safety_class="read_only",
        allowed_tools=("read_files",),
        max_files_changed=0,
    )
    wr = _mk(
        "python",
        skill_class="artifact_skill",
        execution_kinds=("WRITE", "TEST"),
        safety_class="write_safe",
        allowed_tools=("read_files", "write_file", "run_tests"),
        max_files_changed=60,
    )
    d = select_skill_for_intent(
        objective="Build a small Python script at fib.py that prints fibonacci, then run it",
        catalog=[ro, wr],
    )
    assert d.selected and d.skill_name == "python"
    assert d.safety_class == "write_safe"
    assert int((d.limits or {}).get("max_files_changed", 0)) > 0


def test_files_changed_counts_unique_files_not_write_operations():
    # qwen edits one file many times -> touched_files carries many entries; the count that the
    # max_files_changed guard sees must be UNIQUE files, else a legit 1-file build can breach it.
    raw = {"ledger": {"touched_files": [{"path": "fib.py"}] * 5 + [{"path": "test_fib.py"}]}}
    assert _files_from_ledger(raw) == ["fib.py", "test_fib.py"]
