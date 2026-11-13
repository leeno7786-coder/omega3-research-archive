"""The idle rest must actually rest — a zero-second wait is a hot spin.

Live 2026-09-07: a fresh install (idle learning off by default, which is the
shipped default) sat quiet past the five-minute idle window and then ran the
pressure tick roughly sixty times a SECOND, burying the operator's terminal in
gather / tonic-decay / tick-summary rows until the process was killed.

The cause was the sleep bound. With the governor OFF the wake can never fire,
so nothing ever stamps the wake throttle, so `seconds_until_wake_attempt`
answers 0.0 for the rest of the run once the idle window passes. The loop slept
on that answer, and `Event.wait(timeout=0.0)` returns immediately.

The wake timer answers a question only the governor-ON path may ask. The OFF
path rests on its own recheck window, which is never zero.
"""

from __future__ import annotations

import threading

import pytest

import pressure_loop.idle_wake as iw
from pressure_loop import loop


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    import core.paths as paths

    monkeypatch.setattr(paths, "GUARDIAN_STATE_ROOT", tmp_path)
    iw._reset_for_tests()
    loop.reset_pressure_loop_stop()
    yield
    iw._reset_for_tests()
    loop.reset_pressure_loop_stop()


def test_the_idle_recheck_window_is_never_zero():
    """Catches any bound that lets the loop wake instantly and re-tick."""
    assert iw.seconds_until_idle_recheck() > 0.0


def test_the_wake_timer_still_reads_zero_once_the_window_passes():
    """The defect's ingredient, kept visible so the two are never conflated.

    `seconds_until_wake_attempt` is correct as written — 0.0 means the wake may
    fire now. It is only wrong as a sleep bound when no wake will ever fire.
    """
    iw._last_activity_ts = iw._last_activity_ts - (iw.idle_wake_seconds() + 1)

    assert iw.seconds_until_wake_attempt() == 0.0
    assert iw.seconds_until_idle_recheck() > 0.0


class _SharedState:
    def write(self, key, value):  # noqa: D102 - test double
        return None


class _Runtime:
    shared_state = _SharedState()
    episode_store = None


def _run_until_first_rest(monkeypatch) -> tuple[list[float | None], list[int]]:
    """Drive the real loop to its idle rest; report every wait bound it used."""
    ticks: list[int] = []
    waits: list[float | None] = []

    monkeypatch.setattr(loop, "_run_pressure_tick_body", lambda _runtime: ticks.append(1))
    monkeypatch.setattr(
        loop, "_repass_while_pressure_changes", lambda _runtime, sig: (sig, False)
    )
    monkeypatch.setattr(
        loop, "pressure_loop_pause_state", lambda: {"paused": False}, raising=False
    )
    monkeypatch.setattr(
        "pressure_loop.pressure_loop_pause.pressure_loop_pause_state",
        lambda: {"paused": False},
    )

    real_event = loop._work_event

    class _Recording(threading.Event):
        def wait(self, timeout=None):  # noqa: D102 - test double
            waits.append(timeout)
            loop.request_pressure_loop_stop()  # rest reached: end the run
            return True

    monkeypatch.setattr(loop, "_work_event", _Recording())
    try:
        loop.run_pressure_tick_loop(_Runtime())
    finally:
        monkeypatch.setattr(loop, "_work_event", real_event)
    return waits, ticks


def test_a_quiet_mind_with_autonomy_off_rests_instead_of_spinning(monkeypatch):
    """The exact live shape: governor off, idle window long past."""
    monkeypatch.setattr(iw, "governor_enabled", lambda: False)
    iw._last_activity_ts = iw._last_activity_ts - (iw.idle_wake_seconds() + 60)

    waits, ticks = _run_until_first_rest(monkeypatch)

    assert ticks == [1], "the loop must rest after one idle tick, not re-tick"
    assert waits and all(w is not None and w > 0.0 for w in waits), waits


def test_the_same_holds_the_moment_the_window_expires(monkeypatch):
    """The boundary, where the wake timer first reads 0.0."""
    monkeypatch.setattr(iw, "governor_enabled", lambda: False)
    iw._last_activity_ts = iw._last_activity_ts - iw.idle_wake_seconds()

    waits, ticks = _run_until_first_rest(monkeypatch)

    assert ticks == [1]
    assert waits and all(w is not None and w > 0.0 for w in waits), waits
