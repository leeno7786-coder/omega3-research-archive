"""The semantic retina in the brain (operator 2026-09-05): a frozen Ettin 17M
encoder, resident for the process, feeding HQ-NP binary codes that only ever
PROPOSE candidates.

Pure geometry and the retina's build/load/scan are tested with a fake encoder
so no weights are needed; the resident-seat tests run only when the pinned
snapshot is present under memory/models.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from core.paths import OMEGA_ROOT
from memory import ettin_encoder, retina, semantic_plane
from memory.semantic_plane import (
    LATENT_DIM,
    NO_SIGNAL,
    STORED,
    PlaneConfig,
    SemanticPlane,
    balanced_tie_bits,
    quantize_row,
    rademacher_bank,
    retrieval_k_for_pool,
)

PINNED = (
    OMEGA_ROOT / "memory" / "models" / ettin_encoder.ETTIN_CACHE_DIRNAME / "snapshots" / ettin_encoder.ETTIN_SNAPSHOT
)
weights_present = all((PINNED / rel).exists() for rel in ettin_encoder.ETTIN_HASHES)
REAL_RETINA_ENCODER = retina.encoder


class FakeEncoder:
    """Deterministic 256-d latent from the text's hash; empty text -> zeros."""

    def encode(self, texts):
        out = []
        for text in texts:
            if not str(text).strip():
                out.append(np.zeros(LATENT_DIM, dtype=np.float32))
                continue
            seed = int.from_bytes(hashlib.sha256(str(text).encode()).digest()[:8], "big")
            rng = np.random.Generator(np.random.PCG64(seed))
            vec = rng.normal(size=LATENT_DIM).astype(np.float32)
            out.append(vec / np.linalg.norm(vec))
        return np.stack(out)


@pytest.fixture(autouse=True)
def _cold_retina(monkeypatch):
    retina.unload()
    monkeypatch.setattr(retina, "encoder", lambda: FakeEncoder())
    yield
    retina.unload()


# ── geometry ────────────────────────────────────────────────────────────────

def test_quantize_row_is_symmetric_int8_and_scale_free():
    latent = np.linspace(-2.0, 2.0, LATENT_DIM)
    codes = quantize_row(latent)
    assert codes.dtype == np.int8 and codes.min() == -127 and codes.max() == 127
    assert np.array_equal(quantize_row(latent * 5.0), codes)  # sign hyperplanes ignore magnitude
    assert not np.any(quantize_row(np.zeros(LATENT_DIM)))
    with pytest.raises(ValueError):
        quantize_row(np.zeros(LATENT_DIM - 1))


def test_bank_and_ties_are_frozen_functions_of_the_seed():
    bank = rademacher_bank(seed_index=0, dimension=4096)
    assert bank.shape == (4096, LATENT_DIM) and set(np.unique(bank)) == {-1, 1}
    assert np.array_equal(bank, rademacher_bank(seed_index=0, dimension=4096))
    assert not np.array_equal(bank, rademacher_bank(seed_index=1, dimension=4096))
    ties = balanced_tie_bits(seed_index=0, dimension=4096)
    assert ties.shape == (4096,) and 0.4 < ties.mean() < 0.6
    with pytest.raises(ValueError):
        PlaneConfig(dimension=1234)


def test_projection_matches_an_explicit_int32_reduction():
    codes = quantize_row(FakeEncoder().encode(["exactness"])[0])
    bank = rademacher_bank(seed_index=0, dimension=4096).astype(np.int32)
    ties = balanced_tie_bits(seed_index=0, dimension=4096)
    explicit = (bank @ codes.astype(np.int32))
    bits = (explicit > 0).astype(np.uint8)
    bits[explicit == 0] = ties[explicit == 0]
    expected = np.packbits(bits, bitorder="little")
    packed = semantic_plane._project(codes, bank.astype(np.float64), ties)
    assert np.array_equal(packed, expected)


def test_retrieval_window_scales_with_the_pool():
    assert retrieval_k_for_pool(0) == 32 and retrieval_k_for_pool(1500) == 32
    assert retrieval_k_for_pool(6000) == 72
    with pytest.raises(ValueError):
        retrieval_k_for_pool(-1)


