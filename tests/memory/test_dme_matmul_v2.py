"""DME v2 — Dewey-lobe MatMul Hamming engine (TDD)."""

from __future__ import annotations

import json

import numpy as np

from memory.dewey_map import LOBE_MAP


def test_lobe_layout_uses_dewey_ids_not_5w1h():
    from memory.dme.lobe_layout import DEWEY_LOBE_IDS, LOBE_NAME

    assert set(DEWEY_LOBE_IDS) == set(LOBE_MAP.keys())
    assert "who" not in LOBE_NAME.values()
    assert LOBE_NAME[0] == "000_identity"
    assert LOBE_NAME[2] == "200_knowledge"


def test_packer_sets_bit_in_dewey_lobe_region():
    from memory.dme.lobe_layout import addr_to_bit_position
    from memory.dme.packer import ENTRY_WORDS, has_coordinate, pack_entry

    entry = pack_entry(
        fingerprint=42,
        timestamp=1_700_000_000.0,
        source="gate:test",
        coordinates=[(2, 0, 17)],  # knowledge lobe
    )
    assert entry.shape == (ENTRY_WORDS,)
    assert has_coordinate(entry, (2, 0, 17))
    lobe, word_idx, bit_idx = addr_to_bit_position(2, 0, 17)
    assert lobe == 2
    assert bool(entry[word_idx] & np.uint64(1 << bit_idx))


def test_matmul_overlap_counts_intersection():
    from memory.dme import matmul_engine
    from memory.dme.packer import pack_entry

    q_coords = [(2, 0, 1), (2, 0, 2)]
    query = pack_entry(0, 0.0, "query", q_coords)
    hit = pack_entry(1, 0.0, "hit", q_coords + [(2, 0, 99)])
    miss = pack_entry(2, 0.0, "miss", [(2, 0, 1)])

    sram = np.vstack([hit, miss])
    overlaps = matmul_engine.compute_overlaps(sram, query)
    assert overlaps[0] == matmul_engine.query_popcount(query)
    assert overlaps[1] == 1


def test_search_intersection_returns_matching_rows():
    from memory.dme.packer import pack_entry
    from memory.dme.search import search_intersection

    coords = [(2, 0, 5), (2, 0, 6)]
    sram = np.vstack(
        [
            pack_entry(10, 0.0, "a", coords),
            pack_entry(20, 0.0, "b", [(2, 0, 5)]),
        ]
    )
    index = [
        {"memory_id": "a", "title": "Alpha", "summary": "match both"},
        {"memory_id": "b", "title": "Beta", "summary": "partial"},
    ]
    result = search_intersection(sram, index, coords, max_results=5)
    assert [hit.memory_id for hit in result.hits] == ["a"]
    assert result.hits[0].match_count == len(coords)


def test_matmul_engine_stays_local_when_runtime_is_available(monkeypatch):
    """The standalone DME never probes the runtime CLIENT; with the NPU
    worker disabled (kill switch) the backend is deterministically numpy."""
    from memory.dme import matmul_engine

    calls = 0

    def runtime_probe():
        nonlocal calls
        calls += 1
        raise AssertionError("standalone DME must not probe the runtime")

    monkeypatch.setattr("runtime.client.get_runtime_client", runtime_probe)
    monkeypatch.setenv("OMEGA_DME_NPU", "0")
    monkeypatch.setattr(matmul_engine, "_ACTIVE_BACKEND", None)
    assert matmul_engine.active_backend(force_probe=True) == "numpy"
    assert calls == 0


def test_dme_backend_prewarm_keeps_one_live_onnx_session():
    from memory.dme import hop_engine

    first = hop_engine.ensure_backend_loaded()
    second = hop_engine.ensure_backend_loaded()

    assert first["ready"] is True
    assert first["providers"]
    assert second == first


