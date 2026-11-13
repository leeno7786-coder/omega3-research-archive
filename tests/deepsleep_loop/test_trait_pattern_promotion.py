"""Trait observations become preferences only after repeated independent evidence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft7Validator

from deepsleep_loop.identity.run import update_identity
from deepsleep_loop.identity_store import load_identity
from memory.core import build_memory_dict
from tests.core.deepsleep_window_fixtures import window_input

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def _install_canonical_backend(monkeypatch):
    state: dict[str, dict] = {}
    writes: list[dict] = []

    def _snapshot():
        return {
            memory_id: {
                "coord": list(meta.get("coord") or []),
                "path": f"/fake/{memory_id}.json",
            }
            for memory_id, meta in state.items()
        }

    def _load_meta(path: str):
        return dict(state.get(Path(path).stem, {}))

    def _write_memory(envelope, *, propose=True, bypass_guardian=False, **_kwargs):
        memory_id = envelope.id or envelope.payload.get("id")
        meta = {
            "id": memory_id,
            "coord": list(envelope.coord),
            "type": envelope.type,
            **dict(envelope.payload),
        }
        state[memory_id] = meta
        writes.append(
            {
                "id": memory_id,
                "coord": list(envelope.coord),
                "type": envelope.type,
                "payload": dict(envelope.payload),
                "propose": propose,
                "bypass_guardian": bypass_guardian,
            }
        )
        return SimpleNamespace(
            cycle_id=envelope.cycle_id,
            staging_ref=None,
            path=memory_id,
            validation_status="committed",
            message="ok",
        )

    monkeypatch.setattr("memory.engine.walker.load_coord_index", lambda: state)
    monkeypatch.setattr("memory.engine.walker.coord_index_snapshot", _snapshot)
    monkeypatch.setattr("memory.engine.walker.load_meta_by_path", _load_meta)
    monkeypatch.setattr("memory.api.write_memory", _write_memory)
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_identity_promote_evidence",
        lambda policy=None: 2,
        raising=False,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_identity_strength_increment",
        lambda policy=None: 0.10,
    )
    return state, writes


def _episode_pattern(
    episode_id: str,
    *,
    trait: str = "focus_on_definition",
    status: str = "new",
) -> dict:
    return {
        "episode_id": episode_id,
        "omega_result": {
            "identity_patterns": [
                {
                    "trait": trait,
                    "pattern": "I define the key term before exploring implications",
                    "evidence": f"response evidence from {episode_id}",
                    "status": status,
                }
            ]
        },
    }


def _preference_writes(writes: list[dict]) -> list[dict]:
    return [row for row in writes if row["coord"][:2] == [0, 1]]


def _observation_writes(writes: list[dict]) -> list[dict]:
    return [row for row in writes if row["coord"][:2] == [0, 3]]


def test_single_foldback_records_evidence_without_promoting_preference(monkeypatch, tmp_path):
    """Regression: changing the promotion branch back to first sight must fail."""
    _, writes = _install_canonical_backend(monkeypatch)
    ds_input = window_input(
        window_id="window-1",
        episodes=[_episode_pattern("ep-1")],
    )

    updated = update_identity(ds_input, identity_path=tmp_path / "identity.json")

    assert updated == []
    assert load_identity(path=tmp_path / "identity.json") == {}
    assert len(_observation_writes(writes)) == 1
    assert _preference_writes(writes) == []
    assert all(row["propose"] is False and row["bypass_guardian"] is True for row in writes)


def test_two_independent_foldbacks_promote_one_preference(monkeypatch, tmp_path):
    """Regression: failing to count the second independent episode must fail."""
    _, writes = _install_canonical_backend(monkeypatch)
    identity_path = tmp_path / "identity.json"

    update_identity(
        window_input(window_id="window-1", episodes=[_episode_pattern("ep-1")]),
        identity_path=identity_path,
    )
    updated = update_identity(
        window_input(window_id="window-2", episodes=[_episode_pattern("ep-2", status="confirmed")]),
        identity_path=identity_path,
    )

    assert len(updated) == 1
    assert updated[0].name == "focus_on_definition"
    assert updated[0].evidence_count == 2
    assert updated[0].strength == 0.20
    assert len(_observation_writes(writes)) == 2
    assert len(_preference_writes(writes)) == 1
    assert load_identity(identity_path)["focus_on_definition"].evidence_count == 2


def test_retrying_same_foldback_is_idempotent(monkeypatch, tmp_path):
    """Regression: counting a retry as independent evidence must fail."""
    _, writes = _install_canonical_backend(monkeypatch)
    identity_path = tmp_path / "identity.json"
    ds_input = window_input(
        window_id="window-1",
        episodes=[_episode_pattern("ep-1")],
    )

    first = update_identity(ds_input, identity_path=identity_path)
    second = update_identity(ds_input, identity_path=identity_path)

    assert first == []
    assert second == []
    assert load_identity(identity_path) == {}
    assert _preference_writes(writes) == []
    assert {row["id"] for row in _observation_writes(writes)} == {
        _observation_writes(writes)[0]["id"]
    }


def test_two_episode_patterns_in_one_window_are_independent_evidence(monkeypatch, tmp_path):
    """Regression: collapsing all same-trait episode rows to last-seen must fail."""
    _, writes = _install_canonical_backend(monkeypatch)

    updated = update_identity(
        window_input(
            window_id="window-1",
            episodes=[
                _episode_pattern("ep-1"),
                _episode_pattern("ep-2", status="confirmed"),
            ],
        ),
        identity_path=tmp_path / "identity.json",
    )

    assert len(updated) == 1
    assert updated[0].evidence_count == 2
    assert len(_observation_writes(writes)) == 2
    assert len(_preference_writes(writes)) == 1


def test_tension_does_not_count_as_support_for_promotion(monkeypatch, tmp_path):
    """Regression: treating contradictory evidence as confirmation must fail."""
    _, writes = _install_canonical_backend(monkeypatch)
    identity_path = tmp_path / "identity.json"

    update_identity(
        window_input(window_id="window-1", episodes=[_episode_pattern("ep-1")]),
        identity_path=identity_path,
    )
    updated = update_identity(
        window_input(
            window_id="window-2",
            episodes=[_episode_pattern("ep-2", status="tensions_with_prior")],
        ),
        identity_path=identity_path,
    )

    assert updated == []
    assert load_identity(identity_path) == {}
    assert len(_observation_writes(writes)) == 2
    assert _preference_writes(writes) == []


def test_trait_observation_survives_memory_dict_construction():
    observation = {
        "name": "persistent_learner",
        "observation_id": "episode:ep-1:persistent_learner",
        "supporting": True,
    }

    mem = build_memory_dict(
        text="Candidate identity observation",
        coord=[0, 3, 0],
        source_tag="deepsleep.identity_store",
        metadata={
            "type": "trait_observation",
            "trait_observation": observation,
        },
    )

    assert mem["trait_observation"] == observation
    schema_path = Path(__file__).resolve().parents[2] / "schema" / "memory_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert list(Draft7Validator(schema).iter_errors(mem)) == []
