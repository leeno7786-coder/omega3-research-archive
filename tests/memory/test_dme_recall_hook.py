"""DME recall hook: load once, run merge, and retain the hot MatMul seat."""

from __future__ import annotations

import json
from pathlib import Path

import memory.taxonomy.graph_store as G
from memory.api import QuerySpec
from memory.dme.adapter import rebuild_sram_from_graph
from memory.dme.recall_hook import (
    apply_dme_recall_hook,
    dme_recall_session,
    dme_session_refcount,
    force_unload_all_sessions,
    is_dme_resident,
    last_recall_hook_stats,
)
from memory.taxonomy.compiler import compile_memory_graph


def _write_record(tmp_path: Path, memory_id: str, *, title: str, summary: str) -> None:
    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    from memory.source_ledger.store import record_source

    artifact = record_source(
        f"{title}. {summary}",
        source_type="operator_chat",
        origin_id=f"cycle-{memory_id}",
        root=tmp_path,
    )
    prov = artifact.citation()
    record = {
        "id": memory_id,
        "coord": [2, 0, int(memory_id[-1])],
        "type": "concept",
        "title": title,
        "summary": summary,
        "domain": "biology",
        "tags": ["biology"],
        "provenance": prov,
        "enrichment": {"canonical": True, "status": "active"},
    }
    (memory_root / f"{memory_id}.json").write_text(json.dumps(record), encoding="utf-8")
    from memory.projection_receipt import write_projection_receipt

    write_projection_receipt(record, root=tmp_path)
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    index[memory_id] = {"coord": record["coord"], "path": f"memory/{memory_id}.json"}
    index_path.write_text(json.dumps(index), encoding="utf-8")


def _seed(tmp_path: Path) -> None:
    _write_record(
        tmp_path,
        "k1",
        title="Neuron",
        summary="Neurons pass signals through synapses.",
    )
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)
    force_unload_all_sessions()


