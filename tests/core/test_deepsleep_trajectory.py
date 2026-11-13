"""Slice 6c B3 — DeepSleep trajectory maintenance store and phase."""

from __future__ import annotations

import pytest

from deepsleep_loop._deepsleep_state import TrajectoryRecord
from tests.core.deepsleep_window_fixtures import trajectory_input as _trajectory_input
from deepsleep_loop.trajectory.run import maintain_trajectory, trajectory_effects_from_records
from deepsleep_loop.trajectory_store import load_trajectory, trajectory_key, upsert_trajectory


def test_trajectory_record_round_trip():
    original = TrajectoryRecord(
        target_domain="quantum_physics",
        target_capability="foundational understanding",
        confidence=0.85,
        reinforcement_count=2,
        supporting_episodes=["ep-1", "ep-2"],
    )
    restored = TrajectoryRecord.from_dict(original.to_dict())
    assert restored == original


def test_first_update_creates_active_record(tmp_path, monkeypatch):
    path = tmp_path / "trajectory.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_trajectory_path",
        lambda policy=None: path,
    )

    records = maintain_trajectory(_trajectory_input("ep-1", domain="quantum_physics"))
    assert len(records) == 1
    assert records[0].status == "active"
    assert records[0].reinforcement_count == 1

    loaded = load_trajectory(path=path)
    key = trajectory_key("quantum_physics", "foundational understanding")
    assert key in loaded


def test_matching_update_reinforces(tmp_path, monkeypatch):
    path = tmp_path / "trajectory.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_trajectory_path",
        lambda policy=None: path,
    )

    maintain_trajectory(_trajectory_input("ep-1", domain="quantum_physics"))
    records = maintain_trajectory(_trajectory_input("ep-2", domain="quantum_physics"))

    assert records[0].reinforcement_count == 2
    assert "ep-1" in records[0].supporting_episodes
    assert "ep-2" in records[0].supporting_episodes


def test_reviving_superseded_trajectory_carries_history(tmp_path, monkeypatch):
    """I12: a superseded trajectory revived under the same key must carry its
    accumulated reinforcement_count + supporting_episodes forward, not reset to 1
    (reinforcement_count is the projection tiebreaker + the audit trail)."""
    path = tmp_path / "trajectory.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_trajectory_path",
        lambda policy=None: path,
    )

    # quantum: active, reinforced twice (count=2, supporting ep-1, ep-2)
    maintain_trajectory(_trajectory_input("ep-1", domain="quantum_physics", confidence=0.6))
    maintain_trajectory(_trajectory_input("ep-2", domain="quantum_physics", confidence=0.6))
    # a higher-confidence different domain supersedes quantum
    maintain_trajectory(_trajectory_input("ep-3", domain="classical_physics", confidence=0.95))
    qkey = trajectory_key("quantum_physics", "foundational understanding")
    assert load_trajectory(path=path)[qkey].status == "superseded"

    # revive quantum — must carry history forward, not reset to 1
    records = maintain_trajectory(
        _trajectory_input("ep-4", domain="quantum_physics", confidence=0.7)
    )
    assert records[0].status == "active"
    assert records[0].reinforcement_count == 3
    assert set(records[0].supporting_episodes) >= {"ep-1", "ep-2", "ep-4"}


def test_higher_confidence_different_target_supersedes_old(tmp_path, monkeypatch):
    path = tmp_path / "trajectory.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_trajectory_path",
        lambda policy=None: path,
    )

    maintain_trajectory(_trajectory_input("ep-1", domain="classical_physics", confidence=0.5))
    maintain_trajectory(_trajectory_input("ep-2", domain="quantum_physics", confidence=0.9))

    loaded = load_trajectory(path=path)
    old_key = trajectory_key("classical_physics", "foundational understanding")
    assert loaded[old_key].status == "superseded"


def test_trajectory_effects_from_records():
    records = [
        TrajectoryRecord(
            target_domain="quantum_physics",
            target_capability="foundational understanding",
            confidence=0.85,
            reinforcement_count=1,
        )
    ]
    effects = trajectory_effects_from_records(records, prior=None)
    key = trajectory_key("quantum_physics", "foundational understanding")
    assert key in effects
