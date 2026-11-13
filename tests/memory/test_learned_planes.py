"""Learned association planes are disposable, deterministic projection state."""

from __future__ import annotations

import numpy as np
import pytest

from memory.association_strength import association_id_for
from memory.dme.lobe_layout import HEADER_WORDS
from memory.dme.packer import has_coordinate, pack_entry


def _record(source, target, relation, *, structural, experiential, provenance="learned_experiential"):
    association_id = association_id_for(source, relation, target, provenance)
    return association_id, {
        "association_id": association_id,
        "source_id": source,
        "target_id": target,
        "relation_type": relation,
        "provenance_class": provenance,
        "structural_strength": structural,
        "experiential_strength": experiential,
        "status": "active",
    }


def test_learned_planes_split_active_nonstructural_associations_by_effective_strength():
    from memory.dme.learned_planes import build_learned_planes

    coords = [(2, 0, 1), (2, 0, 2), (2, 0, 3), (2, 0, 4)]
    sram = np.vstack(
        [pack_entry(i, 0.0, "user", [coord]) for i, coord in enumerate(coords, start=1)]
    )
    row_index = [{"memory_id": mid} for mid in ("a", "b", "c", "d")]

    strong_id, strong = _record("a", "b", "SUPPORTS", structural=0.8, experiential=0.8)
    medium_id, medium = _record("c", "b", "SUPPORTS", structural=0.5, experiential=0.5)
    weak_id, weak = _record("c", "d", "CO_LOCATED", structural=0.25, experiential=0.25)
    structural_id, structural = _record(
        "a", "d", "SUPPORTS", structural=1.0, experiential=1.0, provenance="structural"
    )
    before = sram.copy()

    planes = build_learned_planes(
        sram,
        row_index,
        {strong_id: strong, medium_id: medium, weak_id: weak, structural_id: structural},
        association_generation="a" * 64,
    )

    assert has_coordinate(planes.strong[1], coords[0])
    assert not has_coordinate(planes.strong[1], coords[2])
    assert has_coordinate(planes.medium[1], coords[2])
    assert has_coordinate(planes.weak[2], coords[3])
    assert has_coordinate(planes.weak[3], coords[2])
    assert not has_coordinate(planes.weak[3], coords[0])
    assert np.array_equal(sram, before)
    assert np.all(planes.strong[:, :HEADER_WORDS] == 0)
    assert np.all(planes.medium[:, :HEADER_WORDS] == 0)
    assert np.all(planes.weak[:, :HEADER_WORDS] == 0)
    assert planes.association_generation == "a" * 64
    assert planes.projection_config_hash


def test_learned_planes_reject_active_associations_with_missing_endpoints():
    from memory.dme.learned_planes import LearnedPlaneError, build_learned_planes

    association_id, record = _record(
        "missing", "b", "SUPPORTS", structural=1.0, experiential=1.0
    )
    sram = np.vstack([pack_entry(1, 0.0, "user", [(2, 0, 1)])])

    with pytest.raises(LearnedPlaneError, match="missing endpoint"):
        build_learned_planes(
            sram,
            [{"memory_id": "b"}],
            {association_id: record},
            association_generation="b" * 64,
        )


def test_learned_plane_config_hash_changes_with_threshold_policy():
    from memory.dme.learned_planes import LearnedPlaneThresholds, projection_config_hash_for

    first = projection_config_hash_for(LearnedPlaneThresholds())
    second = projection_config_hash_for(LearnedPlaneThresholds(strong=0.8))
    assert first != second


def test_learned_plane_recall_is_opt_in_and_returns_tiered_hits(monkeypatch):
    from memory.dme.learned_planes import (
        build_learned_planes,
        learned_planes_enabled,
        search_learned_planes,
    )

    monkeypatch.delenv("OMEGA_DME_LEARNED_PLANES", raising=False)
    assert learned_planes_enabled() is False
    monkeypatch.setenv("OMEGA_DME_LEARNED_PLANES", "1")
    assert learned_planes_enabled() is True

    coords = [(2, 0, 1), (2, 0, 2)]
    sram = np.vstack(
        [pack_entry(i, 0.0, "user", [coord]) for i, coord in enumerate(coords, start=1)]
    )
    row_index = [{"memory_id": "source"}, {"memory_id": "target"}]
    association_id, record = _record(
        "source", "target", "SUPPORTS", structural=0.8, experiential=0.8
    )
    planes = build_learned_planes(
        sram,
        row_index,
        {association_id: record},
        association_generation="c" * 64,
    )

    hits = search_learned_planes(
        sram,
        row_index,
        planes,
        [coords[0]],
    )

    assert [(hit.memory_id, hit.edge_class) for hit in hits] == [("target", "learned_strong")]


def test_learned_plane_top_k_receipt_names_discarded_hits():
    from memory.dme.learned_planes import build_learned_planes, search_learned_planes

    coords = [(2, 0, 1), (2, 0, 2), (2, 0, 3)]
    sram = np.vstack(
        [pack_entry(i, 0.0, "user", [coord]) for i, coord in enumerate(coords, start=1)]
    )
    row_index = [{"memory_id": memory_id} for memory_id in ("source", "target-a", "target-b")]
    first_id, first = _record("source", "target-a", "SUPPORTS", structural=0.8, experiential=0.8)
    second_id, second = _record("source", "target-b", "SUPPORTS", structural=0.8, experiential=0.8)
    planes = build_learned_planes(
        sram,
        row_index,
        {first_id: first, second_id: second},
        association_generation="d" * 64,
    )
    receipts = []

    hits = search_learned_planes(
        sram,
        row_index,
        planes,
        [coords[0]],
        max_results=1,
        truncation_receipts=receipts,
    )

    assert len(hits) == 1
    assert receipts[0]["stage"] == "dme_learned_planes:max_results"
    assert receipts[0]["input_count"] == 2
    assert receipts[0]["output_count"] == 1
    assert receipts[0]["dropped_candidate_ids"] == ["target-b"]


def test_opt_in_dme_session_loads_generation_bound_learned_projection(tmp_path, monkeypatch):
    from memory import association_strength
    from memory.dme.recall_hook import dme_recall_session, force_unload_all_sessions
    from tests.memory.test_recall_basis import _seed_store

    _seed_store(tmp_path)
    sidecar_path = tmp_path / "association_strength.json"
    monkeypatch.setattr(association_strength, "association_path", lambda: sidecar_path)
    monkeypatch.setenv("OMEGA_DME_LEARNED_PLANES", "1")
    with association_strength.deep_sleep_mutation():
        association_strength.AssociationStrengthIndex(sidecar_path).register(
            "k1",
            "SUPPORTS",
            "k3",
            provenance_class="learned_experiential",
            structural_strength=0.8,
            experiential_strength=0.8,
            support_count=0,
        )

    with dme_recall_session(tmp_path) as session:
        assert session.learned_planes is not None
        assert session.learned_planes.association_generation == association_strength.association_generation(
            sidecar_path
        )
        assert any(record["source_id"] == "k1" for record in session.learned_records.values())
    force_unload_all_sessions()
