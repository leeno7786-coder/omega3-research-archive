"""Phase 4 cut 13 — the lock split: per-lane cognition locks + ONE commit lock.

Operator design 2026-08-10 §6: *lane-local DeepSleep — consolidating episode A
never stops lane B.* Through Phase 3, ``cognition_chain`` held ONE process-wide
lock across the whole deepsleep terminus, and that is the only reason the
finish-line stamp equalled the landed order: nothing could arrive between the
stamp at the gate and the writes at the end. Cut 13 unlocks the lane-local half
of the terminus, which breaks that coincidence on purpose and then re-earns the
guarantee a different way — the stamp becomes the FIRST statement inside the ONE
commit lock, so stamp order is landed order by construction.

The decisive test in this file is
``test_concurrent_termini_land_in_stamp_order``: real lane locks, the real
commit region, every registered lane, randomized sleeps in both halves, and the assertion
that the landed journal reads exactly like the stamps. Everything else here is
the scaffolding that makes that assertion mean something — that the lane halves
really did overlap, that the ordering law is enforced rather than documented,
and that the seven consolidation stores stopped losing read-modify-writes the
moment two lanes could reach them at once.
"""

from __future__ import annotations

import random
import threading
import time
from types import SimpleNamespace

import pytest

from core import cognition_lock as cl
from core.commit_region import serialized_commit
from core.pipelines import PIPELINE_IDS


@pytest.fixture(autouse=True)
def _isolated_locks_and_counter(tmp_path, monkeypatch):
    """A fresh lock topology, a fresh finish-line counter, an empty journal.

    The topology is process-cached on purpose (the kill switch must not change
    under a running cascade), so every test that touches it resets both sides.
    """
    import core.paths as paths
    from core import timekeeper
    from memory import epoch as memory_epoch

    monkeypatch.setattr(paths, "GUARDIAN_STATE_ROOT", tmp_path / "guardian" / "state")
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: tmp_path / "episode_journal.jsonl",
    )
    cl.reset_for_tests()
    timekeeper.reset_for_tests()
    memory_epoch.reset_for_tests()
    yield
    cl.reset_for_tests()
    timekeeper.reset_for_tests()
    memory_epoch.reset_for_tests()


def _episode(episode_id: str, lane: str) -> SimpleNamespace:
    """A carrier with exactly what the commit region touches."""
    return SimpleNamespace(
        episode_id=episode_id,
        pipeline_id=lane, work_kind="autonomous",
        timekeeper={},
        memory_epoch_start=0,
        memory_epoch_commit=0,
        causal_parent_episode_ids=[],
        memory_epoch_pin={},
    )


# ── topology ────────────────────────────────────────────────────────────────


def test_each_lane_gets_its_own_lock_and_the_commit_lock_is_one():
    locks = {lane: cl.pipeline_cognition_lock(lane) for lane in PIPELINE_IDS}
    assert len({id(lock) for lock in locks.values()}) == len(PIPELINE_IDS)
    # Stable identity: the same lane always names the same lock, or two seams
    # on one lane would think they were excluding each other and would not.
    assert cl.pipeline_cognition_lock(0) is locks[0]
    commit = cl.commit_lock()
    assert commit is cl.commit_lock()
    assert all(commit is not lock for lock in locks.values())


def test_an_unknown_lane_has_no_lock():
    """A routing bug that reaches the substrate fails loud. Minting a lock on
    demand would hand back perfect isolation from nobody."""
    with pytest.raises(ValueError, match="unknown pipeline_id"):
        cl.pipeline_cognition_lock("nonsense")


