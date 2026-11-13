"""Traits are a pattern-voting system, not semantic memory (operator 2026-08-10).

The trait family holds filesystem + coord-index citizenship. Semantic encoding
and reflection/DME membership are bypassed. Source-backed canonical trait claims
still receive immutable integrity receipts; the voting system reads their files
and the omega fold appends promoted traits to the chat templates.
"""

from __future__ import annotations

import pytest


def _record(rtype: str, rid: str) -> dict:
    return {
        "id": rid,
        "type": rtype,
        "coord": [0, 1, 0],
        "text": "Candidate identity observation for verifiable_precision: I verify.",
        "provenance": {},
    }


@pytest.mark.parametrize(
    ("rtype", "rid"),
    [
        ("emergent_trait", "trait.verifiable_precision.abc123"),
        ("trait_observation", "trait-observation.verifiable_precision.def456"),
    ],
)
def test_trait_family_bypasses_the_encode_pipeline(tmp_path, rtype, rid):
    from memory import encoding

    record = _record(rtype, rid)
    enc = encoding.encode_committed_memory(record, str(tmp_path / f"{rid}.json"))

    assert enc.ok
    assert enc.steps_completed == ["non_semantic_bypass"]
    assert "embeddings" not in record  # no vector minted
    receipts = list(tmp_path.rglob("projection_receipts/*"))
    assert receipts == []  # this low-level fixture has no source citation


def test_other_identity_writes_still_encode(tmp_path, monkeypatch):
    """The bypass is TYPE-scoped: a non-trait identity write still enters the
    pipeline and walks every encoding step.

    This used to prove it by watching the write attempt the embed step against
    a dead embedder. There is no embed step any more — semantic perception is
    the resident Ettin retina, encoded at publication, not a vector attached at
    write — so the proof is the step ledger itself: a persona fact runs the
    pipeline; the trait family (test above) never does."""
    from memory import encoding

    record = {
        "id": "persona.tz.abc",
        "type": "persona_fact",
        "coord": [0, 0, 0],
        "text": "Operator timezone is US/Central.",
        "provenance": {},
    }
    enc = encoding.encode_committed_memory(record, str(tmp_path / "p.json"))

    assert enc.ok
    assert enc.is_identity_write is True
    assert "persistence_anchor" in enc.steps_completed
    assert "deepsleep_candidacy" in enc.steps_completed
    assert "embed" not in enc.steps_completed
