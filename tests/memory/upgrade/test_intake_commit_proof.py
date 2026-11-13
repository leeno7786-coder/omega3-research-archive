"""Real-file regressions for the intake acknowledgement boundary (M02)."""
from __future__ import annotations

import json

import pytest

from memory import api, episode_sram, intake_journal
from memory.engine import walker
from tests.memory.projection._fixtures import redirect_timekeeper


@pytest.fixture(autouse=True)
def isolated_episodes(tmp_path):
    redirect_timekeeper(tmp_path)
    episode_sram.reset()
    yield
    episode_sram.reset()


def envelope(text="First durable claim", cycle="first"):
    return api.CanonicalEnvelope(cycle_id=cycle, coord=[2, 0, 0], type="concept",
                                 id="commit.proof.example", payload={"text": text},
                                 source="tests.intake_commit_proof")


def commit_one():
    episode_sram.open_bucket("first", pipeline_id=0)
    receipt = api.intake_memory(envelope(), event_id="first-event")
    episode_sram.close_bucket("first")
    return receipt


def test_same_id_with_different_content_cannot_be_acknowledged_as_previous_write():
    commit_one()
    episode_sram.open_bucket("second", pipeline_id=0)
    receipt = api.intake_memory(envelope("Changed claim", "second"), event_id="second-event")

    with pytest.raises(intake_journal.IntakeJournalError, match="canonical binding"):
        episode_sram.close_bucket("second")

    assert intake_journal.record_by_id(receipt.journal_record_id).status == intake_journal.PENDING
    assert episode_sram.pending("second")
    assert walker.load_meta_by_path(walker.path_for_id("commit.proof.example"))["text"] == "First durable claim"


@pytest.mark.parametrize("remove_requirement", [False, True])
def test_committed_replay_rejects_changed_canonical_claim(remove_requirement):
    commit_one()
    path = walker.path_for_id("commit.proof.example")
    from pathlib import Path
    path = Path(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["text"] = "Altered after the commit"
    if remove_requirement:
        document["provenance"].pop("projection_receipt_required", None)
    path.write_text(json.dumps(document), encoding="utf-8")
    episode_sram.open_bucket("replay", pipeline_id=0)

    with pytest.raises(intake_journal.IntakeJournalError, match="verifiable canonical"):
        api.intake_memory(envelope(), event_id="first-event")


def test_lost_ack_recovers_exact_write_without_writing_twice(monkeypatch):
    episode_sram.open_bucket("first", pipeline_id=0)
    receipt = api.intake_memory(envelope(), event_id="first-event")
    real_mark = intake_journal.mark_committed
    with monkeypatch.context() as fault:
        fault.setattr(intake_journal, "mark_committed", lambda *a, **k: (_ for _ in ()).throw(OSError("lost acknowledgement")))
        with pytest.raises(OSError, match="lost acknowledgement"):
            episode_sram.close_bucket("first")
    path = walker.path_for_id("commit.proof.example")
    assert path
    monkeypatch.setattr(api, "write_memory", lambda *a, **k: pytest.fail("exact committed event was written twice"))
    assert episode_sram.close_bucket("first")["memories"] == ["commit.proof.example"]
    assert intake_journal.record_by_id(receipt.journal_record_id).status == intake_journal.COMMITTED
    assert intake_journal.mark_committed == real_mark


@pytest.mark.parametrize("identity", ["payload", "generated"])
def test_lost_ack_recovers_envelopes_without_outer_id(monkeypatch, identity):
    from dataclasses import replace

    item = replace(envelope(), id=None)
    if identity == "payload":
        item.payload["id"] = "payload.identity"
    episode_sram.open_bucket("first", pipeline_id=0)
    receipt = api.intake_memory(item, event_id="identity-event")
    with monkeypatch.context() as fault:
        fault.setattr(intake_journal, "mark_committed", lambda *a, **k: (_ for _ in ()).throw(OSError("lost ack")))
        with pytest.raises(OSError, match="lost ack"):
            episode_sram.close_bucket("first")
    monkeypatch.setattr(api, "write_memory", lambda *a, **k: pytest.fail("durable event written twice"))
    assert len(episode_sram.close_bucket("first")["memories"]) == 1
    assert intake_journal.record_by_id(receipt.journal_record_id).status == intake_journal.COMMITTED


def test_payload_source_identity_cannot_merge_distinct_intake_records():
    from dataclasses import replace

    episode_sram.open_bucket("first", pipeline_id=0)
    for ordinal in (1, 2):
        item = replace(envelope(f"Phase claim {ordinal}"), id=None)
        item.payload["id"] = "shared.source.citation"
        api.intake_memory(item, event_id=f"phase-{ordinal}")
    result = episode_sram.close_bucket("first")
    assert len(set(result["memories"])) == 2
    assert "shared.source.citation" not in result["memories"]


def test_nonsemantic_claim_alteration_is_rejected(tmp_path):
    from deepsleep_loop import identity_store
    from deepsleep_loop._deepsleep_state import Trait
    from pathlib import Path

    episode_sram.open_bucket("trait", pipeline_id=0)
    identity_store.upsert_trait(Trait("synthetic_trait", 0.2, 2), path=tmp_path / "absent.json", cycle_id="trait")
    pending = intake_journal.pending_records(owner_episode_id="trait")[0]
    episode_sram.close_bucket("trait")
    record = intake_journal.record_by_id(pending.record_id)
    path = Path(walker.path_for_id(record.memory_id))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["trait"]["strength"] = 0.99
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert intake_journal.verify_canonical_binding(record)[0] == "mismatch"


def test_retained_committed_prefix_is_reverified_after_failed_drain(monkeypatch):
    from dataclasses import replace
    from pathlib import Path

    episode_sram.open_bucket("first", pipeline_id=0)
    api.intake_memory(envelope(), event_id="first-event")
    api.intake_memory(replace(envelope("Second claim"), id="second.claim"), event_id="second-event")
    original_write = api.write_memory

    def fail_second(env, **kwargs):
        if env.id == "second.claim":
            raise OSError("second write interrupted")
        return original_write(env, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(api, "write_memory", fail_second)
        with pytest.raises(OSError, match="second write interrupted"):
            episode_sram.close_bucket("first")
    path = Path(walker.path_for_id("commit.proof.example"))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["text"] = "Changed between retries"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(intake_journal.IntakeJournalError, match="canonical binding"):
        episode_sram.close_bucket("first")


def test_identity_update_creates_recallable_revision_on_real_disk(tmp_path):
    from deepsleep_loop import identity_store
    from deepsleep_loop._deepsleep_state import Trait

    for cycle, strength, count in (("trait-first", 0.2, 2), ("trait-update", 0.3, 3)):
        episode_sram.open_bucket(cycle, pipeline_id=0)
        identity_store.upsert_trait(Trait("synthetic_trait", strength, count),
                                    path=tmp_path / "absent.json", cycle_id=cycle)
        episode_sram.close_bucket(cycle)
        actual = identity_store.load_identity(path=tmp_path / "absent.json")["synthetic_trait"]
        assert (actual.strength, actual.evidence_count) == (strength, count)
