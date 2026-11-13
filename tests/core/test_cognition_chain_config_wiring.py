"""Cognition-chain queue and mandatory episode-closure wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

import cognition_chain as cl
import pytest
from config import loader
from pressure_loop.contributors import EpisodeIgnition, make_contributor


def _ignition():
    contributor_type = "operator_command"
    c = make_contributor("operator", contributor_type, 1.0, {"message": "hi", "cycle_id": "config-test"})
    return EpisodeIgnition(
        pipeline_id=0, work_kind="operator_turn",
        episode_id="ep-1",
        aggregate_pressure=1.0,
        opened_by=contributor_type,
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


def test_disabled_config_cannot_skip_the_cognition_cycle(monkeypatch):
    monkeypatch.setattr(
        "config.loader.load_subsystem_bundle",
        lambda _stem: {
            "config": {"cognition_chain": {"enabled": False}},
            "policy": {},
        },
    )

    store = MagicMock()
    store.get_locked_window.return_value = None
    store.open.return_value = None

    with pytest.raises(RuntimeError, match="episode open failed"):
        cl._ignite_episode_cascade(store, _ignition())


def test_episode_open_failure_is_always_hard(monkeypatch):
    monkeypatch.setattr(
        "config.loader.load_subsystem_bundle",
        lambda _stem: {
            "config": {"cognition_chain": {"fail_hard_on_episode_open": False}},
            "policy": {},
        },
    )

    store = MagicMock()
    store.get_locked_window.return_value = None
    store.open.return_value = None

    with pytest.raises(RuntimeError, match="episode open failed"):
        cl._ignite_episode_cascade(store, _ignition())


def test_episode_open_failure_hard_by_default():
    loader.clear_cache()

    store = MagicMock()
    store.get_locked_window.return_value = None
    store.open.return_value = None

    try:
        cl._ignite_episode_cascade(store, _ignition())
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "episode open failed" in str(exc)
    assert raised


def _cascade_fixture(
    tmp_path,
    monkeypatch,
    *,
    global_a_ok: bool,
    on_global_a=None,
):
    """Drive one episode cascade against an isolated operator line."""
    import pressure_loop.cognition_queue as oq

    monkeypatch.setattr(oq, "_queue_path", lambda: tmp_path / "operator_queue.json")
    oq._reset_cache_for_tests()
    ignition = _ignition()
    oq.enqueue_input(ignition.dominant_contributor, operator_activity=False)
    from dataclasses import replace
    leased = oq.lease_next_input(pipeline_id=0)
    ignition = replace(ignition, dominant_contributor=leased, contributors=(leased,))

    # Phase 4 cut 13: the ignition seam holds THIS LANE's lock and fails hard
    # if the opened carrier names a different one. The mock derives its lane the
    # same way a real EpisodeStore.open does, so operator and external ignitions
    # each get the carrier they would really get.

    episode = MagicMock(
        episode_id="ep-opened", pipeline_id=0, work_kind="operator_turn"
    )
    episode._window_reflection_requested = False
    store = MagicMock()
    store.get_locked_window.return_value = None
    store.open.return_value = episode
    store.current.return_value = episode
    monkeypatch.setattr(cl, "_attach_pending_system_events", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "_record_operator_ingress", lambda *_a, **_k: None)
    # WO-04: the seam records its origin event BEFORE the acknowledgement, so it needs the
    # real SRAM owner. The bucket is opened for the carrier under test; the queued
    # memory-envelope replay this stub skipped is still skipped.
    from memory import episode_sram

    monkeypatch.setattr(
        cl, "_open_episode_sram",
        lambda ep, **_k: episode_sram.open_bucket(ep.episode_id, pipeline_id=ep.pipeline_id),
    )
    queue_at_global_a: list[tuple] = []

    def _start_global_a(*_args, **_kwargs):
        queue_at_global_a.append(oq.peek_inputs())
        if on_global_a is not None:
            on_global_a()
        return global_a_ok

    monkeypatch.setattr(cl, "_start_global_a", _start_global_a)
    monkeypatch.setattr(cl, "_effective_cascade_status", lambda _ep, status: status)
    monkeypatch.setattr(cl, "_record_operator_outcome", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "_record_system_message", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "_record_proactive_chat", lambda *_a, **_k: None)
    pack = MagicMock()
    pack.projection_state = {"episode_sram_flush": {"memories": ["ws-memory-1"]}}
    monkeypatch.setattr(cl, "_run_per_episode_terminus", lambda *_a, **_k: pack)
    monkeypatch.setattr(cl, "_close_episode_sram", lambda *_a, **_k: None)
    # ``_apply_per_episode_mastery_delta`` used to be stubbed here. Risk 8
    # removed it: the delta now rides the commit-region window append, which the
    # stubbed ``_run_per_episode_terminus`` above never reaches.

    assert cl._ignite_episode_cascade(store, ignition) is episode
    return oq, queue_at_global_a


def test_episode_admission_consumes_the_queue_head_before_global_a(tmp_path, monkeypatch):
    """The carrier is open before Global A, so its turn has crossed the gate."""
    oq, queue_at_global_a = _cascade_fixture(tmp_path, monkeypatch, global_a_ok=True)
    assert queue_at_global_a == [()]
    assert oq.peek_inputs() == ()
    assert oq.lease_next_input(pipeline_id=0) is None  # line empty, gate open


def test_failed_cascade_does_not_requeue_a_turn_after_its_carrier_opened(tmp_path, monkeypatch):
    """Post-admission failure belongs to the opened episode, not a replayed turn."""
    oq, queue_at_global_a = _cascade_fixture(tmp_path, monkeypatch, global_a_ok=False)
    assert queue_at_global_a == [()]
    assert oq.peek_inputs() == ()
    assert oq.lease_next_input(pipeline_id=0) is None


def test_deepsleep_runs_even_when_obsolete_journal_flag_is_disabled(monkeypatch):
    monkeypatch.setattr(
        "config.loader.load_subsystem_bundle",
        lambda _stem: {
            "config": {"cognition_chain": {"per_episode": {"journal_write": False}}},
            "policy": {},
        },
    )

    episode = MagicMock(episode_id="ep-1")
    store = MagicMock()
    result = object()
    calls: list[str] = []
    monkeypatch.setattr(
        "deepsleep_loop.loop.run_deepsleep_for_episode",
        lambda *_args, **_kwargs: calls.append("DEEPSLEEP") or result,
    )

    assert cl._run_per_episode_terminus(store, episode, cascade_status="completed") is result
    assert calls == ["DEEPSLEEP"]


def test_a_staged_successor_records_its_continuation_when_the_cascade_opens_it(tmp_path, monkeypatch):
    """T11: the ignition seam commits the CONTINUED fact into the successor's own SRAM bucket
    — opened just before — from its durable contributor row, before Global A runs. The fact
    is an intake record the instant the successor ignites."""
    from memory import episode_sram, intake_journal
    from memory.causal_edges import causal_facts
    from types import SimpleNamespace

    monkeypatch.setattr(intake_journal, "journal_root", lambda: tmp_path / "intake")
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    order = []

    detail = {"lane": "agent", "omega_lane": "agent",
              "successor_id": "omega-successor-abc", "parent_episode_id": "ep-parent",
              "continued_from": {"fold_id": "fold-1", "continuation_key": "key-1"}}
    seen = {}

    def open_successor(**kwargs):
        episode = SimpleNamespace(episode_id="ep-next", pipeline_id=2, work_kind="autonomous",
                                  opened_by="omega_successor", meta={},
                                  pressure={"contributors": [{"id": "omega_successor", "detail": detail}]},
                                  _window_reflection_requested=False)
        seen["episode"] = episode
        return episode

    store = MagicMock()
    store.get_locked_window.return_value = None
    store.open.side_effect = open_successor
    store.current.side_effect = lambda: seen.get("episode")
    monkeypatch.setattr(cl, "_attach_pending_system_events", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "_record_operator_ingress", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "_open_episode_sram",
                        lambda ep: order.append("bucket") or episode_sram.open_bucket(ep.episode_id, pipeline_id=2))
    real_hook = cl._record_continuation_fact
    monkeypatch.setattr(cl, "_record_continuation_fact", lambda ep: order.append("fact") or real_hook(ep))
    monkeypatch.setattr(cl, "_start_global_a",
                        lambda *_a, **_k: seen.setdefault("facts_at_global_a", causal_facts(seen["episode"])) and False)
    monkeypatch.setattr(cl, "_effective_cascade_status", lambda _ep, status: status)
    for name in ("_record_operator_outcome", "_record_system_message", "_record_proactive_chat",
                 "_close_episode_sram"):
        monkeypatch.setattr(cl, name, lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "_run_per_episode_terminus", lambda *_a, **_k: MagicMock(projection_state={}))
    # The seam consumes the queue head it was leased: the successor is enqueued and leased
    # exactly as the pressure loop would.
    import pressure_loop.cognition_queue as oq

    monkeypatch.setattr(oq, "_queue_path", lambda: tmp_path / "queue.json")
    oq._reset_cache_for_tests()
    c = make_contributor("omega", "omega_successor", 1.0, dict(detail, cycle_id="omega-successor-abc"))
    oq.enqueue_input(c, operator_activity=False)
    leased = oq.lease_next_input(pipeline_id=2)
    ignition = EpisodeIgnition(pipeline_id=2, work_kind="autonomous", episode_id="ep-next",
                               aggregate_pressure=1.0, opened_by="omega_successor",
                               dominant_contributor=leased, contributors=(leased,),
                               trajectory_snapshot=None)

    try:
        cl._ignite_episode_cascade(store, ignition)

        [fact] = seen["facts_at_global_a"]
        assert (fact["kind"], fact["source"], fact["target"]) == (
            "continued", "episode:ep-parent", "continuation:omega-successor-abc")
        assert fact["detail"]["continuation_key"] == "key-1"
        assert order == ["bucket", "fact"], "the bucket the fact enters is opened first"
        record = intake_journal.record_by_id(fact["journal_record_id"])
        assert record.status == intake_journal.PENDING and record.owner_episode_id == "ep-next"
        # WO-04: the bucket also holds this episode's own admission events, so the assertion
        # is that the continuation fact IS held — not that it is the only item in it.
        held = [i.journal_record_id for i in episode_sram.pending("ep-next")]
        assert record.record_id in held, held
    finally:
        episode_sram.reset()
        intake_journal.reset_cache_for_tests()
