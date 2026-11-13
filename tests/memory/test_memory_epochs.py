"""Phase 4 cut 12 — memory epochs: snapshot-read bookkeeping.

Operator design 2026-08-10 §5: *"every episode captures a memory_epoch at open;
reads see epoch + its own local writes; the commit appends immutable records,
bumps the global epoch."* The epoch is the TIMEKEEPER's ``commit_seq`` — one
ordering authority for both "which memory did I read" and "which memory landed
first", because two counters eventually disagree and the disagreement is
invisible.

Cut 12 is observational-first: no lock is touched, no recall changes what it
returns. What changes is that an episode whose memory moved under it mid-thought
can now SAY SO — pin-with-fallback-to-the-next-snapshot, recorded in the
receipt, never silent. These tests hold that honesty to the wall: a divergence
must be recorded, a surface that could not be pinned must be named as degraded
rather than reported as honored, and an incomplete causal-parent scan must
admit it is incomplete.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from core import timekeeper
from memory import epoch as memory_epoch


@pytest.fixture(autouse=True)
def _isolated_epoch_state(tmp_path, monkeypatch):
    """Hermetic counter + empty pin registry + empty SRAM bucket map."""
    import core.paths as paths
    from memory import episode_sram

    monkeypatch.setattr(paths, "GUARDIAN_STATE_ROOT", tmp_path / "guardian" / "state")
    timekeeper.reset_for_tests()
    memory_epoch.reset_for_tests()
    episode_sram.reset()
    yield
    timekeeper.reset_for_tests()
    memory_epoch.reset_for_tests()
    episode_sram.reset()


def _episode(episode_id: str, pipeline_id: int = 0):
    from pressure_loop.episode import CognitiveEpisode

    ep = CognitiveEpisode(opened_by="operator_query", pipeline_id=pipeline_id, work_kind="operator_turn")
    ep.episode_id = episode_id
    return ep


def _open_episode(episode_id: str, pipeline_id: int = 0):
    """Open the SRAM bucket + the epoch pin exactly as the ignition seam does."""
    from memory import episode_sram

    ep = _episode(episode_id, pipeline_id)
    episode_sram.open_bucket(episode_id, pipeline_id=pipeline_id)
    pin = memory_epoch.open_episode_epoch(ep)
    return ep, pin


# ── the counter: one ordering authority ─────────────────────────────────────


def test_last_commit_seq_observes_without_advancing():
    """Reading the epoch must never consume a finish-line position — an episode
    asks 'which memory state am I reading', not 'give me a place in line'."""
    assert timekeeper.last_commit_seq() == 0
    assert timekeeper.last_commit_seq() == 0

    stamp = timekeeper.finish_line_stamp(0, "operator_turn")
    assert stamp["commit_seq"] == 1
    assert timekeeper.last_commit_seq() == 1
    # Still 1 after two more reads: reads are free, stamps are not.
    assert timekeeper.last_commit_seq() == 1
    assert timekeeper.finish_line_stamp(2, "operator_turn")["commit_seq"] == 2


def test_last_commit_seq_seeds_from_the_durable_counter():
    """An epoch read before the boot's first stamp must see the pre-reboot
    value, or every episode after a restart would claim to have read epoch 0."""
    timekeeper.finish_line_stamp(0, "operator_turn")
    timekeeper.finish_line_stamp(0, "operator_turn")
    timekeeper.reset_for_tests()  # simulate the process restart

    # Memory-organ Cut 13 (D-13-73): the epoch read after a restart is the durable
    # reservation boundary — never below the pre-reboot stamps (never 0), never reused.
    # (Revised from ``== 2``; the block model resumes beyond the old block, gaps are lawful.)
    assert timekeeper.last_commit_seq() >= 2
    assert timekeeper.last_commit_seq() == timekeeper.RESERVATION_BLOCK


def test_epoch_read_is_serialized_against_concurrent_stamps():
    """A four-lane storm must never expose a half-advanced counter."""
    seen: list[int] = []
    seen_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def stamp():
        barrier.wait(timeout=5)
        timekeeper.finish_line_stamp(2, "operator_turn")

    def read():
        barrier.wait(timeout=5)
        value = timekeeper.last_commit_seq()
        with seen_lock:
            seen.append(value)

    threads = [threading.Thread(target=stamp) for _ in range(4)]
    threads += [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert timekeeper.last_commit_seq() == 4
    assert all(0 <= value <= 4 for value in seen)


# ── capture: the pin opens beside the SRAM bucket ───────────────────────────


def test_open_captures_the_epoch_onto_the_carrier():
    timekeeper.finish_line_stamp(2, "operator_turn")  # someone committed before we opened
    ep, pin = _open_episode("ep-open")

    assert pin is not None
    assert pin.epoch == 1
    assert ep.memory_epoch_start == 1
    assert ep.memory_epoch_pin["epoch"] == 1
    assert ep.memory_epoch_pin["captured_at"]
    # Not yet sealed — the commit epoch does not exist until the gate.
    assert ep.memory_epoch_commit == 0
    assert ep.causal_parent_episode_ids == []


def test_pin_resolves_through_the_sram_bucket_binding():
    """The snapshot and the read-your-own-writes overlay must name the SAME
    episode; sharing episode_sram's resolver makes that structural."""
    from memory import episode_sram

    assert memory_epoch.active_pin() is None  # no episode, no epoch

    _ep, pin = _open_episode("ep-bound")
    assert memory_epoch.active_pin() is pin
    assert episode_sram.current_episode_id() == "ep-bound"

    memory_epoch.close_episode_epoch("ep-bound")
    assert memory_epoch.active_pin() is None


