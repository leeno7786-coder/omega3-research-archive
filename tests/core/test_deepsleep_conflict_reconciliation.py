"""DeepSleep activates conflict reconciliation as required memory maintenance.

The reconciliation chain runs before the one graph reindex so tombstones are
visible in the same episode's recall projection. A fault blocks finalization.
"""

from __future__ import annotations

import pytest

from deepsleep_loop.loop import (
    _run_conflict_reconciliation,
    run_deepsleep_for_episode,
)
from memory import episode_sram
from pressure_loop.episode import EpisodeStore
from tests.core.deepsleep_window_fixtures import (
    active_store_from_episode_body,
    episode_entry,
    patch_window_deepsleep_paths,
)


def _learn_body(eid: str = "ep-cr-1") -> dict:
    return episode_entry(eid, intent="LEARN", target="Python", success=True, knowledge_mastery=0.10)


def test_reconciliation_failure_propagates(monkeypatch):
    def _boom():
        raise RuntimeError("queue exploded")

    monkeypatch.setattr("memory.contradiction_resolution.process_contradiction_candidates", _boom)
    with pytest.raises(RuntimeError, match="queue exploded"):
        _run_conflict_reconciliation(context_id="x", scope="episode")


def test_reconciliation_helper_passes_report_through(monkeypatch):
    sentinel = {"groups_processed": 2, "tombstoned_count": 1, "demoted_count": 0}
    monkeypatch.setattr(
        "memory.contradiction_resolution.process_contradiction_candidates",
        lambda: sentinel,
    )
    assert _run_conflict_reconciliation(context_id="x", scope="episode") is sentinel


def test_episode_core_runs_reconciliation(tmp_path, monkeypatch):
    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "memory.contradiction_resolution.process_contradiction_candidates",
        lambda: calls.append("ep") or {"groups_processed": 0},
    )

    body = _learn_body("ep-cr-1")
    store, episode = active_store_from_episode_body(body)
    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    run_deepsleep_for_episode(store)

    assert calls == ["ep"]
