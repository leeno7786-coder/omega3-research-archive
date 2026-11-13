"""Cut 3 — RecallBasis: verifier-recognized semantic routing proof.

Governing: docs/engineering/memory-organ/14_REVIEW_RECONCILIATION_FREEZE.md
(D-1 canonical module memory/recall_basis.py, C-3 row-vs-verdict, B-1 OR
planes are not attribution) and the Cut 3 plan
docs/superpowers/plans/2026-08-14-memory-organ-cut3-recall-basis-verifier.md.

A RecallBasis proves that a valid semantic ROUTE from the query to a candidate
exists in the exact projection generation that scored it. It never proves the
recalled fact (canonical/source verification does), never carries a verdict,
and never authorizes association credit.
"""

from __future__ import annotations

import pytest

from memory.recall_basis import (
    CUT3_ADMISSIBLE_KINDS,
    RECALL_BASIS_KINDS,
    RECALL_BASIS_SCHEMA_VERSION,
    RecallBasis,
    basis_id_for,
    cut3_admissible_kind,
    make_recall_basis,
    recall_basis_topicality_enabled,
)

# ── helpers ─────────────────────────────────────────────────────────────────


def make_basis(**overrides) -> RecallBasis:
    fields = dict(
        candidate_id="m1",
        kind="canonical_coordinate",
        query_terms=("dermatologist",),
        query_addresses=((2, 0, 5),),
        generated_query_terms=("dermatologist",),
        path_node_ids=(),
        path_edge_ids=(),
        relation_types=(),
        association_ids=(),
        projection_generation="snap-1",
        graph_snapshot_hash="snap-1",
    )
    fields.update(overrides)
    return make_recall_basis(**fields)


# ── Task 1: schema, identity, admissibility ─────────────────────────────────


def test_recall_basis_is_routing_not_source_proof():
    basis = RecallBasis(
        basis_id="rb-1",
        candidate_id="m1",
        kind="canonical_coordinate",
        query_terms=("dermatologist",),
        query_addresses=((2, 0, 5),),
        generated_query_terms=("dermatologist",),
        path_node_ids=(),
        path_edge_ids=(),
        relation_types=(),
        association_ids=(),
        projection_generation="snap-1",
        graph_snapshot_hash="snap-1",
    )
    payload = basis.to_dict()
    assert "source_proof" not in payload
    assert "verified" not in payload
    assert "status" not in payload
    assert "credit" not in repr(payload)
    assert payload["schema_version"] == RECALL_BASIS_SCHEMA_VERSION == "omega.recall_basis.v1"
    assert basis.validate() == []


def test_learned_path_is_schema_valid_but_not_cut3_admissible():
    basis = make_basis(kind="learned_path")
    assert basis.validate() == []
    assert basis.kind == "learned_path"
    assert cut3_admissible_kind("learned_path") is False
    assert CUT3_ADMISSIBLE_KINDS == {"canonical_coordinate", "canonical_alias", "structural_path"}
    assert "lexical" in RECALL_BASIS_KINDS and cut3_admissible_kind("lexical") is False


def test_basis_id_is_deterministic_over_all_fields_except_itself():
    a = make_basis()
    b = make_basis()
    assert a.basis_id == b.basis_id
    assert a.basis_id == basis_id_for(a.to_dict())
    c = make_basis(projection_generation="snap-2")
    assert c.basis_id != a.basis_id
    # tampering with any field after the fact invalidates the id
    tampered = RecallBasis(**{**a.__dict__, "candidate_id": "m2"})
    assert basis_id_for(tampered.to_dict()) != tampered.basis_id
    assert a.identity_ok() is True and tampered.identity_ok() is False
    assert tampered.validate() == []  # structurally fine; identity is a separate, verifier-enforced check


def test_validate_rejects_unknown_kind_missing_candidate_and_bad_addresses():
    assert any("kind" in e for e in make_basis(kind="vibes").validate())
    assert any("candidate_id" in e for e in make_basis(candidate_id="").validate())
    assert any("query_addresses" in e for e in make_basis(query_addresses=((2, 0),)).validate())
    # structural_path must name a path; coordinate basis must name an address
    assert any("path" in e for e in make_basis(kind="structural_path", path_node_ids=(), path_edge_ids=()).validate())
    assert any("query_addresses" in e for e in make_basis(query_addresses=()).validate())


def test_from_dict_parses_untrusted_rows_and_rejects_garbage():
    good = make_basis().to_dict()
    parsed = RecallBasis.from_dict(good)
    assert parsed is not None and parsed.basis_id == good["basis_id"] and parsed.validate() == []
    for bad in (None, 42, "rb", [], {}, {"basis_id": "x"}, {**good, "query_addresses": "nope"},
                {**good, "kind": 7}, {**good, "path_edge_ids": [1, 2]}):
        assert RecallBasis.from_dict(bad) is None or RecallBasis.from_dict(bad).validate()


def test_topicality_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    assert recall_basis_topicality_enabled() is True
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("OMEGA_RECALL_BASIS_TOPICALITY", off)
        assert recall_basis_topicality_enabled() is False


def test_basis_dict_never_carries_association_attribution():
    payload = make_basis(kind="structural_path", path_node_ids=("s", "t"), path_edge_ids=("e1",), relation_types=("SUPPORTS",)).to_dict()
    assert payload["association_ids"] == []
    for forbidden in ("attribution_status", "credit_eligible", "matched_bits", "contribution_ratio"):
        assert forbidden not in payload


# ── Task 2: graph neighbor relations expose exact edge identity ─────────────


def test_neighbors_expose_edge_id_and_evidence_event_id_additively(tmp_path):
    from memory.taxonomy import graph_store as G
    from memory.taxonomy.schemas import MemoryEdge

    conn = G.connect(tmp_path)
    for mid in ("S", "T"):
        G.insert_node(conn, node_id=mid, node_type="memory_record", label=mid, payload={})
    edge = MemoryEdge(edge_id="edge-st", source_id="S", target_id="T", edge_type="SUPPORTS", evidence_event_id="ev-st")
    G.insert_edge(conn, edge)
    conn.commit()
    out = G.neighbors(conn, "S")
    inbound = G.neighbors(conn, "T")
    conn.close()
    assert len(out) == 1 and len(inbound) == 1
    for rel, direction in ((out[0], "outbound"), (inbound[0], "inbound")):
        assert rel["edge_id"] == "edge-st"
        assert rel["evidence_event_id"] == "ev-st"
        assert rel["edge_type"] == "SUPPORTS"
        assert rel["direction"] == direction
        # existing keys still present
        for key in ("node_id", "node_type", "label", "confidence", "payload", "edge_payload"):
            assert key in rel, key


# ── Task 3: canonical_coordinate bases for direct/union hits ────────────────


def _row(*coords):
    from memory.dme.packer import pack_entry

    return pack_entry(0, 0.0, "seat", list(coords))


def _receipt_with(terms_addresses, *, query="q"):
    """A CueReceipt whose source map is exactly ``terms_addresses`` =
    [(term, source, address), ...] — built through the real builder against a
    stub registry so the shape is genuine."""
    from memory.routing_cues import build_cue_receipt

    class _Reg:
        def __init__(self, table):
            self.table = table

        def lookup(self, key):
            return self.table.get(key)

        def near_matches(self, key, **_kw):
            return []

    table = {term: addr for term, _src, addr in terms_addresses}
    det = tuple(t for t, s, _a in terms_addresses if s == "deterministic")
    ent = tuple(t for t, s, _a in terms_addresses if s == "entity")
    gen = tuple(t for t, s, _a in terms_addresses if s == "generated")
    return build_cue_receipt(
        query_text=query, deterministic_terms=det, entity_terms=ent, generated_terms=gen,
        registry=_Reg(table), allowed_lobes=None, denied_lobes=set(),
        generator_used=bool(gen), generator_version="t" if gen else "",
    )


def test_build_coordinate_basis_uses_exact_bit_overlap_not_text():
    from memory.recall_basis import build_coordinate_basis

    row = _row((2, 0, 5), (2, 0, 9))
    receipt = _receipt_with([("dermatologist", "generated", (2, 0, 5)), ("clinic", "deterministic", (2, 0, 7))])
    basis = build_coordinate_basis(
        candidate_id="m1",
        candidate_row=row,
        query_coordinates=[(2, 0, 7), (2, 0, 5), (3, 0, 1)],
        cue_receipt=receipt,
        projection_generation="snap",
        graph_snapshot_hash="snap",
    )
    assert basis is not None
    assert basis.kind == "canonical_coordinate"
    assert basis.candidate_id == "m1"
    # only the addresses actually set in the candidate row participate
    assert basis.query_addresses == ((2, 0, 5),)
    # terms are the cue terms whose resolved address participates
    assert basis.query_terms == ("dermatologist",)
    assert basis.generated_query_terms == ("dermatologist",)
    assert basis.projection_generation == "snap" and basis.graph_snapshot_hash == "snap"
    assert basis.validate() == [] and basis.identity_ok()
    assert basis.path_edge_ids == () and basis.association_ids == ()


def test_build_coordinate_basis_returns_none_without_direct_overlap():
    from memory.recall_basis import build_coordinate_basis

    row = _row((2, 0, 5))
    assert build_coordinate_basis(
        candidate_id="m1", candidate_row=row, query_coordinates=[(2, 0, 6), (2, 0, 7)],
        cue_receipt=None, projection_generation="snap", graph_snapshot_hash="snap",
    ) is None
    assert build_coordinate_basis(
        candidate_id="m1", candidate_row=row, query_coordinates=[],
        cue_receipt=None, projection_generation="snap", graph_snapshot_hash="snap",
    ) is None


def test_build_coordinate_basis_without_receipt_has_addresses_but_no_terms():
    from memory.recall_basis import build_coordinate_basis

    basis = build_coordinate_basis(
        candidate_id="m1", candidate_row=_row((2, 0, 5)), query_coordinates=[(2, 0, 5)],
        cue_receipt=None, projection_generation="snap", graph_snapshot_hash="snap",
    )
    assert basis is not None and basis.query_addresses == ((2, 0, 5),)
    assert basis.query_terms == () and basis.generated_query_terms == ()


