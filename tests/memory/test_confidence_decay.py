"""Governed provenance confidence decay at recall."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memory.confidence_decay import (
    apply_recall_confidence_decay,
    cycles_elapsed_since,
    decay_confidence,
)


def test_decay_confidence_applies_after_multiple_cycles():
    after, changed = decay_confidence(0.80, cycles=10.0)
    assert changed is True
    assert after == pytest.approx(0.80 * (0.995**10), rel=1e-6)


def test_decay_confidence_respects_floor():
    after, changed = decay_confidence(0.12, cycles=100.0)
    assert changed is True
    assert after == pytest.approx(0.10)


def test_decay_confidence_no_change_within_one_cycle():
    after, changed = decay_confidence(0.80, cycles=0.5)
    assert changed is False
    assert after == 0.80


def test_cycles_elapsed_since_uses_tick_interval(monkeypatch):
    monkeypatch.setattr(
        "memory.confidence_decay._tick_interval_seconds",
        lambda: 5.0,
    )
    past = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
    assert cycles_elapsed_since(past) == pytest.approx(5.0, rel=1e-3)


@pytest.fixture
def memory_tree(tmp_path, monkeypatch):
    import memory.engine.walker as walker
    import memory.strength_index as si_mod

    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    si_mod._singleton = None  # noqa: SLF001

    class _FakeSI:
        def __init__(self):
            self._records: dict[str, dict] = {}

        def get_record(self, mid: str):
            rec = self._records.get(mid)
            return dict(rec) if rec else None

        def register(self, mid: str, write_strength: float = 0.5, salience=None) -> None:
            self._records.setdefault(mid, {})["write_strength"] = write_strength

        def set_write_strength(self, mid: str, value: float):
            if mid not in self._records:
                return None
            self._records[mid]["write_strength"] = value
            return value

    fake_si = _FakeSI()
    monkeypatch.setattr(si_mod, "ensure_strength_index", lambda: fake_si)

    def _write(mid: str, *, confidence: float = 0.80, canonical: bool = False):
        path = mem_root / f"{mid}.json"
        path.write_text(
            json.dumps(
                {
                    "id": mid,
                    "coord": [2, 0, 0],
                    "type": "concept",
                    "title": mid,
                    "summary": mid,
                    "provenance": {
                        "origin_id": "test",
                        "origin_type": "test",
                        "author": "test",
                        "ingested_at": "2026-01-01T00:00:00+00:00",
                        "confidence": confidence,
                    },
                    "flags": {"canonical": canonical},
                    "audit": [],
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

    def _set_last_access(mid: str, iso: str) -> None:
        fake_si._records[mid] = {"last_access": iso}

    return _write, _set_last_access, fake_si


def test_apply_recall_confidence_decay_persists(memory_tree, monkeypatch):
    """Decay persists on the StrengthIndex only; the canonical file is immutable.

    Mutating provenance.confidence in place would invalidate projection
    receipts and force false recall conflicts — the doc staying byte-stable
    IS the contract (adopted with the strength-index decay path, 2026-08-07).
    """
    write, set_last_access, fake_si = memory_tree
    path = write("2.00.decay1", confidence=0.80)

    past = (datetime.now(timezone.utc) - timedelta(seconds=50)).isoformat()
    set_last_access("2.00.decay1", past)

    monkeypatch.setattr("memory.confidence_decay._tick_interval_seconds", lambda: 5.0)

    result = apply_recall_confidence_decay("2.00.decay1")
    assert result["applied"] is True
    assert result["confidence_before"] == pytest.approx(0.80)
    assert result["confidence_after"] < 0.80
    assert result["store"] == "strength_index"
    assert fake_si._records["2.00.decay1"]["write_strength"] == pytest.approx(
        result["confidence_after"]
    )

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["provenance"]["confidence"] == pytest.approx(0.80)  # untouched
    assert not any(e.get("what") == "confidence_decayed" for e in updated.get("audit") or [])


def test_decay_no_op_when_last_access_unknown(memory_tree, monkeypatch):
    """Decay owns the access clock and never substitutes an intake timestamp.

    After reboot, bulk registration can legitimately have no ``last_access``.
    Even though ``ingested_at`` is observed time, it answers a different question;
    unknown access age means no decay until ``touch()`` seeds the access clock.
    """
    write, _set_last_access, _fake_si = memory_tree  # deliberately do NOT set last_access
    path = write("2.00.nohash", confidence=0.80)

    monkeypatch.setattr("memory.confidence_decay._tick_interval_seconds", lambda: 5.0)

    result = apply_recall_confidence_decay("2.00.nohash")
    assert result["applied"] is False
    assert result.get("confidence") == pytest.approx(0.80)  # untouched, not slammed

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["provenance"]["confidence"] == pytest.approx(0.80)
    # confidence unchanged -> no decay audit entry appended
    assert not any(e.get("what") == "confidence_decayed" for e in updated.get("audit") or [])


def test_apply_recall_confidence_decay_skips_canonical(memory_tree, monkeypatch):
    write, set_last_access, _fake_si = memory_tree
    write("2.00.canon", confidence=0.80, canonical=True)

    past = (datetime.now(timezone.utc) - timedelta(seconds=50)).isoformat()
    set_last_access("2.00.canon", past)
    monkeypatch.setattr("memory.confidence_decay._tick_interval_seconds", lambda: 5.0)

    result = apply_recall_confidence_decay("2.00.canon")
    assert result["applied"] is False
    assert result["reason"] == "canonical_immune"
