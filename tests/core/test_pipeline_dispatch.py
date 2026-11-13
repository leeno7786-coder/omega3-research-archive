import queue
import threading
import time
from types import SimpleNamespace

import pytest

from pressure_loop import pipeline_dispatch


@pytest.fixture(autouse=True)
def isolated_dispatch(monkeypatch):
    pipeline_dispatch.shutdown_for_tests(wait=True)
    monkeypatch.setattr(pipeline_dispatch, "mark_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_dispatch, "mark_failed", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_dispatch, "notify_work", lambda: None)
    yield
    pipeline_dispatch.shutdown_for_tests(wait=True)


def _ignition(pid):
    return SimpleNamespace(pipeline_id=pid, work_kind="operator_turn",
                           dominant_contributor=SimpleNamespace(metadata={
                               "arrival_seq": pid + 1,
                               "lane": ("chat", "browser", "agent", "window")[pid],
                           }))


def test_dispatch_rejects_a_contributor_bound_to_another_semantic_lane(monkeypatch):
    from pressure_loop.contributors import make_contributor

    contributor = make_contributor("operator", "operator_query", 1.0, {"arrival_seq": 1})
    ignition = SimpleNamespace(
        pipeline_id=1,
        work_kind="operator_turn",
        dominant_contributor=contributor,
    )
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda *_: object())

    with pytest.raises(ValueError, match="semantic lane"):
        pipeline_dispatch.dispatch_cascade(
            None,
            ignition,
            contributor,
            pipeline_id=1,
            work_kind="operator_turn",
            on_not_admitted=lambda: None,
        )


def test_worker_failure_between_tick_check_and_capacity_snapshot_serves_the_other_lanes(monkeypatch):
    """The failure lands after the tick's fatal check and before capacity is read.

    The pass must still see three assignable pipelines and admit the waiting turns on
    them. Only the pipeline that actually failed is withheld, and its FAILED envelope is
    the durable record of why.
    """
    from dataclasses import replace
    from core import pipeline_state
    from core.shared_state import SharedState
    from pressure_loop import cognition_queue as cq, loop, pressure
    from pressure_loop.contributors import make_contributor

    entered, fail, finished = threading.Event(), threading.Event(), threading.Event()
    primary = RuntimeError("cascade failed during gather")
    submissions, leases, release_ownership = [], [], []
    monkeypatch.setattr(pipeline_dispatch, "mark_ready", pipeline_state.mark_ready)
    monkeypatch.setattr(pipeline_dispatch, "mark_failed", pipeline_state.mark_failed)
    monkeypatch.setattr(pipeline_dispatch, "notify_work", finished.set)

    def ignite(store, ignition):
        submissions.append(ignition)
        if ignition.pipeline_id != 0:
            return object()
        entered.set()
        assert fail.wait(5)
        raise primary

    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    cq.enqueue_input(make_contributor("operator", "operator_query", 1,
        {"arrival_seq": 1, "cycle_id": "fatal-race-1"}), operator_activity=False)
    cq.enqueue_input(make_contributor("omega", "browser_work", 1,
        {"arrival_seq": 2, "cycle_id": "fatal-race-2", "lane": "browser"}),
        operator_activity=False)
    row = cq.lease_next_input(0)
    ignition = replace(pressure.evaluate([row], active_episode=False),
                       pipeline_id=0, work_kind="operator_turn")

    def release_claim():
        release_ownership.append(pipeline_dispatch.pipeline_in_flight(0))
        cq.release_input_lease(0)

    pipeline_dispatch.dispatch_cascade(None, ignition, row, pipeline_id=0,
        work_kind="operator_turn", on_not_admitted=release_claim)
    assert entered.wait(2)

    def gather(*args, **kwargs):
        # The tick's initial fatal check passed, but capacity has not been read.
        fail.set()
        assert finished.wait(2)
        return []

    original_lease = cq.lease_next_input
    monkeypatch.setattr(pressure, "gather_contributors", gather)
    monkeypatch.setattr(cq, "lease_next_input",
                        lambda pid: leases.append(pid) or original_lease(pid))
    try:
        # The Browser turn progresses even though Chat is quarantined; only pipeline 0
        # is withheld by the failure.
        assert loop._run_pressure_tick_body(
            SimpleNamespace(episode_store=None, shared_state=SharedState()))
        assert leases == [1, 2, 3]
        assert release_ownership == [True]
        deadline = time.monotonic() + 2
        while len(submissions) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        assert sorted(sub.pipeline_id for sub in submissions) == [0, 1]
        # The failure is preserved where it can be read after the fact, on both rails.
        assert pipeline_state.read_envelope(0).state == "FAILED"
        assert pipeline_dispatch.pipeline_faults() == {0: primary}
        # ...and it is not a process-lifetime latch.
        for _ in range(2):
            pipeline_dispatch.raise_pending_fatal()
            assert 0 not in pipeline_dispatch.free_pipeline_ids()
        # Even a caller holding an earlier capacity snapshot cannot publish READY on it.
        with pytest.raises(RuntimeError) as caught:
            pipeline_dispatch.dispatch_cascade(None, ignition, row, pipeline_id=0,
                work_kind="operator_turn", on_not_admitted=lambda: None)
        assert caught.value is primary
        assert pipeline_state.read_envelope(0).state == "FAILED"
    finally:
        fail.set()


