"""Terminal cognition slice 1 — pressure weights/dominant + global intent emits."""

from __future__ import annotations

from unittest.mock import patch

from global_loop.loop import run_global_a
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop import pressure as pressure_mod
from pressure_loop.pressure import evaluate, gather_contributors


def _collect_events():
    events = []

    def _cb(ev):
        events.append(ev)

    return events, patch("guardian.terminal_feedback._publish_event", side_effect=_cb)


def _phase_ok(events, *, loop: str, phase: str, kind: str = "phase"):
    return [
        e
        for e in events
        if e.get("kind") == kind
        and e.get("loop") == loop
        and e.get("phase") == phase
        and e.get("status") == "ok"
    ]


def test_gather_contributors_phase_carries_weights():
    events, ctx = _collect_events()
    pressure_mod._RESEED_STAGING.clear()
    c1 = make_contributor("omega", "curiosity_gap", 0.18, {})
    c2 = make_contributor("omega", "learning_debt", 0.52, {})
    pressure_mod.stage_reseed((c1, c2))

    try:
        with ctx, patch("pressure_loop.pressure_field.build_registry", return_value={}):
            gather_contributors(type("State", (), {"read": lambda *_: None})())
    finally:
        pressure_mod._RESEED_STAGING.clear()

    ok = _phase_ok(
        events, loop="pressure", phase="gather_contributors_snapshot", kind="data"
    )
    assert len(ok) == 1
    assert ok[0]["weights"] == {
        "curiosity_gap": 0.18,
        "learning_debt": 0.52,
    }


def test_evaluate_phase_carries_dominant_agg_margin_on_ignition():
    events, ctx = _collect_events()
    dom = make_contributor("autonomous", "learning_debt", 0.8, {})
    runner = make_contributor("autonomous", "curiosity_gap", 0.2, {})

    with ctx:
        ignition = evaluate(
            [dom, runner], active_episode=False, open_threshold=0.1, neuromod={}
        )
    assert ignition is not None

    ok = _phase_ok(events, loop="pressure", phase="ignition_gate_detail", kind="data")
    assert len(ok) == 1
    assert ok[0]["top"] == "learning_debt"
    assert ok[0]["top_weight"] == 0.8
    assert ok[0]["gap"] == 0.6
    assert ok[0]["decision"] == "IGNITE:learning_debt"


def test_global_intent_phase_carries_intent_and_opened_by():
    events, ctx = _collect_events()
    dom = make_contributor(
        "autonomous",
        "idle_probe_aggregate",
        0.9,
        {},
    )
    ignition = EpisodeIgnition(
        episode_id="ep-global",
        aggregate_pressure=0.9,
        opened_by="idle_probe_aggregate",
        dominant_contributor=dom,
        contributors=(dom,),
        trajectory_snapshot=None,
    )
    episode = type("Ep", (), {"episode_id": "ep-global", "global_": {}})()

    with ctx:
        run_global_a(episode, ignition)

    ok = _phase_ok(events, loop="global", phase="intent")
    assert len(ok) == 1
    assert ok[0]["intent"] == "IDLE"
    assert ok[0]["opened_by"] == "idle_probe_aggregate"
