"""Cut 13 Task A3b — ONE reentrant projection-read transaction owning every derived read
(D-13-19/28) on the REAL seams: memory.api.recall_verified / read_memory, meta_recall,
pressure_signals, graph_diff, dme.adapter entry points; unavailable required
perception fails explicitly while canonical truth survives; the derived-read audit."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import core.paths as core_paths
from memory import api as memory_api
from memory.api import QuerySpec
from memory.dme import adapter as A
from memory.dme import recall_hook as RH
from memory.projection import generation as G
from tests.memory.projection._fixtures import clock, publish_from_legacy, seed_legacy_profile

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_sessions():
    RH.force_unload_all_sessions()
    yield
    RH.force_unload_all_sessions()
    from memory.dme import matmul_engine as ME
    ME.unregister_plane()


def _profile(monkeypatch):
    profile = Path(core_paths.MEMORY_ROOT).parent
    seed_legacy_profile(profile)
    monkeypatch.setattr(memory_api, "OMEGA_ROOT", profile)
    return profile


def _query(cycle="c1"):
    return QuerySpec(cycle_id=cycle, text="neuron synapse", episode_id="e", allowed_lobes=[2], denied_lobes=[], limit=8)


def _mid_call_publisher(profile, published: dict, clk):
    def hook(*args, **kwargs):
        if not published:
            published["r"] = publish_from_legacy(profile, clk, variant="mid-call")
    return hook


def test_recall_verified_owns_one_transaction_through_basis_verification(monkeypatch):
    profile = _profile(monkeypatch)
    r1 = publish_from_legacy(profile, clock(100))
    proj = G.projection_root(profile)
    published: dict = {}
    seen: dict = {}
    real_cue = memory_api._compile_routing_cue_receipt

    def cue_then_publish(query, plan, root):
        # AFTER the taxonomy graph access, BEFORE DME session / verifier: G+1 publishes
        _mid_call_publisher(profile, published, clock(101))()
        seen["cue_generation"] = G.current_view().generation_id
        seen["leased_during"] = G.generation_is_leased(proj, r1.generation_id)
        return real_cue(query, plan, root)

    monkeypatch.setattr(memory_api, "_compile_routing_cue_receipt", cue_then_publish)
    real_verify = memory_api.verify_retrieval_result if hasattr(memory_api, "verify_retrieval_result") else None
    from memory import recall_verifier as RV
    real_v = RV.verify_retrieval_result

    def spy_verify(*a, **k):
        seen["verify_generation"] = G.current_view().generation_id       # basis verification still inside the transaction
        return real_v(*a, **k)

    monkeypatch.setattr(RV, "verify_retrieval_result", spy_verify)
    verdict = memory_api.recall_verified(_query("c1"))
    assert published and published["r"].generation_id != r1.generation_id
    assert seen["cue_generation"] == r1.generation_id and seen["verify_generation"] == r1.generation_id
    assert seen["leased_during"] is True
    # the READER lease is released after the call; only the scorer's pinned registration
    # (D-13-24/37, live root) may still hold G until unregister_plane
    from memory.dme import matmul_engine as ME
    assert ME.registered_generation_id() == r1.generation_id
    ME.unregister_plane(r1.generation_id)
    assert not G.generation_is_leased(proj, r1.generation_id)             # no reader lease outlives the transaction
    assert verdict.status in ("verified", "unknown", "ambiguous", "conflict")
    # a subsequent call is on G+1
    with G.projection_read_transaction(profile) as ctx:
        assert ctx.view.generation_id == published["r"].generation_id


def test_read_memory_stays_on_G_and_reports_the_lane(monkeypatch):
    profile = _profile(monkeypatch)
    r1 = publish_from_legacy(profile, clock(100))
    published: dict = {}
    real_cue = memory_api._compile_routing_cue_receipt
    seen = {}

    def cue_then_publish(query, plan, root):
        _mid_call_publisher(profile, published, clock(101))()
        seen["gen"] = G.current_view().generation_id
        return real_cue(query, plan, root)

    monkeypatch.setattr(memory_api, "_compile_routing_cue_receipt", cue_then_publish)
    result = memory_api.read_memory(_query("c2"))
    assert seen["gen"] == r1.generation_id
    assert result.meta["projection_lane"] == "generation" and result.meta["projection_generation_id"] == r1.generation_id
    assert result.meta["snapshot_hash"] == r1.active_manifest.content.graph_snapshot_hash
    result2 = memory_api.read_memory(_query("c3"))
    assert result2.meta["projection_generation_id"] == published["r"].generation_id


def test_other_entry_points_open_or_reuse_a_transaction_and_stay_on_one_generation(monkeypatch):
    from memory.taxonomy import graph_diff, meta_recall, pressure_signals
    profile = _profile(monkeypatch)
    r1 = publish_from_legacy(profile, clock(100))
    opened: list[str] = []
    real_txn = G.projection_read_transaction

    from contextlib import contextmanager

    @contextmanager
    def spy_txn(root):
        opened.append("open")
        with real_txn(root) as ctx:
            yield ctx

    for mod in (graph_diff, pressure_signals, meta_recall, A):
        monkeypatch.setattr(G, "projection_read_transaction", spy_txn)
    # graph_diff: the hash and the rows come from ONE generation even if G+1 publishes mid-call
    published: dict = {}
    from memory.taxonomy import graph_store as GS
    real_connect = GS.connect_for_read

    def connect_then_publish(root=None):
        conn = real_connect(root)
        _mid_call_publisher(profile, published, clock(101))()
        return conn

    monkeypatch.setattr(GS, "connect_for_read", connect_then_publish)
    state = graph_diff.capture_graph_state(profile)
    assert opened and state["snapshot_hash"] == r1.active_manifest.content.graph_snapshot_hash
    assert published["r"].generation_id != r1.generation_id
    n = len(opened)
    pressure_signals.analyze_memory_pressure(profile)
    assert len(opened) > n
    n = len(opened)
    meta_recall.recall_skill_context("neuron", root=profile)
    assert len(opened) > n
    n = len(opened)
    ex = A.recall_candidates_observed("neuron", root=profile, allowed_lobes=[2], denied_lobes=[])
    assert len(opened) > n and ex.snapshot_hash == published["r"].active_manifest.content.graph_snapshot_hash
    # reuse: inside an outer transaction the entry points do NOT open a second view
    with G.projection_read_transaction(profile) as ctx:
        n = len(opened)
        A.recall_candidates_observed("neuron", root=profile, allowed_lobes=[2], denied_lobes=[])
        pressure_signals.analyze_memory_pressure(profile)
        assert G.current_view() is ctx.view


def test_unavailable_required_perception_fails_recall_without_damaging_truth(monkeypatch):
    from memory import retina
    from tests.memory.projection.test_generation_semantic_retina import CountingEncoder

    monkeypatch.setattr(retina, "encoder", lambda: CountingEncoder())
    profile = _profile(monkeypatch)
    publish_from_legacy(profile, clock(100))
    proj = G.projection_root(profile)
    from memory.dewey_map import LOBE_MAP

    canonical_dir = profile / "memory" / LOBE_MAP[2]
    before = {path.name: path.read_bytes() for path in canonical_dir.glob("*.json")}
    assert before
    (proj / G.MANIFEST_FILE).write_bytes(b"{corrupt manifest")
    reason = G.manifest_state(proj).reason
    assert reason
    for call, cycle in ((memory_api.read_memory, "c4"), (memory_api.recall_verified, "c5")):
        with pytest.raises(retina.RetinaUnavailable) as error:
            call(_query(cycle))
        assert reason in str(error.value)
    assert {path.name: path.read_bytes() for path in canonical_dir.glob("*.json")} == before


def test_legacy_behaviour_without_manifest_is_unchanged(monkeypatch):
    profile = _profile(monkeypatch)
    result = memory_api.read_memory(_query("c7"))
    assert result.meta["projection_lane"] == "legacy" and result.meta["projection_generation_id"] is None
    with G.projection_read_transaction(profile) as ctx:
        assert ctx.mode == "legacy"


def test_derived_read_audit_no_bare_connect_in_readers_and_bare_helper_refuses(monkeypatch):
    # grep-style audit: reader modules use connect_for_read, never the writer connect()
    readers = ["memory/taxonomy/recall_graph.py", "memory/taxonomy/recall.py", "memory/taxonomy/pressure_signals.py",
               "memory/taxonomy/graph_diff.py", "memory/dme/hop_engine.py", "memory/recall_basis.py", "memory/integrity/audit.py"]
    for rel in readers:
        src = (REPO / rel).read_text(encoding="utf-8")
        assert not re.search(r"\bG\.connect\(", src), f"{rel} still uses the writer connect()"
        assert "connect_for_read(" in src, rel
    # writers keep connect(): compiler + the legacy in-place DME rebuild
    assert re.search(r"\bG\.connect\(", (REPO / "memory/taxonomy/compiler.py").read_text(encoding="utf-8"))
    # bare helper under a manifest ⇒ GenerationViewRequired
    profile = _profile(monkeypatch)
    publish_from_legacy(profile, clock(100))
    from memory.taxonomy import graph_store as GS
    for helper in (lambda: GS.graph_path(profile), lambda: A.dme_sram_path(profile), lambda: GS.connect_for_read(profile)):
        with pytest.raises(G.GenerationViewRequired):
            helper()


def test_scorer_registration_lease_blocks_gc_until_unregister(monkeypatch):
    from memory.dme import matmul_engine as ME
    profile = _profile(monkeypatch)
    r1 = publish_from_legacy(profile, clock(100))
    proj = G.projection_root(profile)
    with G.projection_read_transaction(profile):
        A.load_sram(root=profile)                     # live root ⇒ pinned + leased registration
    assert ME.registered_generation_id() == r1.generation_id
    publish_from_legacy(profile, clock(101), variant="v2")
    publish_from_legacy(profile, clock(102), variant="v3")
    assert (proj / f"gen-{r1.generation_id}").is_dir()   # older than active+1 but leased by the scorer
    ME.unregister_plane(r1.generation_id)
    publish_from_legacy(profile, clock(103), variant="v4")
    assert not (proj / f"gen-{r1.generation_id}").exists()
