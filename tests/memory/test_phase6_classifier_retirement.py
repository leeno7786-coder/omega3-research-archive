"""Phase 6 — classifier retirement + LFM advisory seam."""

from __future__ import annotations

from unittest.mock import patch

from core.paths import OMEGA_ROOT
from memory.routing.resolve_undecidable import UNDECIDABLE, resolve_undecidable


def test_resolve_undecidable_default_is_no_op():
    assert resolve_undecidable("blizzard penguin teapot", {"lobe": 3}) is UNDECIDABLE


def test_retired_classifier_paths_are_absent():
    retired_paths = (
        OMEGA_ROOT / "memory" / "engine" / "train_setfit.py",
        OMEGA_ROOT / "memory" / "engine" / "train_lobe_vm.py",
        OMEGA_ROOT / "memory" / "engine" / "hierarchical_classifier.py",
        OMEGA_ROOT / "memory" / "engine" / "classifier_cache.py",
        OMEGA_ROOT / "runtime" / "setfit",
        OMEGA_ROOT / "scripts" / "setfit_dataset",
    )
    assert all(not path.exists() for path in retired_paths), [
        str(path) for path in retired_paths if path.exists()
    ]


def test_memory_gate_has_no_classifier_helpers():
    from memory.gates.base import MemoryGate

    assert not hasattr(MemoryGate, "_classify")
    assert not hasattr(MemoryGate, "_get_classifier")


def test_undecided_ingest_calls_resolve_undecidable():
    from memory.gates import base as gate_base
    from memory.gates.lobe_gates import ReasoningGate

    calls = []

    def _spy_resolve(text, context):
        calls.append({"text": text, "context": dict(context or {})})
        return UNDECIDABLE

    with (
        patch.object(gate_base, "atomic_write", return_value="<test>/x.json"),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset()),
        patch(
            "memory.routing.resolve_undecidable.resolve_undecidable",
            side_effect=_spy_resolve,
        ),
    ):
        result = ReasoningGate().ingest(
            "blizzard penguin teapot",
            metadata={"type": "concept", "title": "test/reasoning/x"},
        )

    assert result is not None
    assert len(calls) == 1
    assert calls[0]["context"]["lobe"] == 3
    assert calls[0]["context"]["source"] == "lobe:reasoning"
    assert calls[0]["context"]["coord"][1] == 127


def test_undecided_ingest_emits_suggestion_when_lfm_returns():
    from memory.gates import base as gate_base
    from memory.gates.lobe_gates import ReasoningGate
    from memory.routing.resolve_undecidable import UndecidableSuggestion

    emitted = []

    def _capture_emit(event_name, **data):
        emitted.append((event_name, data))

    suggestion = UndecidableSuggestion(
        lobe=3,
        subdomain_y=1,
        label="3.01",
        confidence=0.9,
        rationale="test",
    )

    with (
        patch.object(gate_base, "atomic_write", return_value="<test>/x.json"),
        patch.object(gate_base, "_emit", side_effect=_capture_emit),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset()),
        patch(
            "memory.routing.resolve_undecidable.resolve_undecidable",
            return_value=suggestion,
        ),
    ):
        ReasoningGate().ingest(
            "blizzard penguin teapot",
            metadata={"type": "concept", "title": "test/reasoning/x"},
        )

    suggestion_events = [
        data for event, data in emitted if event == "ingest_undecidable_suggestion"
    ]
    assert len(suggestion_events) == 1
    assert suggestion_events[0]["suggestion_y"] == 1
    assert suggestion_events[0]["label"] == "3.01"
