"""Tests for core.cognition_loop — pressure tick + ignition cascade."""

from __future__ import annotations

import pytest

import cognition_chain as cl
from pressure_loop.contributors import EpisodeIgnition, from_operator_message, make_contributor
from pressure_loop.episode import EpisodeStore
from core.shared_state import SharedState


class _Orch:
    def __init__(self, ss: SharedState, store: EpisodeStore):
        self.shared_state = ss
        self.episode_store = store


def _drain_lanes() -> None:
    """Wait out every dispatched cascade before the test's patches come off.

    Since cut 4 a tick SUBMITS the cascade to its lane's worker thread and
    returns, and the thread resolves ``cognition_chain.ignite_episode`` when it
    runs. A test that returns without draining can therefore have its fake
    ignite swapped back for the real one mid-flight.
    """
    from pressure_loop import pipeline_dispatch

    pipeline_dispatch.shutdown_for_tests(wait=True)
    try:
        pipeline_dispatch.raise_pending_fatal()
    except BaseException:
        pass


@pytest.fixture
def lines(tmp_path, monkeypatch):
    """Point the durable operator lines at a per-test directory (cut 7/8)."""
    import pressure_loop.cognition_queue as queue_mod

    monkeypatch.setattr(
        queue_mod, "_queue_path", lambda: tmp_path / "operator_input_queue.json"
    )
    queue_mod._reset_cache_for_tests()
    yield queue_mod
    queue_mod._reset_cache_for_tests()


def _ignition(**overrides):
    from pressure_loop import cognition_queue as queue
    c = make_contributor("operator", "operator_command", 1.0, {"message": "Learn Python", "cycle_id": "test-cycle"})
    queue.enqueue_input(c, operator_activity=False)
    c = queue.lease_next_input(0)
    base = EpisodeIgnition(
        episode_id="ep-test",
        pipeline_id=0,
        work_kind="operator_turn",
        aggregate_pressure=1.0,
        opened_by="operator_command",
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )
    if not overrides:
        return base
    return EpisodeIgnition(**{**base.__dict__, **overrides})


def test_below_threshold_no_episode(monkeypatch):
    store = EpisodeStore()
    orch = _Orch(SharedState(), store)
    monkeypatch.setattr("pressure_loop.pressure.gather_contributors", lambda ss, **k: [])
    monkeypatch.setattr("pressure_loop.pressure.evaluate", lambda *a, **k: None)
    from pressure_loop.loop import run_pressure_tick_once

    run_pressure_tick_once(orch)
    assert store.current() is None


def test_pressure_tick_raises_on_cognition_failure(monkeypatch):
    """Fail-fast: a cognition failure in the tick body must propagate (halt),
    not be swallowed and rolled into the next tick — a swallowed failure is a
    mind running on poison."""
    store = EpisodeStore()
    orch = _Orch(SharedState(), store)

    def _boom(ss, **kwargs):
        raise RuntimeError("contributor gather exploded")

    monkeypatch.setattr("pressure_loop.pressure.gather_contributors", _boom)
    from pressure_loop.loop import run_pressure_tick_once

    with pytest.raises(RuntimeError, match="contributor gather exploded"):
        run_pressure_tick_once(orch)


def test_tick_body_reports_ignited(monkeypatch):
    """_run_pressure_tick_body returns whether an episode ignited — the loop chains the
    next cycle on True (the mind keeps acting on the task) and rests on False (idle).
    This replaces the deterministic 5s auto-tick: Omega drives the autonomous cycles."""
    from pressure_loop.loop import _run_pressure_tick_body

    store = EpisodeStore()
    orch = _Orch(SharedState(), store)

    from pressure_loop.pressure import evaluate as _real_evaluate

    # idle: nothing wins -> no episode -> False (the loop will rest).
    monkeypatch.setattr("pressure_loop.pressure.gather_contributors", lambda ss, **k: [])
    monkeypatch.setattr("pressure_loop.pressure.evaluate", lambda *a, **k: None)
    assert _run_pressure_tick_body(orch) is False

    # ignited: an episode opens -> the cascade runs -> True (the loop will chain).
    monkeypatch.setattr(
        "pressure_loop.pressure.gather_contributors",
        lambda ss, **k: [make_contributor("operator", "operator_query", 1.0, {"cycle_id": "tick"})],
    )
    monkeypatch.setattr("pressure_loop.pressure.evaluate", _real_evaluate)
    monkeypatch.setattr("pressure_loop.pressure.gather_contributors",
                        lambda ss, **k: [make_contributor("operator", "operator_query", 1.0, {"cycle_id": "tick"})])
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda store, ignition: object())
    assert _run_pressure_tick_body(orch) is True
    _drain_lanes()


