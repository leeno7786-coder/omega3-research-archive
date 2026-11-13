"""Slice 1 B3 — Global A intent router."""

from __future__ import annotations

import pytest

from pressure_loop.contributors import (
    ALLOWED_INTENTS,
    EpisodeIgnition,
    from_operator_message,
    infer_operator_command_kind,
    make_contributor,
)
from tests.core.rebuild_helpers import form_intent


def _ignition(opened_by: str, **meta):
    c = make_contributor("operator", opened_by, 1.0, meta)
    return EpisodeIgnition(
        episode_id="ep-test",
        aggregate_pressure=0.9,
        opened_by=opened_by,
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


def test_operator_query_respond():
    g = form_intent(_ignition("operator_query", message="What is Python?"))
    assert g.intent == "RESPOND"


def test_operator_response_resolve():
    g = form_intent(_ignition("operator_response", message="yes that one"))
    assert g.intent == "RESOLVE_QUERY"


def test_learning_debt_learn():
    g = form_intent(_ignition("learning_debt", domain="python"))
    assert g.intent == "LEARN"


def test_proactive_update_maps_to_proactive_response():
    g = form_intent(_ignition("proactive_update", message="Progress update on learning."))
    assert g.intent == "PROACTIVE_RESPONSE"


def test_operator_command_learn_python():
    g = form_intent(_ignition("operator_command", message="Learn Python"))
    assert g.intent == "LEARN"
    assert not hasattr(g, "target")


@pytest.mark.parametrize(
    ("message", "command_kind", "expected_intent"),
    [
        ("Please learn durable queue semantics", "learn", "LEARN"),
        ("Learn how to build a FastAPI service", "learn", "LEARN"),
        ("Build a tutorial that helps me learn Python", None, "RESPOND"),
        ("Write a script that prints system health", None, "RESPOND"),
        ("Create an app for tracking tasks", None, "RESPOND"),
        ("Read this file and summarize it", None, "RESPOND"),
        ("Inspect the existing build", None, "RESPOND"),
        ("Review the attached document", None, "RESPOND"),
        ("Open the project configuration", None, "RESPOND"),
        ("Edit the launcher script", None, "RESPOND"),
        ("Search the workspace for stale routes", None, "RESPOND"),
        ("Analyze this log file", None, "RESPOND"),
        ("Summarize the attached report", None, "RESPOND"),
        ("Install the required dependency", None, "RESPOND"),
        ("Verify the pipeline end to end", None, "RESPOND"),
        (
            "Remember Mitchell Ray. He is Noahs best friend and business partner. "
            "He is also married",
            "remember",
            "RESPOND",
        ),
    ],
)
def test_operator_command_kind_is_stamped_before_queue_and_controls_global_a(
    message, command_kind, expected_intent
):
    """The queued contributor owns the speech-act decision; Global A must not
    reinterpret delayed text and confuse a learning request with a build.
    """
    contributor = from_operator_message(
        {
            "message": message,
            "ingress_class": "operator_command",
            "cycle_id": f"cycle-{command_kind or 'interpreter'}",
        }
    )
    if command_kind is None:
        assert "operator_command_kind" not in contributor.metadata
        assert "directive_intent" not in contributor.metadata
    else:
        assert contributor.metadata["operator_command_kind"] == command_kind
    ignition = EpisodeIgnition(
        episode_id=f"ep-{command_kind or 'interpreter'}",
        aggregate_pressure=1.0,
        opened_by=contributor.type,
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot=None,
    )
    assert form_intent(ignition).intent == expected_intent


@pytest.mark.parametrize(
    "message",
    [
        "What should I learn next?",
        "I learned how to build a durable queue yesterday.",
        "Do you remember Mitchell Ray?",
        "I remember Mitchell Ray.",
        "How do I write a script?",
        "Explain how to create an app",
        "What happens when you read a file?",
        "Tell me why the pipeline needs verification",
    ],
)
def test_operator_command_kind_rejects_mentions_that_are_not_directives(message):
    """A command kind is a leading operator speech act, not any keyword hit."""
    assert infer_operator_command_kind(message) is None


def test_intent_always_allowed():
    for ob in (
        "operator_query",
        "operator_response",
        "learning_debt",
        "curiosity_gap",
        "epistemic_emptiness",
        "evidence_terminal",
        "identity_gap",
        "idle_probe_aggregate",
        "unknown_future_type",
    ):
        g = form_intent(_ignition(ob, message="hello"))
        assert g.intent in ALLOWED_INTENTS


def test_action_goal_has_no_tool_fields():
    g = form_intent(_ignition("operator_command", message="Learn Python"))
    assert g.intent == "LEARN"
    d = g.__dict__
    for forbidden in ("tool", "model", "web_fetch", "execution"):
        assert forbidden not in d
