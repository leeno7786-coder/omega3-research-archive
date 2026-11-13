from __future__ import annotations

from types import SimpleNamespace

import pytest

from pressure_loop.episode import EpisodeStore
from pressure_loop.episode_journal import journal_window


def _setup_episode(store: EpisodeStore, *, episode_id: str | None = None) -> object:
    ep = store.current()
    if ep is None:
        raise RuntimeError("no active episode after open()")
    if episode_id:
        ep.episode_id = episode_id
    # Minimal slots needed by the PriorEpisode extractor.
    ep.global_ = {
        "action_goal": {"intent": "LEARN"},
        "outcome": {"status": "success"},
    }
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}
    ep.omega = {}
    return ep


def test_episode_store_close_archives_episode_failure_soft(tmp_path, monkeypatch):
    journal_path = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    store = EpisodeStore()
    store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")
    ep = _setup_episode(store)

    closed = store.close()
    assert closed is ep
    assert store.current() is None

    window = journal_window(50, path=journal_path)
    assert len(window) == 1
    assert window[0].episode_id == closed.episode_id
    assert window[0].intent == "LEARN"
    assert window[0].action == "USE_TOOL"
    assert window[0].trajectory_update is None
    assert "omega_reflection" not in window[0].to_dict()


def test_episode_store_close_archive_failure_is_non_fatal(tmp_path, monkeypatch):
    journal_path = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    import pressure_loop.episode_journal as epj

    def _boom(_ep, path=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(epj, "archive_episode", _boom)

    store = EpisodeStore()
    store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")
    ep = _setup_episode(store)

    closed = store.close()
    assert closed is ep
    assert store.current() is None


def test_episode_store_close_failure_keeps_active_episode_for_retry(tmp_path, monkeypatch):
    journal_path = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    import pressure_loop.episode_journal as epj

    real_enrich = epj.enriched_episode_from_episode
    calls = 0

    def _fail_once(ep):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic enrichment failure")
        return real_enrich(ep)

    monkeypatch.setattr(epj, "enriched_episode_from_episode", _fail_once)

    store = EpisodeStore()
    store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")
    ep = _setup_episode(store, episode_id="ep-retry-close")

    with pytest.raises(RuntimeError, match="synthetic enrichment failure"):
        store.close()
    assert store.current() is ep

    closed = store.close()
    assert closed is ep
    assert store.current() is None
    assert [row.episode_id for row in journal_window(50, path=journal_path)] == ["ep-retry-close"]


def test_reflection_window_save_failure_rolls_back_for_close_retry(tmp_path, monkeypatch):
    """Phase 4 cut 13 moved the append into the DeepSleep commit region, which
    ``close`` reaches through ``before_finalize`` — so the append is one frame
    deeper than it used to be and the rollback contract is unchanged: a failed
    window save leaves the SAME carrier active and retryable, and the retry
    lands the episode exactly once."""
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: tmp_path / "episode_journal.jsonl",
    )
    import pressure_loop.reflection_window as rw

    real_save = rw.save_reflection_window
    calls = 0

    def _fail_once(window, path=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic reflection save failure")
        return real_save(window, path)

    monkeypatch.setattr(rw, "save_reflection_window", _fail_once)

    store = EpisodeStore(
        reflection_window_path=tmp_path / "reflection_window.json",
    )
    store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")
    ep = _setup_episode(store, episode_id="ep-reflection-retry")

    def _commit(episode, body):
        store.commit_reflection_window_append(episode, body)

    with pytest.raises(OSError, match="synthetic reflection save failure"):
        store.close(before_finalize=_commit)
    assert store.current() is ep

    assert store.close(before_finalize=_commit) is ep
    assert calls == 2
    assert store.episode_in_reflection_window("ep-reflection-retry") is True


def test_closed_tenth_stages_the_locked_window_for_the_window_lane(tmp_path, monkeypatch):
    """Phase 4 cut 10: the tenth close still makes the ten-episode window durable
    and reflection-ready — it now lands in the STAGING half of the double buffer
    instead of staying in the collecting file, so the eleventh close is not
    dropped while the reflection runs."""
    from pressure_loop.reflection_window import (
        load_reflection_window,
        load_staged_windows,
        staged_window_path,
    )

    path = tmp_path / "reflection_window.json"
    store = EpisodeStore(reflection_window_path=path)
    for index in range(9):
        store._append_episode_to_reflection_window(
            f"ep-{index}",
            body={"episode_id": f"ep-{index}"},
            inline_omega_staging=True,
        )

    store._append_episode_to_reflection_window(
        "ep-10",
        body={"episode_id": "ep-10"},
    )

    assert load_reflection_window(path) is None  # the collecting half swapped out
    staged = load_staged_windows(staged_window_path(path))
    assert len(staged) == 1
    assert "ep-10" in staged[0].episodes
    assert staged[0].is_ready_for_reflection()
    assert store.get_locked_window() is not None
