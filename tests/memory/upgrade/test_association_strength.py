"""DeepSleep-gated durable association-strength sidecar contracts."""

from __future__ import annotations

import json

import pytest

from memory import association_strength as associations


def _index(tmp_path):
    return associations.AssociationStrengthIndex(tmp_path / "association_strength.json")


def test_association_mutation_is_not_allowed_outside_deepsleep(tmp_path):
    index = _index(tmp_path)
    with pytest.raises(associations.AssociationMutationOutsideDeepSleep):
        index.register(
            "memory-a",
            "CO_LOCATED",
            "memory-b",
            provenance_class="canonical_coactivation",
        )


def test_symmetric_association_identity_is_order_independent(tmp_path):
    index = _index(tmp_path)
    with associations.deep_sleep_mutation():
        first = index.register(
            "memory-a",
            "CO_LOCATED",
            "memory-b",
            provenance_class="canonical_coactivation",
            structural_strength=0.4,
        )
        second = index.register(
            "memory-b",
            "CO_LOCATED",
            "memory-a",
            provenance_class="canonical_coactivation",
            structural_strength=0.9,
        )
    assert first == second
    record = index.get_record(first)
    assert record is not None
    assert record["structural_strength"] == pytest.approx(0.4)


def test_positive_and_negative_credit_only_change_experiential_strength(tmp_path):
    index = _index(tmp_path)
    with associations.deep_sleep_mutation():
        association_id = index.register(
            "memory-a",
            "CO_LOCATED",
            "memory-b",
            provenance_class="canonical_coactivation",
            structural_strength=0.8,
            experiential_strength=0.5,
        )
        index.reinforce(association_id, 0.25, eligibility=0.5, verified=True)
        index.reinforce(association_id, -0.10, eligibility=1.0, verified=True)
    record = index.get_record(association_id)
    assert record is not None
    assert record["structural_strength"] == pytest.approx(0.8)
    assert record["experiential_strength"] == pytest.approx(0.525)
    assert record["positive_credit"] == pytest.approx(0.125)
    assert record["negative_credit"] == pytest.approx(0.10)


def test_unverified_or_unknown_credit_does_not_create_or_change_an_association(tmp_path):
    index = _index(tmp_path)
    with associations.deep_sleep_mutation():
        assert index.reinforce("missing", 0.5, eligibility=1.0, verified=True) is None
        association_id = index.register(
            "memory-a",
            "CO_LOCATED",
            "memory-b",
            provenance_class="canonical_coactivation",
            experiential_strength=0.2,
        )
        assert index.reinforce(association_id, 0.5, eligibility=1.0, verified=False) is None
    assert index.get_record(association_id)["experiential_strength"] == pytest.approx(0.2)


def test_decay_weathers_experience_without_erasing_structural_evidence(tmp_path):
    index = _index(tmp_path)
    with associations.deep_sleep_mutation():
        association_id = index.register(
            "memory-a",
            "CO_LOCATED",
            "memory-b",
            provenance_class="canonical_coactivation",
            structural_strength=0.4,
            experiential_strength=0.5,
            encode_tau=10.0,
        )
        assert index.decay(elapsed_seconds=10.0) == 1
    record = index.get_record(association_id)
    assert record["structural_strength"] == pytest.approx(0.4)
    assert record["experiential_strength"] < 0.5
    assert record["status"] == associations.STATUS_ACTIVE


def test_sidecar_reload_is_durable_and_corruption_fails_closed(tmp_path):
    path = tmp_path / "association_strength.json"
    index = associations.AssociationStrengthIndex(path)
    with associations.deep_sleep_mutation():
        association_id = index.register(
            "memory-a",
            "CO_LOCATED",
            "memory-b",
            provenance_class="canonical_coactivation",
        )
    reloaded = associations.AssociationStrengthIndex(path)
    assert reloaded.get_record(association_id) is not None
    path.write_text("{\"version\":", encoding="utf-8")
    with pytest.raises(associations.AssociationSidecarUnreadable):
        associations.AssociationStrengthIndex(path)


def test_association_generation_is_content_addressed_not_write_timestamped(tmp_path):
    path = tmp_path / "association_strength.json"
    empty_generation = associations.association_generation(path)
    index = _index(tmp_path)
    with associations.deep_sleep_mutation():
        index.register(
            "memory-a",
            "SUPPORTS",
            "memory-b",
            provenance_class=associations.STRUCTURAL_PROVENANCE_CLASS,
            structural_strength=1.0,
            support_count=1,
        )
    populated_generation = associations.association_generation(path)
    assert populated_generation != empty_generation
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = "different-write-time"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert associations.association_generation(path) == populated_generation


