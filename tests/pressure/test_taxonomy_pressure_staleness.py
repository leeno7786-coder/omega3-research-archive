"""Taxonomy advisory staleness decay — introspection must not starve study.

First ungated live boot (2026-07-08): the compiled-graph memory_gap
signal re-fired at a fixed 0.30 every gather (IDLE episodes never
change the graph), permanently out-bidding learning_debt's 0.25
base — 22 onboarding-seeded mastery gaps never got a LEARN episode.

Cure: when the advisory keeps firing off the SAME graph snapshot, its
weight decays per emission after a grace window; a changed snapshot
(new memories compiled — e.g. by the LEARN it yielded to) restores full
weight. Deterministic, self-limiting, and general to any static
advisory signal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pressure_loop.taxonomy_pressure as tp  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state():
    tp._reset_staleness_for_tests()
    yield
    tp._reset_staleness_for_tests()


def _patch_analysis(monkeypatch, snapshot="snap-A"):
    monkeypatch.setattr(
        "memory.taxonomy.pressure_signals.analyze_memory_pressure",
        lambda root=None: {
            "signals": [{"signal": "memory_gap", "recommended_pressure": "memory_gap"}],
            "recommended_pressure": "memory_gap",
            "evidence_event_ids": ["ev-1"],
            "snapshot_hash": snapshot,
        },
    )


def test_full_weight_inside_grace_window(monkeypatch):
    _patch_analysis(monkeypatch)
    for _ in range(3):
        c = tp.taxonomy_pressure_contributor()
        assert c is not None and c.weight == pytest.approx(0.30)


def test_stale_snapshot_decays_below_learning_debt_base(monkeypatch):
    _patch_analysis(monkeypatch)
    weights = [tp.taxonomy_pressure_contributor().weight for _ in range(8)]
    assert weights[0] == pytest.approx(0.30)
    assert weights[3] < 0.30, "decay must begin after the grace window"
    assert weights[-1] < 0.25, (
        "a never-changing advisory must eventually yield to learning_debt "
        f"(0.25 base); got {weights}"
    )
    assert weights[-1] >= tp._STALE_FLOOR


def test_changed_snapshot_restores_full_weight(monkeypatch):
    _patch_analysis(monkeypatch, "snap-A")
    for _ in range(8):
        tp.taxonomy_pressure_contributor()
    _patch_analysis(monkeypatch, "snap-B")  # graph changed — e.g. LEARN compiled
    c = tp.taxonomy_pressure_contributor()
    assert c.weight == pytest.approx(0.30), "fresh snapshot = fresh signal"


def test_decay_never_kills_the_advisory_entirely(monkeypatch):
    _patch_analysis(monkeypatch)
    last = None
    for _ in range(40):
        last = tp.taxonomy_pressure_contributor()
    assert last is not None
    assert last.weight == pytest.approx(tp._STALE_FLOOR)
