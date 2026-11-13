"""Phase 4 — single-lobe gate lock (no multi-lobe classifier paths)."""

from __future__ import annotations

import pytest

from memory.gates.base import MemoryGate
from memory.gates.lobe_gates import ToolsGate


def test_lobe_gates_module_rejects_multi_lobe_writable_subclass():
    """Production gate subclasses in lobe_gates must declare at most one writable lobe."""
    with pytest.raises(TypeError, match="WRITABLE_LOBES"):
        type(
            "BadWritableGate",
            (MemoryGate,),
            {
                "__module__": "memory.gates.lobe_gates",
                "READABLE_LOBES": [0],
                "WRITABLE_LOBES": [0, 1],
                "SOURCE_TAG": "gate:bad",
            },
        )


def test_lobe_gates_module_rejects_multi_lobe_readable_subclass():
    with pytest.raises(TypeError, match="READABLE_LOBES"):
        type(
            "BadReadableGate",
            (MemoryGate,),
            {
                "__module__": "memory.gates.lobe_gates",
                "READABLE_LOBES": [0, 2],
                "WRITABLE_LOBES": [0],
                "SOURCE_TAG": "gate:bad",
            },
        )


def test_ad_hoc_multi_lobe_gate_ingest_rejected():
    """Test-only multi-lobe gates outside lobe_gates are rejected at ingest, not import."""

    class _MultiWriteGate(MemoryGate):
        READABLE_LOBES = [2]
        WRITABLE_LOBES = [0, 2]
        SOURCE_TAG = "test:multi"

    result = _MultiWriteGate().ingest("some text", metadata={"title": "test/x"})
    assert result is None


def test_ad_hoc_multi_lobe_gate_recall_rejected():
    """Ad-hoc multi-readable gates return [] at recall without calling the classifier."""

    class _MultiReadGate(MemoryGate):
        READABLE_LOBES = [0, 2]
        WRITABLE_LOBES = []
        SOURCE_TAG = "test:multi_read"

    hits = _MultiReadGate().recall("some query", limit=5)
    assert hits == []


def test_gate_recall_makes_no_classifier_call(monkeypatch):
    """Phase 4/6: gate.recall is deterministic; no SetFit on recall path."""
    from memory.engine import walker as W

    fake_index = {
        "tool_1": {"coord": [5, 0, 0], "path": "/fake/tool.json"},
        "other": {"coord": [2, 0, 0], "path": "/fake/other.json"},
    }

    def fake_load_meta(path):
        if "tool" in path:
            return {
                "id": "tool_1",
                "coord": [5, 0, 0],
                "title": "web_fetch",
                "summary": "web fetch tool schema catalog",
                "text": "",
                "provenance": {"confidence": 0.9},
                "tags": [],
            }
        return {
            "id": "other",
            "coord": [2, 0, 0],
            "title": "other",
            "summary": "unrelated",
            "text": "",
            "provenance": {"confidence": 0.9},
            "tags": [],
        }

    monkeypatch.setattr(W, "COORD_INDEX", fake_index)
    monkeypatch.setattr(W, "load_coord_index", lambda: fake_index)
    monkeypatch.setattr(W, "load_meta_by_path", fake_load_meta)

    hits = ToolsGate().recall("web fetch tool schema catalog", limit=5)

    assert hits, "ToolsGate.recall must return lobe-5 hits without the classifier"
    assert all(h.get("coord", [None])[0] == 5 for h in hits)