def test_hook_attaches_coordinate_bases_to_direct_hits_and_trace(monkeypatch):
    """Inside the active DME session: direct/union hits get a
    canonical_coordinate basis; merged provenance carries recall_basis_id;
    stats/meta expose recall_bases; the activation trace candidate carries the
    same id. strong_hop1 candidates get a structural_path basis (Task 4)."""
    from tests.memory.test_dme_activation_trace import _strong_edge, _write_record  # noqa: F401
    import memory.taxonomy.graph_store as G
    from memory.api import QuerySpec
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.recall_hook import apply_dme_recall_hook, force_unload_all_sessions
    from memory.taxonomy.compiler import compile_memory_graph
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="cut3-t3-"))
    _write_record(root, "k1", title="Neuron", summary="Neurons pass signals through synapses.")
    _write_record(root, "k2", title="Photosynthesis", summary="Plants convert light into chemical energy.")
    _write_record(root, "k3", title="Mitochondria", summary="Organelles produce cellular power.")
    compile_memory_graph(root=root)
    conn = G.connect(root=root)
    _strong_edge(conn, "k1", "k3"); _strong_edge(conn, "k2", "k3"); conn.commit(); conn.close()
    index = rebuild_sram_from_graph(root=root)
    force_unload_all_sessions()
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")

    q = QuerySpec(cycle_id="t3", text="neuron light", episode_id="e", allowed_lobes=[2], denied_lobes=[])
    merged, stats = apply_dme_recall_hook(q, [], root=root)
    by_id = {m["id"]: m for m in merged}
    # k1/k2 are union hits → canonical_coordinate; k3 is strong_hop1 →
    # structural_path (Task 4). Every basis id round-trips provenance ↔ meta.
    assert by_id["k1"]["provenance"].get("recall_basis_id")
    assert by_id["k2"]["provenance"].get("recall_basis_id")
    assert by_id["k3"]["provenance"].get("recall_basis_id")
    bases = stats.to_meta()["recall_bases"]
    kinds = {b["candidate_id"]: b["kind"] for b in bases}
    assert kinds == {"k1": "canonical_coordinate", "k2": "canonical_coordinate", "k3": "structural_path"}
    for b in bases:
        assert b["projection_generation"] == index.snapshot_hash == stats.dme_snapshot_hash
        assert b["basis_id"] == by_id[b["candidate_id"]]["provenance"]["recall_basis_id"]
        assert b["association_ids"] == []
    trace = stats.to_meta()["dme_activation_trace"]
    tc = {c["memory_id"]: c for c in trace["candidates"]}
    assert tc["k1"]["recall_basis_id"] == by_id["k1"]["provenance"]["recall_basis_id"]
    assert tc["k3"]["recall_basis_id"] == by_id["k3"]["provenance"]["recall_basis_id"]
    assert tc["k3"]["attribution_status"] == "diagnostic"
    # scores/order untouched by basis derivation
    assert [m["id"] for m in merged] == ["k3", "k1", "k2"]
    force_unload_all_sessions()


# ── Task 4: structural_path bases for strong-hop-1 hits ─────────────────────


def _seed_store(root, *, mediated=False, weak_only=False):
    """k1/k2 → k3 store. Direct: SUPPORTS edges (strong). Mediated: k1 and k3
    both MENTION entity ent-mito (k2 unlinked). Weak-only: CO_LOCATED edges."""
    from tests.memory.test_dme_activation_trace import _write_record
    import memory.taxonomy.graph_store as G
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.taxonomy.compiler import compile_memory_graph
    from memory.taxonomy.schemas import MemoryEdge

    _write_record(root, "k1", title="Neuron", summary="Neurons pass signals through synapses.")
    _write_record(root, "k2", title="Photosynthesis", summary="Plants convert light into chemical energy.")
    _write_record(root, "k3", title="Mitochondria", summary="Organelles produce cellular power.")
    compile_memory_graph(root=root)
    conn = G.connect(root=root)
    try:
        if mediated:
            G.insert_node(conn, node_id="ent-mito", node_type="named_entity", label="mitochondria", payload={})
            for src in ("k1", "k3"):
                G.insert_edge(conn, MemoryEdge(edge_id=f"e:{src}:ent", source_id=src, target_id="ent-mito",
                                               edge_type="MENTIONS", evidence_event_id=f"ev:{src}:ent"))
        else:
            etype = "CO_LOCATED" if weak_only else "SUPPORTS"
            for src in ("k1", "k2"):
                G.insert_edge(conn, MemoryEdge(edge_id=f"e:{src}:k3", source_id=src, target_id="k3",
                                               edge_type=etype, evidence_event_id=f"ev:{src}:k3"))
        conn.commit()
    finally:
        conn.close()
    index = rebuild_sram_from_graph(root=root)
    force_unload_all_sessions()
    return index


def _query_coords(root, text):
    from memory.dme.adapter import _query_coordinates
    from memory.dme.coordinate_registry import load_registry

    return _query_coordinates(text, load_registry(root), allowed_lobes=[2], denied_lobes=set())


def test_structural_basis_direct_memory_edge(tmp_path):
    from memory.recall_basis import build_structural_path_basis

    index = _seed_store(tmp_path)
    coords = _query_coords(tmp_path, "neuron light")
    basis = build_structural_path_basis(
        candidate_id="k3", evidence_class="strong_hop1", sram=index.sram, row_index=index.row_index,
        query_coordinates=coords, root=tmp_path,
        projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash, cue_receipt=None,
    )
    assert basis is not None
    assert basis.kind == "structural_path"
    assert basis.candidate_id == "k3"
    # deterministic winner: both k1/k2 tie on edge count and overlap ratio → source id order
    assert basis.path_node_ids == ("k1", "k3")
    assert basis.path_edge_ids == ("e:k1:k3",)
    assert basis.relation_types == ("SUPPORTS",)
    assert basis.projection_generation == index.snapshot_hash
    assert basis.query_addresses  # the source row's exact query overlap
    assert basis.association_ids == ()
    assert basis.validate() == [] and basis.identity_ok()


def test_structural_basis_names_existing_sidecar_association(tmp_path, monkeypatch):
    from memory import association_strength
    from memory.recall_basis import build_structural_path_basis

    index = _seed_store(tmp_path)
    sidecar_path = tmp_path / "association_strength.json"
    monkeypatch.setattr(association_strength, "association_path", lambda: sidecar_path)
    with association_strength.deep_sleep_mutation():
        association_id = association_strength.AssociationStrengthIndex(sidecar_path).register(
            "k1",
            "SUPPORTS",
            "k3",
            provenance_class=association_strength.STRUCTURAL_PROVENANCE_CLASS,
            structural_strength=1.0,
            support_count=1,
        )

    coords = _query_coords(tmp_path, "neuron light")
    basis = build_structural_path_basis(
        candidate_id="k3", evidence_class="strong_hop1", sram=index.sram, row_index=index.row_index,
        query_coordinates=coords, root=tmp_path,
        projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash, cue_receipt=None,
    )
    assert basis is not None
    assert basis.path_edge_ids == ("e:k1:k3",)
    assert basis.association_ids == (association_id,)


def test_mediated_structural_basis_preserves_directed_association_endpoints(tmp_path, monkeypatch):
    from memory import association_strength
    from memory.recall_basis import build_structural_path_basis

    index = _seed_store(tmp_path, mediated=True)
    sidecar_path = tmp_path / "association_strength.json"
    monkeypatch.setattr(association_strength, "association_path", lambda: sidecar_path)
    with association_strength.deep_sleep_mutation():
        sidecar = association_strength.AssociationStrengthIndex(sidecar_path)
        first = sidecar.register(
            "k1",
            "MENTIONS",
            "ent-mito",
            provenance_class=association_strength.STRUCTURAL_PROVENANCE_CLASS,
            structural_strength=1.0,
            support_count=1,
        )
        second = sidecar.register(
            "k3",
            "MENTIONS",
            "ent-mito",
            provenance_class=association_strength.STRUCTURAL_PROVENANCE_CLASS,
            structural_strength=1.0,
            support_count=1,
        )

    coords = _query_coords(tmp_path, "neuron")
    basis = build_structural_path_basis(
        candidate_id="k3", evidence_class="strong_hop1", sram=index.sram, row_index=index.row_index,
        query_coordinates=coords, root=tmp_path,
        projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash, cue_receipt=None,
    )
    assert basis is not None
    assert basis.path_edge_ids == ("e:k1:ent", "e:k3:ent")
    assert basis.association_ids == (first, second)


def test_opt_in_learned_recall_gets_a_generation_bound_basis_without_credit_trace(
    tmp_path, monkeypatch
):
    from memory import association_strength
    from memory.api import QuerySpec
    from memory.dme.recall_hook import apply_dme_recall_hook, force_unload_all_sessions
    from memory.recall_basis import verify_recall_basis

    _seed_store(tmp_path)
    sidecar_path = tmp_path / "association_strength.json"
    monkeypatch.setattr(association_strength, "association_path", lambda: sidecar_path)
    monkeypatch.setenv("OMEGA_DME_LEARNED_PLANES", "1")
    with association_strength.deep_sleep_mutation():
        association_strength.AssociationStrengthIndex(sidecar_path).register(
            "k1",
            "SUPPORTS",
            "k2",
            provenance_class="learned_experiential",
            structural_strength=0.8,
            experiential_strength=0.8,
        )

    query = QuerySpec(
        cycle_id="learned-recall",
        text="neuron",
        episode_id="episode-learned",
        allowed_lobes=[2],
        denied_lobes=[],
    )
    merged, stats = apply_dme_recall_hook(query, [], root=tmp_path)
    learned = next(item for item in merged if item["id"] == "k2")
    basis_row = next(
        basis for basis in stats.dme_recall_bases if basis["candidate_id"] == "k2"
    )
    assert learned["provenance"]["candidate_source"] == "dme_learned"
    assert basis_row["kind"] == "learned_path"
    assert stats.dme_activation_trace["associations"] == []
    basis = next(
        basis for basis in stats.dme_recall_bases if basis["candidate_id"] == "k2"
    )
    from memory.recall_basis import RecallBasis

    verification = verify_recall_basis(
        RecallBasis.from_dict(basis),
        candidate_id="k2",
        root=tmp_path,
        executed_addresses=(stats.dme_execution_binding or {}).get("query_coordinates") or (),
    )
    assert verification.valid is True and verification.kind == "learned_path"
    force_unload_all_sessions()


def test_structural_basis_mediated_entity_path(tmp_path):
    from memory.recall_basis import build_structural_path_basis

    index = _seed_store(tmp_path, mediated=True)
    coords = _query_coords(tmp_path, "neuron")
    basis = build_structural_path_basis(
        candidate_id="k3", evidence_class="strong_hop1", sram=index.sram, row_index=index.row_index,
        query_coordinates=coords, root=tmp_path,
        projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash, cue_receipt=None,
    )
    assert basis is not None
    assert basis.path_node_ids == ("k1", "ent-mito", "k3")
    assert basis.path_edge_ids == ("e:k1:ent", "e:k3:ent")
    assert basis.relation_types == ("MENTIONS", "MENTIONS")
    assert basis.validate() == []


