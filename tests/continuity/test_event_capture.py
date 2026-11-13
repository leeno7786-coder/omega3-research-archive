"""WO-03 — owner-bound episode event capture through the existing journal and SRAM.

These tests drive the REAL seams: an actual `episode_sram` bucket opened for a named physical
owner, the real `intake_journal` on an isolated path, and `record_event` itself. Nothing
mocks `record_event`, per the order's RED seed.

The property the whole order exists for: **one real operation is one event, forever.** A
retry of the same operation token must return the original occurrence — same event id, same
sequence, same timestamp — rather than mint a second. Two different tokens are two events even
when their payload bytes are identical.
"""

from __future__ import annotations

import threading

import pytest

from memory import episode_sram, intake_journal
from memory.episode_events import (
    CAPTURE_COMPONENT,
    EVENT_JOURNAL_KIND,
    capture_context,
    read_episode_events,
    record_event,
)
from schema.continuity_json import (
    ContinuityContractError,
    EpisodeCaptureUnavailable,
    EpisodeOwnershipError,
    EventIdentityConflict,
    canonical_bytes,
)
from schema.episode_event import SEALED_EVENT_FIELDS, EpisodeEvent

PAYLOAD = {"tool": "synthetic_tool", "verdict": "passed"}


@pytest.fixture
def owner():
    """One open SRAM bucket for a named physical owner, torn down cleanly."""
    episode_sram.reset()
    episode_sram.open_bucket("ep-cap-0001", pipeline_id=0)
    yield "ep-cap-0001", 0
    episode_sram.reset()


# ── the RED seed: one operation token is one event ────────────────────────────


def test_the_same_operation_token_yields_one_event_id_and_sequence(owner):
    episode_id, _ = owner
    first = record_event(
        episode_id, operation_id="op-fixed", phase="auto", event_kind="tool_call", payload=PAYLOAD
    )
    second = record_event(
        episode_id, operation_id="op-fixed", phase="auto", event_kind="tool_call", payload=PAYLOAD
    )
    assert first.event_id == second.event_id
    assert first.event_seq == second.event_seq
    assert first.observed_at == second.observed_at, "a retry must not restamp the original"
    assert first.sealed_sha256() == second.sealed_sha256()
    assert len(read_episode_events(episode_id)) == 1


def test_two_operation_tokens_with_identical_payloads_are_two_ordered_events(owner):
    episode_id, _ = owner
    first = record_event(
        episode_id, operation_id="op-a", phase="auto", event_kind="tool_call", payload=PAYLOAD
    )
    second = record_event(
        episode_id, operation_id="op-b", phase="auto", event_kind="tool_call", payload=PAYLOAD
    )
    assert first.content_sha256() == second.content_sha256(), "identical content, by construction"
    assert first.event_id != second.event_id
    assert first.occurrence_id() != second.occurrence_id()
    assert (first.event_seq, second.event_seq) == (1, 2)
    assert tuple(e.event_id for e in read_episode_events(episode_id)) == (
        first.event_id,
        second.event_id,
    )


def test_the_sequence_is_per_episode_and_starts_at_one(owner):
    """Two live episodes, each capture bound to its OWN owner (WO-03A R1).

    This previously relied on the defect: `open_bucket` rebinds the opening thread, so the
    captures for `ep-cap-0001` ran while the thread was bound to `ep-cap-0002` and were accepted
    only because a named open target counted as authorization. The multi-episode property under
    test is unchanged; the intended owner is now stated.
    """
    episode_id, _ = owner
    episode_sram.open_bucket("ep-cap-0002", pipeline_id=1)
    try:
        with episode_sram.bind_episode(episode_id, pipeline_id=0):
            a1 = record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload={})
        with episode_sram.bind_episode("ep-cap-0002", pipeline_id=1):
            b1 = record_event("ep-cap-0002", operation_id="op-1", phase="auto", event_kind="k", payload={})
        with episode_sram.bind_episode(episode_id, pipeline_id=0):
            a2 = record_event(episode_id, operation_id="op-2", phase="auto", event_kind="k", payload={})
        assert (a1.event_seq, a2.event_seq) == (1, 2)
        assert b1.event_seq == 1, "a second episode numbers from one; there is no global counter"
        assert a1.event_id != b1.event_id, "same token, different episode, different event"
    finally:
        episode_sram.reset()
        episode_sram.open_bucket(episode_id, pipeline_id=0)


