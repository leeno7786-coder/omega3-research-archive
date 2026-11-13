"""Slice 4a — closed episodes persist as lobe-9 (temporal) records."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from memory.api import QuerySpec, read_memory
from memory.engine.walker import COORD_INDEX, load_coord_index, load_meta_by_path
from pressure_loop.episode import EpisodeStore
from pressure_loop.episode_journal import journal_window, prior_episode_from_episode
from pressure_loop.reflection_window import load_reflection_window


def _closed_episode(*, episode_id: str = "ep-temporal-1") -> SimpleNamespace:
    return SimpleNamespace(
        episode_id=episode_id,
        pipeline_id=0,
        work_kind="operator_turn",
        opened_by="operator_command",
        closed_at="2026-06-07T12:00:00+00:00",
        cascade_status="completed",
        global_={
            "action_goal": {"intent": "LEARN"},
            "outcome": {"status": "success"},
        },
        meta={"execution_action_pack": {"action": "USE_TOOL"}},
        omega={},
    )


def _candidate_text(candidate) -> str:
    entry = COORD_INDEX.get(candidate.id) or {}
    path = entry.get("path")
    if not path:
        raise AssertionError(f"no path for candidate {candidate.id!r}")
    meta = load_meta_by_path(path)
    return str(meta.get("text") or "")


def test_write_episode_to_temporal_creates_lobe9_record():
    from memory.episode_sram import close_bucket, open_bucket
    from pressure_loop.temporal_store import write_episode_to_temporal

    ep = _closed_episode()
    open_bucket(ep.episode_id, pipeline_id=ep.pipeline_id)
    write_episode_to_temporal(ep)
    close_bucket(ep.episode_id)
    load_coord_index()

    open_bucket("slice4a-write", pipeline_id=0)
    result = read_memory(
        QuerySpec(
            cycle_id="slice4a-write",
            text=ep.episode_id,
            allowed_lobes=[9],
            allowed_subdomains=[1],
            limit=20,
        )
    )
    close_bucket("slice4a-write")
    assert result.candidates, "expected a lobe-9 candidate after temporal write"

    episode_candidates = [
        c
        for c in result.candidates
        if c.coord[0] == 9 and (c.title == ep.episode_id or ep.episode_id in (c.summary or ""))
    ]
    assert episode_candidates, f"no episode candidate in {result.candidates!r}"

    cand = episode_candidates[0]
    assert cand.coord[0] == 9
    meta = load_meta_by_path((COORD_INDEX.get(cand.id) or {}).get("path"))
    assert meta.get("type") == "episode"
    parsed = json.loads(_candidate_text(cand))
    assert parsed["episode_id"] == ep.episode_id
    assert parsed["intent"] == "LEARN"
    assert parsed["action"] == "USE_TOOL"


def test_write_episode_roundtrip_lossless():
    from memory.episode_sram import close_bucket, open_bucket
    from pressure_loop.temporal_store import write_episode_to_temporal

    ep = _closed_episode(episode_id="ep-temporal-roundtrip")
    expected = prior_episode_from_episode(ep).to_dict()

    open_bucket(ep.episode_id, pipeline_id=ep.pipeline_id)
    write_episode_to_temporal(ep)
    close_bucket(ep.episode_id)
    load_coord_index()

    open_bucket("slice4a-roundtrip", pipeline_id=0)
    result = read_memory(
        QuerySpec(
            cycle_id="slice4a-roundtrip",
            text=ep.episode_id,
            allowed_lobes=[9],
            allowed_subdomains=[1],
            limit=20,
        )
    )
    close_bucket("slice4a-roundtrip")
    cand = next(c for c in result.candidates if c.coord[0] == 9)
    parsed = json.loads(_candidate_text(cand))
    assert parsed == expected


def test_write_episode_preserves_canonical_body_larger_than_general_memory_preview():
    """Canonical temporal bodies must remain parseable past the 4K recall preview cap."""
    from memory.episode_sram import close_bucket, open_bucket
    from pressure_loop.temporal_store import write_episode_to_temporal

    episode = {
        "episode_id": "ep-temporal-large-body",
        "closed_at": "2026-09-05T18:42:15+00:00",
        "cascade_status": "completed",
        "response_text": "x" * 5000,
    }

    open_bucket(episode["episode_id"], pipeline_id=0)
    write_episode_to_temporal(episode)
    close_bucket(episode["episode_id"])
    load_coord_index()

    matches = []
    for memory_id, entry in COORD_INDEX.items():
        meta = load_meta_by_path(entry.get("path"))
        if meta.get("type") == "episode" and meta.get("title") == episode["episode_id"]:
            matches.append((memory_id, meta))

    assert matches, "expected the large canonical episode to be committed"
    stored = str(matches[0][1].get("text") or "")
    assert len(stored) > 4000
    assert json.loads(stored) == episode


# WO-09 retired ``test_hydrate_reflection_window_reads_bodies_and_mastery_from_canonical``: it
# pinned the id-scan hydration (every lobe-9 document naming an episode id, latest by clock).
# A window now hydrates only the exact CanonicalRefs its commit region pinned; bodies and mastery
# effects from exact canonical records are covered by
# tests/continuity/test_window_hydration.py::test_restore_carries_the_manifest_records_and_bodies_with_their_canonical_refs.


def test_episode_store_close_checkpoints_id_and_preserves_jsonl_window(
    tmp_path,
    monkeypatch,
):
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
    store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")
    ep = store.current()
    assert ep is not None
    ep.global_ = {
        "action_goal": {"intent": "LEARN"},
        "outcome": {"status": "success"},
    }
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}

    # Phase 4 cut 13: the reflection-window append moved into the DeepSleep
    # commit region, which close reaches through before_finalize. This test
    # asserts on the window, so it runs the seam the terminus runs.
    closed = store.close(
        before_finalize=lambda ep, body: store.commit_reflection_window_append(ep, body)
    )
    assert closed is ep

    window = journal_window(50, path=journal_path)
    assert len(window) == 1
    assert window[0].episode_id == closed.episode_id

    rw = load_reflection_window(window_path)
    assert rw is not None
    assert closed.episode_id in list(getattr(rw, "episodes", []) or [])
    assert rw.episode_bodies == []

    load_coord_index()
    result = read_memory(
        QuerySpec(
            cycle_id="slice4a-close-integration",
            text=closed.episode_id,
            allowed_lobes=[9],
            allowed_subdomains=[1],
            limit=20,
        )
    )
    assert result.candidates == []
