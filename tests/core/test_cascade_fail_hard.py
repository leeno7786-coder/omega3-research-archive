"""Fail-hard cognition across the per-episode cascade layers.

Operator directive: cognition crashes the system. A COMPUTE failure inside a
cascade layer must propagate and HALT — it is never swallowed to None. The
LEGITIMATE return-None paths (a layer that simply has nothing to produce, e.g.
Global B with no folded AUTO outcome) are preserved: absence is not failure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pressure_loop.contributors import Contributor, EpisodeIgnition


def _ignition() -> EpisodeIgnition:
    c = Contributor(
        source="operator",
        type="operator_command",
        weight=0.9,
        metadata={"message": "Learn Python"},
    )
    return EpisodeIgnition(
        episode_id="ep-fh",
        aggregate_pressure=0.9,
        opened_by="operator_command",
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


def _boom(*_a, **_k):
    raise RuntimeError("phase boom")


def test_run_global_a_halts_on_phase_failure(monkeypatch):
    import global_loop.loop as gl

    monkeypatch.setattr("global_loop.ignition.run", _boom)
    episode = SimpleNamespace(episode_id="ep-fh")

    with pytest.raises(RuntimeError, match="phase boom"):
        gl.run_global_a(episode, _ignition())


def test_run_global_b_halts_on_phase_failure(monkeypatch):
    import global_loop.loop as gl

    monkeypatch.setattr("global_loop.assemble.run", _boom)
    episode = SimpleNamespace(episode_id="ep-fh")

    with pytest.raises(RuntimeError, match="phase boom"):
        gl.run_global_b(episode, {"execution": {}})


def test_run_global_b_returns_none_when_nothing_to_assemble(monkeypatch):
    # ABSENCE is not failure: assemble legitimately returns None (no folded
    # AUTO outcome) -> Global B returns None without raising.
    import global_loop.loop as gl

    monkeypatch.setattr("global_loop.assemble.run", lambda ep, mr: None)
    episode = SimpleNamespace(episode_id="ep-fh")

    assert gl.run_global_b(episode, {"execution": {}}) is None
