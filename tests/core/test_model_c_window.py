"""Model C MC-0/MC-1 — window checkpoints reference canonical episodes."""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import CognitiveEpisode, EpisodeStore
from pressure_loop.episode_journal import enriched_episode_from_episode
from pressure_loop.reflection_window import (
    ReflectionWindow,
    append_episode_to_window,
    load_reflection_window,
    load_staged_windows,
    open_reflection_window,
    save_reflection_window,
)
from memory.api import QuerySpec, read_memory


def _close_episode_with_window_rules(store: EpisodeStore, ep: CognitiveEpisode) -> None:
    """Close while staging episode 10 inline (slice5-on production rule)."""
    if store.is_tenth_episode_window_reflection():
        store.stage_episode_for_window_reflection(ep, cascade_status="completed")
    _close_with_window_commit(store)


def _close_with_window_commit(store, **kwargs):
    """Close the way the DeepSleep terminus does (Phase 4 cut 13).

    The reflection-window append moved out of ``EpisodeStore.close`` into the
    terminus's serialized commit region, which ``close`` reaches through
    ``before_finalize``. A bare ``close()`` no longer appends, so a window
    assertion after one would be testing a close production never performs.
    """
    return store.close(
        before_finalize=lambda ep, body: store.commit_reflection_window_append(ep, body),
        **kwargs,
    )


def test_reflection_window_does_not_serialize_episode_bodies(tmp_path):
    body = {
        "episode_id": "ep-001",
        "intent": "LEARN",
        "action": "USE_TOOL",
        "outcome": {"status": "success"},
    }
    window = ReflectionWindow(
        window_id="win-test",
        started_at_episode="ep-001",
        trajectory_snapshot={},
        episodes=["ep-001"],
        episode_bodies=[body],
    )
    payload = window.to_dict()
    assert "episode_bodies" not in payload
    assert "episode_mastery_deltas" not in payload
    restored = ReflectionWindow.from_dict(payload)
    assert restored.episode_bodies == []
    assert restored.episode_mastery_deltas == []


def test_save_load_reflection_window_persists_only_episode_references(tmp_path, monkeypatch):
    from pressure_loop import reflection_window as rw

    window_path = tmp_path / "reflection_window.json"
    monkeypatch.setattr(rw, "reflection_window_path", lambda: window_path)

    body = {"episode_id": "ep-002", "intent": "IDLE"}
    window = ReflectionWindow(
        window_id="win-persist",
        started_at_episode="ep-002",
        trajectory_snapshot={},
        episodes=["ep-002"],
        episode_bodies=[body],
    )
    save_reflection_window(window, window_path)
    persisted = window_path.read_text(encoding="utf-8")
    assert "episode_bodies" not in persisted
    assert "episode_mastery_deltas" not in persisted
    loaded = load_reflection_window(window_path)
    assert loaded is not None
    assert loaded.episodes == ["ep-002"]
    assert loaded.episode_bodies == []


@pytest.mark.parametrize("legacy_field", ["episode_bodies", "episode_mastery_deltas"])
def test_reflection_checkpoints_reject_legacy_episode_state(tmp_path, legacy_field):
    """The control checkpoint must not silently accept the retired side store."""
    row = {
        "window_id": "win-legacy",
        "started_at_episode": "ep-legacy",
        "trajectory_snapshot": {},
        "episodes": ["ep-legacy"],
        "status": "collecting",
        legacy_field: [{"episode_id": "ep-legacy"}],
    }
    active_path = tmp_path / "reflection_window.json"
    staged_path = tmp_path / "reflection_window_staged.json"
    active_path.write_text(json.dumps(row), encoding="utf-8")
    staged_path.write_text(json.dumps([row]), encoding="utf-8")

    with pytest.raises(RuntimeError, match="legacy episode state"):
        load_reflection_window(active_path)
    with pytest.raises(RuntimeError, match="legacy episode state"):
        load_staged_windows(staged_path)


@pytest.mark.parametrize("active_root", [[], "not-an-object", None])
def test_active_reflection_checkpoint_rejects_wrong_json_root_shape(tmp_path, active_root):
    """Corrupt control state must not be reported as an idle reflection lane."""
    active_path = tmp_path / "reflection_window.json"
    active_path.write_text(json.dumps(active_root), encoding="utf-8")

    with pytest.raises(RuntimeError, match="reflection window root"):
        load_reflection_window(active_path)


