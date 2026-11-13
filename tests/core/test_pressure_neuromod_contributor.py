"""Neuromod tonic state + advisory ignition contract (B1–B7).

Neuromod is body-state context refreshed on close and decayed on idle ticks.
It is not aggregated as pressure and cannot control episode ignition.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.shared_state import SharedState
from pressure_loop.contributors import make_contributor
from pressure_loop.episode import EpisodeStore
from pressure_loop.episode_journal import enriched_episode_from_episode
from pressure_loop.policy import pressure_neuromod_tonic_decay
from pressure_loop.pressure import evaluate, gather_contributors
from pressure_loop.pressure_field import (
    decay_neuromod_tonic,
    read_neuromod_tonic,
    refresh_neuromod_tonic,
    reset_neuromod_tonic,
)


def _fail_high_stress_neuromod() -> dict:
    return {
        "dopamine": -1.0,
        "plasticity": 0.9,
        "stress": 0.9,
        "serotonin": 0.0,
        "strength": 1.0,
        "cycle_kind": "fail",
    }


def _deadlock_tonic() -> dict:
    return {"dopamine": -0.4, "serotonin": 0.67, "stress": 0.5}


@pytest.fixture(autouse=True)
def _reset_tonic():
    reset_neuromod_tonic()
    yield
    reset_neuromod_tonic()


# ---- B1: neuromod_spike no longer competes as contributor -----------------


def test_gather_excludes_neuromod_spike_even_with_high_stress_body():
    store = EpisodeStore()
    store._last_closed_body = {"neuromod": _fail_high_stress_neuromod()}
    contributors = gather_contributors(SharedState(), episode_store=store)
    assert not [c for c in contributors if c.type == "neuromod_spike"]


# ---- B3: decaying tonic state -------------------------------------------


def test_refresh_then_decay_drives_tonic_toward_baseline():
    refresh_neuromod_tonic(_deadlock_tonic())
    tonic = read_neuromod_tonic()
    assert tonic["dopamine"] == pytest.approx(-0.4)
    assert tonic["serotonin"] == pytest.approx(0.67)
    assert tonic["stress"] == pytest.approx(0.5)

    for _ in range(12):
        decay_neuromod_tonic()
    tonic = read_neuromod_tonic()
    assert abs(tonic["dopamine"]) < 0.05
    assert abs(tonic["serotonin"]) < 0.05
    assert abs(tonic["stress"]) < 0.05


def test_refresh_missing_keys_failure_soft():
    refresh_neuromod_tonic({"dopamine": -0.2})
    tonic = read_neuromod_tonic()
    assert tonic["dopamine"] == pytest.approx(-0.2)
    assert tonic["serotonin"] == pytest.approx(0.0)
    assert tonic["stress"] == pytest.approx(0.0)


def test_refresh_malformed_values_failure_soft():
    refresh_neuromod_tonic({"dopamine": "bad", "serotonin": "nan", "stress": 0.4})
    tonic = read_neuromod_tonic()
    assert tonic["dopamine"] == pytest.approx(0.0)
    assert tonic["serotonin"] == pytest.approx(0.0)
    assert tonic["stress"] == pytest.approx(0.4)


# ---- B4: refresh on episode close ---------------------------------------


def _minimal_ep(*, auto=None, episode_id: str = "ep-tonic-1"):
    return SimpleNamespace(
        episode_id=episode_id,
        opened_by="curiosity_gap",
        closed_at="2026-06-08T12:00:00+00:00",
        cascade_status="completed",
        global_={
            "action_goal": {"intent": "LEARN"},
            "outcome": {"status": "success"},
        },
        meta={"execution_action_pack": {"action": "USE_TOOL"}},
        auto=auto,
        pressure={},
        state="complete",
        brain_turns=[],
    )


def test_close_refreshes_neuromod_tonic_from_body():
    neuromod = _deadlock_tonic()
    ep = _minimal_ep(auto={"neuromod": neuromod})
    store = EpisodeStore()
    store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
    live = store.current()
    assert live is not None
    live.global_ = ep.global_
    live.meta = ep.meta
    live.auto = ep.auto
    store.close()

    tonic = read_neuromod_tonic()
    assert tonic["dopamine"] == pytest.approx(-0.4)
    assert tonic["serotonin"] == pytest.approx(0.67)
    assert tonic["stress"] == pytest.approx(0.5)


# ---- B5: decay on idle tick only -----------------------------------------


def test_idle_tick_decays_tonic_igniting_tick_does_not(monkeypatch):
    from pressure_loop.loop import _run_pressure_tick_body
    from pressure_loop.runtime import PressureRuntime

    refresh_neuromod_tonic(_deadlock_tonic())
    before = read_neuromod_tonic()
    decay_calls: list[str] = []

    def _track_decay():
        decay_calls.append("decay")
        decay_neuromod_tonic()

    monkeypatch.setattr(
        "pressure_loop.pressure_field.decay_neuromod_tonic",
        _track_decay,
    )

    ss = SharedState()
    store = EpisodeStore()
    runtime = PressureRuntime(shared_state=ss, episode_store=store)
    debt = make_contributor("autonomous", "learning_debt", 1.0, {"lane": "agent"})

    # Idle tick: evaluate returns None
    monkeypatch.setattr(
        "pressure_loop.pressure.gather_contributors",
        lambda *a, **k: [debt],
    )
    monkeypatch.setattr(
        "pressure_loop.pressure.evaluate",
        lambda *a, **k: None,
    )
    _run_pressure_tick_body(runtime)
    assert decay_calls == ["decay"]

    decay_calls.clear()
    # Igniting tick: evaluate returns ignition
    from pressure_loop.contributors import EpisodeIgnition

    ign = EpisodeIgnition(
        pipeline_id=2,
        work_kind="autonomous",
        episode_id="ep-idle",
        aggregate_pressure=1.0,
        opened_by="learning_debt",
        dominant_contributor=debt,
        contributors=(debt,),
        trajectory_snapshot=None,
    )
    monkeypatch.setattr(
        "pressure_loop.pressure.evaluate",
        lambda contributors, *a, **k: EpisodeIgnition(
            **{
                **ign.__dict__,
                "dominant_contributor": contributors[0],
                "contributors": (contributors[0],),
            }
        ),
    )
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda *a, **k: None)
    _run_pressure_tick_body(runtime)
    assert decay_calls == []


# ---- B6: neuromod is advisory, never gate authority -----------------------


def test_tonic_cannot_suppress_eligible_learning_debt_ignition():
    debt = make_contributor(
        "autonomous", "learning_debt", 1.0, {"lane": "agent", "domain": "python"}
    )
    refresh_neuromod_tonic(_deadlock_tonic())

    ignition = evaluate(
        [debt],
        active_episode=False,
        open_threshold=1.0,
        episode_store=EpisodeStore(),
    )
    assert ignition is not None
    assert ignition.opened_by == "learning_debt"
    assert ignition.aggregate_pressure == pytest.approx(1.0)
    assert ignition.dominant_contributor.weight == pytest.approx(1.0)
    assert ignition.neuromod_state == pytest.approx({**_deadlock_tonic(), "spike": 0.0})


# ---- B7: config loader --------------------------------------------------


def test_pressure_neuromod_tonic_decay_loader_reads_config_and_defaults():
    cfg = pressure_neuromod_tonic_decay()
    assert cfg["enabled"] is True
    assert 0.0 < float(cfg["decay_rate"]) <= 1.0
    assert float(cfg["baseline"]) == pytest.approx(0.0)

    custom = pressure_neuromod_tonic_decay(
        {"pressure": {"neuromod": {"tonic_decay": {"decay_rate": 0.33, "enabled": False}}}}
    )
    assert custom["enabled"] is False
    assert custom["decay_rate"] == pytest.approx(0.33)


# ---- salience invariant: live episode nm, not tonic ---------------------


def test_salience_latch_uses_live_episode_neuromod_not_tonic(monkeypatch):
    import cognition_chain as cl
    from autonomic.symphony_neuromod import current_memory_salience, compute_memory_salience

    refresh_neuromod_tonic(_deadlock_tonic())

    def _auto(ep):
        ep.auto = {"cycle_kind": "success"}
        return ep.auto

    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto)
    monkeypatch.setattr(cl, "_start_meta_b_global_b", lambda *_a, **_k: None)
    monkeypatch.setattr(EpisodeStore, "recent_outcome_population",
                        lambda self, n=10: _measured(0.0))

    episode = SimpleNamespace(
        episode_id="ep-sal",
        pipeline_id=0,
        work_kind="autonomous",
        auto=None,
        meta=None,
    )
    store = EpisodeStore()
    cl._start_auto(store, episode)

    latched = current_memory_salience()
    expected = compute_memory_salience(episode.auto["neuromod"])
    assert latched == expected
    tonic = read_neuromod_tonic()
    assert tonic["serotonin"] == pytest.approx(0.67)
    assert latched["scale"] != pytest.approx(0.0)


def _measured(rate):
    """A recent-outcome population whose measured task success rate is ``rate`` (WO-06)."""
    from pressure_loop.episode import OutcomePopulation

    successes = round(rate * 10)
    return OutcomePopulation(total=10, task_successes=successes, task_failures=10 - successes)