def test_shutdown_reports_submitted_cascade_before_its_thread_registers(monkeypatch):
    blocked, release = threading.Event(), threading.Event()
    def before_cascade():
        blocked.set()
        assert release.wait(5)
    pipeline_dispatch._executor_for(0).submit(before_cascade)
    assert blocked.wait(2)
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda *_: object())
    ignition = _ignition(0)
    try:
        pipeline_dispatch.dispatch_cascade(None, ignition, ignition.dominant_contributor,
            pipeline_id=0, work_kind="operator_turn", on_not_admitted=lambda: None)
        report = pipeline_dispatch.shutdown_pipelines(timeout_s=.02)
        assert report["still_running"] == [0]
        assert report["waited_for"] == [0]
    finally:
        release.set()


def test_four_same_kind_cascades_overlap_and_first_freed_pipeline_reopens(monkeypatch):
    entered = queue.Queue()
    releases = {pid: threading.Event() for pid in range(4)}

    def ignite(store, ignition):
        entered.put(ignition.pipeline_id)
        assert releases[ignition.pipeline_id].wait(5)
        return object()

    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    try:
        for pid in range(4):
            ignition = _ignition(pid)
            pipeline_dispatch.dispatch_cascade(None, ignition, ignition.dominant_contributor,
                pipeline_id=pid, work_kind="operator_turn", on_not_admitted=lambda: None)
        assert sorted(entered.get(timeout=2) for _ in range(4)) == [0, 1, 2, 3]
        assert pipeline_dispatch.free_pipeline_ids() == ()
        releases[2].set()
        deadline = time.monotonic() + 2
        while 2 not in pipeline_dispatch.free_pipeline_ids() and time.monotonic() < deadline:
            time.sleep(.01)
        assert pipeline_dispatch.free_pipeline_ids() == (2,)
    finally:
        for event in releases.values():
            event.set()


def test_ready_failure_never_starts_worker_and_releases_claim(monkeypatch):
    calls = []
    def fail(*args):
        raise OSError("ready persistence failed")
    monkeypatch.setattr(pipeline_dispatch, "mark_ready", fail)
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda *_: calls.append("ignite"))
    ignition = _ignition(0)
    with pytest.raises(OSError, match="ready persistence"):
        pipeline_dispatch.dispatch_cascade(None, ignition, ignition.dominant_contributor,
            pipeline_id=0, work_kind="operator_turn", on_not_admitted=lambda: calls.append("release"))
    assert calls == ["release"]
    assert pipeline_dispatch.free_pipeline_ids() == (0, 1, 2, 3)


