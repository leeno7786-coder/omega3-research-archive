"""WO-07 Steps 4–6 — the terminus recovers from its own durable state, once, in stamp order.

A terminus that raises leaves its episode open, and the next close runs it again from the top.
Before WO-07 that retry minted a NEW finish-line stamp and wrote a SECOND lobe-9 record — the
content differed (new stamp, new close time) so the drain's content-derived identity could not
see the first — and the window hydrated whichever had the later close time.

Now the durable state decides:

* a sealed record the drain already COMMITTED is replayed: no second seal, no second write, no
  new ``commit_seq``;
* a record written to disk whose journal acknowledgement was lost is reconciled from the file
  (``verify_canonical_binding``), not rewritten;
* a sealed record that never reached disk is SUPERSEDED — visibly, with the reason on its row —
  so the burned stamp it carried can never land;
* a rejected required record blocks finalization, and late intake is preserved, not dropped;
* canonical durability and recall publication are separate facts, and a window cohort is
  released only when its members are actually published.
"""

from __future__ import annotations

import threading

import pytest

from pressure_loop.episode import EpisodeStore
from tests.helpers import continuity_terminus as ct


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _view():
    import memory.episode_commit_view as view

    return view


def _snapshot():
    from tests.continuity.test_episode_commit_view import _snapshot as snapshot

    return snapshot()


