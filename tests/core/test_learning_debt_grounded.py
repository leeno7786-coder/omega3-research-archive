"""Slice 5 — grounded learning_debt: real mastery gaps drive it; a fully
mastered substrate RESTS (no baseline floor); penalties only apply atop a gap."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.shared_state import SharedState
from pressure_loop import pressure_field


@dataclass
class _Rec:
    knowledge_mastery: float
    target_mastery: float


def _envelope(*, empty=False, stale=False, low_conf=False):
    return {
        "mastery": {"source": "live", "domains_count": 3, "top_weak": []},
        "learning_debt_flags": {
            "empty_substrate": empty,
            "stale_mastery": stale,
            "low_confidence_recall": low_conf,
        },
    }


def _patch_gaps(monkeypatch, records):
    monkeypatch.setattr(
        "deepsleep_loop.mastery_store.load_mastery",
        lambda *a, **k: records,
    )


def _debt(monkeypatch, records, **env):
    _patch_gaps(monkeypatch, records)
    return pressure_field.read_learning_debt(
        SharedState(), learning_state_envelope=_envelope(**env)
    )


def test_no_gaps_means_zero_debt_no_floor(monkeypatch):
    # every category at/above target -> the mind rests; there is NO floor.
    debt = _debt(monkeypatch, {"a": _Rec(0.8, 0.7), "b": _Rec(0.7, 0.7)})
    assert debt == 0.0


def test_one_open_gap_drives_growing_debt(monkeypatch):
    debt = _debt(monkeypatch, {"a": _Rec(0.1, 0.7)})
    assert debt == pytest.approx(pressure_field.LEARNING_DEBT_BASE_GROWING)


def test_many_gaps_escalate_above_few(monkeypatch):
    debt = _debt(monkeypatch, {f"d{i}": _Rec(0.1, 0.7) for i in range(5)})
    assert debt == pytest.approx(pressure_field.LEARNING_DEBT_BASE_EARLY)


def test_empty_substrate_is_highest(monkeypatch):
    debt = _debt(monkeypatch, {}, empty=True)
    assert debt == pytest.approx(pressure_field.LEARNING_DEBT_BASE_EMPTY)


def test_penalty_does_not_apply_without_a_gap(monkeypatch):
    # stale flag set but every category mastered -> still 0 (no base, no penalty).
    debt = _debt(monkeypatch, {"a": _Rec(0.8, 0.7)}, stale=True)
    assert debt == 0.0


def test_penalty_applies_atop_a_gap(monkeypatch):
    debt = _debt(monkeypatch, {"a": _Rec(0.1, 0.7)}, stale=True)
    assert debt == pytest.approx(
        pressure_field.LEARNING_DEBT_BASE_GROWING + pressure_field.LEARNING_DEBT_PENALTY_STALE
    )


def test_gaps_drive_debt_even_when_envelope_source_unavailable(monkeypatch):
    # The live SI/recall surface is flaky -> the learning_state envelope's
    # mastery.source can be 'unavailable'. Real gaps (a DIRECT mastery read)
    # must still drive debt — otherwise onboarding seeds gaps but the mind never
    # ignites (the live first-wake bug). gap_count must NOT be gated on the
    # envelope source.
    _patch_gaps(
        monkeypatch,
        {"a": _Rec(0.0, 0.7), "b": _Rec(0.0, 0.7), "c": _Rec(0.0, 0.7)},
    )
    env = {"mastery": {"source": "unavailable"}, "learning_debt_flags": {}}
    debt = pressure_field.read_learning_debt(SharedState(), learning_state_envelope=env)
    assert debt == pytest.approx(pressure_field.LEARNING_DEBT_BASE_EARLY)