def test_pin_dies_with_its_bucket_and_orphans_are_reaped():
    """A pin outliving its episode would be handed to the next unbound read on
    this thread as if it were that read's own snapshot."""
    from memory import episode_sram

    _first, first_pin = _open_episode("ep-first", pipeline_id=0)
    assert memory_epoch.pin_for_episode("ep-first") is first_pin

    # episode_sram reaps drained same-lane leftovers on the next open; the
    # epoch registry must not accrete the pins those buckets left behind.
    episode_sram.open_bucket("ep-second", pipeline_id=0)
    _second = _episode("ep-second")
    memory_epoch.open_episode_epoch(_second)

    assert memory_epoch.pin_for_episode("ep-first") is None
    assert memory_epoch.pin_for_episode("ep-second") is not None


# ── risk 11: attribution is approximate under four lanes, and says so ───────


def test_the_cascade_thread_owns_its_attribution():
    """The ordinary case: the thread that opened the bucket reads through it,
    so the attribution is a fact, not an inference."""
    _ep, pin = _open_episode("ep-bound")

    resolved, attribution = memory_epoch.resolve_active_pin()

    assert resolved is pin
    assert attribution == memory_epoch.ATTRIBUTION_THREAD_BOUND
    assert memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "x")["attribution"] == (
        memory_epoch.ATTRIBUTION_THREAD_BOUND
    )
    assert pin.inferred_reads == 0


def test_an_unbound_thread_with_one_open_episode_cannot_be_wrong():
    """The fallback picked the only candidate. Still labelled, because the
    receipt should say which question it answered."""
    _ep, pin = _open_episode("ep-sole")
    seen: dict = {}

    def _worker():
        seen["resolved"] = memory_epoch.resolve_active_pin()

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5)

    resolved, attribution = seen["resolved"]
    assert resolved is pin
    assert attribution == memory_epoch.ATTRIBUTION_SOLE_BUCKET


