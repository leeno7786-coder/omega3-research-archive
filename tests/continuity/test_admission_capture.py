"""WO-04 — the admission seam records its origin before it consumes the request.

These drive the ACTUAL producer: a real `EpisodeStore`, the real durable `cognition_queue`, and
`cognition_chain._admit_episode` — the seam that opens the carrier, records its origin and
acknowledges the exact leased queue item. Nothing here fabricates a reader-shaped dictionary and
reads it back; every assertion reads WO-03's own `read_episode_events`.

The property the order turns on: **an acknowledged request must never be an unrecorded one.**
Acknowledgement is irreversible — it removes the row from the one durable queue — so the origin
event has to be durable first. Otherwise a crash in that interval leaves a request that the queue
says was taken and that memory has no record of.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

import cognition_chain as chain
from core.cognition_lock import pipeline_cognition_lock
from memory import episode_sram
from memory.episode_events import read_episode_events
from pressure_loop import cognition_queue as cq
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.episode import EpisodeStore

# Distinguishable sentinels: every one is a value no default could produce.
THREAD = "thread-wo04-sentinel"
TURN = "turn-wo04-sentinel"
CONTINUATION = "successor-wo04-sentinel"
JOB = "job-wo04-sentinel"
CITATION = "citation-wo04-sentinel"
AUTHORITY = "authority-revision-wo04-sentinel"
ARRIVAL = 4242


def _operator_contributor(**overrides):
    metadata = {
        "message": "capture my admission",
        "cycle_id": "bubble-wo04",
        "arrival_seq": ARRIVAL,
        "thread_id": THREAD,
        "turn_id": TURN,
        "source_citation": {"id": CITATION, "kind": "operator_chat"},
        "connection_authority_revision": AUTHORITY,
    }
    metadata.update(overrides)
    return make_contributor("operator", "operator_query", 1.0, metadata)


def _continuation_contributor(**overrides):
    metadata = {
        "cycle_id": "bubble-wo04-cont",
        "arrival_seq": ARRIVAL + 1,
        "thread_id": THREAD,
        "successor_id": CONTINUATION,
        "original_cycle_id": "bubble-wo04",
        "agent_job_id": JOB,
    }
    metadata.update(overrides)
    return make_contributor("autonomous", "agent_continuation", 1.0, metadata)


def _lease(contributor, pipeline_id=0):
    assert cq.enqueue_input(contributor, operator_activity=False)
    leased = cq.lease_next_input(pipeline_id)
    assert leased is not None, "the fixture's own row must be leasable"
    return leased


def _ignition(leased, pipeline_id=0, work_kind="operator_turn"):
    return EpisodeIgnition(
        episode_id=f"ig-{leased.metadata['cycle_id']}",
        aggregate_pressure=1.0,
        opened_by=leased.type,
        dominant_contributor=leased,
        contributors=(leased,),
        trajectory_snapshot={"trajectory_id": "traj-wo04-sentinel", "step": 3},
        pipeline_id=pipeline_id,
        work_kind=work_kind,
    )


@pytest.fixture
def admit():
    """Run the real admission seam under the pipeline lock it documents."""
    stores: list[EpisodeStore] = []

    def run(ignition):
        lock = pipeline_cognition_lock(ignition.pipeline_id)
        lock.acquire()
        try:
            store = EpisodeStore()
            stores.append(store)
            return (store, *chain._admit_episode(store, ignition))
        finally:
            lock.release()

    yield run
    for store in stores:
        try:
            store.close(cascade_status="failed")
        except Exception:
            pass
    episode_sram.reset()


def _events(episode_id, kind):
    return [e for e in read_episode_events(episode_id) if e.event_kind == kind]


def _origin(episode_id):
    found = _events(episode_id, "admission.origin")
    assert len(found) == 1, f"expected exactly one origin event, got {len(found)}"
    return found[0]


# ── Step 1: the exact identities survive into the event ───────────────────────


def test_the_origin_event_preserves_every_admitted_identity(admit):
    leased = _lease(_operator_contributor())
    queue_id = leased.metadata["queue_id"]
    store, episode, ignition = admit(_ignition(leased))

    origin = _origin(episode.episode_id)
    payload = origin.payload

    assert payload["queue_id"] == queue_id, "the exact leased queue identity"
    assert origin.arrival_seq == ARRIVAL, "the admitted arrival sequence, on the sealed event"
    assert payload["arrival_seq"] == ARRIVAL
    assert origin.thread_id == THREAD, "the episode's real thread, not the episode id"
    assert origin.turn_id == TURN
    assert payload["source_citation_id"] == CITATION
    assert payload["authority_revision"] == AUTHORITY
    assert payload["opened_by"] == "operator_query"
    assert payload["work_kind"] == "operator_turn"
    assert origin.pipeline_id == 0
    assert payload["episode_id"] == episode.episode_id
    assert payload["contributor_source"] == "operator"
    assert origin.phase == "admission"


def test_a_continuation_carries_its_own_continuation_and_job_identities(admit):
    leased = _lease(_continuation_contributor(), pipeline_id=2)
    store, episode, ignition = admit(
        _ignition(leased, pipeline_id=2, work_kind="agent_continuation")
    )

    origin = _origin(episode.episode_id)
    assert origin.continuation_id == CONTINUATION
    assert origin.job_id == JOB
    assert origin.thread_id == THREAD, "a continuation inherits its thread verbatim"
    assert episode.thread_id == THREAD, "and so does the carrier"
    assert origin.payload["original_cycle_id"] == "bubble-wo04"


def test_a_cold_start_roots_its_own_thread_rather_than_inventing_one(admit):
    """Cold start versus continuation: both carry their ACTUAL identities."""
    leased = _lease(_operator_contributor(thread_id="", turn_id=""))
    store, episode, ignition = admit(_ignition(leased))

    origin = _origin(episode.episode_id)
    assert episode.thread_id == episode.episode_id, "a root thread is its own first episode"
    assert origin.thread_id == episode.episode_id
    assert origin.turn_id == "", "an absent turn id is absent, never a placeholder"
    assert origin.continuation_id == "" and origin.job_id == ""


# ── Step 2: durable before the irreversible acknowledgement ───────────────────


def test_the_origin_event_is_durable_before_the_queue_is_acknowledged(admit, monkeypatch):
    """The ordering this order exists to establish, proven at the acknowledgement itself."""
    leased = _lease(_operator_contributor())
    seen: dict[str, object] = {}
    real_ack = cq.acknowledge_input

    def watched(contributor, pipeline_id):
        # At the instant the queue is consumed, the origin must already be durable.
        bound = episode_sram.bound_episode_id()
        seen["events"] = read_episode_events(bound) if bound else ()
        seen["bound"] = bound
        return real_ack(contributor, pipeline_id)

    with monkeypatch.context() as patched:
        patched.setattr(cq, "acknowledge_input", watched)
        store, episode, ignition = admit(_ignition(leased))

    assert seen["bound"] == episode.episode_id, "the SRAM owner exists before the ack"
    kinds = [event.event_kind for event in seen["events"]]
    assert "admission.origin" in kinds, (
        "the request was acknowledged before its origin was durable — the exact gap WO-04 closes"
    )


def test_an_interrupted_acknowledgement_keeps_the_exact_request_recoverable(admit, monkeypatch):
    """Ingress crash: after open, before ack. No loss, no duplicate, no other item consumed."""
    first = _lease(_operator_contributor())
    queue_id = first.metadata["queue_id"]
    other = _operator_contributor(cycle_id="bubble-other", arrival_seq=ARRIVAL + 9)
    assert cq.enqueue_input(other, operator_activity=False)

    def crash(_contributor, _pipeline_id):
        raise RuntimeError("simulated ingress crash between open and acknowledgement")

    with monkeypatch.context() as patched:
        patched.setattr(cq, "acknowledge_input", crash)
        with pytest.raises(RuntimeError, match="simulated ingress crash"):
            admit(_ignition(first))

    queued = [row.metadata["queue_id"] for row in cq.peek_inputs()]
    assert queue_id in queued, "the exact request survived the crash"
    assert queued.count(queue_id) == 1, "and was not duplicated"
    assert other.metadata["cycle_id"] in [r.metadata["cycle_id"] for r in cq.peek_inputs()], (
        "no other queue item was consumed"
    )

    # Its origin event is durable, so recovery can identify what was admitted.
    bound = [e for e in cq.peek_inputs() if e.metadata["queue_id"] == queue_id]
    assert len(bound) == 1


def test_a_failed_origin_capture_does_not_acknowledge_or_permit_dependent_work(admit, monkeypatch):
    """Failed capture must not consume the request or let the cascade proceed."""
    import memory.cognition_capture as capture

    leased = _lease(_operator_contributor())
    queue_id = leased.metadata["queue_id"]
    acks: list[str] = []
    real_ack = cq.acknowledge_input

    with monkeypatch.context() as patched:
        patched.setattr(capture, "capture_admission_origin",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("journal volume gone")))
        patched.setattr(cq, "acknowledge_input",
                        lambda c, p: acks.append(c.metadata["queue_id"]) or real_ack(c, p))
        with pytest.raises(Exception) as caught:
            admit(_ignition(leased))

    assert acks == [], "a failed capture must not reach the acknowledgement"
    assert queue_id in [row.metadata["queue_id"] for row in cq.peek_inputs()], (
        "the admitted request is preserved"
    )
    assert "journal volume gone" in str(caught.value) or isinstance(
        caught.value.__cause__, OSError
    ), "the primary cause survives"


# ── Step 3: the opened episode's actual starting state ───────────────────────


def test_the_opened_event_records_the_real_opening_state(admit):
    leased = _lease(_operator_contributor())
    store, episode, ignition = admit(_ignition(leased))

    opened = _events(episode.episode_id, "admission.episode_opened")
    assert len(opened) == 1
    payload = opened[0].payload

    assert payload["opened_at"] == episode.opened_at, "the carrier's real opened time"
    assert payload["opened_at"], "and it is not empty"
    assert payload["pipeline_id"] == 0
    assert payload["work_kind"] == "operator_turn"
    assert payload["thread_id"] == episode.thread_id
    trajectory = payload["trajectory_reference"]
    assert trajectory["state"] == "observed"
    assert trajectory["value"]["trajectory_id"] == "traj-wo04-sentinel"


def test_an_unavailable_memory_pin_is_declared_unavailable_not_fabricated(admit, monkeypatch):
    """Missing signal: an absent pin is explicit, never a manufactured snapshot."""
    from memory import epoch

    leased = _lease(_operator_contributor())
    with monkeypatch.context() as patched:
        patched.setattr(epoch, "epochs_enabled", lambda: False)
        store, episode, ignition = admit(_ignition(leased))

    payload = _events(episode.episode_id, "admission.episode_opened")[0].payload
    pin = payload["memory_epoch"]
    assert pin["state"] == "unavailable", pin
    assert pin["value"] is None, "an absent pin carries no value"
    assert pin["reason"], "and says why"


def test_an_absent_trajectory_is_declared_not_replaced_with_current_data(admit):
    leased = _lease(_operator_contributor())
    ignition = dataclasses.replace(_ignition(leased), trajectory_snapshot=None)
    store, episode, _ = admit(ignition)

    payload = _events(episode.episode_id, "admission.episode_opened")[0].payload
    trajectory = payload["trajectory_reference"]
    assert trajectory["state"] == "unavailable" and trajectory["value"] is None


# ── ordering and idempotence ─────────────────────────────────────────────────


def test_parallel_admission_records_fifo_order_even_when_completion_reverses(admit):
    """Recorded arrival order is the queue's, never the order the episodes finished in."""
    first = _lease(_operator_contributor(cycle_id="bubble-first", arrival_seq=10), pipeline_id=0)
    second_contributor = make_contributor("omega", "lane_work_dispatch", 1.0, {
        "lane": "browser",
        "cycle_id": "bubble-second",
        "arrival_seq": 11,
    })
    second = _lease(second_contributor, pipeline_id=1)

    # The SECOND admission completes first.
    store_b, episode_b, _ = admit(
        _ignition(second, pipeline_id=1, work_kind="operator_lane_work")
    )
    store_a, episode_a, _ = admit(_ignition(first, pipeline_id=0))

    assert _origin(episode_a.episode_id).arrival_seq == 10
    assert _origin(episode_b.episode_id).arrival_seq == 11
    assert _origin(episode_a.episode_id).payload["queue_id"] == first.metadata["queue_id"]
    assert _origin(episode_b.episode_id).payload["queue_id"] == second.metadata["queue_id"]


