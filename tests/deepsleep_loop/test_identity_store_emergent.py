"""Emergent-traits reader over canonical identity-lobe memory."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepsleep_loop._deepsleep_state import Trait
from deepsleep_loop.identity_store import (
    emergent_traits,
    load_identity,
    normalize_trait_name,
    upsert_trait,
)

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def _trait_meta(
    name: str,
    *,
    strength: float,
    evidence_count: int,
    coord: list[int] | None = None,
    memory_id: str | None = None,
) -> dict:
    trait_name = normalize_trait_name(name)
    return {
        "id": memory_id or f"trait.{trait_name}",
        "coord": list(coord or [0, 2, 0]),
        "type": "emergent_trait",
        "title": f"Identity Trait - {trait_name}",
        "summary": f"{trait_name} trait",
        "text": f"Trait: {trait_name}",
        "trait": {
            "name": trait_name,
            "strength": float(strength),
            "evidence_count": int(evidence_count),
        },
    }


def _install_fake_canonical_backend(monkeypatch, *, initial: list[dict] | None = None):
    state = {meta["id"]: dict(meta) for meta in (initial or [])}
    writes: list[dict] = []

    def _snapshot():
        return {
            mid: {"coord": list(meta.get("coord") or []), "path": f"/fake/{mid}.json"}
            for mid, meta in state.items()
        }

    def _load_meta(path: str):
        return dict(state.get(Path(path).stem, {}))

    def _write_memory(envelope, *, propose=True, bypass_guardian=False, **kwargs):
        memory_id = envelope.id or envelope.payload.get("id")
        meta = {
            "id": memory_id,
            "coord": list(envelope.coord),
            "type": envelope.type,
            "title": envelope.payload.get("title"),
            "summary": envelope.payload.get("summary"),
            "text": envelope.payload.get("text"),
            "trait": dict(envelope.payload.get("trait") or {}),
        }
        state[memory_id] = meta
        writes.append(
            {
                "id": memory_id,
                "coord": list(envelope.coord),
                "type": envelope.type,
                "propose": propose,
                "bypass_guardian": bypass_guardian,
                "payload": dict(envelope.payload),
                "cycle_id": envelope.cycle_id,
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
    return state, writes


def test_load_identity_reads_canonical_trait_records(monkeypatch, tmp_path):
    _install_fake_canonical_backend(
        monkeypatch,
        initial=[
            _trait_meta("curious", strength=0.8, evidence_count=5),
            _trait_meta("history_only", strength=0.2, evidence_count=9, coord=[0, 3, 0]),
        ],
    )

    loaded = load_identity(path=tmp_path / "legacy_identity.json")

    assert list(loaded) == ["curious"]
    assert loaded["curious"] == Trait("curious", strength=0.8, evidence_count=5)


def test_load_identity_hides_pre_migration_one_off_trait(monkeypatch, tmp_path):
    _install_fake_canonical_backend(
        monkeypatch,
        initial=[_trait_meta("fleeting", strength=0.9, evidence_count=1)],
    )

    assert load_identity(path=tmp_path / "legacy_identity.json") == {}


def test_upsert_trait_writes_stable_canonical_emergent_trait_record(monkeypatch, tmp_path):
    _, writes = _install_fake_canonical_backend(monkeypatch)
    legacy_path = tmp_path / "identity.json"

    trait = Trait("persistent_learner", strength=0.2, evidence_count=2)
    upsert_trait(trait, path=legacy_path)
    upsert_trait(trait, path=legacy_path)

    assert len(writes) == 2
    first, second = writes
    assert first["coord"] == [0, 1, 0]
    assert first["type"] == "emergent_trait"
    assert first["propose"] is False
    assert first["bypass_guardian"] is True
    assert first["payload"]["trait"] == {
        "name": "persistent_learner",
        "strength": pytest.approx(0.2),
        "evidence_count": 2,
        "evidence": "",  # post-chat reflection metadata (empty for a window-emergent trait)
        "status": "",
        "promoted": True,
    }
    assert first["id"] == second["id"]


def test_upsert_trait_uses_filesystem_safe_deterministic_id(monkeypatch, tmp_path):
    _, writes = _install_fake_canonical_backend(monkeypatch)

    upsert_trait(
        Trait("careful/listener:mode", strength=0.4, evidence_count=3),
        path=tmp_path / "identity.json",
    )

    assert len(writes) == 1
    assert "/" not in writes[0]["id"]
    assert ":" not in writes[0]["id"]
    assert writes[0]["id"].startswith("trait.")


def test_load_identity_imports_legacy_json_once_when_canonical_empty(monkeypatch, tmp_path):
    _, writes = _install_fake_canonical_backend(monkeypatch)
    legacy_path = tmp_path / "identity.json"
    legacy_path.write_text(
        json.dumps(
            {
                "persistent_learner": {
                    "name": "persistent_learner",
                    "strength": 0.3,
                    "evidence_count": 4,
                }
            }
        ),
        encoding="utf-8",
    )

    first = load_identity(path=legacy_path)
    second = load_identity(path=legacy_path)

    assert first["persistent_learner"] == Trait(
        "persistent_learner", strength=0.3, evidence_count=4
    )
    assert second["persistent_learner"] == first["persistent_learner"]
    assert len(writes) == 1


def test_emergent_traits_filters_by_evidence_and_ranks_by_strength(monkeypatch, tmp_path):
    _install_fake_canonical_backend(
        monkeypatch,
        initial=[
            _trait_meta("curious", strength=0.8, evidence_count=5),
            _trait_meta("direct", strength=0.5, evidence_count=3),
            _trait_meta("one_off", strength=0.9, evidence_count=1),
        ],
    )

    out = emergent_traits(min_evidence=2, path=tmp_path / "legacy_identity.json")
    names = [t.name for t in out]

    assert names == ["curious", "direct"]


def test_emergent_traits_empty_store(monkeypatch, tmp_path):
    _install_fake_canonical_backend(monkeypatch)
    assert emergent_traits(min_evidence=2, path=tmp_path / "missing.json") == []
