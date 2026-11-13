"""Fold-time statement ingestion — memory is first and truth (operator 2026-07-11).

Fold statements are the mind's own comprehension: they land in SITUATIONAL (4, 1)
"what", never in knowledge (2) / coding (8), which are reserved for fetched
external content (operator routing directive, same day).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memory import episode_sram, knowledge_ingest


def _receipt(status: str, path=None):
    return SimpleNamespace(validation_status=status, path=path, message="")


@pytest.fixture(autouse=True)
def _episode_bucket():
    episode_sram.reset()
    episode_sram.open_bucket("test-knowledge-ingest", pipeline_id=0)
    try:
        yield
    finally:
        episode_sram.reset()


def test_accepted_statements_return_held_receipts() -> None:

    receipts = knowledge_ingest.ingest_knowledge_products(
        "ep-1",
        [
            {"domain": "ml", "statement": "concept drift is a risk", "confidence": 0.9},
            {"domain": "ml", "statement": "DOME standardizes validation", "confidence": 0.9},
        ],
    )
    envelopes = [item.envelope for item in episode_sram.pending()]

    assert len(receipts) == 2
    assert all(r["status"] == "held" for r in receipts)
    assert all(r["coord"] == knowledge_ingest.FOLD_STATEMENT_COORD for r in receipts), (
        "fold statements live in situational (4,1) 'what'"
    )
    assert all(env.coord == [4, 1, 0] for env in envelopes)
    assert all(env.coord[0] not in (2, 8) for env in envelopes), (
        "knowledge/coding lobes are for FETCHED information, never fold synthesis"
    )
    assert receipts[0]["path"] is None


def test_zero_commits_against_claimed_knowledge_fails_hard(monkeypatch) -> None:
    """An unrecorded action is a system failure — never a silent success."""
    monkeypatch.setattr("memory.api.intake_memory", lambda envelope, **_k: _receipt("rejected"))

    with pytest.raises(RuntimeError, match="memory is first and truth"):
        knowledge_ingest.ingest_knowledge_products(
            "ep-2", [{"domain": "ml", "statement": "s", "confidence": 0.5}]
        )


def test_partial_commit_survives_with_honest_statuses(monkeypatch) -> None:
    outcomes = iter([_receipt("held"), _receipt("rejected")])
    monkeypatch.setattr("memory.api.intake_memory", lambda envelope, **_k: next(outcomes))

    receipts = knowledge_ingest.ingest_knowledge_products(
        "ep-3",
        [
            {"domain": "ml", "statement": "a", "confidence": 0.5},
            {"domain": "ml", "statement": "b", "confidence": 0.5},
        ],
    )

    assert [r["status"] for r in receipts] == ["held", "rejected"]


def test_empty_products_are_a_noop() -> None:
    assert knowledge_ingest.ingest_knowledge_products("ep-4", []) == []
