"""Phase 6F — Meta A selects the exact ExecutionSkill (deterministic scoring)."""

from __future__ import annotations

import pytest

from meta_loop.skill.run import select_skill_for_intent
from runtime.execution_skills import selectable_execution_skills
from runtime.qwen_skills import BUNDLED_SKILL_DIRS
from schema.execution_skill import ExecutionSkill
from schema.selected_skill import SelectedSkillDecision

_BUNDLED = BUNDLED_SKILL_DIRS[0]


@pytest.fixture(scope="module")
def catalog():
    return selectable_execution_skills(roots=[_BUNDLED], config={})


def _mk(
    name="syn",
    *,
    skill_class="repair_skill",
    execution_kinds=("REPAIR",),
    safety_class="write_safe",
    enabled=True,
    allowed_tools=("read_files", "patch_file"),
) -> ExecutionSkill:
    return ExecutionSkill(
        name=name,
        skill_id=f"5.01.qwenskill-{name}",
        kind="execution_skill",
        version=1,
        enabled=enabled,
        description=name,
        skill_class=skill_class,
        intents=("USE_SKILL",),
        execution_kinds=tuple(execution_kinds),
        domains=("python",),
        allowed_tools=tuple(allowed_tools),
        forbidden_tools=(),
        safety_class=safety_class,
        requires_confirmation_for=(),
        limits={"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": 10},
        evidence_required=("summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="body",
        source_path="syn",
    )


# ── routing examples (work-order acceptance) ──────────────────────────────────
@pytest.mark.parametrize(
    "objective,expected_names,expected_kind",
    [
        # Several skills share the needed kind and tie on score; the domain-aware tie-break
        # (objective relevance → GENERALITY) resolves a GENERIC objective to the general
        # engineering/planning skill, NOT a narrow specialist (auditing-security) that merely
        # shares the kind. The specialty-invoking counterpart lives in
        # tests/core/test_skill_selection_domain_aware.py.
        ("inspect this repo and find the dispatch schema", {"omega-planner-executor"}, "INSPECT"),
        ("fix this bug in the parser", {"debugging-methodology", "codex-grade-coding"}, "REPAIR"),
        ("make the smallest safe refactor", {"omega-refactor-surgeon"}, "REFACTOR"),
        ("run the tests and report", {"testing-strategy", "test-driven-development"}, "TEST"),
        ("review this code for quality", {"code-review-excellence"}, "REVIEW"),
        (
            "modify a skill in the skill system",
            {"skill-system-maintainer", "git-conventional-commits"},
            "MAINTAIN",
        ),
    ],
)
def test_routing_selects_expected_skill(catalog, objective, expected_names, expected_kind):
    d = select_skill_for_intent(objective=objective, catalog=catalog)
    assert d.selected, d.failure_reason
    assert d.skill_name in expected_names, f"{objective!r} -> {d.skill_name}"
    assert d.execution_kind == expected_kind


# ── the decision carries the full contract Auto needs ─────────────────────────
def test_selected_decision_carries_contract(catalog):
    d = select_skill_for_intent(objective="fix this bug", catalog=catalog)
    assert d.selected
    assert d.allowed_tools and d.forbidden_tools is not None
    assert set(d.limits) == {"max_steps", "max_duration_ms", "max_files_changed"}
    assert d.evidence_required and d.completion_criteria and d.output_schema
    assert d.skill_id.startswith("5.01.qwenskill-") and d.safety_class


# ── failure modes (no silent drop) ────────────────────────────────────────────
def test_empty_catalog_fails_catalog_unavailable():
    d = select_skill_for_intent(objective="fix this", catalog=[])
    assert not d.selected and d.failure_reason == "skill_catalog_unavailable"


def test_only_disabled_skills_fails_no_matching_skill():
    d = select_skill_for_intent(objective="fix this bug", catalog=[_mk("disabled", enabled=False)])
    assert not d.selected and d.failure_reason == "no_matching_skill"


def test_only_unsafe_capable_skill_fails_unsafe_required():
    # a self_modification skill that COULD do the task is blocked without policy
    selfmod = _mk(
        "self-mod",
        skill_class="maintenance_skill",
        execution_kinds=("REPAIR",),
        safety_class="self_modification",
    )
    d = select_skill_for_intent(objective="fix this bug", catalog=[selfmod])
    assert not d.selected and d.failure_reason == "unsafe_skill_required"
    # ...but allowed when policy authorizes self-modification
    d2 = select_skill_for_intent(
        objective="fix this bug", catalog=[selfmod], allow_self_modification=True
    )
    assert d2.selected and d2.skill_name == "self-mod"


def test_read_only_skill_not_selected_for_write_task():
    ro = _mk(
        "ro-inspect",
        skill_class="inspection_skill",
        execution_kinds=("INSPECT",),
        safety_class="read_only",
        allowed_tools=("read_files",),
    )
    wr = _mk(
        "codex-grade-coding",
        skill_class="repair_skill",
        execution_kinds=("REPAIR", "PATCH"),
        safety_class="write_safe",
    )
    d = select_skill_for_intent(objective="fix this bug", catalog=[ro, wr])
    assert d.selected and d.skill_name == "codex-grade-coding"


# ── deterministic tie-break ───────────────────────────────────────────────────
def test_selection_is_deterministic(catalog):
    a = select_skill_for_intent(objective="inspect this repo", catalog=catalog)
    b = select_skill_for_intent(objective="inspect this repo", catalog=catalog)
    assert a.skill_id == b.skill_id and a.score == b.score


def test_tie_break_prefers_name_then_id():
    # two equally-scoring write skills with the same class/kinds -> name ascending wins
    s1 = _mk("zeta-fixer", skill_class="repair_skill", execution_kinds=("REPAIR",))
    s2 = _mk("alpha-fixer", skill_class="repair_skill", execution_kinds=("REPAIR",))
    d = select_skill_for_intent(objective="fix this bug", catalog=[s1, s2])
    assert d.skill_name == "alpha-fixer"


def test_tie_break_prefers_higher_capability_strength():
    s1 = _mk("zeta-fixer", skill_class="repair_skill", execution_kinds=("REPAIR",))
    s2 = _mk("alpha-fixer", skill_class="repair_skill", execution_kinds=("REPAIR",))
    strengths = {s1.skill_id: 0.2, s2.skill_id: 0.95}
    d = select_skill_for_intent(objective="fix this bug", catalog=[s1, s2], strengths=strengths)
    assert d.skill_name == "alpha-fixer"


# ── Meta selects, never executes ──────────────────────────────────────────────
def test_selection_returns_a_decision_not_an_execution(catalog):
    d = select_skill_for_intent(objective="fix this bug", catalog=catalog)
    assert isinstance(d, SelectedSkillDecision)
    # the decision has NO execution/result/tool_call fields — selection only
    keys = set(d.to_dict())
    assert not (keys & {"result", "stdout", "tool_calls", "executed", "qwen_result"})
