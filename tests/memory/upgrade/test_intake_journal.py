"""Durable identity and terminal-state contracts for canonical SRAM intake."""

from __future__ import annotations

import pytest

from memory import api


@pytest.fixture
def isolated_intake_journal(tmp_path, monkeypatch):
    from memory import episode_sram, intake_journal

    root = tmp_path / "guardian" / "state" / "intake"
    monkeypatch.setattr(intake_journal, "journal_root", lambda: root)
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    yield intake_journal
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()


def _envelope(text="durable fact"):
    return api.CanonicalEnvelope(
        cycle_id="episode-journal",
        coord=[2, 0, 0],
        type="concept",
        id="journal-memory",
        payload={"text": text},
        source="tests.memory.upgrade",
    )


def test_intake_acknowledgement_has_fsynced_identity_before_sram_hold(isolated_intake_journal):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    receipt = api.intake_memory(_envelope(), event_id="source-event-1")

    assert receipt.validation_status == "held"
    assert receipt.durable is True
    assert receipt.journal_record_id
    assert receipt.event_id == "source-event-1"
    record = isolated_intake_journal.record_by_id(receipt.journal_record_id)
    assert record is not None
    assert record.status == isolated_intake_journal.PENDING
    assert isolated_intake_journal.journal_path().is_file()
    assert len(episode_sram.pending("episode-journal")) == 1


def test_same_source_event_is_idempotent_and_does_not_add_a_second_sram_item(
    isolated_intake_journal,
):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    first = api.intake_memory(_envelope(), event_id="source-event-1")
    second = api.intake_memory(_envelope(), event_id="source-event-1")

    assert first.journal_record_id == second.journal_record_id
    assert second.replayed is True
    assert len(episode_sram.pending("episode-journal")) == 1


def test_same_source_event_with_different_content_is_a_conflict(isolated_intake_journal):
    from memory import episode_sram, intake_journal

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    api.intake_memory(_envelope("first"), event_id="source-event-1")
    with pytest.raises(intake_journal.IntakeJournalConflict):
        api.intake_memory(_envelope("changed"), event_id="source-event-1")
    assert len(episode_sram.pending("episode-journal")) == 1


def test_replaying_a_committed_event_reuses_the_verified_canonical_receipt(
    isolated_intake_journal, monkeypatch
):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    first = api.intake_memory(_envelope(), event_id="source-event-1")
    isolated_intake_journal.mark_committed(
        first.journal_record_id,
        memory_id="canonical-memory",
        canonical_path="canonical-memory",
    )
    monkeypatch.setattr(api, "_canonical_binding_exists", lambda memory_id: True)

    second = api.intake_memory(_envelope(), event_id="source-event-1")

    assert second.validation_status == "committed"
    assert second.durable is True
    assert second.replayed is True
    assert second.path == "canonical-memory"
    assert episode_sram.pending("episode-journal") == []


def test_journal_write_failure_never_returns_a_held_success(
    isolated_intake_journal, monkeypatch
):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)

    def fail(_handle):
        raise OSError("journal volume offline")

    with monkeypatch.context() as fault:
        fault.setattr(isolated_intake_journal.os, "fsync", fail)
        with pytest.raises(isolated_intake_journal.IntakeJournalError, match="journal"):
            api.intake_memory(_envelope(), event_id="source-event-1")
    assert episode_sram.pending("episode-journal") == []


def test_corrupt_journal_is_not_read_as_empty(isolated_intake_journal):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    path = isolated_intake_journal.journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version":1\n', encoding="utf-8")
    isolated_intake_journal.reset_cache_for_tests()

    with pytest.raises(isolated_intake_journal.IntakeJournalUnreadable):
        api.intake_memory(_envelope(), event_id="source-event-1")
    assert episode_sram.pending("episode-journal") == []


def test_perception_and_turn_holds_have_durable_item_identity(isolated_intake_journal):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    episode_sram.hold_perception("I prefer short explanations.")
    episode_sram.hold_turn("user", "Please keep this concise.")

    records = isolated_intake_journal.records()
    assert [record.kind for record in records] == ["perception", "turn"]
    assert all(record.status == isolated_intake_journal.PENDING for record in records)
    assert all(item.journal_record_id for item in episode_sram.pending("episode-journal"))


def test_graduated_turn_is_terminally_consumed(isolated_intake_journal):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    episode_sram.hold_turn("user", "The turn graduates now.")
    assert episode_sram.graduate_turns_now("episode-journal") == 1

    record = next(record for record in isolated_intake_journal.records() if record.kind == "turn")
    assert record.status == isolated_intake_journal.CONSUMED