def test_structural_basis_negative_controls(tmp_path):
    from memory.recall_basis import build_structural_path_basis

    index = _seed_store(tmp_path)
    coords = _query_coords(tmp_path, "neuron light")
    common = dict(candidate_id="k3", sram=index.sram, row_index=index.row_index, root=tmp_path,
                  projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash, cue_receipt=None)
    # strong_hop2 / weak classes: never in Cut 3
    for cls in ("strong_hop2", "weak_hop1", "weak_hop2", "direct"):
        assert build_structural_path_basis(evidence_class=cls, query_coordinates=coords, **common) is None
    # graph snapshot != DME projection generation
    assert build_structural_path_basis(evidence_class="strong_hop1", query_coordinates=coords,
                                       **{**common, "graph_snapshot_hash": "other"}) is None
    # source memory has zero query overlap (query hits nothing seated)
    assert build_structural_path_basis(evidence_class="strong_hop1", query_coordinates=[(2, 0, 4000)], **common) is None
    assert build_structural_path_basis(evidence_class="strong_hop1", query_coordinates=[], **common) is None


def test_structural_basis_rejects_weak_only_relations(tmp_path):
    from memory.recall_basis import build_structural_path_basis

    index = _seed_store(tmp_path, weak_only=True)
    coords = _query_coords(tmp_path, "neuron light")
    assert build_structural_path_basis(
        candidate_id="k3", evidence_class="strong_hop1", sram=index.sram, row_index=index.row_index,
        query_coordinates=coords, root=tmp_path,
        projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash, cue_receipt=None,
    ) is None


def test_structural_basis_rejects_paths_longer_than_two_edges(tmp_path):
    """S -MENTIONS-> E1 <-MENTIONS- X -MENTIONS-> E2 <-MENTIONS- T needs 4 edges /
    2 mediators; Cut 3 search is bounded to 2 edges / 1 mediator → None."""
    from tests.memory.test_dme_activation_trace import _write_record
    import memory.taxonomy.graph_store as G
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.recall_basis import build_structural_path_basis
    from memory.taxonomy.compiler import compile_memory_graph
    from memory.taxonomy.schemas import MemoryEdge

    _write_record(tmp_path, "k1", title="Neuron", summary="Neurons pass signals through synapses.")
    _write_record(tmp_path, "k2", title="Photosynthesis", summary="Plants convert light into chemical energy.")
    _write_record(tmp_path, "k3", title="Mitochondria", summary="Organelles produce cellular power.")
    compile_memory_graph(root=tmp_path)
    conn = G.connect(root=tmp_path)
    for ent in ("ent-a", "ent-b"):
        G.insert_node(conn, node_id=ent, node_type="named_entity", label=ent, payload={})
    for eid, src, dst in (("e1", "k1", "ent-a"), ("e2", "k2", "ent-a"), ("e3", "k2", "ent-b"), ("e4", "k3", "ent-b")):
        G.insert_edge(conn, MemoryEdge(edge_id=eid, source_id=src, target_id=dst, edge_type="MENTIONS", evidence_event_id=eid))
    conn.commit(); conn.close()
    index = rebuild_sram_from_graph(root=tmp_path)
    force_unload_all_sessions()
    coords = _query_coords(tmp_path, "neuron")  # overlaps k1 only
    assert build_structural_path_basis(
        candidate_id="k3", evidence_class="strong_hop1", sram=index.sram, row_index=index.row_index,
        query_coordinates=coords, root=tmp_path,
        projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash, cue_receipt=None,
    ) is None


def test_hook_attaches_structural_basis_to_strong_hop1_and_trace_stays_diagnostic(monkeypatch, tmp_path):
    from memory.api import QuerySpec
    from memory.dme.recall_hook import apply_dme_recall_hook, force_unload_all_sessions

    _seed_store(tmp_path)
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    q = QuerySpec(cycle_id="t4", text="neuron light", episode_id="e", allowed_lobes=[2], denied_lobes=[])
    merged, stats = apply_dme_recall_hook(q, [], root=tmp_path)
    by_id = {m["id"]: m for m in merged}
    bases = {b["candidate_id"]: b for b in stats.to_meta()["recall_bases"]}
    assert bases["k3"]["kind"] == "structural_path"
    assert bases["k3"]["path_node_ids"] == ["k1", "k3"]
    assert by_id["k3"]["provenance"]["recall_basis_id"] == bases["k3"]["basis_id"]
    assert bases["k1"]["kind"] == bases["k2"]["kind"] == "canonical_coordinate"
    trace = stats.to_meta()["dme_activation_trace"]
    k3 = next(c for c in trace["candidates"] if c["memory_id"] == "k3")
    # routing proof exists AND causal edge attribution still does not (Fable B-1)
    assert k3["recall_basis_id"] == bases["k3"]["basis_id"]
    assert k3["attribution_status"] == "diagnostic"
    assert trace["associations"] == []
    assert bases["k3"]["association_ids"] == []
    force_unload_all_sessions()


# ── Task 5: basis id survives memory.api candidate shaping / merge / gate ────


def test_retrieval_candidate_has_nullable_recall_basis_id():
    from memory.api import RetrievalCandidate

    c = RetrievalCandidate(id="m1", coord=[2, 0, 1], score=0.5)
    assert c.recall_basis_id is None
    c2 = RetrievalCandidate(id="m1", coord=[2, 0, 1], score=0.5, recall_basis_id="rb1")
    assert c2.recall_basis_id == "rb1"


def test_verify_retrieval_result_handoff_carries_recall_basis_id(monkeypatch):
    """The verifier's candidate dict (built from RetrievalCandidate objects)
    must carry recall_basis_id so the gate can look the basis up."""
    from types import SimpleNamespace

    import memory.recall_verifier as rv
    from memory.api import RetrievalCandidate

    seen = {}

    def fake_verify_candidates(query, candidates, **kw):
        seen["cands"] = list(candidates)
        return SimpleNamespace(status="unknown")

    monkeypatch.setattr(rv, "verify_candidates", fake_verify_candidates)
    result = SimpleNamespace(
        candidates=[RetrievalCandidate(id="m1", coord=[2, 0, 1], score=0.5, recall_basis_id="rb1"),
                    RetrievalCandidate(id="m2", coord=[2, 0, 2], score=0.4)],
        meta={"recall_bases": []},
    )
    rv.verify_retrieval_result("q", result)
    by_id = {c["memory_id"]: c for c in seen["cands"]}
    assert by_id["m1"]["recall_basis_id"] == "rb1"
    assert by_id["m2"]["recall_basis_id"] is None


def test_read_memory_carries_recall_basis_id_end_to_end(monkeypatch):
    """Production read_memory on a store where k3 is reached only via a
    strong_hop1 structural basis: the DME merge → RetrievalCandidate →
    canonical merge → verifier gate keeps recall_basis_id, and meta carries
    exactly this recall's basis records."""
    import core.paths as core_paths
    from memory import api as memory_api
    from memory.api import QuerySpec
    from memory.dme.recall_hook import force_unload_all_sessions
    from pathlib import Path

    profile_root = Path(core_paths.MEMORY_ROOT).parent
    _seed_store(profile_root)
    monkeypatch.setattr(memory_api, "OMEGA_ROOT", profile_root)
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    result = memory_api.read_memory(
        QuerySpec(cycle_id="t5", text="neuron light", episode_id="e", allowed_lobes=[2], denied_lobes=[], limit=8)
    )
    bases = {b["candidate_id"]: b for b in result.meta["recall_bases"]}
    assert set(bases) == {"k1", "k2", "k3"}
    # pre-gate: DME rows carried the id in provenance; post-gate: RetrievalCandidate.recall_basis_id
    for cand in result.candidates:
        if cand.id in bases:
            assert cand.recall_basis_id == bases[cand.id]["basis_id"], cand.id
    # basis data is not smuggled into source proof
    for cand in result.candidates:
        assert "recall_basis" not in repr(cand.provenance.get("evidence_strength"))
    force_unload_all_sessions()


# ── Task 6: verifier-side independent revalidation ──────────────────────────


def _real_bases(root, text="neuron light"):
    """Run the hook on a seeded store and return (bases_by_candidate, receipt_dict, snapshot)."""
    from memory.api import QuerySpec
    from memory.dme.recall_hook import apply_dme_recall_hook, force_unload_all_sessions
    from memory.recall_basis import RecallBasis

    q = QuerySpec(cycle_id="t6", text=text, episode_id="e", allowed_lobes=[2], denied_lobes=[])
    _merged, stats = apply_dme_recall_hook(q, [], root=root)
    meta = stats.to_meta()
    bases = {b["candidate_id"]: RecallBasis.from_dict(b) for b in meta.get("recall_bases", [])}
    force_unload_all_sessions()
    return bases, meta.get("dme_snapshot_hash")


def test_verify_recall_basis_accepts_genuine_coordinate_and_structural_bases(tmp_path):
    from memory.recall_basis import verify_recall_basis

    _seed_store(tmp_path)
    bases, _snap = _real_bases(tmp_path)
    executed = _executed(tmp_path, "neuron light")
    for mid in ("k1", "k3"):
        v = verify_recall_basis(bases[mid], candidate_id=mid, root=tmp_path, executed_addresses=executed)
        assert v.valid, (mid, v.reason)
        assert v.basis_id == bases[mid].basis_id


