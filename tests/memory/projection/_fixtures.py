"""Shared fixture helpers for the Cut 13 projection tests (not a test module)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from core.timekeeper import CoverageClock
from memory import retina
from memory.projection import generation as G
from memory.taxonomy.normalize import canonical_json


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


ARTIFACT_BYTES = {
    "coord_index.snapshot.json": b'{"m1":{"path":"memory/0/m1.json"}}',
    "coordinate_registry.json": b'{"assignments":{"m1":[0,0,0]}}',
    "graph.sqlite": b"SQLITE-BYTES-PLACEHOLDER-graph",
    "sram.bin": b"\x00\x01\x02\x03" * 8,
    "sram_meta.json": b'{"rows":32,"width_bits":256}',
    "sram_index.json": b'{"m1":0}',
    "dme_manifest.json": b'{"planes":["hop_plane_1"]}',
}
GRAPH_SNAPSHOT_HASH = "a" * 64
GRAPH_SNAPSHOT_BYTES = canonical_json({"snapshot_hash": GRAPH_SNAPSHOT_HASH, "edge_count": 3}).encode("utf-8")
PLANE_BYTES = {"planes/hop_plane_1.bin": b"\xff" * 64, "planes/hop_plane_2.bin": b"\x0f" * 64}

EPOCH_A = "epoch-a" + "0" * 25
EPOCH_B = "epoch-b" + "0" * 25


def clock(seq: int, epoch: str = EPOCH_A) -> CoverageClock:
    return CoverageClock(clock_epoch_id=epoch, commit_seq=seq)


def content(*, registry: bytes | None = None, **over) -> G.GenerationContent:
    reg = registry if registry is not None else ARTIFACT_BYTES["coordinate_registry.json"]
    planes = tuple(
        G.PlaneEntry(plane_id=f"hop_plane_{i}", path=p, sha256=sha(b), rows=2, width_bits=256, scorer="hop", geometry={"kind": "hop"})
        for i, (p, b) in enumerate(PLANE_BYTES.items(), start=1)
    )
    base = dict(
        canonical_corpus_hash="c" * 64, canonical_files=1,
        coord_index_sha256=sha(ARTIFACT_BYTES["coord_index.snapshot.json"]), registry_sha256=sha(reg),
        graph_sqlite_sha256=sha(ARTIFACT_BYTES["graph.sqlite"]), graph_snapshot_sha256=sha(GRAPH_SNAPSHOT_BYTES),
        graph_snapshot_hash=GRAPH_SNAPSHOT_HASH, compiler_version="v7", dme_schema_version="dme.v1",
        sram_sha256=sha(ARTIFACT_BYTES["sram.bin"]), sram_meta_sha256=sha(ARTIFACT_BYTES["sram_meta.json"]),
        sram_index_sha256=sha(ARTIFACT_BYTES["sram_index.json"]), dme_manifest_sha256=sha(ARTIFACT_BYTES["dme_manifest.json"]),
        planes=planes,
        source_inputs=(G.SourceInputContent(kind="source_ledger", sha256="e" * 64, schema_version="ledger.v1"),
                       G.SourceInputContent(kind="canonical_corpus", sha256="d" * 64, schema_version="corpus.v1")),
        components={"semantic_retina": retina.empty_component()},   # placeholder artifacts carry no retina
    )
    base.update(over)
    return G.GenerationContent(**base)


def candidate_manifest(c: G.GenerationContent, **over) -> G.CandidateManifest:
    base = dict(content=c, generation_id=G.generation_id_for(c), snapshot_closure="proved",
                page0_capacity_by_lobe={"0": 128}, page0_used_by_lobe={"0": 1}, page0_utilization_by_lobe={"0": 0.0078},
                page0_overflow_attempts=0, unscorable_coordinate_count=0)
    base.update(over)
    return G.CandidateManifest(**base)


def materialize(dir_: Path, c: G.GenerationContent, *, registry: bytes | None = None) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    for name, data in ARTIFACT_BYTES.items():
        (dir_ / name).write_bytes(data)
    if registry is not None:
        (dir_ / "coordinate_registry.json").write_bytes(registry)
    (dir_ / "graph_snapshot.json").write_bytes(GRAPH_SNAPSHOT_BYTES)
    for rel, data in PLANE_BYTES.items():
        t = dir_ / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(data)


def make_candidate(root: Path, clk: CoverageClock, *, registry: bytes | None = None, origins: dict | None = None,
                   builder: str = "cut13", reason: str = "test", **content_over) -> G.Candidate:
    c = content(registry=registry, **content_over)
    cm = candidate_manifest(c)
    cdir = G.candidate_dir(root, cm.generation_id)
    materialize(cdir, c, registry=registry)
    return G.Candidate(candidate_dir=cdir, manifest=cm, coverage_clock=clk,
                       source_input_origins=origins or {"canonical_corpus": "legacy:memory", "source_ledger": "legacy:memory/source_ledger"},
                       builder=builder, reason=reason)


def dir_state(dir_: Path) -> dict[str, str]:
    """{relative path: sha256} for every file — proves 'no byte under gen-G changes'."""
    return {p.relative_to(dir_).as_posix(): sha(p.read_bytes()) for p in sorted(dir_.rglob("*")) if p.is_file()}


def publish(root: Path, clk: CoverageClock, **kw) -> G.PublicationResult:
    cand = make_candidate(root, clk, **kw)
    with G.projection_publication_authority(root) as auth:
        return G.publish_generation(root, cand, authority=auth)


# ── real-artifact profiles (legacy pipeline → assembled generation) ─────────────

def write_record(profile: Path, memory_id: str, *, title: str, summary: str, lobe: int = 2) -> None:
    """A canonical record + ledger artifact + projection receipt + coord_index entry
    (the same shape tests/memory/test_dme_recall_hook.py seeds)."""
    memory_root = profile / "memory"
    memory_root.mkdir(exist_ok=True, parents=True)
    from memory.source_ledger.store import record_source
    from memory.projection_receipt import write_projection_receipt

    artifact = record_source(f"{title}. {summary}", source_type="operator_chat", origin_id=f"cycle-{memory_id}", root=profile)
    provenance = dict(artifact.citation())
    provenance.update({"origin_id": f"cycle-{memory_id}", "origin_type": "operator_chat", "author": "test",
                       "ingested_at": "2026-08-17T00:00:00Z", "confidence": 0.9})
    record = {  # schema-valid canonical record (strict membership, D-13-26)
        "id": memory_id, "coord": [lobe, 0, int(memory_id[-1])], "type": "concept", "title": title, "summary": summary,
        "domain": "biology", "tags": ["biology"], "provenance": provenance,
        "flags": {"canonical": True, "private": False},
        "audit": [{"when": "2026-08-17T00:00:00Z", "what": "created", "by": "test"}],
        "enrichment": {"canonical": True, "status": "active"},
    }
    from memory.dewey_map import LOBE_MAP
    lobe_dir = memory_root / LOBE_MAP[lobe]
    lobe_dir.mkdir(parents=True, exist_ok=True)
    (lobe_dir / f"{memory_id}.json").write_text(json.dumps(record), encoding="utf-8")   # canonical lobe directory (directory authority)
    write_projection_receipt(record, root=profile)
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    index[memory_id] = {"coord": record["coord"], "path": f"memory/{LOBE_MAP[lobe]}/{memory_id}.json"}
    index_path.write_text(json.dumps(index), encoding="utf-8")


def seed_legacy_profile(profile: Path, records: dict[str, tuple[str, str]] | None = None) -> None:
    """Seed canonical records and run TODAY's legacy pipeline (compile + DME rebuild)."""
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.taxonomy.compiler import compile_memory_graph

    records = records or {"k1": ("Neuron", "Neurons pass signals through synapses."),
                          "k2": ("Synapse", "A synapse joins two neurons.")}
    for mid, (title, summary) in records.items():
        write_record(profile, mid, title=title, summary=summary)
    compile_memory_graph(root=profile)
    rebuild_sram_from_graph(root=profile)
    force_unload_all_sessions()