def test_recall_maintenance_commits_only_after_all_steps(isolated_intake_journal, monkeypatch):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    assert episode_sram.hold_verified_recall_for_episode("episode-journal", ["memory-1"]) == 1
    monkeypatch.setattr(
        "memory.confidence_decay.apply_recall_confidence_decay", lambda _memory_id: None
    )
    monkeypatch.setattr(
        "memory.demotion.apply_recall_sustained_floor_demotion", lambda _memory_id: None
    )
    monkeypatch.setattr(
        "memory.strength_index.ensure_strength_index", lambda: type("Index", (), {"touch": lambda self, _id: None})()
    )

    episode_sram.close_bucket("episode-journal")
    record = next(record for record in isolated_intake_journal.records() if record.kind == "recall")
    assert record.status == isolated_intake_journal.COMMITTED


def test_interrupted_recall_maintenance_is_uncertain_not_replayed(
    isolated_intake_journal, monkeypatch
):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    episode_sram.hold_verified_recall_for_episode("episode-journal", ["memory-1"])
    monkeypatch.setattr(
        "memory.confidence_decay.apply_recall_confidence_decay",
        lambda _memory_id: (_ for _ in ()).throw(OSError("decay store offline")),
    )

    with pytest.raises(OSError, match="decay store offline"):
        episode_sram.close_bucket("episode-journal")
    record = next(record for record in isolated_intake_journal.records() if record.kind == "recall")
    assert record.status == isolated_intake_journal.UNCERTAIN


@pytest.mark.parametrize("status", ["committed", "rejected"])
def test_deepsleep_marks_the_journal_terminal(isolated_intake_journal, monkeypatch, status):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    receipt = api.intake_memory(_envelope(), event_id="source-event-1")

    def fake_write(envelope, **_kwargs):
        return api.WriteReceipt(
            cycle_id=envelope.cycle_id,
            staging_ref=None,
            path="canonical-memory" if status == "committed" else None,
            validation_status=status,
            message="test outcome",
            durable=True,
        )

    monkeypatch.setattr(api, "write_memory", fake_write)
    result = episode_sram.close_bucket("episode-journal")
    record = isolated_intake_journal.record_by_id(receipt.journal_record_id)
    assert record is not None
    assert record.status == status
    if status == "committed":
        assert result["memories"] == ["canonical-memory"]
    else:
        assert result["skipped"]


# ── confirm_durable: visibility is not a durability receipt (WO-03A R4) ───────
#
# Necessity: `_append_locked` writes and flushes BEFORE it fsyncs, so a failed durability step
# leaves readable bytes in the journal. Nothing in this module could re-establish or prove
# durability for a row that already exists, so a caller retrying after such a failure could only
# ask "can I read it?" — which is not the same question. These are the paired regressions for
# that added seam.


def test_confirm_durable_returns_the_row_it_proved(isolated_intake_journal):
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    receipt = api.intake_memory(_envelope(), event_id="source-event-confirm")
    record_id = isolated_intake_journal._record_id("source-event-confirm")

    confirmed = isolated_intake_journal.confirm_durable(record_id)
    assert confirmed.record_id == record_id
    assert confirmed.event_id == "source-event-confirm"
    assert confirmed.status == isolated_intake_journal.PENDING
    assert receipt.validation_status == "held"


def test_confirm_durable_refuses_a_row_that_is_not_on_disk(isolated_intake_journal):
    with pytest.raises(isolated_intake_journal.IntakeJournalError, match="is not present on disk"):
        isolated_intake_journal.confirm_durable("ijr_nothing_was_ever_written")


def test_confirm_durable_requires_a_record_id(isolated_intake_journal):
    with pytest.raises(isolated_intake_journal.IntakeJournalError, match="requires a record id"):
        isolated_intake_journal.confirm_durable("   ")


def test_confirm_durable_fails_attributably_when_fsync_fails(isolated_intake_journal, monkeypatch):
    """Readable bytes plus a broken fsync is a failure, not a confirmation."""
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    api.intake_memory(_envelope(), event_id="source-event-unsynced")
    record_id = isolated_intake_journal._record_id("source-event-unsynced")
    assert isolated_intake_journal.record_by_id(record_id) is not None, "the row is readable"

    def _failed_fsync(_descriptor):
        raise OSError("fsync unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(isolated_intake_journal.os, "fsync", _failed_fsync)
        with pytest.raises(isolated_intake_journal.IntakeJournalError) as caught:
            isolated_intake_journal.confirm_durable(record_id)
    assert "could not confirm durable acceptance" in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError), "the primary cause survives"
    # The row itself is untouched: confirmation proves, it never rewrites.
    assert isolated_intake_journal.record_by_id(record_id).event_id == "source-event-unsynced"


def test_confirm_durable_can_prove_bytes_another_handle_wrote(isolated_intake_journal):
    """The fsync commits the FILE, so a row appended through a closed handle is covered."""
    from memory import episode_sram

    episode_sram.open_bucket("episode-journal", pipeline_id=0)
    api.intake_memory(_envelope("first"), event_id="source-event-a")
    api.intake_memory(_envelope("second"), event_id="source-event-b")
    for event_id in ("source-event-a", "source-event-b"):
        record_id = isolated_intake_journal._record_id(event_id)
        assert isolated_intake_journal.confirm_durable(record_id).event_id == event_id
