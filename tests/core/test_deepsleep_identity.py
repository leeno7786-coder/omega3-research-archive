"""Slice 6c B1 — DeepSleep identity store, phase, and Identity Safety Law."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from deepsleep_loop._deepsleep_state import Trait
from memory import episode_sram
from tests.core.deepsleep_window_fixtures import window_input
from deepsleep_loop.identity.run import identity_effects_from_traits, update_identity
from deepsleep_loop.identity_store import load_identity, normalize_trait_name, upsert_trait


@pytest.fixture(autouse=True)
def _isolate_memory_root(tmp_path, monkeypatch):
    """Use real canonical encoding and receipts in an isolated memory profile."""
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)


@pytest.fixture(autouse=True)
def _isolate_canonical_memory_for_every_suite(_isolate_memory_root):
    """Override the root fixture too, so neither parent can install an OK-only stub."""


def _trait(**kwargs) -> Trait:
    defaults = {"name": "persistent_learner", "strength": 0.0, "evidence_count": 0}
    defaults.update(kwargs)
    return Trait(**defaults)


def _record_canonical_writes(monkeypatch):
    """Observe the real writer; canonical files, receipts and revision links stay real."""
    from memory import api

    real_write_memory = api.write_memory
    writes: list[dict] = []

    def _write_memory(envelope, *, propose=True, bypass_guardian=False, **kwargs):
        receipt = real_write_memory(
            envelope, propose=propose, bypass_guardian=bypass_guardian, **kwargs,
        )
        writes.append(
            {
                "id": receipt.path,
                "coord": list(envelope.coord),
                "type": envelope.type,
                "propose": propose,
                "bypass_guardian": bypass_guardian,
                "cycle_id": envelope.cycle_id,
            }
        )
        return receipt

    monkeypatch.setattr(api, "write_memory", _write_memory)
    return writes


def test_trait_round_trip():
    original = _trait(strength=0.45, evidence_count=3)
    restored = Trait.from_dict(original.to_dict())
    assert restored == original


def test_normalize_trait_name():
    assert normalize_trait_name("Persistent Learner") == "persistent_learner"
    assert normalize_trait_name("LEARN on physics") == "learn_on_physics"


def test_upsert_established_trait_creates_then_strengthens(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    first = _trait(strength=0.20, evidence_count=2)
    episode_sram.open_bucket("ep-trait-first", pipeline_id=0)
    upsert_trait(first, path=path)
    episode_sram.close_bucket("ep-trait-first")

    loaded = load_identity(path=path)
    assert "persistent_learner" in loaded
    assert loaded["persistent_learner"].strength == pytest.approx(0.20)

    updated = _trait(strength=0.30, evidence_count=3)
    episode_sram.open_bucket("ep-trait-updated", pipeline_id=0)
    upsert_trait(updated, path=path)
    episode_sram.close_bucket("ep-trait-updated")
    loaded2 = load_identity(path=path)
    assert loaded2["persistent_learner"].strength == pytest.approx(0.30)
    assert loaded2["persistent_learner"].evidence_count == 3
    assert not path.exists()


def test_load_identity_missing_path_returns_empty(tmp_path, monkeypatch):
    assert load_identity(path=tmp_path / "missing.json") == {}


def _omega_with_observation(behavior: str) -> dict:
    return {
        "reflection": {
            "reasoning": {"who": "persistent learner"},
            "emergent_observations": {
                "repeated_behaviors": [behavior],
                "stable_patterns": [],
                "unresolved_patterns": [],
            },
        },
        "trajectory_update": {},
    }


def test_repeated_observation_promotes_then_strengthens_trait(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_identity_strength_increment",
        lambda policy=None: 0.10,
    )

    ds_input = window_input(
        window_id="ep-1",
        reflection=_omega_with_observation("persistent learner").get("reflection"),
    )

    episode_sram.open_bucket("ep-1", pipeline_id=0)
    traits1 = update_identity(ds_input, identity_path=path)
    episode_sram.close_bucket("ep-1")
    assert traits1 == []
    assert load_identity(path=path) == {}

    ds_input2 = window_input(
        window_id="ep-2",
        reflection=_omega_with_observation("persistent learner").get("reflection"),
    )
    episode_sram.open_bucket("ep-2", pipeline_id=0)
    traits2 = update_identity(ds_input2, identity_path=path)
    episode_sram.close_bucket("ep-2")
    assert traits2[0].name == "persistent_learner"
    assert traits2[0].strength == pytest.approx(0.20)
    assert traits2[0].evidence_count == 2

    loaded = load_identity(path=path)
    assert loaded["persistent_learner"].evidence_count == 2
    assert not path.exists()


def test_identity_safety_law_writes_only_identity_store(tmp_path, monkeypatch):
    """Identity phase must NOT write pressure, mastery, or recall stores."""
    writes = _record_canonical_writes(monkeypatch)
    identity_path = tmp_path / "identity.json"
    mastery_path = tmp_path / "mastery.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_identity_strength_increment",
        lambda policy=None: 0.10,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda policy=None: mastery_path,
    )

    mastery_writes: list[str] = []
    pressure_writes: list[str] = []

    original_upsert_mastery = None
    from deepsleep_loop import mastery_store

    original_upsert_mastery = mastery_store.upsert_mastery

    def _track_mastery(*args, **kwargs):
        mastery_writes.append("mastery")
        return original_upsert_mastery(*args, **kwargs)

    monkeypatch.setattr("deepsleep_loop.mastery_store.upsert_mastery", _track_mastery)

    with patch("pressure_loop.pressure_field.reset_pressure_accumulator") as mock_pressure:
        mock_pressure.side_effect = lambda: pressure_writes.append("pressure")

        ds_input = window_input(
            window_id="ep-safe",
            episodes=[
                {
                    "episode_id": "ep-safe",
                    "intent": "LEARN",
                    "outcome": {
                        "goal": {"intent": "LEARN", "target": "Python"},
                        "outcome": {"goal_state": "completed"},
                    },
                }
            ],
            reflection=_omega_with_observation("persistent learner").get("reflection"),
        )
        episode_sram.open_bucket("ep-safe", pipeline_id=0)
        update_identity(ds_input, identity_path=identity_path)
        episode_sram.close_bucket("ep-safe")

    assert not identity_path.exists()
    assert writes, "identity updates must write canonical lobe-0 observations"
    assert all(call["coord"] == [0, 3, 0] for call in writes)
    assert all(call["propose"] is False and call["bypass_guardian"] is True for call in writes)
    assert mastery_writes == []
    assert pressure_writes == []


def test_identity_effects_from_traits():
    traits = [_trait(strength=0.30, evidence_count=2)]
    effects = identity_effects_from_traits(traits, prior=None)
    assert "persistent_learner" in effects
    assert effects["persistent_learner"]["strength_delta"] == pytest.approx(0.30)


def test_deepsleep_identity_policy_loaders():
    from pressure_loop.policy import (
        clear_policy_cache,
        deepsleep_identity_path,
        deepsleep_identity_promote_evidence,
    )

    clear_policy_cache()
    # Legacy import path only; canonical preferences are lobe 0/1.
    assert str(deepsleep_identity_path()).endswith("identity.json")
    assert deepsleep_identity_promote_evidence() == 2
