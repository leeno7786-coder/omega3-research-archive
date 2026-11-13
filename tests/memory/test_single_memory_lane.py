from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

import memory.api as memory_api
import memory.episode_sram as sram
from memory import intake_journal
from memory.api import CanonicalEnvelope


def setup_function(_):
    sram.reset()


def teardown_function(_):
    sram.reset()


def _envelope(cycle_id: str = "ep-single-lane") -> CanonicalEnvelope:
    return CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=[2, 1, 0],
        type="knowledge_statement",
        payload={"text": "SRAM is the only intake lane."},
        source="omega:test",
    )


def test_write_memory_holds_in_the_open_episode_until_sram_drains(monkeypatch):
    committed: list[tuple[CanonicalEnvelope, dict]] = []
    source = _envelope()
    original = deepcopy(source)

    def _commit(envelope, **options):
        committed.append((envelope, options))
        return SimpleNamespace(
            cycle_id=envelope.cycle_id,
            staging_ref=None,
            path="2.1.0.memory-1",
            validation_status="committed",
            message="committed",
        )

    monkeypatch.setattr(memory_api, "write_memory", _commit)
    sram.open_bucket("ep-single-lane", pipeline_id=0)

    receipt = memory_api.intake_memory(
        source,
        propose=False,
        bypass_guardian=True,
        skip_embed=True,
    )

    assert receipt.validation_status == "held"
    assert receipt.path is None
    assert committed == []
    assert [item.kind for item in sram.pending()] == ["memory"]
    accepted = intake_journal.record_by_id(receipt.journal_record_id)
    assert accepted is not None and accepted.status == intake_journal.PENDING
    assert accepted.event_id == receipt.event_id
    assert source == original

    result = sram.close_bucket("ep-single-lane")

    assert len(committed) == 1
    written = committed[0][0]
    assert written is not source
    assert written.id == f"intake.{accepted.record_id}"
    unstamped = deepcopy(written.payload)
    proof = unstamped["enrichment"].pop("intake_commit")
    assert proof == {
        "record_id": accepted.record_id,
        "event_id": accepted.event_id,
        "fingerprint": accepted.fingerprint,
    }
    if not unstamped["enrichment"]:
        del unstamped["enrichment"]
    assert replace(written, id=original.id, payload=unstamped) == original
    assert source == original
    assert committed[0][1] == {
        "propose": False,
        "bypass_guardian": True,
        "skip_embed": True,
    }
    assert result["memories"] == ["2.1.0.memory-1"]


@pytest.mark.parametrize("status", ["held", "committed"])
def test_intake_receipt_accepts_only_live_lane_states(status):
    receipt = SimpleNamespace(validation_status=status)

    assert memory_api.intake_receipt_accepted(receipt) is True


@pytest.mark.parametrize("status", ["", "staged", "rejected", None])
def test_intake_receipt_rejects_non_lane_states(status):
    receipt = SimpleNamespace(validation_status=status)

    assert memory_api.intake_receipt_accepted(receipt) is False


def test_memory_intake_without_an_episode_fails_instead_of_creating_a_second_lane():
    with pytest.raises(RuntimeError, match="no episode SRAM bucket"):
        memory_api.intake_memory(_envelope("detached-intake"), skip_embed=True)

    assert sram.current_episode_id() is None
    assert sram.pending() == []


def test_failed_memory_commit_keeps_the_sram_record_for_retry(monkeypatch):
    monkeypatch.setattr(
        memory_api,
        "write_memory",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("canonical filesystem unavailable")),
        raising=False,
    )
    sram.open_bucket("ep-memory-retry", pipeline_id=0)
    memory_api.intake_memory(_envelope("ep-memory-retry"), skip_embed=True)

    with pytest.raises(RuntimeError, match="canonical filesystem unavailable"):
        sram.flush("ep-memory-retry")

    assert [item.kind for item in sram.pending()] == ["memory"]


def test_atomic_write_memory_is_unavailable_outside_the_sram_drain():
    with pytest.raises(RuntimeError, match="DeepSleep SRAM drain"):
        memory_api.write_memory(_envelope("outside-drain"), skip_embed=True)


def test_canonical_io_exception_propagates_and_retains_the_held_envelope(monkeypatch):
    sram.open_bucket("ep-real-commit-failure", pipeline_id=0)
    memory_api.intake_memory(_envelope("ep-real-commit-failure"), skip_embed=True)
    monkeypatch.setattr(
        memory_api,
        "create_staging",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("canonical volume offline")),
    )

    with pytest.raises(OSError, match="canonical volume offline"):
        sram.flush("ep-real-commit-failure")

    assert [item.kind for item in sram.pending()] == ["memory"]
