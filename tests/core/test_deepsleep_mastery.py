"""Slice 6b — DeepSleep mastery store, phase, and reseed contributors."""

from __future__ import annotations

import pytest

from deepsleep_loop._deepsleep_state import MasteryRecord
from tests.core.deepsleep_window_fixtures import minimal_input
from deepsleep_loop.mastery_store import (
    load_mastery,
    normalize_domain_id,
    status_for,
    upsert_mastery,
)
from deepsleep_loop.mastery.run import mastery_effects_from_records
from deepsleep_loop.reseed.run import generate_reseed


def _mastery_record(**kwargs) -> MasteryRecord:
    defaults = {
        "domain_id": "python",
        "domain_name": "Python",
        "knowledge_mastery": 0.0,
        "application_mastery": 0.0,
        "confidence": 0.0,
        "evidence_count": 0,
        "subdomains": {},
        "target_mastery": 0.70,
        "status": "learning",
    }
    defaults.update(kwargs)
    return MasteryRecord(**defaults)


# --- B1: MasteryRecord + store ---


def test_mastery_record_round_trip():
    original = _mastery_record(
        knowledge_mastery=0.45,
        application_mastery=0.20,
        evidence_count=3,
        status="developing" if False else "learning",
    )
    restored = MasteryRecord.from_dict(original.to_dict())
    assert restored == original


def test_status_for_thresholds():
    assert status_for(0.0) == "learning"
    assert status_for(0.59) == "learning"
    assert status_for(0.60) == "operational"
    assert status_for(0.79) == "operational"
    assert status_for(0.80) == "mastered"
    assert status_for(1.0) == "mastered"


def test_normalize_domain_id():
    assert normalize_domain_id("Python") == "python"
    assert normalize_domain_id("Quantum Physics") == "quantum-physics"


def test_normalize_domain_id_matches_slug():
    from knowledge_extractor.derive.run import _slug

    samples = [
        "Python",
        "Quantum Physics",
        "Knowledge Acquisition/Domain Mastery",
        "How do transformers work?",
    ]
    for sample in samples:
        assert normalize_domain_id(sample) == _slug(sample)


def test_upsert_mastery_creates_then_updates(tmp_path):
    path = tmp_path / "mastery.json"
    first = _mastery_record(knowledge_mastery=0.10)
    upsert_mastery(first, path=path)

    loaded = load_mastery(path=path)
    assert "python" in loaded
    assert loaded["python"].knowledge_mastery == pytest.approx(0.10)

    updated = _mastery_record(knowledge_mastery=0.20, evidence_count=2)
    upsert_mastery(updated, path=path)
    loaded2 = load_mastery(path=path)
    assert loaded2["python"].knowledge_mastery == pytest.approx(0.20)
    assert loaded2["python"].evidence_count == 2


def test_load_mastery_missing_path_returns_empty(tmp_path):
    missing = tmp_path / "no_mastery.json"
    assert load_mastery(path=missing) == {}


def test_upsert_mastery_bad_path_halts(monkeypatch):
    """Fail-hard: mastery is the single source of truth — a write failure CRASHES,
    a success-credit update must not be silently lost."""

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("deepsleep_loop.mastery_store._resolve_path", _boom)
    with pytest.raises(OSError, match="disk full"):
        upsert_mastery(_mastery_record(), path="ignored")


def test_deepsleep_mastery_policy_loaders():
    from pressure_loop.policy import (
        clear_policy_cache,
        deepsleep_mastery_default_target,
        deepsleep_mastery_path,
    )

    clear_policy_cache()
    assert str(deepsleep_mastery_path()).endswith("mastery.json")
    assert deepsleep_mastery_default_target() == pytest.approx(0.70)


# --- B2: mastery phase (window-level behavior covered in test_deepsleep_window_consolidation.py) ---


def test_mastery_effects_from_records():
    records = [
        _mastery_record(
            knowledge_mastery=0.20,
            application_mastery=0.0,
        )
    ]
    effects = mastery_effects_from_records(records, prior=None)
    assert "python" in effects
    assert effects["python"]["knowledge_delta"] == pytest.approx(0.20)


# --- B3: DeepSleep state cannot manufacture successor cognition ---


@pytest.mark.parametrize(
    "record",
    [
        _mastery_record(knowledge_mastery=0.30, target_mastery=0.70),
        _mastery_record(
            knowledge_mastery=0.70,
            application_mastery=0.30,
            target_mastery=0.70,
            status="operational",
        ),
    ],
)
def test_mastery_maintenance_never_emits_successor_pressure(record):
    """Mastery gaps inform Omega's fold; DeepSleep itself cannot choose to wake."""
    contributors = generate_reseed(minimal_input(), mastery_records=[record])
    assert contributors == []
