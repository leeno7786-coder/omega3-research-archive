"""Domain-aware tie-break among equal-scoring skills (6F follow-up).

When several skills share the needed kind and tie on the deterministic fit score, the selection
no longer falls straight to an alphabetical name sort (which let a narrow specialist like
auditing-security win a GENERIC objective just by sorting first). It now orders ties by:
  objective RELEVANCE (term overlap incl. the skill description) → GENERALITY (rarest-domain
  catalog frequency; least-specialized wins) → strength → name.
So a generic objective prefers a general engineering/planning skill, while an objective that
NAMES a specialty routes to that specialist.
"""

from __future__ import annotations

import pytest

from meta_loop.skill.run import select_skill_for_intent
from runtime.execution_skills import selectable_execution_skills
from runtime.qwen_skills import BUNDLED_SKILL_DIRS


@pytest.fixture(scope="module")
def catalog():
    return selectable_execution_skills(roots=[BUNDLED_SKILL_DIRS[0]], config={})


def test_generic_inspect_prefers_general_skill_over_narrow_specialist(catalog):
    # generic code-inspection objective: no skill's terms match it, so all the INSPECT-class skills
    # tie on score. The general engineering/planning skill wins over the security/creative
    # specialists that merely share the INSPECT kind and sort earlier alphabetically.
    d = select_skill_for_intent(
        objective="inspect this repo and find the dispatch schema", catalog=catalog
    )
    assert d.selected
    assert d.skill_name == "omega-planner-executor", d.skill_name
    assert d.skill_name not in {"auditing-security", "brainstorming", "model-feedback-loop"}


def test_specialist_wins_when_the_objective_names_its_specialty(catalog):
    # when the objective NAMES the specialty, the specialist's domain/description match SCORES it
    # higher (a genuine score win, not just a tie-break) — generality does not hold it back.
    d = select_skill_for_intent(
        objective="inspect this codebase for security vulnerabilities and owasp risks",
        catalog=catalog,
    )
    assert d.selected
    assert d.skill_name == "auditing-security", d.skill_name
    assert d.score > 0.845  # scored above the generic tie — the specialty was matched


def test_tie_break_is_deterministic(catalog):
    a = select_skill_for_intent(
        objective="inspect this repo and find the dispatch schema", catalog=catalog
    )
    b = select_skill_for_intent(
        objective="inspect this repo and find the dispatch schema", catalog=catalog
    )
    assert a.skill_name == b.skill_name and a.score == b.score


def test_recalled_specialist_success_cannot_capture_an_unrelated_generic_build(catalog):
    """Learned confidence refines fit; it cannot manufacture domain relevance."""
    decision = select_skill_for_intent(
        objective="build a todo app",
        catalog=catalog,
        recall_context={"skill_boosts": {"sql-database": 0.08}},
    )

    assert decision.selected
    assert decision.skill_name == "codex-grade-coding", decision.reason
