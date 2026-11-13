"""Boot classification and next-episode adoption for durable SRAM intake."""

from __future__ import annotations

import pytest

from memory import api
from memory import episode_sram


@pytest.fixture
def replay_state(tmp_path, monkeypatch):
    from memory import intake_journal

    root = tmp_path / "guardian" / "state" / "intake"
    monkeypatch.setattr(intake_journal, "journal_root", lambda: root)
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    yield intake_journal
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()


def _envelope():
    return api.CanonicalEnvelope(
        cycle_id="replay-source-cycle",
        coord=[2, 0, 0],
        type="concept",
        id="replay-memory",
        payload={"text": "replay me exactly once"},
        source="tests.memory.replay",
    )


def test_boot_releases_pending_intake_and_next_episode_adopts_it(
    replay_state, monkeypatch
):
    episode_sram.open_bucket("episode-old", pipeline_id=0)
    receipt = api.intake_memory(_envelope(), event_id="replay-event")
    episode_sram.reset()

    calls = []
    monkeypatch.setattr(api, "write_memory", lambda *args, **kwargs: calls.append(True))
    report = replay_state.reconcile_at_boot(
        episode_id="episode-old", pipeline_id=0, cascade_replay=False
    )

    assert report["pending"] == 1
    assert report["released"] == 1
    assert calls == []

    episode_sram.open_bucket("episode-new", pipeline_id=0)
    pending = episode_sram.pending("episode-new")
    assert len(pending) == 1
    assert pending[0].journal_record_id == receipt.journal_record_id
    record = replay_state.record_by_id(receipt.journal_record_id)
    assert record is not None
    assert record.owner_episode_id == "episode-new"
    assert record.adoptable is False
    assert calls == []


def test_cascade_replay_reowns_the_source_event_without_boot_consolidation(
    replay_state, monkeypatch
):
    episode_sram.open_bucket("episode-old", pipeline_id=0)
    receipt = api.intake_memory(_envelope(), event_id="replay-event")
    episode_sram.reset()

    monkeypatch.setattr(api, "write_memory", lambda *args, **kwargs: pytest.fail("boot wrote memory"))
    report = replay_state.reconcile_at_boot(
        episode_id="episode-old", pipeline_id=0, cascade_replay=True
    )
    assert report["pending"] == 1
    assert report["released"] == 0

    episode_sram.open_bucket("episode-new", pipeline_id=0)
    assert episode_sram.pending("episode-new") == []
    replayed = api.intake_memory(_envelope(), event_id="replay-event")

    assert replayed.validation_status == "held"
    assert replayed.replayed is True
    assert replayed.journal_record_id == receipt.journal_record_id
    assert len(episode_sram.pending("episode-new")) == 1
    record = replay_state.record_by_id(receipt.journal_record_id)
    assert record is not None
    assert record.owner_episode_id == "episode-new"


def test_boot_reconciliation_refuses_a_corrupt_journal(replay_state):
    path = replay_state.journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version":1\n', encoding="utf-8")
    replay_state.reset_cache_for_tests()

    with pytest.raises(replay_state.IntakeJournalUnreadable):
        replay_state.reconcile_at_boot(
            episode_id="episode-old", pipeline_id=0, cascade_replay=False
        )
    assert episode_sram.pending() == []


def test_retry_after_canonical_write_before_journal_ack_does_not_write_twice(
    replay_state, monkeypatch
):
    episode_sram.open_bucket("episode-crash", pipeline_id=0)
    receipt = api.intake_memory(_envelope(), event_id="crash-window-event")
    writes = []

    def fake_write(envelope, **_kwargs):
        writes.append(envelope.id)
        return api.WriteReceipt(
            cycle_id=envelope.cycle_id,
            staging_ref=None,
            path="replay-memory",
            validation_status="committed",
            message="canonical commit",
            durable=True,
        )

    monkeypatch.setattr(api, "write_memory", fake_write)
    monkeypatch.setattr(
        replay_state,
        "verify_canonical_binding",
        lambda record: ("missing", "replay-memory", ""),
    )
    real_mark = replay_state.mark_committed

    def fail_ack(*args, **kwargs):
        if not writes or len(writes) == 1:
            raise OSError("process died before journal acknowledgement")
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(replay_state, "mark_committed", fail_ack)
    with pytest.raises(OSError, match="journal acknowledgement"):
        episode_sram.close_bucket("episode-crash")
    assert writes == ["replay-memory"]
    assert replay_state.record_by_id(receipt.journal_record_id).status == replay_state.PENDING

    monkeypatch.setattr(
        replay_state,
        "verify_canonical_binding",
        lambda record: ("ok", "replay-memory", "canonical.json"),
    )
    monkeypatch.setattr(replay_state, "mark_committed", real_mark)
    result = episode_sram.close_bucket("episode-crash")

    assert writes == ["replay-memory"]
    assert result["memories"] == ["replay-memory"]
    assert replay_state.record_by_id(receipt.journal_record_id).status == replay_state.COMMITTED


def test_committed_replay_fails_closed_when_coordinate_index_is_unavailable(
    replay_state, monkeypatch
):
    episode_sram.open_bucket("episode-index", pipeline_id=0)
    receipt = api.intake_memory(_envelope(), event_id="index-event")
    replay_state.mark_committed(
        receipt.journal_record_id,
        memory_id="replay-memory",
        canonical_path="replay-memory",
    )
    monkeypatch.setattr(
        api,
        "_canonical_binding_exists",
        lambda _memory_id: (_ for _ in ()).throw(OSError("coordinate index unavailable")),
    )

    with pytest.raises(OSError, match="coordinate index unavailable"):
        api.intake_memory(_envelope(), event_id="index-event")
    assert episode_sram.pending("episode-index") == []
