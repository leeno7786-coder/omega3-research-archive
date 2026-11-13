"""Pipeline boot recovery releases intake without consolidating it."""

from __future__ import annotations

import json

from core import pipeline_state
from memory import api, episode_sram
from memory import intake_journal


def test_pipeline_restore_releases_owed_intake_for_the_next_episode(tmp_path, monkeypatch):
    journal_root = tmp_path / "guardian" / "state" / "intake"
    pipeline_root = tmp_path / "guardian" / "state" / "pipelines"
    monkeypatch.setattr(intake_journal, "journal_root", lambda: journal_root)
    monkeypatch.setattr(pipeline_state, "pipelines_dir", lambda: pipeline_root)
    monkeypatch.setattr(pipeline_state, "_recover_old_envelopes", lambda: [])
    intake_journal.reset_cache_for_tests()
    pipeline_state.reset_for_tests()
    episode_sram.reset()

    old = pipeline_state.PipelineEnvelope(
        pipeline_id=0,
        state="COMMITTING",
        phase="deepsleep_gate",
        active_episode_id="episode-old",
    )
    pipeline_root.mkdir(parents=True, exist_ok=True)
    (pipeline_root / "pipeline_0.json").write_text(
        json.dumps(old.to_dict()), encoding="utf-8"
    )
    envelope = api.CanonicalEnvelope(
        cycle_id="recovery-cycle",
        coord=[2, 0, 0],
        type="concept",
        id="recovery-memory",
        payload={"text": "recovery fact"},
        source="tests.memory.recovery",
    )
    record, _ = intake_journal.journal_memory_intake(
        envelope,
        owner_episode_id="episode-old",
        owner_pipeline_id=0,
        write_options={"propose": False, "bypass_guardian": True, "skip_embed": False},
        event_id="recovery-event",
    )

    report = pipeline_state.restore_pipelines_at_boot()

    assert report["pipelines"][0]["intake"]["released"] == 1
    assert record.record_id == intake_journal.pending_records()[0].record_id
    assert episode_sram.pending() == []

    episode_sram.open_bucket("episode-new", pipeline_id=0)
    pending = episode_sram.pending("episode-new")
    assert len(pending) == 1
    assert pending[0].journal_record_id == record.record_id
    assert pending[0].envelope.payload == {"text": "recovery fact"}


def test_corrupt_intake_journal_at_boot_degrades_one_pipeline_and_continues(tmp_path, monkeypatch):
    """A torn journal must not abort job/continuation/transaction recovery."""
    from guardian import terminal_feedback as tf

    journal_root = tmp_path / "guardian" / "state" / "intake"
    pipeline_root = tmp_path / "guardian" / "state" / "pipelines"
    monkeypatch.setattr(intake_journal, "journal_root", lambda: journal_root)
    monkeypatch.setattr(pipeline_state, "pipelines_dir", lambda: pipeline_root)
    monkeypatch.setattr(pipeline_state, "_recover_old_envelopes", lambda: [])
    intake_journal.reset_cache_for_tests()
    pipeline_state.reset_for_tests()
    episode_sram.reset()
    tf._reset_recent_events_ring_for_tests()

    pipeline_root.mkdir(parents=True, exist_ok=True)
    for pid, episode_id in ((0, "episode-old"), (1, "episode-other")):
        envelope = pipeline_state.PipelineEnvelope(
            pipeline_id=pid,
            state="COMMITTING",
            phase="deepsleep_gate",
            active_episode_id=episode_id,
        )
        (pipeline_root / f"pipeline_{pid}.json").write_text(
            json.dumps(envelope.to_dict()), encoding="utf-8"
        )

    journal_root.mkdir(parents=True, exist_ok=True)
    intake_journal.journal_path().write_text('{"schema_version":1\n', encoding="utf-8")
    intake_journal.reset_cache_for_tests()

    emitted: list[tuple] = []

    def _capture(system, event, **fields):
        emitted.append((system, event, fields))

    monkeypatch.setattr("guardian.terminal_feedback.emit_data", _capture)

    report = pipeline_state.restore_pipelines_at_boot()

    assert "jobs" in report
    assert "completed_transactions" in report
    assert 0 in report["pipelines"] and 1 in report["pipelines"]
    intake = report["pipelines"][0]["intake"]
    assert intake.get("error"), intake
    assert intake.get("released", 0) == 0
    assert any(
        event == "intake_journal_unreadable" and fields.get("status") == "degraded"
        for _system, event, fields in emitted
    )
    assert pipeline_state.read_envelope(1).state == "IDLE"
