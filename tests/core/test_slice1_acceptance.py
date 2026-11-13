"""Slice 1 B5 — acceptance + invariants (spec PART 9–10)."""

from __future__ import annotations

from pressure_loop.contributors import ALLOWED_INTENTS, from_operator_message
from tests.core.rebuild_helpers import form_intent
from pressure_loop.pressure import clear_pending_contributors, evaluate


def test_acceptance_learn_python_no_tool_or_memory():
    """Operator 'Learn Python' → operator_command → LEARN / Python."""
    clear_pending_contributors()
    contributor = from_operator_message(
        {"message": "Learn Python", "ingress_class": "operator_command", "weight": 1.0}
    )
    assert contributor.type == "operator_command"

    ignition = evaluate(
        [contributor],
        active_episode=False,
        open_threshold=0.55,
        neuromod={},
    )
    assert ignition is not None
    assert ignition.opened_by == "operator_command"

    goal = form_intent(ignition)
    assert goal.intent == "LEARN"
    assert not hasattr(goal, "target")
    assert goal.intent in ALLOWED_INTENTS

    forbidden = ("tool", "web_fetch", "memory", "model", "execute")
    blob = {**goal.__dict__, **(ignition.__dict__ if hasattr(ignition, "__dict__") else {})}
    for key in blob:
        for f in forbidden:
            assert f not in str(key).lower() or key == "reason"


def test_invariant_pressure_never_emits_action_fields():
    clear_pending_contributors()
    c = from_operator_message({"message": "Learn Python", "weight": 1.0})
    ign = evaluate([c], active_episode=False, open_threshold=0.1, neuromod={})
    assert ign is not None
    for field in ("intent", "tool", "action", "goal"):
        assert field not in ign.__dict__


def test_invariant_global_a_never_selects_tools():
    clear_pending_contributors()
    c = from_operator_message({"message": "Learn Python", "weight": 1.0})
    ign = evaluate([c], active_episode=False, open_threshold=0.1, neuromod={})
    goal = form_intent(ign)
    assert "tool" not in goal.__dict__
    assert goal.intent != "web_fetch"
