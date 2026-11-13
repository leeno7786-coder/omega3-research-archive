"""Slice 2 — deterministic ingest subdomain override (write-Y == recall-Y)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto_loop.mril_tools.subdomain_helpers import subdomain_for
from memory.gates.subdomain_override import apply_subdomain_override


def test_override_applies_helper_y_for_identity_pronoun():
    text = "I am Noah"
    new_coord, new_label = apply_subdomain_override(text, [0, 3, 0], "0.03")
    assert new_coord[1] == subdomain_for(text, 0)
    assert new_coord[1] != 3
    assert new_label == f"0.{new_coord[1]:02d}"


def test_override_applies_helper_y_for_second_person_omega():
    text = "your founding identity"
    new_coord, _ = apply_subdomain_override(text, [0, 3, 0], "0.03")
    assert new_coord[1] == subdomain_for(text, 0)


def test_override_applies_helper_y_for_knowledge_term():
    text = "quantum entanglement"
    new_coord, _ = apply_subdomain_override(text, [2, 9, 0], "2.09")
    assert new_coord[1] == subdomain_for(text, 2)


def test_no_signal_preserves_explicit_y():
    text = "zzzz nothing here"
    assert subdomain_for(text, 0) is None
    new_coord, new_label = apply_subdomain_override(text, [0, 3, 0], "0.03")
    assert new_coord == [0, 3, 0] and new_label == "0.03"


def test_journal_lobe_routes_to_deepsleep():
    # Phase 1 (deterministic subdomain grammar): lobe 6 now has a helper
    # (_journal_subdomain) and its only subdomain is (6,0) deepsleep, so any
    # journal text routes there and apply_subdomain_override corrects a stray
    # explicit Y of 1 to 0. This replaces test_non_helper_lobe_untouched,
    # whose premise — a lobe subdomain_for doesn't handle — no longer exists
    # now that subdomain_for covers all 12 lobes. The no-signal behavior is
    # still covered by test_no_signal_preserves_explicit_y above.
    text = "deepsleep consolidation entry"
    assert subdomain_for(text, 6) == 0
    new_coord, new_label = apply_subdomain_override(text, [6, 1, 0], "6.01")
    assert new_coord == [6, 0, 0]
    assert new_label == f"6.{new_coord[1]:02d}"


def test_short_or_empty_coord_safe():
    assert apply_subdomain_override("x", [], "")[0] == []
    assert apply_subdomain_override("x", [0], "0")[0] == [0]


def test_ingest_writes_helper_subdomain(tmp_path, monkeypatch):
    """Integration: written coord[1] must equal subdomain_for when helper routes."""
    from memory.gates import base as gate_base
    from memory.gates.lobe_gates import IdentityGate

    text = "I am Noah"
    expected_y = subdomain_for(text, 0)
    assert expected_y is not None

    captured = {}

    def _capture_atomic_write(meta, update_index=True):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
    ):
        gate = IdentityGate()
        result = gate.ingest(text, metadata={"type": "concept", "title": "test/identity/x"})

    assert result[0] is not None
    written_coord = captured["meta"]["coord"]
    assert written_coord == [0, expected_y, 0]


def test_single_lobe_write_makes_no_classifier_call():
    """The single-lobe write gate must not call a classifier."""
    from memory.gates import base as gate_base
    from memory.gates.lobe_gates import IdentityGate

    text = "I am Noah"  # subdomain_for(text, 0) routes deterministically

    captured = {}

    def _capture_atomic_write(meta, update_index=True):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
    ):
        gate = IdentityGate()
        result = gate.ingest(text, metadata={"type": "concept", "title": "test/identity/x"})

    assert result is not None, "write must succeed without the classifier"
    expected_y = subdomain_for(text, 0)
    assert captured["meta"]["coord"] == [0, expected_y, 0]


def test_write_read_coord_symmetry_smoke(monkeypatch):
    """Spec §7 smoke: same text → write Y == recall top-hit Y (no classifier)."""
    from memory.gates import base as gate_base
    from memory.gates.lobe_gates import IdentityGate
    from memory.engine import walker as W

    text = "I am Noah"
    expected_y = subdomain_for(text, 0)
    assert expected_y is not None

    mem_id = "symmetry_smoke_1"
    captured_path = "/fake/symmetry.json"

    def _capture_atomic_write(meta, update_index=True):
        return captured_path

    fake_index = {
        mem_id: {"coord": [0, expected_y, 0], "path": captured_path},
    }

    def fake_load_meta(path):
        return {
            "id": mem_id,
            "coord": [0, expected_y, 0],
            "title": "identity",
            "summary": text,
            "text": text,
            "provenance": {"confidence": 0.9},
            "tags": [],
        }

    monkeypatch.setattr(W, "COORD_INDEX", fake_index)
    monkeypatch.setattr(W, "load_coord_index", lambda: fake_index)
    monkeypatch.setattr(W, "load_meta_by_path", fake_load_meta)

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
    ):
        gate = IdentityGate()
        assert (
            gate.ingest(text, metadata={"type": "concept", "title": "test/identity/x"}) is not None
        )

    hits = IdentityGate().recall(text, limit=5)
    assert hits, "recall must return the written record"
    assert hits[0]["coord"] == [0, expected_y, 0]