def test_structural_edges_collapse_to_one_association_with_support_count(tmp_path):
    from memory.taxonomy.schemas import MemoryEdge

    path = tmp_path / "association_strength.json"
    edges = [
        MemoryEdge(
            edge_id="edge-1",
            source_id="memory-a",
            target_id="memory-b",
            edge_type="SUPPORTS",
            evidence_event_id="event-1",
        ),
        MemoryEdge(
            edge_id="edge-2",
            source_id="memory-a",
            target_id="memory-b",
            edge_type="SUPPORTS",
            evidence_event_id="event-2",
        ),
        MemoryEdge(
            edge_id="edge-2-duplicate",
            source_id="memory-a",
            target_id="memory-b",
            edge_type="SUPPORTS",
            evidence_event_id="event-2",
        ),
    ]

    updates = associations.structural_updates_from_edges(edges)
    assert len(updates) == 1
    assert updates[0]["operation"] == "reconcile_structural"
    assert updates[0]["support_count"] == 2
    assert updates[0]["structural_strength"] == pytest.approx(1.0)

    def monkeypatch_path():
        return path
    # The explicit path keeps this test independent of the operator's state tree.
    original_path = associations.association_path
    associations.association_path = monkeypatch_path
    try:
        result = associations.apply_deepsleep_updates(updates)
    finally:
        associations.association_path = original_path

    assert result["reconciled"] == 1
    association_id = updates[0]["association_id"]
    record = associations.AssociationStrengthIndex(path).get_record(association_id)
    assert record is not None
    assert record["support_count"] == 2
    assert record["structural_strength"] == pytest.approx(1.0)
    assert record["schema_version"] == associations.ASSOCIATION_RECORD_SCHEMA_VERSION


def test_structural_reconciliation_removes_support_without_erasing_experience(tmp_path):
    path = tmp_path / "association_strength.json"
    index = _index(tmp_path)
    with associations.deep_sleep_mutation():
        association_id = index.register(
            "memory-a",
            "SUPPORTS",
            "memory-b",
            provenance_class=associations.STRUCTURAL_PROVENANCE_CLASS,
            structural_strength=1.0,
            support_count=1,
        )
        index.reinforce(association_id, 0.4, eligibility=1.0, verified=True)

    with associations.deep_sleep_mutation():
        index.reconcile_structural(())

    record = index.get_record(association_id)
    assert record is not None
    assert record["support_count"] == 0
    assert record["structural_strength"] == pytest.approx(0.0)
    assert record["experiential_strength"] == pytest.approx(0.4)


def test_deepsleep_update_batch_is_the_only_public_mutation_entry(tmp_path, monkeypatch):
    path = tmp_path / "association_strength.json"
    monkeypatch.setattr(associations, "association_path", lambda: path)
    result = associations.apply_deepsleep_updates(
        [
            {
                "operation": "register",
                "source_id": "memory-a",
                "target_id": "memory-b",
                "relation_type": "CO_LOCATED",
                "provenance_class": "canonical_coactivation",
                "structural_strength": 0.3,
            }
        ]
    )
    assert result["registered"] == 1
    assert result["credited"] == 0
    assert result["skipped"] == []


def test_activation_trace_v2_carries_only_proven_association_rows():
    from memory.dme.activation_trace import (
        DME_ACTIVATION_TRACE_ASSOCIATION_SCHEMA_VERSION,
        DME_ACTIVATION_TRACE_SCHEMA_VERSION,
        ActivatedCandidate,
        build_activation_trace,
    )

    candidate = ActivatedCandidate(
        memory_id="memory-b",
        row_index=1,
        score=0.8,
        evidence_class="strong_hop1",
        plane_id="structural_strong",
        hop_depth=1,
        match_count=1,
        query_count=1,
        attribution_status="diagnostic",
    )
    base = build_activation_trace(
        cycle_id="cycle-1",
        episode_id="episode-1",
        query_text="question",
        projection_generation="generation-1",
        backend="numpy",
        query_coordinates=[(2, 0, 0)],
        candidates=[candidate],
    )
    assert base.schema_version == DME_ACTIVATION_TRACE_SCHEMA_VERSION
    assert base.associations == ()

    associated = build_activation_trace(
        cycle_id="cycle-1",
        episode_id="episode-1",
        query_text="question",
        projection_generation="generation-1",
        backend="numpy",
        query_coordinates=[(2, 0, 0)],
        candidates=[candidate],
        associations=[
            {
                "association_id": "association-1",
                "source_id": "memory-a",
                "target_id": "memory-b",
                "relation_type": "CO_LOCATED",
                "attribution_status": "proven",
            }
        ],
    )
    assert associated.schema_version == DME_ACTIVATION_TRACE_ASSOCIATION_SCHEMA_VERSION
    assert associated.validate() == []


