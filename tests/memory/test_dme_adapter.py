from __future__ import annotations

import json

import memory.taxonomy.graph_store as G
from memory.api import QuerySpec
from memory.dme.adapter import (
    dme_sram_path,
    parity_report,
    rebuild_sram_from_graph,
    recall_verified,
)
from memory.taxonomy.compiler import compile_memory_graph


def _write_record(
    tmp_path,
    memory_id: str,
    *,
    title: str,
    summary: str,
    status: str = "active",
    provenance=None,
    cite: bool = False,
) -> None:
    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    prov = dict(provenance or {})
    # cite=True mints a SourceLedger citation so recall verifies via ledger crypto
    # (post 2026-07-21 fail-closed: rederive is no longer accepted as verified).
    if cite and not prov.get("source_id"):
        from memory.source_ledger.store import record_source

        artifact = record_source(
            f"{title}. {summary}",
            source_type="operator_chat",
            origin_id=f"cycle-{memory_id}",
            root=tmp_path,
        )
        prov.update(artifact.citation())
    record = {
        "id": memory_id,
        "coord": [2, 0, int(memory_id[-1])],
        "type": "concept",
        "title": title,
        "summary": summary,
        "domain": "biology",
        "tags": ["biology"],
        "provenance": prov,
        "enrichment": {"canonical": True, "status": status},
    }
    (memory_root / f"{memory_id}.json").write_text(json.dumps(record), encoding="utf-8")
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    index[memory_id] = {"coord": record["coord"], "path": f"memory/{memory_id}.json"}
    index_path.write_text(json.dumps(index), encoding="utf-8")


def test_dme_rebuild_seats_only_active_memory_leaf_nodes(tmp_path):
    _write_record(
        tmp_path, "k1", title="Photosynthesis", summary="Plants convert light into chemical energy."
    )
    _write_record(
        tmp_path,
        "k2",
        title="Poisoned graph leaf",
        summary="Quarantined memory.",
        status="quarantined",
    )

    compile_memory_graph(root=tmp_path)
    assert dme_sram_path(tmp_path).exists()

    index = rebuild_sram_from_graph(root=tmp_path)

    assert index.seat_count == 1
    assert [seat.memory_id for seat in index.seats] == ["k1"]
    assert index.seats[0].coord == (2, 0, 1)


def test_empty_dme_rebuild_uses_canonical_backend(tmp_path):
    index = rebuild_sram_from_graph(root=tmp_path)

    assert index.seat_count == 0
    assert index.backend == "numpy"


def test_dme_recall_still_requires_verifier_match(tmp_path):
    _write_record(
        tmp_path, "k1", title="Neuron", summary="Neurons pass signals through synapses.", cite=True
    )
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)

    query = QuerySpec(cycle_id="dme-ok", text="neuron synapses", allowed_lobes=[2], denied_lobes=[])
    verdict = recall_verified(query, root=tmp_path)
    assert verdict.status == "verified"
    assert verdict.answer_payload["candidates"][0]["memory_id"] == "k1"

    # Break the provenance: remove the SourceLedger artifact this record cites. A
    # cited record whose ledger crypto proof can no longer be established FAILS
    # CLOSED (2026-07-21) — proving the DME recall path still gates on the verifier,
    # not a bare graph/file match. (A cited record is drift-agnostic; tampering the
    # graph label alone would NOT — and should not — unverify it.)
    import glob as _glob
    import os as _os

    record = json.loads((tmp_path / "memory" / "k1.json").read_text(encoding="utf-8"))
    source_id = record["provenance"]["source_id"]
    arts = _glob.glob(
        str(tmp_path / "memory" / "source_ledger" / "**" / f"*{source_id}*"), recursive=True
    )
    assert arts, "expected a ledger artifact to tamper with"
    for _a in arts:
        _os.remove(_a)

    stale = recall_verified(
        QuerySpec(cycle_id="dme-stale", text="neuron synapses", allowed_lobes=[2], denied_lobes=[]),
        root=tmp_path,
    )
    assert stale.status == "unknown"
    assert stale.answer_payload is None


def test_dme_rows_carry_source_session_fields_from_graph_leaf(tmp_path):
    from memory.source_ledger.store import record_source

    artifact = record_source(
        "Neuron. Neurons pass signals through synapses.",
        source_type="longmemeval.session",
        origin_id="instance-9",
        metadata={
            "source_session_id": "session-neuron",
            "source_session_timestamp": "2025-05-06T07:08:09+00:00",
        },
        root=tmp_path,
    )
    _write_record(
        tmp_path,
        "k1",
        title="Neuron",
        summary="Neurons pass signals through synapses.",
        provenance=artifact.citation(),
    )
    compile_memory_graph(root=tmp_path)

    index = rebuild_sram_from_graph(root=tmp_path)

    assert index.row_index[0]["source_session_id"] == "session-neuron"
    assert index.row_index[0]["source_session_timestamp"] == "2025-05-06T07:08:09+00:00"