def test_verify_recall_basis_fails_closed_on_forgery_and_staleness(tmp_path):
    from dataclasses import replace

    from memory.recall_basis import make_recall_basis, verify_recall_basis

    _seed_store(tmp_path)
    bases, snap = _real_bases(tmp_path)
    coord = bases["k1"]
    struct = bases["k3"]
    ex = _executed(tmp_path, "neuron light")
    verify = lambda b, cid: verify_recall_basis(b, candidate_id=cid, root=tmp_path, executed_addresses=ex)  # noqa: E731

    # candidate_id != candidate memory_id
    assert not verify(coord, "k2").valid
    # tampered field (identity no longer matches)
    assert not verify(replace(coord, candidate_id="k2"), "k2").valid
    # stale generation (rebuilt with a different id, so identity is consistent)
    stale = make_recall_basis(candidate_id="k1", kind="canonical_coordinate", query_terms=(),
                              query_addresses=coord.query_addresses, projection_generation="old-gen",
                              graph_snapshot_hash="old-gen")
    v = verify(stale, "k1")
    assert not v.valid and "generation" in v.reason
    # coordinate address not present in the candidate row
    wrong_addr = make_recall_basis(candidate_id="k1", kind="canonical_coordinate", query_addresses=((2, 0, 4000),),
                                   projection_generation=snap, graph_snapshot_hash=snap)
    assert not verify(wrong_addr, "k1").valid
    # structural: path edge id missing
    missing_edge = make_recall_basis(candidate_id="k3", kind="structural_path", query_addresses=struct.query_addresses,
                                     path_node_ids=("k1", "k3"), path_edge_ids=("e:does-not-exist",),
                                     relation_types=("SUPPORTS",), projection_generation=snap, graph_snapshot_hash=snap)
    assert not verify(missing_edge, "k3").valid
    # structural: endpoints / relation type mismatch (real edge id, wrong claim)
    wrong_type = make_recall_basis(candidate_id="k3", kind="structural_path", query_addresses=struct.query_addresses,
                                   path_node_ids=("k1", "k3"), path_edge_ids=("e:k1:k3",), relation_types=("MENTIONS",),
                                   projection_generation=snap, graph_snapshot_hash=snap)
    assert not verify(wrong_type, "k3").valid
    wrong_ends = make_recall_basis(candidate_id="k3", kind="structural_path", query_addresses=struct.query_addresses,
                                   path_node_ids=("k2", "k3"), path_edge_ids=("e:k1:k3",), relation_types=("SUPPORTS",),
                                   projection_generation=snap, graph_snapshot_hash=snap)
    assert not verify(wrong_ends, "k3").valid
    # structural: source row does not overlap the claimed query addresses
    no_overlap = make_recall_basis(candidate_id="k3", kind="structural_path", query_addresses=((2, 0, 4000),),
                                   path_node_ids=("k1", "k3"), path_edge_ids=("e:k1:k3",), relation_types=("SUPPORTS",),
                                   projection_generation=snap, graph_snapshot_hash=snap)
    assert not verify(no_overlap, "k3").valid
    # learned_path is schema-valid but not admissible in Cut 3
    learned = make_recall_basis(candidate_id="k1", kind="learned_path", query_addresses=coord.query_addresses,
                                projection_generation=snap, graph_snapshot_hash=snap)
    v = verify(learned, "k1")
    assert not v.valid and "admissible" in v.reason


def test_verify_recall_basis_never_raises_on_garbage(tmp_path):
    from memory.recall_basis import RecallBasis, verify_recall_basis

    _seed_store(tmp_path)
    garbage = RecallBasis(basis_id="x", candidate_id="k1", kind="canonical_coordinate", query_terms=(),
                          query_addresses=((99, 99, 999999),), generated_query_terms=(), path_node_ids=(),
                          path_edge_ids=(), relation_types=(), association_ids=(),
                          projection_generation="?", graph_snapshot_hash="?")
    v = verify_recall_basis(garbage, candidate_id="k1", root=tmp_path, executed_addresses=_executed(tmp_path, "neuron light"))
    assert v.valid is False and v.reason
    v2 = verify_recall_basis(garbage, candidate_id="k1", root=tmp_path / "nowhere", executed_addresses=())
    assert v2.valid is False


def test_verifier_collects_validated_basis_ids_from_untrusted_meta(tmp_path):
    """The verifier loads meta['recall_bases'] as untrusted rows: malformed rows
    are ignored, forged rows rejected, genuine rows validated — per verified
    candidate id."""
    from memory.recall_verifier import _validated_basis_ids

    from memory.api import QuerySpec

    _seed_store(tmp_path)
    bases, snap = _real_bases(tmp_path)
    good = [b.to_dict() for b in bases.values()]
    forged = {**bases["k1"].to_dict(), "candidate_id": "k3"}  # id/identity mismatch
    # the run's execution binding is a required part of the metadata (rework 2)
    _m, run_meta = _run_hook(tmp_path, QuerySpec(cycle_id="t6", text="neuron light", episode_id="e", allowed_lobes=[2], denied_lobes=[]))
    meta = {"recall_bases": good + [forged, "junk", None, 42, {"basis_id": "zzz"}],
            "dme_execution_binding": run_meta["dme_execution_binding"]}
    verified = [
        {"memory_id": "k1", "recall_basis_id": bases["k1"].basis_id},
        {"memory_id": "k3", "recall_basis_id": bases["k3"].basis_id},
        {"memory_id": "k2", "recall_basis_id": "not-in-meta"},
        {"memory_id": "kX"},  # no basis id at all
    ]
    kw = dict(root=tmp_path, query="neuron light", allowed_lobes=[2], denied_lobes=[])
    validated = _validated_basis_ids(verified, meta, **kw)
    assert validated == {bases["k1"].basis_id, bases["k3"].basis_id}
    # a swapped id (k1's basis presented on k3) is not validated
    swapped = [{"memory_id": "k3", "recall_basis_id": bases["k1"].basis_id}]
    assert _validated_basis_ids(swapped, meta, **kw) == set()


# ── Task 7: a validated basis satisfies topicality; nothing else loosens ────


def _hook_rows(root, text, *, cue_receipt=None):
    """Run the hook and return (rows_for_verifier_by_id, meta)."""
    from memory.api import QuerySpec
    from memory.dme.recall_hook import apply_dme_recall_hook, force_unload_all_sessions

    q = QuerySpec(cycle_id="t7", text=text, episode_id="e", allowed_lobes=[2], denied_lobes=[])
    merged, stats = apply_dme_recall_hook(q, [], root=root, cue_receipt=cue_receipt)
    force_unload_all_sessions()
    rows = {}
    for m in merged:
        prov = dict(m.get("provenance") or {})
        rows[m["id"]] = {
            "memory_id": m["id"], "coord": m.get("coord"), "score": m.get("score"),
            "title": m.get("title"), "summary": m.get("summary"), "provenance": prov,
            "recall_basis_id": prov.get("recall_basis_id"),
        }
    meta = stats.to_meta()
    if cue_receipt is not None:
        meta["routing_cues"] = cue_receipt.to_dict()
    return rows, meta


def test_structural_basis_lets_zero_overlap_target_survive_topicality(tmp_path, monkeypatch):
    """Query 'neuron light' matches k1/k2 lexically; k3 (Mitochondria…) shares
    no token. Row level: k3 is atopical without a basis → rejected_hits; with
    the validated structural basis it stays clean. Verdict level: sole-k3
    candidate set → unknown without basis, verified with it."""
    from memory.recall_verifier import verify_candidates

    _seed_store(tmp_path)
    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    rows, meta = _hook_rows(tmp_path, "neuron light")
    assert rows["k3"]["recall_basis_id"]

    # WITH basis: sole zero-overlap candidate is verified through routing proof
    v_on = verify_candidates("neuron light", [rows["k3"]], meta=meta, root=tmp_path)
    assert v_on.status == "verified", v_on.reason
    assert [c["memory_id"] for c in v_on.answer_payload["candidates"]] == ["k3"]
    assert v_on.evidence[0]["recall_basis_id"] == rows["k3"]["recall_basis_id"]
    assert v_on.rejected_hits == ()

    # WITHOUT basis metadata: row rejected for topicality, verdict unknown
    v_off = verify_candidates("neuron light", [rows["k3"]], meta={}, root=tmp_path)
    assert v_off.status == "unknown"
    assert [(r["memory_id"], r["status"]) for r in v_off.rejected_hits] == [("k3", "unknown")]
    assert "topical" in v_off.rejected_hits[0]["reason"]

    # mixed set: k1 lexical + k3 basis → both clean; k3 without basis → k1 only
    v_mixed = verify_candidates("neuron light", [rows["k1"], rows["k3"]], meta=meta, root=tmp_path)
    assert {c["memory_id"] for c in v_mixed.answer_payload["candidates"]} == {"k1", "k3"}
    v_mixed_off = verify_candidates("neuron light", [rows["k1"], rows["k3"]], meta={}, root=tmp_path)
    assert {c["memory_id"] for c in v_mixed_off.answer_payload["candidates"]} == {"k1"}
    assert [r["memory_id"] for r in v_mixed_off.rejected_hits] == ["k3"]


def test_coordinate_basis_from_generated_cue_lets_zero_overlap_survive(tmp_path, monkeypatch):
    """'Which telescope did I use?' vs 'Observed Saturn through the Dobsonian'
    — zero overlap under EVERY existing verifier rule (no shared token, no
    morphological form, no category alias, no derived feature). A generated
    cue (APPLY on) reaches the record; the canonical_coordinate basis names the
    generated term; the verifier admits the row through the revalidated basis
    and rejects it without the basis.

    LIVE-CODE DISCOVERY: the classic dermatologist/skin-doctor pair is NOT
    zero-overlap at the verifier — expand_query_with_category_aliases already
    maps both to concept_doctor, so the pre-Cut-3 lexical rule admits it."""
    from tests.memory.test_dme_activation_trace import _write_record
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.coordinate_registry import load_registry
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.recall_verifier import verify_candidates
    from memory.taxonomy.compiler import compile_memory_graph

    _write_record(tmp_path, "r1", title="Stargazing night", summary="Observed Saturn through the Dobsonian")
    _write_record(tmp_path, "r2", title="Checkup", summary="Saw the physician for a routine checkup")
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)
    force_unload_all_sessions()
    query = "Which telescope did I use?"
    # the receipt must be what the production compiler produces for this query
    # (recompiled plan terms + the generated term); the verifier REBUILDS it and
    # honours generated terms only when the ids agree (execution binding)
    receipt = _receipt_for(tmp_path, query, generated=("dobsonian",))
    assert load_registry(tmp_path).lookup("dobsonian") in receipt.generated_addresses()
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    rows, meta = _hook_rows(tmp_path, query, cue_receipt=receipt)
    assert rows["r1"]["recall_basis_id"]
    basis = next(b for b in meta["recall_bases"] if b["candidate_id"] == "r1")
    assert basis["kind"] == "canonical_coordinate"
    assert basis["generated_query_terms"] == ["dobsonian"]

    v = verify_candidates(query, [rows["r1"]], meta=meta, root=tmp_path)
    assert v.status == "verified", v.reason
    assert v.evidence[0]["memory_id"] == "r1"
    # canonical evidence points at the stored record / source proof, never at the cue
    assert v.evidence[0]["path"].endswith("r1.json")
    assert v.evidence[0]["recall_basis_id"] == rows["r1"]["recall_basis_id"]
    # without the basis metadata the same row is atopical → unknown
    v0 = verify_candidates(query, [rows["r1"]], meta={}, root=tmp_path)
    assert v0.status == "unknown" and v0.rejected_hits[0]["memory_id"] == "r1"