# ── ownership is authorized before mutation ───────────────────────────────────


def test_an_unbound_worker_cannot_capture():
    episode_sram.reset()
    with pytest.raises(EpisodeOwnershipError, match="matching live owner binding"):
        record_event("ep-nobody", operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert intake_journal.records() == (), "a refused capture journals nothing"


def test_a_wrong_owner_write_fails(owner):
    episode_id, _ = owner
    with pytest.raises(EpisodeOwnershipError, match="matching live owner binding"):
        record_event("ep-not-open", operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert read_episode_events(episode_id) == ()


def test_a_bound_worker_cannot_write_into_another_live_episode(owner):
    """WO-03A R1: a valid OPEN target is not proof that this caller owns it."""
    episode_id, _ = owner
    episode_sram.open_bucket("ep-cap-other", pipeline_id=1)
    try:
        with episode_sram.bind_episode(episode_id, pipeline_id=0):
            with pytest.raises(EpisodeOwnershipError, match="matching live owner binding"):
                record_event("ep-cap-other", operation_id="op-1", phase="auto",
                             event_kind="k", payload={})
        assert intake_journal.records() == (), "a refused cross-owner capture journals nothing"
        assert episode_sram.pending("ep-cap-other") == []
        assert read_episode_events("ep-cap-other") == ()
    finally:
        episode_sram.reset()
        episode_sram.open_bucket(episode_id, pipeline_id=0)


def test_an_unbound_worker_thread_cannot_authorize_itself_by_naming_a_live_episode(owner):
    """A dispatched worker inherits no binding; naming the live episode is not binding to it."""
    episode_id, _ = owner
    seen: list[BaseException | None] = []

    def _attempt() -> None:
        assert episode_sram.bound_episode_id() is None, "a fresh thread starts unbound"
        try:
            record_event(episode_id, operation_id="op-unbound", phase="auto",
                         event_kind="k", payload={})
            seen.append(None)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            seen.append(exc)

    worker = threading.Thread(target=_attempt)
    worker.start()
    worker.join(timeout=30)
    assert len(seen) == 1
    assert isinstance(seen[0], EpisodeOwnershipError), f"unbound capture was accepted: {seen[0]!r}"
    assert intake_journal.records() == ()
    assert episode_sram.pending(episode_id) == []


def test_four_bound_owners_cannot_receive_one_anothers_events():
    """Acceptance case: four concurrent physical owners, no cross-contamination."""
    episode_sram.reset()
    ids = [f"ep-cap-p{pid}" for pid in range(4)]
    for pid, eid in enumerate(ids):
        episode_sram.open_bucket(eid, pipeline_id=pid)
    try:
        for pid, eid in enumerate(ids):
            with episode_sram.bind_episode(eid, pipeline_id=pid):
                record_event(eid, operation_id=f"op-{pid}", phase="auto", event_kind="k",
                             payload={"pipeline": pid})
        for pid, eid in enumerate(ids):
            events = read_episode_events(eid)
            assert len(events) == 1
            assert events[0].episode_id == eid
            assert events[0].payload["pipeline"] == pid
            assert events[0].pipeline_id == pid
    finally:
        episode_sram.reset()


def test_four_owners_capturing_concurrently_keep_bucket_and_sealed_thread_attribution():
    """WO-03A R1 acceptance: four bound owners at once, checked on identity not on labels.

    The payload label a capture supplies proves nothing — it is the caller's own string. What is
    asserted here is where the event physically LANDED (its owner's SRAM bucket, and only that
    bucket) and the identity the event SEALED (episode, thread, pipeline), for four threads
    released together.
    """
    episode_sram.reset()
    ids = [f"ep-cap-c{pid}" for pid in range(4)]
    for pid, eid in enumerate(ids):
        episode_sram.open_bucket(eid, pipeline_id=pid)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []
    try:
        def _capture(pid: int, eid: str) -> None:
            try:
                barrier.wait(timeout=30)          # ordering control, not a sleep
                with episode_sram.bind_episode(eid, pipeline_id=pid):
                    with capture_context(thread_id=f"thread-{pid}"):
                        record_event(eid, operation_id="op-shared-token", phase="auto",
                                     event_kind="k", payload={"claimed": "identical"})
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=_capture, args=(pid, eid)) for pid, eid in enumerate(ids)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert errors == []

        for pid, eid in enumerate(ids):
            held = [item for item in episode_sram.pending(eid) if item.kind == "event"]
            assert len(held) == 1, f"{eid} holds {len(held)} events"
            sealed = held[0].event
            assert sealed.episode_id == eid, "the bucket holds its own episode's event"
            assert sealed.thread_id == f"thread-{pid}", "the capturing thread is sealed"
            assert sealed.pipeline_id == pid
            assert sealed.event_seq == 1, "each episode numbers from one"

            events = read_episode_events(eid)
            assert [event.event_id for event in events] == [sealed.event_id]
            others = {other for other in ids if other != eid}
            assert not others & {event.episode_id for event in events}
    finally:
        episode_sram.reset()


def test_capture_never_infers_ownership_from_the_only_open_bucket():
    """The diagnostic sole-bucket fallback is not write authority."""
    episode_sram.reset()
    episode_sram.open_bucket("ep-sole", pipeline_id=0)
    try:
        with pytest.raises(EpisodeOwnershipError, match="matching live owner binding"):
            record_event("ep-other", operation_id="op-1", phase="auto", event_kind="k", payload={})
    finally:
        episode_sram.reset()


def test_a_lost_owner_leaves_durable_work_unacknowledged_rather_than_redirecting_it(
    owner, monkeypatch
):
    """WO-03A R1: the binding is rechecked at acknowledgement, and a loss is recoverable.

    The bucket disappears in the interval between the durable journal row and the SRAM hold. The
    event must not land in whichever bucket is newest or sole; it must stay durable, unheld, and
    replayable by its real owner.
    """
    episode_id, _ = owner
    import memory.episode_events as episode_events

    real_confirm = episode_events._confirmed_durable

    def _drop_the_owner(record, *, event_id):
        confirmed = real_confirm(record, event_id=event_id)
        episode_sram.reset()
        episode_sram.open_bucket("ep-cap-successor", pipeline_id=0)
        return confirmed

    with monkeypatch.context() as patched:
        patched.setattr(episode_events, "_confirmed_durable", _drop_the_owner)
        with pytest.raises(EpisodeCaptureUnavailable, match="owner binding moved"):
            record_event(episode_id, operation_id="op-lost-owner", phase="auto",
                         event_kind="k", payload={"v": 1})

    assert episode_sram.pending("ep-cap-successor") == [], "the successor did not inherit it"
    durable = read_episode_events(episode_id)
    assert len(durable) == 1, "the durable row survives an unacknowledged capture"
    assert durable[0].episode_id == episode_id

    episode_sram.reset()
    episode_sram.open_bucket(episode_id, pipeline_id=0)
    replayed = record_event(episode_id, operation_id="op-lost-owner", phase="auto",
                            event_kind="k", payload={"v": 1})
    assert replayed.event_id == durable[0].event_id
    assert replayed.observed_at == durable[0].observed_at
    assert len(episode_sram.pending(episode_id)) == 1


# ── durability precedes acknowledgement ───────────────────────────────────────


def test_the_event_is_in_the_journal_before_it_is_acknowledged(owner):
    episode_id, _ = owner
    event = record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload=PAYLOAD)
    record = intake_journal.record_by_id(intake_journal._record_id(event.event_id))
    assert record is not None and record.kind == EVENT_JOURNAL_KIND
    assert record.status == intake_journal.PENDING
    assert record.owner_episode_id == episode_id