def test_admission_exception_resolves_originating_chat_bubble(monkeypatch):
    delivered = threading.Event()
    records = []

    def append_response(**kwargs):
        records.append(kwargs)
        delivered.set()
        return kwargs

    monkeypatch.setattr(
        "pressure_loop.operator_chat_responses.append_response", append_response
    )
    monkeypatch.setattr(
        "cognition_chain.ignite_episode",
        lambda *_args: (_ for _ in ()).throw(OSError("episode store unavailable")),
    )
    contributor = SimpleNamespace(
        metadata={
            "cycle_id": "failed-before-admission",
            "chat_bubble_waiting": True,
            "arrival_seq": 1,
            "lane": "chat",
        }
    )
    ignition = SimpleNamespace(
        pipeline_id=0,
        work_kind="operator_command",
        dominant_contributor=contributor,
    )
    pipeline_dispatch.dispatch_cascade(
        None,
        ignition,
        contributor,
        pipeline_id=0,
        work_kind="operator_command",
        on_not_admitted=lambda: None,
    )

    assert delivered.wait(2), "admission failures must close the waiting chat bubble"
    assert records[0]["cycle_id"] == "failed-before-admission"
    assert records[0]["delivery"] == {"failed": True}
    assert records[0]["work_kind"] == "operator_command"
    assert "episode store unavailable" in records[0]["text"]


def test_not_admitted_releases_before_publishing_free_worker(monkeypatch):
    released = threading.Event()
    observed = []
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda *_: None)
    def release():
        observed.append(pipeline_dispatch.pipeline_in_flight(0))
        released.set()
    ignition = _ignition(0)
    pipeline_dispatch.dispatch_cascade(None, ignition, ignition.dominant_contributor,
        pipeline_id=0, work_kind="operator_turn", on_not_admitted=release)
    assert released.wait(2)
    assert observed == [True]


def test_fifo_fifth_waits_for_whole_cascade_after_exact_ack(monkeypatch):
    from pressure_loop import cognition_queue as cq, loop, pressure
    from pressure_loop.contributors import make_contributor
    from pressure_loop.episode import EpisodeStore
    from core.shared_state import SharedState
    store = EpisodeStore()
    runtime = SimpleNamespace(episode_store=store, shared_state=SharedState())
    entered = queue.Queue()
    releases = {pid: threading.Event() for pid in range(4)}
    monkeypatch.setattr(pressure, "gather_contributors", lambda *_a, **_k: [])
    def ignite(store, ignition):
        pid = ignition.pipeline_id
        contributor = ignition.dominant_contributor
        episode = store.open(opened_by=ignition.opened_by, pipeline_id=pid,
                             work_kind=ignition.work_kind)
        assert cq.acknowledge_input(contributor, pid)
        entered.put((pid, contributor.metadata["arrival_seq"]))
        assert releases[pid].wait(5)
        store.close()
        return episode
    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    lanes = ("chat", "browser", "agent", "window", "browser")
    sources = ("operator", "omega", "omega", "omega", "omega")
    for seq, (lane, source) in enumerate(zip(lanes, sources, strict=True), start=1):
        contributor_type = "operator_query" if lane == "chat" else f"{lane}_work"
        cq.enqueue_input(make_contributor(source, contributor_type, 1,
            {"arrival_seq": seq, "cycle_id": f"turn-{seq}", "lane": lane}),
            operator_activity=False)
    try:
        assert loop._run_pressure_tick_body(runtime)
        assert sorted(entered.get(timeout=2) for _ in range(4)) == [(0, 1), (1, 2), (2, 3), (3, 4)]
        assert cq.queue_depth() == 1
        assert len(store.active_episodes()) == 4
        assert {ep.pipeline_id for ep in store.active_episodes()} == {0, 1, 2, 3}
        assert not loop._run_pressure_tick_body(runtime)
        releases[1].set()
        deadline = time.monotonic() + 2
        while 1 not in pipeline_dispatch.free_pipeline_ids() and time.monotonic() < deadline:
            time.sleep(.01)
        assert loop._run_pressure_tick_body(runtime)
        assert entered.get(timeout=2) == (1, 5)
    finally:
        for event in releases.values():
            event.set()


def test_standing_internal_signal_is_not_requeued_after_ack(monkeypatch):
    from pressure_loop import cognition_queue as cq, loop, pressure
    from pressure_loop.contributors import make_contributor
    from core.shared_state import SharedState
    internal = make_contributor("omega", "curiosity_gap", 1,
                                {"signal_id": "standing", "lane": "chat"})
    monkeypatch.setattr(pressure, "gather_contributors", lambda *_a, **_k: [internal])
    loop._STAGED_INTERNAL.clear()
    entered, release = threading.Event(), threading.Event()
    def ignite(store, ignition):
        assert cq.acknowledge_input(ignition.dominant_contributor, ignition.pipeline_id)
        entered.set()
        assert release.wait(5)
        return object()
    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    runtime = SimpleNamespace(episode_store=None, shared_state=SharedState())
    try:
        assert loop._run_pressure_tick_body(runtime)
        assert entered.wait(2)
        assert cq.queue_depth() == 0
        for _ in range(3):
            assert not loop._run_pressure_tick_body(runtime)
        assert pipeline_dispatch.free_pipeline_ids() == (1, 2, 3)
    finally:
        release.set()
        loop._STAGED_INTERNAL.clear()