def assemble_candidate_from_legacy(profile: Path, clk, *, origins: dict | None = None, reason: str = "test",
                                   variant: str | None = None) -> G.Candidate:
    """Assemble a candidate generation directory from the profile's LEGACY fixed-name
    artifacts (a stand-in for the Task A4 builder): copies graph.sqlite (journal DELETE),
    graph_snapshot.json, coordinate_registry.json, coord index, DME sram/meta/index/manifest
    and both hop planes, and computes the type-closed GenerationContent from their bytes."""
    import shutil
    import sqlite3
    from memory.dme.lobe_layout import ENTRY_WORDS
    from memory.taxonomy import paths as P

    mem = profile / "memory"
    compiled = P.compiled_root(profile)
    dme = mem / "dme"
    staging = profile / "_assemble"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    # graph: checkpoint + copy + journal_mode=DELETE
    src = sqlite3.connect(str(compiled / "graph.sqlite"))
    src.execute("PRAGMA wal_checkpoint(TRUNCATE)"); src.close()
    shutil.copy2(compiled / "graph.sqlite", staging / "graph.sqlite")
    conn = sqlite3.connect(str(staging / "graph.sqlite"))
    conn.execute("PRAGMA journal_mode=DELETE"); conn.commit(); conn.close()
    for wal in ("graph.sqlite-wal", "graph.sqlite-shm"):
        (staging / wal).unlink(missing_ok=True)
    shutil.copy2(compiled / "graph_snapshot.json", staging / "graph_snapshot.json")
    shutil.copy2(dme / "coordinate_registry.json", staging / "coordinate_registry.json")
    shutil.copy2(mem / "indexes" / "coord_index.json", staging / "coord_index.snapshot.json")
    for name in ("sram.bin", "sram_meta.json", "sram_index.json"):
        shutil.copy2(dme / name, staging / name)
    shutil.copy2(dme / "manifest.json", staging / "dme_manifest.json")
    if variant is not None:  # a DIFFERENT generation with the same DME packs (content differs by the dme manifest bytes)
        doc = json.loads((staging / "dme_manifest.json").read_text()); doc["variant"] = variant
        (staging / "dme_manifest.json").write_text(json.dumps(doc, sort_keys=True))
    # The legacy rebuild wrote the retina beside the DME packs; pin exactly those bytes.
    semantic_retina = retina.empty_component()
    if (dme / retina.INDEX_FILE).is_file():
        for name in (retina.CODES_FILE, retina.INDEX_FILE):
            shutil.copy2(dme / name, staging / name)
        index = json.loads((staging / retina.INDEX_FILE).read_text(encoding="utf-8"))
        semantic_retina = {"status": "built", "schema": index["schema"], "contract_sha256": index["contract_sha256"],
                           "records": index["count"], "codes_sha256": sha((staging / retina.CODES_FILE).read_bytes()),
                           "index_sha256": sha((staging / retina.INDEX_FILE).read_bytes())}
    (staging / "planes").mkdir()
    shutil.copy2(dme / "hop_plane_strong.bin", staging / "planes" / "hop_plane_strong.bin")
    shutil.copy2(dme / "hop_plane_weak.bin", staging / "planes" / "hop_plane_weak.bin")
    snap = json.loads((staging / "graph_snapshot.json").read_text())
    rows = len(json.loads((staging / "sram_index.json").read_text()))
    planes = tuple(
        G.PlaneEntry(plane_id=f"hop_plane_{cls}", path=f"planes/hop_plane_{cls}.bin", sha256=sha((staging / "planes" / f"hop_plane_{cls}.bin").read_bytes()),
                     rows=rows, width_bits=ENTRY_WORDS * 64, scorer="hop", geometry={"kind": "hop", "edge_class": cls})
        for cls in ("strong", "weak"))
    meta = json.loads((staging / "sram_meta.json").read_text())
    ledger_root = mem / "source_ledger"
    ledger_bytes = b"".join(sorted(p.read_bytes() for p in ledger_root.rglob("*") if p.is_file())) if ledger_root.exists() else b""
    from memory.dewey_map import LOBE_MAP as _LM
    canonical_bytes = b"".join(sorted(p.read_bytes() for d in sorted(set(_LM.values())) for p in (mem / d).rglob("*.json") if p.is_file()))
    c = G.GenerationContent(
        canonical_corpus_hash=sha(canonical_bytes), canonical_files=sum(1 for d in sorted(set(_LM.values())) for p in (mem / d).rglob("*.json") if p.is_file()),
        coord_index_sha256=sha((staging / "coord_index.snapshot.json").read_bytes()),
        registry_sha256=sha((staging / "coordinate_registry.json").read_bytes()),
        graph_sqlite_sha256=sha((staging / "graph.sqlite").read_bytes()),
        graph_snapshot_sha256=sha((staging / "graph_snapshot.json").read_bytes()),
        graph_snapshot_hash=str(snap["snapshot_hash"]), compiler_version=str(snap.get("compiler_version") or "v7"),
        dme_schema_version=str(meta.get("schema_version") or ""),
        sram_sha256=sha((staging / "sram.bin").read_bytes()), sram_meta_sha256=sha((staging / "sram_meta.json").read_bytes()),
        sram_index_sha256=sha((staging / "sram_index.json").read_bytes()), dme_manifest_sha256=sha((staging / "dme_manifest.json").read_bytes()),
        planes=planes,
        source_inputs=(G.SourceInputContent(kind="canonical_corpus", sha256=sha(canonical_bytes), schema_version="corpus.v1"),
                       G.SourceInputContent(kind="source_ledger", sha256=sha(ledger_bytes), schema_version="ledger.v1")),
        components={"semantic_retina": semantic_retina},
    )
    cm = candidate_manifest(c)
    cdir = G.candidate_dir(profile, cm.generation_id)
    if cdir.exists():
        shutil.rmtree(cdir)
    cdir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(cdir))
    return G.Candidate(candidate_dir=cdir, manifest=cm, coverage_clock=clk,
                       source_input_origins=origins or {"canonical_corpus": "legacy:memory", "source_ledger": "legacy:memory/source_ledger"},
                       builder="cut13-fixture", reason=reason)


