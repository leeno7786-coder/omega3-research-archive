"""Interrupted non-idempotent intake must be accounted for without replay (M09)."""
from __future__ import annotations

import pytest

from memory import episode_sram, intake_journal


@pytest.fixture(autouse=True)
def isolated_episodes():
    episode_sram.reset()
    yield
    episode_sram.reset()


@pytest.mark.parametrize("kind", ["turn", "recall"])
@pytest.mark.parametrize("cascade_replay", [False, True])
def test_boot_classifies_interrupted_effects_with_durable_cause(kind, cascade_replay):
    episode_sram.open_bucket("interrupted", pipeline_id=0)
    if kind == "turn":
        episode_sram.hold_turn("user", "Synthetic interrupted turn")
    else:
        episode_sram.hold_verified_recall_for_episode("interrupted", ["synthetic.memory"])
    record = intake_journal.pending_records(owner_episode_id="interrupted")[0]
    intake_journal.mark_applying(record.record_id, note="effect started before simulated interruption")
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()

    report = intake_journal.reconcile_at_boot(episode_id="interrupted", pipeline_id=0,
                                             cascade_replay=cascade_replay)

    recovered = intake_journal.record_by_id(record.record_id)
    assert report["applying"] == 1
    assert report["uncertain"] == 1
    assert recovered.status == intake_journal.UNCERTAIN
    assert "interrupted" in recovered.note
    assert "effect started" in recovered.note
    episode_sram.open_bucket("after-restart", pipeline_id=0)
    failures = episode_sram.pending("after-restart")
    assert len(failures) == 1
    assert failures[0].kind == "memory"
    assert failures[0].envelope.payload["enrichment"]["intake_recovery"]["record_id"] == record.record_id
    assert "effect started" in failures[0].envelope.payload["text"]


def test_recovery_failure_reaches_canonical_memory_without_repeating_effect(monkeypatch):
    from memory.engine import walker

    episode_sram.open_bucket("interrupted", pipeline_id=0)
    episode_sram.hold_turn("user", "Synthetic interrupted turn")
    original = intake_journal.pending_records(owner_episode_id="interrupted")[0]
    intake_journal.mark_applying(original.record_id, note="turn delivery interrupted")
    episode_sram.reset()
    intake_journal.reconcile_at_boot(episode_id="interrupted", pipeline_id=0, cascade_replay=False)
    monkeypatch.setattr("memory.hot_buffer.append_turn", lambda *a, **k: pytest.fail("uncertain effect replayed"))
    episode_sram.open_bucket("recovery", pipeline_id=0)
    held = episode_sram.pending("recovery")
    assert len(held) == 1
    memory_id = held[0].envelope.id
    assert walker.path_for_id(memory_id) is None
    result = episode_sram.close_bucket("recovery")
    assert result["memories"] == [memory_id]
    meta = walker.load_meta_by_path(walker.path_for_id(memory_id))
    assert "turn delivery interrupted" in meta["text"]
    assert meta["enrichment"]["intake_recovery"]["record_id"] == original.record_id
    episode_sram.reset()
    intake_journal.reconcile_at_boot(episode_id="recovery", pipeline_id=0, cascade_replay=False)
    episode_sram.open_bucket("next", pipeline_id=0)
    assert episode_sram.pending("next") == []


def test_recovery_does_not_classify_another_lanes_work():
    episode_sram.open_bucket("other-lane", pipeline_id=1)
    episode_sram.hold_turn("user", "Browser turn")
    record = intake_journal.pending_records(owner_episode_id="other-lane")[0]
    intake_journal.mark_applying(record.record_id, note="browser effect in progress")
    report = intake_journal.reconcile_at_boot(episode_id="chat-old", pipeline_id=0, cascade_replay=False)
    assert report.get("applying", 0) == 0
    assert intake_journal.record_by_id(record.record_id).status == intake_journal.APPLYING


def test_failed_recovery_admission_is_retryable_without_restart(monkeypatch):
    from memory import api

    episode_sram.open_bucket("interrupted", pipeline_id=0)
    episode_sram.hold_turn("user", "Synthetic interrupted turn")
    record = intake_journal.pending_records(owner_episode_id="interrupted")[0]
    intake_journal.mark_applying(record.record_id, note="interrupted delivery")
    episode_sram.reset()
    intake_journal.reconcile_at_boot(episode_id="interrupted", pipeline_id=0, cascade_replay=False)
    with monkeypatch.context() as fault:
        fault.setattr(api, "intake_memory", lambda *a, **k: (_ for _ in ()).throw(OSError("admission unavailable")))
        with pytest.raises(OSError, match="admission unavailable"):
            episode_sram.open_bucket("failed-recovery", pipeline_id=0)
    assert intake_journal.record_by_id(record.record_id).adoptable
    episode_sram.open_bucket("retry-recovery", pipeline_id=0)
    items = episode_sram.pending("retry-recovery")
    assert len(items) == 1
    assert items[0].envelope.payload["enrichment"]["intake_recovery"]["record_id"] == record.record_id
