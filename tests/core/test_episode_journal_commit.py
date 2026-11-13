"""Episode journal commit ordering and durable writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from pressure_loop.durable_write import append_jsonl_line
from pressure_loop.episode import EpisodeStore
from pressure_loop.episode_journal import archive_episode


def test_append_jsonl_line_fsyncs(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "journal.jsonl"
    fsync_calls: list[int] = []

    monkeypatch.setattr(
        "pressure_loop.durable_write.os.fsync",
        lambda fd: fsync_calls.append(fd),
    )
    append_jsonl_line(path, '{"ok":true}')
    assert path.read_text(encoding="utf-8") == '{"ok":true}\n'
    assert fsync_calls


def test_archive_episode_fail_hard(tmp_path, monkeypatch):
    journal_path = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    class _Ep:
        episode_id = "ep-1"
        opened_by = "test"
        opened_at = "2026-01-01T00:00:00Z"
        closed_at = "2026-01-01T00:01:00Z"
        cascade_status = "completed"
        pressure = {}
        global_ = {}
        meta = {}
        auto = {}
        omega = {}
        sensory = []
        brain_turns = []

    archive_episode(_Ep())
    assert journal_path.is_file()

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(
        "pressure_loop.durable_write.append_jsonl_line",
        _boom,
    )
    with pytest.raises(OSError, match="disk full"):
        archive_episode(_Ep())


def test_close_leaves_no_active_episode(tmp_path, monkeypatch):
    journal_path = tmp_path / "episode_journal.jsonl"
    window_path = tmp_path / "reflection_window.json"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )
    store = EpisodeStore(
        reflection_window_path=window_path,
        trajectory_path=tmp_path / "trajectory.json",
    )
    ep = store.open(
        opened_by="test", pressure={}, pipeline_id=0, work_kind="autonomous"
    )
    assert ep is not None
    ep.global_ = {"outcome": {"outcome": {"goal_state": "idle"}}}
    ep.meta = {"execution_action_pack": {"action": "IDLE"}}
    ep.auto = {"status": "idle", "cycle_kind": "idle"}
    store.close()
    assert store.current() is None
    assert journal_path.read_text(encoding="utf-8").strip()