def test_a_worker_thread_under_four_lanes_admits_it_inferred():
    """RISK 11. A worker a chat cascade spawned inherits no binding, so the SRAM
    resolver hands it the most recently opened bucket — which under four live
    lanes is another lane's episode. The read is still served; whose receipt it
    lands on is the part that was being guessed at silently."""
    _chat, chat_pin = _open_episode("ep-chat", pipeline_id=0)
    _agent, agent_pin = _open_episode("ep-agent", pipeline_id=1)
    _meta, meta_pin = _open_episode("ep-meta", pipeline_id=2)
    seen: dict = {}

    def _worker():
        seen["resolved"] = memory_epoch.resolve_active_pin()
        seen["observation"] = memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "served")
        seen["receipt"] = memory_epoch.recall_receipt()

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5)

    resolved, attribution = seen["resolved"]
    # The newest bucket wins — NOT the chat episode whose cascade spawned it.
    assert resolved is meta_pin
    assert resolved is not chat_pin and resolved is not agent_pin
    assert attribution == memory_epoch.ATTRIBUTION_INFERRED
    # …and both published surfaces say so rather than presenting it as a fact.
    assert seen["observation"]["attribution"] == memory_epoch.ATTRIBUTION_INFERRED
    assert seen["receipt"]["attribution"] == memory_epoch.ATTRIBUTION_INFERRED
    assert seen["receipt"]["inferred_reads"] == 1
    assert seen["receipt"]["attribution_exact"] is False
    assert meta_pin.inferred_reads == 1


def test_one_inferred_read_makes_the_whole_receipt_approximate():
    """Read exactly as ``honored`` is: an episode that inferred once has a
    receipt that is approximate from then on, however many exact reads follow."""
    _chat, chat_pin = _open_episode("ep-chat", pipeline_id=0)
    _agent, _agent_pin = _open_episode("ep-agent", pipeline_id=1)

    def _worker():
        memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "served")

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5)

    # The agent episode's own cascade thread now reads exactly.
    from memory import episode_sram

    episode_sram._binding.episode_id = "ep-agent"
    receipt = memory_epoch.recall_receipt()

    assert receipt["episode_id"] == "ep-agent"
    assert receipt["attribution"] == memory_epoch.ATTRIBUTION_THREAD_BOUND
    assert receipt["inferred_reads"] == 1  # the worker's read, still counted
    assert receipt["attribution_exact"] is False
    assert chat_pin.inferred_reads == 0  # it was never the one credited


def test_the_bound_reader_never_invents_a_binding():
    """``bound_episode_id`` is the honest half of the pair: no fallback, and a
    binding whose bucket has been drained is not a binding."""
    from memory import episode_sram

    assert episode_sram.bound_episode_id() is None
    assert episode_sram.open_bucket_count() == 0

    _ep, _pin = _open_episode("ep-live")
    assert episode_sram.bound_episode_id() == "ep-live"
    assert episode_sram.open_bucket_count() == 1

    episode_sram.close_bucket("ep-live")
    assert episode_sram.bound_episode_id() is None


# ── observation: pin-with-fallback, recorded ────────────────────────────────


def test_matching_snapshot_is_honored():
    _ep, pin = _open_episode("ep-honored")
    pin.graph_snapshot_hash = "snap-a"
    pin.degraded.clear()

    observation = memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "snap-a")

    assert observation["honored"] is True
    assert "fallback" not in observation
    assert pin.honored_reads == 1
    assert pin.fallback_reads == 0
    assert pin.first_divergence is None


def test_moved_snapshot_falls_back_to_next_and_says_so():
    """The compiled graph is forward-only: there is no earlier snapshot left to
    read, so the recall gets the newer one and the receipt carries the change."""
    _ep, pin = _open_episode("ep-moved")
    pin.graph_snapshot_hash = "snap-a"
    pin.degraded.clear()

    observation = memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "snap-b")

    assert observation["honored"] is False
    assert observation["fallback"] == "next_snapshot"
    assert observation["pinned_value"] == "snap-a"
    assert observation["served"] == "snap-b"
    assert pin.fallback_reads == 1
    assert pin.first_divergence["served"] == "snap-b"


def test_first_divergence_is_the_one_that_is_kept():
    _ep, pin = _open_episode("ep-first-div")
    pin.graph_snapshot_hash = "snap-a"
    pin.degraded.clear()

    memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "snap-b")
    memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "snap-c")

    assert pin.fallback_reads == 2
    assert pin.first_divergence["served"] == "snap-b"


