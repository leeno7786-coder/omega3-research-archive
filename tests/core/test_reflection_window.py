"""Slice 1 — EpisodeStore owns the reflection window."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pressure_loop.episode import EpisodeStore
from pressure_loop.reflection_window import (
    STATUS_COLLECTING,
    STATUS_LOCKED,
    WINDOW_SIZE,
    ReflectionWindow,
    append_episode_to_window,
    load_reflection_window,
    open_reflection_window,
)


def _setup_episode(store: EpisodeStore, *, episode_id: str) -> object:
    store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")
    ep = store.current()
    assert ep is not None
    ep.episode_id = episode_id
    ep.global_ = {
        "action_goal": {"intent": "LEARN"},
        "outcome": {"status": "success"},
    }
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}
    ep.omega = {}
    return ep


def _close(store: EpisodeStore, **kwargs) -> object:
    """Close an episode the way the terminus does (Phase 4 cut 13).

    The reflection-window append moved out of ``EpisodeStore.close`` and into
    the DeepSleep commit region, which ``close`` reaches through
    ``before_finalize``. A bare ``close()`` therefore no longer appends — and a
    test that used one would be asserting against a close shape production
    never performs.
    """
    return store.close(
        before_finalize=lambda ep, body: store.commit_reflection_window_append(ep, body),
        **kwargs,
    )


def test_reflection_window_opens_on_first_close(tmp_path, monkeypatch):
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    journal_path = tmp_path / "episode_journal.jsonl"

    monkeypatch.setattr(
        "pressure_loop.reflection_window.reflection_window_path",
        lambda: window_path,
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.current_trajectory_path",
        lambda: traj_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )
    monkeypatch.setattr(
        "pressure_loop.temporal_store.read_trajectory",
        lambda: {
            "trajectory_id": "traj-test-001",
            "target_domain": "python",
            "target_capability": "testing",
        },
    )

    store = EpisodeStore(reflection_window_path=window_path, trajectory_path=traj_path)
    _setup_episode(store, episode_id="ep-001")
    _close(store)

    loaded = load_reflection_window(window_path)
    assert loaded is not None
    assert loaded.status == STATUS_COLLECTING
    assert loaded.episode_count == 1
    assert loaded.episodes == ["ep-001"]
    assert loaded.started_at_episode == "ep-001"
    assert loaded.trajectory_snapshot["target_domain"] == "python"
    assert loaded.trajectory_snapshot["target_capability"] == "testing"
    assert loaded.trajectory_snapshot.get("trajectory_id")
    assert store.window_ready is False


def test_reflection_window_locks_at_ten_without_a_stage_gate(tmp_path, monkeypatch):
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    journal_path = tmp_path / "episode_journal.jsonl"

    monkeypatch.setattr(
        "pressure_loop.policy.load_policy",
        lambda: {"pressure": {"slice5_omega_front": False}},
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.reflection_window_path",
        lambda: window_path,
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.current_trajectory_path",
        lambda: traj_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    store = EpisodeStore(reflection_window_path=window_path, trajectory_path=traj_path)

    for i in range(1, WINDOW_SIZE + 1):
        _setup_episode(store, episode_id=f"ep-{i:03d}")
        _close(store)

    # Phase 4 cut 10: the locked window no longer STAYS in the collecting file —
    # it is double-buffered into durable staging the instant it fills, and the
    # collecting half is dropped so the next close starts a fresh window. What
    # the assertions below still pin is unchanged: at the tenth close there is a
    # locked, ten-episode window waiting for reflection, and the store hands it
    # to whoever asks. Only WHICH file holds it moved.
    from pressure_loop.reflection_window import load_staged_windows, staged_window_path

    assert load_reflection_window(window_path) is None
    staged = load_staged_windows(staged_window_path(window_path))
    assert len(staged) == 1
    assert staged[0].episode_count == WINDOW_SIZE
    assert staged[0].is_ready_for_reflection()
    assert store.get_locked_window() is not None


def test_reflection_window_starts_a_fresh_window_at_the_eleventh_close(tmp_path, monkeypatch):
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    journal_path = tmp_path / "episode_journal.jsonl"

    monkeypatch.setattr(
        "pressure_loop.policy.load_policy",
        lambda: {"pressure": {"slice5_omega_front": False}},
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.reflection_window_path",
        lambda: window_path,
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.current_trajectory_path",
        lambda: traj_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    store = EpisodeStore(reflection_window_path=window_path, trajectory_path=traj_path)

    for i in range(1, WINDOW_SIZE + 2):
        _setup_episode(store, episode_id=f"ep-{i:03d}")
        _close(store)

    # Pre-cut-10 this test pinned the eleventh episode being DROPPED: the locked
    # window sat in the one window file and ``append_episode_to_window``'s "no
    # 11th append" rule discarded every close until the next ignition seam
    # archived it. The double buffer is exactly the fix — the eleventh close now
    # opens a fresh collecting window while the locked ten wait in staging.
    from pressure_loop.reflection_window import load_staged_windows, staged_window_path

    staged = load_staged_windows(staged_window_path(window_path))
    assert len(staged) == 1
    assert len(staged[0].episodes) == WINDOW_SIZE
    assert staged[0].episodes[-1] == f"ep-{WINDOW_SIZE:03d}"
    assert staged[0].is_ready_for_reflection()

    loaded = load_reflection_window(window_path)
    assert loaded is not None
    assert loaded.window_id != staged[0].window_id
    assert loaded.episodes == [f"ep-{WINDOW_SIZE + 1:03d}"]


def test_trajectory_snapshot_immutable_after_window_open(tmp_path, monkeypatch):
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    journal_path = tmp_path / "episode_journal.jsonl"

    monkeypatch.setattr(
        "pressure_loop.reflection_window.reflection_window_path",
        lambda: window_path,
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.current_trajectory_path",
        lambda: traj_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    current = {"target_domain": "snapshot-v1", "target_capability": "cap-v1"}
    monkeypatch.setattr(
        "pressure_loop.temporal_store.read_trajectory",
        lambda: dict(current),
    )
    store = EpisodeStore(reflection_window_path=window_path, trajectory_path=traj_path)
    _setup_episode(store, episode_id="ep-001")
    _close(store)

    current.update(target_domain="snapshot-v2", target_capability="cap-v2")
    _setup_episode(store, episode_id="ep-002")
    _close(store)

    loaded = load_reflection_window(window_path)
    assert loaded is not None
    assert loaded.trajectory_snapshot["target_domain"] == "snapshot-v1"
    assert loaded.trajectory_snapshot["target_capability"] == "cap-v1"


def test_reflection_window_persists_and_resumes(tmp_path, monkeypatch):
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    journal_path = tmp_path / "episode_journal.jsonl"

    monkeypatch.setattr(
        "pressure_loop.reflection_window.reflection_window_path",
        lambda: window_path,
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.current_trajectory_path",
        lambda: traj_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    store1 = EpisodeStore(reflection_window_path=window_path, trajectory_path=traj_path)
    for i in range(1, 4):
        _setup_episode(store1, episode_id=f"ep-{i:03d}")
        _close(store1)

    store2 = EpisodeStore(reflection_window_path=window_path, trajectory_path=traj_path)
    assert store2.get_active_window() is not None
    assert store2.get_active_window().episode_count == 3

    _setup_episode(store2, episode_id="ep-004")
    _close(store2)
    assert store2.get_active_window().episode_count == 4


def test_append_episode_to_window_unit():
    window = open_reflection_window("ep-1", trajectory_snapshot={"a": 1})
    for i in range(WINDOW_SIZE):
        window = append_episode_to_window(window, f"ep-{i}")
    assert window.status == STATUS_LOCKED
    assert len(window.episodes) == WINDOW_SIZE

    locked = append_episode_to_window(window, "ep-overflow")
    assert len(locked.episodes) == WINDOW_SIZE
    assert "ep-overflow" not in locked.episodes