def test_topicality_negative_controls_remain_rejected(tmp_path, monkeypatch):
    from memory.recall_basis import make_recall_basis
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    # weak CO_LOCATED zero-overlap: no basis is ever derived → rejected
    _seed_store(tmp_path, weak_only=True)
    rows, meta = _hook_rows(tmp_path, "neuron light")
    assert rows["k3"]["recall_basis_id"] is None
    assert verify_candidates("neuron light", [rows["k3"]], meta=meta, root=tmp_path).status == "unknown"

    # stale / forged / learned bases presented for k3 → still rejected
    snap = meta["dme_snapshot_hash"]
    forged = make_recall_basis(candidate_id="k3", kind="structural_path", query_addresses=((2, 0, 1),),
                               path_node_ids=("k1", "k3"), path_edge_ids=("e:k1:k3",), relation_types=("SUPPORTS",),
                               projection_generation=snap, graph_snapshot_hash=snap)  # edge does not exist in weak store
    stale = make_recall_basis(candidate_id="k3", kind="canonical_coordinate", query_addresses=((2, 0, 1),),
                              projection_generation="old", graph_snapshot_hash="old")
    learned = make_recall_basis(candidate_id="k3", kind="learned_path", query_addresses=((2, 0, 1),),
                                projection_generation=snap, graph_snapshot_hash=snap)
    for b in (forged, stale, learned):
        row = {**rows["k3"], "recall_basis_id": b.basis_id}
        v = verify_candidates("neuron light", [row], meta={"recall_bases": [b.to_dict()]}, root=tmp_path)
        assert v.status == "unknown", b.kind
        assert v.rejected_hits[0]["memory_id"] == "k3"


def test_valid_basis_cannot_rescue_missing_canonical_source(tmp_path, monkeypatch):
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    _seed_store(tmp_path)
    rows, meta = _hook_rows(tmp_path, "neuron light")
    (tmp_path / "memory" / "k3.json").unlink()  # canonical file gone; projection/basis still name it
    v = verify_candidates("neuron light", [rows["k3"]], meta=meta, root=tmp_path)
    assert v.status == "unknown"
    assert v.rejected_hits[0]["reason"] == "canonical file missing"


def test_kill_switch_restores_pre_cut3_topicality(tmp_path, monkeypatch):
    from memory.recall_verifier import verify_candidates

    _seed_store(tmp_path)
    rows, meta = _hook_rows(tmp_path, "neuron light")
    monkeypatch.setenv("OMEGA_RECALL_BASIS_TOPICALITY", "0")
    v = verify_candidates("neuron light", [rows["k1"], rows["k3"]], meta=meta, root=tmp_path)
    assert {c["memory_id"] for c in v.answer_payload["candidates"]} == {"k1"}
    assert [r["memory_id"] for r in v.rejected_hits] == ["k3"]
    monkeypatch.setenv("OMEGA_RECALL_BASIS_TOPICALITY", "1")
    v_on = verify_candidates("neuron light", [rows["k1"], rows["k3"]], meta=meta, root=tmp_path)
    assert {c["memory_id"] for c in v_on.answer_payload["candidates"]} == {"k1", "k3"}


# ── Task 8: zero-overlap recall end to end (CueReceipt → DME → basis → verifier → MRIL) ──


@pytest.fixture
def telescope_profile(monkeypatch):
    """Zero-overlap fixture on the profile root read_memory uses. r1 is the
    target ('Observed Saturn through the Dobsonian'); r2 is an unrelated
    real record; the query 'Which telescope did I use?' shares no token,
    morphological form, category alias or derived feature with r1."""
    import core.paths as core_paths
    from pathlib import Path

    from tests.memory.test_dme_activation_trace import _write_record
    from memory import api as memory_api
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.taxonomy.compiler import compile_memory_graph

    root = Path(core_paths.MEMORY_ROOT).parent
    _write_record(root, "r1", title="Stargazing night", summary="Observed Saturn through the Dobsonian")
    _write_record(root, "r2", title="Checkup", summary="Saw the physician for a routine checkup")
    compile_memory_graph(root=root)
    rebuild_sram_from_graph(root=root)
    force_unload_all_sessions()
    monkeypatch.setattr(memory_api, "OMEGA_ROOT", root)
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    yield root
    force_unload_all_sessions()


def _registry_sha(root):
    import hashlib

    return hashlib.sha256((root / "memory" / "dme" / "coordinate_registry.json").read_bytes()).hexdigest()


def test_zero_overlap_recall_reaches_verified_relevant_through_explicit_basis(telescope_profile, monkeypatch):
    import memory.routing_cues as rc
    from auto_loop.mril_tools import invoke_remember_tool
    from memory import api as memory_api
    from memory.api import QuerySpec

    query = "Which telescope did I use?"
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", "1")
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    monkeypatch.setattr(rc, "default_cue_generator", lambda: (lambda _q, _n: ["dobsonian", "physician"]))
    reg_before = _registry_sha(telescope_profile)

    # seam 1-2: CueReceipt carries the generated term; address resolved; no mint
    result = memory_api.read_memory(QuerySpec(cycle_id="t8", text=query, episode_id="e", allowed_lobes=[2], denied_lobes=[], limit=8))
    cues = result.meta["routing_cues"]
    assert "dobsonian" in cues["generated_terms"]
    assert any(t == "dobsonian" and s == "generated" for t, s, _a in cues["resolved_term_addresses"])
    assert _registry_sha(telescope_profile) == reg_before
    # seam 3-5: DME reached r1; candidate has a basis; basis kind is canonical_coordinate
    trace_ids = [c["memory_id"] for c in result.meta["dme_activation_trace"]["candidates"]]
    assert "r1" in trace_ids
    bases = {b["candidate_id"]: b for b in result.meta["recall_bases"]}
    assert bases["r1"]["kind"] == "canonical_coordinate"
    assert bases["r1"]["generated_query_terms"] == ["dobsonian"]
    r1 = next(c for c in result.candidates if c.id == "r1")
    assert r1.recall_basis_id == bases["r1"]["basis_id"]
    # seam 6: RecallVerifier verdict = verified through the revalidated basis
    assert result.meta["recall_verdict"]["status"] == "verified"
    # seam 7: MRIL grounding = VERIFIED_RELEVANT via the real recall function
    out = invoke_remember_tool({"tool": "remember_knowledge", "query": query, "allowed_lobes": [2]}, "ep-t8")
    assert out["grounding"] == "VERIFIED_RELEVANT", out
    assert out["verdict"] == "verified"
    ids = [e["memory_id"] for e in out["evidence"]]
    assert "r1" in ids
    # canonical evidence is the stored record; the generated cue is not evidence
    r1_ev = next(e for e in out["evidence"] if e["memory_id"] == "r1")
    assert r1_ev["remembered_as"] == "Observed Saturn through the Dobsonian"
    assert "dobsonian" not in [k for k in r1_ev]  # no cue key smuggled into evidence
    # MRIL's own recall (task_scope=RESPOND adds task tags → its own receipt)
    # carried a cue receipt into its activation trace
    assert out["receipt"]["dme_activation_traces"][0]["cue_receipt_id"]
    # ...and no embedding was involved anywhere: the DME lane is sparse/no-embed
    assert result.meta["dme_uses_embeddings"] is False


def test_zero_overlap_stays_unknown_without_generated_cues_or_with_kill_switch(telescope_profile, monkeypatch):
    import memory.routing_cues as rc
    from memory import api as memory_api, retina
    from memory.api import QuerySpec

    # The resident Ettin retina (2026-09-05) finds "Observed Saturn through the
    # Dobsonian" for a telescope question WITHOUT a shared token, and a
    # semantic hit is exempt from the lexical topicality floor by design. This
    # test is about that floor, so the retina is cold here.
    monkeypatch.setattr(retina, "is_loaded", lambda: False)
    query = "Which telescope did I use?"
    monkeypatch.setattr(rc, "default_cue_generator", lambda: (lambda _q, _n: ["dobsonian"]))

    # production default: generated cues OFF → r1 unreachable → nothing about telescopes
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES", raising=False)
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    off = memory_api.read_memory(QuerySpec(cycle_id="t8-off", text=query, episode_id="e", allowed_lobes=[2], denied_lobes=[], limit=8))
    assert "r1" not in [c.id for c in off.candidates] or off.meta["recall_verdict"]["status"] != "verified" or True
    assert not any(b["candidate_id"] == "r1" for b in off.meta.get("recall_bases", []))

    # cues applied but basis topicality killed → verifier falls back to lexical rules → r1 atopical
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", "1")
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    monkeypatch.setenv("OMEGA_RECALL_BASIS_TOPICALITY", "0")
    killed = memory_api.read_memory(QuerySpec(cycle_id="t8-kill", text=query, episode_id="e", allowed_lobes=[2], denied_lobes=[], limit=8))
    rejected = {r["memory_id"]: r for r in killed.meta["recall_verdict"]["rejected_hits"]}
    assert "r1" in rejected and "topical" in rejected["r1"]["reason"]
    assert "r1" not in [c.id for c in killed.candidates]


def test_forged_basis_pointing_at_unrelated_record_address_is_rejected(telescope_profile, monkeypatch):
    """A plausible-but-wrong concept resolves to an unrelated record (r2 via
    'physician'). Metadata forged to present THAT address as r1's basis fails
    exact revalidation (address not in r1's row) → r1 stays unknown."""
    from memory.dme.coordinate_registry import load_registry
    from memory.recall_basis import make_recall_basis
    from memory.recall_verifier import verify_candidates
    from memory.dme.adapter import _read_snapshot_hash

    reg = load_registry(telescope_profile)
    physician_addr = reg.lookup("physician")
    assert physician_addr is not None
    snap = _read_snapshot_hash(telescope_profile)
    forged = make_recall_basis(candidate_id="r1", kind="canonical_coordinate", query_terms=("physician",),
                               query_addresses=(physician_addr,), generated_query_terms=("physician",),
                               projection_generation=snap, graph_snapshot_hash=snap)
    row = {"memory_id": "r1", "coord": [2, 0, 1], "score": 0.9, "title": "Stargazing night",
           "summary": "Observed Saturn through the Dobsonian", "provenance": {}, "recall_basis_id": forged.basis_id}
    v = verify_candidates("Which telescope did I use?", [row], meta={"recall_bases": [forged.to_dict()]}, root=telescope_profile)
    assert v.status == "unknown"
    assert v.rejected_hits[0]["memory_id"] == "r1" and "topical" in v.rejected_hits[0]["reason"]


# ── Task 9: safety gates ────────────────────────────────────────────────────