def test_plane_is_insertion_order_independent_and_refuses_no_signal():
    a = SemanticPlane(FakeEncoder(), PlaneConfig(dimension=4096))
    b = SemanticPlane(FakeEncoder(), PlaneConfig(dimension=4096))
    texts = {"x": "alpha beta", "y": "gamma delta", "z": "alpha beta gamma"}
    for rid in ("x", "y", "z"):
        assert a.put(rid, texts[rid]) == STORED
    for rid in ("z", "y", "x"):
        b.put(rid, texts[rid])
    assert a.fingerprint() == b.fingerprint()
    assert a.window("alpha beta") == b.window("alpha beta")
    assert a.window("alpha beta")[0][0] == "x"
    assert a.query_status("") == NO_SIGNAL and a.put("empty", "   ") == NO_SIGNAL
    assert "empty" not in a


# ── the retina: build at publish, map at recall, propose only ────────────

def _seats():
    return [
        ("m1", "The operator prefers metric units and short replies."),
        ("m2", "The GPU is a Radeon 860M with Vulkan."),
        ("m3", "Parakeet transcribes speech on the iGPU."),
        ("m4", ""),  # nothing to encode
    ]


def test_renderer_is_the_pinned_title_summary_text_contract():
    record = {
        "title": "  Shared value  ",
        "summary": "Shared value",
        "text": "Full canonical body",
        "tags": ["must", "not", "enter"],
        "memory_id": "also-excluded",
    }
    assert retina.render_record(record) == "Shared value\nFull canonical body"
    assert retina.render_record({"title": " ", "summary": None, "text": ""}) == ""


def test_build_then_load_then_window(tmp_path: Path):
    report = retina.build(_seats(), tmp_path)
    assert report["count"] == 3 and report["dimension"] == retina.DIMENSION
    index = json.loads((tmp_path / retina.INDEX_FILE).read_text(encoding="utf-8"))
    assert index["record_ids"] == ["m1", "m2", "m3"] and index["schema"] == retina.SCHEMA
    assert (tmp_path / retina.CODES_FILE).stat().st_size == 3 * retina.DIMENSION // 8
    assert retina.is_loaded() is False
    assert retina.load(tmp_path).ids == ("m1", "m2", "m3") and retina.is_loaded()
    hits = retina.window("The operator prefers metric units and short replies.", 2)
    assert [h.record_id for h in hits][0] == "m1" and hits[0].distance == 0 and hits[0].similarity == 1.0
    assert retina.window("", 3) == []  # no signal never fabricates a neighbour
    assert retina.stats()["codes"] == 3 and retina.retrieval_k() == 32
    retina.unload()
    assert retina.window("anything", 2) == [] and retina.stats()["loaded"] is False


def test_query_similarity_uses_packed_retina_codes(monkeypatch):
    width = retina.DIMENSION // 8
    codes = {
        "same": np.zeros(width, dtype=np.uint8),
        "different": np.full(width, 0xFF, dtype=np.uint8),
    }
    monkeypatch.setattr(retina, "encode_code", lambda text: codes[text])

    assert retina.similarity("same", "same") == 1.0
    assert retina.similarity("same", "different") == pytest.approx(0.0)


def test_loading_a_different_generation_clears_stale_codes(tmp_path: Path):
    current = tmp_path / "current"
    retina.build(_seats(), current)
    retina.load(current)
    assert retina.is_loaded() is True

    with pytest.raises(retina.RetinaUnavailable):
        retina.load(tmp_path / "next-generation-without-retina")
    assert retina.is_loaded() is False
    assert retina.window("metric units", 1) == []


def test_encoder_failure_is_not_reported_as_a_no_signal_query(monkeypatch):
    def broken():
        raise ettin_encoder.EttinExecutionError("forward failed")

    monkeypatch.setattr(retina, "encoder", broken)
    with pytest.raises(retina.RetinaUnavailable, match="query encode failed"):
        retina.encode_code("a valid non-empty query")


def test_build_refuses_an_empty_generation_and_load_refuses_a_foreign_dimension(tmp_path: Path):
    with pytest.raises(retina.RetinaUnavailable):
        retina.build([("m", "")], tmp_path)
    retina.build(_seats(), tmp_path)
    meta = json.loads((tmp_path / retina.INDEX_FILE).read_text(encoding="utf-8"))
    meta["dimension"] = 4096
    (tmp_path / retina.INDEX_FILE).write_text(json.dumps(meta), encoding="utf-8")
    # Missing or foreign artifacts fail with their cause, never as a cold retina.
    with pytest.raises(retina.RetinaUnavailable, match="dimension"):
        retina.load(tmp_path)
    with pytest.raises(retina.RetinaUnavailable, match="unreadable"):
        retina.load(tmp_path / "missing")


