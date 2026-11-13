"""Cadence units only — the cascade pacing sleeps are DEAD.

Phase 3 (concurrent-cognition spec 2026-08-08 §6, landed 2026-08-09) deleted
the six 5s pause_after_cascade_phase sleeps: ~30s of pure wall-clock padding
per episode, protecting no write ordering. These tests pin the surviving
cadence units and that no pacing sleep can reappear in a pressure tick.
"""

from __future__ import annotations

import pytest

from core import cognition_pacing
from core.cognition_pacing import (
    between_tick_seconds,
    pressure_tick_interval_seconds,
    reset_cognition_pacing_for_tests,
)
from guardian.terminal_feedback import phase_span


@pytest.fixture(autouse=True)
def _reset_pacing():
    reset_cognition_pacing_for_tests()
    yield
    reset_cognition_pacing_for_tests()


def test_tick_interval_is_five_seconds():
    assert pressure_tick_interval_seconds() == 5.0
    assert between_tick_seconds() == 5.0


def test_cascade_pacing_is_gone():
    # The deleted machinery must not quietly return.
    assert not hasattr(cognition_pacing, "pause_after_cascade_phase")
    assert not hasattr(cognition_pacing, "cascade_phase_pause_seconds")
    import cognition_chain as chain

    assert not hasattr(chain, "pause_after_cascade_phase")


def test_phase_span_no_in_phase_pause(monkeypatch):
    import time as _time

    sleeps: list[float] = []
    monkeypatch.setattr(_time, "sleep", sleeps.append)
    with phase_span("deepsleep", "intake", episode_id="ep-1"):
        pass
    assert sleeps == []


def test_pressure_tick_phases_no_pause(monkeypatch):
    from pressure_loop import loop as pressure_loop

    import time as _time

    sleeps: list[float] = []
    monkeypatch.setattr(_time, "sleep", sleeps.append)
    monkeypatch.setattr(
        "pressure_loop.pressure.gather_contributors",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "pressure_loop.pressure.evaluate",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "pressure_loop.pressure_field.decay_neuromod_tonic",
        lambda: None,
    )

    class _Store:
        # The tick asks each free physical pipeline for its carrier before it
        # leases anything.
        def current(self, pipeline_id=None):
            return None

    class _Runtime:
        episode_store = _Store()
        shared_state = object()

    pressure_loop._run_pressure_tick_body(_Runtime())
    assert sleeps == []
