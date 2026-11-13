"""WO-03 — crash, genuine process restart, and recovery attribution.

The restart cases spawn a REAL second interpreter process against the same isolated journal
directory and let it exit, because clearing an in-process cache proves nothing about durability:
it is the same process reading the same file it just wrote. Ordering is controlled with barriers
and explicit sequencing, never with sleeps.

The invariant under test: **recovery moves ownership, never history.** A recovering bucket owner
is additional metadata. The event's own source episode, thread, sequence, timestamp and
attribution are sealed at capture and survive every reownership.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT
from memory import episode_sram, intake_journal
from memory.episode_events import EVENT_JOURNAL_KIND, read_episode_events, record_event
from schema.continuity_json import EpisodeCaptureUnavailable, EventIdentityConflict
from schema.episode_event import EpisodeEvent

INTERPRETER = Path(OMEGA_ROOT) / ".venv-brain" / "Scripts" / "python.exe"


@pytest.fixture
def journal_dir(tmp_path, monkeypatch) -> Path:
    """The isolated journal root shared between this process and the spawned one."""
    root = tmp_path / "wo03" / "intake"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(intake_journal, "journal_root", lambda: root)
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    yield root
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()


def _run_in_fresh_process(journal_root: Path, body: str) -> dict:
    """Execute *body* in a genuinely separate interpreter and return its printed JSON."""
    script = textwrap.dedent(
        f"""
        import json, sys
        sys.path.insert(0, {str(OMEGA_ROOT)!r})
        from pathlib import Path
        from memory import episode_sram, intake_journal
        intake_journal.journal_root = lambda: Path({str(journal_root)!r})
        intake_journal.reset_cache_for_tests()
        from memory.episode_events import read_episode_events, record_event
        out = {{}}
        {textwrap.indent(textwrap.dedent(body), " " * 8).lstrip()}
        print("@@JSON@@" + json.dumps(out))
        """
    )
    completed = subprocess.run(
        [str(INTERPRETER), "-c", script],
        capture_output=True,
        text=True,
        cwd=str(OMEGA_ROOT),
        timeout=180,
    )
    assert completed.returncode == 0, f"child failed:\n{completed.stdout}\n{completed.stderr}"
    marker = [line for line in completed.stdout.splitlines() if line.startswith("@@JSON@@")]
    assert marker, f"child printed no result:\n{completed.stdout}\n{completed.stderr}"
    return json.loads(marker[-1][len("@@JSON@@") :])


# ── genuine process restart ───────────────────────────────────────────────────


@pytest.mark.skipif(not INTERPRETER.is_file(), reason="SSD interpreter not present")
def test_an_event_captured_in_another_process_survives_its_exit(journal_dir):
    """Durability proved across a real process boundary, not a cache reset."""
    child = _run_in_fresh_process(
        journal_dir,
        """
        episode_sram.open_bucket("ep-rec-0001", pipeline_id=0)
        event = record_event("ep-rec-0001", operation_id="op-1", phase="auto",
                             event_kind="tool_call", payload={"v": 1})
        out["event_id"] = event.event_id
        out["event_seq"] = event.event_seq
        out["observed_at"] = event.observed_at
        out["thread_id"] = event.thread_id
        out["boot_id"] = event.boot_id
        """,
    )
    intake_journal.reset_cache_for_tests()
    events = read_episode_events("ep-rec-0001")
    assert len(events) == 1
    assert events[0].event_id == child["event_id"]
    assert events[0].event_seq == child["event_seq"]
    assert events[0].observed_at == child["observed_at"]
    assert events[0].thread_id == child["thread_id"]


@pytest.mark.skipif(not INTERPRETER.is_file(), reason="SSD interpreter not present")
def test_a_retry_after_restart_returns_the_original_occurrence(journal_dir):
    """Acceptance case: one event before and after process restart."""
    first = _run_in_fresh_process(
        journal_dir,
        """
        episode_sram.open_bucket("ep-rec-0002", pipeline_id=0)
        event = record_event("ep-rec-0002", operation_id="op-fixed", phase="auto",
                             event_kind="tool_call", payload={"v": 1})
        out["event_id"] = event.event_id
        out["event_seq"] = event.event_seq
        out["observed_at"] = event.observed_at
        out["boot_id"] = event.boot_id
        """,
    )
    second = _run_in_fresh_process(
        journal_dir,
        """
        import memory.episode_events as ee
        episode_sram.open_bucket("ep-rec-0002", pipeline_id=0)
        event = record_event("ep-rec-0002", operation_id="op-fixed", phase="auto",
                             event_kind="tool_call", payload={"v": 1})
        out["event_id"] = event.event_id
        out["event_seq"] = event.event_seq
        out["observed_at"] = event.observed_at
        out["boot_id"] = event.boot_id
        out["live_boot_id"] = ee._BOOT_ID
        out["count"] = len(read_episode_events("ep-rec-0002"))
        """,
    )
    assert second["event_id"] == first["event_id"]
    assert second["event_seq"] == first["event_seq"]
    assert second["observed_at"] == first["observed_at"], "a restart must not restamp"
    assert second["boot_id"] == first["boot_id"], "the ORIGINAL boot is preserved, not the retry's"
    assert second["count"] == 1

    # Proof this really was a second PROCESS, and that the retry preserved the original boot
    # rather than stamping its own: the retrying process has a different live boot id, and the
    # returned event still carries the first one.
    assert second["live_boot_id"] != second["boot_id"]
    assert second["live_boot_id"] != first["boot_id"]

    intake_journal.reset_cache_for_tests()
    assert len(read_episode_events("ep-rec-0002")) == 1


@pytest.mark.skipif(not INTERPRETER.is_file(), reason="SSD interpreter not present")
def test_a_different_boot_still_numbers_the_next_event_from_durable_state(journal_dir):
    _run_in_fresh_process(
        journal_dir,
        """
        episode_sram.open_bucket("ep-rec-0003", pipeline_id=0)
        record_event("ep-rec-0003", operation_id="op-1", phase="auto", event_kind="k", payload={})
        record_event("ep-rec-0003", operation_id="op-2", phase="auto", event_kind="k", payload={})
        """,
    )
    later = _run_in_fresh_process(
        journal_dir,
        """
        episode_sram.open_bucket("ep-rec-0003", pipeline_id=0)
        event = record_event("ep-rec-0003", operation_id="op-3", phase="auto", event_kind="k", payload={})
        out["event_seq"] = event.event_seq
        """,
    )
    assert later["event_seq"] == 3, "the sequence continues from the journal, not from memory"


@pytest.mark.skipif(not INTERPRETER.is_file(), reason="SSD interpreter not present")
def test_a_conflicting_retry_after_restart_is_still_refused(journal_dir):
    _run_in_fresh_process(
        journal_dir,
        """
        episode_sram.open_bucket("ep-rec-0004", pipeline_id=0)
        record_event("ep-rec-0004", operation_id="op-fixed", phase="auto",
                     event_kind="k", payload={"v": "first"})
        """,
    )
    result = _run_in_fresh_process(
        journal_dir,
        """
        from schema.continuity_json import EventIdentityConflict
        episode_sram.open_bucket("ep-rec-0004", pipeline_id=0)
        try:
            record_event("ep-rec-0004", operation_id="op-fixed", phase="auto",
                         event_kind="k", payload={"v": "second"})
            out["refused"] = False
        except EventIdentityConflict as exc:
            out["refused"] = True
            out["message"] = str(exc)
        out["surviving"] = [e.payload["v"] for e in read_episode_events("ep-rec-0004")]
        """,
    )
    assert result["refused"] is True
    assert "payload" in result["message"]
    assert result["surviving"] == ["first"], "the original survives a cross-restart conflict"


# ── the crash boundary: journalled, never acknowledged ────────────────────────


def test_a_crash_between_journal_append_and_bucket_acknowledgement_keeps_the_event(journal_dir):
    """Acceptance case: the event is preserved and rehydrated exactly once."""
    episode_sram.open_bucket("ep-rec-0005", pipeline_id=0)
    import memory.episode_events as episode_events

    original_hold = episode_events._hold_in_owner_bucket

    def _crash(*_a, **_k):
        raise KeyboardInterrupt("simulated crash after the journal append")

    episode_events._hold_in_owner_bucket = _crash
    try:
        with pytest.raises(KeyboardInterrupt):
            record_event("ep-rec-0005", operation_id="op-crash", phase="auto",
                         event_kind="k", payload={"v": 1})
    finally:
        episode_events._hold_in_owner_bucket = original_hold

    # The journal kept it; the bucket never saw it.
    assert episode_sram.pending("ep-rec-0005") == []
    events = read_episode_events("ep-rec-0005")
    assert len(events) == 1, "a journalled event is durable even though capture never finished"

    # Replaying the same operation rehydrates it ONCE, with its original identity.
    replayed = record_event("ep-rec-0005", operation_id="op-crash", phase="auto",
                            event_kind="k", payload={"v": 1})
    assert replayed.event_id == events[0].event_id
    assert replayed.event_seq == events[0].event_seq
    assert replayed.observed_at == events[0].observed_at
    assert len(read_episode_events("ep-rec-0005")) == 1
    assert len(episode_sram.pending("ep-rec-0005")) == 1, "rehydrated into the bucket once"


def test_a_rehydrated_event_is_not_held_twice(journal_dir):
    episode_sram.open_bucket("ep-rec-0006", pipeline_id=0)
    record_event("ep-rec-0006", operation_id="op-1", phase="auto", event_kind="k", payload={})
    record_event("ep-rec-0006", operation_id="op-1", phase="auto", event_kind="k", payload={})
    record_event("ep-rec-0006", operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert len(episode_sram.pending("ep-rec-0006")) == 1


# ── recovery attribution: ownership moves, history does not ───────────────────


def test_reownership_changes_only_the_recovery_owner(journal_dir):
    """Acceptance case: original source episode, thread and sequence remain intact."""
    episode_sram.open_bucket("ep-rec-0007", pipeline_id=0)
    original = record_event("ep-rec-0007", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"v": 1})
    record_id = intake_journal._record_id(original.event_id)
    before = intake_journal.record_by_id(record_id)
    assert before.origin_episode_id == "ep-rec-0007" and before.origin_pipeline_id == 0

    reowned = intake_journal.reown_record(
        record_id, episode_id="ep-recovery-owner", pipeline_id=2,
        reason="previous SRAM owner disappeared",
    )
    assert reowned.owner_episode_id == "ep-recovery-owner"
    assert reowned.owner_pipeline_id == 2
    # History is untouched on BOTH sides of the boundary.
    assert reowned.origin_episode_id == "ep-rec-0007"
    assert reowned.origin_pipeline_id == 0

    rebuilt = EpisodeEvent.from_dict(reowned.payload["event"])
    assert rebuilt.episode_id == "ep-rec-0007", "the event's own source episode is sealed"
    assert rebuilt.thread_id == original.thread_id
    assert rebuilt.event_seq == original.event_seq
    assert rebuilt.observed_at == original.observed_at
    assert rebuilt.sealed_sha256() == original.sealed_sha256()
    assert rebuilt.pipeline_id == 0, "the capturing pipeline, not the recovering one"


def test_the_original_episode_still_reads_its_own_event_after_reownership(journal_dir):
    episode_sram.open_bucket("ep-rec-0008", pipeline_id=0)
    original = record_event("ep-rec-0008", operation_id="op-1", phase="auto", event_kind="k", payload={})
    intake_journal.reown_record(
        intake_journal._record_id(original.event_id),
        episode_id="ep-recovery-owner", pipeline_id=3, reason="recovery",
    )
    events = read_episode_events("ep-rec-0008")
    assert len(events) == 1 and events[0].event_id == original.event_id
    assert read_episode_events("ep-recovery-owner") == (), (
        "the recovery owner did not author the event and must not appear to"
    )


def test_boot_release_and_adoption_preserve_the_sealed_event(journal_dir):
    """The existing release/adopt path must rebuild an event item, not refuse the kind."""
    episode_sram.open_bucket("ep-rec-0009", pipeline_id=1)
    original = record_event("ep-rec-0009", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"v": 1})
    episode_sram.reset()

    report = intake_journal.reconcile_at_boot(
        episode_id="ep-rec-0009", pipeline_id=1, cascade_replay=False
    )
    assert report["pending"] == 1 and report["released"] == 1

    episode_sram.open_bucket("ep-rec-0010", pipeline_id=1)
    adopted = episode_sram.pending("ep-rec-0010")
    assert [item.kind for item in adopted] == ["event"]
    assert adopted[0].event_id == original.event_id

    rebuilt = read_episode_events("ep-rec-0009")
    assert len(rebuilt) == 1
    assert rebuilt[0].sealed_sha256() == original.sealed_sha256()


def test_the_journal_row_records_its_kind_so_recovery_can_classify_it(journal_dir):
    episode_sram.open_bucket("ep-rec-0011", pipeline_id=0)
    event = record_event("ep-rec-0011", operation_id="op-1", phase="auto", event_kind="k", payload={})
    record = intake_journal.record_by_id(intake_journal._record_id(event.event_id))
    assert record.kind == EVENT_JOURNAL_KIND
    assert record.status == intake_journal.PENDING, "pending until DeepSleep publication (WO-07)"


# ── a required capture failure blocks dependent work ─────────────────────────


def test_a_required_capture_failure_does_not_return_an_empty_success(journal_dir, monkeypatch):
    episode_sram.open_bucket("ep-rec-0012", pipeline_id=0)

    def _explode(*_a, **_k):
        raise OSError("journal volume gone")

    # A scoped context, never `monkeypatch.undo()`: the autouse conftest redirect shares this
    # same monkeypatch, so a blanket undo would point the assertions below at live memory state
    # — where they would pass for the wrong reason (WO-03A).
    with monkeypatch.context() as patched:
        patched.setattr(intake_journal, "_append_locked", _explode)
        with pytest.raises(Exception) as excinfo:
            record_event("ep-rec-0012", operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert excinfo.value is not None
    assert not isinstance(excinfo.value, (tuple, list, dict))
    assert read_episode_events("ep-rec-0012") == ()


# ── WO-03A R3: a journal-backed event must verify its own integrity claim ─────


def _cause_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    cursor = error.__cause__
    while cursor is not None:
        chain.append(cursor)
        cursor = cursor.__cause__
    return chain


def _journal_rows() -> list[dict]:
    path = intake_journal.journal_path()
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _rewrite_rows(rows: list[dict]) -> None:
    """Replace this test's ISOLATED journal bytes. Never touches real memory."""
    intake_journal.journal_path().write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    intake_journal.reset_cache_for_tests()


