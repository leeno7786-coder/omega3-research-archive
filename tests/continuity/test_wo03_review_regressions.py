"""WO-03 independent boundary checks; all journal mutation is synthetic and isolated.

Install under tests/continuity for the normal isolated suite. The fixture also
isolates the journal when this file is executed alone from a review directory.
No production record, model, service, or canonical-memory writer is used.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from memory import episode_sram, intake_journal
from memory.episode_events import capture_context, read_episode_events, record_event
from schema.continuity_json import (
    EpisodeCaptureUnavailable,
    EpisodeOwnershipError,
    EventIdentityConflict,
)

OWNER_ERRORS = (EpisodeOwnershipError, RuntimeError)
CAPTURE_ERRORS = (EpisodeCaptureUnavailable, intake_journal.IntakeJournalError)


@pytest.fixture
def isolated_journal(tmp_path, monkeypatch):
    """Never reach the real journal, including during bucket-open adoption."""
    target = tmp_path / "wo03-review" / "intake"
    target.mkdir(parents=True)
    monkeypatch.setattr(intake_journal, "journal_root", lambda: target)
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    try:
        yield target
    finally:
        episode_sram.reset()
        intake_journal.reset_cache_for_tests()


def _capture(episode_id="review-A", *, operation_id="op-1", payload=None):
    return record_event(
        episode_id,
        operation_id=operation_id,
        phase="auto",
        event_kind="review_observation",
        payload={"value": "original"} if payload is None else payload,
    )


def test_bound_worker_cannot_write_to_another_open_owner(isolated_journal):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    episode_sram.open_bucket("review-B", pipeline_id=1)
    with episode_sram.bind_episode("review-A", pipeline_id=0):
        with capture_context(thread_id="thread-A"):
            with pytest.raises(OWNER_ERRORS):
                _capture("review-B")
    assert intake_journal.records() == ()
    assert episode_sram.pending("review-B") == []


def test_unbound_worker_cannot_authorize_itself_by_naming_open_episode(isolated_journal):
    episode_sram.open_bucket("review-A", pipeline_id=0)

    def attempt():
        assert episode_sram.bound_episode_id() is None
        return _capture()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(attempt)
        with pytest.raises(OWNER_ERRORS):
            future.result(timeout=15)
    assert intake_journal.records() == ()
    assert episode_sram.pending("review-A") == []


@pytest.mark.parametrize("first,second", [(1, True), (False, 0)], ids=["int-to-bool", "bool-to-int"])
def test_replay_preserves_json_types_not_python_equality(isolated_journal, first, second):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    original = _capture(payload={"nested": {"value": first}})
    with pytest.raises(EventIdentityConflict):
        _capture(payload={"nested": {"value": second}})
    saved = read_episode_events("review-A")
    assert len(saved) == 1
    assert saved[0].sealed_sha256() == original.sealed_sha256()
    assert len(episode_sram.pending("review-A")) == 1


def _damage_synthetic_row(change):
    """Change only this test's journal bytes, retaining old integrity metadata."""
    path = intake_journal.journal_path()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    change(rows[0])
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
    intake_journal.reset_cache_for_tests()


@pytest.mark.parametrize("field", ["payload", "event_seq"])
def test_journal_rehydration_verifies_sealed_event_digest(isolated_journal, field):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    _capture()

    def change(row):
        if field == "payload":
            row["payload"]["event"]["payload"]["value"] = "not the recorded evidence"
        else:
            row["payload"]["event"]["event_seq"] = 9
        # Intentionally retain payload.sealed_sha256 and fingerprint from capture.

    _damage_synthetic_row(change)
    with pytest.raises(CAPTURE_ERRORS):
        read_episode_events("review-A")


def test_missing_event_body_is_unavailable_not_successfully_empty(isolated_journal):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    _capture()
    _damage_synthetic_row(lambda row: row["payload"].pop("event"))
    with pytest.raises(CAPTURE_ERRORS):
        read_episode_events("review-A")


def test_retry_cannot_acknowledge_row_while_fsync_is_still_failing(isolated_journal, monkeypatch):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    calls = []

    def failed_fsync(descriptor):
        calls.append(descriptor)
        raise OSError("review: actual fsync unavailable")

    # Do not mock _append_locked: execute its real write -> flush -> fsync path.
    with monkeypatch.context() as fault:
        fault.setattr(intake_journal.os, "fsync", failed_fsync)
        with pytest.raises(CAPTURE_ERRORS):
            _capture()
        assert calls, "the real append reached fsync"
        assert episode_sram.pending("review-A") == []
        with pytest.raises(CAPTURE_ERRORS):
            _capture()
        assert episode_sram.pending("review-A") == []


def test_capture_failure_is_named_at_capture_boundary_and_keeps_cause(isolated_journal, monkeypatch):
    episode_sram.open_bucket("review-A", pipeline_id=0)

    def failed_fsync(_descriptor):
        raise OSError("review: actual fsync unavailable")

    with monkeypatch.context() as fault:
        fault.setattr(intake_journal.os, "fsync", failed_fsync)
        with pytest.raises(EpisodeCaptureUnavailable) as caught:
            _capture()
    causes = []
    cursor = caught.value.__cause__
    while cursor is not None:
        causes.append(cursor)
        cursor = cursor.__cause__
    assert any(isinstance(cause, OSError) for cause in causes)
    assert episode_sram.pending("review-A") == []


# Positive controls: fixes must retain these accepted behaviors.

def test_valid_bound_capture_and_identical_retry_keep_original_occurrence(isolated_journal):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    with episode_sram.bind_episode("review-A", pipeline_id=0):
        first = _capture()
        second = _capture()
    assert first.to_dict() == second.to_dict()
    assert len(episode_sram.pending("review-A")) == 1


def test_identical_payloads_from_distinct_operations_remain_distinct(isolated_journal):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    first = _capture(operation_id="op-1")
    second = _capture(operation_id="op-2")
    assert first.event_id != second.event_id
    assert (first.event_seq, second.event_seq) == (1, 2)


def test_empty_valid_episode_returns_empty_tuple(isolated_journal):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    assert read_episode_events("review-A") == ()


def test_valid_reownership_preserves_source_event_and_digest(isolated_journal):
    episode_sram.open_bucket("review-A", pipeline_id=0)
    original = _capture()
    intake_journal.reown_record(
        intake_journal._record_id(original.event_id),
        episode_id="review-recovery",
        pipeline_id=2,
        reason="synthetic recovery after original owner exit",
    )
    assert read_episode_events("review-A")[0].sealed_sha256() == original.sealed_sha256()
    assert read_episode_events("review-recovery") == ()