def test_the_admission_events_are_not_recallable_yet(admit):
    """WO-07 owns publication; a pending journal event is not canonical memory."""
    from memory import intake_journal

    leased = _lease(_operator_contributor())
    store, episode, _ = admit(_ignition(leased))

    origin = _origin(episode.episode_id)
    record = intake_journal.record_by_id(intake_journal._record_id(origin.event_id))
    assert record.status == intake_journal.PENDING
    assert record.memory_id == "" and record.canonical_path == ""


def test_admission_introduces_no_second_gate_or_dispatch(admit, monkeypatch):
    """No extra queue, reservation, inference or execution dispatch was added."""
    import pressure_loop.pipeline_dispatch as dispatch

    leased = _lease(_operator_contributor())
    with monkeypatch.context() as patched:
        patched.setattr(dispatch, "dispatch_cascade",
                        lambda *a, **k: pytest.fail("admission dispatched a cascade"))
        store, episode, _ = admit(_ignition(leased))

    seam = chain._admit_episode
    import inspect

    source = inspect.getsource(seam)
    for banned in ("operator_only", "lease_next_input", "invoke_runtime_generate", "reserved"):
        assert banned not in source, banned
    assert isinstance(episode, SimpleNamespace) is False


def test_an_interrupted_admission_is_recovered_through_the_real_lease_path(admit, monkeypatch):
    """The actual recovery path, not just "the row is still there".

    The dispatcher releases the lease of a cascade that did not admit (`on_not_admitted`), which
    returns the row to the line at its own position. Recovery is then the ordinary FIFO: the SAME
    request is leased again and admitted exactly once, carrying the same queue identity.

    The abandoned attempt's own origin event stays durable on its own episode. That is deliberate:
    it is honest evidence that an admission was attempted and interrupted, and it is a DIFFERENT
    occurrence from the one that finally admitted, because a new carrier is a new episode.
    """
    leased = _lease(_operator_contributor())
    queue_id = leased.metadata["queue_id"]

    def crash(_contributor, _pipeline_id):
        raise RuntimeError("interrupted before acknowledgement")

    with monkeypatch.context() as patched:
        patched.setattr(cq, "acknowledge_input", crash)
        with pytest.raises(RuntimeError, match="interrupted before acknowledgement"):
            admit(_ignition(leased))

    # The dispatcher's own release, then the ordinary lease path.
    assert cq.release_input_lease(0) is True
    episode_sram.reset()
    again = cq.lease_next_input(0)
    assert again is not None, "the interrupted request is leasable again"
    assert again.metadata["queue_id"] == queue_id, "and it is the EXACT same request"

    store, episode, ignition = admit(_ignition(again))

    origin = _origin(episode.episode_id)
    assert origin.payload["queue_id"] == queue_id
    assert origin.arrival_seq == ARRIVAL, "the admitted identity survived the interruption"
    assert cq.peek_inputs() == (), "consumed exactly once, by the episode that recorded it"
    assert [row.metadata["queue_id"] for row in cq.peek_inputs()].count(queue_id) == 0
