"""Sustained below-floor confidence demotion (config/memory.yaml demotion section)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from memory.demotion import (
    apply_recall_sustained_floor_demotion,
    compute_next_below_floor_cycles,
    sustained_floor_demotion_due,
)


def test_compute_next_below_floor_cycles_resets_above_floor():
    assert compute_next_below_floor_cycles(4, confidence=0.25, cycles=10.0, floor=0.20) == 0


def test_compute_next_below_floor_cycles_accumulates_below_floor():
    assert compute_next_below_floor_cycles(2, confidence=0.15, cycles=5.0, floor=0.20) == 7


def test_compute_next_below_floor_cycles_no_advance_within_one_cycle():
    assert compute_next_below_floor_cycles(2, confidence=0.15, cycles=0.5, floor=0.20) == 2


def test_sustained_floor_demotion_due_at_threshold():
    assert sustained_floor_demotion_due(5, threshold=5) is True
    assert sustained_floor_demotion_due(4, threshold=5) is False


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
            self._dirty = 0

        def get_record(self, mid: str):
            rec = self._records.get(mid)
            return dict(rec) if rec else None

        def advance_below_floor_cycles(
            self,
            mid: str,
            *,
            confidence: float,
            cycles: float,
            floor: float,
        ) -> int:
            from memory.demotion import compute_next_below_floor_cycles

            rec = self._records.setdefault(mid, {})
            current = int(rec.get("below_floor_cycles") or 0)
            next_val = compute_next_below_floor_cycles(
                current,
                confidence=confidence,
                cycles=cycles,
                floor=floor,
            )
            rec["below_floor_cycles"] = next_val
            self._dirty += 1
            return next_val

        def reset_below_floor_cycles(self, mid: str) -> None:
            rec = self._records.get(mid)
            if rec is not None:
                rec["below_floor_cycles"] = 0
                self._dirty += 1

    fake_si = _FakeSI()
    monkeypatch.setattr(si_mod, "ensure_strength_index", lambda: fake_si)

    def _write(mid: str, *, confidence: float = 0.15, memory_type: str = "semantic_fact"):
        path = mem_root / f"{mid}.json"
        path.write_text(
            json.dumps(
                {
                    "id": mid,
                    "coord": [2, 0, 0],
                    "type": memory_type,
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
                "type": memory_type,
            }
        return path

    def _set_last_access(mid: str, iso: str, *, below_floor_cycles: int = 0) -> None:
        fake_si._records[mid] = {
            "last_access": iso,
            "below_floor_cycles": below_floor_cycles,
        }

    return _write, _set_last_access, fake_si


def test_apply_recall_sustained_floor_demotion_after_threshold(memory_tree, monkeypatch):
    write, set_last_access, fake_si = memory_tree
    path = write("2.00.floor1", confidence=0.15)

    past = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
    set_last_access("2.00.floor1", past, below_floor_cycles=0)

    monkeypatch.setattr("memory.demotion._tick_interval_seconds", lambda: 5.0)
    monkeypatch.setattr(
        "memory.engine.validate_write.atomic_write",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("test fallback")),
    )

    result = apply_recall_sustained_floor_demotion("2.00.floor1")
    assert result["demoted"] is True
    assert result["reason"] == "sustained_confidence_floor"
    assert result["below_floor_cycles_before"] == 0
    assert result["trigger_cycles"] == 5
    assert result["below_floor_cycles_after"] == 0

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["type"] == "concept"
    assert updated["audit"][-1]["what"] == "lifecycle_demoted"
    assert fake_si._records["2.00.floor1"]["below_floor_cycles"] == 0


def test_apply_recall_sustained_floor_no_demotion_below_threshold(memory_tree, monkeypatch):
    write, set_last_access, fake_si = memory_tree
    write("2.00.floor2", confidence=0.15)

    past = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
    set_last_access("2.00.floor2", past, below_floor_cycles=0)

    monkeypatch.setattr("memory.demotion._tick_interval_seconds", lambda: 5.0)

    result = apply_recall_sustained_floor_demotion("2.00.floor2")
    assert result["demoted"] is False
    assert result["below_floor_cycles_after"] == 3
    assert fake_si._records["2.00.floor2"]["below_floor_cycles"] == 3


def test_demotion_no_op_when_last_access_unknown(memory_tree, monkeypatch):
    """Demotion owns access age and never substitutes observed intake time.

    Bulk registration can legitimately have no ``last_access``. Unknown access age
    means zero accrued cycles until recall seeds that clock.
    """
    write, _set_last_access, fake_si = memory_tree  # deliberately do NOT set last_access
    path = write("2.00.nohash_dem", confidence=0.15)

    monkeypatch.setattr("memory.demotion._tick_interval_seconds", lambda: 5.0)

    result = apply_recall_sustained_floor_demotion("2.00.nohash_dem")
    assert result["demoted"] is False
    assert result["below_floor_cycles_after"] == 0

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["type"] == "semantic_fact"  # not demoted to "concept"


def test_apply_recall_sustained_floor_skips_canonical(memory_tree, monkeypatch):
    import memory.engine.walker as walker

    write, set_last_access, _fake_si = memory_tree
    path = write("2.00.canon", confidence=0.15)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["flags"] = {"canonical": True}
    path.write_text(json.dumps(doc), encoding="utf-8")

    past = (datetime.now(timezone.utc) - timedelta(seconds=50)).isoformat()
    set_last_access("2.00.canon", past, below_floor_cycles=10)

    monkeypatch.setattr("memory.demotion._tick_interval_seconds", lambda: 5.0)

    result = apply_recall_sustained_floor_demotion("2.00.canon")
    assert result["demoted"] is False
    assert result["reason"] == "canonical_immune"