def test_at_threshold_cascade_order_and_close(monkeypatch):
    store = EpisodeStore()
    orch = _Orch(SharedState(), store)
    calls: list[str] = []

    monkeypatch.setattr(
        "pressure_loop.pressure.gather_contributors",
        lambda ss, **k: [make_contributor("op", "operator_command", 1.0, {})],
    )
    monkeypatch.setattr(
        "pressure_loop.pressure.evaluate",
        lambda *a, **k: _ignition(),
    )
    # This assertion covers Global A -> Meta A -> AUTO -> Meta B -> Global B ->
    # Omega ordering. Execution contracts have their own suite; these hermetic
    # stub packs are not projected into programs here.
    monkeypatch.setattr(
        "meta_loop.execution_contract.motor_programs_enabled", lambda policy=None: False
    )

    def _meta(ep):
        calls.append("META_A")
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

    def _auto(ep):
        calls.append("AUTO")
        ep.auto = {"cycle_kind": "skipped", "status": "skipped"}
        return ep.auto

    def _meta_b_front(ep):
        calls.append("META_B")
        ep.meta = dict(ep.meta or {})
        ep.meta["reflection"] = {"disposition": "skipped"}
        return ep.meta["reflection"]

    def _global_b(ep, _reflection):
        calls.append("GLOBAL_B")
        ep.global_ = dict(ep.global_ or {})
        ep.global_["outcome"] = {"summary": "ok"}
        ep.global_["omega_packet"] = {
            "episode_id": ep.episode_id,
            "intent": "LEARN",
            "action": "USE_TOOL",
            "domain": "python",
        }
        return ep.global_["omega_packet"]

    def _omega(ep):
        calls.append("OMEGA")

    monkeypatch.setattr("meta_loop.loop.run_meta_a", _meta)
    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto)
    monkeypatch.setattr("meta_loop.meta_b.run_meta_b_front", _meta_b_front)
    monkeypatch.setattr("global_loop.loop.run_global_b", _global_b)
    monkeypatch.setattr(cl, "_run_omega_episode_cognition", _omega)

    goals: list[dict] = []
    orig_global = cl._start_global_a

    def _wrap_global(store, episode, ignition, cascade_status="completed"):
        orig_global(store, episode, ignition, cascade_status)
        goals.append(dict((episode.global_ or {}).get("intent_decision") or {}))

    monkeypatch.setattr(cl, "_start_global_a", _wrap_global)
    cl.ignite_episode(store, _ignition())
    assert calls == ["META_A", "AUTO", "META_B", "GLOBAL_B", "OMEGA"]
    assert store.current() is None
    assert goals and goals[0].get("intent") == "LEARN"


def _cascade_to_omega(monkeypatch, omega_packet: dict):
    """Drive a real cascade whose Global B hands the fold the given packet.

    Everything before Omega is stubbed (the stages have their own suites); the
    REAL ``_run_omega_episode_cognition`` and the REAL terminus run, because the
    property under test is what the fold's failure does to the ORGANISM, not
    what the fold returns. Returns the carrier so the caller can read the stamp
    the close consumers read."""
    seen: dict = {}
    monkeypatch.setattr(
        "meta_loop.execution_contract.motor_programs_enabled", lambda policy=None: False
    )

    def _meta(ep):
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

    def _auto(ep):
        ep.auto = {"cycle_kind": "skipped", "status": "skipped"}
        return ep.auto

    def _meta_b_front(ep):
        ep.meta = dict(ep.meta or {})
        ep.meta["reflection"] = {"disposition": "skipped"}
        return ep.meta["reflection"]

    def _global_b(ep, _reflection):
        ep.global_ = dict(ep.global_ or {})
        ep.global_["outcome"] = {"summary": "ok"}
        ep.global_["omega_packet"] = {"episode_id": ep.episode_id, **omega_packet}
        seen["episode"] = ep
        return ep.global_["omega_packet"]

    monkeypatch.setattr("meta_loop.loop.run_meta_a", _meta)
    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto)
    monkeypatch.setattr("meta_loop.meta_b.run_meta_b_front", _meta_b_front)
    monkeypatch.setattr("global_loop.loop.run_global_b", _global_b)
    return seen


