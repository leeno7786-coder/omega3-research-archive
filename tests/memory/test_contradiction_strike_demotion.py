"""Second contradiction strike demotes even above confidence floor."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from memory.contradiction_resolution import apply_loser_penalty, persist_adjudication_outcome


@pytest.fixture
def memory_tree(tmp_path, monkeypatch):
    import memory.engine.walker as walker

    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)

    def _write(mid: str, *, confidence: float = 0.80, audit=None):
        path = mem_root / f"{mid}.json"
        path.write_text(
            json.dumps(
                {
                    "id": mid,
                    "coord": [2, 0, 0],
                    "type": "semantic_fact",
                    "title": mid,
                    "summary": mid,
                    "provenance": {
                        "origin_id": "test",
                        "origin_type": "test",
                        "author": "test",
                        "ingested_at": "2026-01-01T00:00:00+00:00",
                        "confidence": confidence,
                    },
                    "flags": {},
                    "audit": list(audit or []),
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
                "type": "semantic_fact",
            }
        return path

    return _write


def test_second_strike_demotes_without_confidence_floor(memory_tree, monkeypatch):
    write = memory_tree
    prior_strike = {
        "when": datetime.now(timezone.utc).isoformat(),
        "what": "contradiction_adjudicated",
        "by": "test",
        "notes": "seed strike",
    }
    path = write("2.00.strike2", confidence=0.80, audit=[prior_strike])

    monkeypatch.setattr(
        "memory.engine.validate_write.atomic_write",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("test fallback")),
    )

    adjudication = {
        "loser_id": "2.00.strike2",
        "winner_id": "2.00.winner",
        "loser_penalty": 0.30,
    }
    result = persist_adjudication_outcome(adjudication)
    assert result["persisted"] is True
    assert result["confidence_after"] == pytest.approx(0.50)
    demotion = result.get("demotion") or {}
    assert demotion.get("demoted") is True
    assert demotion.get("reason") == "contradiction_strike_threshold"
    assert demotion.get("type_after") == "concept"

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["type"] == "concept"
    assert sum(1 for e in updated["audit"] if e.get("what") == "contradiction_adjudicated") == 2


def test_first_strike_no_demotion_when_confidence_stays_high(memory_tree):
    write = memory_tree
    doc_path = write("2.00.once", confidence=0.80)
    doc = json.loads(doc_path.read_text(encoding="utf-8"))

    mutation = apply_loser_penalty(
        doc,
        {"loser_id": "2.00.once", "winner_id": "2.00.win", "loser_penalty": 0.30},
    )
    assert mutation["confidence_after"] == pytest.approx(0.50)
    assert mutation.get("demotion_eligible") is False
    from memory.demotion import demotion_trigger_after_contradiction

    should, _ = demotion_trigger_after_contradiction(
        doc, confidence_after=mutation["confidence_after"]
    )
    assert should is False
