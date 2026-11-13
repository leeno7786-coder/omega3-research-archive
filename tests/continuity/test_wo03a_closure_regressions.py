"""WO-03A closure probes. All journal writes target synthetic temporary files."""
from __future__ import annotations

import json
import os

import pytest

from memory import episode_sram, intake_journal
from memory.episode_events import read_episode_events, record_event
from schema.continuity_json import EpisodeCaptureUnavailable

EPISODE = "closure-source"


@pytest.fixture
def journal(tmp_path, monkeypatch):
    target = tmp_path / "synthetic-intake"
    target.mkdir()
    monkeypatch.setattr(intake_journal, "journal_root", lambda: target)
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    episode_sram.open_bucket(EPISODE, pipeline_id=0)
    try:
        yield target
    finally:
        episode_sram.reset()
        intake_journal.reset_cache_for_tests()


def capture():
    return record_event(
        EPISODE, operation_id="op-one", phase="auto", event_kind="observation",
        payload={"value": "before00"},
    )


def rewrite(change):
    path = intake_journal.journal_path()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    change(rows[0])
    path.write_text(json.dumps(rows[0], sort_keys=True) + "\n", encoding="utf-8")
    intake_journal.reset_cache_for_tests()


@pytest.mark.parametrize("field,value", [
    ("origin_episode_id", "wrong-source"),
    ("kind", "recall"),
])
def test_recognizable_event_cannot_disappear_from_original_source(journal, field, value):
    original = capture()
    rewrite(lambda row: row.__setitem__(field, value))
    # Inner episode identity and sealed digest remain those of the requested episode.
    row = intake_journal.records()[0]
    assert row.payload["event"]["episode_id"] == EPISODE
    assert row.payload["sealed_sha256"] == original.sealed_sha256()
    with pytest.raises(EpisodeCaptureUnavailable):
        read_episode_events(EPISODE)


@pytest.mark.parametrize("route", ["read", "retry"])
def test_record_locator_must_agree_with_event_identity(journal, route):
    original = capture()
    rewrite(lambda row: row.__setitem__("record_id", "ijr_wrong_locator"))
    with pytest.raises(EpisodeCaptureUnavailable):
        if route == "read":
            read_episode_events(EPISODE)
        else:
            capture()
    assert len(intake_journal.records()) == 1
    assert intake_journal.records()[0].event_id == original.event_id


def test_confirmation_cannot_acknowledge_a_different_unverified_row(journal):
    original = capture()
    # Reopen BEFORE damage: preserve a cached validated row and an empty new SRAM bucket.
    episode_sram.reset()
    episode_sram.open_bucket(EPISODE, pipeline_id=0)
    assert episode_sram.pending(EPISODE) == []
    record_id = intake_journal._record_id(original.event_id)
    assert intake_journal.record_by_id(record_id).payload["event"]["payload"]["value"] == "before00"
    path = intake_journal.journal_path()
    stamp = path.stat()
    raw = path.read_bytes()
    assert raw.count(b"before00") == 1
    changed = raw.replace(b"before00", b"after000")
    assert len(changed) == len(raw)
    path.write_bytes(changed)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns
    # The ordinary lookup still returns the cached version; confirm_durable force-reads disk.
    assert intake_journal.record_by_id(record_id).payload["event"]["payload"]["value"] == "before00"
    with pytest.raises(EpisodeCaptureUnavailable):
        capture()
    assert episode_sram.pending(EPISODE) == []


def test_initial_journal_lookup_failure_is_named_at_capture_boundary(journal):
    path = intake_journal.journal_path()
    path.write_text('{"schema_version":', encoding="utf-8")
    intake_journal.reset_cache_for_tests()
    with pytest.raises(EpisodeCaptureUnavailable) as caught:
        capture()
    assert isinstance(caught.value.__cause__, intake_journal.IntakeJournalError)
    assert episode_sram.pending(EPISODE) == []


def test_unchanged_retry_remains_one_occurrence(journal):
    first = capture()
    again = capture()
    assert first.to_dict() == again.to_dict()
    assert len(read_episode_events(EPISODE)) == 1


def test_recovery_owner_may_differ_from_preserved_origin(journal):
    first = capture()
    intake_journal.reown_record(
        intake_journal._record_id(first.event_id), episode_id="recovery-owner",
        pipeline_id=2, reason="synthetic recovery",
    )
    assert read_episode_events(EPISODE)[0].sealed_sha256() == first.sealed_sha256()
    assert read_episode_events("recovery-owner") == ()