def test_dme_shutdown_releases_every_in_process_seat(monkeypatch):
    from memory import dme

    calls: list[str] = []
    monkeypatch.setattr(
        "memory.dme.recall_hook.force_unload_all_sessions",
        lambda: calls.append("recall_sessions"),
    )
    monkeypatch.setattr(
        "memory.dme.hop_engine.close_backend",
        lambda: calls.append("hop_backend"),
    )
    monkeypatch.setattr(
        "memory.dme.matmul_engine.shutdown_runtime",
        lambda: calls.append("matmul_runtime"),
    )

    result = dme.shutdown_runtime()

    assert result == {
        "ok": True,
        "released": ["recall_sessions", "hop_backend", "matmul_runtime"],
    }
    assert calls == ["recall_sessions", "hop_backend", "matmul_runtime"]


def test_hop_backend_close_drops_onnx_session_and_bit_cache(monkeypatch):
    from memory.dme import hop_engine

    class Session:
        closed = False

        def close(self):
            self.closed = True

    session = Session()
    cache = {1: (object(), np.zeros((1, 1), dtype=np.uint8))}
    monkeypatch.setattr(hop_engine, "_ORT_SESSION", session)
    monkeypatch.setattr(hop_engine, "_ORT_FAILED", True)
    monkeypatch.setattr(hop_engine, "_ORT_GEOMETRY_VALIDATED_SESSION", session)
    monkeypatch.setattr(hop_engine, "_BITS_CACHE", cache)

    hop_engine.close_backend()

    assert session.closed is True
    assert hop_engine._ORT_SESSION is None
    assert hop_engine._ORT_FAILED is False
    assert hop_engine._ORT_GEOMETRY_VALIDATED_SESSION is None
    assert hop_engine._BITS_CACHE == {}


def test_rebuild_writes_binary_sram_and_meta(tmp_path, monkeypatch):
    from memory.dme import matmul_engine
    from memory.dme.adapter import dme_sram_path, rebuild_sram_from_graph

    monkeypatch.setenv("OMEGA_DME_NPU", "0")
    monkeypatch.setattr(matmul_engine, "_ACTIVE_BACKEND", None)
    from memory.dme.lobe_layout import ENTRY_WORDS
    from memory.taxonomy.compiler import compile_memory_graph

    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    record = {
        "id": "k1",
        "coord": [2, 0, 1],
        "type": "concept",
        "title": "Neuron",
        "summary": "Neurons pass signals through synapses.",
        "domain": "biology",
        "tags": ["biology"],
        "enrichment": {"canonical": True},
    }
    (memory_root / "k1.json").write_text(json.dumps(record), encoding="utf-8")
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"k1": {"coord": [2, 0, 1], "path": "memory/k1.json"}}),
        encoding="utf-8",
    )

    compile_memory_graph(root=tmp_path)
    index = rebuild_sram_from_graph(root=tmp_path)

    assert index.seat_count == 1
    raw = dme_sram_path(tmp_path).read_bytes()
    assert len(raw) == ENTRY_WORDS * 8  # one uint64 row
    meta_path = tmp_path / "memory" / "dme" / "sram_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    from memory.dme.adapter import DME_SCHEMA_VERSION

    assert meta["schema_version"] == DME_SCHEMA_VERSION
    assert meta["backend"] == "numpy"


def test_recall_candidates_use_coordinate_matmul_not_token_bitmap(tmp_path):
    from memory.dme.adapter import rebuild_sram_from_graph, recall_candidates
    from memory.taxonomy.compiler import compile_memory_graph

    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    record = {
        "id": "k1",
        "coord": [2, 0, 1],
        "type": "concept",
        "title": "Neuron",
        "summary": "Neurons pass signals through synapses.",
        "domain": "biology",
        "tags": ["biology"],
        "enrichment": {"canonical": True},
    }
    (memory_root / "k1.json").write_text(json.dumps(record), encoding="utf-8")
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"k1": {"coord": [2, 0, 1], "path": "memory/k1.json"}}),
        encoding="utf-8",
    )

    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)

    hits = recall_candidates(
        "neuron synapses", allowed_lobes=[2], denied_lobes=[], limit=5, root=tmp_path
    )
    assert hits
    assert hits[0]["memory_id"] == "k1"
    from memory.dme.hop_engine import ensure_backend_loaded

    assert (
        hits[0]["provenance"].get("dme_backend")
        == ensure_backend_loaded()["backend"]
    )
    assert "dme_coordinate_bit" not in hits[0]["provenance"]