def test_a_journal_failure_is_attributable_and_prevents_acknowledgement(owner, monkeypatch):
    """Acceptance case: fsync failure is attributable, and nothing is acknowledged."""
    episode_id, _ = owner

    def _explode(*_a, **_k):
        raise OSError("simulated fsync failure")

    # A scoped context, never `monkeypatch.undo()`: the autouse conftest redirect shares this
    # same monkeypatch, so a blanket undo would point the assertions below at live memory state
    # — where they would pass for the wrong reason (WO-03A).
    with monkeypatch.context() as patched:
        patched.setattr(intake_journal, "_append_locked", _explode)
        with pytest.raises(Exception) as excinfo:
            record_event(episode_id, operation_id="op-doomed", phase="auto", event_kind="k", payload={})
        assert "simulated fsync failure" in str(excinfo.value) or isinstance(
            excinfo.value.__cause__, OSError
        ), "the primary cause must survive"
    assert read_episode_events(episode_id) == (), "a failed capture is not acknowledged"
    assert episode_sram.pending(episode_id) == [], "and nothing reached the bucket"


def test_a_capture_failure_does_not_log_itself_through_the_broken_path(owner, monkeypatch):
    """Step 5: no recursive attempt to journal the journal's own failure."""
    episode_id, _ = owner
    calls: list[str] = []
    real = intake_journal._append_locked

    def _counting(record, operation):
        calls.append(operation)
        raise OSError("boom")

    monkeypatch.setattr(intake_journal, "_append_locked", _counting)
    with pytest.raises(Exception):
        record_event(episode_id, operation_id="op-x", phase="auto", event_kind="k", payload={})
    monkeypatch.setattr(intake_journal, "_append_locked", real)
    assert len(calls) == 1, f"capture retried through the broken path: {calls}"


