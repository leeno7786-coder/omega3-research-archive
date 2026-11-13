"""Regression guards for the shared four-seat cognitive substrate."""

from __future__ import annotations

import queue
import threading

import pytest

from core.pipelines import PIPELINE_IDS, PIPELINE_TO_LANE
from pressure_loop import pipeline_dispatch
from pressure_loop.episode import EpisodeStore
from tests.core.test_pipeline_dispatch import _ignition


@pytest.fixture(autouse=True)
def isolated_dispatch(monkeypatch):
    """Reuse the dispatch suite's isolated physical-worker boundary."""
    pipeline_dispatch.shutdown_for_tests(wait=True)
    monkeypatch.setattr(pipeline_dispatch, "mark_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_dispatch, "mark_failed", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_dispatch, "notify_work", lambda: None)
    yield
    pipeline_dispatch.shutdown_for_tests(wait=True)


def test_blueprint_binds_each_semantic_lane_to_one_physical_pipeline():
    assert PIPELINE_IDS == (0, 1, 2, 3)
    assert PIPELINE_TO_LANE == {0: "chat", 1: "browser", 2: "agent", 3: "window"}


def test_four_dedicated_lane_episodes_overlap_without_duplicate_ownership(monkeypatch):
    """Each lane may overlap with the others; an occupied lane cannot be re-dispatched."""
    store = EpisodeStore()
    entered = queue.Queue()
    releases = {pipeline_id: threading.Event() for pipeline_id in PIPELINE_IDS}

    def ignite(episode_store, ignition):
        episode = episode_store.open(
            opened_by="operator_query",
            pipeline_id=ignition.pipeline_id,
            work_kind=ignition.work_kind,
        )
        entered.put((episode.pipeline_id, episode.work_kind))
        assert releases[episode.pipeline_id].wait(5)
        episode_store.close()
        return episode

    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    try:
        work_kinds = ("operator_turn", "operator_lane_work", "agent_continuation", "reflection")
        for pipeline_id in PIPELINE_IDS:
            ignition = _ignition(pipeline_id)
            ignition.work_kind = work_kinds[pipeline_id]
            pipeline_dispatch.dispatch_cascade(
                store,
                ignition,
                ignition.dominant_contributor,
                pipeline_id=pipeline_id,
                work_kind=work_kinds[pipeline_id],
                on_not_admitted=lambda: None,
            )

        assert sorted(entered.get(timeout=2) for _ in PIPELINE_IDS) == [
            (0, "operator_turn"),
            (1, "operator_lane_work"),
            (2, "agent_continuation"),
            (3, "reflection"),
        ]
        assert {episode.pipeline_id for episode in store.active_episodes()} == set(PIPELINE_IDS)

        occupied = _ignition(0)
        with pytest.raises(RuntimeError, match="pipeline 0 is unavailable"):
            pipeline_dispatch.dispatch_cascade(
                store,
                occupied,
                occupied.dominant_contributor,
                pipeline_id=0,
                work_kind="operator_turn",
                on_not_admitted=lambda: None,
            )
    finally:
        for release in releases.values():
            release.set()