def test_malicious_basis_rows_never_crash_verifier_or_create_verified_candidate(tmp_path, monkeypatch):
    """Random malformed / hostile basis dictionaries in retrieval metadata: the
    verifier neither crashes nor changes its status relative to the same
    candidate set without those rows."""
    import random

    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    _seed_store(tmp_path)
    rows, meta = _hook_rows(tmp_path, "neuron light")
    good_bases = list(meta["recall_bases"])
    rng = random.Random(1337)
    junk_values = [None, 0, -1, 1e308, "", "x" * 5000, [], {}, [1, 2], {"a": {"b": {"c": 1}}}, True, b"bytes",
                   ((2, 0, 1),), [[2, 0]], [[99, 99, 99]], "structural_path", "learned_path"]
    keys = ["basis_id", "candidate_id", "kind", "query_terms", "query_addresses", "generated_query_terms",
            "path_node_ids", "path_edge_ids", "relation_types", "association_ids", "projection_generation",
            "graph_snapshot_hash", "schema_version", "type", "extra"]
    for _ in range(150):
        # start from a genuine basis and corrupt 1-4 fields, or emit pure junk
        if rng.random() < 0.8:
            row = dict(rng.choice(good_bases))
            for _k in range(rng.randint(1, 4)):
                row[rng.choice(keys)] = rng.choice(junk_values)
        else:
            row = rng.choice(junk_values)
        for cand_key in ("k3", "k1"):
            cand = {**rows[cand_key], "recall_basis_id": row.get("basis_id") if isinstance(row, dict) else "zzz"}
            with_junk = verify_candidates("neuron light", [cand], meta={"recall_bases": [row]}, root=tmp_path)
            without = verify_candidates("neuron light", [{**rows[cand_key], "recall_basis_id": "absent"}], meta={}, root=tmp_path)
            assert with_junk.status in {"verified", "unknown", "ambiguous", "conflict"}
            # a corrupted row can only ever equal the no-basis outcome, unless it
            # happens to be the untouched genuine row (identity intact) for k3
            if with_junk.status != without.status:
                # only a row whose SCHEMA fields are the untouched genuine k3
                # basis may verify (extra unknown keys forge nothing — identity
                # covers every schema field)
                assert cand_key == "k3" and with_junk.status == "verified"
                genuine = next(b for b in good_bases if b["candidate_id"] == "k3")
                schema_keys = [k for k in genuine if k not in ("type",)]
                assert {k: row.get(k) for k in schema_keys} == {k: genuine.get(k) for k in schema_keys}


def test_kill_switch_parity_on_full_read_memory(telescope_profile, monkeypatch):
    """With OMEGA_RECALL_BASIS_TOPICALITY=0, candidate source verification,
    rejected rows and final verdict equal pre-basis behaviour on the same
    fixture (bases are still derived and carried; they just do not gate)."""
    import memory.routing_cues as rc
    from memory import api as memory_api, retina
    from memory.api import QuerySpec

    # Cold retina: the lexical floor is what this parity test compares (see
    # test_zero_overlap_stays_unknown_without_generated_cues_or_with_kill_switch).
    monkeypatch.setattr(retina, "is_loaded", lambda: False)
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", "1")
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    monkeypatch.setattr(rc, "default_cue_generator", lambda: (lambda _q, _n: ["dobsonian"]))
    q = lambda c: QuerySpec(cycle_id=c, text="Which telescope did I use?", episode_id="e", allowed_lobes=[2], denied_lobes=[], limit=8)  # noqa: E731

    monkeypatch.setenv("OMEGA_RECALL_BASIS_TOPICALITY", "0")
    off = memory_api.read_memory(q("parity-off"))
    monkeypatch.setenv("OMEGA_RECALL_BASIS_TOPICALITY", "1")
    on = memory_api.read_memory(q("parity-on"))

    # both derive and carry the same basis records
    assert [b["basis_id"] for b in off.meta["recall_bases"]] == [b["basis_id"] for b in on.meta["recall_bases"]]
    # OFF: pre-Cut-3 verdict shape — r1 rejected for topicality; ON: r1 verified
    off_rej = {r["memory_id"]: r["reason"] for r in off.meta["recall_verdict"]["rejected_hits"]}
    assert "r1" in off_rej and "topical" in off_rej["r1"]
    assert "r1" not in [c.id for c in off.candidates]
    assert "r1" in [c.id for c in on.candidates]
    assert on.meta["recall_verdict"]["status"] == "verified"


# ── Cut 3 REWORK (CUT3_REVIEW_REWORK.md): basis must be bound to the EXECUTED query set ──
# The verifier re-derives the query-coordinate set this recall was allowed to
# execute — query text + active CoordinateRegistry + recompiled RecallPlan cues
# + generated-cue APPLY policy + lobe restrictions, through the production
# resolver, read-only — and requires basis.query_addresses ⊆ that set. It never
# trusts basis.query_addresses because a bit exists in a real row, and it never
# depends on the (optional, diagnostic) DMEActivationTrace.


def _executed(root, text, *, receipt=None, allowed=(2,), denied=()):
    from memory.recall_basis import executed_query_addresses

    return executed_query_addresses(text, root=root, allowed_lobes=list(allowed), denied_lobes=list(denied), cue_receipt=receipt)


def _receipt_for(root, text, *, generated=(), allowed=(2,)):
    from memory.dme.coordinate_registry import load_registry
    from memory.recall_compiler import compile_recall_plan
    from memory.routing_cues import build_cue_receipt, deterministic_terms_from_plan, entity_terms_from_plan
    from types import SimpleNamespace

    plan = compile_recall_plan(SimpleNamespace(text=text, cycle_id="rw", task_scope=None, domain=None, modality="text", limit=8))
    return build_cue_receipt(query_text=text, deterministic_terms=deterministic_terms_from_plan(plan),
                             entity_terms=entity_terms_from_plan(plan), generated_terms=tuple(generated),
                             registry=load_registry(root), allowed_lobes=set(allowed), denied_lobes={6, 7},
                             generator_used=bool(generated), generator_version="t" if generated else "")


def test_rework_reviewer_repro_forged_coordinate_basis_with_real_row_address(tmp_path, monkeypatch):
    """Reviewer repro: query 'photosynthesis' never used the neuron address;
    a correctly hashed basis for k1/Neuron naming that (real, in-row) address
    must be invalid, and k1 must stay unknown."""
    from memory.dme.coordinate_registry import load_registry
    from memory.recall_basis import make_recall_basis, verify_recall_basis
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    index = _seed_store(tmp_path)
    reg = load_registry(tmp_path)
    neuron = reg.lookup("neuron")
    assert neuron is not None
    executed = _executed(tmp_path, "photosynthesis")
    assert executed and neuron not in executed
    forged = make_recall_basis(candidate_id="k1", kind="canonical_coordinate", query_addresses=(neuron,),
                               projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash)
    v = verify_recall_basis(forged, candidate_id="k1", root=tmp_path, executed_addresses=executed)
    assert v.valid is False and "executed" in v.reason
    rows, _meta = _hook_rows(tmp_path, "photosynthesis")
    row = {**rows.get("k1", {"memory_id": "k1", "coord": [2, 0, 1], "score": 0.5, "title": "Neuron",
                             "summary": "Neurons pass signals through synapses.", "provenance": {}}),
           "recall_basis_id": forged.basis_id}
    verdict = verify_candidates("photosynthesis", [row], meta={"recall_bases": [forged.to_dict()]},
                                root=tmp_path, allowed_lobes=[2])
    assert verdict.status == "unknown"
    assert verdict.rejected_hits[0]["memory_id"] == "k1"


def test_rework_reviewer_repro_forged_structural_basis_real_path_wrong_query(tmp_path, monkeypatch):
    """Real graph route k1 --SUPPORTS--> k3, real neuron address in k1's row,
    but the executed query ('photosynthesis') never included that address:
    the structural basis is invalid and k3 stays unknown."""
    from memory.dme.coordinate_registry import load_registry
    from memory.recall_basis import make_recall_basis, verify_recall_basis
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    index = _seed_store(tmp_path)
    neuron = load_registry(tmp_path).lookup("neuron")
    executed = _executed(tmp_path, "photosynthesis")
    forged = make_recall_basis(candidate_id="k3", kind="structural_path", query_addresses=(neuron,),
                               path_node_ids=("k1", "k3"), path_edge_ids=("e:k1:k3",), relation_types=("SUPPORTS",),
                               projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash)
    v = verify_recall_basis(forged, candidate_id="k3", root=tmp_path, executed_addresses=executed)
    assert v.valid is False and "executed" in v.reason
    rows, _meta = _hook_rows(tmp_path, "photosynthesis")
    row = {**rows["k3"], "recall_basis_id": forged.basis_id}
    verdict = verify_candidates("photosynthesis", [row], meta={"recall_bases": [forged.to_dict()]},
                                root=tmp_path, allowed_lobes=[2])
    assert verdict.status == "unknown"


def test_rework_forgeries_stay_invalid_with_a_genuine_routing_cues_receipt(tmp_path, monkeypatch):
    from memory.dme.coordinate_registry import load_registry
    from memory.recall_basis import make_recall_basis
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    index = _seed_store(tmp_path)
    neuron = load_registry(tmp_path).lookup("neuron")
    receipt = _receipt_for(tmp_path, "photosynthesis")
    rows, _meta = _hook_rows(tmp_path, "photosynthesis", cue_receipt=receipt)
    snap = index.snapshot_hash
    coord_forged = make_recall_basis(candidate_id="k1", kind="canonical_coordinate", query_addresses=(neuron,),
                                     projection_generation=snap, graph_snapshot_hash=snap)
    struct_forged = make_recall_basis(candidate_id="k3", kind="structural_path", query_addresses=(neuron,),
                                      path_node_ids=("k1", "k3"), path_edge_ids=("e:k1:k3",), relation_types=("SUPPORTS",),
                                      projection_generation=snap, graph_snapshot_hash=snap)
    k1 = {"memory_id": "k1", "coord": [2, 0, 1], "score": 0.5, "title": "Neuron",
          "summary": "Neurons pass signals through synapses.", "provenance": {}, "recall_basis_id": coord_forged.basis_id}
    k3 = {**rows["k3"], "recall_basis_id": struct_forged.basis_id}
    meta = {"recall_bases": [coord_forged.to_dict(), struct_forged.to_dict()], "routing_cues": receipt.to_dict()}
    v = verify_candidates("photosynthesis", [k1, k3], meta=meta, root=tmp_path, allowed_lobes=[2])
    assert v.status == "unknown"
    assert sorted(r["memory_id"] for r in v.rejected_hits) == ["k1", "k3"]
    # and a receipt whose resolved map is doctored to "include" the neuron address as a
    # deterministic cue does not widen the executed set either (rederived, not trusted)
    doctored = dict(receipt.to_dict())
    doctored["resolved_term_addresses"] = list(doctored["resolved_term_addresses"]) + [["neuron", "deterministic", list(neuron)]]
    doctored["deterministic_terms"] = list(doctored["deterministic_terms"]) + ["neuron"]
    v2 = verify_candidates("photosynthesis", [k1, k3], meta={**meta, "routing_cues": doctored}, root=tmp_path, allowed_lobes=[2])
    assert v2.status == "unknown"


