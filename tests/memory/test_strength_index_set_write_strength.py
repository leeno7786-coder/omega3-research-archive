"""Unit tests for StrengthIndex.set_write_strength (GAP-2 — the forgetting
/ decay demotion primitive).

set_write_strength(mid, value) authoritatively overwrites an EXISTING
engram's write_strength to ``value`` (clamped [0,1]); no-op (None) for an
unknown mid; never lazy-creates. This is the method the DeepSleep phase-3
reconciliation forgetting path uses to demote — register() would NO-OP on
an already-tracked mid (the long-standing dead-decay bug this fixes).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.strength_index import StrengthIndex  # noqa: E402


def _fresh():
    """Return a TRULY independent StrengthIndex per test.

    NOTE: do NOT use init_strength_index() here — it returns the shared
    process singleton (idempotent), so every test would share one record
    set and pollute each other (a reused mid in one test would leak into
    the next). Construct the class directly with a temp file for real
    per-test isolation."""
    td = tempfile.mkdtemp()
    return StrengthIndex(Path(td) / "strength_index.json")


def _ws(idx, mid):
    return idx._records[mid]["write_strength"]


def test_set_overwrites_existing_engram():
    idx = _fresh()
    idx.register("m1", write_strength=0.8)
    new = idx.set_write_strength("m1", 0.4)
    assert new is not None
    assert abs(new - 0.4) < 1e-9
    assert abs(_ws(idx, "m1") - 0.4) < 1e-9


def test_set_is_authoritative_where_register_no_ops():
    # register() NO-OPs on an existing mid; set_write_strength must NOT —
    # this is the exact bug that made DeepSleep forgetting a silent no-op.
    idx = _fresh()
    idx.register("m1", write_strength=0.8)
    idx.register("m1", write_strength=0.1)  # no-op (existing) -> stays 0.8
    assert abs(_ws(idx, "m1") - 0.8) < 1e-9
    idx.set_write_strength("m1", 0.1)  # authoritative overwrite
    assert abs(_ws(idx, "m1") - 0.1) < 1e-9


def test_set_clamps_high():
    idx = _fresh()
    idx.register("m1", write_strength=0.5)
    assert abs(idx.set_write_strength("m1", 1.5) - 1.0) < 1e-9


def test_set_clamps_low():
    idx = _fresh()
    idx.register("m1", write_strength=0.5)
    assert abs(idx.set_write_strength("m1", -0.5) - 0.0) < 1e-9


def test_set_unknown_mid_is_noop():
    idx = _fresh()
    assert idx.set_write_strength("ghost", 0.3) is None
    assert "ghost" not in idx._records  # NOT lazy-created (unlike touch)


def test_set_empty_mid_is_noop():
    idx = _fresh()
    assert idx.set_write_strength("", 0.3) is None
