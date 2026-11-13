"""WO-07 Step 1 / 4 — a sealed episode survives the real terminus and comes back by its exact ref.

The order's seed: a real isolated DeepSleep terminus commits a sentinel thread id and a FAILED
acceptance criterion through canonical capture, then the record is hydrated by the exact
``CanonicalRef`` and compared for equality. Before WO-07 the lobe-9 record was the enriched body
under a content-derived id minted at drain time, so nothing outside the drain could name it, and
the episode's own captured events stayed ``pending`` forever: WO-03 captured them, and nothing
published them.

Every value asserted here was produced by a real producer (WO-05's evidence capture, the real
Meta B / Global B fold, the real Omega lane parser) and carried by the real terminus. Nothing is
read back from a fixture.
"""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import EpisodeStore
from schema.continuity_json import CanonicalRef, content_hash
from schema.episode_record import MEMORY_COMMIT_STATUSES, EpisodeRecord
from tests.helpers import continuity_terminus as ct


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _view():
    import importlib

    try:
        return importlib.import_module("memory.episode_commit_view")
    except ModuleNotFoundError:
        pytest.fail(
            "memory.episode_commit_view does not exist: nothing can resolve a canonical episode "
            "by its exact reference, so a window cannot tell a committed member from a lost one"
        )


def _seal_module():
    import importlib

    try:
        return importlib.import_module("memory.episode_record_seal")
    except ModuleNotFoundError:
        pytest.fail("memory.episode_record_seal does not exist: DeepSleep seals no record")


def _snapshot():
    """A SnapshotRef pinned to the recall projection being served right now."""
    from core.timekeeper import last_coverage_clock
    from tests.helpers.continuity_records import make_snapshot_ref

    view = _view()
    clock = last_coverage_clock()
    return make_snapshot_ref(
        generation_id=view.published_projection_id(),
        coverage_clock={"clock_epoch_id": clock.clock_epoch_id, "commit_seq": clock.commit_seq},
    )


def _closed_failed_build():
    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    pack = ct.close(store)
    return carrier, pack


def _ref_for(episode_id: str) -> CanonicalRef:
    ref = _view().episode_record_ref(episode_id)
    assert ref is not None, (
        f"episode {episode_id!r} closed through the terminus but no committed sealed record can "
        "be found for it"
    )
    return ref


# ── the seed ────────────────────────────────────────────────────────────────


def test_a_failed_criterion_and_its_sentinel_thread_round_trip_by_the_exact_ref():
    carrier, pack = _closed_failed_build()
    eid = carrier.episode_id

    ref = _ref_for(eid)
    reported = pack.projection_state.get("episode_record")
    assert isinstance(reported, dict), (
        "the terminus reports no sealed record: its caller cannot name what it committed"
    )
    assert CanonicalRef.from_dict(reported["ref"]) == ref

    commit = _view().resolve_episode_commit(ref, snapshot=_snapshot())
    record = commit.record
    assert isinstance(record, EpisodeRecord)
    assert record.content_sha256() == ref.content_sha256
    assert EpisodeRecord.from_dict(record.to_dict()) == record

    assert record.identity["episode_id"] == eid
    assert record.identity["thread_id"] == ct.SENTINEL_THREAD
    assert list(record.objective["acceptance_criteria"]) == [ct.CRITERION]
    assert record.outcome.verification_status == "failed"
    assert record.outcome.task_outcome == "failure"
    assert record.outcome.cascade_status == "completed"


def test_times_errors_decision_and_evidence_refs_survive_the_round_trip():
    carrier, _pack = _closed_failed_build()
    record = _view().resolve_episode_commit(_ref_for(carrier.episode_id), snapshot=_snapshot()).record

    # times — the one ordering authority, and the carrier's own open/close observations
    assert record.temporal["finish_line"]["commit_seq"] == carrier.timekeeper["commit_seq"]
    assert record.temporal["finish_line"]["commit_ms"] == carrier.timekeeper["commit_ms"]
    assert record.temporal["coverage_clock"]["commit_seq"] == carrier.timekeeper["commit_seq"]
    assert record.temporal["opened_at"].value == carrier.opened_at
    assert record.temporal["closed_at"].is_observed and record.temporal["closed_at"].value

    # errors — the tool's own failure text, by value, from its captured event
    events = {event.event_kind: event for event in record.events["by_value"]}
    assert ct.SENTINEL_ERROR in json.dumps(events["auto.tool_result"].to_dict())
    assert record.events["coverage"]["retained"] == len(record.events["by_value"])
    for event in record.events["by_value"]:
        assert record.events["integrity"][event.event_id] == event.content_sha256()

    # the per-episode Omega decision, validated and verbatim
    control = record.metacognition["control"]
    assert control.decision == "PRESSURE"
    assert control.pressure.reason == ct.SENTINEL_REASON
    assert all(row["status"] == "proposed" for row in record.metacognition["proposals"])

    # evidence references — the failed check cites the verification event that failed it
    verification = events["auto.verification_result"]
    assert list(record.outcome.assessments["verification_status"]["check_refs"]) == [
        verification.event_id
    ]


