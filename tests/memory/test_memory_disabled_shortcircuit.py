"""confidence_decay + demotion recall entrypoints short-circuit when memory is disabled.

§7-P1-3(a). Only ``memory/symphony_strength.py``'s disabled path was pinned. The
decay (``memory/confidence_decay.apply_recall_confidence_decay``) and demotion
(``memory/demotion.apply_recall_sustained_floor_demotion``) recall entrypoints ALSO
short-circuit via ``core.memory_config.memory_enabled()`` — but in BOTH directions
nothing pinned them.

These tests assert BOTH directions against the SAME on-disk fixture so the ONLY
variable is the ``enabled`` flag:

* DISABLED  -> the entrypoint returns ``{"reason": "memory_disabled"}`` and performs
  NO state mutation: the on-disk document is byte-for-byte identical, even though the
  fixture is deliberately constructed so a decay / demotion WOULD fire if reached.
  (Pins the skip fail-mode explicitly — the gate is what blocks the write.)
* ENABLED   -> the SAME fixture DOES mutate: decay rewrites a lower confidence + an
  audit entry; sustained-floor demotion drops the lifecycle ``type`` and rewrites.

``memory_enabled`` is imported into each consuming module's namespace
(``from core.memory_config import ... memory_enabled``), so the live gate reads the
name bound in ``confidence_decay`` / ``demotion`` — that is the name these tests patch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memory import confidence_decay, demotion


# Confidence below the demotion floor (0.20) so the sustained-floor counter
# would advance if the disabled gate did not short-circuit first, but ABOVE the
# decay floor (confidence_floor_no_recall = 0.10) so decay can actually move it
# downward (a value <= the decay floor is already clamped -> changed=False).
_BELOW_FLOOR_CONFIDENCE = 0.15
# A last_access far enough in the past that many whole pressure cycles have
# elapsed -> decay_confidence(changed=True) AND advance_below_floor_cycles past
# the sustained_floor_cycles threshold (default 5) in a single recall.
_STALE_LAST_ACCESS = "2000-01-01T00:00:00+00:00"


def _write_doc(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _make_doc(memory_id: str) -> dict[str, Any]:
    # A NON-canonical promoted memory (type "semantic" -> demotable) whose
    # confidence sits below the floor and whose last access is ancient.
    return {
        "id": memory_id,
        "type": "semantic",
        "flags": {"canonical": False},
        "provenance": {
            "confidence": _BELOW_FLOOR_CONFIDENCE,
            "ingested_at": _STALE_LAST_ACCESS,
        },
        "audit": [],
    }


class _StubStrengthIndex:
    """Minimal StrengthIndex stand-in.

    last_access is ancient so cycles_elapsed_since() returns a large value.
    advance_below_floor_cycles() returns a count already over the demotion
    threshold so the ENABLED demotion path reaches an actual lifecycle demote.
    """

    def __init__(self) -> None:
        self.advanced = False
        self.reset = False
        self.write_strength: float | None = None

    def register(self, _mid: str, write_strength: float = 0.5, salience=None) -> None:
        self.write_strength = write_strength

    def set_write_strength(self, _mid: str, value: float):
        self.write_strength = value
        return value

    def get_record(self, _mid: str) -> dict[str, Any]:
        return {"last_access": _STALE_LAST_ACCESS, "below_floor_cycles": 0}

    def advance_below_floor_cycles(self, _mid: str, **_kw: Any) -> int:
        self.advanced = True
        return 999  # well past sustained_floor_cycles (default 5)

    def reset_below_floor_cycles(self, _mid: str) -> None:
        self.reset = True


@pytest.fixture()
def memory_doc(tmp_path, monkeypatch):
    """A real on-disk memory document + a strength index stub, wired so the
    decay/demotion entrypoints resolve and (if enabled) mutate it.

    Returns (memory_id, path, read_doc) where read_doc() reloads from disk.
    """
    memory_id = "9.99.shortcircuit_probe"
    path = tmp_path / f"{memory_id}.json"
    _write_doc(path, _make_doc(memory_id))

    # Resolve the id to our temp doc in BOTH modules' walker import sites.
    def _path_for_id(idstr: str):
        return str(path) if idstr == memory_id else None

    monkeypatch.setattr("memory.engine.walker.path_for_id", _path_for_id, raising=False)

    stub = _StubStrengthIndex()
    monkeypatch.setattr("memory.strength_index.ensure_strength_index", lambda: stub, raising=False)

    def _read_doc() -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    return memory_id, path, _read_doc, stub


# ---------------------------------------------------------------------------
# DISABLED direction — short-circuit, NO state mutation (pin the skip fail-mode)
# ---------------------------------------------------------------------------


def test_decay_disabled_short_circuits_no_mutation(memory_doc, monkeypatch):
    memory_id, path, read_doc, _stub = memory_doc
    before_bytes = path.read_bytes()

    # The live gate reads the name bound INSIDE confidence_decay.
    monkeypatch.setattr(confidence_decay, "memory_enabled", lambda: False)

    result = confidence_decay.apply_recall_confidence_decay(memory_id)

    # Fail-mode is explicit and attributable to the disabled gate.
    assert result["applied"] is False
    assert result["reason"] == "memory_disabled"
    # NO state mutation: the document on disk is byte-for-byte identical even
    # though the fixture WOULD have decayed (stale last_access + changed=True).
    assert path.read_bytes() == before_bytes
    doc = read_doc()
    assert doc["provenance"]["confidence"] == _BELOW_FLOOR_CONFIDENCE
    assert doc["audit"] == []


def test_demotion_disabled_short_circuits_no_mutation(memory_doc, monkeypatch):
    memory_id, path, read_doc, stub = memory_doc
    before_bytes = path.read_bytes()

    monkeypatch.setattr(demotion, "memory_enabled", lambda: False)

    result = demotion.apply_recall_sustained_floor_demotion(memory_id)

    assert result["demoted"] is False
    assert result["reason"] == "memory_disabled"
    # NO state mutation: type still "semantic", no audit, and the disabled gate
    # short-circuits BEFORE the strength index is even consulted.
    assert path.read_bytes() == before_bytes
    doc = read_doc()
    assert doc["type"] == "semantic"
    assert doc["audit"] == []
    assert stub.advanced is False


# ---------------------------------------------------------------------------
# ENABLED direction — the SAME fixture DOES act (proves the gate is the cause)
# ---------------------------------------------------------------------------


def test_decay_enabled_mutates_confidence(memory_doc, monkeypatch):
    memory_id, path, read_doc, _stub = memory_doc

    monkeypatch.setattr(confidence_decay, "memory_enabled", lambda: True)

    result = confidence_decay.apply_recall_confidence_decay(memory_id)

    # The decay path ran to completion and persisted on the StrengthIndex.
    # The canonical file stays byte-stable (receipt-immutability contract).
    assert result["applied"] is True
    assert result["confidence_after"] < result["confidence_before"]
    assert result["store"] == "strength_index"
    assert _stub.write_strength == pytest.approx(result["confidence_after"])
    doc = read_doc()
    assert doc["provenance"]["confidence"] == pytest.approx(result["confidence_before"])
    assert not any(entry.get("what") == "confidence_decayed" for entry in doc["audit"])


def test_demotion_enabled_mutates_lifecycle_type(memory_doc, monkeypatch):
    memory_id, path, read_doc, stub = memory_doc

    monkeypatch.setattr(demotion, "memory_enabled", lambda: True)

    result = demotion.apply_recall_sustained_floor_demotion(memory_id)

    # The demotion path ran: strength index consulted AND the lifecycle type
    # dropped one stage (semantic -> concept) with an audit trail.
    assert stub.advanced is True
    assert result["demoted"] is True
    assert result["reason"] == "sustained_confidence_floor"
    doc = read_doc()
    assert doc["type"] == "concept"  # _DEMOTED_TYPE[rank=2]
    assert any(entry.get("what") == "lifecycle_demoted" for entry in doc["audit"])
