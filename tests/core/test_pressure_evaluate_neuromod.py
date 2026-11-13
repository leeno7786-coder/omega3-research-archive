"""N5b — evaluate gate modulation never-raises from episode_store body-state."""

from __future__ import annotations

import pytest

from pressure_loop.contributors import make_contributor
from pressure_loop.episode import EpisodeStore
from pressure_loop.pressure import evaluate


def _drive(weight: float = 1.0):
    return make_contributor("autonomous", "curiosity_gap", weight, {})


def test_evaluate_empty_store_last_closed_body_none_does_not_raise():
    store = EpisodeStore()
    assert store.last_closed_body() is None
    ign = evaluate(
        [_drive()],
        active_episode=False,
        open_threshold=1.0,
        episode_store=store,
    )
    assert ign is not None


def test_evaluate_episode_store_none_headless_does_not_raise():
    ign = evaluate(
        [_drive()],
        active_episode=False,
        open_threshold=1.0,
        episode_store=None,
    )
    assert ign is not None
