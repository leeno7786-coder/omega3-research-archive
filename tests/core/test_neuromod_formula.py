"""N1 — compute_episode_neuromod pure RPE formula (RED-first)."""

from __future__ import annotations

import pytest

from autonomic.symphony_neuromod import compute_episode_neuromod


def test_always_fail_agent_succeeds_big_encode():
    nm = compute_episode_neuromod({"cycle_kind": "success"}, 0.0)
    assert nm["dopamine"] == pytest.approx(1.0)
    assert nm["plasticity"] == pytest.approx(1.0)


def test_routine_success_agent_succeeds_mundane():
    nm = compute_episode_neuromod({"cycle_kind": "success"}, 1.0)
    assert nm["dopamine"] == pytest.approx(0.0)
    assert nm["plasticity"] == pytest.approx(0.0)


def test_routine_success_agent_fails_high_stress():
    nm = compute_episode_neuromod({"cycle_kind": "fail"}, 1.0)
    assert nm["dopamine"] == pytest.approx(-1.0)
    assert nm["plasticity"] == pytest.approx(1.0)
    assert nm["stress"] == pytest.approx(1.0)


def test_neutral_baseline_success():
    nm = compute_episode_neuromod({"cycle_kind": "success"}, 0.5)
    assert nm["dopamine"] == pytest.approx(0.5)
    assert nm["plasticity"] == pytest.approx(0.5)


def test_skipped_cycle_neutral():
    nm = compute_episode_neuromod({"cycle_kind": "skipped"}, 0.5)
    assert nm["dopamine"] == pytest.approx(0.0)
    assert nm["plasticity"] == pytest.approx(0.0)
    assert nm["stress"] == pytest.approx(0.0)


def test_garbage_inputs_never_raise():
    zeros = {
        "dopamine": 0.0,
        "plasticity": 0.0,
        "stress": 0.0,
        "serotonin": 0.0,
        "strength": 0.0,
        "cycle_kind": "",
    }
    assert compute_episode_neuromod(None, "x") == zeros

    nm = compute_episode_neuromod({}, 0.5)
    assert nm["dopamine"] == pytest.approx(0.0)
    assert nm["plasticity"] == pytest.approx(0.0)
    assert nm["stress"] == pytest.approx(0.0)
    assert nm["cycle_kind"] == ""