def test_dme_adapter_receipt_accounts_for_filters_and_candidate_limit(monkeypatch):
    from types import SimpleNamespace

    from memory.dme import adapter, hop_engine

    ids = ["dme-a", "dme-b", "dme-c"]
    rows = [
        {"memory_id": memory_id, "coord": coord, "title": memory_id}
        for memory_id, coord in zip(ids, ([2, 0, 1], [2, 0, 2], [3, 0, 3]))
    ]
    index = SimpleNamespace(
        seat_count=3,
        snapshot_hash="snapshot",
        backend="numpy",
        row_index=rows,
        sram=object(),
    )
    result = hop_engine.ThoughtResult(
        hits=[
            hop_engine.ThoughtHit(
                row_index=rank,
                memory_id=memory_id,
                score=1.0 - rank * 0.1,
                hop=0,
                match_count=1,
                query_count=1,
            )
            for rank, memory_id in enumerate(ids)
        ],
        entries_scanned=3,
        hops_run=0,
        query_coordinates=[(2, 0, 1)],
    )
    monkeypatch.setattr(adapter, "_session_index_and_planes", lambda _root: (index, None))
    monkeypatch.setattr(adapter, "load_registry", lambda _root: object())
    monkeypatch.setattr(adapter, "_query_coordinates", lambda *_args, **_kwargs: [(2, 0, 1)])
    monkeypatch.setattr(hop_engine, "search_thought", lambda *_args, **_kwargs: result)

    execution = adapter._recall_execution_loaded(
        "query", allowed_lobes=[2], denied_lobes=[], limit=1
    )

    assert [candidate["memory_id"] for candidate in execution.candidates] == ["dme-a"]
    receipt = execution.truncation_receipts[-1]
    assert receipt["stage"] == "dme_adapter:filters_and_candidate_limit"
    assert receipt["input_count"] == 3
    assert receipt["output_count"] == 1
    assert receipt["dropped_count"] == 2
    assert receipt["dropped_candidate_ids"] == ["dme-b", "dme-c"]


def test_dme_parity_reports_graph_count_drift(tmp_path):
    _write_record(
        tmp_path, "k1", title="Photosynthesis", summary="Plants convert light into chemical energy."
    )
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)

    conn = G.connect(root=tmp_path)
    try:
        G.insert_node(
            conn,
            node_id="k2",
            node_type="memory_record",
            label="Unseated active leaf",
            payload={
                "type": "memory_leaf_v2",
                "memory_id": "k2",
                "coord": [2, 0, 2],
                "title": "Unseated active leaf",
                "summary": "Only the graph knows this one.",
                "status": "active",
            },
            source_event_id="dewey:k2",
        )
        conn.commit()
    finally:
        conn.close()

    report = parity_report(root=tmp_path)
    assert report.status == "drift"
    assert report.graph_active_leaf_count == 2
    assert report.dme_seat_count == 1
    assert report.missing_in_dme == ("k2",)


# ── Cut 2: cue addresses resolve additively, generated gated, no mint ───────


def _seed_clinic_store(tmp_path):
    """Two seated leaves whose vocabulary the query below never touches:
    r1 carries 'dermatologist', r2 carries 'physician'. Query tokens
    (skin / doctor / visit?) resolve to nothing → the only way to reach r1 is a
    cue address for 'dermatologist'."""
    _write_record(tmp_path, "r1", title="Clinic appointment", summary="Consulted a dermatologist at Mercy Clinic today")
    _write_record(tmp_path, "r2", title="Checkup", summary="Saw the physician for a routine checkup")
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)
    from memory.dme.recall_hook import force_unload_all_sessions

    force_unload_all_sessions()


def _clinic_receipt(tmp_path, *, generated=("dermatologist",)):
    from memory.dme.coordinate_registry import load_registry
    from memory.routing_cues import build_cue_receipt

    return build_cue_receipt(
        query_text="Which skin doctor did I visit?",
        deterministic_terms=("skin", "doctor", "visit?"),
        entity_terms=(),
        generated_terms=tuple(generated),
        registry=load_registry(tmp_path),
        allowed_lobes={2},
        denied_lobes=set(),
        generator_used=True,
        generator_version="test",
    )