def publish_from_legacy(profile: Path, clk, **kw) -> G.PublicationResult:
    cand = assemble_candidate_from_legacy(profile, clk, **kw)
    with G.projection_publication_authority(profile) as auth:
        return G.publish_generation(profile, cand, authority=auth)


def seed_records_only(profile: Path, records: dict[str, tuple[str, str]] | None = None) -> None:
    """Canonical records + ledger + receipts + coord_index, NO legacy compile/DME (no registry yet)."""
    records = records or {"k1": ("Neuron", "Neurons pass electrical signals through synapses to other neurons."),
                          "k2": ("Synapse", "A synapse joins two neurons and carries neurotransmitter signals."),
                          "k3": ("Axon", "An axon carries the action potential away from the neuron body."),
                          "k4": ("Dendrite", "Dendrites receive synaptic input from many axon terminals.")}
    for mid, (title, summary) in records.items():
        write_record(profile, mid, title=title, summary=summary)


def redirect_timekeeper(tmp: Path) -> None:
    """Point the timekeeper authority + encoding journal at tmp (never the live guardian/state)."""
    import core.paths as core_paths
    import memory.encoding_journal as EJ
    from core import timekeeper as TK

    core_paths.GUARDIAN_STATE_ROOT = tmp / "guardian" / "state"
    EJ.JOURNAL_PATH = tmp / "guardian" / "state" / "encoding_journal.jsonl"
    TK.reset_for_tests()


def build_and_publish(profile: Path, *, reason: str = "test"):
    """build_generation + publish_generation under one authority; returns (candidate-or-rejected, result-or-None)."""
    from memory.projection import build as B

    with G.projection_publication_authority(profile) as auth:
        out = B.build_generation(profile, reason=reason, authority=auth)
        if isinstance(out, G.Candidate):
            return out, G.publish_generation(profile, out, authority=auth)
        return out, None
