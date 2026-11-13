"""N4b-boundary — last_closed_body survives window archive; lobe-9 neuromod roundtrip."""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import EpisodeStore
from pressure_loop.reflection_window import WINDOW_SIZE, load_reflection_window
from tests.conftest import close_episode_with_window_rules

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def _close_with_neuromod(store: EpisodeStore, *, dopamine: float, episode_idx: int) -> None:
    store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
    ep = store.current()
    assert ep is not None
    ep.global_ = {
        "action_goal": {"intent": "LEARN"},
        "outcome": {"status": "success"},
    }
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}
    ep.auto = {
        "neuromod": {
            "dopamine": dopamine,
            "plasticity": abs(dopamine),
            "stress": 0.1,
            "serotonin": 0.4,
            "strength": abs(dopamine),
            "cycle_kind": "success",
        }
    }
    close_episode_with_window_rules(store, store.current())


def test_last_closed_body_survives_window_archive(tmp_path, monkeypatch):
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    journal_path = tmp_path / "episode_journal.jsonl"

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

    tenth_dopamine = 0.99
    for i in range(WINDOW_SIZE):
        dopamine = tenth_dopamine if i == WINDOW_SIZE - 1 else float(i) / WINDOW_SIZE
        _close_with_neuromod(store, dopamine=dopamine, episode_idx=i)

    window = store.consume_window_ready()
    assert window is not None
    store.archive_and_reset_window(window, omega_outcome={})

    assert load_reflection_window(window_path) is None

    last = store.last_closed_body()
    assert last is not None
    assert last["neuromod"]["dopamine"] == tenth_dopamine


def test_lobe9_neuromod_roundtrip():
    from memory.engine.walker import (
        COORD_INDEX,
        list_ids_in_subdomain,
        load_coord_index,
        load_meta_by_path,
    )
    from pressure_loop.temporal_store import write_episode_to_temporal

    neuromod = {
        "dopamine": 0.72,
        "plasticity": 0.72,
        "stress": 0.2,
        "serotonin": 0.55,
        "strength": 0.72,
        "cycle_kind": "success",
    }
    enriched = {
        "episode_id": "ep-nm-lobe9",
        "intent": "LEARN",
        "action": "USE_TOOL",
        "outcome": {"status": "success"},
        "pressure": {"aggregate": 0.5},
        "execution": {"execution_action_pack": {"action": "USE_TOOL"}},
        "brain_turns": [],
        "open_query_state": [],
        "cascade_status": "completed",
        "neuromod": neuromod,
    }

    write_episode_to_temporal(enriched)
    load_coord_index()

    stored: dict | None = None
    for idstr in list_ids_in_subdomain(9, 1):
        path = (COORD_INDEX.get(idstr) or {}).get("path")
        if not path:
            continue
        meta = load_meta_by_path(path)
        body = json.loads(meta.get("text") or "{}")
        if body.get("episode_id") == enriched["episode_id"]:
            stored = body
            break

    assert stored is not None, "expected lobe-9 record for enriched episode with neuromod"
    assert stored.get("neuromod") == neuromod