# ── conflict ──────────────────────────────────────────────────────────────────


def test_the_same_token_with_a_changed_payload_is_refused(owner):
    episode_id, _ = owner
    original = record_event(
        episode_id, operation_id="op-fixed", phase="auto", event_kind="k", payload={"v": "first"}
    )
    with pytest.raises(EventIdentityConflict) as excinfo:
        record_event(
            episode_id, operation_id="op-fixed", phase="auto", event_kind="k", payload={"v": "second"}
        )
    assert "payload" in str(excinfo.value)
    surviving = read_episode_events(episode_id)
    assert len(surviving) == 1
    assert surviving[0].payload["v"] == "first", "neither record may be replaced"
    assert surviving[0].sealed_sha256() == original.sealed_sha256()


@pytest.mark.parametrize(
    "field,value",
    [("phase", "deepsleep"), ("event_kind", "something_else"), ("evidence_refs", ("ev-9",))],
)
def test_any_changed_immutable_input_is_refused_by_name(owner, field, value):
    episode_id, _ = owner
    base = dict(operation_id="op-fixed", phase="auto", event_kind="k", payload=PAYLOAD)
    record_event(episode_id, **base)
    with pytest.raises(EventIdentityConflict) as excinfo:
        record_event(episode_id, **{**base, field: value})
    assert field in str(excinfo.value)


def test_a_conflicting_retry_leaves_the_sequence_untouched(owner):
    episode_id, _ = owner
    record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    with pytest.raises(EventIdentityConflict):
        record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload={"v": 2})
    nxt = record_event(episode_id, operation_id="op-2", phase="auto", event_kind="k", payload={})
    assert nxt.event_seq == 2, "a refused conflict must not burn a sequence number"


# ── replay equality is canonical bytes, not Python equality (WO-03A R2) ───────


@pytest.mark.parametrize(
    "first,second",
    [
        ({"v": 1}, {"v": True}),
        ({"v": True}, {"v": 1}),
        ({"v": 0}, {"v": False}),
        ({"v": False}, {"v": 0}),
        ({"deep": {"list": [0, 1]}}, {"deep": {"list": [False, True]}}),
    ],
    ids=["int-bool", "bool-int", "zero-false", "false-zero", "nested-array"],
)
def test_a_json_type_substitution_is_a_conflict_not_a_replay(owner, first, second):
    """Python calls `True == 1`; the canonical encoding does not, and the journal holds bytes."""
    episode_id, _ = owner
    assert first == second, "the point of the case: Python equality cannot tell these apart"
    assert canonical_bytes(first) != canonical_bytes(second)
    original = record_event(episode_id, operation_id="op-typed", phase="auto",
                            event_kind="k", payload=first)
    with pytest.raises(EventIdentityConflict, match="payload"):
        record_event(episode_id, operation_id="op-typed", phase="auto",
                     event_kind="k", payload=second)
    surviving = read_episode_events(episode_id)
    assert len(surviving) == 1
    assert surviving[0].sealed_sha256() == original.sealed_sha256()
    assert len(episode_sram.pending(episode_id)) == 1
    nxt = record_event(episode_id, operation_id="op-after", phase="auto", event_kind="k", payload={})
    assert nxt.event_seq == 2, "a refused type substitution consumes no sequence"


