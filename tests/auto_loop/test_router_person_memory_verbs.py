"""Person-memory recall must not hinge on which synonym the operator used.

Live finding 2026-07-30: "Omega, what do you remember about Noah?" recalled 8
persona records; "Omega, what do you recall about Noah?" recalled zero. The only
difference was the verb. ``_PERSON_MEMORY_QUERY_RE`` listed ``remember|know``,
so "recall" missed every persona tier and fell through to the knowledge
catch-all (lobe 2), where the operator's persona records do not live.

The recall planner read both turns correctly (``intent=identity
preferred=[1,2,4]``) — only the tool router disagreed, and the router picks the
lane. These pin the verb set so a synonym can never again decide whether a
person is remembered.
"""

from __future__ import annotations

import pytest

from auto_loop.mril_tools.router import route_memory_tool


def _entry(query: str) -> dict:
    return route_memory_tool(query)


PERSONA_LOBE = 1


@pytest.mark.parametrize(
    "verb",
    ["remember", "know", "recall", "recollect"],
)
def test_person_memory_query_routes_to_persona_for_every_verb(verb: str):
    entry = _entry(f"Omega, what do you {verb} about Noah?")
    assert entry["lobe"] == PERSONA_LOBE, f"{verb!r} misrouted to lobe {entry['lobe']}"
    assert entry["tool"] == "remember_persona"


@pytest.mark.parametrize("verb", ["remember", "know", "recall", "recollect"])
def test_tell_me_everything_you_verb_about_person_routes_to_persona(verb: str):
    entry = _entry(f"tell me everything you {verb} about Mitchell Ray")
    assert entry["lobe"] == PERSONA_LOBE, f"{verb!r} misrouted to lobe {entry['lobe']}"
    assert entry["tool"] == "remember_persona"


def test_the_exact_live_query_that_returned_zero_hits():
    """The verbatim turn from the live bust."""
    entry = _entry("Omega, what do you recall about Noah?")
    assert entry["tool"] == "remember_persona"
    assert entry["lobe"] == PERSONA_LOBE


def test_person_memory_recall_still_spans_the_whole_persona_lobe():
    """Identity/persona grounding must not be narrowed to one subdomain.

    ``_tool_entry(..., scope_subdomain=False)`` omits the ``subdomain`` key
    entirely, which is what lets a person query hit every subdirectory of
    lobe 1. A subdomain-scoped persona recall would drop records by a wording
    heuristic — the widened verb set must not quietly reintroduce that.
    """
    entry = _entry("Omega, what do you recall about Noah?")
    assert "subdomain" not in entry
    assert entry["allowed_lobes"] == [PERSONA_LOBE]


def test_knowledge_questions_are_not_captured_by_the_verb_widening():
    """Widening the verb list must not steal genuine knowledge queries."""
    assert _entry("what do you know about quantum entanglement")["lobe"] != PERSONA_LOBE
