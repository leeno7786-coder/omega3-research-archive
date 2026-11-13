"""Pressure loop closure — Omega pressure_feedback → reseed → next-tick intent."""

from __future__ import annotations

import pytest

from deepsleep_loop.reseed.run import generate_reseed
from global_loop.intent.run import run as map_intent
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.pressure import (
    evaluate,
    gather_contributors,
    stage_reseed,
)
from tests.core.deepsleep_window_fixtures import episode_entry, window_input
from tests.core.rebuild_helpers import form_intent


def _pressure_feedback_input(pf: dict):
    return window_input(
        window_id="ep-pf",
        episodes=[episode_entry("ep-pf", intent="IDLE", target="x")],
        reflection={
            "reasoning": {"what": "omega window"},
            "trajectory": {},
            "pressure_feedback": pf,
        },
    )


def _reset_pressure_staging():
    from pressure_loop import pressure as pressure_mod

    pressure_mod.clear_pending_contributors()
    pressure_mod._RESEED_STAGING.clear()
    if hasattr(pressure_mod, "_RESEED_SUPPRESS"):
        pressure_mod._RESEED_SUPPRESS.clear()


def _ignition(opened_by: str, **meta):
    c = make_contributor("autonomous", opened_by, 1.0, meta)
    return EpisodeIgnition(
        episode_id="ep-test",
        aggregate_pressure=0.9,
        opened_by=opened_by,
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


# --- Packet 1: pressure_feedback raise + create → reseed contributors ---


def test_generate_reseed_includes_pressure_feedback_raise_and_create(monkeypatch):
    pf = {
        "contributors_to_raise": [{"type": "learning_debt", "weight": 0.8}],
        "contributors_to_lower": [],
        "contributors_to_create": [{"type": "system_diagnostic", "weight": 0.9, "lane": "chat"}],
    }
    contributors = generate_reseed(_pressure_feedback_input(pf))
    by_type = {c.type: c for c in contributors}
    assert "learning_debt" in by_type
    assert by_type["learning_debt"].weight == pytest.approx(0.8)
    assert by_type["learning_debt"].source == "omega"
    assert "system_diagnostic" in by_type
    assert by_type["system_diagnostic"].weight == pytest.approx(0.9)
    assert by_type["system_diagnostic"].source == "omega"
    assert by_type["system_diagnostic"].metadata["lane"] == "chat"


# --- Packet 2: lower → suppression overlay ---


def test_stage_reseed_suppress_reduces_gathered_weight(monkeypatch):
    from pressure_loop import pressure as pressure_mod

    _reset_pressure_staging()
    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    stage_reseed([make_contributor("omega", "idle_probe_aggregate", 1.0, {})])
    pressure_mod.stage_reseed_suppress("idle_probe_aggregate", 0.5)
    gathered = gather_contributors(shared_state=None)
    idle = [c for c in gathered if c.type == "idle_probe_aggregate"]
    assert len(idle) == 1
    assert idle[0].weight == pytest.approx(0.5)


def test_generate_reseed_lowers_via_suppression_overlay(monkeypatch):
    _reset_pressure_staging()
    stage_reseed([make_contributor("omega", "idle_probe_aggregate", 1.0, {})])
    pf = {
        "contributors_to_raise": [],
        "contributors_to_lower": [{"type": "idle_probe_aggregate", "weight": 0.6}],
        "contributors_to_create": [],
    }
    generate_reseed(_pressure_feedback_input(pf))
    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    gathered = gather_contributors(shared_state=None)
    idle = [c for c in gathered if c.type == "idle_probe_aggregate"]
    assert len(idle) == 1
    assert idle[0].weight == pytest.approx(0.4)


# --- Packet 3: novel-type → IDLE ---


def test_novel_opened_by_maps_to_idle():
    assert map_intent(_ignition("system_diagnostic")) == "IDLE"


def test_idle_probe_aggregate_stays_idle():
    assert map_intent(_ignition("idle_probe_aggregate")) == "IDLE"


# --- Packet 4: end-to-end course alteration ---


def test_omega_pressure_feedback_alters_next_tick_intent(monkeypatch):
    """Raise learning_debt + lower idle_probe → LEARN wins over IDLE."""
    _reset_pressure_staging()
    pf = {
        "contributors_to_raise": [{"type": "learning_debt", "weight": 1.0}],
        "contributors_to_lower": [{"type": "idle_probe_aggregate", "weight": 0.5}],
        "contributors_to_create": [],
    }
    reseed = generate_reseed(_pressure_feedback_input(pf))
    stage_reseed(reseed)

    monkeypatch.setattr(
        "pressure_loop.pressure_field.build_registry",
        lambda: {
            "idle_probe_aggregate": lambda ss: 1.0,
            # learning_debt reader contract takes learning_state_envelope
            "learning_debt": lambda ss, **k: 0.3,
        },
    )

    contributors = gather_contributors(shared_state=None)
    ignition = evaluate(
        contributors,
        active_episode=False,
        open_threshold=0.9,
        neuromod={},
    )
    assert ignition is not None
    assert ignition.opened_by == "learning_debt"

    goal = form_intent(ignition)
    assert goal.intent == "LEARN"
    assert goal.intent != "IDLE"
