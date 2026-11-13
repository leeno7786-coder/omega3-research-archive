"""Remediation P1 — fail-fast / correctness fixes (RED-first)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from pressure_loop.contributors import Contributor, EpisodeIgnition
from tests.helpers.pipeline_ownership import bind_ignition


def test_cascade_exception_propagates_over_store_close():
    """Cascade failure must not be masked by store.close() in the finally block."""
    from cognition_chain import ignite_episode

    store = MagicMock()
    # Phase 4 cut 13: the ignition seam holds THIS LANE's cognition lock and
    # fails hard if the opened carrier rides a different one. A mock carrier
    # has to name its lane like a real one, or the mismatch guard (correctly)
    # fires before the cascade this test is about ever starts.
    episode = MagicMock(
        episode_id="ep-mask", pipeline_id=0, work_kind="operator_turn"
    )
    store.open.return_value = episode
    store.current.return_value = episode
    store.consume_window_ready.return_value = None
    store.get_locked_window.return_value = None

    cascade_exc = RuntimeError("cascade failed")

    def _close_raises(*_args, **_kwargs):
        raise ValueError("store.close failed")

    store.close.side_effect = _close_raises

    ignition = EpisodeIgnition(
        episode_id="ign-mask",
        aggregate_pressure=0.5,
        opened_by="operator_command",
        dominant_contributor=Contributor(
            source="operator",
            type="operator_command",
            weight=0.5,
            metadata={},
        ),
        contributors=(),
        trajectory_snapshot=None,
    )

    with patch("cognition_chain._start_global_a", side_effect=cascade_exc):
        with pytest.raises(RuntimeError, match="cascade failed"):
            ignite_episode(store, bind_ignition(ignition))


def test_corrupt_reflection_window_crashes(tmp_path):
    """Corrupt reflection_window.json must crash, not silently degrade to None."""
    from pressure_loop.reflection_window import load_reflection_window

    window_path = tmp_path / "reflection_window.json"
    window_path.write_text("{not valid json!!!", encoding="utf-8")

    with pytest.raises(RuntimeError, match="corrupted reflection window"):
        load_reflection_window(window_path)


def test_corrupt_reflection_window_crashes_episode_store_init(tmp_path):
    """EpisodeStore must propagate corrupted reflection_window.json (fail-fast)."""
    from pressure_loop.episode import EpisodeStore

    window_path = tmp_path / "reflection_window.json"
    window_path.write_text("{invalid json!!", encoding="utf-8")

    with pytest.raises(RuntimeError, match="corrupted reflection window"):
        EpisodeStore(reflection_window_path=window_path)


def test_episode_store_init_without_reflection_window_file(tmp_path):
    """Absent reflection_window.json is a normal boot (no crash)."""
    from pressure_loop.episode import EpisodeStore

    store = EpisodeStore(reflection_window_path=tmp_path / "missing.json")
    assert store.consume_window_ready() is None