def test_the_published_read_returns_the_same_sentinels_through_the_recall_index():
    carrier, _pack = _closed_failed_build()
    ref = _ref_for(carrier.episode_id)

    commit = _view().resolve_episode_commit(ref, snapshot=_snapshot())
    assert commit.memory_commit_status == "recall_published"
    assert commit.memory_commit_status in MEMORY_COMMIT_STATUSES

    from memory.engine.walker import COORD_INDEX, load_coord_index, path_for_id

    load_coord_index()
    assert ref.record_id in COORD_INDEX, "the recall index does not contain the sealed record"
    document = json.loads(open(path_for_id(ref.record_id), encoding="utf-8").read())
    published = EpisodeRecord.from_dict(json.loads(document["text"])["episode_record"])
    assert published.identity["thread_id"] == ct.SENTINEL_THREAD
    assert published.content_sha256() == ref.content_sha256


def test_the_canonical_record_id_is_stable_and_names_its_episode():
    carrier, _pack = _closed_failed_build()
    ref = _ref_for(carrier.episode_id)

    assert ref.record_id == _seal_module().episode_record_id(carrier.episode_id)
    docs = ct.canonical_episode_documents(carrier.episode_id)
    assert [doc["id"] for doc in docs] == [ref.record_id], "exactly one lobe-9 record per episode"


# ── Step 4: no recursive receipt ────────────────────────────────────────────


def test_the_sealed_content_never_contains_its_own_receipt():
    carrier, _pack = _closed_failed_build()
    ref = _ref_for(carrier.episode_id)
    record = _view().resolve_episode_commit(ref, snapshot=_snapshot()).record
    body = record.to_dict()

    text = json.dumps(body)
    assert ref.content_sha256 not in text, "the record hashes its own future receipt"
    assert ref.record_id not in text, "the record names the canonical id it was about to receive"
    assert "memory_commit_status" not in text
    assert content_hash(body) == ref.content_sha256, (
        "the content hash is computed over the record alone, never over a receipt bound to it"
    )


def test_the_external_receipt_binding_validates_without_being_inside_the_content():
    carrier, _pack = _closed_failed_build()
    ref = _ref_for(carrier.episode_id)
    commit = _view().resolve_episode_commit(ref, snapshot=_snapshot())

    receipt = commit.canonical_receipt
    assert receipt["memory_id"] == ref.record_id
    assert receipt["status"] == "committed"
    assert receipt["content_sha256"] == ref.content_sha256
    assert receipt["journal_record_id"] not in json.dumps(commit.record.to_dict())


# ── sealing is pure content assembly ────────────────────────────────────────


def test_sealing_is_pure_and_repeatable(monkeypatch):
    from core.commit_region import serialized_commit
    from memory import intake_journal
    from memory.episode_events import read_episode_events

    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    seal = _seal_module().seal_episode_record
    events = read_episode_events(carrier.episode_id)
    before = len(intake_journal.records())
    with serialized_commit(carrier):
        first = seal(carrier, events=events, effects=())
        second = seal(carrier, events=events, effects=())
    assert first.content_sha256() == second.content_sha256()
    assert len(intake_journal.records()) == before, "sealing wrote something"


def test_sealing_outside_the_commit_region_is_refused():
    from memory.episode_events import read_episode_events

    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    with pytest.raises(Exception) as caught:
        _seal_module().seal_episode_record(
            carrier, events=read_episode_events(carrier.episode_id), effects=()
        )
    assert "finish" in str(caught.value).lower() or "stamp" in str(caught.value).lower()


def test_a_failed_omega_fold_still_seals_with_its_metacognition_unavailable():
    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    carrier.meta["omega_fold_failure"] = "control contract rejected: decision missing"
    global_ = dict(carrier.global_)
    global_.pop("omega_result", None)
    carrier.global_ = global_
    ct.close(store, cascade_status="failed")

    record = _view().resolve_episode_commit(
        _ref_for(carrier.episode_id), snapshot=_snapshot()
    ).record
    control = record.metacognition["control"]
    assert getattr(control, "state", None) == "unavailable"
    assert "control contract rejected" in control.reason
    assert record.metacognition["proposals"] == ()


def test_the_episode_events_are_published_into_the_record_and_acknowledged():
    """WO-03 left captured events PENDING for WO-07 to publish. Publication is the sealed record
    carrying them by value; the acknowledgement is the journal row leaving pending, bound to it."""
    from memory import intake_journal

    carrier, _pack = _closed_failed_build()
    ref = _ref_for(carrier.episode_id)
    record = _view().resolve_episode_commit(ref, snapshot=_snapshot()).record
    sealed = {event.event_id for event in record.events["by_value"]}

    rows = [row for row in intake_journal.records()
            if row.kind == "episode_event" and row.origin_episode_id == carrier.episode_id]
    assert rows and {row.event_id for row in rows} <= sealed
    assert all(row.status == intake_journal.CONSUMED for row in rows), (
        "a published event row is still pending: the next boot would adopt it as owed work"
    )
    assert all(ref.record_id in row.note for row in rows)
