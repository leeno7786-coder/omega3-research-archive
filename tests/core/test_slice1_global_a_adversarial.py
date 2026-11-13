"""Slice 1 — ADVERSARIAL coverage for Global A (the semantic intent router).

Every opened_by -> intent mapping, the "never a 9th intent" invariant under
fuzzed input, hint-precedence collisions (image vs task vs learn), target
extraction edges, and confidence bounds. Independent of the implementer's B3.
"""

from __future__ import annotations

import pytest

from pressure_loop.contributors import ALLOWED_INTENTS, EpisodeIgnition, make_contributor
from tests.core.rebuild_helpers import form_intent


def _ign(opened_by, message="", *, weight=1.0, aggregate=0.8, metadata=None):
    meta = {"message": message} if message else {}
    if metadata:
        meta.update(metadata)
    c = make_contributor("x", opened_by, weight, meta)
    return EpisodeIgnition(
        episode_id="ep-1",
        aggregate_pressure=aggregate,
        opened_by=opened_by,
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


# ---- every opened_by maps to its designed intent -------------------------


@pytest.mark.parametrize(
    "opened_by,message,expected",
    [
        ("operator_query", "What is X?", "RESPOND"),
        ("operator_response", "fixed it", "RESOLVE_QUERY"),
        ("proactive_update", "", "PROACTIVE_RESPONSE"),
        ("learning_debt", "", "LEARN"),
        ("curiosity_gap", "", "LEARN"),
        ("epistemic_emptiness", "", "LEARN"),
        ("identity_gap", "", "IDLE"),
        ("trajectory_gap", "", "IDLE"),
        ("idle_probe_aggregate", "", "IDLE"),
        ("operator_command", "Learn Python", "LEARN"),
        # Opaque operator language remains a cognition turn. Only explicit UI
        # directives choose a concrete dispatch intent before AUTO interprets it.
        ("operator_command", "draw a cat", "RESPOND"),
        ("operator_command", "run the build", "RESPOND"),
        ("operator_command", "do the thing", "RESPOND"),
        # Phase 7M: tool_failure is now a first-class agentic opened_by (-> USE_SKILL).
        ("tool_failure", "", "USE_SKILL"),
    ],
)
def test_opened_by_maps_to_intent(opened_by, message, expected, monkeypatch):
    # tool_failure (and the agentic gaps) MAP to USE_SKILL, which the dispatchability
    # guard downgrades to IDLE without an objective; mint one so this map-coverage
    # test asserts the DESIGNED route — the guard's downgrade is owned by its own test
    # (test_use_skill_dispatchability_guard). Operator/LEARN/RESPOND routes are unaffected.
    monkeypatch.setattr("pressure_loop.practice_mint.mint_for_prepared_apply", lambda: True)
    goal = form_intent(_ign(opened_by, message))
    assert goal.intent == expected


# ---- gap: unknown / novel opened_by → IDLE (pressure loop closure) --------


@pytest.mark.parametrize(
    "opened_by",
    [
        "",
        "totally_unknown",
        "garbage",
        "LEARN",
        "USE_TOOL",
        # within-episode observations are NOT ignition reasons -> never opened_by;
        # if one ever leaked through it maps to IDLE (unknown catch-all path).
        "evidence_terminal",
        "novel_observation",
        "system_diagnostic",
    ],
)
def test_unknown_opened_by_falls_to_idle(opened_by):
    goal = form_intent(_ign(opened_by, ""))
    assert goal.intent == "IDLE"


def test_output_intent_is_always_allowed_under_fuzz():
    fuzz = [
        "",
        "?",
        "learn",
        "LEARN something",
        "build a learn study",
        "draw run learn",
        "operator_command",
        "x" * 500,
        "🙂",
        "learn ",
    ]
    openers = [
        "operator_command",
        "operator_query",
        "operator_response",
        "learning_debt",
        "unknown",
        "evidence_terminal",
        "",
        "idle_probe_aggregate",
    ]
    for ob in openers:
        for msg in fuzz:
            goal = form_intent(_ign(ob, msg))
            assert goal.intent in ALLOWED_INTENTS, (ob, msg, goal.intent)


# ---- hint precedence collisions (operator_command) -----------------------


def test_directive_intent_beats_message_heuristics():
    # IMAGE_CREATE only from explicit UI directive — never word parsing.
    goal = form_intent(
        _ign(
            "operator_command",
            "learn and build a logo",
            metadata={"directive_intent": "IMAGE_CREATE"},
        )
    )
    assert goal.intent == "IMAGE_CREATE"


def test_task_like_operator_language_remains_opaque():
    goal = form_intent(_ign("operator_command", "fix the failing test"))
    assert goal.intent == "RESPOND"


def test_learn_prefix_when_no_image_or_task_hint():
    goal = form_intent(_ign("operator_command", "learn the history of rome"))
    assert goal.intent == "LEARN"


# ---- confidence bounds + intent-only output ------------------------------


def test_confidence_is_unit_bounded():
    for w in (-1.0, 0.0, 0.5, 1.0, 9.0):
        goal = form_intent(_ign("operator_command", "learn x", weight=w))
        assert 0.0 <= goal.confidence <= 1.0


def test_action_goal_never_carries_a_tool():
    goal = form_intent(_ign("operator_command", "run a tool"))
    assert "tool" not in goal.__dict__
    assert "model" not in goal.__dict__
    assert goal.intent in ALLOWED_INTENTS


def test_episode_id_is_propagated():
    ign = _ign("operator_query", "What is X?")
    goal = form_intent(ign)
    assert goal.episode_id == ign.episode_id