@pytest.mark.parametrize("staged_root", [{}, ["not-a-window"]])
def test_staged_reflection_checkpoint_rejects_wrong_json_root_shape(tmp_path, staged_root):
    """Corrupt staged control state must not be reported as an empty FIFO."""
    staged_path = tmp_path / "reflection_window_staged.json"
    staged_path.write_text(json.dumps(staged_root), encoding="utf-8")

    with pytest.raises(RuntimeError, match="reflection window root"):
        load_staged_windows(staged_path)


def test_open_reflection_window_snapshots_lobe9_trajectory():
    from memory.episode_sram import close_bucket, open_bucket
    from pressure_loop.temporal_store import write_trajectory

    open_bucket("model-c-trajectory", pipeline_id=0)
    write_trajectory({"target_domain": "lobe9-marker", "target_capability": "snap"})
    close_bucket("model-c-trajectory")
    window = open_reflection_window("ep-001")
    assert window.trajectory_snapshot.get("target_domain") == "lobe9-marker"
    assert window.trajectory_snapshot.get("target_capability") == "snap"


def test_enriched_episode_carries_real_fields(tmp_path, monkeypatch):
    from pressure_loop import open_questions as oq

    questions_path = tmp_path / "omega_open_questions.jsonl"
    monkeypatch.setattr(oq, "OPEN_QUESTIONS_PATH", questions_path)

    oq.record_open_question(
        curiosity_id="cur-1",
        act="ASK",
        question_text="What is the capital of France?",
        asked_cycle="cycle-1",
    )

    ep = CognitiveEpisode(
        episode_id="ep-enriched-1",
        opened_by="curiosity_gap",
        pressure={"aggregate": 0.82, "dominant": "curiosity_gap"},
        global_={
            "action_goal": {"intent": "LEARN"},
            "outcome": {"status": "success", "detail": "learned"},
        },
        meta={"execution_action_pack": {"action": "USE_TOOL", "tool": "web_fetch"}},
        brain_turns=[{"turn": 1, "role": "user", "text": "hello"}],
        cascade_status="completed",
    )

    body = enriched_episode_from_episode(ep)

    assert body["episode_id"] == "ep-enriched-1"
    assert body["intent"] == "LEARN"
    assert body["action"] == "USE_TOOL"
    assert body["outcome"] == {"status": "success", "detail": "learned"}
    assert body["pressure"] == {"aggregate": 0.82, "dominant": "curiosity_gap"}
    assert body["execution"] == {
        "execution_action_pack": {"action": "USE_TOOL", "tool": "web_fetch"},
        "state": "open",
    }
    assert body["brain_turns"] == [{"turn": 1, "role": "user", "text": "hello"}]
    assert body["cascade_status"] == "completed"
    assert len(body["open_query_state"]) == 1
    assert body["open_query_state"][0]["question_text"] == "What is the capital of France?"
    assert body["open_query_state"][0]["status"] == "open"
    assert "mastery_deltas" not in body


def test_close_persists_only_episode_reference_to_window(tmp_path, monkeypatch):
    from pressure_loop import open_questions as oq

    journal_path = tmp_path / "episode_journal.jsonl"
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    questions_path = tmp_path / "omega_open_questions.jsonl"

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
    monkeypatch.setattr(oq, "OPEN_QUESTIONS_PATH", questions_path)

    oq.record_open_question(
        curiosity_id="cur-2",
        act="ASK",
        question_text="Why is the sky blue?",
        asked_cycle="cycle-2",
    )

    store = EpisodeStore(
        reflection_window_path=window_path,
        trajectory_path=traj_path,
    )
    store.open(
        pressure={"aggregate": 0.55},
        opened_by="curiosity_gap",
        pipeline_id=0,
        work_kind="autonomous",
    )
    ep = store.current()
    assert ep is not None
    ep.global_ = {
        "action_goal": {"intent": "RESOLVE_QUERY"},
        "outcome": {"status": "partial"},
    }
    ep.meta = {"execution_action_pack": {"action": "RESPOND"}}
    ep.brain_turns = [{"turn": 1, "role": "omega", "text": "thinking"}]

    closed = _close_with_window_commit(store)
    assert closed is ep

    rw = load_reflection_window(window_path)
    assert rw is not None
    assert rw.episodes == [closed.episode_id]
    assert rw.episode_bodies == []
    persisted = window_path.read_text(encoding="utf-8")
    assert "episode_bodies" not in persisted


