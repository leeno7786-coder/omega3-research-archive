"""Operator-personal questions must reach the persona lane (live 2026-09-05).

"What is my favorite editor?" and "What is my dog's name?" scored only the
generic "what is" pattern, landed in KNOWLEDGE_EXPLANATION (persona lane weight
0.0, lobes 2/3), and Omega answered "the verified data does not contain that
information" about two persona records that were on disk and indexed. The
possessive "my" plus a preference / relation / name shape is a BACKGROUND_LOOKUP
(persona lane 1.5, lobe 1 first).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memory.recall_compiler import compile_recall_plan
from memory.recall_posture import select_recall_posture


@pytest.mark.parametrize(
    "query",
    [
        "What is my favorite editor?",
        "What is my dog's name?",
        "What is my name?",
        "Who is my dad?",
        "What's my wife's name?",
        "Do you remember my birthday?",
        "What do you know about me?",
        "Which text editor do I like best?",
    ],
)
def test_operator_personal_questions_select_background_lookup(query: str):
    decision = select_recall_posture(query.lower())
    assert decision.posture == "BACKGROUND_LOOKUP", decision
    assert decision.fallback_used is False
    assert decision.confidence > 0.34  # beats the generic "what is" score


@pytest.mark.parametrize(
    "query",
    ["What is my favorite editor?", "What is my dog's name?"],
)
def test_operator_personal_plan_runs_the_persona_lane_first(query: str):
    plan = compile_recall_plan(SimpleNamespace(text=query, limit=10))
    assert plan.posture.name == "BACKGROUND_LOOKUP"
    assert plan.lane_weights.get("persona", 0.0) > 0.0
    assert plan.lane_weights["persona"] >= max(
        w for name, w in plan.lane_weights.items() if name != "persona"
    )
    assert plan.coord_hypotheses[0][0] == 1  # lobe 1 (personas) leads


def test_exact_terms_drop_edge_punctuation_and_possessives():
    """The lanes score by substring term-overlap: ``dog's`` / ``name?`` never
    matched "Operator's dog: Biscuit" and still counted in the denominator."""
    plan = compile_recall_plan(SimpleNamespace(text="What is my dog's name?", limit=10))
    assert plan.exact_terms == ["what", "dog", "name"]
    assert not any(t.endswith("?") or t.endswith("'s") for t in plan.expanded_terms)
    plan = compile_recall_plan(SimpleNamespace(text="What is my favorite editor?", limit=10))
    assert "editor" in plan.exact_terms and "editor?" not in plan.exact_terms


@pytest.mark.parametrize(
    ("query", "posture"),
    [
        ("What is your name?", "IDENTITY_RESOLUTION"),
        ("who are you", "IDENTITY_RESOLUTION"),
        ("Explain photosynthesis", "KNOWLEDGE_EXPLANATION"),
        ("What is a mutex?", "KNOWLEDGE_EXPLANATION"),
    ],
)
def test_omega_identity_and_knowledge_questions_are_untouched(query: str, posture: str):
    assert select_recall_posture(query.lower()).posture == posture