def test_recall_hook_reads_only_active_ids_named_by_the_recall_basis(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from memory.dme.recall_hook import _proven_association_rows

    path = tmp_path / "association_strength.json"
    monkeypatch.setattr(associations, "association_path", lambda: path)
    with associations.deep_sleep_mutation():
        association_id = associations.AssociationStrengthIndex(path).register(
            "memory-a",
            "CO_LOCATED",
            "memory-b",
            provenance_class="canonical_coactivation",
        )
    rows = _proven_association_rows(
        {"memory-b": SimpleNamespace(association_ids=(association_id,))}
    )
    assert len(rows) == 1
    assert rows[0]["association_id"] == association_id
    assert rows[0]["attribution_status"] == "proven"


def test_verified_episode_adapter_ignores_unverified_or_missing_outcomes():
    from types import SimpleNamespace

    episode = SimpleNamespace(
        meta={},
        auto={
            "outputs": {
                "association_outcome": {
                    "verified": False,
                    "relevance": 1.0,
                    "delta": 0.5,
                    "associations": [],
                },
                "remembered_knowledge_tool_results": [
                    {"receipt": {"dme_activation_traces": [{"associations": []}]}}
                ]
            }
        },
    )
    assert associations.updates_from_verified_episode(episode) == []
    episode.auto["outputs"]["association_outcome"] = None
    assert associations.updates_from_verified_episode(episode) == []


def test_verified_use_receipt_keeps_only_verified_ids_and_used_proven_associations():
    results = [
        {
            "verdict": "verified",
            "evidence": [{"memory_id": "memory-a"}, {"memory_id": "memory-unused"}],
            "receipt": {
                "dme_activation_traces": [
                    {
                        "projection_generation": "g1",
                        "associations": [
                            {
                                "association_id": "a1",
                                "source_id": "memory-a",
                                "target_id": "memory-b",
                                "relation_type": "SUPPORTS",
                                "attribution_status": "proven",
                                "activation_strength": 0.8,
                            },
                            {
                                "association_id": "a2",
                                "source_id": "memory-unused",
                                "target_id": "memory-b",
                                "relation_type": "SUPPORTS",
                                "attribution_status": "proven",
                                "activation_strength": 1.0,
                            },
                            {
                                "association_id": "a3",
                                "source_id": "memory-a",
                                "target_id": "memory-b",
                                "relation_type": "SUPPORTS",
                                "attribution_status": "diagnostic",
                                "activation_strength": 1.0,
                            },
                        ],
                    }
                ]
            },
        },
        {"verdict": "unknown", "evidence": [{"memory_id": "memory-b"}]},
    ]

    receipt = associations.build_verified_use_receipt(results, ["memory-a"])

    assert receipt is not None
    assert receipt["schema_version"] == associations.ASSOCIATION_OUTCOME_SCHEMA_VERSION
    assert receipt["verified_memory_ids"] == ["memory-a"]
    assert [row["association_id"] for row in receipt["associations"]] == ["a1"]
    assert receipt["projection_generations"] == ["g1"]


def test_verified_use_receipt_becomes_credit_eligible_only_for_surprising_outcomes():
    receipt = {
        "schema_version": associations.ASSOCIATION_OUTCOME_SCHEMA_VERSION,
        "verified": True,
        "verified_memory_ids": ["memory-a"],
        "associations": [
            {
                "association_id": "a1",
                "activation_strength": 0.5,
            }
        ],
    }

    predicted = associations.finalize_verified_use_receipt(receipt, {"dopamine": 0.1})
    surprising = associations.finalize_verified_use_receipt(receipt, {"dopamine": 0.5})

    assert predicted["verified"] is False
    assert predicted["delta"] == 0.0
    assert surprising["verified"] is True
    assert surprising["delta"] == pytest.approx(0.5)


def test_verified_episode_adapter_emits_only_proven_used_associations():
    from types import SimpleNamespace

    episode = SimpleNamespace(
        meta={},
        auto={
            "outputs": {
                "association_outcome": {
                    "verified": True,
                    "relevance": 0.5,
                    "delta": -0.4,
                    "associations": [
                        {
                            "association_id": "a1",
                            "attribution_status": "proven",
                            "activation_strength": 0.8,
                        },
                        {
                            "association_id": "a2",
                            "attribution_status": "diagnostic",
                            "activation_strength": 1.0,
                        },
                    ],
                },
                "remembered_knowledge_tool_results": [
                    {
                        "receipt": {
                            "dme_activation_traces": [
                                {
                                    "associations": [
                                        {
                                            "association_id": "a1",
                                            "attribution_status": "proven",
                                            "activation_strength": 0.8,
                                        },
                                        {
                                            "association_id": "a2",
                                            "attribution_status": "diagnostic",
                                            "activation_strength": 1.0,
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                ]
            }
        },
    )
    assert associations.updates_from_verified_episode(episode) == [
        {
            "operation": "credit",
            "association_id": "a1",
            "delta": -0.4,
            "eligibility": 0.4,
            "verified": True,
        }
    ]
