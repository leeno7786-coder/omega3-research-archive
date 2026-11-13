"""Real native intake, canonical commit and verified recall in an isolated profile."""

from memory import api
from memory import episode_sram as sram


def test_new_fact_is_absent_until_commit_then_verified_without_an_answer_model(monkeypatch):
    from runtime.client import RuntimeClient

    answer_calls = []

    def forbid_answer(*args, **kwargs):
        answer_calls.append(True)
        raise AssertionError("an answer model must not be used by this recall")

    monkeypatch.setattr(RuntimeClient, "generate", forbid_answer)
    sram.open_bucket("episode-first", pipeline_id=0)
    envelope = api.CanonicalEnvelope(
        cycle_id="source-event-calibration",
        coord=[2, 0, 0],
        type="concept",
        id="upgrade-calibration-event",
        payload={"text": "The calibration token is zephyrquartz."},
        source="memory.upgrade",
        confidence=0.9,
        timestamp_created="2026-09-01T12:00:00+00:00",
    )
    held = api.intake_memory(envelope)
    assert held.validation_status == "held" and held.path is None
    before = api.recall_verified(api.QuerySpec(cycle_id="query-first", text="zephyrquartz"))
    assert before.status == "unknown"
    assert before.evidence == ()
    outcome = sram.close_bucket("episode-first")
    assert outcome["memories"] and not outcome["skipped"]
    sram.open_bucket("episode-next", pipeline_id=0)
    after = api.recall_verified(api.QuerySpec(cycle_id="query-next", text="zephyrquartz"))
    assert after.status == "verified", after.reason
    evidence = {row["memory_id"]: row for row in after.evidence}
    assert "upgrade-calibration-event" in evidence
    assert evidence["upgrade-calibration-event"]["evidence_strength"] == "source_ledger_crypto"
    assert answer_calls == []
