"""Episode outcome honesty — failed cascades carry cascade_status into journal/window."""

from __future__ import annotations

import pytest

import cognition_chain as cl
from pressure_loop.cognition_queue import deserialize_input
from pressure_loop.contributors import EpisodeIgnition
from pressure_loop.episode import EpisodeStore
from pressure_loop.episode_journal import journal_window, prior_episode_from_episode
from pressure_loop.reflection_window import load_reflection_window
import pressure_loop.operator_chat_responses as ocr


def _ignition(**overrides):
    c = deserialize_input(
        {
            "source": "operator",
            "type": "operator_command",
            "weight": 1.0,
            "metadata": {
                "message": "Learn Python",
                "arrival_seq": 1,
                "cycle_id": "ep-test",
            },
            "queue_id": "test-episode-outcome",
            "work_kind": "operator_turn",
        }
    )
    base = EpisodeIgnition(
        episode_id="ep-test",
        aggregate_pressure=1.0,
        opened_by="operator_command",
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
        pipeline_id=0,
        work_kind="operator_turn",
    )
    if not overrides:
        return base
    return EpisodeIgnition(**{**base.__dict__, **overrides})


def _journal_setup(tmp_path, monkeypatch):
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    journal_path = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )
    store = EpisodeStore(reflection_window_path=window_path, trajectory_path=traj_path)
    return store, window_path, journal_path


def test_global_a_none_crashes_recording_failed(tmp_path, monkeypatch):
    store, _window_path, journal_path = _journal_setup(tmp_path, monkeypatch)
    monkeypatch.setattr("global_loop.loop.run_global_a", lambda ep, ign: None)

    with pytest.raises(RuntimeError, match="Global A produced no IntentDecision"):
        cl.ignite_episode(store, _ignition())

    assert store.current() is None
    window = journal_window(1, path=journal_path)
    assert len(window) == 1
    assert window[0].cascade_status == "failed"


def test_global_a_failure_resolves_waiting_chat_with_a_failed_terminal_reply(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ocr, "_store_path", lambda: tmp_path / "responses.json")
    ocr._reset_cache_for_tests()
    store, _window_path, _journal_path = _journal_setup(tmp_path, monkeypatch)
    ignition = _ignition()
    ignition.dominant_contributor.metadata["chat_bubble_waiting"] = True
    monkeypatch.setattr("global_loop.loop.run_global_a", lambda _ep, _ign: None)

    with pytest.raises(RuntimeError, match="Global A produced no IntentDecision"):
        cl.ignite_episode(store, ignition)

    record = ocr.get_response("ep-test")
    assert record is not None
    assert "I could not complete this turn" in record["text"]
    assert "Global A produced no IntentDecision" in record["text"]
    assert record["delivery"]["failed"] is True
    ocr._reset_cache_for_tests()


def test_auto_raises_cascade_failed_exception_propagates(tmp_path, monkeypatch):
    store, window_path, journal_path = _journal_setup(tmp_path, monkeypatch)

    def _global_ok(ep, ign):
        ep.global_ = dict(ep.global_ or {})
        ep.global_["action_goal"] = {"intent": "LEARN", "episode_id": ep.episode_id}
        return ep.global_["action_goal"]  # non-None = Global A produced an ActionGoal

    monkeypatch.setattr("global_loop.loop.run_global_a", _global_ok)

    def _meta_ok(ep):
        ep.meta = dict(ep.meta or {})
        ep.meta["execution_action_pack"] = {
            "episode_id": ep.episode_id,
            "action": "USE_TOOL",
            "tool": "web_fetch",
            "routing": {},
            "memory_scope": "general",
            "prompt_type": "tool",
            "model_profile": "TEXT_FAST",
            "constraints": {},
        }
        return ep.meta["execution_action_pack"]

    monkeypatch.setattr("meta_loop.loop.run_meta_a", _meta_ok)

    def _auto_boom(ep):
        raise RuntimeError("auto failed")

    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto_boom)

    with pytest.raises(RuntimeError, match="auto failed"):
        cl.ignite_episode(store, _ignition())

    assert store.current() is None
    pe = journal_window(1, path=journal_path)[0]
    assert pe.cascade_status == "failed"

    loaded = load_reflection_window(window_path)
    assert loaded is not None
    assert loaded.cascade_status_for(pe.episode_id) == "failed"


def test_idle_cascade_completes_with_cascade_completed(tmp_path, monkeypatch):
    store, window_path, journal_path = _journal_setup(tmp_path, monkeypatch)

    def _global_idle(ep, ign):
        ep.global_ = dict(ep.global_ or {})
        ep.global_["action_goal"] = {"intent": "IDLE", "episode_id": ep.episode_id}
        ep.global_["outcome"] = {"summary": "No execution performed"}
        return ep.global_["action_goal"]  # IDLE still produces an ActionGoal → cascade completes

    monkeypatch.setattr("global_loop.loop.run_global_a", _global_idle)

    def _meta_idle(ep):
        ep.meta = dict(ep.meta or {})
        ep.meta["execution_action_pack"] = {
            "episode_id": ep.episode_id,
            "action": "IDLE",
            "tool": "",
            "routing": {},
            "memory_scope": "general",
            "prompt_type": "idle",
            "model_profile": "TEXT_FAST",
            "constraints": {},
        }
        return ep.meta["execution_action_pack"]

    monkeypatch.setattr("meta_loop.loop.run_meta_a", _meta_idle)

    def _auto_idle(ep):
        ep.auto = {"cycle_kind": "skipped"}
        return ep.auto

    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto_idle)
    # The verdict the real Meta B writes for a skipped cycle. WO-06's outcome extractor reads
    # it at close, and an execution block with no result is refused rather than guessed at.
    monkeypatch.setattr(
        "meta_loop.meta_b.run_meta_b_front",
        lambda ep: (
            setattr(ep, "meta", {"reflection": {"execution": {
                "executed": False, "result": "none", "status": "skipped",
                "disposition": "skipped"}}})
            or ep.meta["reflection"]
        ),
    )
    def _global_b(ep, _reflection):
        ep.global_ = dict(ep.global_ or {})
        ep.global_["omega_packet"] = {
            "episode_id": ep.episode_id,
            "intent": "IDLE",
            "action": "IDLE",
            "domain": "",
        }
        return ep.global_["omega_packet"]

    monkeypatch.setattr("global_loop.loop.run_global_b", _global_b)
    monkeypatch.setattr(cl, "_run_omega_episode_cognition", lambda _ep: None)

    cl.ignite_episode(store, _ignition())

    pe = journal_window(1, path=journal_path)[0]
    assert pe.cascade_status == "completed"

    loaded = load_reflection_window(window_path)
    assert loaded is not None
    assert loaded.cascade_status_for(pe.episode_id) == "completed"


def test_prior_episode_from_episode_exposes_cascade_status():
    from types import SimpleNamespace

    ep = SimpleNamespace(
        episode_id="ep-1",
        opened_by="operator_command",
        pressure={},
        global_={"action_goal": {"intent": "LEARN"}, "outcome": {"status": "success"}},
        meta={"execution_action_pack": {"action": "USE_TOOL"}},
        omega={},
        cascade_status="failed",
    )
    pe = prior_episode_from_episode(ep)
    assert pe.cascade_status == "failed"


def test_episode_store_close_sets_cascade_status_on_episode():
    store = EpisodeStore()
    store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")
    ep = store.current()
    assert ep is not None
    ep.global_ = {"action_goal": {"intent": "LEARN"}, "outcome": {"status": "success"}}
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}

    closed = store.close(cascade_status="failed")
    assert closed is not None
    assert closed.cascade_status == "failed"
