"""N2 — salience latched LIVE in cognition_chain._start_auto."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import cognition_chain as cl
from autonomic.symphony_neuromod import (
    compute_memory_salience,
    current_memory_salience,
)
from pressure_loop.episode import EpisodeStore


def _stub_episode(*, auto: dict | None = None):
    return SimpleNamespace(
        episode_id="ep-salience",
        pipeline_id=0,
        work_kind="autonomous",
        auto=dict(auto) if auto is not None else None,
        meta=None,
    )


def _patch_auto_sets_outcome(monkeypatch, outcome: dict, *, baseline: float = 0.0):
    def _auto(ep):
        ep.auto = dict(outcome)
        return ep.auto

    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto)
    monkeypatch.setattr(cl, "_start_meta_b_global_b", lambda *_a, **_k: None)
    monkeypatch.setattr(
        EpisodeStore,
        "recent_outcome_population",
        lambda self, n=10: _measured(baseline),
    )


def test_surprising_success_latches_elevated_salience(monkeypatch):
    store = EpisodeStore()
    _patch_auto_sets_outcome(monkeypatch, {"cycle_kind": "success"})
    episode = _stub_episode()

    cl._start_auto(store, episode)

    latched = current_memory_salience()
    expected = compute_memory_salience(episode.auto["neuromod"])
    assert latched == expected
    assert latched["scale"] > 1.0
    assert isinstance(episode.auto.get("neuromod"), dict)


def test_anti_bleed_skipped_cycle_resets_elevated_scale(monkeypatch):
    store = EpisodeStore()
    # Baseline 0.5: success is mildly surprising (scale>1); skipped is neutral (rpe=0).
    _patch_auto_sets_outcome(monkeypatch, {"cycle_kind": "success"}, baseline=0.5)
    episode1 = _stub_episode()
    cl._start_auto(store, episode1)
    assert current_memory_salience()["scale"] > 1.0

    _patch_auto_sets_outcome(monkeypatch, {"cycle_kind": "skipped"}, baseline=0.5)
    episode2 = _stub_episode()
    cl._start_auto(store, episode2)
    assert current_memory_salience()["scale"] == pytest.approx(1.0)


def _measured(rate):
    """A recent-outcome population whose measured task success rate is ``rate`` (WO-06)."""
    from pressure_loop.episode import OutcomePopulation

    successes = round(rate * 10)
    return OutcomePopulation(total=10, task_successes=successes, task_failures=10 - successes)