def test_object_key_insertion_order_is_not_part_of_the_identity(owner):
    episode_id, _ = owner
    original = record_event(
        episode_id, operation_id="op-keys", phase="auto", event_kind="k",
        payload={"alpha": 1, "beta": {"x": "1", "y": [1, 2]}},
    )
    replayed = record_event(
        episode_id, operation_id="op-keys", phase="auto", event_kind="k",
        payload={"beta": {"y": [1, 2], "x": "1"}, "alpha": 1},
    )
    assert replayed.event_id == original.event_id
    assert replayed.event_seq == original.event_seq
    assert replayed.observed_at == original.observed_at
    assert replayed.sealed_sha256() == original.sealed_sha256()
    assert len(read_episode_events(episode_id)) == 1


def test_array_order_is_part_of_the_identity(owner):
    episode_id, _ = owner
    record_event(episode_id, operation_id="op-order", phase="auto", event_kind="k",
                 payload={"steps": ["a", "b"]})
    with pytest.raises(EventIdentityConflict, match="payload"):
        record_event(episode_id, operation_id="op-order", phase="auto", event_kind="k",
                     payload={"steps": ["b", "a"]})
    assert [e.payload["steps"] for e in read_episode_events(episode_id)] == [("a", "b")]


def test_a_malformed_retry_is_refused_before_any_acknowledgement(owner):
    """Input validation runs ahead of BOTH branches, so a bad retry cannot ride the original in."""
    episode_id, _ = owner
    import memory.episode_events as episode_events

    original = record_event(episode_id, operation_id="op-mal", phase="auto",
                            event_kind="k", payload={"v": 1})
    episode_sram.reset()
    episode_sram.open_bucket(episode_id, pipeline_id=0)          # nothing acknowledged yet
    assert episode_sram.pending(episode_id) == []

    holds: list[str] = []
    real_hold = episode_events._hold_in_owner_bucket
    episode_events._hold_in_owner_bucket = lambda *a, **k: holds.append("held")
    try:
        with pytest.raises(ContinuityContractError):
            record_event(episode_id, operation_id="op-mal", phase="not-a-phase",
                         event_kind="k", payload={"v": 1})
        with pytest.raises(ContinuityContractError):
            record_event(episode_id, operation_id="op-mal", phase="auto",
                         event_kind="k", payload={"v": {1, 2}})
    finally:
        episode_events._hold_in_owner_bucket = real_hold
    assert holds == [], "a malformed retry reached acknowledgement"
    assert read_episode_events(episode_id)[0].sealed_sha256() == original.sealed_sha256()


def test_the_caller_cannot_mutate_the_payload_it_already_supplied(owner):
    """The compared and written copy is taken at the boundary, not shared with the caller."""
    episode_id, _ = owner
    supplied = {"v": {"nested": [1, 2]}}
    original = record_event(episode_id, operation_id="op-mutate", phase="auto",
                            event_kind="k", payload=supplied)
    supplied["v"]["nested"].append(3)
    supplied["added"] = True
    assert read_episode_events(episode_id)[0].sealed_sha256() == original.sealed_sha256()
    with pytest.raises(EventIdentityConflict, match="payload"):
        record_event(episode_id, operation_id="op-mutate", phase="auto",
                     event_kind="k", payload=supplied)


# ── host metadata comes from the bound context, never the payload ─────────────


def test_host_metadata_is_derived_from_the_bound_context_not_the_payload(owner):
    episode_id, _ = owner
    event = record_event(
        episode_id,
        operation_id="op-1",
        phase="auto",
        event_kind="k",
        payload={"thread_id": "SPOOFED", "producer": "SPOOFED", "boot_id": "SPOOFED"},
    )
    assert event.thread_id != "SPOOFED"
    assert event.boot_id != "SPOOFED"
    assert event.producer["component"] == CAPTURE_COMPONENT
    assert event.producer["authority"] == "host"
    assert event.evidence_class == "host_observation"


def test_the_capture_context_supplies_the_thread_and_component(owner):
    episode_id, _ = owner
    with capture_context(thread_id="thread-xyz", component="auto_loop.execute", component_version="t1"):
        event = record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert event.thread_id == "thread-xyz"
    assert event.producer["component"] == "auto_loop.execute"
    assert event.producer["component_version"] == "t1"