def test_degraded_surface_is_named_not_blamed():
    """A surface that had nothing to pin at capture cannot have diverged.
    Reporting a fallback there would blame the read for a capture-time gap."""
    _ep, pin = _open_episode("ep-degraded")
    pin.dme_snapshot_hash = ""
    if memory_epoch.SURFACE_DME not in pin.degraded:
        pin.degraded.append(memory_epoch.SURFACE_DME)

    observation = memory_epoch.observe(memory_epoch.SURFACE_DME, "packs-77")

    assert observation["degraded"] is True
    assert observation["honored"] is True
    assert "fallback" not in observation
    assert pin.fallback_reads == 0


def test_never_loaded_coord_index_is_unpinnable_not_a_divergence(monkeypatch):
    """Generation 0 means the walker has not loaded the index in this process.
    Pinning against it would make the first recall of every boot cry wolf."""
    import memory.engine.walker as walker

    monkeypatch.setattr(walker, "coord_index_generation", lambda: 0)
    _ep, pin = _open_episode("ep-cold-index")

    assert pin.coord_index_generation == memory_epoch.UNPINNABLE_GENERATION
    assert memory_epoch.SURFACE_COORD_INDEX in pin.degraded

    observation = memory_epoch.observe(memory_epoch.SURFACE_COORD_INDEX, 1)
    assert observation["degraded"] is True
    assert observation["honored"] is True
    assert pin.fallback_reads == 0


def test_unpinned_read_admits_it_has_no_epoch():
    """HUD peeks, diagnostics and boot reads run outside any episode. Inventing
    an epoch for them would be worse than admitting there is none."""
    observation = memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "snap-a")

    assert observation == {
        "pinned": False,
        "surface": memory_epoch.SURFACE_GRAPH,
        "served": "snap-a",
    }
    assert memory_epoch.recall_receipt() == {"pinned": False}


def test_receipt_carries_every_surface_and_never_forgets_a_divergence():
    _ep, pin = _open_episode("ep-receipt")
    pin.graph_snapshot_hash = "snap-a"
    pin.coord_index_generation = 7
    pin.dme_snapshot_hash = "packs-1"
    pin.degraded.clear()

    memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "snap-a")
    memory_epoch.observe(memory_epoch.SURFACE_COORD_INDEX, 8)
    memory_epoch.observe(memory_epoch.SURFACE_DME, "packs-1")

    receipt = memory_epoch.recall_receipt()
    assert receipt["pinned"] is True
    assert receipt["episode_id"] == "ep-receipt"
    assert receipt["pipeline_id"] == 0
    assert set(receipt["surfaces"]) == {
        memory_epoch.SURFACE_GRAPH,
        memory_epoch.SURFACE_COORD_INDEX,
        memory_epoch.SURFACE_DME,
    }
    assert receipt["surfaces"][memory_epoch.SURFACE_COORD_INDEX]["honored"] is False
    assert receipt["honored"] is False

    # A later clean read does NOT restore the episode's honored verdict: once
    # this episode has read across an epoch change, every later read in it
    # inherits that doubt.
    memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "snap-a")
    assert memory_epoch.recall_receipt()["honored"] is False


def test_dme_session_reload_is_observed_at_the_session_seam(monkeypatch):
    """Packs reloaded mid-episode (stale flag / fingerprint drift / TTL expiry)
    move the DME surface under an episode that pinned the previous set."""
    from memory.dme import recall_hook

    recall_hook.force_unload_all_sessions()
    monkeypatch.setattr(
        recall_hook,
        "_load_state",
        lambda root: recall_hook.DmeSessionState(
            root_key="k", index=SimpleNamespace(seat_count=0, backend="numpy"),
            planes=None, snapshot_hash="packs-new",
        ),
    )

    _ep, pin = _open_episode("ep-dme")
    pin.dme_snapshot_hash = "packs-old"
    pin.degraded.clear()

    with recall_hook.dme_recall_session(None):
        pass

    assert pin.fallback_reads == 1
    assert pin.surfaces[memory_epoch.SURFACE_DME]["served"] == "packs-new"
    assert pin.surfaces[memory_epoch.SURFACE_DME]["fallback"] == "next_snapshot"
    recall_hook.force_unload_all_sessions()