def test_restored_work_kind_survives_subsequent_dispatch(monkeypatch):
    from dataclasses import replace
    from core import pipeline_state
    from core.shared_state import SharedState
    from pressure_loop import cognition_queue as cq, loop, pressure
    from pressure_loop.contributors import make_contributor
    contributor = make_contributor(
        "omega", "curiosity_gap", 1, {"signal_id": "restored", "lane": "agent"}
    )
    cq.enqueue_input(contributor, operator_activity=False)
    contributor = cq.lease_next_input(2)
    ignition = replace(pressure.evaluate([contributor], active_episode=False),
                       pipeline_id=2, work_kind="restored-special-work")
    pipeline_state.mark_ready(2, ignition)
    cq.acknowledge_input(contributor, 2)
    pipeline_state.restore_pipelines_at_boot()
    entered = queue.Queue()
    monkeypatch.setattr(pressure, "gather_contributors", lambda *_a, **_k: [])
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda store, ignition: entered.put(ignition) or object())
    assert loop._run_pressure_tick_body(SimpleNamespace(episode_store=None, shared_state=SharedState()))
    assert entered.get(timeout=2).work_kind == "restored-special-work"


def _fail_pipeline(monkeypatch, pid, failure, *, on_not_admitted=lambda: None):
    """Dispatch a cascade on ``pid`` that raises, and wait for the worker to finish."""
    done = threading.Event()
    previous = pipeline_dispatch.notify_work
    monkeypatch.setattr(pipeline_dispatch, "notify_work", lambda: (previous(), done.set()))

    def ignite(store, ignition):
        raise failure

    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    ignition = _ignition(pid)
    pipeline_dispatch.dispatch_cascade(None, ignition, ignition.dominant_contributor,
        pipeline_id=pid, work_kind="operator_turn", on_not_admitted=on_not_admitted)
    assert done.wait(2)


def test_cascade_failure_quarantines_only_its_own_pipeline(monkeypatch):
    """One lane's cascade exception must not end every conversation (demo bar B2).

    The failure is still preserved per-cycle — durably attributed through ``mark_failed``
    and retrievable from ``pipeline_faults`` — and the lane it happened on is quarantined.
    What must NOT happen is the process-lifetime latch: the other three pipelines keep
    their capacity and still carry whole turns.
    """
    failure = RuntimeError("browser lane cascade blew up")
    marked, woke = [], []
    healthy = threading.Event()

    def ignite(store, ignition):
        if ignition.pipeline_id == 1:
            raise failure
        healthy.set()
        return object()

    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    monkeypatch.setattr(pipeline_dispatch, "mark_failed", lambda pid, exc: marked.append((pid, exc)))
    monkeypatch.setattr(pipeline_dispatch, "notify_work",
                        lambda: woke.append(pipeline_dispatch.free_pipeline_ids()))
    failed = _ignition(1)
    pipeline_dispatch.dispatch_cascade(None, failed, failed.dominant_contributor,
        pipeline_id=1, work_kind="operator_turn", on_not_admitted=lambda: None)
    deadline = time.monotonic() + 2
    while not woke and time.monotonic() < deadline:
        time.sleep(.01)
    # Preserved, not swallowed: durably attributed and still readable in-process.
    assert marked == [(1, failure)]
    assert pipeline_dispatch.pipeline_faults() == {1: failure}
    # The race: the wake that republishes capacity already excludes the quarantined lane,
    # so no tick can ever observe pipeline 1 as free between the failure and the release.
    assert woke == [(0, 2, 3)]
    # The process is NOT latched.
    pipeline_dispatch.raise_pending_fatal()
    assert pipeline_dispatch.free_pipeline_ids() == (0, 2, 3)
    ok = _ignition(2)
    pipeline_dispatch.dispatch_cascade(None, ok, ok.dominant_contributor,
        pipeline_id=2, work_kind="operator_turn", on_not_admitted=lambda: None)
    assert healthy.wait(2)


