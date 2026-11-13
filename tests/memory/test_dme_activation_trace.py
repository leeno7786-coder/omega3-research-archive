"""Cut 1 — DMEActivationTrace observation-only contract.

Governing: docs/engineering/memory-organ/14_REVIEW_RECONCILIATION_FREEZE.md
and docs/superpowers/plans/2026-08-14-memory-organ-cut1-dme-activation-trace.md.

The trace is runtime/episode evidence of what the DME scorer activated. It is
not canonical memory, not a graph, and it never claims association identity
for OR-plane hop hits (Fable B-1 / 14 §3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import memory.taxonomy.graph_store as G
from memory.api import QuerySpec
from memory.dme import adapter
from memory.dme.activation_trace import (
    ActivatedCandidate,
    DMEActivationTrace,
    build_activation_trace,
)
from memory.dme.adapter import rebuild_sram_from_graph
from memory.dme.recall_hook import force_unload_all_sessions
from memory.taxonomy.compiler import compile_memory_graph
from memory.taxonomy.schemas import MemoryEdge

# ── fixtures ────────────────────────────────────────────────────────────────
# Same store recipe as tests/memory/test_dme_recall_hook.py::_write_record
# (ledger citation so the verifier can ground, projection receipt, coord_index)
# wrapped as fixtures. Not a second fixture system — the same recipe, reused.


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
    record = {
        "id": memory_id,
        "coord": [2, 0, int(memory_id[-1])],
        "type": "concept",
        "title": title,
        "summary": summary,
        "domain": "biology",
        "tags": ["biology"],
        "provenance": artifact.citation(),
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


def _strong_edge(conn, src: str, dst: str) -> None:
    # SUPPORTS is in hop_engine.STRONG_EDGE_TYPES; both endpoints are seated
    # leaves so the bulk adjacency puts them in the STRONG lane both ways.
    G.insert_edge(
        conn,
        MemoryEdge(
            edge_id=f"e:{src}:{dst}",
            source_id=src,
            target_id=dst,
            edge_type="SUPPORTS",
            evidence_event_id=f"ev:{src}:{dst}",
        ),
    )


@pytest.fixture
def isolated_dme_store(tmp_path: Path) -> Path:
    """Three seated leaves; k1 and k2 both SUPPORT k3.

    k3's strong hop plane is therefore OR(k1 bits, k2 bits): a query on k1/k2
    vocabulary reaches k3 through ``strong_hop1`` while k3 itself has no
    direct overlap. Two contributors, one OR row — the Fable B-1 shape."""
    _write_record(tmp_path, "k1", title="Neuron", summary="Neurons pass signals through synapses.")
    _write_record(
        tmp_path, "k2", title="Photosynthesis", summary="Plants convert light into chemical energy."
    )
    _write_record(tmp_path, "k3", title="Mitochondria", summary="Organelles produce cellular power.")
    compile_memory_graph(root=tmp_path)
    conn = G.connect(root=tmp_path)
    try:
        _strong_edge(conn, "k1", "k3")
        _strong_edge(conn, "k2", "k3")
        conn.commit()
    finally:
        conn.close()
    rebuild_sram_from_graph(root=tmp_path)
    force_unload_all_sessions()
    yield tmp_path
    force_unload_all_sessions()


@pytest.fixture
def query_spec() -> QuerySpec:
    return QuerySpec(
        cycle_id="cut1-cycle",
        text="neuron synapses",
        episode_id="cut1-episode",
        allowed_lobes=[2],
        denied_lobes=[],
    )


@pytest.fixture
def bridge_query_spec() -> QuerySpec:
    # vocabulary from BOTH k1 and k2 → k3 wins strong_hop1 through the OR row
    return QuerySpec(
        cycle_id="cut1-bridge",
        text="neuron light",
        episode_id="cut1-episode",
        allowed_lobes=[2],
        denied_lobes=[],
    )


# ── Task 1: schema ──────────────────────────────────────────────────────────


def test_trace_contains_hash_not_raw_query():
    trace = build_activation_trace(
        cycle_id="cycle-1",
        episode_id="ep-1",
        query_text="Where did I put the key?",
        projection_generation="snap-abc",
        backend="npu_packed",
        query_coordinates=[(2, 0, 4)],
        candidates=[],
    )
    payload = trace.to_dict()
    assert payload["query_hash"]
    assert "Where did I put the key?" not in repr(payload)
    assert payload["cue_receipt_id"] is None
    assert payload["associations"] == []


def test_trace_id_is_deterministic_for_same_execution_receipt():
    kwargs = dict(
        cycle_id="cycle-1",
        episode_id="ep-1",
        query_text="doctor",
        projection_generation="snap-abc",
        backend="numpy",
        query_coordinates=[(2, 0, 1)],
        candidates=[
            ActivatedCandidate(
                memory_id="m1",
                row_index=3,
                score=0.6,
                evidence_class="strong_hop1",
                plane_id="structural_strong",
                hop_depth=1,
                match_count=1,
                query_count=1,
                recall_basis_id=None,
                attribution_status="diagnostic",
            )
        ],
    )
    assert build_activation_trace(**kwargs).trace_id == build_activation_trace(**kwargs).trace_id


def test_cut1_trace_never_claims_association_ids():
    trace = build_activation_trace(
        cycle_id="c",
        episode_id="e",
        query_text="x",
        projection_generation="g",
        backend="numpy",
        query_coordinates=[],
        candidates=[],
    )
    assert isinstance(trace, DMEActivationTrace)
    assert trace.associations == ()


# ── Task 2: adapter observed-execution seam ─────────────────────────────────


def test_observed_and_legacy_candidate_paths_are_identical(isolated_dme_store):
    legacy = adapter.recall_candidates("neuron synapses", root=isolated_dme_store, limit=10)
    observed = adapter.recall_candidates_observed(
        "neuron synapses", root=isolated_dme_store, limit=10
    )
    assert legacy, "fixture must produce at least one DME hit"
    assert list(observed.candidates) == legacy
    assert observed.thought_result is not None
    assert observed.snapshot_hash


def test_merge_observed_preserves_existing_merge_output(isolated_dme_store, query_spec):
    baseline = [{"id": "existing", "score": 0.4, "provenance": {}}]
    old = adapter.merge_dme_candidate_dicts(query_spec, baseline, root=isolated_dme_store)
    new, execution = adapter.merge_dme_candidate_dicts_observed(
        query_spec, baseline, root=isolated_dme_store
    )
    assert new == old
    assert execution.snapshot_hash


def test_observed_execution_on_empty_store_has_no_thought(tmp_path):
    taxonomy = tmp_path / "memory" / "taxonomy_store" / "compiled"
    taxonomy.mkdir(parents=True, exist_ok=True)
    (taxonomy / "latest_snapshot_hash.txt").write_text("empty-snap", encoding="utf-8")
    G.connect(root=tmp_path).close()
    force_unload_all_sessions()
    execution = adapter.recall_candidates_observed("anything", root=tmp_path)
    assert execution.candidates == ()
    assert execution.thought_result is None
    force_unload_all_sessions()


# ── Task 3: hook trace reflects the execution, OR planes stay honest ────────


def test_hook_trace_hop_candidate_is_diagnostic_and_names_no_association(
    monkeypatch, isolated_dme_store, bridge_query_spec
):
    from memory.dme.recall_hook import apply_dme_recall_hook

    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    merged, stats = apply_dme_recall_hook(bridge_query_spec, [], root=isolated_dme_store)
    trace = stats.to_meta()["dme_activation_trace"]
    hop = next(c for c in trace["candidates"] if c["evidence_class"] == "strong_hop1")
    assert hop["memory_id"] == "k3"
    assert hop["plane_id"] == "structural_strong"
    assert hop["hop_depth"] == 1
    assert hop["attribution_status"] == "diagnostic"
    assert trace["associations"] == []
    assert "association_id" not in repr(hop)
    # both contributors are present only because they are candidates in their
    # own right (union), never as association contributors of the hop row
    direct = {c["memory_id"]: c for c in trace["candidates"] if c["hop_depth"] == 0}
    assert set(direct) == {"k1", "k2"}
    for cand in direct.values():
        assert cand["attribution_status"] is None
        assert cand["plane_id"] == "plane0"


def test_hook_trace_candidates_are_the_dme_execution_not_the_merge_winner(
    monkeypatch, isolated_dme_store, query_spec
):
    from memory.dme.recall_hook import apply_dme_recall_hook

    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    # a baseline row that ties the DME direct hit (1.0) for the same id: the
    # merge keeps the existing lane on a tie, so the baseline row "wins"
    baseline = [{"id": "k1", "score": 1.0, "provenance": {"candidate_source": "lexical"}}]
    merged, stats = apply_dme_recall_hook(query_spec, baseline, root=isolated_dme_store)
    winner = next(item for item in merged if item["id"] == "k1")
    assert winner["provenance"]["candidate_source"] == "lexical"  # baseline won the merge
    trace = stats.to_meta()["dme_activation_trace"]
    k1 = next(c for c in trace["candidates"] if c["memory_id"] == "k1")
    # …but the trace records what DME actually scored for k1, from the scorer
    assert k1["evidence_class"] == "direct"
    assert k1["match_count"] == k1["query_count"] >= 1
    assert k1["row_index"] >= 0
    assert trace["query_coordinates"], "trace must carry the scorer's query coordinates"


# ── Task 6: observation is behavior-neutral and read-only ───────────────────


def _hash_tree(root: Path, *, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    import hashlib

    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()   # keys are compared against POSIX-style literals
        if any(rel.endswith(x) for x in exclude):
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture
def live_root_dme_store(tmp_path: Path, monkeypatch) -> Path:
    """The same three-leaf store, built where ``read_memory`` will look:
    the memory-suite conftest redirects core.paths.MEMORY_ROOT to
    ``<tmp>/_canonical_memory_isolation/memory``; the profile root is its
    parent, and every DME/taxonomy/ledger helper accepts that root."""
    import core.paths as core_paths
    from memory import api as memory_api

    profile_root = Path(core_paths.MEMORY_ROOT).parent
    _write_record(profile_root, "k1", title="Neuron", summary="Neurons pass signals through synapses.")
    _write_record(
        profile_root, "k2", title="Photosynthesis", summary="Plants convert light into chemical energy."
    )
    _write_record(profile_root, "k3", title="Mitochondria", summary="Organelles produce cellular power.")
    compile_memory_graph(root=profile_root)
    conn = G.connect(root=profile_root)
    try:
        _strong_edge(conn, "k1", "k3")
        _strong_edge(conn, "k2", "k3")
        conn.commit()
    finally:
        conn.close()
    rebuild_sram_from_graph(root=profile_root)
    force_unload_all_sessions()
    monkeypatch.setattr(memory_api, "OMEGA_ROOT", profile_root)
    yield profile_root
    force_unload_all_sessions()


def _read(text: str, cycle: str):
    from memory import api as memory_api

    return memory_api.read_memory(
        QuerySpec(
            cycle_id=cycle,
            text=text,
            episode_id="cut1-episode",
            allowed_lobes=[2],
            denied_lobes=[],
            limit=8,
        )
    )


def _candidate_signature(c) -> tuple:
    prov = dict(getattr(c, "provenance", None) or {})
    dme_prov = {k: v for k, v in prov.items() if k.startswith("dme_") or k == "candidate_source"}
    return (
        c.id,
        tuple(c.coord or ()),
        round(float(c.score), 9),
        c.title,
        c.summary,
        tuple(sorted(dme_prov.items())),
    )


def test_read_memory_trace_on_off_parity(live_root_dme_store, monkeypatch):
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "0")
    off = _read("neuron light", "cut1-off")
    force_unload_all_sessions()
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    on = _read("neuron light", "cut1-on")

    assert on.candidates, "fixture must verify at least one candidate"
    assert [_candidate_signature(c) for c in on.candidates] == [
        _candidate_signature(c) for c in off.candidates
    ]
    assert on.meta["recall_verdict"]["status"] == off.meta["recall_verdict"]["status"]
    assert on.meta["recall_verdict"]["reason"] == off.meta["recall_verdict"]["reason"]
    assert on.meta["dme_ran"] is True and off.meta["dme_ran"] is True
    assert "dme_activation_trace" not in off.meta
    trace = on.meta["dme_activation_trace"]
    assert trace["projection_generation"] == on.meta["dme_snapshot_hash"]
    assert trace["episode_id"] == "cut1-episode"
    # the trace records the whole DME execution — including the strong_hop1
    # row the verifier/anchor later dropped — without changing the answer set
    classes = {c["memory_id"]: c["evidence_class"] for c in trace["candidates"]}
    assert classes == {"k1": "union", "k2": "union", "k3": "strong_hop1"}
    assert {c.id for c in on.candidates} <= set(classes)


def test_trace_enabled_recall_performs_no_durable_memory_io(live_root_dme_store, monkeypatch):
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    # the verifier's diagnostic verdict log is written by EXISTING contract on
    # every read_memory; it is not memory/projection/adaptive state
    exclude = ("recall_verifier/verdict_log.jsonl",)
    before = _hash_tree(live_root_dme_store, exclude=exclude)
    for key in (
        "memory/k1.json",
        "memory/indexes/coord_index.json",
        "memory/dme/coordinate_registry.json",
        "memory/dme/sram.bin",
        "memory/dme/hop_plane_strong.bin",
        "memory/dme/hop_plane_weak.bin",
        "memory/taxonomy_store/compiled/graph.sqlite",
    ):
        assert key in before, key

    result = _read("neuron light", "cut1-io")
    assert "dme_activation_trace" in result.meta

    after = _hash_tree(live_root_dme_store, exclude=exclude)
    assert after == before
    # and the only file the read may have created is the pre-existing-contract log
    all_after = set(_hash_tree(live_root_dme_store))
    assert all_after - set(before) <= {"memory/recall_verifier/verdict_log.jsonl"}


def test_query_time_registry_is_immutable_through_trace_path(live_root_dme_store, monkeypatch):
    import hashlib

    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    registry = live_root_dme_store / "memory" / "dme" / "coordinate_registry.json"
    before = hashlib.sha256(registry.read_bytes()).hexdigest()
    # registered ("neuron") + never-seen tokens: no coordinate may be minted
    result = _read("neuron zorblax quintessence flibbertigibbet", "cut1-nomint")
    assert "dme_activation_trace" in result.meta
    assert hashlib.sha256(registry.read_bytes()).hexdigest() == before
    trace = result.meta["dme_activation_trace"]
    # only the registered token resolved; unseen tokens produced no coordinate
    assert len(trace["query_coordinates"]) == 1


def test_or_plane_hop_row_names_no_source_contributor(isolated_dme_store, monkeypatch):
    """Fable B-1 negative control: k3's strong plane is OR(k1, k2). The trace
    may say k3 was reached through structural_strong at hop 1 — it may NOT
    say which of k1/k2 supplied the bits, and it may not list them as
    association contributors. They appear only as candidates in their own
    right (union), which is an independent reason."""
    from memory.dme.recall_hook import apply_dme_recall_hook

    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    # a k2-only query: k2 direct, k3 strong_hop1 through the OR row; k1 has no
    # direct overlap and is NOT a contributor of the k3 row for this query
    q = QuerySpec(cycle_id="cut1-or", text="light", episode_id="e", allowed_lobes=[2], denied_lobes=[])
    _merged, stats = apply_dme_recall_hook(q, [], root=isolated_dme_store)
    trace = stats.to_meta()["dme_activation_trace"]
    by_id = {c["memory_id"]: c for c in trace["candidates"]}
    assert by_id["k3"]["evidence_class"] == "strong_hop1"
    assert by_id["k3"]["attribution_status"] == "diagnostic"
    assert trace["associations"] == []
    frozen_keys = {
        "memory_id", "row_index", "score", "evidence_class", "plane_id",
        "hop_depth", "match_count", "query_count", "recall_basis_id", "attribution_status",
    }
    for cand in trace["candidates"]:
        assert set(cand) == frozen_keys, cand
    # exact execution shape: k2 direct (its own bits), k3 strong_hop1 through
    # the OR row, k1 weak_hop1 through the compiler's shared-domain lane. k1
    # is present ONLY as its own weak-plane candidate — never attached to k3.
    assert {m: (c["evidence_class"], c["hop_depth"]) for m, c in by_id.items()} == {
        "k2": ("direct", 0),
        "k3": ("strong_hop1", 1),
        "k1": ("weak_hop1", 1),
    }
    assert by_id["k1"]["plane_id"] == "structural_weak"
    assert by_id["k1"]["attribution_status"] == "diagnostic"
    assert by_id["k2"]["attribution_status"] is None
    assert "association_id" not in repr(trace)
    assert "contributor" not in repr(trace)


# ── Cut 2: the trace names its cue receipt by id only ───────────────────────


def test_trace_links_cue_receipt_by_id_without_duplicating_cues():
    trace = build_activation_trace(
        cycle_id="c",
        episode_id="e",
        query_text="x",
        projection_generation="g",
        backend="numpy",
        query_coordinates=[],
        candidates=[],
        cue_receipt_id="cue-abc",
    )
    payload = trace.to_dict()
    assert trace.cue_receipt_id == "cue-abc"
    assert payload["cue_receipt_id"] == "cue-abc"
    assert "generated_terms" not in payload
    assert "deterministic_terms" not in payload
    assert "entity_terms" not in payload
    # the id participates in trace identity
    without = build_activation_trace(
        cycle_id="c", episode_id="e", query_text="x", projection_generation="g",
        backend="numpy", query_coordinates=[], candidates=[],
    )
    assert without.trace_id != trace.trace_id


def test_hook_trace_carries_receipt_id_when_a_receipt_is_supplied(monkeypatch, isolated_dme_store, query_spec):
    from memory.dme.coordinate_registry import load_registry
    from memory.dme.recall_hook import apply_dme_recall_hook
    from memory.routing_cues import build_cue_receipt

    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    receipt = build_cue_receipt(
        query_text=query_spec.text,
        deterministic_terms=("neuron",),
        entity_terms=(),
        generated_terms=(),
        registry=load_registry(isolated_dme_store),
        allowed_lobes={2},
        denied_lobes=set(),
    )
    _m, stats = apply_dme_recall_hook(query_spec, [], root=isolated_dme_store, cue_receipt=receipt)
    trace = stats.to_meta()["dme_activation_trace"]
    assert trace["cue_receipt_id"] == receipt.receipt_id
    assert "neuron" not in repr(trace)  # no cue duplication inside the trace
    _m2, stats2 = apply_dme_recall_hook(query_spec, [], root=isolated_dme_store)
    assert stats2.to_meta()["dme_activation_trace"]["cue_receipt_id"] is None