def test_read_memory_attaches_the_receipt_to_its_meta(monkeypatch):
    """The receipt is only worth building if it reaches the caller. Driven
    through the real ``read_memory`` with a faked taxonomy packet (the
    verdict-gate suite's harness) so the placement — after the canonical merge,
    which is itself a read — is the thing under test."""
    from memory.api import QuerySpec, read_memory

    class FakePacket:
        results = []
        snapshot_hash = "snap-recompiled"
        resolved_entities = ()
        evidence_event_ids = []

    monkeypatch.setattr("memory.taxonomy.recall.recall", lambda **kwargs: FakePacket())
    monkeypatch.setattr("memory.recall_compiler.compile_recall_plan", lambda query: None)

    _ep, pin = _open_episode("ep-read")
    pin.graph_snapshot_hash = "snap-pinned"
    pin.degraded.clear()

    result = read_memory(QuerySpec(cycle_id="cycle-epoch", text="anything", limit=3))
    receipt = (result.meta or {}).get("memory_epoch") or {}

    assert receipt["pinned"] is True
    assert receipt["episode_id"] == "ep-read"
    assert receipt["honored"] is False
    graph = receipt["surfaces"][memory_epoch.SURFACE_GRAPH]
    assert graph["pinned_value"] == "snap-pinned"
    assert graph["served"] == "snap-recompiled"
    assert graph["fallback"] == "next_snapshot"


def test_read_memory_outside_an_episode_reports_no_epoch(monkeypatch):
    from memory.api import QuerySpec, read_memory

    class FakePacket:
        results = []
        snapshot_hash = "snap-any"
        resolved_entities = ()
        evidence_event_ids = []

    monkeypatch.setattr("memory.taxonomy.recall.recall", lambda **kwargs: FakePacket())
    monkeypatch.setattr("memory.recall_compiler.compile_recall_plan", lambda query: None)

    result = read_memory(QuerySpec(cycle_id="cycle-none", text="anything", limit=3))

    assert (result.meta or {}).get("memory_epoch") == {"pinned": False}


# ── sealing: the commit half ────────────────────────────────────────────────


def test_seal_takes_the_commit_epoch_from_the_stamp_and_no_other_counter(tmp_path, monkeypatch):
    """The commit epoch is read off this episode's own stamp — no counter is
    consulted a second time.

    Phase 4 cut 13 sharpened the completeness half. Another lane crosses the
    line first here, so the interval (0, 2] holds a parent — and the journal has
    no row for it, because ``archive_episode`` runs OUTSIDE the commit lock and
    that episode has not reached it yet (or, in this fixture, never will). The
    honest answer is "this set may be short", and the flag now says so instead
    of reading an empty file as proof that nothing committed.
    """
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda *a, **k: tmp_path / "episode_journal.jsonl",
    )
    ep, _pin = _open_episode("ep-seal")
    timekeeper.finish_line_stamp(1, "operator_turn")  # another lane crosses the line first
    ep.timekeeper = timekeeper.finish_line_stamp(0, "operator_turn")

    sealed = memory_epoch.seal_episode_epoch(ep)

    assert ep.memory_epoch_start == 0
    assert sealed["commit"] == 2 == ep.timekeeper["commit_seq"]
    assert ep.memory_epoch_commit == 2
    # commit > start + 1, so the scan really ran — against an empty journal.
    assert sealed["causal_parent_episode_ids"] == []
    assert sealed["causal_parents_complete"] is False


