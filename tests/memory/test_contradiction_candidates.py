"""DeepSleep candidate drain adjudicates contradiction groups."""

from __future__ import annotations

import json

import pytest

from memory.contradiction_resolution import (
    apply_loser_penalty,
    process_contradiction_candidates,
)
from memory.queues import enqueue_deepsleep_candidate


@pytest.fixture
def memory_tree(tmp_path, monkeypatch):
    import memory.engine.walker as walker
    import memory.queues as queues

    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    queue_dir = tmp_path / "queues"
    queue_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(queues, "DEEPSLEEP_QUEUE_PATH", queue_dir / "deepsleep_candidates.jsonl")

    def _write(mid: str, *, confidence: float, evidence: int = 0, when: str = ""):
        path = mem_root / f"{mid}.json"
        path.write_text(
            json.dumps(
                {
                    "id": mid,
                    "coord": [2, 0, 0],
                    "provenance": {"confidence": confidence},
                    "evidence_refs": ["e"] * evidence,
                    "written_at": when,
                }
            ),
            encoding="utf-8",
        )
        with walker._COORD_INDEX_LOCK:
            walker.COORD_INDEX[mid] = {
                "id": mid,
                "coord": [2, 0, 0],
                "path": str(path),
                "title": mid,
                "type": "concept",
            }
        return path

    return _write


def test_apply_loser_penalty_subtracts_confidence():
    doc = {
        "id": "evid-1",
        "provenance": {"confidence": 0.55},
        "audit": [],
    }
    outcome = apply_loser_penalty(
        doc,
        {"loser_id": "evid-1", "winner_id": "canon-1", "loser_penalty": 0.30},
    )
    assert outcome["applied"] is True
    assert outcome["confidence_before"] == 0.55
    assert outcome["confidence_after"] == pytest.approx(0.25)
    assert doc["provenance"]["confidence"] == pytest.approx(0.25)
    assert doc["audit"][-1]["what"] == "contradiction_adjudicated"


def test_apply_loser_penalty_skips_canonical():
    doc = {
        "id": "canon-1",
        "flags": {"canonical": True},
        "provenance": {"confidence": 0.9},
    }
    outcome = apply_loser_penalty(
        doc,
        {"loser_id": "canon-1", "winner_id": "evid-1", "loser_penalty": 0.30},
    )
    assert outcome["applied"] is False
    assert outcome["reason"] == "canonical_immune"
    assert doc["provenance"]["confidence"] == 0.9


def test_process_contradiction_candidates_adjudicates_group(memory_tree, monkeypatch):
    write = memory_tree
    loser_path = write("evid-1", confidence=0.54)
    write("canon-1", confidence=0.8)
    # Patch loser type so demotion has a visible target (semantic_fact -> concept).
    loser_doc = json.loads(loser_path.read_text(encoding="utf-8"))
    loser_doc["type"] = "semantic_fact"
    loser_path.write_text(json.dumps(loser_doc), encoding="utf-8")

    reason = "contradiction:canonical=canon-1;evidence=evid-1"
    enqueue_deepsleep_candidate("canon-1", [2, 0, 0], reason=reason)
    enqueue_deepsleep_candidate("evid-1", [2, 0, 0], reason=reason)

    report = process_contradiction_candidates()
    assert report["groups_processed"] == 1
    assert report["adjudications"][0]["winner_id"] == "canon-1"
    assert report["adjudications"][0]["loser_id"] == "evid-1"
    assert report["persisted_count"] == 1
    persist = report["adjudications"][0]["persist"]
    assert persist["persisted"] is True
    assert persist["confidence_after"] == pytest.approx(0.24)
    demotion = persist.get("demotion") or {}
    assert demotion.get("demoted") is True
    assert demotion.get("type_after") == "concept"

    updated = json.loads(loser_path.read_text(encoding="utf-8"))
    assert updated["provenance"]["confidence"] == pytest.approx(0.24)
    assert updated["type"] == "concept"
    assert updated["audit"][-1]["what"] == "lifecycle_demoted"
    assert updated["audit"][-2]["what"] == "contradiction_adjudicated"
