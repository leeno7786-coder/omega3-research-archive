"""The association sidecar is written only by the real DeepSleep terminus."""

from __future__ import annotations

from deepsleep_loop.loop import run_deepsleep_for_episode
from memory import association_strength, episode_sram
from tests.core.deepsleep_window_fixtures import (
    active_store_from_episode_body,
    episode_entry,
    patch_window_deepsleep_paths,
)


def test_deepsleep_applies_explicit_association_updates(tmp_path, monkeypatch):
    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    association_path = tmp_path / "guardian" / "state" / "association_strength.json"
    monkeypatch.setattr(association_strength, "association_path", lambda: association_path)

    store, episode = active_store_from_episode_body(
        episode_entry("ep-association-deepsleep", intent="IDLE", target="Python")
    )
    episode.meta["association_updates"] = [
        {
            "operation": "register",
            "source_id": "memory-a",
            "target_id": "memory-b",
            "relation_type": "CO_LOCATED",
            "provenance_class": "canonical_coactivation",
            "structural_strength": 0.25,
        }
    ]
    with association_strength.deep_sleep_mutation():
        association_id = association_strength.AssociationStrengthIndex(association_path).register(
            "memory-a",
            "CO_LOCATED",
            "memory-b",
            provenance_class="canonical_coactivation",
        )
    episode.meta.pop("association_updates")
    episode.auto["outputs"]["association_outcome"] = {
        "schema_version": association_strength.ASSOCIATION_OUTCOME_SCHEMA_VERSION,
        "verified": True,
        "relevance": 1.0,
        "delta": 0.2,
        "associations": [],
    }
    episode.auto["outputs"]["association_outcome"]["associations"] = [
        {
            "association_id": association_id,
            "attribution_status": "proven",
            "activation_strength": 0.5,
        }
    ]
    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)

    outcome = run_deepsleep_for_episode(store)

    assert outcome is not None
    assert outcome.projection_state["association_plasticity"]["credited"] == 1
    index = association_strength.AssociationStrengthIndex(association_path)
    assert index.get_record(association_id)["experiential_strength"] == 0.1


def test_structural_reconciliation_is_deepsleep_owned_and_uses_compiler_edges(tmp_path, monkeypatch):
    from deepsleep_loop import loop as deepsleep_loop
    from memory.taxonomy.schemas import MemoryEdge

    association_path = tmp_path / "guardian" / "state" / "association_strength.json"
    monkeypatch.setattr(association_strength, "association_path", lambda: association_path)
    monkeypatch.setattr(
        "memory.taxonomy.compiler.compile_structural_edges",
        lambda root: [
            MemoryEdge(
                edge_id="edge-structural",
                source_id="memory-a",
                target_id="memory-b",
                edge_type="SUPPORTS",
                evidence_event_id="event-structural",
            )
        ],
    )

    result = deepsleep_loop._reconcile_structural_associations(root=tmp_path)

    assert result["reconciled"] == 1
    association_id = association_strength.association_id_for(
        "memory-a",
        "SUPPORTS",
        "memory-b",
        association_strength.STRUCTURAL_PROVENANCE_CLASS,
    )
    record = association_strength.AssociationStrengthIndex(association_path).get_record(association_id)
    assert record is not None
    assert record["support_count"] == 1


def test_deepsleep_association_consolidation_precedes_program0_publication(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from deepsleep_loop import loop as deepsleep_loop
    from memory.projection.dispatch import DispatchReport

    events: list[str] = []
    monkeypatch.setattr("core.paths.MEMORY_ROOT", tmp_path / "memory", raising=False)
    monkeypatch.setattr(
        "memory.association_strength.apply_deepsleep_updates",
        lambda _updates: events.append("explicit_association_updates")
        or {"reconciled": 0, "retired": 0},
    )
    monkeypatch.setattr(
        "deepsleep_loop.loop._reconcile_structural_associations",
        lambda _root: events.append("structural_edges")
        or {"reconciled": 1, "retired": 0},
    )
    monkeypatch.setattr(
        "memory.source_validity.rebuild_from_canonical",
        lambda _root: events.append("source_validity")
        or {"objects": 0, "revisions": 0},
    )
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda _root, *, reason: events.append("program0_publication")
        or DispatchReport(
            owner="program0",
            action="published",
            publication=None,
            drain_status="complete",
            generation_id="generation-1",
        ),
    )

    closing_episode = SimpleNamespace(
        meta={
            "association_updates": [
                {"operation": "record", "source_id": "memory-a", "target_id": "memory-b"}
            ]
        }
    )
    deepsleep_loop._maintain_projection_inputs_after_deepsleep(
        context_id="episode-order",
        scope="episode",
        before_projection=lambda active_root: deepsleep_loop._consolidate_associations(
            closing_episode,
            active_root,
        ),
    )
    report = deepsleep_loop._publish_projection_after_deepsleep(
        context_id="episode-order",
        scope="episode",
    )

    assert events == [
        "explicit_association_updates",
        "structural_edges",
        "source_validity",
        "program0_publication",
    ]
    assert report.owner == "program0"
    assert report.generation_id == "generation-1"