def test_lone_lane_seals_without_reading_the_journal(monkeypatch):
    """commit == start + 1 means the only sequence number in the interval is
    this episode's own stamp — provably no parents, so the whole-file journal
    read is skipped. This is the common case; it must not cost I/O."""
    from pressure_loop import episode_journal

    def _forbidden(*a, **k):
        raise AssertionError("the lone-lane seal must not read the journal")

    monkeypatch.setattr(episode_journal, "episodes_committed_between", _forbidden)

    ep, _pin = _open_episode("ep-alone")
    ep.timekeeper = timekeeper.finish_line_stamp(0, "operator_turn")

    sealed = memory_epoch.seal_episode_epoch(ep)

    assert ep.memory_epoch_start == 0
    assert sealed["commit"] == 1
    assert sealed["causal_parent_episode_ids"] == []
    assert sealed["causal_parents_complete"] is True


def test_seal_before_the_stamp_fails_loud():
    """The commit epoch IS the stamp. Sealing first is a caller ordering bug
    with nothing honest to record, so it raises rather than writing a zero."""
    ep, _pin = _open_episode("ep-unstamped")

    with pytest.raises(RuntimeError, match="finish-line stamp"):
        memory_epoch.seal_episode_epoch(ep)


# ── causal parents: what I could not have known ─────────────────────────────


