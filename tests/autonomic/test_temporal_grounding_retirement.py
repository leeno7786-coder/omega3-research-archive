"""The superseded session-only temporal grounding module stays retired."""

from __future__ import annotations

from pathlib import Path


def test_session_only_temporal_grounding_module_is_absent() -> None:
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "boot.py").exists())
    assert not (root / "autonomic" / "temporal_grounding" / "autonomic_module.py").exists()