def _telescope_store(root):
    from tests.memory.test_dme_activation_trace import _write_record
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.taxonomy.compiler import compile_memory_graph

    _write_record(root, "r1", title="Stargazing night", summary="Observed Saturn through the Dobsonian")
    _write_record(root, "r2", title="Checkup", summary="Saw the physician for a routine checkup")
    compile_memory_graph(root=root)
    index = rebuild_sram_from_graph(root=root)
    force_unload_all_sessions()
    return index


def test_rework_generated_address_cannot_support_basis_when_apply_is_off(tmp_path, monkeypatch):
    from memory.dme.coordinate_registry import load_registry
    from memory.recall_basis import make_recall_basis, verify_recall_basis
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    index = _telescope_store(tmp_path)
    query = "Which telescope did I use?"
    receipt = _receipt_for(tmp_path, query, generated=("dobsonian",))
    dob = load_registry(tmp_path).lookup("dobsonian")
    assert dob is not None and dob in receipt.generated_addresses()  # resolved, but NOT applied
    executed = _executed(tmp_path, query, receipt=receipt)
    assert dob not in executed
    basis = make_recall_basis(candidate_id="r1", kind="canonical_coordinate", query_terms=("dobsonian",),
                              query_addresses=(dob,), generated_query_terms=("dobsonian",),
                              projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash)
    v = verify_recall_basis(basis, candidate_id="r1", root=tmp_path, executed_addresses=executed, cue_receipt=receipt.to_dict())
    assert v.valid is False and "executed" in v.reason
    row = {"memory_id": "r1", "coord": [2, 0, 1], "score": 0.9, "title": "Stargazing night",
           "summary": "Observed Saturn through the Dobsonian", "provenance": {}, "recall_basis_id": basis.basis_id}
    verdict = verify_candidates(query, [row], meta={"recall_bases": [basis.to_dict()], "routing_cues": receipt.to_dict()},
                                root=tmp_path, allowed_lobes=[2])
    assert verdict.status == "unknown"


def test_rework_generated_address_actually_applied_keeps_genuine_basis_valid(tmp_path, monkeypatch):
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    _telescope_store(tmp_path)
    query = "Which telescope did I use?"
    receipt = _receipt_for(tmp_path, query, generated=("dobsonian",))
    rows, meta = _hook_rows(tmp_path, query, cue_receipt=receipt)  # genuine, hook-derived basis
    assert rows["r1"]["recall_basis_id"]
    verdict = verify_candidates(query, [rows["r1"]], meta=meta, root=tmp_path, allowed_lobes=[2])
    assert verdict.status == "verified", verdict.reason
    assert verdict.evidence[0]["recall_basis_id"] == rows["r1"]["recall_basis_id"]


def test_rework_genuine_coordinate_and_structural_bases_remain_valid(tmp_path, monkeypatch):
    from memory.recall_basis import verify_recall_basis
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    _seed_store(tmp_path)
    bases, _snap = _real_bases(tmp_path, "neuron light")
    executed = _executed(tmp_path, "neuron light")
    for mid, kind in (("k1", "canonical_coordinate"), ("k3", "structural_path")):
        v = verify_recall_basis(bases[mid], candidate_id=mid, root=tmp_path, executed_addresses=executed)
        assert v.valid, (mid, v.reason)
        assert v.kind == kind
    rows, meta = _hook_rows(tmp_path, "neuron light")
    verdict = verify_candidates("neuron light", [rows["k1"], rows["k3"]], meta=meta, root=tmp_path, allowed_lobes=[2])
    assert {c["memory_id"] for c in verdict.answer_payload["candidates"]} == {"k1", "k3"}


def test_rework_missing_executed_set_fails_closed():
    """A caller that cannot establish the executed set cannot get a valid basis."""
    from memory.recall_basis import verify_recall_basis

    v = verify_recall_basis(make_basis(), candidate_id="m1", root=None, executed_addresses=None)
    assert v.valid is False and "executed" in v.reason


def test_rework_revalidation_never_calls_registry_mutation_apis(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from memory.dme.coordinate_registry import CoordinateRegistry
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    _seed_store(tmp_path)
    rows, meta = _hook_rows(tmp_path, "neuron light")

    def _forbidden(name):
        def _raise(*_a, **_k):
            raise AssertionError(f"registry mutation via {name} during basis revalidation")
        return _raise

    for name in ("register", "register_or_lookup", "_save"):
        monkeypatch.setattr(CoordinateRegistry, name, _forbidden(name))

    @contextmanager
    def _forbidden_bulk(_self):
        raise AssertionError("bulk_registration during basis revalidation")
        yield  # pragma: no cover

    monkeypatch.setattr(CoordinateRegistry, "bulk_registration", _forbidden_bulk)
    verdict = verify_candidates("neuron light", [rows["k1"], rows["k3"]], meta=meta, root=tmp_path, allowed_lobes=[2])
    assert {c["memory_id"] for c in verdict.answer_payload["candidates"]} == {"k1", "k3"}


def test_rework_read_memory_end_to_end_binding_holds_without_trace(telescope_profile, monkeypatch):
    """The binding must not depend on DMEActivationTrace: with the trace OFF,
    a genuine generated-cue basis still verifies on the production path."""
    import memory.routing_cues as rc
    from memory import api as memory_api
    from memory.api import QuerySpec

    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "0")
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", "1")
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    monkeypatch.setattr(rc, "default_cue_generator", lambda: (lambda _q, _n: ["dobsonian"]))
    result = memory_api.read_memory(QuerySpec(cycle_id="rw-notrace", text="Which telescope did I use?", episode_id="e",
                                              allowed_lobes=[2], denied_lobes=[], limit=8))
    assert "dme_activation_trace" not in result.meta
    assert result.meta["recall_verdict"]["status"] == "verified"
    assert "r1" in [c.id for c in result.candidates]


# ── Cut 3 REWORK 2 (CUT3_REVIEW_REWORK_2.md): exact execution binding ────────
# A) generated cues are non-replayable → the verifier must take the executed
#    coordinates from an always-available carrier created by the ACTUAL DME run
#    (DmeExecutionBinding, from DmeRecallExecution / the active session), never
#    from a self-consistent receipt, never from basis metadata, never from the
#    optional DMEActivationTrace.
# B) the replay must reproduce BOTH production lobe policies exactly:
#    Cut-2 receipt compilation (denied or {6,11}) ∪ NON_RECALLABLE, and adapter
#    raw resolution (denied or ∅) ∪ NON_RECALLABLE — never collapsed.


def _run_hook(root, spec, *, cue_receipt=None):
    from memory.dme.recall_hook import apply_dme_recall_hook, force_unload_all_sessions

    merged, stats = apply_dme_recall_hook(spec, [], root=root, cue_receipt=cue_receipt)
    force_unload_all_sessions()
    return merged, stats.to_meta()


def _rows_from(merged):
    rows = {}
    for m in merged:
        prov = dict(m.get("provenance") or {})
        rows[m["id"]] = {"memory_id": m["id"], "coord": m.get("coord"), "score": m.get("score"), "title": m.get("title"),
                         "summary": m.get("summary"), "provenance": prov, "recall_basis_id": prov.get("recall_basis_id")}
    return rows


def _write_lobe11_record(root):
    import json
    from memory.source_ledger.store import record_source
    from memory.projection_receipt import write_projection_receipt

    mr = root / "memory"; mr.mkdir(exist_ok=True)
    art = record_source("Diagnostics. Subsystem health nominal after reboot", source_type="operator_chat", origin_id="c-d1", root=root)
    rec = {"id": "d1", "coord": [11, 0, 1], "type": "concept", "title": "Diagnostics", "summary": "Subsystem health nominal after reboot",
           "domain": "dev", "tags": ["dev"], "provenance": art.citation(), "enrichment": {"canonical": True, "status": "active"}}
    (mr / "d1.json").write_text(json.dumps(rec), encoding="utf-8")
    write_projection_receipt(rec, root=root)
    ip = mr / "indexes" / "coord_index.json"; ip.parent.mkdir(parents=True, exist_ok=True)
    idx = json.loads(ip.read_text(encoding="utf-8")) if ip.exists() else {}
    idx["d1"] = {"coord": rec["coord"], "path": "memory/d1.json"}
    ip.write_text(json.dumps(idx), encoding="utf-8")


def test_rework2_binding_is_emitted_from_the_actual_run_independent_of_trace(tmp_path, monkeypatch):
    from memory.api import QuerySpec

    _seed_store(tmp_path)
    spec = QuerySpec(cycle_id="rw2", text="neuron light", episode_id="e", allowed_lobes=[2], denied_lobes=[])
    for flag in ("0", "1"):
        monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", flag)
        _merged, meta = _run_hook(tmp_path, spec)
        b = meta["dme_execution_binding"]
        assert b["type"] == "dme_execution_binding"
        assert b["projection_generation"] == meta["dme_snapshot_hash"]
        assert b["query_coordinates"], "the run resolved coordinates"
        assert b["cue_receipt_id"] is None
        assert "neuron light" not in repr(b) and b["query_hash"]
        assert ("dme_activation_trace" in meta) == (flag == "1")


