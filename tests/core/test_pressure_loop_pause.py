from __future__ import annotations

import pytest

from pressure_loop.pressure_loop_pause import (
    _reset_pressure_loop_pause_for_tests,
    is_pressure_loop_paused,
    pause_pressure_loop,
    pressure_loop_pause_state,
    resume_pressure_loop,
)


@pytest.fixture(autouse=True)
def _reset_pause():
    from pressure_loop.loop import reset_pressure_loop_stop

    reset_pressure_loop_stop()
    _reset_pressure_loop_pause_for_tests()
    yield
    _reset_pressure_loop_pause_for_tests()
    reset_pressure_loop_stop()


def test_pause_state_tracks_owner_set():
    assert is_pressure_loop_paused() is False

    state = pause_pressure_loop("browser_agent", reason="engaged")
    assert state["paused"] is True
    assert state["owners"] == ["browser_agent"]
    assert pressure_loop_pause_state()["reasons"]["browser_agent"] == "engaged"

    state = resume_pressure_loop("browser_agent", reason="done")
    assert state["paused"] is False
    assert state["owners"] == []
    assert is_pressure_loop_paused() is False


def test_pause_owner_required():
    with pytest.raises(ValueError, match="owner required"):
        pause_pressure_loop("")


def test_pressure_tick_loop_does_not_advance_when_paused(monkeypatch):
    from pressure_loop import loop

    writes: list[tuple[str, object]] = []

    class _SharedState:
        def write(self, key, value):
            writes.append((key, value))

    class _Runtime:
        shared_state = _SharedState()
        episode_store = object()

    pause_pressure_loop("browser_agent", reason="engaged")
    monkeypatch.setattr(loop, "between_tick_seconds", lambda: 0)
    monkeypatch.setattr(
        loop.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        loop.run_pressure_tick_loop(_Runtime())

    assert ("pressure_loop_paused", True) in writes
    assert not any(key == "_pressure_tick" for key, _value in writes)