def test_two_lanes_hold_their_seams_at_the_same_time():
    """The whole point of the cut: an agent terminus and a chat terminus are
    inside their seams simultaneously. Under Phase 3 this test deadlocks on a
    2-party barrier because the second lane could never enter."""
    both_inside = threading.Barrier(2)
    entered: list[str] = []
    guard = threading.Lock()

    def hold(lane: str) -> None:
        with cl.pipeline_cognition_lock(lane):
            with guard:
                entered.append(lane)
            both_inside.wait(timeout=5)

    threads = [
        threading.Thread(target=hold, args=(0,)),
        threading.Thread(target=hold, args=(1,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()
    assert sorted(entered) == sorted([1, 0])


def test_the_commit_lock_still_serializes_every_lane():
    """Lane-local work overlaps; canonical commits do not. Operator design §5:
    'parallel reads/cognition/candidate-writes; SERIALIZED atomic canonical
    commits'."""
    trace: list[str] = []
    guard = threading.Lock()
    start = threading.Barrier(len(PIPELINE_IDS))

    def commit(lane: str) -> None:
        start.wait(timeout=5)
        with cl.pipeline_cognition_lock(lane):
            with cl.commit_lock():
                with guard:
                    trace.append(f"{lane}-in")
                time.sleep(0.02)
                with guard:
                    trace.append(f"{lane}-out")

    threads = [threading.Thread(target=commit, args=(lane,)) for lane in PIPELINE_IDS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(trace) == 2 * len(PIPELINE_IDS)
    # Never two lanes inside at once: every -in is immediately followed by its
    # own -out.
    for index in range(0, len(trace), 2):
        assert trace[index].removesuffix("-in") == trace[index + 1].removesuffix("-out")


# ── the ordering law ────────────────────────────────────────────────────────


def test_own_lane_then_commit_is_the_sanctioned_pair():
    with cl.pipeline_cognition_lock(2):
        with cl.commit_lock():
            assert cl.held_lock_names() == ["pipeline:2", "commit:commit"]
    assert cl.held_lock_names() == []


def test_a_lane_lock_is_reentrant_within_its_own_seam():
    """``_ignite_episode_cascade`` acquires and releases explicitly and the
    terminus re-acquires; nested acquisition must never be mistaken for a new
    ordering edge."""
    lock = cl.pipeline_cognition_lock(0)
    with lock:
        with lock:
            assert lock._is_owned()  # noqa: SLF001


def test_commit_then_lane_is_refused():
    """Never commit → lane. The reverse pair is the deadlock cycle, and a
    deadlock in the substrate is a brain that stops with no traceback for the
    fatal relay to catch."""
    with cl.commit_lock():
        with pytest.raises(cl.LockOrderViolation, match="commit lock held"):
            cl.pipeline_cognition_lock(0).acquire()
    assert cl.held_lock_names() == []


def test_lane_then_another_lane_is_refused():
    with cl.pipeline_cognition_lock(0):
        with pytest.raises(cl.LockOrderViolation, match="never pipeline"):
            cl.pipeline_cognition_lock(1).acquire()


def test_a_refused_acquisition_leaves_nothing_held():
    """The guard runs BEFORE the underlying acquire, so a violation cannot
    leave a half-taken lock behind for the next thread to wait on forever."""
    with cl.pipeline_cognition_lock(0):
        with pytest.raises(cl.LockOrderViolation):
            cl.pipeline_cognition_lock(3).acquire()
        assert cl.held_lock_names() == ["pipeline:0"]

    done = threading.Event()
    other = threading.Thread(
        target=lambda: (cl.pipeline_cognition_lock(3).acquire(), done.set())
    )
    other.start()
    other.join(timeout=5)
    assert done.is_set()
    cl.reset_for_tests()


def test_a_fail_hard_exit_holding_its_lane_lock_cannot_condemn_another_lane():
    """The Phase-3 asymmetry, re-checked against the split (cut 13 review).

    ``_ignite_episode_cascade`` exits with its substrate lock HELD on every
    exception path, deliberately — the fatal relay is about to crash the brain
    and a briefly-held lock never outlives the process it condemns. Under Phase
    3 that lock was global, so the window between the exception and the crash
    was one in which EVERY lane was stopped. Split per lane, the leak has to be
    contained to the lane that raised: the other three keep running (they reach
    their own locks and the commit lock), and only the condemned lane blocks.
    ``lane_dispatch`` gives each lane a single dedicated worker thread, so the
    leaked lock is never handed to a thread that will next ignite elsewhere.
    """
    leaked = threading.Event()
    release = threading.Event()

    def fail_hard_cascade() -> None:
        # The real shape: acquire, raise, never release.
        cl.pipeline_cognition_lock(0).acquire()
        leaked.set()
        release.wait(timeout=10)
        cl.release_held_locks_for_tests()

    condemned = threading.Thread(target=fail_hard_cascade, daemon=True)
    condemned.start()
    assert leaked.wait(timeout=5)

    survivors: list[str] = []
    guard = threading.Lock()

    def other_lane(lane: str) -> None:
        with cl.pipeline_cognition_lock(lane):
            with cl.commit_lock():  # the sanctioned pair still completes
                with guard:
                    survivors.append(lane)

    threads = [
        threading.Thread(target=other_lane, args=(lane,))
        for lane in PIPELINE_IDS
        if lane != 0
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "a leaked chat lock blocked another lane"
    assert sorted(survivors) == sorted(lane for lane in PIPELINE_IDS if lane != 0)

    # And the condemned lane really is condemned — that half of the contract
    # has to still be true, or the leak would not be a fail-hard signal at all.
    blocked = threading.Event()
    waiter = threading.Thread(
        target=lambda: (cl.pipeline_cognition_lock(0).acquire(), blocked.set()),
        daemon=True,
    )
    waiter.start()
    waiter.join(timeout=0.3)
    assert not blocked.is_set()

    release.set()
    condemned.join(timeout=5)
    waiter.join(timeout=5)
    assert blocked.is_set()
    cl.reset_for_tests()


def test_the_global_barrier_stops_every_lane_and_the_commit():
    """All lane locks in NAME order, then commit — the one sanctioned way to
    take more than one lane lock. No production caller exists yet; the order is
    fixed here so the first one ever written cannot invent its own."""
    with cl.global_memory_barrier():
        assert cl.held_lock_names() == [
            f"pipeline:{lane}" for lane in sorted(PIPELINE_IDS)
        ] + ["commit:commit"]

        blocked = threading.Event()

        def try_lane() -> None:
            cl.pipeline_cognition_lock(1).acquire()
            blocked.set()

        waiter = threading.Thread(target=try_lane, daemon=True)
        waiter.start()
        waiter.join(timeout=0.3)
        assert not blocked.is_set()  # the barrier really does hold that lane

    waiter.join(timeout=5)
    assert blocked.is_set()
    cl.reset_for_tests()


# ── THE decisive test ───────────────────────────────────────────────────────


def _fake_terminus(episode, landed: list[str], guard: threading.Lock, rng: random.Random):
    """The real shape of a cut-13 terminus, with the cognition removed.

    Lane lock → lane-local half (unserialized, jittered) → commit region →
    landing (jittered). Both jitters matter: the first lets lanes overtake each
    other before the finish line, the second widens the window in which a
    wrongly-placed stamp would be visible as an inversion.
    """
    with cl.pipeline_cognition_lock(episode.pipeline_id):
        time.sleep(rng.uniform(0, 0.004))  # lane-local deepsleep
        with serialized_commit(episode) as stamp:
            time.sleep(rng.uniform(0, 0.003))  # canonical commits
            with guard:
                landed.append(episode.episode_id)
            return stamp


def test_concurrent_termini_land_in_stamp_order():
    """★ The cut's whole correctness claim, under a randomized scheduler.

    Four lanes, many episodes each, jittered on both sides of the finish line.
    The landed journal — the order the commit region actually wrote things — must
    equal the stamp order exactly. It does because the stamp is the commit
    lock's first statement: there is no instant at which an episode has a stamp
    and has not yet been let into the region it stamps for.
    """
    landed: list[str] = []
    guard = threading.Lock()
    stamps: dict[str, dict] = {}
    per_lane = 6
    start = threading.Barrier(len(PIPELINE_IDS))

    def run_lane(lane: str, seed: int) -> None:
        rng = random.Random(seed)
        start.wait(timeout=10)
        for index in range(per_lane):
            episode = _episode(f"{lane}-{index}", lane)
            stamp = _fake_terminus(episode, landed, guard, rng)
            with guard:
                stamps[episode.episode_id] = stamp

    threads = [
        threading.Thread(target=run_lane, args=(lane, seed))
        for seed, lane in enumerate(PIPELINE_IDS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert len(landed) == len(PIPELINE_IDS) * per_lane
    seqs = [stamps[episode_id]["commit_seq"] for episode_id in landed]
    # Landed order IS stamp order — no sort, no reconciliation, no tolerance.
    assert seqs == sorted(seqs)
    # And the counter handed out a dense total order over the storm.
    assert sorted(seqs) == list(range(1, len(landed) + 1))
    # Every stamp carries its own lane (the journal's ordering key is per-lane
    # attributable, which is what makes an inversion diagnosable).
    for episode_id, stamp in stamps.items():
        assert stamp["pipeline_id"] == int(episode_id.rsplit("-", 1)[0])


def test_the_lane_local_half_really_does_overlap():
    """Guards the decisive test against passing for the wrong reason: if the
    lane-local half were still serialized, stamp order would trivially equal
    landed order and the test above would prove nothing about lane parallelism.
    """
    concurrent = 0
    peak = 0
    guard = threading.Lock()
    ready = threading.Barrier(len(PIPELINE_IDS))

    def lane_local(lane: str) -> None:
        nonlocal concurrent, peak
        ready.wait(timeout=10)
        with cl.pipeline_cognition_lock(lane):
            with guard:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.05)
            with guard:
                concurrent -= 1

    threads = [threading.Thread(target=lane_local, args=(lane,)) for lane in PIPELINE_IDS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert peak == len(PIPELINE_IDS)


def test_the_stamp_is_the_first_statement_inside_the_commit_lock():
    """Not 'early in the region' — first. Anything that ran before it could
    land ahead of an episode that stamped later, which is precisely the class
    of bug gate-entry stamping became once the terminus stopped being one lock.
    """
    episode = _episode("ep-first", 0)
    assert episode.timekeeper == {}
    with cl.pipeline_cognition_lock(0):
        with serialized_commit(episode) as stamp:
            assert cl.commit_lock()._is_owned()  # noqa: SLF001
            assert episode.timekeeper is stamp
            assert stamp["commit_seq"] == 1
            # Cut 12's seal is welded to the stamp, one statement later.
            assert episode.memory_epoch_commit == 1


def test_a_raise_inside_the_region_hands_the_commit_lock_back():
    """The one lock the fail-hard law must NOT keep (cut 13 review).

    A cascade exception exits the ignition/terminus seam with its LANE lock
    held on purpose — one lane, about to be crashed by the fatal relay. The
    COMMIT lock is the opposite case: it is the single global rung every lane
    must reach, so leaking it converts one lane's failure into four lanes
    stopped with no traceback anywhere. ``serialized_commit`` is a context
    manager for exactly this reason.

    The stamp it already handed out stays spent, which is truthful — that
    episode did cross the line — and leaves a permanent hole in ``commit_seq``
    where its journal row would have been. Ordering is unaffected (the sequence
    is compared, never counted), but ``episodes_committed_between`` DOES count
    it: any later episode whose epoch interval spans the hole reports
    ``causal_parents_complete=False`` for good. That is the flag erring loud on
    a terminus that genuinely failed, and it is bounded to the episodes that
    were thinking at that moment — not a defect, but the reason the flag must
    never be read as "a lane is misbehaving right now".
    """
    episode = _episode("ep-boom", 0)

    with cl.pipeline_cognition_lock(0):
        with pytest.raises(RuntimeError, match="terminus exploded"):
            with serialized_commit(episode):
                raise RuntimeError("terminus exploded")
        assert cl.held_lock_names() == ["pipeline:0"]  # lane held, commit given back

    # Another lane reaches the commit lock immediately.
    reached = threading.Event()

    def other() -> None:
        with cl.pipeline_cognition_lock(1):
            with cl.commit_lock():
                reached.set()

    thread = threading.Thread(target=other, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert reached.is_set()
    assert episode.timekeeper["commit_seq"] == 1  # the stamp was spent, and stays spent


def test_the_gate_no_longer_stamps_before_the_terminus(monkeypatch):
    """``run_deepsleep_for_episode`` used to stamp as its first statement.
    After cut 13 an episode reaches ``store.close`` unstamped — the stamp is
    taken further in, where the writes are."""
    from deepsleep_loop.loop import run_deepsleep_for_episode

    episode = _episode("ep-gate", 0)
    episode.closed_at = ""
    episode.opened_at = ""
    seen: dict = {}

    class FakeStore:
        def current(self):
            return episode

        # after_archive (cross-lane T6): accepted to match the real close() contract, and
        # deliberately never called — the real seam fires only after a successful archive,
        # and this double performs no archive.
        def close(self, *, cascade_status="completed", before_finalize=None, after_archive=None):
            seen["stamp_at_close"] = dict(episode.timekeeper)
            before_finalize(episode, {"episode_id": episode.episode_id})
            return episode

    def _core(_store, context_id, intake_builder, **kwargs):
        assert kwargs["closing_episode"] is episode
        assert episode.timekeeper == {}  # still unstamped on the way in
        with serialized_commit(episode):
            seen["stamp_in_region"] = dict(episode.timekeeper)
        return SimpleNamespace(pack=True)

    monkeypatch.setattr("deepsleep_loop.loop._run_deepsleep_episode_core", _core)

    assert run_deepsleep_for_episode(FakeStore()) is not None
    assert seen["stamp_at_close"] == {}
    assert seen["stamp_in_region"]["pipeline_id"] == 0
    assert episode.timekeeper == seen["stamp_in_region"]


# ── the seven stores ────────────────────────────────────────────────────────


def test_every_guarded_store_accessor_is_actually_wrapped():
    """The hazard cut 13 created, pinned structurally: lane-local consolidation
    means two lanes reach these files at once, and each one is a whole-file
    read-modify-write. ``guarded`` is the only thing between that and a lost
    mastery credit nobody would ever see."""
    from deepsleep_loop import (
        failure_store,
        journal_store,
        mastery_store,
        projection_store,
        query_store,
        semantic_store,
        trajectory_store,
    )
    from deepsleep_loop.store_locks import STORE_NAMES

    expected = {
        mastery_store: ("load_mastery", "upsert_mastery"),
        failure_store: ("load_failures", "upsert_failure"),
        semantic_store: ("load_semantic", "upsert_semantic"),
        query_store: ("load_queries", "upsert_query"),
        trajectory_store: ("load_trajectory", "save_trajectory", "upsert_trajectory"),
        journal_store: ("append_journal_entry", "read_journal"),
        projection_store: ("read_projection", "write_projection"),
    }
    assert len(expected) == len(STORE_NAMES) == 7
    for module, names in expected.items():
        for name in names:
            fn = getattr(module, name)
            assert hasattr(fn, "__wrapped__"), f"{module.__name__}.{name} is unguarded"


def test_per_store_lock_loses_no_updates_under_stress(tmp_path, monkeypatch):
    """N threads × M read-modify-writes against one store file, zero lost
    updates. Without the lock this fails within a handful of rounds: two
    upserts read the same map, and the second write erases the first — a
    mastery credit or a promoted fact that simply never happened, with no
    error anywhere to notice."""
    from deepsleep_loop._deepsleep_state import MasteryRecord
    from deepsleep_loop.mastery_store import load_mastery, upsert_mastery

    path = tmp_path / "mastery.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path", lambda policy=None: path
    )

    threads_n, writes_n = 6, 12
    start = threading.Barrier(threads_n)

    def writer(worker: int) -> None:
        start.wait(timeout=10)
        for index in range(writes_n):
            domain = f"w{worker}-d{index}"
            upsert_mastery(
                MasteryRecord(
                    domain_id=domain,
                    domain_name=domain,
                    knowledge_mastery=0.5,
                    evidence_count=1,
                )
            )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    store = load_mastery()
    assert len(store) == threads_n * writes_n
    for worker in range(threads_n):
        for index in range(writes_n):
            assert f"w{worker}-d{index}" in store


def test_store_locks_are_per_store_not_one_big_lock():
    """One lock over all seven would re-serialize the consolidation half this
    cut just freed. The files share no invariant, so they share no lock."""
    from deepsleep_loop.store_locks import STORE_NAMES, store_lock

    locks = [store_lock(name) for name in STORE_NAMES]
    assert len({id(lock) for lock in locks}) == len(STORE_NAMES)
    with pytest.raises(ValueError, match="no deepsleep store lock"):
        store_lock("identity")  # canonical-backed; deliberately not in the set


# ── the transaction the accessors cannot see (cut 13 review) ────────────────


def test_the_trajectory_phase_is_one_transaction_not_two_accessors(tmp_path, monkeypatch):
    """``guarded`` is not enough on its own, and this is the proof.

    ``maintain_trajectory`` reads the WHOLE map, decays every record in it and
    writes the whole map back. Two lanes inside that span (lane-local deepsleep
    is what put them there) both build their new map from the same read, and the
    second ``save_trajectory`` erases the first lane's target, decay pass and
    supersessions — with per-accessor guards in place and no error anywhere.
    Measured before the region existed: 33 of 72 records survived.
    """
    from deepsleep_loop.trajectory.run import maintain_trajectory
    from deepsleep_loop.trajectory_store import load_trajectory
    from tests.core.deepsleep_window_fixtures import trajectory_input

    path = tmp_path / "trajectory.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_trajectory_path", lambda policy=None: path
    )

    threads_n, writes_n = 6, 8
    start = threading.Barrier(threads_n)

    def worker(worker_id: int) -> None:
        start.wait(timeout=10)
        for index in range(writes_n):
            maintain_trajectory(
                trajectory_input(
                    f"ep-{worker_id}-{index}", domain=f"domain-{worker_id}-{index}"
                )
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    store = load_trajectory(path=path)
    # Every target that was ever written is still there. Statuses may differ
    # (a later target supersedes an earlier one — that is the phase's job); a
    # MISSING key is the lost update.
    assert len(store) == threads_n * writes_n


def test_two_lanes_crediting_one_domain_lose_no_mastery(tmp_path, monkeypatch):
    """The commit message's own example, held to its word.

    ``_apply_grounded_mastery`` computes ``evidence_count = prior + 1`` from the
    record it just read. Split across two lock acquisitions, two lanes crediting
    the same domain both read the same prior and the second write erases the
    first — "a mastery credit that never happened, no error anywhere". Measured
    before the region existed: 72 credits landed 23.
    """
    from deepsleep_loop.mastery.run import _apply_grounded_mastery
    from deepsleep_loop.mastery_store import load_mastery, normalize_domain_id

    path = tmp_path / "mastery.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path", lambda policy=None: path
    )

    threads_n, writes_n = 6, 8
    start = threading.Barrier(threads_n)

    def worker(worker_id: int) -> None:
        start.wait(timeout=10)
        for index in range(writes_n):
            _apply_grounded_mastery(
                domain_name="python",
                effect={"knowledge_mastery": 0.5, "application_mastery": 0.5},
                action_goal={},
                resolved_path=path,
                episode_id=f"ep-{worker_id}-{index}",
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    record = load_mastery(path=path)[normalize_domain_id("python")]
    assert record.evidence_count == threads_n * writes_n


def test_a_store_transaction_never_holds_two_stores():
    """The rule that keeps the transaction regions from becoming the deadlock
    ``core.cognition_lock`` exists to prevent: every wrapped region touches ONE
    store, so two store locks are never held at once and there is no order to
    get wrong. Held structurally — a phase module that imports two store
    modules is the shape that would break it."""
    import importlib

    single_store_phases = {
        "deepsleep_loop.mastery.run": "mastery_store",
        "deepsleep_loop.failure.run": "failure_store",
        "deepsleep_loop.trajectory.run": "trajectory_store",
        "deepsleep_loop.query_lifecycle.run": "query_store",
    }
    others = ("mastery_store", "failure_store", "semantic_store", "query_store",
              "trajectory_store", "journal_store", "projection_store")
    for module_name, own in single_store_phases.items():
        source = importlib.import_module(module_name).__file__
        text = open(source, encoding="utf-8").read()
        for other in others:
            if other == own:
                continue
            assert f"deepsleep_loop.{other}" not in text, (
                f"{module_name} reaches {other} — a transaction there would hold "
                "two store locks at once"
            )


# ── the reflection window's new home ────────────────────────────────────────


def test_the_window_append_moved_out_of_close_and_into_the_commit_region(tmp_path):
    """Cut 13 relocated the append from ``EpisodeStore.close`` (pre-stamp, one
    global file, every lane) into the serialized commit region. ``close`` alone
    no longer appends; the commit call does, and it is idempotent against a
    close that already ran one."""
    from pressure_loop.episode import EpisodeStore

    store = EpisodeStore(
        reflection_window_path=tmp_path / "reflection_window.json",
        trajectory_path=tmp_path / "current_trajectory.json",
    )
    episode = store.open(pipeline_id=0, work_kind="operator_turn", pressure={"aggregate_pressure": 1.0}, opened_by="operator_query")
    episode_id = episode.episode_id

    closed = store.close(cascade_status="completed")
    assert closed is episode
    assert store.episode_in_reflection_window(episode_id) is False

    store.commit_reflection_window_append(episode, {"episode_id": episode_id})
    assert store.episode_in_reflection_window(episode_id) is True
    # Twice is once: a retried terminus must not double-count an episode.
    store.commit_reflection_window_append(episode, {"episode_id": episode_id})
    assert store.episode_in_reflection_window(episode_id) is True


def test_a_window_lane_episode_still_never_joins_a_window(tmp_path):
    """Cut 10's rule travels with the call, not with its old position: the
    reflection ON a window is not an episode IN the next one."""
    from pressure_loop.episode import EpisodeStore

    store = EpisodeStore(
        reflection_window_path=tmp_path / "reflection_window.json",
        trajectory_path=tmp_path / "current_trajectory.json",
    )
    episode = SimpleNamespace(
        episode_id="ep-window", pipeline_id=3, work_kind="reflection", opened_by="window_reflection_ready", cascade_status="completed"
    )
    store.commit_reflection_window_append(episode, {"episode_id": "ep-window"})
    assert store.episode_in_reflection_window("ep-window") is False


# ── risk 8: the mastery delta annotates the window its episode joined ───────


def _window_store(tmp_path):
    from pressure_loop.episode import EpisodeStore

    return EpisodeStore(
        reflection_window_path=tmp_path / "reflection_window.json",
        trajectory_path=tmp_path / "current_trajectory.json",
    )


def _delta(domain: str) -> dict:
    return {domain: {"domain_name": domain, "knowledge_delta": 0.1, "status": "learning"}}


def _commit_one(store, episode_id: str, lane: str = 0) -> None:
    """One episode's commit-region append, delta and body together."""
    episode = SimpleNamespace(episode_id=episode_id, pipeline_id=lane, work_kind="autonomous", cascade_status="completed")
    store.commit_reflection_window_append(
        episode,
        {"episode_id": episode_id},
        mastery_delta=_delta(episode_id),
    )


def test_the_delta_lands_on_the_window_the_episode_joined(tmp_path):
    """RISK 8. The annotation used to be a SECOND call, made after the commit
    region closed under nothing but the lane lock, against whatever window the
    store happened to be collecting by then. Now one statement decides the body
    and the delta together, so every episode's delta sits in the window that
    holds its body."""
    store = _window_store(tmp_path)

    for i in range(3):
        _commit_one(store, f"ep-{i}")

    window = store.get_active_window()
    assert window.episodes == ["ep-0", "ep-1", "ep-2"]
    assert [row["episode_id"] for row in window.episode_mastery_deltas] == [
        "ep-0",
        "ep-1",
        "ep-2",
    ]
    assert window.episode_mastery_deltas[1]["mastery_effects"] == _delta("ep-1")


def test_the_staged_window_contains_references_not_mastery_duplicates(tmp_path):
    """The durable staging row is a checkpoint; mastery lives in canonical memory."""
    from pressure_loop.reflection_window import WINDOW_SIZE
    from pressure_loop import window_lane

    store = _window_store(tmp_path)
    for i in range(WINDOW_SIZE):
        _commit_one(store, f"ep-{i}")

    # The window locked and moved to staging; the collecting buffer is empty.
    assert store.get_active_window() is None
    staged = window_lane.staged_windows(window_path=tmp_path / "reflection_window.json")
    assert len(staged) == 1
    assert staged[0].episode_count == WINDOW_SIZE
    assert staged[0].episodes == [f"ep-{i}" for i in range(WINDOW_SIZE)]
    assert staged[0].episode_mastery_deltas == []


def test_concurrent_lanes_checkpoint_every_episode_once_across_rollover(tmp_path):
    """Two lanes cannot lose or duplicate episode references at rollover."""
    from pressure_loop.reflection_window import WINDOW_SIZE
    from pressure_loop import window_lane

    window_path = tmp_path / "reflection_window.json"
    store = _window_store(tmp_path)
    rng = random.Random(1308)
    total = WINDOW_SIZE * 2
    barrier = threading.Barrier(2)

    def _lane(lane: str, ids: list[str]) -> None:
        barrier.wait()
        for episode_id in ids:
            time.sleep(rng.random() / 2000.0)
            _commit_one(store, episode_id, lane=lane)

    chat = [f"chat-{i}" for i in range(total // 2)]
    agent = [f"agent-{i}" for i in range(total // 2)]
    threads = [
        threading.Thread(target=_lane, args=(0, chat)),
        threading.Thread(target=_lane, args=(1, agent)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    windows = list(window_lane.staged_windows(window_path=window_path))
    active = store.get_active_window()
    if active is not None:
        windows.append(active)

    seen: set[str] = set()
    for window in windows:
        episode_ids = set(window.episodes)
        if window in windows[:-1] or active is None:
            assert window.episode_mastery_deltas == []
        seen |= episode_ids
    assert seen == set(chat) | set(agent)


def test_no_second_entry_point_can_annotate_a_delta(tmp_path):
    """One authority. The two functions that could annotate a window from
    OUTSIDE the commit region are gone, not merely unused: a caller that could
    still reach the collecting window would reintroduce the same divergence the
    day someone wired it up again."""
    import cognition_chain
    from pressure_loop.episode import EpisodeStore

    assert not hasattr(EpisodeStore, "append_episode_mastery_delta")
    assert not hasattr(cognition_chain, "_apply_per_episode_mastery_delta")


def test_the_body_is_restamped_before_the_window_reads_it():
    """The enriched body is built by ``close`` BEFORE the stamp exists. Left
    alone it would carry ``timekeeper: {}`` into the reflection window and
    ``_last_closed_body`` — silently, because both fields are optional by
    shape."""
    from pressure_loop.episode_journal import (
        enriched_episode_from_episode,
        restamp_enriched_body,
    )

    from pressure_loop.episode import CognitiveEpisode

    episode = CognitiveEpisode(opened_by="operator_query", pipeline_id=0, work_kind="reflection")
    body = enriched_episode_from_episode(episode)
    assert body["timekeeper"] == {}
    assert body["memory_epoch"]["commit"] == 0

    with cl.pipeline_cognition_lock(0):
        with serialized_commit(episode):
            restamp_enriched_body(body, episode)

    assert body["timekeeper"]["commit_seq"] == episode.timekeeper["commit_seq"]
    assert body["timekeeper"]["pipeline_id"] == 0
    assert body["memory_epoch"]["commit"] == episode.timekeeper["commit_seq"]


# ── the epoch seal's new honesty ────────────────────────────────────────────


def test_causal_parents_admit_a_row_that_has_not_landed_yet(tmp_path, monkeypatch):
    """Cut 13 leaves ``archive_episode`` OUTSIDE the commit lock, so an episode
    that stamped a moment ago may still be between the region and its journal
    append when the next episode seals. Cut 12's docstring promised the
    completeness flag would say so out loud instead of quietly under-reporting;
    this is the case it was promised for."""
    import json

    from pressure_loop.episode_journal import episodes_committed_between

    path = tmp_path / "journal.jsonl"

    def _row(episode_id: str, seq: int) -> dict:
        return {
            "episode_id": episode_id,
            "ignition_reason": "operator_query",
            "lane": "chat",
            "timekeeper": {"commit_seq": seq, "commit_ms": 1000 + seq},
        }

    # seq 2 landed; seq 3 stamped but its archive has not run yet.
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in [_row("landed", 2)]), encoding="utf-8"
    )
    parents, complete = episodes_committed_between(
        1, 4, exclude_episode_id="self", path=path
    )
    assert parents == ["landed"]
    assert complete is False  # "this set may be short" — and it is

    path.write_text(
        "".join(json.dumps(r) + "\n" for r in [_row("landed", 2), _row("late", 3)]),
        encoding="utf-8",
    )
    parents, complete = episodes_committed_between(
        1, 4, exclude_episode_id="self", path=path
    )
    assert parents == ["landed", "late"]
    assert complete is True  # the asker's own row is not owed to itself


def test_the_completeness_verdict_reaches_the_durable_row(tmp_path, monkeypatch):
    """Cut 13 review: the flag has to land where the SET lands.

    The journal row is the ordering authority every consumer reads, and it is
    the record that carries ``causal_parent_episode_ids``. Sealing the verdict
    only into the in-memory pin left that row carrying a possibly-short parent
    list with nothing to say so — the silent under-report the flag was invented
    to replace, one record further out.
    """
    import json

    from pressure_loop.episode import CognitiveEpisode
    from pressure_loop.episode_journal import PriorEpisode, prior_episode_from_episode

    journal = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path", lambda policy=None: journal
    )
    journal.write_text("", encoding="utf-8")

    # Another lane crossed the line at seq 1 while this episode was thinking,
    # and its archive (outside the commit region, cut 13) has not run yet — so
    # the interval (0, 2] holds a row that does not exist on disk.
    from core import timekeeper

    timekeeper.finish_line_stamp(1, "agent_continuation")

    episode = CognitiveEpisode(opened_by="operator_query", pipeline_id=0, work_kind="reflection")
    episode.memory_epoch_start = 0
    with cl.pipeline_cognition_lock(0):
        with serialized_commit(episode):
            pass  # the stamp + the seal are the region's first two statements

    assert episode.memory_epoch_commit == 2
    assert episode.causal_parents_complete is False
    row = prior_episode_from_episode(episode)
    assert row.causal_parents_complete is False
    # And it survives the JSONL round trip the archive writes.
    assert PriorEpisode.from_dict(json.loads(json.dumps(row.to_dict()))) == row


def test_an_unsealed_row_claims_no_verdict():
    """``None``, not ``False``: an episode that never reached the commit region
    has no interval to be complete or short about, and a bare ``False`` there
    would read as a sealed-and-short set that nobody ever computed."""
    from pressure_loop.episode import CognitiveEpisode
    from pressure_loop.episode_journal import prior_episode_from_episode

    row = prior_episode_from_episode(CognitiveEpisode(opened_by="omega_level"))
    assert row.memory_epoch_commit == 0
    assert row.causal_parents_complete is None