def test_close_does_not_write_lobe9(tmp_path, monkeypatch):
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
    store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
    ep = store.current()
    assert ep is not None
    ep.global_ = {
        "action_goal": {"intent": "LEARN"},
        "outcome": {"status": "success"},
    }
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}

    closed = _close_with_window_commit(store)
    assert closed is not None

    result = read_memory(
        QuerySpec(
            cycle_id="mc1-close-no-lobe9",
            text=closed.episode_id,
            allowed_lobes=[9],
            allowed_subdomains=[1],
            limit=20,
        )
    )
    assert result.candidates == []


def test_consume_window_ready_returns_maintained_window_at_ten(tmp_path, monkeypatch):
    from pressure_loop.reflection_window import WINDOW_SIZE, STATUS_LOCKED

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
    episode_ids: list[str] = []
    for _ in range(WINDOW_SIZE):
        store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
        ep = store.current()
        assert ep is not None
        ep.global_ = {
            "action_goal": {"intent": "LEARN"},
            "outcome": {"status": "success"},
        }
        ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}
        episode_ids.append(ep.episode_id)
        _close_episode_with_window_rules(store, ep)

    window = store.consume_window_ready()
    assert window is not None
    assert window.status == STATUS_LOCKED
    assert window.episodes == episode_ids
    assert window.episode_bodies == []


def test_consume_window_ready_none_before_ten(tmp_path, monkeypatch):
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
    store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
    _close_with_window_commit(store)
    assert store.consume_window_ready() is None


def test_archive_and_reset_clears_window(tmp_path, monkeypatch):
    from pressure_loop.reflection_window import WINDOW_SIZE

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
        _close_episode_with_window_rules(store, ep)

    window = store.consume_window_ready()
    assert window is not None
    store.archive_and_reset_window(window, omega_outcome={})
    assert store.consume_window_ready() is None
    assert store.get_active_window() is None


def test_fresh_window_after_archive_and_ten_more_closes(tmp_path, monkeypatch):
    from pressure_loop.reflection_window import WINDOW_SIZE

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
        _close_episode_with_window_rules(store, ep)

    window1 = store.consume_window_ready()
    assert window1 is not None
    store.archive_and_reset_window(window1, omega_outcome={})

    second_ids: list[str] = []
    for _ in range(WINDOW_SIZE):
        store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
        ep = store.current()
        assert ep is not None
        ep.global_ = {
            "action_goal": {"intent": "IDLE"},
            "outcome": {"status": "success"},
        }
        ep.meta = {"execution_action_pack": {"action": "IDLE"}}
        second_ids.append(ep.episode_id)
        _close_episode_with_window_rules(store, ep)

    window2 = store.consume_window_ready()
    assert window2 is not None
    assert window2.window_id != window1.window_id
    assert window2.episodes == second_ids


def test_write_episode_to_temporal_dict_stores_full_enriched_body():
    """MC-6: enriched body dict must land in lobe-9 text lossless (not thin PriorEpisode)."""
    import json

    from memory.engine.walker import (
        COORD_INDEX,
        list_ids_in_subdomain,
        load_coord_index,
        load_meta_by_path,
    )
    from memory.episode_sram import close_bucket, open_bucket
    from pressure_loop.temporal_store import write_episode_to_temporal

    enriched = {
        "episode_id": "ep-mc6-full",
        "intent": "LEARN",
        "action": "USE_TOOL",
        "outcome": {"status": "success", "detail": "learned"},
        "pressure": {"aggregate": 0.82, "dominant": "curiosity_gap"},
        "execution": {
            "execution_action_pack": {"action": "USE_TOOL", "tool": "web_fetch"},
            "state": "open",
        },
        "brain_turns": [{"turn": 1, "role": "user", "text": "hello"}],
        "open_query_state": [{"question_text": "What is the capital of France?", "status": "open"}],
        "cascade_status": "completed",
        "closed_at": "2026-06-07T12:00:00+00:00",
    }

    open_bucket(enriched["episode_id"], pipeline_id=0)
    write_episode_to_temporal(enriched)
    close_bucket(enriched["episode_id"])
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

    assert stored is not None, "expected lobe-9 record for enriched episode"
    assert stored.get("pressure") == enriched["pressure"]
    assert stored.get("execution") == enriched["execution"]
    assert stored.get("open_query_state") == enriched["open_query_state"]
    assert stored.get("brain_turns") == enriched["brain_turns"]
