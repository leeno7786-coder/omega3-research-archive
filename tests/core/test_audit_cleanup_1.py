"""Original-audit cleanup #1 — fixes A-F from work_order audit-cleanup-1."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from pressure_loop.episode import EpisodeStore
from pressure_loop.loop import run_pressure_tick_once
from pressure_loop.reflection_window import (
    STATUS_LOCKED,
    WINDOW_SIZE,
    ReflectionWindow,
    append_episode_to_window,
    open_reflection_window,
)


class _BrokenSharedState:
    def write(self, key: object, value: object) -> None:
        raise RuntimeError("shared-state write failed")


def test_pressure_tick_raises_on_shared_state_write_failure(monkeypatch):
    """Fail-fast: a SharedState write failure HALTS the tick (raises) rather than
    being logged-and-continued — corrupt cognition bookkeeping cannot proceed."""
    runtime = SimpleNamespace(
        episode_store=SimpleNamespace(),
        shared_state=_BrokenSharedState(),
    )
    monkeypatch.setattr(
        "pressure_loop.loop._run_pressure_tick_body",
        lambda _runtime: None,
    )

    with pytest.raises(RuntimeError, match="shared-state write failed"):
        run_pressure_tick_once(runtime)


def test_concurrent_window_append_is_thread_safe(tmp_path, monkeypatch):
    """Fix C: reflection-window append is lock-protected under concurrency."""
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    monkeypatch.setattr(
        "pressure_loop.reflection_window.reflection_window_path",
        lambda: window_path,
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.current_trajectory_path",
        lambda: traj_path,
    )
    store = EpisodeStore(
        reflection_window_path=window_path,
        trajectory_path=traj_path,
    )
    errors: list[BaseException] = []

    def append_once(episode_id: str) -> None:
        try:
            store._append_episode_to_reflection_window(
                episode_id,
                inline_omega_staging=True,
            )
        except BaseException as exc:  # noqa: BLE001 - collect thread errors
            errors.append(exc)

    threads = [
        threading.Thread(target=append_once, args=(f"ep-{idx}",)) for idx in range(WINDOW_SIZE)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    # Phase 4 cut 10: the tenth append locks the window and double-buffers it
    # into durable staging, so the COLLECTING half is empty again and the locked
    # ten are read through ``get_locked_window``. What this test pins is
    # unchanged — ten concurrent appends produce exactly ten distinct episodes in
    # one locked window, with no lost update.
    assert store.get_active_window() is None
    window = store.get_locked_window()
    assert window is not None
    assert len(window.episodes) == WINDOW_SIZE
    assert len(set(window.episodes)) == WINDOW_SIZE
    assert window.status == STATUS_LOCKED


def test_open_while_active_fails_closed_and_preserves_original_episode():
    """A second open in the SAME lane must never erase the live episode carrier.

    Phase 3 cut 2: carriers are per-lane — a chat episode opening while a meta
    episode runs is legal concurrency (tests/core/test_episode_store_lanes.py);
    same-lane admission still fails closed exactly as the single-carrier store did."""
    store = EpisodeStore()
    store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
    first = store.current()

    with pytest.raises(RuntimeError, match="cannot open pipeline 0"):
        store.open(opened_by="idle_probe", pipeline_id=0, work_kind="autonomous")

    assert store.current() is first


def test_episode_count_derives_from_episodes_list():
    """Fix F: episode_count tracks len(episodes) and cannot drift."""
    window = open_reflection_window("ep-start")
    assert window.episode_count == 0

    for idx in range(WINDOW_SIZE - 1):
        window = append_episode_to_window(window, f"ep-{idx}")
        assert window.episode_count == idx + 1

    window = append_episode_to_window(window, f"ep-{WINDOW_SIZE - 1}")
    assert window.episode_count == WINDOW_SIZE
    assert window.is_ready_for_reflection()

    # Direct mutation of episodes must update derived count (no stale cache).
    window.episodes.append("ep-extra-should-not-happen")
    assert window.episode_count == len(window.episodes)


def test_from_dict_ignores_stale_episode_count_field():
    """Fix F: persisted episode_count cannot override len(episodes)."""
    window = ReflectionWindow.from_dict(
        {
            "window_id": "w-stale",
            "started_at_episode": "ep-1",
            "episode_count": 99,
            "trajectory_snapshot": {},
            "episodes": ["ep-1", "ep-2"],
            "status": "collecting",
        }
    )
    assert window.episode_count == 2
