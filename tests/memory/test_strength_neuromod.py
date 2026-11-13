"""Slice 3 B2/B3 — strength_index register salience + per-memory encode_tau."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autonomic import symphony_neuromod as sn  # noqa: E402
from memory.strength_index import DECAY_TAU_SECONDS, StrengthIndex  # noqa: E402


def _fresh():
    td = tempfile.mkdtemp()
    return StrengthIndex(Path(td) / "strength_index.json")


def test_register_scales_write_strength_with_salience():
    sn._CURRENT_MEMORY_SALIENCE = None
    idx = _fresh()
    idx.register("m1", 0.4, salience={"scale": 1.5, "tau_factor": 1.0})
    assert idx.get_record("m1")["write_strength"] == pytest.approx(0.6)
    idx.register("m2", 0.4)
    assert idx.get_record("m2")["write_strength"] == pytest.approx(0.4)


def test_register_stores_encode_tau():
    idx = _fresh()
    idx.register("m3", 0.5, salience={"scale": 1.0, "tau_factor": 0.5})
    assert idx.get_record("m3")["encode_tau"] == pytest.approx(DECAY_TAU_SECONDS * 0.5)


def test_register_baseline_no_salience():
    sn._CURRENT_MEMORY_SALIENCE = None
    idx = _fresh()
    idx.register("m4", 0.5)
    rec = idx.get_record("m4")
    assert rec["write_strength"] == pytest.approx(0.5)
    assert rec["encode_tau"] == pytest.approx(DECAY_TAU_SECONDS)


def test_get_strength_decays_faster_under_short_tau():
    idx = _fresh()
    idx.register("m_base", 0.8, salience={"scale": 1.0, "tau_factor": 1.0})
    idx.register("m_stress", 0.8, salience={"scale": 1.0, "tau_factor": 0.5})

    past = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    for mid in ("m_base", "m_stress"):
        idx._records[mid]["last_access"] = past
        idx._records[mid]["ingested_at"] = past

    assert idx.get_strength("m_stress") < idx.get_strength("m_base")
