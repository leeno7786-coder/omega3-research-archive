"""Status contracts across AUTO, Omega, episode close, and DeepSleep."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import cognition_chain as chain
from deepsleep_loop._deepsleep_state import DeepSleepInput, _episode_entry_success
from pressure_loop.episode import _prior_episode_success
from schema.continuity_json import CanonicalEpisodeUnavailable


def test_omega_fold_failure_closes_the_same_episode_as_failed(monkeypatch):
    episode = SimpleNamespace(
        episode_id="ep-ten",
        meta={},
        auto={},
        global_={},
    )
    store = MagicMock()
    store.is_tenth_episode_window_reflection.return_value = True

    def _meta_b(ep):
        ep.meta["reflection"] = {}
        return {}

    def _fail_omega(ep):
        ep.meta["omega_fold_failure"] = "invalid contract"

    monkeypatch.setattr("meta_loop.meta_b.run_meta_b_front", _meta_b)
    monkeypatch.setattr("global_loop.loop.run_global_b", lambda *_a, **_k: {})
    monkeypatch.setattr(chain, "_run_skill_foldback", lambda *_a, **_k: None)
    monkeypatch.setattr(chain, "_refresh_packet_wip_from_execution_report", lambda *_a, **_k: None)
    monkeypatch.setattr(chain, "_run_omega_episode_cognition", _fail_omega)
    monkeypatch.setattr(chain, "_execute_pending_workspace_port", lambda *_a, **_k: None)
    chain._start_meta_b_global_b(store, episode, cascade_status="completed")

    assert not hasattr(episode, "_window_reflection_requested")
    store.is_tenth_episode_window_reflection.assert_not_called()
    assert chain._effective_cascade_status(episode, "completed") == "failed"


def test_the_ignition_seam_no_longer_archives_a_window_unreflected():
    """Phase 4 cut 10 deleted ``_archive_locked_reflection_window`` and both of
    its call sites (ignition seam + terminus). Its whole job was filing a locked
    window away UNREFLECTED — the amnesia this cut ends. The store now
    double-buffers a window into durable staging the moment it locks, so it never
    holds a locked window for anyone to archive, and the window lane reflects the
    staged one instead (tests/core/test_window_lane.py).

    Pinned as an absence because the function returning is what the mind's
    ten-episode memory depends on: a future edit that reinstates it would
    silently go back to throwing every window away."""
    assert not hasattr(chain, "_archive_locked_reflection_window")
    assert not hasattr(chain, "_recover_locked_window_after_close_failure")


def test_pressure_pause_does_not_skip_mandatory_foldback(monkeypatch):
    episode = SimpleNamespace(
        episode_id="ep-paused",
        pipeline_id=0,
        work_kind="autonomous",
        meta={},
        auto={"status": "success", "outputs": {}},
        global_={},
        pressure={"contributors": []},
    )
    store = MagicMock()
    # WO-06: the typed population; an empty one yields the labelled 0.5 prior.
    from pressure_loop.episode import OutcomePopulation

    store.recent_outcome_population.return_value = OutcomePopulation()

    monkeypatch.setattr("auto_loop.auto.run_auto_front", lambda *_a, **_k: object())
    monkeypatch.setattr(
        "autonomic.symphony_neuromod.compute_episode_neuromod",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        "autonomic.symphony_neuromod.set_current_memory_salience",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "autonomic.neuromod_reinforce.reinforce_recalled_memories",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(chain, "_deliver_chat_reply_at_auto", lambda *_a, **_k: None)
    monkeypatch.setattr(chain, "_comprehend_and_ingest_fetched", lambda *_a, **_k: None)
    monkeypatch.setattr("pressure_loop.pressure_loop_pause.is_pressure_loop_paused", lambda: True)
    folded: list[str] = []
    monkeypatch.setattr(
        chain,
        "_start_meta_b_global_b",
        lambda *_a, **_k: folded.append("meta_b"),
    )

    chain._start_auto(store, episode, cascade_status="completed")

    assert folded == ["meta_b"]
    assert "meta_b_foldback_deferred" not in episode.meta
    assert chain._effective_cascade_status(episode, "completed") == "completed"


def test_incomplete_cascade_cannot_count_as_success():
    # The axes the WO-06 producer records when the cascade deferred before Meta B gave a verdict.
    deferred = {
        "cascade_status": "deferred",
        "outcome": {
            "status": "success",
            "outcome": {"goal_state": "completed"},
        },
        "outcome_axes": {"cascade_status": "deferred", "task_outcome": "unknown"},
    }

    assert _prior_episode_success(deferred) is False
    assert _episode_entry_success(deferred) is False

    ds_input = DeepSleepInput(
        window_id="ep-paused",
        window_reflection={},
        episode_window=[deferred],
        trajectory_snapshot={},
    )
    assert ds_input.auto_outcome["status"] == "partial"
    assert ds_input.global_outcome["outcome"]["goal_state"] == "partial"


def test_completed_cascade_retains_success_accounting():
    """WO-06: success is the recorded TASK outcome, never cascade completion. A completed cascade
    whose task axis records success still counts; a row with no axes (written before WO-06)
    does not, whatever its flat status says. WO-08: DeepSleep's credit helper reads the same
    axis, and refuses a row without axes as a migration requirement rather than guessing."""
    completed = {
        "cascade_status": "completed",
        "outcome": {"status": "success"},
        "outcome_axes": {"cascade_status": "completed", "task_outcome": "success"},
    }
    legacy = {"cascade_status": "completed", "outcome": {"status": "success"}}

    assert _prior_episode_success(completed) is True
    assert _prior_episode_success(legacy) is False
    assert _episode_entry_success(completed) is True
    with pytest.raises(CanonicalEpisodeUnavailable, match="migration required"):
        _episode_entry_success(legacy)
