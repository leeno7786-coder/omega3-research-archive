from __future__ import annotations

from auto_loop.mril_tools.router import route_memory_tool
from memory.recall_aliases import expand_recall_terms
from memory.recall_posture import select_recall_posture


def test_spouse_vocabulary_expands_across_relationship_and_marital_state() -> None:
    terms = set(expand_recall_terms("Does Mitchell have a spouse?"))

    assert {"spouse", "wife", "husband", "married"} <= terms


def test_child_vocabulary_expands_across_collective_and_gendered_terms() -> None:
    terms = set(expand_recall_terms("Does Mitchell have children?"))

    assert {"child", "children", "kid", "daughter", "son"} <= terms


def test_record_vocabulary_expands_back_to_query_vocabulary() -> None:
    married_terms = set(expand_recall_terms("Mitchell Ray is married."))
    daughter_terms = set(expand_recall_terms("Mitchell Ray has daughters."))

    assert "spouse" in married_terms
    assert "children" in daughter_terms


def test_relationship_questions_open_persona_recall() -> None:
    for query in ("Who has a spouse?", "Who has children?"):
        decision = select_recall_posture(query)
        route = route_memory_tool(query)

        assert decision.posture == "ROLE_BINDING"
        assert route["lobe"] == 1
        assert "subdomain" not in route