def test_recall_hook_loads_runs_and_stays_hot(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    assert is_dme_resident(tmp_path) is False

    query = QuerySpec(
        cycle_id="hook-1",
        text="neuron synapses",
        allowed_lobes=[2],
        denied_lobes=[],
    )
    merged, stats = apply_dme_recall_hook(query, [], root=tmp_path)

    assert stats.dme_ran is True
    assert stats.dme_loaded is True
    assert stats.dme_seat_count == 1
    assert stats.dme_unloaded is False
    assert stats.dme_resident_after is True
    assert stats.dme_geometry_compatible is True
    assert stats.dme_query_dimension == 261_952
    assert stats.dme_weight_dimension == 261_952
    assert stats.dme_uses_embeddings is False
    assert is_dme_resident(tmp_path) is True
    assert any(str(item.get("id") or "") == "k1" for item in merged)
    assert last_recall_hook_stats().dme_seat_count == 1
    receipt_stages = {
        receipt["stage"] for receipt in stats.to_meta()["dme_truncation_receipts"]
    }
    assert "dme_hop_search:max_results" in receipt_stages
    assert "dme_adapter:filters_and_candidate_limit" in receipt_stages


def test_nested_session_refcount_keeps_resident_after_outer_exit(tmp_path: Path) -> None:
    _seed(tmp_path)
    with dme_recall_session(tmp_path) as outer:
        assert outer.seat_count == 1
        assert dme_session_refcount(tmp_path) == 1
        assert is_dme_resident(tmp_path) is True
        with dme_recall_session(tmp_path) as inner:
            assert inner.seat_count == 1
            assert dme_session_refcount(tmp_path) == 2
            assert is_dme_resident(tmp_path) is True
        assert dme_session_refcount(tmp_path) == 1
        assert is_dme_resident(tmp_path) is True
    assert dme_session_refcount(tmp_path) == 0
    assert is_dme_resident(tmp_path) is True
    force_unload_all_sessions()
    assert is_dme_resident(tmp_path) is False


def test_empty_graph_rebuilds_to_zero_seats_without_corrupt_error(tmp_path: Path) -> None:
    # No leaves — load/rebuild yields an empty but valid hot index.
    taxonomy = tmp_path / "memory" / "taxonomy_store" / "compiled"
    taxonomy.mkdir(parents=True, exist_ok=True)
    (taxonomy / "latest_snapshot_hash.txt").write_text("empty-snap", encoding="utf-8")
    conn = G.connect(root=tmp_path)
    conn.close()

    query = QuerySpec(cycle_id="hook-empty", text="nothing", allowed_lobes=[2], denied_lobes=[])
    merged, stats = apply_dme_recall_hook(query, [], root=tmp_path)
    assert stats.dme_ran is True
    assert stats.dme_seat_count == 0
    assert merged == []
    assert is_dme_resident(tmp_path) is True
    force_unload_all_sessions()
    assert is_dme_resident(tmp_path) is False


def test_concurrent_nested_hook_does_not_double_corrupt(tmp_path: Path) -> None:
    _seed(tmp_path)
    query = QuerySpec(
        cycle_id="hook-nested",
        text="neuron synapses",
        allowed_lobes=[2],
        denied_lobes=[],
    )
    with dme_recall_session(tmp_path):
        merged, stats = apply_dme_recall_hook(query, [], root=tmp_path)
        # Nested under outer session: still ran, still resident after inner exit.
        assert stats.dme_ran is True
        assert stats.dme_seat_count == 1
        assert is_dme_resident(tmp_path) is True
        assert any(str(item.get("id") or "") == "k1" for item in merged)
    assert is_dme_resident(tmp_path) is True
    force_unload_all_sessions()
    assert is_dme_resident(tmp_path) is False


def test_rebuild_marks_in_flight_session_stale_without_nulling(tmp_path: Path) -> None:
    _seed(tmp_path)
    with dme_recall_session(tmp_path) as session:
        assert is_dme_resident(tmp_path) is True
        assert session.index is not None
        seats_before = session.seat_count
        rebuild_sram_from_graph(root=tmp_path)
        # In-flight holder keeps a live index; packs are marked stale for
        # the next acquire rather than yanked to None mid-yield.
        assert session.index is not None
        assert session.seat_count == seats_before
        assert is_dme_resident(tmp_path) is True
    # The old in-flight pack is dropped once its holder exits; the next
    # acquire loads the rebuilt pack and keeps that fresh pack hot.
    assert is_dme_resident(tmp_path) is False
    with dme_recall_session(tmp_path) as refreshed:
        assert refreshed.seat_count == 1
    assert is_dme_resident(tmp_path) is True
    force_unload_all_sessions()
    assert is_dme_resident(tmp_path) is False


# ── Cut 1: activation trace attaches as metadata only ───────────────────────


def test_recall_hook_attaches_trace_without_changing_candidates(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    query = QuerySpec(
        cycle_id="hook-trace",
        text="neuron synapses",
        episode_id="ep-trace",
        allowed_lobes=[2],
        denied_lobes=[],
    )
    merged, stats = apply_dme_recall_hook(query, [], root=tmp_path)
    trace = stats.to_meta()["dme_activation_trace"]
    assert trace["type"] == "dme_activation_trace"
    assert trace["projection_generation"] == stats.dme_snapshot_hash
    assert trace["backend"] == stats.dme_backend
    assert trace["episode_id"] == "ep-trace"
    assert trace["cycle_id"] == "hook-trace"
    assert trace["cue_receipt_id"] is None
    assert trace["associations"] == []
    assert "neuron synapses" not in repr(trace)
    assert [c["memory_id"] for c in trace["candidates"]] == [
        item["id"] for item in merged if (item.get("provenance") or {}).get("dme_backend")
    ]
    # Cut 1 pinned recall_basis_id=None; Cut 3 populates it for direct/union
    # hits (canonical_coordinate basis) — the id must match the merged row's
    # provenance and appear in the recall_bases receipts
    bases = {b["basis_id"] for b in stats.to_meta().get("recall_bases", [])}
    for cand in trace["candidates"]:
        if cand["hop_depth"] == 0:
            assert cand["recall_basis_id"] in bases
        else:
            assert cand["recall_basis_id"] is None or cand["recall_basis_id"] in bases
    force_unload_all_sessions()


def test_trace_off_omits_only_trace_metadata(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path)
    query = QuerySpec(
        cycle_id="hook-toggle", text="neuron synapses", allowed_lobes=[2], denied_lobes=[]
    )
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "0")
    off_merged, off_stats = apply_dme_recall_hook(query, [], root=tmp_path)
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    on_merged, on_stats = apply_dme_recall_hook(query, [], root=tmp_path)
    assert on_merged == off_merged
    assert "dme_activation_trace" not in off_stats.to_meta()
    assert "dme_activation_trace" in on_stats.to_meta()
    off_meta = off_stats.to_meta()
    on_meta = {k: v for k, v in on_stats.to_meta().items() if k != "dme_activation_trace"}
    # every other stat is identical apart from cache-hit bookkeeping between calls
    for key in off_meta:
        if key in ("dme_cache_hit", "dme_loaded"):
            continue
        assert on_meta[key] == off_meta[key], key
    force_unload_all_sessions()


def test_trace_build_failure_is_diagnostic_only(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    query = QuerySpec(cycle_id="hook-soft", text="neuron synapses", allowed_lobes=[2], denied_lobes=[])
    baseline_merged, _ = apply_dme_recall_hook(query, [], root=tmp_path)

    import memory.dme.activation_trace as at

    def _boom(**_kw):
        raise RuntimeError("trace exploded")

    monkeypatch.setattr(at, "build_activation_trace", _boom)
    merged, stats = apply_dme_recall_hook(query, [], root=tmp_path)
    assert merged == baseline_merged
    assert stats.dme_ran is True
    assert stats.error == ""
    assert "dme_activation_trace" not in stats.to_meta()
    force_unload_all_sessions()


# ── Cut 2: the hook threads a cue receipt through to the adapter ────────────


def test_hook_accepts_cue_receipt_and_applies_generated_only_when_enabled(tmp_path: Path, monkeypatch) -> None:
    from memory.dme.coordinate_registry import load_registry
    from memory.routing_cues import build_cue_receipt

    _write_record(tmp_path, "r1", title="Clinic appointment", summary="Consulted a dermatologist at Mercy Clinic today")
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)
    force_unload_all_sessions()
    receipt = build_cue_receipt(
        query_text="Which skin doctor did I visit?",
        deterministic_terms=("skin", "doctor"),
        entity_terms=(),
        generated_terms=("dermatologist",),
        registry=load_registry(tmp_path),
        allowed_lobes={2},
        denied_lobes=set(),
        generator_used=True,
        generator_version="test",
    )
    query = QuerySpec(cycle_id="hook-cue", text="Which skin doctor did I visit?", allowed_lobes=[2], denied_lobes=[])

    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    merged_off, stats_off = apply_dme_recall_hook(query, [], root=tmp_path, cue_receipt=receipt)
    assert stats_off.dme_ran is True
    assert merged_off == []

    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    merged_on, stats_on = apply_dme_recall_hook(query, [], root=tmp_path, cue_receipt=receipt)
    assert [m["id"] for m in merged_on] == ["r1"]
    assert stats_on.dme_hit_count == 1
    assert "dme_activation_trace" in stats_on.to_meta()
    # no receipt → legacy behaviour regardless of flag
    merged_none, _ = apply_dme_recall_hook(query, [], root=tmp_path)
    assert merged_none == []
    force_unload_all_sessions()
