"""
tests/memory/test_strength_index_reinforce.py

Phase-2 Task 4 — per-engram RPE choice-credit via reinforce(mid, delta).

Construction: fresh StrengthIndex instance via init_strength_index() with a
tmp path, matching the pattern used in test_strength_index_review_fixes.py.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.strength_index import init_strength_index  # noqa: E402


def _fresh():
    """Return a StrengthIndex backed by a fresh temp file (auto-cleaned)."""
    td = tempfile.mkdtemp()
    return init_strength_index(Path(td) / "strength_index.json")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reinforce_boosts_then_clamps_high():
    si = _fresh()
    si.register("m1", write_strength=0.5)
    assert si.reinforce("m1", 0.3) == pytest.approx(0.8)
    assert si.reinforce("m1", 0.5) == pytest.approx(1.0)  # clamp high
    assert si._records["m1"]["write_strength"] == pytest.approx(1.0)


def test_reinforce_demotes_then_clamps_low():
    si = _fresh()
    si.register("m2", write_strength=0.4)
    assert si.reinforce("m2", -0.1) == pytest.approx(0.3)
    assert si.reinforce("m2", -2.0) == pytest.approx(0.0)  # clamp low
    assert si._records["m2"]["write_strength"] == pytest.approx(0.0)


def test_reinforce_unknown_mid_is_noop_returns_none():
    si = _fresh()
    assert si.reinforce("ghost", 0.5) is None
    assert "ghost" not in si._records  # NOT lazy-created (unlike touch)


def test_reinforce_empty_mid_is_noop():
    si = _fresh()
    assert si.reinforce("", 0.5) is None