def test_a_quarantined_pipeline_refuses_a_stale_capacity_snapshot(monkeypatch):
    """A caller holding capacity read before the failure must not publish READY on it."""
    failure = RuntimeError("cascade blew up")
    ready = []
    monkeypatch.setattr(pipeline_dispatch, "mark_ready", lambda *a: ready.append(a))
    _fail_pipeline(monkeypatch, 0, failure)
    del ready[:]
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda *_: object())
    ignition = _ignition(0)
    released = []
    with pytest.raises(RuntimeError) as caught:
        pipeline_dispatch.dispatch_cascade(None, ignition, ignition.dominant_contributor,
            pipeline_id=0, work_kind="operator_turn",
            on_not_admitted=lambda: released.append("lease"))
    assert caught.value is failure
    assert released == ["lease"], "a stale capacity choice must return the queue lease"
    assert ready == []
    assert pipeline_dispatch.free_pipeline_ids() == (1, 2, 3)


def test_failed_envelope_persistence_is_fail_hard_for_the_process(monkeypatch):
    """The durable FAILED write is how the failure survives; unwritten, isolation is unproven."""
    persistence = OSError("pipeline envelope write failed")

    def explode(pid, exc):
        raise persistence

    monkeypatch.setattr(pipeline_dispatch, "mark_failed", explode)
    _fail_pipeline(monkeypatch, 2, RuntimeError("cascade blew up"))
    with pytest.raises(OSError) as caught:
        pipeline_dispatch.raise_pending_fatal()
    assert caught.value is persistence
    assert pipeline_dispatch.free_pipeline_ids() == ()


def test_continuation_release_failure_is_fail_hard_for_the_process(monkeypatch):
    """The successor ledger is shared by all four pipelines, so its failure is not a lane's."""
    ledger = OSError("successor ledger write failed")

    def explode(metadata, reason):
        raise ledger

    monkeypatch.setattr("pressure_loop.pressure.release_continuation", explode)
    _fail_pipeline(monkeypatch, 3, RuntimeError("cascade blew up"))
    with pytest.raises(OSError) as caught:
        pipeline_dispatch.raise_pending_fatal()
    assert caught.value is ledger
    assert pipeline_dispatch.free_pipeline_ids() == ()


def test_queue_lease_release_failure_is_fail_hard_for_the_process(monkeypatch):
    """A lease the shared FIFO may still believe is held is not confined to one pipeline."""
    lease = OSError("queue lease release failed")
    done = threading.Event()
    monkeypatch.setattr(pipeline_dispatch, "notify_work", done.set)
    monkeypatch.setattr("cognition_chain.ignite_episode", lambda *_: None)

    def explode():
        raise lease

    ignition = _ignition(1)
    pipeline_dispatch.dispatch_cascade(None, ignition, ignition.dominant_contributor,
        pipeline_id=1, work_kind="operator_turn", on_not_admitted=explode)
    assert done.wait(2)
    with pytest.raises(OSError) as caught:
        pipeline_dispatch.raise_pending_fatal()
    assert caught.value is lease
    assert pipeline_dispatch.free_pipeline_ids() == ()


def test_quarantining_every_pipeline_escalates_to_the_process_fatal(monkeypatch):
    """With no isolated capacity left there is nothing to isolate INTO: halt honestly."""
    failures = [RuntimeError(f"cascade {pid} blew up") for pid in range(4)]
    for pid in range(4):
        _fail_pipeline(monkeypatch, pid, failures[pid])
        if pid < 3:
            pipeline_dispatch.raise_pending_fatal()
            assert pipeline_dispatch.free_pipeline_ids() == tuple(range(pid + 1, 4))
    assert pipeline_dispatch.pipeline_faults() == dict(enumerate(failures))
    with pytest.raises(RuntimeError) as caught:
        pipeline_dispatch.raise_pending_fatal()
    assert caught.value is failures[0]
    assert len(getattr(failures[0], "__notes__", [])) == 3
    assert pipeline_dispatch.free_pipeline_ids() == ()
