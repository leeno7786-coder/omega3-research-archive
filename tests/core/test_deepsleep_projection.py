"""Slice 6d B2 — DeepSleep prompt projection (read-only)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from deepsleep_loop._deepsleep_state import (
    MasteryRecord,
    ProjectionState,
    Trait,
    TrajectoryRecord,
)
from deepsleep_loop.identity_store import load_identity, upsert_trait
from deepsleep_loop.mastery_store import upsert_mastery
from deepsleep_loop.projection.run import build_projection
from deepsleep_loop.projection_store import read_projection
from deepsleep_loop.trajectory_store import upsert_trajectory


@pytest.fixture(autouse=True)
def _isolate_memory_root(tmp_path, monkeypatch):
    """Use real canonical encoding and receipts in an isolated memory profile."""
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)


@pytest.fixture(autouse=True)
def _isolate_canonical_memory_for_every_suite(_isolate_memory_root):
    """Override the root fixture too, so neither parent can install an OK-only stub."""


def test_projection_state_round_trip():
    original = ProjectionState(
        identity_projection={"traits": [{"name": "persistent_learner", "strength": 0.3}]},
        mastery_projection={
            "quantum_physics": {"knowledge": 0.4, "application": 0.2, "status": "learning"}
        },
        trajectory_projection={"target_domain": "quantum_physics", "status": "active"},
        memory_integrity_projection={"status": "ok", "issue_count": 0},
    )
    restored = ProjectionState.from_dict(original.to_dict())
    assert restored == original


def test_build_projection_reads_stores_without_mutation(tmp_path, monkeypatch):
    from core.paths import MEMORY_ROOT
    from memory.engine import walker
    from memory.episode_sram import close_bucket, open_bucket
    from memory.projection_receipt import receipt_path, verify_projection_receipt

    identity_path = tmp_path / "identity.json"
    mastery_path = tmp_path / "mastery.json"
    trajectory_path = tmp_path / "trajectory.json"
    projection_path = tmp_path / "projection.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_identity_path",
        lambda policy=None: identity_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda policy=None: mastery_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_trajectory_path",
        lambda policy=None: trajectory_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_projection_path",
        lambda policy=None: projection_path,
    )

    open_bucket("ep-projection-read", pipeline_id=0)
    upsert_trait(
        Trait(name="persistent learner", strength=0.35, evidence_count=2),
        path=identity_path,
    )
    upsert_mastery(
        MasteryRecord(
            domain_id="quantum_physics",
            domain_name="Quantum Physics",
            knowledge_mastery=0.45,
            application_mastery=0.25,
            status="learning",
        ),
        path=mastery_path,
    )
    upsert_trajectory(
        TrajectoryRecord(
            target_domain="quantum_physics",
            target_capability="foundational understanding",
            confidence=0.8,
            status="active",
        ),
        path=trajectory_path,
    )
    close_bucket("ep-projection-read")

    walker.load_coord_index()
    trait_entries = [
        (memory_id, entry) for memory_id, entry in walker.coord_index_snapshot().items()
        if list(entry.get("coord") or [])[:2] == [0, 1]
    ]
    assert len(trait_entries) == 1
    trait_id, trait_entry = trait_entries[0]
    resolved_trait_path = walker.resolve_coord_index_path(trait_entry["path"])
    assert resolved_trait_path is not None
    trait_path = Path(resolved_trait_path)
    canonical = json.loads(trait_path.read_text(encoding="utf-8"))
    proof = verify_projection_receipt(canonical, root=MEMORY_ROOT)
    assert proof.ok, proof.reason
    canonical_before = {
        path: path.read_bytes()
        for path in (trait_path, walker.INDEX_PATH, receipt_path(trait_id, root=MEMORY_ROOT))
    }

    identity_before = copy.deepcopy(load_identity(path=identity_path))
    mastery_before = copy.deepcopy(mastery_path.read_text(encoding="utf-8"))
    trajectory_before = copy.deepcopy(trajectory_path.read_text(encoding="utf-8"))

    state = build_projection()

    assert isinstance(state, ProjectionState)
    assert state.identity_projection.get("traits")
    assert "quantum_physics" in state.mastery_projection
    assert state.mastery_projection["quantum_physics"]["knowledge"] == 0.45
    assert state.trajectory_projection.get("target_domain") == "quantum_physics"

    assert load_identity(path=identity_path) == identity_before
    for path, before in canonical_before.items():
        assert path.read_bytes() == before, f"projection mutated canonical input: {path}"
    assert not identity_path.exists()
    assert mastery_path.read_text(encoding="utf-8") == mastery_before
    assert trajectory_path.read_text(encoding="utf-8") == trajectory_before

    persisted = read_projection(path=projection_path)
    assert persisted.to_dict() == state.to_dict()