# ── the seams: recall lane, semantic admission, DME generation, boot ─────

def test_recall_lane_proposes_from_the_retina_without_an_alternate_encoder(monkeypatch, tmp_path: Path):
    from memory import recall_lanes

    retina.build(_seats(), tmp_path)
    retina.load(tmp_path)
    monkeypatch.setattr(recall_lanes, "_rerank_window", lambda q, c: None)
    plan = SimpleNamespace(original_query="short metric replies", limit_per_lane=3)
    snapshot = {"m1": {"coord": [4, 1, 0], "title": "prefs", "summary": "metric, short"}, "m3": {"coord": [5, 0, 0], "title": "stt"}}

    hits = recall_lanes._run_retina_lane(plan, snapshot, lambda mid: None, set())
    assert hits and all(h.lane == recall_lanes.LANE_EMBEDDING for h in hits)
    assert {h.memory_id for h in hits} <= {"m1", "m3"}  # m2 is not in the coord index: canonical truth wins
    assert hits[0].reason.startswith("retina: hamming=") and 0.0 <= hits[0].score <= 1.0
    assert hits[0].metadata["retina_similarity"] == pytest.approx(hits[0].score, abs=1e-6)

    retina.unload()
    assert recall_lanes._run_perception_lane(plan, snapshot, lambda mid: None, set()) == []
    # ADR-MO-018: a cold retina never dials another encoder.
    assert recall_lanes._LANE_RUNNERS[recall_lanes.LANE_EMBEDDING] is recall_lanes._run_perception_lane


def test_semantic_admission_seeds_by_meaning_and_tells_cold_from_empty(tmp_path: Path):
    from memory.semantic_admission import semantic_seed_ids

    records = [{"id": "m1"}, {"id": "m3"}, {"id": "ghost"}]
    assert semantic_seed_ids(records, "metric", k=2) == ([], 0, 0.0, "retina_cold", {})
    retina.build(_seats(), tmp_path)
    retina.load(tmp_path)
    seeds, dim, mean_sim, status, similarity = semantic_seed_ids(records, "The operator prefers metric units and short replies.", k=1)
    assert status == "ok" and dim == retina.DIMENSION and seeds == ["m1"] and mean_sim == 1.0
    assert set(similarity) <= {"m1", "m3"} and similarity["m1"] == 1.0
    assert semantic_seed_ids([], "x", k=2)[3] == "no_records"
    assert semantic_seed_ids(records, "", k=2)[3] == "no_query_signal"


def test_generation_publish_builds_the_retina_beside_the_planes(tmp_path: Path, monkeypatch):
    import inspect

    from memory.dme import adapter

    seats = [SimpleNamespace(memory_id="m1", title="prefs", summary="metric units"),
             SimpleNamespace(memory_id="m2", title="", summary=""),
             SimpleNamespace(memory_id="m3", title="stt", summary="Parakeet on the iGPU")]
    component = adapter._build_generation_retina(seats, tmp_path)
    index = json.loads((tmp_path / retina.INDEX_FILE).read_text(encoding="utf-8"))
    assert index["record_ids"] == ["m1", "m3"]
    assert component["status"] == "built" and component["records"] == 2
    assert component["contract_sha256"] == retina.contract_sha256() == index["contract_sha256"]
    # Wired at publish after the hop planes; the protected DME scoring/read core
    # remains unchanged.
    src = inspect.getsource(adapter._rebuild_generation)
    assert src.index('GENERATION_PLANE_PATHS["hop_plane_weak"]') < src.index("_build_generation_retina(seats, output_dir")
    module_src = inspect.getsource(adapter)
    assert "_build_generation_retina(index.seats, out_dir, prior=prior, activate=True)" in module_src
    # nothing to render is a declared empty plane, not a missing one
    blank = [SimpleNamespace(memory_id="m2", title="", summary="")]
    assert adapter._build_generation_retina(blank, tmp_path / "blank") == retina.empty_component()
    assert not (tmp_path / "blank" / retina.CODES_FILE).exists()
    # a retina build failure fails the publish with its cause (audit 2026-09-28 M05)
    def broken(rows, out, *, prior=None):
        raise retina.RetinaUnavailable("encode failed")

    monkeypatch.setattr(retina, "build", broken)
    with pytest.raises(retina.RetinaUnavailable, match="encode failed"):
        adapter._build_generation_retina(seats, tmp_path / "other")


