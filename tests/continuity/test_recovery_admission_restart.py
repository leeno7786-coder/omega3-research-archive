"""A second crash during READY admission must not orphan recovery memory."""
from __future__ import annotations

import pytest

import cognition_chain
from core import pipeline_state
from core.cognition_lock import pipeline_cognition_lock
from memory import api, episode_sram, intake_journal
from memory.engine import walker
from pressure_loop import cognition_queue
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.episode import CognitiveEpisode, EpisodeStore


class AdmissionCrash(BaseException):
    """Process loss bypasses ordinary exception cleanup."""


def _restart():
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()
    pipeline_state.reset_for_tests()
    cognition_queue._reset_cache_for_tests()
    return pipeline_state.restore_pipelines_at_boot()


def _ready():
    contributor = make_contributor(
        "operator", "operator_query", 1.0,
        {"cycle_id": "recovery-admission", "message": "Continue the conversation"},
    )
    cognition_queue.enqueue_input(contributor, operator_activity=False)
    leased = cognition_queue.lease_next_input(0)
    ignition = EpisodeIgnition(
        "recovery-ignition", 1.0, leased.type, leased, (leased,), None,
        pipeline_id=0, work_kind="operator_turn",
    )
    pipeline_state.mark_ready(0, ignition)
    return ignition


@pytest.mark.parametrize("kind", ["turn", "recall"])
@pytest.mark.parametrize("cut", ["before_evidence", "before_link", "after_link"])
def test_second_boot_recovers_admission_owned_intake_without_active_envelope(monkeypatch, kind, cut):
    original_episode = CognitiveEpisode(pipeline_id=0, work_kind="operator_turn")
    episode_sram.open_bucket(original_episode.episode_id, pipeline_id=0)
    if kind == "turn":
        episode_sram.hold_turn("user", "Synthetic interrupted delivery")
    else:
        episode_sram.hold_verified_recall_for_episode(original_episode.episode_id, ["synthetic.memory"])
    original = intake_journal.pending_records(owner_episode_id=original_episode.episode_id)[0]
    intake_journal.mark_applying(original.record_id, note="effect interrupted before completion")
    pipeline_state.mark_committing(original_episode)

    _restart()
    assert intake_journal.record_by_id(original.record_id).adoptable
    ignition = _ready()

    def crash(*args, **kwargs):
        raise AdmissionCrash(cut)

    with monkeypatch.context() as fault:
        if cut == "before_evidence":
            fault.setattr(api, "intake_memory", crash)
        elif cut == "before_link":
            fault.setattr(intake_journal, "mark_recovery_record", crash)
        else:
            fault.setattr("memory.cognition_capture.capture_admission_origin", crash)
        with pipeline_cognition_lock(0), pytest.raises(AdmissionCrash, match=cut):
            cognition_chain._admit_episode(EpisodeStore(), ignition)

    assert pipeline_state.read_envelope(0).active_episode_id == ""
    assert intake_journal.record_by_id(original.record_id).owner_episode_id != original_episode.episode_id
    _restart()  # Discover owners from durable state, never pass the hidden episode ID.
    from memory import hot_buffer

    append_turn = hot_buffer.append_turn

    def reject_original_effect(role, text):
        assert text != "Synthetic interrupted delivery", "uncertain turn effect repeated"
        return append_turn(role, text)

    monkeypatch.setattr(hot_buffer, "append_turn", reject_original_effect)
    monkeypatch.setattr(
        "memory.confidence_decay.apply_recall_confidence_decay",
        lambda *a, **k: pytest.fail("uncertain recall effect repeated"),
    )
    episode_sram.open_bucket("after-second-crash", pipeline_id=0)
    recovered = [
        item for item in episode_sram.pending("after-second-crash")
        if item.kind == "memory" and item.envelope.payload.get("enrichment", {}).get("intake_recovery")
    ]
    assert len(recovered) == 1
    assert recovered[0].kind == "memory"
    record = recovered[0].envelope
    assert record.payload["enrichment"]["intake_recovery"]["record_id"] == original.record_id
    episode_sram.close_bucket("after-second-crash")
    canonical = walker.load_meta_by_path(walker.path_for_id(record.id))
    assert "effect interrupted before completion" in canonical["text"]
    assert intake_journal.record_by_id(original.record_id).status == intake_journal.UNCERTAIN


def test_boot_keeps_current_replay_pending_while_releasing_an_untracked_owner():
    def hold(episode_id):
        episode_sram.open_bucket(episode_id, pipeline_id=0)
        receipt = api.intake_memory(api.CanonicalEnvelope(
            cycle_id=episode_id, id=f"recovery.{episode_id}", coord=[2, 0, 0], type="concept",
            payload={"text": f"Evidence for {episode_id}"}, source="tests.recovery",
        ), event_id=episode_id)
        episode_sram.reset()
        return receipt.journal_record_id

    orphan_id = hold("untracked")
    current_id = hold("replayed")
    report = intake_journal.reconcile_at_boot(episode_id="replayed", pipeline_id=0, cascade_replay=True)
    assert report["released"] == 1
    assert intake_journal.record_by_id(orphan_id).adoptable
    assert not intake_journal.record_by_id(current_id).adoptable
