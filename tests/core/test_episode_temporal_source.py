"""I2 — temporal envelope source is honest: 'live' only when a real tick was
threaded, 'unavailable' otherwise. Hardcoding 'live' (tick_count defaults to 0,
which is always >= 0) fabricates a measurement that was never taken."""

from __future__ import annotations

from core.episode_temporal import capture_temporal_context


def test_source_unavailable_when_no_tick_threaded():
    # The sole live caller (cognition_chain) passes only phase — no real tick.
    env = capture_temporal_context(phase="pressure")
    assert env["source"] == "unavailable"


def test_source_live_only_when_real_tick_threaded():
    env = capture_temporal_context(phase="pressure", tick_count=7, cycle_id="c1", epoch_id="e1")
    assert env["source"] == "live"
    assert env["tick_count"] == 7