def test_legacy_dme_rebuild_builds_the_live_retina(tmp_path: Path):
    """The shipping legacy projection owner must publish codes too.

    The generation builder is not the live path while both Cut 13 flags are
    disabled.  DeepSleep calls the in-place rebuild in that mode, so leaving
    the retina only in ``_rebuild_generation`` creates a permanently cold
    retina on the default product configuration.
    """
    from memory.dme.adapter import dme_sram_path, rebuild_sram_from_graph

    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    record = {
        "id": "m1",
        "coord": [1, 0, 1],
        "type": "preference",
        "title": "Answer preferences",
        "summary": "The operator prefers metric units and short replies.",
        "text": "Use metric units, keep the answer brief, and lead with the result.",
        "enrichment": {"status": "active"},
    }
    (memory_root / "m1.json").write_text(json.dumps(record), encoding="utf-8")
    index_dir = memory_root / "indexes"
    index_dir.mkdir()
    (index_dir / "coord_index.json").write_text(
        json.dumps(
            {
                "m1": {
                    "coord": record["coord"],
                    "path": "memory/m1.json",
                }
            }
        ),
        encoding="utf-8",
    )

    rebuilt = rebuild_sram_from_graph(root=tmp_path)
    live_dir = dme_sram_path(tmp_path).parent

    assert rebuilt.seat_count == 1
    assert rebuilt.seats[0].retina_text == retina.render_record(record)
    assert (live_dir / retina.INDEX_FILE).is_file()
    assert (live_dir / retina.CODES_FILE).is_file()
    assert retina.is_loaded() is True
    assert retina.stats()["generation"] == str(live_dir)
    assert retina.window(record["summary"], 1)[0].record_id == "m1"


def test_boot_seats_the_retina_for_the_process_and_fails_closed_without_it(monkeypatch):
    import inspect

    import boot
    from core.required_substrate import RequiredSubstrateError

    # Inspect the module rather than the function's code-object span. Other
    # source-contract tests can load helpers from the same module first, which
    # makes the narrower function lookup brittle in a long isolated run.
    src = inspect.getsource(boot)
    assert "_boot_semantic_retina(report)" in src
    assert "ensure_local_worker" not in src  # the Gemma embed worker is no longer seated at boot
    report: dict = {}
    monkeypatch.setattr(retina, "bound_generation", lambda: {"state": "mapped", "generation_id": "a" * 64, "codes": 3})
    boot._boot_semantic_retina(report)
    assert report["retina"] == "up"

    def broken():
        raise ettin_encoder.EttinUnavailable("no snapshot")

    monkeypatch.setattr(retina, "encoder", broken)
    with pytest.raises(RequiredSubstrateError):
        boot._boot_semantic_retina(report)
    assert report["retina"] == "down"


# ── the resident seat (needs the pinned weights) ─────────────────────────

@pytest.mark.skipif(not weights_present, reason="pinned Ettin snapshot not present under memory/models")
def test_pinned_ettin_loads_from_the_brain_and_encodes_deterministically(monkeypatch):
    monkeypatch.setattr(retina, "encoder", REAL_RETINA_ENCODER)
    retina.reset_encoder()
    assert retina.snapshot_path() == PINNED
    encoder = retina.encoder()
    assert retina.encoder() is encoder  # resident: one load per process
    plane = SemanticPlane(encoder, PlaneConfig(dimension=16384))
    plane.put("a", "The operator prefers metric units and short replies.")
    plane.put("b", "Parakeet transcribes speech on the iGPU.")
    assert plane.window("how does the operator like answers?")[0][0] == "a"
    assert plane.window("what does the speech recognizer run on?")[0][0] == "b"
    latent = plane._encode_one("determinism")
    assert latent.shape == (LATENT_DIM,)
    assert np.array_equal(quantize_row(latent), quantize_row(plane._encode_one("determinism")))