def test_cue_receipt_with_apply_off_leaves_legacy_candidates_unchanged(tmp_path, monkeypatch):
    import hashlib

    from memory.dme.adapter import recall_candidates, recall_candidates_observed
    from memory.dme.coordinate_registry import registry_path

    _seed_clinic_store(tmp_path)
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    receipt = _clinic_receipt(tmp_path)
    reg_file = registry_path(tmp_path)
    before = hashlib.sha256(reg_file.read_bytes()).hexdigest()

    q = "Which skin doctor did I visit?"
    assert recall_candidates(q, root=tmp_path, allowed_lobes=[2], denied_lobes=[]) == []
    with_receipt = recall_candidates(q, root=tmp_path, allowed_lobes=[2], denied_lobes=[], cue_receipt=receipt)
    assert with_receipt == recall_candidates(q, root=tmp_path, allowed_lobes=[2], denied_lobes=[], cue_receipt=None)
    observed = recall_candidates_observed(q, root=tmp_path, allowed_lobes=[2], denied_lobes=[], cue_receipt=receipt)
    assert observed.candidates == ()
    assert hashlib.sha256(reg_file.read_bytes()).hexdigest() == before


def test_cue_receipt_with_apply_on_reaches_zero_overlap_record_without_mint(tmp_path, monkeypatch):
    import hashlib

    from memory.dme.adapter import recall_candidates_observed
    from memory.dme.coordinate_registry import load_registry, registry_path

    _seed_clinic_store(tmp_path)
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    receipt = _clinic_receipt(tmp_path)
    reg = load_registry(tmp_path)
    derm = reg.lookup("dermatologist")
    assert derm is not None
    reg_file = registry_path(tmp_path)
    before = hashlib.sha256(reg_file.read_bytes()).hexdigest()

    observed = recall_candidates_observed(
        "Which skin doctor did I visit?", root=tmp_path, allowed_lobes=[2], denied_lobes=[], cue_receipt=receipt
    )
    assert observed.thought_result is not None
    assert derm in observed.thought_result.query_coordinates
    # r1 is reached DIRECTLY through the cue address; anything else (r2 shares
    # the fixture domain) can only arrive through a hop plane behind it
    assert observed.candidates[0]["memory_id"] == "r1"
    assert observed.candidates[0]["provenance"]["dme_edge_class"] in ("direct", "union")
    assert all(int(c["provenance"]["dme_hop"]) >= 1 for c in observed.candidates[1:])
    assert hashlib.sha256(reg_file.read_bytes()).hexdigest() == before
    # unknown generated terms stay unresolved and mint nothing
    receipt2 = _clinic_receipt(tmp_path, generated=("zorblax", "dermatologist"))
    assert "zorblax" in receipt2.unresolved_terms
    assert hashlib.sha256(reg_file.read_bytes()).hexdigest() == before


def test_deterministic_cue_addresses_are_additive_after_legacy_prefix(tmp_path, monkeypatch):
    """Legacy token/near-match resolution comes first and unchanged; cue
    addresses append after it; generated addresses only with APPLY on."""
    from memory.dme.adapter import _query_coordinates
    from memory.dme.coordinate_registry import load_registry
    from memory.routing_cues import build_cue_receipt

    _seed_clinic_store(tmp_path)
    reg = load_registry(tmp_path)
    legacy = _query_coordinates("physician checkup", reg, allowed_lobes=[2], denied_lobes=set())
    assert legacy
    receipt = build_cue_receipt(
        query_text="physician checkup",
        deterministic_terms=("physician", "clinic"),
        entity_terms=("mercy",),
        generated_terms=("dermatologist",),
        registry=reg,
        allowed_lobes={2},
        denied_lobes=set(),
    )
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    off = _query_coordinates("physician checkup", reg, allowed_lobes=[2], denied_lobes=set(), cue_receipt=receipt)
    assert off[: len(legacy)] == legacy
    assert reg.lookup("clinic") in off and reg.lookup("mercy") in off
    assert reg.lookup("dermatologist") not in off
    assert len(off) == len(set(off))
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    on = _query_coordinates("physician checkup", reg, allowed_lobes=[2], denied_lobes=set(), cue_receipt=receipt)
    assert on[: len(off)] == off
    assert reg.lookup("dermatologist") in on
    # lobe filter still applies to cue addresses
    denied = _query_coordinates("physician checkup", reg, allowed_lobes=[2], denied_lobes={2}, cue_receipt=receipt)
    assert denied == []