def test_an_episode_with_no_bound_thread_roots_its_own(owner):
    """Matches EpisodeStore.open's rule: no inherited thread means root-of-its-own."""
    episode_id, _ = owner
    event = record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert event.thread_id == episode_id


def test_an_occurred_at_the_host_did_not_record_is_not_captured(owner):
    episode_id, _ = owner
    event = record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert event.occurred_at.state == "not_captured"
    assert event.occurred_at.value is None
    supplied = record_event(
        episode_id,
        operation_id="op-2",
        phase="auto",
        event_kind="k",
        payload={},
        occurred_at="2026-03-04T05:06:06.500000+00:00",
    )
    assert supplied.occurred_at.state == "observed"


def test_the_event_validates_through_the_wo_02_contract(owner):
    episode_id, _ = owner
    event = record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload=PAYLOAD)
    assert isinstance(event, EpisodeEvent)
    assert EpisodeEvent.from_dict(event.to_dict()) == event
    for name in SEALED_EVENT_FIELDS:
        assert hasattr(event, name)


def test_an_invalid_phase_is_refused_before_anything_is_journalled(owner):
    episode_id, _ = owner
    with pytest.raises(ContinuityContractError):
        record_event(episode_id, operation_id="op-1", phase="reflection", event_kind="k", payload={})
    assert intake_journal.records() == ()


# ── the event reaches the owner bucket, and stays non-recallable ───────────────


def test_the_validated_event_is_held_in_the_owner_bucket(owner):
    episode_id, _ = owner
    event = record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k", payload=PAYLOAD)
    held = episode_sram.pending(episode_id)
    assert [item.kind for item in held] == ["event"]
    assert held[0].event_id == event.event_id


def test_a_pending_event_is_not_recallable(owner):
    """Journal arrival is not publication. WO-07 owns canonical publication.

    Checked two ways: the capture wrote nothing canonical, and its journal row is still
    PENDING rather than terminal — the two facts that together mean recall cannot see it.
    """
    from memory.engine.walker import COORD_INDEX

    episode_id, _ = owner
    before = dict(COORD_INDEX)
    event = record_event(episode_id, operation_id="op-1", phase="auto", event_kind="k",
                         payload={"needle": "wo03-capture-needle"})

    assert dict(COORD_INDEX) == before, "capture must not publish a canonical record"
    record = intake_journal.record_by_id(intake_journal._record_id(event.event_id))
    assert record.status == intake_journal.PENDING
    assert record.status not in intake_journal.TERMINAL_STATUSES
    assert not record.memory_id and not record.canonical_path, (
        "a pending event has no canonical binding, so nothing can recall it"
    )


def test_reading_an_episode_with_no_events_is_an_empty_tuple_not_an_error(owner):
    episode_id, _ = owner
    assert read_episode_events(episode_id) == ()
    assert read_episode_events("ep-never-existed") == ()


# ── concurrency: deterministic ordering, no sleeps ────────────────────────────


def test_concurrent_captures_on_one_owner_get_distinct_ordered_sequences(owner):
    episode_id, pipeline_id = owner
    barrier = threading.Barrier(4)
    results: dict[str, EpisodeEvent] = {}
    errors: list[BaseException] = []

    def _capture(token: str) -> None:
        try:
            barrier.wait(timeout=30)          # ordering control, not a sleep
            with episode_sram.bind_episode(episode_id, pipeline_id=pipeline_id):
                results[token] = record_event(
                    episode_id, operation_id=token, phase="auto", event_kind="k",
                    payload={"token": token},
                )
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_capture, args=(f"op-{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    sequences = sorted(event.event_seq for event in results.values())
    assert sequences == [1, 2, 3, 4], f"sequence collision: {sequences}"
    assert len({event.event_id for event in results.values()}) == 4
    assert len(read_episode_events(episode_id)) == 4


def test_concurrent_retries_of_one_token_still_yield_one_event(owner):
    episode_id, pipeline_id = owner
    barrier = threading.Barrier(4)
    seen: list[EpisodeEvent] = []
    errors: list[BaseException] = []

    def _capture() -> None:
        try:
            barrier.wait(timeout=30)
            with episode_sram.bind_episode(episode_id, pipeline_id=pipeline_id):
                seen.append(
                    record_event(episode_id, operation_id="op-same", phase="auto",
                                 event_kind="k", payload=PAYLOAD)
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_capture) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], f"a concurrent retry must be idempotent, not an error: {errors}"
    assert len({event.event_id for event in seen}) == 1
    assert len({event.event_seq for event in seen}) == 1
    assert len(read_episode_events(episode_id)) == 1