def test_snapshot_resolution_prefers_the_brain_tree(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(ettin_encoder.SNAPSHOT_ENV, str(tmp_path / "nowhere"))
    if weights_present:
        assert retina.snapshot_path() == PINNED  # the brain copy wins over any hint
    else:
        with pytest.raises(ettin_encoder.EttinArtifactError):
            retina.snapshot_path()
    ordered = ettin_encoder.candidate_snapshots(
        env={"OMEGA_ROOT": "X", "HOME": "H", "HF_HOME": "H/.cache/huggingface"}
    )
    assert ordered == [
        Path("X")
        / "memory"
        / "models"
        / ettin_encoder.ETTIN_CACHE_DIRNAME
        / "snapshots"
        / ettin_encoder.ETTIN_SNAPSHOT
    ]


# ── incremental build across restarts (2026-09-05) ─────────────────────────
# ``_plane()`` is a fresh plane per call, so every publish used to pay one Ettin
# forward pass per record — 258 records, ~7 s, growing with memory. A record
# whose rendered text is unchanged since the previous generation reuses that
# generation's code (the code is a pure function of the text).


class CountingEncoder(FakeEncoder):
    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += len(texts)
        return super().encode(texts)


def _records(n):
    return [(f"r{i}", f"record number {i} says something distinct") for i in range(n)]


def test_second_build_reuses_every_unchanged_code(tmp_path, monkeypatch):
    enc = CountingEncoder()
    monkeypatch.setattr(retina, "encoder", lambda: enc)
    out = tmp_path / "gen"
    first = retina.build(_records(6), out)
    assert first["encoded"] == 6 and first["reused"] == 0 and enc.calls == 6
    codes_before = (out / retina.CODES_FILE).read_bytes()

    second = retina.build(_records(6), out, prior=retina.read(out))  # same texts, "after a restart"
    assert second["encoded"] == 0 and second["reused"] == 6
    assert enc.calls == 6, "no forward pass for an unchanged record"
    assert (out / retina.CODES_FILE).read_bytes() == codes_before
    assert second["fingerprint"] == first["fingerprint"]


def test_only_a_changed_or_new_record_is_encoded_again(tmp_path, monkeypatch):
    enc = CountingEncoder()
    monkeypatch.setattr(retina, "encoder", lambda: enc)
    out = tmp_path / "gen"
    retina.build(_records(4), out)
    rows = _records(4)
    rows[2] = ("r2", "record number 2 was rewritten")
    rows.append(("r9", "a brand new record"))
    report = retina.build(rows, out, prior=retina.read(out))
    assert report["encoded"] == 2 and report["reused"] == 3
    assert enc.calls == 6
    index = json.loads((out / retina.INDEX_FILE).read_text(encoding="utf-8"))
    assert index["record_ids"] == ["r0", "r1", "r2", "r3", "r9"]
    assert len(index["text_sha256"]) == 5
    # and the rewritten record's code is the code of its NEW text
    fresh = retina.build([("r2", "record number 2 was rewritten")], tmp_path / "solo")
    assert fresh["encoded"] == 1
    solo = np.fromfile(tmp_path / "solo" / retina.CODES_FILE, dtype=np.uint8)
    full = np.fromfile(out / retina.CODES_FILE, dtype=np.uint8).reshape(5, -1)
    assert np.array_equal(full[2], solo)


def test_an_index_without_hashes_is_not_a_readable_prior(tmp_path, monkeypatch):
    enc = CountingEncoder()
    monkeypatch.setattr(retina, "encoder", lambda: enc)
    out = tmp_path / "gen"
    retina.build(_records(3), out)
    index = json.loads((out / retina.INDEX_FILE).read_text(encoding="utf-8"))
    del index["text_sha256"]  # a generation written before hashes existed
    (out / retina.INDEX_FILE).write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(retina.RetinaUnavailable, match="malformed"):
        retina.read(out)
    report = retina.build(_records(3), out)  # no verified prior: every text is encoded
    assert report["reused"] == 0 and report["encoded"] == 3 and enc.calls == 6


def test_a_prior_built_under_another_contract_is_never_reused(tmp_path, monkeypatch):
    enc = CountingEncoder()
    monkeypatch.setattr(retina, "encoder", lambda: enc)
    out = tmp_path / "gen"
    retina.build(_records(3), out)
    prior = retina.read(out)
    pinned = retina.encoder_contract()
    monkeypatch.setattr(retina, "encoder_contract", lambda: {**pinned, "render_schema": "another.render.v2"})
    report = retina.build(_records(3), tmp_path / "next", prior=prior)
    assert report["reused"] == 0 and report["encoded"] == 3 and enc.calls == 6
    assert report["component"]["contract_sha256"] == retina.contract_sha256() != prior.contract_sha256
    with pytest.raises(retina.RetinaUnavailable, match="contract"):
        retina.read(out)
