"""Operator-personal questions route to remember_persona (lobe 1).

Live 2026-09-05: "What is my favorite editor?" and "What is my dog's name?"
fell through every router branch to the knowledge catch-all (lobe 2). The
forced recall then ran with allowed_lobes=[2], so the lobe-1 persona records
holding both facts were unreachable and Omega replied "the verified data does
not contain that information" about facts it held.
"""

from __future__ import annotations

import pytest

from auto_loop.mril_tools.router import route_memory_tool


@pytest.mark.parametrize(
    "query",
    [
        "What is my favorite editor?",
        "What is my favourite colour?",
        "What is my dog's name?",
        "What's my wife's name?",
        "What is my name?",
        "When is my birthday?",
        "Do you remember my preferred shell?",
        "What are my kids called?",
        "Which text editor do I like best?",
        "What kind of music do I prefer?",
    ],
)
def test_operator_personal_questions_route_to_persona(query: str):
    entry = route_memory_tool(query)
    assert entry["tool"] == "remember_persona", entry
    assert entry["allowed_lobes"] == [1]


@pytest.mark.parametrize(
    ("query", "tool"),
    [
        ("What is my occupation?", "remember_persona"),  # existing branch, untouched
        ("What are my goals?", "remember_reasoning"),
        ("What is my trajectory?", "remember_time"),
        ("How do I configure vim?", "remember_procedure"),
        ("what is the roadmap", "remember_dev"),
        ("Do you remember what I built yesterday?", "remember_dev"),
        ("What did we make last week?", "remember_dev"),
        ("Do you remember what was in the directory?", "remember_dev"),
        ("What did I ask you about earlier?", "remember_time"),
        ("What were we discussing?", "remember_time"),
        ("who am i", "remember_identity"),
        ("What do you know about rust?", "remember_knowledge"),
    ],
)
def test_other_branches_keep_their_precedence(query: str, tool: str):
    assert route_memory_tool(query)["tool"] == tool


@pytest.mark.parametrize(
    ("query", "subdomain"),
    [
    ("show me the full tree for the project", 5),
    ("which files are in the project?", 5),
        ("what strings did we read from the repo?", 6),
        ("what files did we touch?", 7),
        ("what projects and drives did we inspect?", 4),
    ],
)
def test_dev_observation_views_are_typed(query: str, subdomain: int):
    entry = route_memory_tool(query)
    assert entry["tool"] == "remember_dev"
    assert entry["allowed_lobes"] == [11]
    assert entry["allowed_subdomains"] == [subdomain]
