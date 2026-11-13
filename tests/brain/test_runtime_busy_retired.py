"""Phase E4 — the P3-S31 runtime-busy "tick freeze" is RETIRED.

Per the operator (2026-06-24): the is_runtime_busy / _runtime_busy_scope
epistemic_emptiness freeze was a deterministic-auto-pressure band-aid and should
have been retired with the auto-driven pressure system. The surface is removed so
it cannot be re-consumed, and read_epistemic_emptiness no longer gates its
refractory counter on runtime inference state.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime.client as rc  # noqa: E402


def test_runtime_busy_surface_is_gone():
    assert not hasattr(rc, "is_runtime_busy")
    assert not hasattr(rc, "_runtime_busy_scope")
    assert not hasattr(rc, "_RUNTIME_BUSY_COUNT")
    assert not hasattr(rc, "_reset_runtime_busy_for_tests")


def test_runtime_init_no_longer_exports_is_runtime_busy():
    import runtime

    assert not hasattr(runtime, "is_runtime_busy")


def test_pressure_field_no_longer_references_runtime_busy():
    src = (ROOT / "pressure_loop" / "pressure_field.py").read_text(encoding="utf-8")
    assert "is_runtime_busy" not in src