def _damage_one_row(change) -> None:
    rows = _journal_rows()
    assert len(rows) == 1, f"this helper damages a single synthetic row, found {len(rows)}"
    change(rows[0])
    _rewrite_rows(rows)


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", {"v": "swapped"}),
        ("event_seq", 7),
        ("phase", "omega"),
        ("boot_id", "boot-someone-else"),
    ],
    ids=["payload", "event_seq", "phase", "boot_id"],
)
def test_a_stale_sealed_digest_fails_the_read_rather_than_decoding(journal_dir, field, value):
    """Structural schema validity is not byte integrity: the stored digest is recomputed."""
    episode_sram.open_bucket("ep-int-0001", pipeline_id=0)
    record_event("ep-int-0001", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    _damage_one_row(lambda row: row["payload"]["event"].__setitem__(field, value))
    with pytest.raises(EpisodeCaptureUnavailable, match="integrity claim disagree"):
        read_episode_events("ep-int-0001")


def test_a_retry_over_a_tampered_row_refuses_instead_of_replaying_it(journal_dir):
    """The retry path uses the same decoder, so it cannot acknowledge unverifiable evidence."""
    episode_sram.open_bucket("ep-int-0002", pipeline_id=0)
    record_event("ep-int-0002", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    episode_sram.reset()
    _damage_one_row(lambda row: row["payload"]["event"]["payload"].__setitem__("v", 99))
    episode_sram.open_bucket("ep-int-0002", pipeline_id=0)
    with pytest.raises(EpisodeCaptureUnavailable):
        record_event("ep-int-0002", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    assert episode_sram.pending("ep-int-0002") == []


def test_boot_adoption_refuses_a_tampered_event_row(journal_dir):
    """Adoption rebuilds through the same decoder; a damaged row is not silently adopted."""
    episode_sram.open_bucket("ep-int-0003", pipeline_id=1)
    record_event("ep-int-0003", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    episode_sram.reset()
    intake_journal.reconcile_at_boot(episode_id="ep-int-0003", pipeline_id=1, cascade_replay=False)
    rows = _journal_rows()
    rows[-1]["payload"]["event"]["event_seq"] = 5          # retain the recorded digest
    _rewrite_rows(rows)
    with pytest.raises(EpisodeCaptureUnavailable):
        episode_sram.open_bucket("ep-int-0004", pipeline_id=1)


@pytest.mark.parametrize(
    "change,expected,target",
    [
        (lambda row: row["payload"].pop("event"), "carries no event body", "ep-int-0005"),
        (lambda row: row["payload"].pop("sealed_sha256"), "carries no sealed digest", "ep-int-0005"),
        (
            lambda row: row.__setitem__("event_id", "evt_someone_elses_identity"),
            "identity is inconsistent",
            "ep-int-0005",
        ),
        (
            lambda row: row.__setitem__("origin_episode_id", "ep-somewhere-else"),
            "source linkage is broken",
            "ep-somewhere-else",
        ),
        (
            lambda row: row.__setitem__("origin_pipeline_id", 3),
            "source pipeline",
            "ep-int-0005",
        ),
    ],
    ids=["no-body", "no-digest", "outer-identity", "source-episode", "source-pipeline"],
)
def test_a_recognizable_row_missing_required_content_is_unavailable_not_empty(
    journal_dir, change, expected, target
):
    """A row that cannot be verified fails the read; it never becomes "the episode had none"."""
    episode_sram.open_bucket("ep-int-0005", pipeline_id=0)
    record_event("ep-int-0005", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    _damage_one_row(change)
    with pytest.raises(EpisodeCaptureUnavailable, match=expected):
        read_episode_events(target)


def test_a_damaged_row_cannot_silently_renumber_the_next_event(journal_dir):
    """Sequence allocation reads the same validated history; it never allocates over a bad row."""
    episode_sram.open_bucket("ep-int-0006", pipeline_id=0)
    record_event("ep-int-0006", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    _damage_one_row(lambda row: row["payload"]["event"].__setitem__("event_seq", 4))
    with pytest.raises(EpisodeCaptureUnavailable):
        record_event("ep-int-0006", operation_id="op-2", phase="auto", event_kind="k", payload={})


def test_two_rows_at_one_sequence_are_refused_not_tie_sorted(journal_dir):
    """Duplicate sequence is an inconsistent history, not an ordering question.

    The second row is re-sealed with a digest that MATCHES its altered sequence, so this is
    caught by the history check rather than by the integrity check it would otherwise mask.
    """
    episode_sram.open_bucket("ep-int-0007", pipeline_id=0)
    first = record_event("ep-int-0007", operation_id="op-1", phase="auto", event_kind="k", payload={})
    second = record_event("ep-int-0007", operation_id="op-2", phase="auto", event_kind="k", payload={})
    assert (first.event_seq, second.event_seq) == (1, 2)

    rows = _journal_rows()
    for row in rows:
        body = row["payload"].get("event") or {}
        if body.get("event_id") == second.event_id:
            body = dict(body)
            body["event_seq"] = 1
            resealed = EpisodeEvent.from_dict(body)
            row["payload"] = {"event": body, "sealed_sha256": resealed.sealed_sha256()}
    _rewrite_rows(rows)
    with pytest.raises(EpisodeCaptureUnavailable, match="same sequence"):
        read_episode_events("ep-int-0007")


def test_valid_reownership_still_passes_the_integrity_check(journal_dir):
    """A recovery owner is ALLOWED to differ from the immutable source owner."""
    episode_sram.open_bucket("ep-int-0008", pipeline_id=0)
    original = record_event("ep-int-0008", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"v": 1})
    intake_journal.reown_record(
        intake_journal._record_id(original.event_id),
        episode_id="ep-int-recovery", pipeline_id=2, reason="original owner exited",
    )
    events = read_episode_events("ep-int-0008")
    assert len(events) == 1
    assert events[0].sealed_sha256() == original.sealed_sha256()
    assert events[0].pipeline_id == 0, "the capturing pipeline, not the recovering one"
    assert read_episode_events("ep-int-recovery") == ()


# ── WO-03A R4: row visibility is not proven durable acceptance ───────────────


def test_a_failure_before_any_bytes_are_written_is_named_at_the_capture_boundary(
    journal_dir, monkeypatch
):
    """The control the order distinguishes from the fsync case: nothing was written at all."""
    episode_sram.open_bucket("ep-dur-0001", pipeline_id=0)

    def _explode(*_a, **_k):
        raise OSError("volume gone before any write")

    with monkeypatch.context() as patched:
        patched.setattr(intake_journal, "_append_locked", _explode)
        with pytest.raises(EpisodeCaptureUnavailable) as caught:
            record_event("ep-dur-0001", operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert any(isinstance(cause, OSError) for cause in _cause_chain(caught.value))
    assert read_episode_events("ep-dur-0001") == (), "no row exists to be replayed"
    assert episode_sram.pending("ep-dur-0001") == []


def test_written_and_flushed_but_unsynced_bytes_are_never_acknowledged(journal_dir, monkeypatch):
    """The real append reaches fsync, so the interval under test is write-done/sync-failed."""
    episode_sram.open_bucket("ep-dur-0002", pipeline_id=0)
    reached: list[int] = []

    def _failed_fsync(descriptor):
        reached.append(descriptor)
        raise OSError("fsync unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(intake_journal.os, "fsync", _failed_fsync)
        with pytest.raises(EpisodeCaptureUnavailable) as first:
            record_event("ep-dur-0002", operation_id="op-1", phase="auto", event_kind="k",
                         payload={"v": 1})
        assert reached, "the real write/flush/fsync path ran"
        assert any(isinstance(cause, OSError) for cause in _cause_chain(first.value))
        assert episode_sram.pending("ep-dur-0002") == []

        # The bytes ARE readable. That is exactly why readability cannot be the receipt.
        intake_journal.reset_cache_for_tests()
        assert len(read_episode_events("ep-dur-0002")) == 1

        with pytest.raises(EpisodeCaptureUnavailable, match="not proven durably accepted") as second:
            record_event("ep-dur-0002", operation_id="op-1", phase="auto", event_kind="k",
                         payload={"v": 1})
        assert any(isinstance(cause, OSError) for cause in _cause_chain(second.value))
        assert episode_sram.pending("ep-dur-0002") == [], "a second retry acknowledges nothing"

    original = read_episode_events("ep-dur-0002")[0]
    confirmed = record_event("ep-dur-0002", operation_id="op-1", phase="auto",
                             event_kind="k", payload={"v": 1})
    held = [item for item in episode_sram.pending("ep-dur-0002") if item.kind == "event"]
    assert len(held) == 1, "restored durability acknowledges the original exactly once"
    assert held[0].event_id == confirmed.event_id
    assert confirmed.sealed_sha256() == original.sealed_sha256(), "original bytes preserved"
    assert confirmed.event_seq == original.event_seq == 1
    assert confirmed.observed_at == original.observed_at, "no new occurrence was invented"
    assert len(read_episode_events("ep-dur-0002")) == 1, "the failed attempt was not duplicated"
    record = intake_journal.record_by_id(intake_journal._record_id(confirmed.event_id))
    assert record.status == intake_journal.PENDING, "no commit receipt was manufactured"
    assert record.memory_id == "" and record.canonical_path == ""


def test_an_identity_conflict_is_decided_before_durability_and_stays_distinct(
    journal_dir, monkeypatch
):
    """A conflicting retry is refused as a conflict, not reported as a durability failure."""
    episode_sram.open_bucket("ep-dur-0003", pipeline_id=0)
    record_event("ep-dur-0003", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})

    def _failed_fsync(_descriptor):
        raise OSError("fsync unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(intake_journal.os, "fsync", _failed_fsync)
        with pytest.raises(EventIdentityConflict):
            record_event("ep-dur-0003", operation_id="op-1", phase="auto", event_kind="k",
                         payload={"v": 2})
    assert len(read_episode_events("ep-dur-0003")) == 1


@pytest.mark.skipif(not INTERPRETER.is_file(), reason="SSD interpreter not present")
def test_a_child_that_exits_between_persistence_and_acknowledgement_is_recovered(journal_dir):
    """A REAL process death inside the crash interval, then recovery by the surviving owner."""
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(OMEGA_ROOT)!r})
        from pathlib import Path
        from memory import episode_sram, intake_journal
        intake_journal.journal_root = lambda: Path({str(journal_dir)!r})
        intake_journal.reset_cache_for_tests()
        import memory.episode_events as episode_events

        def _die(*a, **k):
            sys.stdout.flush()
            os._exit(9)              # no unwinding, no atexit, no acknowledgement

        episode_events._hold_in_owner_bucket = _die
        episode_sram.open_bucket("ep-dur-0004", pipeline_id=0)
        episode_events.record_event("ep-dur-0004", operation_id="op-crash", phase="auto",
                                    event_kind="k", payload={{"v": 1}})
        print("@@UNREACHABLE@@")
        """
    )
    completed = subprocess.run(
        [str(INTERPRETER), "-c", script],
        capture_output=True, text=True, cwd=str(OMEGA_ROOT), timeout=180,
    )
    assert completed.returncode == 9, (
        f"the child did not die in the interval: rc={completed.returncode}\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert "@@UNREACHABLE@@" not in completed.stdout

    intake_journal.reset_cache_for_tests()
    durable = read_episode_events("ep-dur-0004")
    assert len(durable) == 1, "the persisted event survived the child's death"

    episode_sram.open_bucket("ep-dur-0004", pipeline_id=0)
    assert episode_sram.pending("ep-dur-0004") == [], "the child acknowledged nothing"
    replayed = record_event("ep-dur-0004", operation_id="op-crash", phase="auto",
                            event_kind="k", payload={"v": 1})
    assert replayed.event_id == durable[0].event_id
    assert replayed.event_seq == durable[0].event_seq
    assert replayed.observed_at == durable[0].observed_at
    assert replayed.boot_id == durable[0].boot_id, "the dead child's boot is preserved"
    held = [item for item in episode_sram.pending("ep-dur-0004") if item.kind == "event"]
    assert [item.event_id for item in held] == [durable[0].event_id]
    assert len(read_episode_events("ep-dur-0004")) == 1


# ── WO-03A closure F1: damaged outer routing cannot hide a recognizable event ──


def test_a_damaged_outer_origin_still_reaches_the_decoder_from_the_original_episode(journal_dir):
    """The ORIGINAL episode's read must reach the decoder, not shorten itself silently.

    The distinction the closure review drew: querying the CHANGED value proves the decoder
    rejects a row it receives; only querying the original identity proves the read gets there.
    """
    episode_sram.open_bucket("ep-clo-0001", pipeline_id=0)
    original = record_event("ep-clo-0001", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"v": 1})
    _damage_one_row(lambda row: row.__setitem__("origin_episode_id", "ep-elsewhere"))
    row = intake_journal.records()[0]
    assert row.payload["event"]["episode_id"] == "ep-clo-0001", "the inner identity is intact"
    assert row.payload["sealed_sha256"] == original.sealed_sha256()

    with pytest.raises(EpisodeCaptureUnavailable, match="source linkage is broken"):
        read_episode_events("ep-clo-0001")


def test_a_damaged_outer_kind_still_reaches_the_decoder(journal_dir):
    """The versioned event envelope is the recognizer, so the outer kind cannot exclude a row."""
    episode_sram.open_bucket("ep-clo-0002", pipeline_id=0)
    original = record_event("ep-clo-0002", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"v": 1})
    _damage_one_row(lambda row: row.__setitem__("kind", "recall"))
    row = intake_journal.records()[0]
    assert row.kind == "recall" and row.payload["sealed_sha256"] == original.sealed_sha256()

    with pytest.raises(EpisodeCaptureUnavailable, match="not 'episode_event'"):
        read_episode_events("ep-clo-0002")


def test_a_damaged_outer_kind_does_not_stop_the_next_sequence_from_refusing(journal_dir):
    """Sequence allocation reads the same history, so it refuses rather than renumbering."""
    episode_sram.open_bucket("ep-clo-0003", pipeline_id=0)
    first = record_event("ep-clo-0003", operation_id="op-1", phase="auto", event_kind="k",
                         payload={"v": 1})
    _damage_one_row(lambda row: row.__setitem__("kind", "recall"))
    with pytest.raises(EpisodeCaptureUnavailable):
        record_event("ep-clo-0003", operation_id="op-2", phase="auto", event_kind="k", payload={})
    assert len(intake_journal.records()) == 1, "no row was appended over an inconsistent history"
    held = [item for item in episode_sram.pending("ep-clo-0003") if item.kind == "event"]
    assert [item.event_id for item in held] == [first.event_id], (
        "the refused second capture acknowledged nothing; only the first event is held"
    )


# ── WO-03A closure F2: the locator binds to the event ─────────────────────────


def test_a_wrong_locator_is_refused_on_the_read_path(journal_dir):
    episode_sram.open_bucket("ep-clo-0004", pipeline_id=0)
    record_event("ep-clo-0004", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    _damage_one_row(lambda row: row.__setitem__("record_id", "ijr_wrong_locator"))
    with pytest.raises(EpisodeCaptureUnavailable, match="locator that disagrees"):
        read_episode_events("ep-clo-0004")


def test_a_wrong_locator_retry_allocates_no_second_occurrence(journal_dir):
    """The defect this closes: the retry missed its key and minted sequence 2 under the same id."""
    episode_sram.open_bucket("ep-clo-0005", pipeline_id=0)
    original = record_event("ep-clo-0005", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"v": 1})
    assert original.event_seq == 1
    _damage_one_row(lambda row: row.__setitem__("record_id", "ijr_wrong_locator"))
    episode_sram.reset()
    episode_sram.open_bucket("ep-clo-0005", pipeline_id=0)

    with pytest.raises(EpisodeCaptureUnavailable, match="locator that disagrees"):
        record_event("ep-clo-0005", operation_id="op-1", phase="auto", event_kind="k",
                     payload={"v": 1})

    rows = intake_journal.records()
    assert len(rows) == 1, "a missing lookup key is not evidence of a new operation"
    assert rows[0].event_id == original.event_id
    assert rows[0].payload["event"]["event_seq"] == 1, "no second sequence was allocated"
    assert episode_sram.pending("ep-clo-0005") == [], "nothing was acknowledged"


def test_a_wrong_locator_is_refused_on_the_adoption_path(journal_dir):
    episode_sram.open_bucket("ep-clo-0006", pipeline_id=1)
    record_event("ep-clo-0006", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1})
    episode_sram.reset()
    intake_journal.reconcile_at_boot(episode_id="ep-clo-0006", pipeline_id=1, cascade_replay=False)
    rows = _journal_rows()
    rows[-1]["record_id"] = "ijr_wrong_locator"
    _rewrite_rows(rows)
    with pytest.raises(EpisodeCaptureUnavailable, match="locator that disagrees"):
        episode_sram.open_bucket("ep-clo-0007", pipeline_id=1)


def test_an_event_id_not_derived_from_its_episode_and_operation_is_refused(journal_dir):
    """Identity is derived from (episode, operation); a re-sealed substitute is still refused."""
    episode_sram.open_bucket("ep-clo-0008", pipeline_id=0)
    original = record_event("ep-clo-0008", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"v": 1})
    rows = _journal_rows()
    body = dict(rows[0]["payload"]["event"])
    body["operation_id"] = "op-someone-elses"          # the id no longer derives from this pair
    resealed = EpisodeEvent.from_dict(body)
    rows[0]["payload"] = {"event": body, "sealed_sha256": resealed.sealed_sha256()}
    _rewrite_rows(rows)
    assert resealed.event_id == original.event_id

    with pytest.raises(EpisodeCaptureUnavailable, match="not produced by this capture"):
        read_episode_events("ep-clo-0008")


# ── WO-03A closure F3: acknowledge the row whose durability was confirmed ──────


def test_confirmation_that_reads_changed_content_acknowledges_nothing(journal_dir):
    """A real cache-versus-disk boundary: size and mtime preserved, body changed, digest kept."""
    episode_sram.open_bucket("ep-clo-0009", pipeline_id=0)
    original = record_event("ep-clo-0009", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"value": "aaaaaaaa"})
    episode_sram.reset()
    episode_sram.open_bucket("ep-clo-0009", pipeline_id=0)
    assert episode_sram.pending("ep-clo-0009") == []

    path = intake_journal.journal_path()
    stamp = path.stat()
    raw = path.read_bytes()
    assert raw.count(b"aaaaaaaa") == 1
    changed = raw.replace(b"aaaaaaaa", b"bbbbbbbb")
    assert len(changed) == len(raw)
    path.write_bytes(changed)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size and path.stat().st_mtime_ns == stamp.st_mtime_ns

    record_id = intake_journal._record_id(original.event_id)
    cached = intake_journal.record_by_id(record_id)
    assert cached.payload["event"]["payload"]["value"] == "aaaaaaaa", "the cache still holds it"

    with pytest.raises(EpisodeCaptureUnavailable):
        record_event("ep-clo-0009", operation_id="op-1", phase="auto", event_kind="k",
                     payload={"value": "aaaaaaaa"})
    assert episode_sram.pending("ep-clo-0009") == [], "nothing was acknowledged"


def test_a_reowned_row_is_refused_by_its_original_owner_under_the_existing_owner_rules(journal_dir):
    """An ordinary ownership transition: the row owes the recovery owner, not the original.

    The original owner's replay is refused at acknowledgement by `hold_event`'s row-owner check —
    the existing rule, unchanged — and the durable row is preserved for the adoption path rather
    than being held by an episode that no longer owes it.
    """
    episode_sram.open_bucket("ep-clo-0010", pipeline_id=0)
    original = record_event("ep-clo-0010", operation_id="op-1", phase="auto",
                            event_kind="k", payload={"v": 1})
    episode_sram.reset()
    episode_sram.open_bucket("ep-clo-0010", pipeline_id=0)
    intake_journal.reown_record(
        intake_journal._record_id(original.event_id),
        episode_id="ep-clo-recovery", pipeline_id=2, reason="original owner exited",
    )
    with pytest.raises(EpisodeCaptureUnavailable, match="would not accept it"):
        record_event("ep-clo-0010", operation_id="op-1", phase="auto", event_kind="k",
                     payload={"v": 1})
    assert episode_sram.pending("ep-clo-0010") == []
    surviving = read_episode_events("ep-clo-0010")
    assert len(surviving) == 1 and surviving[0].sealed_sha256() == original.sealed_sha256()


# ── WO-03A closure F4: journal READ failures at the public capture boundary ────


def _break_the_journal() -> None:
    intake_journal.journal_path().write_text('{"schema_version":', encoding="utf-8")
    intake_journal.reset_cache_for_tests()


def test_an_unreadable_journal_fails_the_initial_capture_lookup_at_the_boundary(journal_dir):
    episode_sram.open_bucket("ep-clo-0011", pipeline_id=0)
    _break_the_journal()
    with pytest.raises(EpisodeCaptureUnavailable, match="could not be read while looking up") as caught:
        record_event("ep-clo-0011", operation_id="op-1", phase="auto", event_kind="k", payload={})
    assert isinstance(caught.value.__cause__, intake_journal.IntakeJournalError)
    assert isinstance(caught.value.__cause__.__cause__, ValueError), "the parse cause survives"
    assert episode_sram.pending("ep-clo-0011") == []


def test_an_unreadable_journal_fails_the_read_path_at_the_boundary(journal_dir):
    episode_sram.open_bucket("ep-clo-0012", pipeline_id=0)
    _break_the_journal()
    with pytest.raises(EpisodeCaptureUnavailable, match="could not be read while reading episode") as caught:
        read_episode_events("ep-clo-0012")
    assert isinstance(caught.value.__cause__, intake_journal.IntakeJournalError)


def test_an_unreadable_journal_is_never_logged_back_through_itself(journal_dir, monkeypatch):
    """No evidence is fabricated and no append is attempted through the path that just failed."""
    episode_sram.open_bucket("ep-clo-0013", pipeline_id=0)
    _break_the_journal()
    appends: list[str] = []

    with monkeypatch.context() as patched:
        patched.setattr(
            intake_journal,
            "_append_locked",
            lambda record, operation: appends.append(operation),
        )
        with pytest.raises(EpisodeCaptureUnavailable):
            record_event("ep-clo-0013", operation_id="op-1", phase="auto", event_kind="k", payload={})
        with pytest.raises(EpisodeCaptureUnavailable):
            read_episode_events("ep-clo-0013")
    assert appends == [], f"capture wrote through the broken journal: {appends}"
    assert episode_sram.pending("ep-clo-0013") == []
    assert intake_journal.journal_path().read_text(encoding="utf-8") == '{"schema_version":'