def _fail_once(monkeypatch, target, name, message, *, after: bool = False) -> dict:
    real = getattr(target, name)
    state = {"calls": 0}

    def wrapper(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1 and not after:
            raise RuntimeError(message)
        result = real(*args, **kwargs)
        if state["calls"] == 1 and after:
            raise RuntimeError(message)
        return result

    monkeypatch.setattr(target, name, wrapper)
    return state


def _sealed_rows(episode_id: str):
    from memory import intake_journal
    from memory.episode_record_seal import episode_record_id

    wanted = episode_record_id(episode_id)
    return [row for row in intake_journal.records()
            if row.kind == "memory" and str((row.payload or {}).get("id") or "") == wanted]


# ── Step 4: a replayed, already-committed episode keeps its place ────────────


def test_a_failure_after_the_canonical_commit_replays_without_a_new_commit_seq(monkeypatch):
    import memory.pathway_performance as pathway

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _fail_once(monkeypatch, pathway, "rebuild_pathway_projection", "injected after commit")
    with pytest.raises(RuntimeError, match="injected after commit"):
        ct.close(store)
    first_stamp = dict(carrier.timekeeper)
    first_ref = _view().episode_record_ref(carrier.episode_id)
    assert first_ref is not None, "the canonical commit that preceded the failure is not visible"

    ct.close(store)
    assert carrier.timekeeper["commit_seq"] == first_stamp["commit_seq"], (
        "a replayed, already-committed episode was given a new place in the memory sequence"
    )
    assert _view().episode_record_ref(carrier.episode_id) == first_ref
    docs = ct.canonical_episode_documents(carrier.episode_id)
    assert len(docs) == 1, f"one episode left {len(docs)} lobe-9 records"
    record = _view().resolve_episode_commit(first_ref, snapshot=_snapshot()).record
    assert record.temporal["finish_line"]["commit_seq"] == first_stamp["commit_seq"]


def test_a_lost_journal_acknowledgement_is_reconciled_from_the_canonical_file(monkeypatch):
    """The drain wrote the record, then crashed before ``mark_committed``."""
    from memory import intake_journal
    from memory.episode_record_seal import episode_record_id

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    record_id = episode_record_id(carrier.episode_id)
    real = intake_journal.mark_committed
    state = {"tripped": False}

    def lose_ack(record_id_arg, *, memory_id, canonical_path="", note=""):
        if memory_id == record_id and not state["tripped"]:
            state["tripped"] = True
            raise RuntimeError("injected: crash between the canonical write and its ack")
        return real(record_id_arg, memory_id=memory_id, canonical_path=canonical_path, note=note)

    monkeypatch.setattr(intake_journal, "mark_committed", lose_ack)
    with pytest.raises(RuntimeError, match="injected"):
        ct.close(store)
    burned = dict(carrier.timekeeper)
    written = ct.canonical_episode_documents(carrier.episode_id)
    assert len(written) == 1 and written[0]["id"] == record_id

    ct.close(store)
    after = ct.canonical_episode_documents(carrier.episode_id)
    assert [doc["id"] for doc in after] == [record_id]
    assert after[0]["text"] == written[0]["text"], "the reconciled record was rewritten"
    assert carrier.timekeeper["commit_seq"] == burned["commit_seq"]
    assert [row.status for row in _sealed_rows(carrier.episode_id)] == ["committed"]


def test_a_sealed_record_that_never_reached_disk_is_superseded_visibly(monkeypatch):
    """The region raised after capture and before the drain wrote anything."""
    import deepsleep_loop.loop as loop
    from memory import intake_journal

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _fail_once(monkeypatch, loop, "_flush_episode_sram", "injected before the drain")
    with pytest.raises(RuntimeError, match="injected before the drain"):
        ct.close(store)
    stale_seq = carrier.timekeeper["commit_seq"]

    ct.close(store)
    rows = _sealed_rows(carrier.episode_id)
    statuses = sorted(row.status for row in rows)
    assert statuses == sorted([intake_journal.COMMITTED, intake_journal.CONSUMED]), statuses
    stale = next(row for row in rows if row.status == intake_journal.CONSUMED)
    assert "supersed" in stale.note and str(stale_seq) in stale.note
    assert carrier.timekeeper["commit_seq"] > stale_seq, (
        "the burned stamp of a never-committed seal must not be reused"
    )
    assert len(ct.canonical_episode_documents(carrier.episode_id)) == 1


# ── Step 5: canonical durability is not recall publication ──────────────────


def test_a_failed_program0_publication_is_canonical_and_appended_but_not_released(
    monkeypatch,
):
    import deepsleep_loop.loop as loop
    from pressure_loop import window_lane
    from tests.continuity.test_episode_commit_view import _served_baseline

    _served_baseline()
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _fail_once(monkeypatch, loop, "_publish_projection_after_deepsleep",
               "injected: publication failed")
    with pytest.raises(RuntimeError, match="publication failed"):
        ct.close(store)

    ref = _view().episode_record_ref(carrier.episode_id)
    assert _view().resolve_episode_commit(ref, snapshot=_snapshot()).memory_commit_status == (
        "canonical_committed"
    )
    assert store.episode_in_reflection_window(carrier.episode_id), (
        "the canonical checkpoint append was lost when publication failed"
    )
    assert window_lane.pressure_contributor() is None, (
        "an unpublished checkpoint member was released for reflection"
    )

    ct.close(store)
    assert _view().resolve_episode_commit(ref, snapshot=_snapshot()).memory_commit_status == (
        "recall_published"
    )
    assert store.episode_in_reflection_window(carrier.episode_id)


# ── Step 6: rejected, uncertain and late intake ─────────────────────────────


def test_a_rejected_required_record_blocks_finalization(monkeypatch):
    from memory.episode_record_seal import episode_record_id
    from tests.continuity.test_episode_commit_view import _reject_writes_of

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _reject_writes_of(monkeypatch, episode_record_id(carrier.episode_id))
    with pytest.raises(Exception, match="reject"):
        ct.close(store)
    assert store.current() is carrier, "a rejected required record finalized the episode"
    assert [row.status for row in _sealed_rows(carrier.episode_id)] == ["rejected"]


def test_intake_appended_during_the_drain_is_preserved_and_blocks_cleanup(monkeypatch):
    from memory import episode_sram, intake_journal
    from memory.api import CanonicalEnvelope, intake_memory

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    eid = carrier.episode_id
    import memory.api as api

    real = api.write_memory
    state = {"late": False}

    def late_producer() -> None:
        # Another producer thread bound to this episode, as a lane-local writer is: its intake
        # is HELD in the bucket the drain is walking, never written through.
        with episode_sram.bind_episode(eid, pipeline_id=0):
            late = CanonicalEnvelope(
                cycle_id=eid, coord=[2, 1, 0], type="fact",
                payload={"title": "late fact", "summary": "arrived during the drain",
                         "text": "LATE-SENTINEL: arrived during the drain"},
                source="wo07.late_intake",
            )
            state["receipt"] = intake_memory(late, propose=False, bypass_guardian=True)

    def write_then_intake(envelope, *args, **kwargs):
        receipt = real(envelope, *args, **kwargs)
        if not state["late"]:
            state["late"] = True
            producer = threading.Thread(target=late_producer)
            producer.start()
            producer.join(timeout=30)
            assert state["receipt"].validation_status == "held"
        return receipt

    monkeypatch.setattr(api, "write_memory", write_then_intake)
    with pytest.raises(Exception):
        ct.close(store)
    assert store.current() is carrier, "pending intake did not prevent finalization"
    late_rows = [row for row in intake_journal.records()
                 if row.kind == "memory" and "LATE-SENTINEL" in str(row.payload)]
    assert [row.status for row in late_rows] == [intake_journal.PENDING]
    event_rows = [row for row in intake_journal.records()
                  if row.kind == "episode_event" and row.origin_episode_id == eid]
    assert all(row.status == intake_journal.PENDING for row in event_rows), (
        "cleanup acknowledged the episode's events while required intake was still pending"
    )

    monkeypatch.setattr(api, "write_memory", real)
    ct.close(store)
    late_rows = [row for row in intake_journal.records()
                 if row.kind == "memory" and "LATE-SENTINEL" in str(row.payload)]
    assert [row.status for row in late_rows] == [intake_journal.COMMITTED]
    assert not episode_sram.is_open(eid)


def test_a_successful_close_leaves_no_pending_work_for_the_episode():
    from memory import episode_sram, intake_journal

    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    ct.close(store)
    pending = intake_journal.pending_records(owner_episode_id=carrier.episode_id)
    assert pending == (), f"a closed episode still owes {len(pending)} journal rows"
    assert not episode_sram.is_open(carrier.episode_id)


# ── ordering follows the existing stamp authority ───────────────────────────


def test_parallel_termini_follow_the_finish_line_stamp_and_nothing_else():
    store = EpisodeStore()
    barrier = threading.Barrier(2)
    carriers: dict[int, object] = {}
    errors: list[BaseException] = []

    def lane(pipeline_id: int) -> None:
        try:
            carrier = ct.dress_learn(ct.open_carrier(store, pipeline_id=pipeline_id),
                                     domain=f"domain_{pipeline_id}")
            carriers[pipeline_id] = carrier
            barrier.wait(timeout=30)
            ct.close(store)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=lane, args=(pid,)) for pid in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors, errors

    seqs = []
    for carrier in carriers.values():
        record = _view().resolve_episode_commit(
            _view().episode_record_ref(carrier.episode_id), snapshot=_snapshot()
        ).record
        finish = record.temporal["finish_line"]
        assert finish["commit_seq"] == carrier.timekeeper["commit_seq"]
        assert record.temporal["coverage_clock"]["commit_seq"] == finish["commit_seq"], (
            "the record carries a second ordering number beside the stamp"
        )
        seqs.append((finish["commit_seq"], carrier.episode_id))
    assert len({seq for seq, _ in seqs}) == 2
    window = store.get_active_window()
    in_window = [eid for eid in window.episodes if eid in {e for _, e in seqs}]
    assert in_window == [eid for _, eid in sorted(seqs)], (
        "the window order is not the finish-line order"
    )