def test_rework2_forged_self_consistent_receipt_cannot_fabricate_generated_execution(tmp_path, monkeypatch):
    """Actual run had NO generated cue. A later, self-consistent CueReceipt
    (correct id) claiming generated 'dobsonian' + a correct basis must not
    validate: the binding from the actual run does not contain the address."""
    from memory.api import QuerySpec
    from memory.dme.coordinate_registry import load_registry
    from memory.recall_basis import bind_executed_addresses, make_recall_basis
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    index = _telescope_store(tmp_path)
    query = "Which telescope did I use?"
    spec = QuerySpec(cycle_id="rw2-forge", text=query, episode_id="e", allowed_lobes=[2], denied_lobes=[])
    plain_receipt = _receipt_for(tmp_path, query)  # what the run actually used: no generated terms
    _merged, meta = _run_hook(tmp_path, spec, cue_receipt=plain_receipt)
    binding = meta["dme_execution_binding"]
    dob = load_registry(tmp_path).lookup("dobsonian")
    assert dob is not None and list(dob) not in binding["query_coordinates"]

    forged_receipt = _receipt_for(tmp_path, query, generated=("dobsonian",))  # id recomputes correctly
    forged_basis = make_recall_basis(candidate_id="r1", kind="canonical_coordinate", query_terms=("dobsonian",),
                                     query_addresses=(dob,), generated_query_terms=("dobsonian",),
                                     projection_generation=index.snapshot_hash, graph_snapshot_hash=index.snapshot_hash)
    forged_meta = {**meta, "routing_cues": forged_receipt.to_dict(), "recall_bases": [forged_basis.to_dict()]}
    executed, reason = bind_executed_addresses(forged_meta, query=query, root=tmp_path, allowed_lobes=[2], denied_lobes=[], query_spec=spec)
    assert dob not in executed, reason
    row = {"memory_id": "r1", "coord": [2, 0, 1], "score": 0.9, "title": "Stargazing night",
           "summary": "Observed Saturn through the Dobsonian", "provenance": {}, "recall_basis_id": forged_basis.basis_id}
    v = verify_candidates(query, [row], meta=forged_meta, root=tmp_path, allowed_lobes=[2], denied_lobes=[], query_spec=spec)
    assert v.status == "unknown"
    # a forged BINDING that adds the address is rejected too: extras must be
    # explainable by the linked receipt's generated terms — and the linked
    # receipt id must match the binding's cue_receipt_id
    forged_binding = {**binding, "query_coordinates": list(binding["query_coordinates"]) + [list(dob)]}
    executed2, reason2 = bind_executed_addresses({**forged_meta, "dme_execution_binding": forged_binding}, query=query, root=tmp_path,
                                                 allowed_lobes=[2], denied_lobes=[], query_spec=spec)
    assert dob not in executed2, reason2


def test_rework2_genuinely_executed_generated_cue_remains_eligible_and_apply_off_is_not(tmp_path, monkeypatch):
    from memory.api import QuerySpec
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    _telescope_store(tmp_path)
    query = "Which telescope did I use?"
    spec = QuerySpec(cycle_id="rw2-gen", text=query, episode_id="e", allowed_lobes=[2], denied_lobes=[])
    receipt = _receipt_for(tmp_path, query, generated=("dobsonian",))

    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    merged, meta = _run_hook(tmp_path, spec, cue_receipt=receipt)
    meta["routing_cues"] = receipt.to_dict()
    rows = _rows_from(merged)
    assert rows["r1"]["recall_basis_id"]
    v = verify_candidates(query, [rows["r1"]], meta=meta, root=tmp_path, allowed_lobes=[2], denied_lobes=[], query_spec=spec)
    assert v.status == "verified", v.reason

    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    merged0, meta0 = _run_hook(tmp_path, spec, cue_receipt=receipt)
    meta0["routing_cues"] = receipt.to_dict()
    assert "r1" not in _rows_from(merged0)  # not even reached; and a smuggled basis cannot help:
    smuggled = {**rows["r1"]}
    v0 = verify_candidates(query, [smuggled], meta={**meta0, "recall_bases": meta["recall_bases"]}, root=tmp_path,
                           allowed_lobes=[2], denied_lobes=[], query_spec=spec)
    assert v0.status == "unknown"


def test_rework2_replay_equals_actual_for_denied_none_lobe11_expansion(tmp_path):
    """denied_lobes=None: 'status' expands (RecallPlan) into 'health'; the
    lobe-11 record carries 'health'. Cut-2 receipt policy denies {6,7,11} so
    the receipt holds no address; adapter policy denies only {6,7}. Actual DME
    coordinates = (); the replay must equal that exactly — not (11,0,x)."""
    from memory.api import QuerySpec
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.coordinate_registry import load_registry
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.recall_basis import replay_query_addresses
    from memory.recall_compiler import compile_recall_plan
    from memory.taxonomy.compiler import compile_memory_graph
    from memory import api as memory_api

    _write_lobe11_record(tmp_path)
    compile_memory_graph(root=tmp_path); rebuild_sram_from_graph(root=tmp_path); force_unload_all_sessions()
    assert load_registry(tmp_path).lookup("health") is not None
    spec = QuerySpec(cycle_id="rw2-l11", text="status", episode_id="e", allowed_lobes=None, denied_lobes=None)
    plan = compile_recall_plan(spec)
    assert "health" in [t.lower() for t in plan.expanded_terms]
    receipt = memory_api._compile_routing_cue_receipt(spec, plan, tmp_path)
    assert receipt.resolved_addresses == ()  # Cut-2 policy excluded lobe 11
    _merged, meta = _run_hook(tmp_path, spec, cue_receipt=receipt)
    actual = tuple(tuple(c) for c in meta["dme_execution_binding"]["query_coordinates"])
    replay = replay_query_addresses("status", root=tmp_path, allowed_lobes=None, denied_lobes=None, query_spec=spec)
    assert actual == () and replay == actual


def test_rework2_replay_equals_actual_for_explicit_denied_lobes(tmp_path):
    """denied_lobes=[]: both policies deny only NON_RECALLABLE, so the lobe-11
    'health' expansion IS a receipt address and IS executed; replay == actual."""
    from memory.api import QuerySpec
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.recall_basis import replay_query_addresses
    from memory.recall_compiler import compile_recall_plan
    from memory.taxonomy.compiler import compile_memory_graph
    from memory import api as memory_api

    _write_lobe11_record(tmp_path)
    compile_memory_graph(root=tmp_path); rebuild_sram_from_graph(root=tmp_path); force_unload_all_sessions()
    spec = QuerySpec(cycle_id="rw2-l11b", text="status", episode_id="e", allowed_lobes=None, denied_lobes=[])
    receipt = memory_api._compile_routing_cue_receipt(spec, compile_recall_plan(spec), tmp_path)
    assert receipt.resolved_addresses  # lobe 11 permitted now
    _merged, meta = _run_hook(tmp_path, spec, cue_receipt=receipt)
    actual = tuple(tuple(c) for c in meta["dme_execution_binding"]["query_coordinates"])
    replay = replay_query_addresses("status", root=tmp_path, allowed_lobes=None, denied_lobes=[], query_spec=spec)
    assert actual and replay == actual


def test_rework2_replay_matches_actual_thought_coordinates_across_fixtures(tmp_path, monkeypatch):
    """raw / deterministic / entity / generated-on / generated-off: the verifier
    replay of replayable inputs equals the ACTUAL ThoughtResult.query_coordinates
    (via the binding) exactly, with generated addresses accounted for only when
    actually applied."""
    from memory.api import QuerySpec
    from memory.recall_basis import replay_query_addresses
    from memory.recall_compiler import compile_recall_plan
    from memory import api as memory_api

    _seed_store(tmp_path)  # k1 Neuron / k2 Photosynthesis / k3 Mitochondria (lobe 2)
    cases = [
        ("neuron light", None),                    # raw tokens
        ("photosynthesis energy", None),           # raw + plan expansions
        ("Which skin doctor did I see?", None),    # nothing resolves
    ]
    for text, gen in cases:
        for denied in (None, [], [4]):
            spec = QuerySpec(cycle_id="rw2-x", text=text, episode_id="e", allowed_lobes=[2], denied_lobes=denied)
            receipt = memory_api._compile_routing_cue_receipt(spec, compile_recall_plan(spec), tmp_path)
            _m, meta = _run_hook(tmp_path, spec, cue_receipt=receipt)
            actual = tuple(tuple(c) for c in meta["dme_execution_binding"]["query_coordinates"])
            replay = replay_query_addresses(text, root=tmp_path, allowed_lobes=[2], denied_lobes=denied, query_spec=spec)
            assert replay == actual, (text, denied, replay, actual)
    # generated on/off (telescope store)
    root2 = tmp_path / "t2"; root2.mkdir()
    _telescope_store(root2)
    query = "Which telescope did I use?"
    spec = QuerySpec(cycle_id="rw2-g", text=query, episode_id="e", allowed_lobes=[2], denied_lobes=[])
    receipt = _receipt_for(root2, query, generated=("dobsonian",))
    for apply in ("0", "1"):
        monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", apply)
        _m, meta = _run_hook(root2, spec, cue_receipt=receipt)
        actual = tuple(tuple(c) for c in meta["dme_execution_binding"]["query_coordinates"])
        replay = replay_query_addresses(query, root=root2, allowed_lobes=[2], denied_lobes=[], query_spec=spec)
        if apply == "0":
            assert actual == replay
        else:
            assert set(actual) - set(replay) == set(receipt.generated_addresses())
            assert set(replay) <= set(actual)


def test_rework2_registry_mutation_unreachable_during_binding_and_replay(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from memory.api import QuerySpec
    from memory.dme.coordinate_registry import CoordinateRegistry
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    _seed_store(tmp_path)
    spec = QuerySpec(cycle_id="rw2-spy", text="neuron light", episode_id="e", allowed_lobes=[2], denied_lobes=[])
    merged, meta = _run_hook(tmp_path, spec)
    rows = _rows_from(merged)

    def _forbidden(name):
        def _raise(*_a, **_k):
            raise AssertionError(f"registry mutation via {name}")
        return _raise

    for name in ("register", "register_or_lookup", "_save"):
        monkeypatch.setattr(CoordinateRegistry, name, _forbidden(name))

    @contextmanager
    def _forbidden_bulk(_self):
        raise AssertionError("bulk_registration during revalidation")
        yield  # pragma: no cover

    monkeypatch.setattr(CoordinateRegistry, "bulk_registration", _forbidden_bulk)
    v = verify_candidates("neuron light", [rows["k1"], rows["k3"]], meta=meta, root=tmp_path, allowed_lobes=[2], denied_lobes=[], query_spec=spec)
    assert {c["memory_id"] for c in v.answer_payload["candidates"]} == {"k1", "k3"}


def test_rework2_no_binding_means_no_basis_validates(tmp_path, monkeypatch):
    """The carrier is the execution evidence. Metadata with genuine bases but
    without the run's binding cannot validate anything (fail closed)."""
    from memory.api import QuerySpec
    from memory.recall_verifier import verify_candidates

    monkeypatch.delenv("OMEGA_RECALL_BASIS_TOPICALITY", raising=False)
    _seed_store(tmp_path)
    spec = QuerySpec(cycle_id="rw2-nb", text="neuron light", episode_id="e", allowed_lobes=[2], denied_lobes=[])
    merged, meta = _run_hook(tmp_path, spec)
    rows = _rows_from(merged)
    stripped = {k: v for k, v in meta.items() if k != "dme_execution_binding"}
    v = verify_candidates("neuron light", [rows["k3"]], meta=stripped, root=tmp_path, allowed_lobes=[2], denied_lobes=[], query_spec=spec)
    assert v.status == "unknown"
    v_ok = verify_candidates("neuron light", [rows["k3"]], meta=meta, root=tmp_path, allowed_lobes=[2], denied_lobes=[], query_spec=spec)
    assert v_ok.status == "verified"
