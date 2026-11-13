"""N3 — strength_index.register consumes the LIVE salience latch (RED-first)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autonomic.symphony_neuromod import (  # noqa: E402
    compute_episode_neuromod,
    set_current_memory_salience,
)
from memory.strength_index import DECAY_TAU_SECONDS, StrengthIndex  # noqa: E402


def _fresh_index() -> StrengthIndex:
    td = tempfile.mkdtemp()
    return StrengthIndex(Path(td) / "strength_index.json")


@pytest.fixture(autouse=True)
def _reset_latch():
    set_current_memory_salience(None)
    yield
    set_current_memory_salience(None)


def test_register_scales_with_high_reward_latch():
    set_current_memory_salience(
        {"dopamine": 1.0, "plasticity": 1.0, "stress": 0.0, "serotonin": 0.0}
    )
    idx = _fresh_index()
    idx.register("m1", write_strength=0.4)
    assert idx.get_record("m1")["write_strength"] > 0.4


def test_register_shortens_tau_under_high_stress_latch():
    set_current_memory_salience(
        {"dopamine": 0.0, "plasticity": 0.0, "stress": 1.0, "serotonin": 0.0}
    )
    idx = _fresh_index()
    idx.register("m2", write_strength=0.4)
    assert idx.get_record("m2")["encode_tau"] < DECAY_TAU_SECONDS


def test_register_baseline_when_latch_none():
    idx = _fresh_index()
    idx.register("m3", write_strength=0.4)
    rec = idx.get_record("m3")
    assert rec["write_strength"] == pytest.approx(0.4)
    assert rec["encode_tau"] == pytest.approx(DECAY_TAU_SECONDS)


def test_anti_bleed_fail_latch_replaces_success_latch():
    set_current_memory_salience(
        {"dopamine": 1.0, "plasticity": 1.0, "stress": 0.0, "serotonin": 0.0}
    )
    success_scale = 2.0  # 1 + 0.6 + 0.4

    fail_nm = compute_episode_neuromod({"cycle_kind": "fail"}, 1.0)
    set_current_memory_salience(fail_nm)

    idx = _fresh_index()
    idx.register("m4", write_strength=0.4)
    got = idx.get_record("m4")["write_strength"]
    assert got == pytest.approx(0.4 * (1.0 + 0.4 * fail_nm["plasticity"]))
    assert got < 0.4 * success_scale
