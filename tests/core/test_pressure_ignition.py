"""Slice 1 B2 — pressure → EpisodeIgnition."""

from __future__ import annotations

from pressure_loop.contributors import make_contributor
from pressure_loop.pressure import clear_pending_contributors, evaluate, pending_contributors


def _cmd(weight: float = 1.0):
    return make_contributor(
        "operator",
        "operator_command",
        weight,
        {"message": "Learn Python"},
    )


def test_evaluate_emits_ignition_at_ceiling():
    clear_pending_contributors()
    ign = evaluate([_cmd()], active_episode=False, open_threshold=1.0, neuromod={})
    assert ign is not None
    assert ign.opened_by == "operator_command"
    assert ign.aggregate_pressure >= 1.0 - 1e-6
    assert len(ign.contributors) >= 1


def test_evaluate_none_while_active_episode_does_not_create_second_queue():
    clear_pending_contributors()
    ign = evaluate([_cmd()], active_episode=True, open_threshold=0.1, neuromod={})
    assert ign is None
    assert pending_contributors() == ()


def test_evaluate_none_below_ceiling():
    clear_pending_contributors()
    low = make_contributor("autonomous", "curiosity_gap", 0.05, {})
    ign = evaluate([low], active_episode=False, open_threshold=1.0, neuromod={})
    assert ign is None


def test_ignition_has_no_action_fields():
    clear_pending_contributors()
    ign = evaluate([_cmd()], active_episode=False, open_threshold=0.1, neuromod={})
    assert ign is not None
    for c in ign.contributors:
        assert "tool" not in c.type
        assert "action" not in c.type


def test_dominant_is_highest_at_ceiling():
    clear_pending_contributors()
    contributors = [
        make_contributor("autonomous", "curiosity_gap", 0.2, {}),
        _cmd(1.0),
    ]
    ign = evaluate(contributors, active_episode=False, open_threshold=1.0, neuromod={})
    assert ign is not None
    assert ign.opened_by == "operator_command"