def _journal(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(episode_id: str, seq: int, ms: int = 1000):
    return {
        "episode_id": episode_id,
        "ignition_reason": "operator_query",
        "pipeline_id": 0,
        "work_kind": "operator_turn",
        "timekeeper": {
            "commit_seq": seq,
            "commit_ms": ms,
            "committed_at": "2026-08-11T00:00:00Z",
            "event_date": "2026-08-11",
            "pipeline_id": 0,
        "work_kind": "operator_turn",
        },
    }


def test_causal_parents_are_the_rows_inside_the_interval(tmp_path):
    from pressure_loop.episode_journal import episodes_committed_between

    path = tmp_path / "journal.jsonl"
    _journal(path, [_row("before", 1), _row("during-a", 2), _row("during-b", 3), _row("self", 4)])

    parents, complete = episodes_committed_between(
        1, 4, exclude_episode_id="self", path=path
    )

    assert parents == ["during-a", "during-b"]
    assert complete is True


def test_causal_parents_sort_by_finish_line_not_append_order(tmp_path):
    """Journal rows land in close-completion order, which can lag arrival."""
    from pressure_loop.episode_journal import episodes_committed_between

    path = tmp_path / "journal.jsonl"
    _journal(path, [_row("late-arrival", 3, ms=2000), _row("early-arrival", 2, ms=1000)])

    parents, complete = episodes_committed_between(1, 3, path=path)

    assert parents == ["early-arrival", "late-arrival"]
    assert complete is True


def test_empty_interval_is_a_complete_result(tmp_path):
    """start == commit means nothing landed mid-thought. That is an ANSWER."""
    from pressure_loop.episode_journal import episodes_committed_between

    path = tmp_path / "journal.jsonl"
    _journal(path, [_row("other", 9)])

    assert episodes_committed_between(4, 4, path=path) == ([], True)
    assert episodes_committed_between(0, 0, path=tmp_path / "missing.jsonl") == ([], True)


def test_unstamped_rows_are_skipped_not_treated_as_epoch_zero(tmp_path):
    """A legacy row predates the counter and cannot be placed in an interval.

    The interval is (1, 2] — exactly one sequence number — so the answer is
    also COMPLETE: skipping the legacy row costs nothing the flag has to
    confess (cut 13 made that flag count the interval's density).
    """
    from pressure_loop.episode_journal import episodes_committed_between

    path = tmp_path / "journal.jsonl"
    _journal(
        path,
        [
            {"episode_id": "legacy", "ignition_reason": "curiosity_gap"},
            _row("stamped", 2),
        ],
    )

    parents, complete = episodes_committed_between(1, 2, path=path)

    assert parents == ["stamped"]
    assert complete is True


def test_truncated_scan_admits_it_is_short(tmp_path):
    """Reading an unbounded journal at every deepsleep gate is a permanent
    cost; a bounded tail with an honest 'incomplete' flag is the trade."""
    from pressure_loop.episode_journal import episodes_committed_between

    path = tmp_path / "journal.jsonl"
    _journal(path, [_row(f"ep-{i}", i) for i in range(1, 21)])

    parents, complete = episodes_committed_between(0, 20, path=path, scan_limit=5)

    assert parents == ["ep-16", "ep-17", "ep-18", "ep-19", "ep-20"]
    assert complete is False


def test_overlapping_episodes_journal_truthful_epochs_and_parents(tmp_path, monkeypatch):
    """The verification-ladder case for this cut. Two lanes overlap: chat opens
    first, agent opens and commits while chat is still thinking, then chat
    commits. Chat must name agent as a causal parent — agent's writes landed
    after chat's snapshot, so chat's reads provably did not contain them — and
    agent, which read a later epoch and committed first, must name nobody."""
    from pressure_loop.episode_journal import archive_episode, journal_window

    path = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr("pressure_loop.policy.episode_journal_path", lambda *a, **k: path)

    chat, _chat_pin = _open_episode("ep-chat", pipeline_id=0)
    agent = _episode("ep-agent", pipeline_id=1)
    from memory import episode_sram

    episode_sram.open_bucket("ep-agent", pipeline_id=1)
    memory_epoch.open_episode_epoch(agent)

    # The agent lane crosses the finish line first.
    agent.timekeeper = timekeeper.finish_line_stamp(1, "operator_turn")
    memory_epoch.seal_episode_epoch(agent)
    archive_episode(agent, path=path)

    # ...then the chat episode, which has been thinking the whole time.
    chat.timekeeper = timekeeper.finish_line_stamp(0, "operator_turn")
    memory_epoch.seal_episode_epoch(chat)
    archive_episode(chat, path=path)

    assert agent.memory_epoch_start == 0
    assert agent.memory_epoch_commit == 1
    assert agent.causal_parent_episode_ids == []

    assert chat.memory_epoch_start == 0
    assert chat.memory_epoch_commit == 2
    assert chat.causal_parent_episode_ids == ["ep-agent"]

    rows = {pe.episode_id: pe for pe in journal_window(10, path=path)}
    assert rows["ep-chat"].memory_epoch_start == 0
    assert rows["ep-chat"].memory_epoch_commit == 2
    assert rows["ep-chat"].causal_parent_episode_ids == ["ep-agent"]
    assert rows["ep-agent"].causal_parent_episode_ids is None


# ── the journal row + enriched body ─────────────────────────────────────────


def test_prior_episode_round_trips_the_epoch_fields():
    from pressure_loop.episode_journal import PriorEpisode, prior_episode_from_episode

    ep = SimpleNamespace(
        episode_id="ep-rt",
        opened_by="operator_query",
        pipeline_id=0,
        timekeeper=timekeeper.finish_line_stamp(0, "operator_turn"),
        global_={},
        meta={},
        cascade_status="completed",
        memory_epoch_start=4,
        memory_epoch_commit=9,
        causal_parent_episode_ids=["ep-a", "ep-b"],
    )

    pe = prior_episode_from_episode(ep)
    assert pe.memory_epoch_start == 4
    assert pe.memory_epoch_commit == 9
    assert pe.causal_parent_episode_ids == ["ep-a", "ep-b"]

    again = PriorEpisode.from_dict(pe.to_dict())
    assert again.memory_epoch_start == 4
    assert again.memory_epoch_commit == 9
    assert again.causal_parent_episode_ids == ["ep-a", "ep-b"]


def test_the_schema_describes_every_field_the_journal_actually_writes():
    """RISK 11. ``schema/journal.schema.yaml``'s PriorEpisode had been stale
    since Phase 3 cut 3: seven fields described against twelve written. A schema
    that omits the fields the ORDERING AUTHORITY is read for is worse than no
    schema — it reads as a licence to drop them. The dataclass is the authority,
    so this holds the file to it in both directions."""
    from dataclasses import fields
    from pathlib import Path

    import yaml

    from pressure_loop.episode_journal import PriorEpisode

    schema = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "schema" / "journal.schema.yaml").read_text(
            encoding="utf-8"
        )
    )
    described = schema["definitions"]["PriorEpisode"]["properties"]
    written = {f.name for f in fields(PriorEpisode)}

    assert set(described) == written
    # ``to_dict`` is asdict, so the row IS the dataclass — nothing else may
    # appear, and the schema has to say that rather than leave it open.
    assert schema["definitions"]["PriorEpisode"]["additionalProperties"] is False
    # The Phase 4 fields specifically, named so a future drop is a test failure
    # and not a silent regression to the shape this risk found.
    for field_name in (
        "pipeline_id",
        "work_kind",
        "timekeeper",
        "memory_epoch_start",
        "memory_epoch_commit",
        "causal_parent_episode_ids",
        "causal_parents_complete",
    ):
        assert field_name in described


