"""Remediation P2-A — robustness fixes (RED-first)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import close_episode_with_window_rules

from runtime.runtime_capability_broker import (
    RuntimeCapability,
    RuntimeCapabilityRoute,
    RuntimeCapabilityState,
    RuntimeCapabilityStatus,
    RuntimeOperationDecision,
    RuntimeOperationReceipt,
)


def test_consume_window_ready_clears_flag(tmp_path, monkeypatch):
    """consume_window_ready must clear _window_ready when handing off a locked window."""
    from pressure_loop.reflection_window import WINDOW_SIZE
    from pressure_loop.episode import EpisodeStore

    journal_path = tmp_path / "episode_journal.jsonl"
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"

    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )
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
    for _ in range(WINDOW_SIZE):
        store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
        ep = store.current()
        assert ep is not None
        ep.global_ = {
            "action_goal": {"intent": "LEARN"},
            "outcome": {"status": "success"},
        }
        ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}
        close_episode_with_window_rules(store, ep)

    assert store.window_ready is True
    window = store.consume_window_ready()
    assert window is not None
    assert store.window_ready is False


def test_cognition_runtime_fails_fast_when_unreachable(monkeypatch):
    """Unreachable runtime must fail-fast via health probe, not wait for 90s load loop."""
    import runtime.model_control as mc

    def _slow_load(*_args, **_kwargs):
        time.sleep(5)
        return {"ok": False, "error": "should not reach load loop"}

    monkeypatch.setattr(mc, "load_model", _slow_load)
    monkeypatch.setattr(mc, "_probe_runtime_reachable", lambda _base: False)

    start = time.monotonic()
    with pytest.raises(mc.RuntimeCognitionUnavailable, match="not reachable"):
        mc.ensure_cognition_runtime(model="gemma_e2b", base_url="http://x")
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"expected fail-fast, took {elapsed:.1f}s"