# ── WO-03A closure: recognition must not claim ordinary intake ────────────────


def test_ordinary_intake_is_not_misclassified_as_an_event_row(owner):
    """Recognition is the versioned event envelope, never an `event` word or key in content."""
    episode_id, _ = owner
    real = record_event(episode_id, operation_id="op-real", phase="auto", event_kind="k",
                        payload={"v": 1})

    assert episode_sram.hold_turn("user", "tell me about the event log") is True
    assert episode_sram.hold_perception("the event calendar for sealed_sha256 week") is True
    intake_journal.journal_recall_maintenance(
        owner_episode_id=episode_id, owner_pipeline_id=0, memory_id="event"
    )

    kinds = {record.kind for record in intake_journal.records()}
    assert {"turn", "perception", "recall", EVENT_JOURNAL_KIND} <= kinds

    events = read_episode_events(episode_id)
    assert [event.event_id for event in events] == [real.event_id], (
        "ordinary intake was misread as captured events"
    )


def test_a_memory_envelope_whose_user_content_mimics_an_event_is_left_alone(owner):
    """The mimic sits one level down, under `payload.payload`, where recognition does not look."""
    from memory import api

    episode_id, _ = owner
    real = record_event(episode_id, operation_id="op-real", phase="auto", event_kind="k",
                        payload={"v": 1})
    mimic = read_episode_events(episode_id)[0].to_dict()
    api.intake_memory(
        api.CanonicalEnvelope(
            cycle_id=episode_id,
            coord=[2, 0, 0],
            type="concept",
            id="mimic-memory",
            payload={"event": mimic, "sealed_sha256": real.sealed_sha256()},
            source="tests.continuity",
        ),
        event_id="source-event-mimic",
    )
    row = next(record for record in intake_journal.records() if record.kind == "memory")
    assert "event" not in row.payload, "a memory envelope has a fixed root key set"
    assert row.payload["payload"]["event"]["event_id"] == real.event_id, "the mimic is present"

    events = read_episode_events(episode_id)
    assert [event.event_id for event in events] == [real.event_id]


# ── WO-03A closure: positive controls for the confirmed-row binding ───────────


def test_unchanged_confirmation_preserves_the_original_identity(owner):
    """The ordinary path: confirmation reads the same row, so the same occurrence is held."""
    episode_id, _ = owner
    first = record_event(episode_id, operation_id="op-stable", phase="auto", event_kind="k",
                         payload={"v": 1})
    held = [item for item in episode_sram.pending(episode_id) if item.kind == "event"]
    assert len(held) == 1
    assert held[0].event.sealed_sha256() == first.sealed_sha256()
    assert held[0].event.observed_at == first.observed_at
    assert held[0].event.boot_id == first.boot_id

    again = record_event(episode_id, operation_id="op-stable", phase="auto", event_kind="k",
                         payload={"v": 1})
    assert again.to_dict() == first.to_dict()
    assert len([item for item in episode_sram.pending(episode_id) if item.kind == "event"]) == 1


def test_a_process_interruption_is_never_converted_into_a_capture_failure(owner, monkeypatch):
    """`KeyboardInterrupt` and `SystemExit` are not retryable capture results."""
    episode_id, _ = owner
    import memory.episode_events as episode_events

    for interruption in (KeyboardInterrupt, SystemExit):
        def _interrupt(*_a, **_k):
            raise interruption("operator stopped the process")

        with monkeypatch.context() as patched:
            patched.setattr(episode_events, "_confirmed_durable", _interrupt)
            with pytest.raises(interruption):
                record_event(episode_id, operation_id=f"op-{interruption.__name__}", phase="auto",
                             event_kind="k", payload={})
        assert [item for item in episode_sram.pending(episode_id) if item.kind == "event"] == []