def test_the_schemas_stamp_matches_the_stamp_the_timekeeper_hands_out():
    """The row's ``timekeeper`` block is not free-form: it is the finish-line
    stamp, and sorting the journal by it is how episode order is read at all."""
    from pathlib import Path

    import yaml

    schema = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "schema" / "journal.schema.yaml").read_text(
            encoding="utf-8"
        )
    )
    described = schema["definitions"]["FinishLineStamp"]

    assert set(described["properties"]) == set(timekeeper.finish_line_stamp(0, "operator_turn"))
    assert set(described["required"]) == set(described["properties"])


def test_legacy_rows_read_as_no_epoch_recorded():
    """Pre-cut-12 rows carry defaults, which must read as 'nothing recorded'
    rather than as a real epoch zero."""
    from pressure_loop.episode_journal import PriorEpisode

    pe = PriorEpisode.from_dict(
        {"episode_id": "legacy", "ignition_reason": "curiosity_gap"}
    )

    assert pe.memory_epoch_start == 0
    assert pe.memory_epoch_commit == 0
    assert pe.causal_parent_episode_ids is None


def test_enriched_body_carries_the_memory_epoch_block():
    from pressure_loop.episode_journal import enriched_episode_from_episode

    ep = _episode("ep-body")
    ep.memory_epoch_start = 3
    ep.memory_epoch_commit = 6
    ep.causal_parent_episode_ids = ["ep-parent"]
    ep.memory_epoch_pin = {"epoch": 3, "graph_snapshot_hash": "snap-a", "degraded": []}

    body = enriched_episode_from_episode(ep)

    assert body["memory_epoch"]["start"] == 3
    assert body["memory_epoch"]["commit"] == 6
    assert body["memory_epoch"]["causal_parent_episode_ids"] == ["ep-parent"]
    assert body["memory_epoch"]["pin"]["graph_snapshot_hash"] == "snap-a"


# ── the kill switch ─────────────────────────────────────────────────────────


def test_kill_switch_restores_phase_three(monkeypatch, tmp_path):
    """OMEGA_MEMORY_EPOCHS=0: no pin, no receipt, carrier fields untouched."""
    monkeypatch.setenv("OMEGA_MEMORY_EPOCHS", "0")
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda *a, **k: tmp_path / "episode_journal.jsonl",
    )
    from memory import episode_sram
    from pressure_loop.episode_journal import prior_episode_from_episode

    timekeeper.finish_line_stamp(2, "operator_turn")
    ep = _episode("ep-off")
    episode_sram.open_bucket("ep-off", pipeline_id=0)

    assert memory_epoch.open_episode_epoch(ep) is None
    assert memory_epoch.active_pin() is None
    assert ep.memory_epoch_start == 0
    assert ep.memory_epoch_pin == {}
    assert memory_epoch.recall_receipt() == {"pinned": False}
    assert memory_epoch.observe(memory_epoch.SURFACE_GRAPH, "snap-a")["pinned"] is False

    ep.timekeeper = timekeeper.finish_line_stamp(0, "operator_turn")
    assert memory_epoch.seal_episode_epoch(ep) == {}
    assert ep.memory_epoch_commit == 0

    pe = prior_episode_from_episode(ep)
    assert pe.memory_epoch_start == 0
    assert pe.memory_epoch_commit == 0
    assert pe.causal_parent_episode_ids is None