def test_empty_model_response_folds_a_failure_and_closes_the_episode(monkeypatch):
    """RISK 3 (organism deaths 2026-08-11). A model that returns nothing after
    its bounded retries is a TRANSPORT condition — unavailable worker, declined
    preflight — so no false thought entered the mind. It must fold an honest
    failure and let the episode CLOSE, not raise into the fail-hard relay.

    The close is the whole point, and not out of leniency: the operator's queued
    input was already acknowledged at admission, so a raise here takes the turn
    with it (the terminus never runs and complete_external_turn never marks it).
    Reaching the terminus with cascade_status='failed' is what resolves that turn
    honestly instead of eating it."""
    from omega_loop.episode import run as omega_run

    store = EpisodeStore()
    seen = _cascade_to_omega(
        monkeypatch,
        {"intent": "LEARN", "action": "USE_TOOL", "domain": "python"},
    )
    # The runtime itself goes silent — the real fold, its real bounded retry and
    # the real containment all run.
    monkeypatch.setattr(omega_run, "_invoke", lambda *_a, **_k: None)

    cl.ignite_episode(store, _ignition())  # must NOT raise

    episode = seen["episode"]
    assert store.current() is None, "the episode must close, not hang open"
    assert "no output" in episode.meta["omega_fold_failure"]
    assert cl._effective_cascade_status(episode, "completed") == "failed"
    # No fabricated stub: a fold that did not happen leaves no result behind.
    assert "omega_result" not in (episode.global_ or {})


def test_substrate_poison_in_the_fold_still_fails_hard(monkeypatch):
    """THE LINE HOLDS. Containing bounded fold failures must not soften genuine
    substrate corruption: a packet carrying an upstream recall_error has nothing
    trustworthy to reason over, so it still raises past the fold and the tick
    relay still halts the organism. The episode is closed on the way out
    (mirrors test_front_raises_episode_still_closed) — halting is not leaking."""
    store = EpisodeStore()
    _cascade_to_omega(
        monkeypatch,
        {
            "intent": "LEARN",
            "action": "USE_TOOL",
            "domain": "python",
            "recall_error": "read_memory timeout",
        },
    )

    with pytest.raises(RuntimeError, match="recall error upstream"):
        cl.ignite_episode(store, _ignition())
    assert store.current() is None


def test_front_raises_episode_still_closed(monkeypatch):
    store = EpisodeStore()

    def _boom(ep):
        raise RuntimeError("front failed")

    monkeypatch.setattr("meta_loop.loop.run_meta_a", _boom)

    with pytest.raises(RuntimeError, match="front failed"):
        cl.ignite_episode(store, _ignition())
    assert store.current() is None


def test_busy_pipeline_does_not_suppress_other_capacity(lines, monkeypatch):
    from pressure_loop.loop import run_pressure_tick_once
    store = EpisodeStore()
    first = store.open(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn")
    lines.enqueue_input(
        make_contributor(
            "omega", "lane_work_dispatch", 1.0,
            {"lane": "browser", "cycle_id": "later", "message": "open the requested site"},
        ),
        operator_activity=False,
    )
    monkeypatch.setattr("pressure_loop.pressure.gather_contributors", lambda *_a, **_k: [])
    opened = []
    monkeypatch.setattr(cl, "ignite_episode", lambda store, ignition: opened.append(ignition.pipeline_id) or object())
    run_pressure_tick_once(_Orch(SharedState(), store))
    _drain_lanes()
    assert opened == [1]
    assert store.current(pipeline_id=0) is first
