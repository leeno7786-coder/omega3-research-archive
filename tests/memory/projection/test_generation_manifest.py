"""Cut 13 Task A1 — type-closed generation identity, receipt, lineage rows, validators.

Plan: docs/superpowers/plans/2026-08-16-memory-organ-cut13-program0-coherent-generation.md
(D-13-12/21/32/34/38/39/44/45/49/54/55/60/61/64/68). Pure unit tests over
``memory.projection.generation`` on tmp_path fixtures — no live profile, no
embedding, no Program-0 type.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from core.timekeeper import CoverageClock
from memory import retina as R
from memory.projection import generation as G
from memory.taxonomy.normalize import canonical_json


# ── fixtures ────────────────────────────────────────────────────────────────

def _sha(data: bytes) -> str:
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


def _content(**over) -> G.GenerationContent:
    planes = tuple(
        G.PlaneEntry(plane_id=f"hop_plane_{i}", path=p, sha256=_sha(b), rows=2, width_bits=256, scorer="hop", geometry={"kind": "hop"})
        for i, (p, b) in enumerate(PLANE_BYTES.items(), start=1)
    )
    base = dict(
        canonical_corpus_hash="c" * 64,
        canonical_files=1,
        coord_index_sha256=_sha(ARTIFACT_BYTES["coord_index.snapshot.json"]),
        registry_sha256=_sha(ARTIFACT_BYTES["coordinate_registry.json"]),
        graph_sqlite_sha256=_sha(ARTIFACT_BYTES["graph.sqlite"]),
        graph_snapshot_sha256=_sha(GRAPH_SNAPSHOT_BYTES),
        graph_snapshot_hash=GRAPH_SNAPSHOT_HASH,
        compiler_version="v7",
        dme_schema_version="dme.v1",
        sram_sha256=_sha(ARTIFACT_BYTES["sram.bin"]),
        sram_meta_sha256=_sha(ARTIFACT_BYTES["sram_meta.json"]),
        sram_index_sha256=_sha(ARTIFACT_BYTES["sram_index.json"]),
        dme_manifest_sha256=_sha(ARTIFACT_BYTES["dme_manifest.json"]),
        planes=planes,
        source_inputs=(
            G.SourceInputContent(kind="source_ledger", sha256="e" * 64, schema_version="ledger.v1"),
            G.SourceInputContent(kind="canonical_corpus", sha256="d" * 64, schema_version="corpus.v1"),
        ),
        components={"semantic_retina": R.empty_component()},
    )
    base.update(over)
    return G.GenerationContent(**base)


def _candidate_manifest(content: G.GenerationContent, **over) -> G.CandidateManifest:
    base = dict(
        content=content,
        generation_id=G.generation_id_for(content),
        snapshot_closure="proved",
        page0_capacity_by_lobe={"0": 128},
        page0_used_by_lobe={"0": 1},
        page0_utilization_by_lobe={"0": 0.0078},
        page0_overflow_attempts=0,
        unscorable_coordinate_count=0,
    )
    base.update(over)
    return G.CandidateManifest(**base)


def _materialize(dir_: Path, content: G.GenerationContent) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    for name, data in ARTIFACT_BYTES.items():
        (dir_ / name).write_bytes(data)
    (dir_ / "graph_snapshot.json").write_bytes(GRAPH_SNAPSHOT_BYTES)
    for rel, data in PLANE_BYTES.items():
        target = dir_ / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


CLOCK = CoverageClock(clock_epoch_id="epoch-a" + "0" * 25, commit_seq=100)


def _receipt(cm: G.CandidateManifest, clock: CoverageClock = CLOCK) -> G.GenerationReceipt:
    return G.GenerationReceipt(candidate=cm, coverage_clock=clock, source_input_origins={"canonical_corpus": "legacy:memory", "source_ledger": "legacy:memory/source_ledger"})


def _write_receipt(dir_: Path, receipt: G.GenerationReceipt) -> None:
    (dir_ / G.RECEIPT_FILE).write_bytes(G.receipt_bytes(receipt))


def _published_fixture(tmp_path: Path):
    """A projection root with one valid published generation + lineage + manifest."""
    root = tmp_path / "projection"
    content = _content()
    cm = _candidate_manifest(content)
    gid = cm.generation_id
    gen_dir = root / f"gen-{gid}"
    _materialize(gen_dir, content)
    receipt = _receipt(cm)
    _write_receipt(gen_dir, receipt)
    row = G.PublicationLineageRow(
        seq=1, event="published_new", generation_id=gid,
        generation_receipt_sha256=G.generation_receipt_sha256(gen_dir), repair_row_sha256=None,
        coverage_clock=CLOCK, source_input_origins=receipt.source_input_origins,
        parent_generation_id=None, previous_row_sha256=G.GENESIS_ROW_SHA256, published_at="2026-08-17T00:00:00Z",
    )
    G.append_publication_row(root / G.LINEAGE_FILE, row)
    manifest = G.ProjectionManifest(
        content=content, generation_id=gid, generation_dir=f"gen-{gid}", lineage_seq=1, parent_generation_id=None,
        coverage_clock=CLOCK, snapshot_closure="proved",
        page0_capacity_by_lobe=cm.page0_capacity_by_lobe, page0_used_by_lobe=cm.page0_used_by_lobe,
        page0_utilization_by_lobe=cm.page0_utilization_by_lobe, page0_overflow_attempts=0, unscorable_coordinate_count=0,
        source_input_origins=receipt.source_input_origins, sequenced_consumed=0, unsequenced_consumed=0,
        pending_delta_count=0, oldest_pending_commit_seq=None, oldest_pending_queued_at="",
        builder="cut13", reason="test", published_at="2026-08-17T00:00:00Z", dir_sync="posix_fsync", backend_at_publish="none",
    )
    return root, content, cm, gen_dir, receipt, row, manifest


# ── singular types / module closure ─────────────────────────────────────────

def test_types_are_singular_and_no_program0_types_leak():
    for name in ("Candidate", "CandidateManifest", "GenerationReceipt", "PublicationLineageRow", "PublicationResult",
                 "PublicationIntent", "RepairIntentPayload", "RepairIntent", "RepairLineageRow", "CoverageClockRegression",
                 "PublicationDiagnostics", "ProjectionManifest", "RegistryAuthorityLineageRow", "GenerationContent",
                 "SourceInputContent", "PlaneEntry"):
        assert inspect.isclass(getattr(G, name)), name
    assert G.CoverageClock is CoverageClock                      # imported from core.timekeeper, not redefined
    src = inspect.getsource(G)
    for banned in ("OutboxSnapshot", "OutboxCursor", "OutboxEpoch", "OutboxBootstrapIntent", "OutboxBootstrapReport",
                   "ProjectionDrainReport", "drain_once", "outbox_exclusion", "bootstrap_outbox"):
        assert banned not in src, banned                        # D-13-53 freeze boundary
    assert "coverage_clock" not in {f.name for f in dataclasses.fields(G.GenerationContent)}
    assert "clock_epoch_id" not in {f.name for f in dataclasses.fields(G.GenerationContent)}
    assert issubclass(G.CoverageClockRegression, G.GenerationIntegrityError)


def test_source_input_kinds_are_closed_and_sorted():
    c = _content()
    assert [s.kind for s in c.source_inputs] == ["canonical_corpus", "source_ledger"]   # sorted by kind
    with pytest.raises(G.GenerationIntegrityError):
        G.SourceInputContent(kind="registry", sha256="0" * 64, schema_version="x")
    with pytest.raises(G.GenerationIntegrityError):
        _content(source_inputs=(G.SourceInputContent(kind="source_ledger", sha256="1" * 64, schema_version="a"),
                                G.SourceInputContent(kind="source_ledger", sha256="2" * 64, schema_version="a")))


# ── identity ────────────────────────────────────────────────────────────────

def test_generation_id_is_deterministic_and_content_closed():
    a, b = _content(), _content()
    assert G.generation_id_for(a) == G.generation_id_for(b)
    assert G.generation_id_for(a) == _sha(canonical_json(a.to_dict()).encode("utf-8"))
    for name, value in (("canonical_corpus_hash", "9" * 64), ("registry_sha256", "9" * 64), ("sram_sha256", "9" * 64),
                        ("compiler_version", "v8"), ("canonical_files", 2), ("graph_snapshot_hash", "b" * 64)):
        assert G.generation_id_for(_content(**{name: value})) != G.generation_id_for(a), name
    changed_ledger = _content(source_inputs=(
        G.SourceInputContent(kind="canonical_corpus", sha256="d" * 64, schema_version="corpus.v1"),
        G.SourceInputContent(kind="source_ledger", sha256="f" * 64, schema_version="ledger.v1")))
    assert G.generation_id_for(changed_ledger) != G.generation_id_for(a)
    # plane change
    planes = list(a.planes); planes[0] = dataclasses.replace(planes[0], sha256="7" * 64)
    assert G.generation_id_for(_content(planes=tuple(planes))) != G.generation_id_for(a)


def test_id_ignores_everything_outside_content(tmp_path):
    content = _content()
    cm = _candidate_manifest(content)
    other_clock = CoverageClock(clock_epoch_id="epoch-b" + "0" * 25, commit_seq=999)
    r1 = _receipt(cm)
    r2 = G.GenerationReceipt(candidate=cm, coverage_clock=other_clock, source_input_origins={"canonical_corpus": "authority:R1", "source_ledger": "generation:x"})
    assert r1.candidate.generation_id == r2.candidate.generation_id
    # origin refs legacy → authority:R1 → authority:R1 (identical bytes) ⇒ one id
    for origins in ({"canonical_corpus": "legacy:memory"}, {"canonical_corpus": "authority:R1"}, {"canonical_corpus": "authority:R1"}):
        assert G.GenerationReceipt(candidate=cm, coverage_clock=CLOCK, source_input_origins=origins).candidate.generation_id == cm.generation_id


def test_registry_convergence_binds_only_the_finalized_registry():
    # built from R0 that finalized R1 vs built from R1 that stays R1 with identical artifacts ⇒ one id
    r1_bytes = b'{"assignments":{"m1":[0,0,0],"m2":[0,0,1]}}'
    from_r0 = _content(registry_sha256=_sha(r1_bytes))
    from_r1 = _content(registry_sha256=_sha(r1_bytes))
    assert G.generation_id_for(from_r0) == G.generation_id_for(from_r1)
    changed_assignment = _content(registry_sha256=_sha(b'{"assignments":{"m1":[0,0,1],"m2":[0,0,1]}}'))
    assert G.generation_id_for(changed_assignment) != G.generation_id_for(from_r1)


# ── coverage clock is singular / aliases derived ────────────────────────────

def test_one_coverage_clock_object_and_derived_aliases_round_trip():
    content = _content(); cm = _candidate_manifest(content); receipt = _receipt(cm)
    d = receipt.to_dict()
    assert d["coverage_clock"] == {"clock_epoch_id": CLOCK.clock_epoch_id, "commit_seq": 100}
    assert d["canonical_watermark_commit_seq"] == 100 and d["latest_canonical_commit_seq_at_capture"] == 100
    back = G.GenerationReceipt.from_dict(d)
    assert back == receipt and back.coverage_clock == CLOCK
    # aliases are not constructor inputs
    with pytest.raises(TypeError):
        G.GenerationReceipt(candidate=cm, coverage_clock=CLOCK, source_input_origins={}, canonical_watermark_commit_seq=5)  # type: ignore[call-arg]
    # alias disagreement is rejected on deserialization (both directions)
    bad = dict(d); bad["canonical_watermark_commit_seq"] = 101
    with pytest.raises(G.GenerationIntegrityError):
        G.GenerationReceipt.from_dict(bad)
    bad = dict(d); bad["latest_canonical_commit_seq_at_capture"] = 99
    with pytest.raises(G.GenerationIntegrityError):
        G.GenerationReceipt.from_dict(bad)
    # substituting only the sequence inside the clock while aliases stay ⇒ rejected
    bad = json.loads(json.dumps(d)); bad["coverage_clock"]["commit_seq"] = 101
    with pytest.raises(G.GenerationIntegrityError):
        G.GenerationReceipt.from_dict(bad)
    # a receipt cannot carry two sequence values for one clock id: fields are derived
    assert receipt.canonical_watermark_commit_seq == receipt.coverage_clock.commit_seq == receipt.latest_canonical_commit_seq_at_capture


def test_manifest_and_row_carry_one_clock_and_reject_alias_mismatch(tmp_path):
    root, content, cm, gen_dir, receipt, row, manifest = _published_fixture(tmp_path)
    md = manifest.to_dict()
    assert G.ProjectionManifest.from_dict(md) == manifest
    md["canonical_watermark_commit_seq"] = 101
    with pytest.raises(G.GenerationIntegrityError):
        G.ProjectionManifest.from_dict(md)
    rd = row.to_dict()
    assert G.PublicationLineageRow.from_dict(rd) == row
    rd["latest_canonical_commit_seq_at_capture"] = 7
    with pytest.raises(G.GenerationIntegrityError):
        G.PublicationLineageRow.from_dict(rd)
    with pytest.raises(G.GenerationIntegrityError):
        G.PublicationLineageRow.from_dict({**row.to_dict(), "event": "bogus"})


# ── candidate manifest / validators ─────────────────────────────────────────

def test_candidate_manifest_has_no_defaults():
    content = _content()
    with pytest.raises(TypeError):
        G.CandidateManifest(content=content, generation_id=G.generation_id_for(content))  # type: ignore[call-arg]


def test_validate_candidate_accepts_candidate_naming_and_rejects_each_defect(tmp_path):
    content = _content(); cm = _candidate_manifest(content)
    cdir = tmp_path / f"gen-{cm.generation_id}.candidate"
    _materialize(cdir, content)
    G.validate_candidate(cdir, cm)                                   # accepts gen-<id>.candidate
    # wrong id
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_candidate(cdir, dataclasses.replace(cm, generation_id="0" * 64))
    # closure / page-0 / retina component
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_candidate(cdir, dataclasses.replace(cm, snapshot_closure="unproved"))
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_candidate(cdir, dataclasses.replace(cm, unscorable_coordinate_count=1))
    # the retina component must pin a built or empty plane (audit 2026-09-28 M05)
    for component in ({"onnx": "x"}, None, {**R.empty_component(), "records": 1}):
        retina = _content(components={"semantic_retina": component})
        rdir = tmp_path / f"gen-{G.generation_id_for(retina)}.candidate"; _materialize(rdir, retina)
        with pytest.raises(G.GenerationIntegrityError):
            G.validate_candidate(rdir, _candidate_manifest(retina))
    # sha mismatch (artifact) — Gate D2 unit
    (cdir / "sram.bin").write_bytes(b"tampered")
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_candidate(cdir, cm)
    (cdir / "sram.bin").write_bytes(ARTIFACT_BYTES["sram.bin"])
    G.validate_candidate(cdir, cm)
    # graph_snapshot_hash ≠ inner graph_snapshot.json
    inner = json.loads(GRAPH_SNAPSHOT_BYTES); inner["snapshot_hash"] = "b" * 64
    bad_snapshot = canonical_json(inner).encode()
    (cdir / "graph_snapshot.json").write_bytes(bad_snapshot)
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_candidate(cdir, dataclasses.replace(cm, content=_content(graph_snapshot_sha256=_sha(bad_snapshot)),
                                                       generation_id=G.generation_id_for(_content(graph_snapshot_sha256=_sha(bad_snapshot)))))
    (cdir / "graph_snapshot.json").write_bytes(GRAPH_SNAPSHOT_BYTES)
    # plane path outside dir / duplicate plane id / plane sha mismatch
    planes = list(content.planes)
    # escaping / absolute plane paths are rejected at type construction already (before any validator, authority, lineage or rename)
    with pytest.raises(G.GenerationIntegrityError):
        outside = _content(planes=(dataclasses.replace(planes[0], path="../escape.bin"), planes[1]))
        G.validate_candidate(cdir, _candidate_manifest(outside))
    with pytest.raises(G.GenerationIntegrityError):
        absolute = _content(planes=(dataclasses.replace(planes[0], path=str(cdir / "planes/hop_plane_1.bin")), planes[1]))
        G.validate_candidate(cdir, _candidate_manifest(absolute))
    with pytest.raises(G.GenerationIntegrityError):                # duplicate plane_id rejected at construction (type-closed)
        dup = _content(planes=(planes[0], dataclasses.replace(planes[1], plane_id="hop_plane_1")))
        G.validate_candidate(cdir, _candidate_manifest(dup))
    (cdir / "planes/hop_plane_2.bin").write_bytes(b"x")
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_candidate(cdir, cm)


def test_validate_candidate_rejects_a_plane_symlinked_outside_the_candidate(tmp_path):
    content = _content(); cm = _candidate_manifest(content)
    cdir = tmp_path / f"gen-{cm.generation_id}.candidate"
    _materialize(cdir, content)
    # a symlink inside the candidate that resolves outside is caught by the validator's resolve() check
    escaped_dir = tmp_path / "outside"; escaped_dir.mkdir(); (escaped_dir / "hop_plane_2.bin").write_bytes(PLANE_BYTES["planes/hop_plane_2.bin"])
    (cdir / "planes/hop_plane_2.bin").unlink()
    try:
        (cdir / "planes/hop_plane_2.bin").symlink_to(escaped_dir / "hop_plane_2.bin")
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_candidate(cdir, cm)
    (cdir / "planes/hop_plane_2.bin").unlink(); (cdir / "planes/hop_plane_2.bin").write_bytes(PLANE_BYTES["planes/hop_plane_2.bin"])
    G.validate_candidate(cdir, cm)


def test_validate_generation_directory_receipt_rules(tmp_path):
    content = _content(); cm = _candidate_manifest(content)
    gen_dir = tmp_path / f"gen-{cm.generation_id}"
    _materialize(gen_dir, content)
    # unreceipted directory (only {content, generation_id}) is NOT adoptable
    (gen_dir / G.RECEIPT_FILE).write_text(canonical_json({"content": content.to_dict(), "generation_id": cm.generation_id}))
    with pytest.raises(G.UnreceiptedGeneration):
        G.validate_generation_directory(gen_dir, content)
    receipt = _receipt(cm)
    _write_receipt(gen_dir, receipt)
    got = G.validate_generation_directory(gen_dir, content)          # valid orphan: no lineage / active requirement
    assert got == receipt
    assert G.validate_generation_directory(gen_dir, receipt) == receipt
    # receipt round-trip validates by recomputation and rejects a wrong id
    wrong = G.GenerationReceipt(candidate=dataclasses.replace(cm, generation_id="1" * 64), coverage_clock=CLOCK, source_input_origins={})
    _write_receipt(gen_dir, wrong)
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_generation_directory(gen_dir, content)
    # substituted closure / page-0 / clock vs the expected receipt/candidate ⇒ rejected
    _write_receipt(gen_dir, G.GenerationReceipt(candidate=dataclasses.replace(cm, snapshot_closure="unproved"), coverage_clock=CLOCK, source_input_origins={}))
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_generation_directory(gen_dir, content)
    _write_receipt(gen_dir, G.GenerationReceipt(candidate=dataclasses.replace(cm, unscorable_coordinate_count=3), coverage_clock=CLOCK, source_input_origins={}))
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_generation_directory(gen_dir, content)
    other_clock = CoverageClock(clock_epoch_id="epoch-b" + "0" * 25, commit_seq=100)
    _write_receipt(gen_dir, G.GenerationReceipt(candidate=cm, coverage_clock=other_clock, source_input_origins=receipt.source_input_origins))
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_generation_directory(gen_dir, receipt)            # receipt.coverage_clock != expected (epoch only)
    _write_receipt(gen_dir, G.GenerationReceipt(candidate=cm, coverage_clock=CoverageClock(clock_epoch_id=CLOCK.clock_epoch_id, commit_seq=101), source_input_origins=receipt.source_input_origins))
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_generation_directory(gen_dir, receipt)            # sequence only


def test_validate_published_generation_active_state(tmp_path):
    root, content, cm, gen_dir, receipt, row, manifest = _published_fixture(tmp_path)
    G.validate_published_generation(root, manifest)
    # generation_dir must be gen-<id>
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_published_generation(root, dataclasses.replace(manifest, generation_dir=manifest.generation_id))
    # outer content ≠ receipt content
    other = _content(compiler_version="v9")
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_published_generation(root, dataclasses.replace(manifest, content=other))
    # manifest clock ≠ head row (epoch only / sequence only)
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_published_generation(root, dataclasses.replace(manifest, coverage_clock=CoverageClock(clock_epoch_id="epoch-b" + "0" * 25, commit_seq=100)))
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_published_generation(root, dataclasses.replace(manifest, coverage_clock=CoverageClock(clock_epoch_id=CLOCK.clock_epoch_id, commit_seq=101)))
    # origins ≠ head row
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_published_generation(root, dataclasses.replace(manifest, source_input_origins={"canonical_corpus": "authority:zzz"}))
    # head row receipt sha ≠ directory receipt
    _write_receipt(gen_dir, G.GenerationReceipt(candidate=cm, coverage_clock=CLOCK, source_input_origins={"canonical_corpus": "legacy:other"}))
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_published_generation(root, manifest)
    _write_receipt(gen_dir, receipt)
    G.validate_published_generation(root, manifest)
    # HEAD row naming an absent directory
    absent_row = G.PublicationLineageRow(
        seq=2, event="published_new", generation_id="9" * 64, generation_receipt_sha256="1" * 64, repair_row_sha256=None,
        coverage_clock=CoverageClock(clock_epoch_id=CLOCK.clock_epoch_id, commit_seq=101), source_input_origins={},
        parent_generation_id=cm.generation_id, previous_row_sha256=G.row_sha256(row), published_at="2026-08-17T00:00:01Z")
    G.append_publication_row(root / G.LINEAGE_FILE, absent_row)
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_published_generation(root, manifest)


# ── lineage framing / chain ─────────────────────────────────────────────────

def _row(seq: int, prev: str, gid: str = "1" * 64, seq_clock: int = 100) -> G.PublicationLineageRow:
    return G.PublicationLineageRow(
        seq=seq, event="published_new", generation_id=gid, generation_receipt_sha256="2" * 64, repair_row_sha256=None,
        coverage_clock=CoverageClock(clock_epoch_id="e" * 32, commit_seq=seq_clock), source_input_origins={},
        parent_generation_id=None, previous_row_sha256=prev, published_at="t")


def test_lineage_rows_are_lf_framed_chain_verified_and_torn_or_tampered_detected(tmp_path):
    path = tmp_path / G.LINEAGE_FILE
    r1 = _row(1, G.GENESIS_ROW_SHA256)
    r2 = _row(2, G.row_sha256(r1))
    G.append_publication_row(path, r1); G.append_publication_row(path, r2)
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 2
    assert raw.split(b"\n")[0] == canonical_json(r1.to_dict()).encode("utf-8")   # canonical JSON rows
    scan = G.read_publication_lineage(path)
    assert [r.seq for r in scan.rows] == [1, 2] and scan.torn_tail is None and scan.head == r2
    # class (b): incomplete final LF row is reported as a torn tail, complete rows still parsed
    path.write_bytes(raw + b'{"seq": 3, "event": "published_new"')
    scan = G.read_publication_lineage(path)
    assert [r.seq for r in scan.rows] == [1, 2] and scan.torn_tail == b'{"seq": 3, "event": "published_new"'
    # class (c): reordered rows fail loud
    lines = raw.split(b"\n")[:2]
    path.write_bytes(b"\n".join([lines[1], lines[0]]) + b"\n")
    with pytest.raises(G.PublicationLineageTampered):
        G.read_publication_lineage(path)
    # class (c): tampered committed row (hash chain broken) fails loud
    tampered = json.loads(lines[0]); tampered["generation_id"] = "3" * 64
    path.write_bytes(canonical_json(tampered).encode() + b"\n" + lines[1] + b"\n")
    with pytest.raises(G.PublicationLineageTampered):
        G.read_publication_lineage(path)
    # sequence gap in committed chain fails loud
    r3 = _row(4, G.row_sha256(r2))
    path.write_bytes(raw)
    with pytest.raises(G.PublicationLineageTampered):
        G.append_publication_row(path, r3)
    # append verifies chain continuity before writing
    ok = _row(3, G.row_sha256(r2))
    G.append_publication_row(path, ok)
    assert G.read_publication_lineage(path).head == ok
    # missing final row (class a) is not detectable by the reader alone — the reader reports the head; recovery needs an intent
    assert G.read_publication_lineage(path).head.seq == 3


# ── intents / repair rows ───────────────────────────────────────────────────

def test_publication_intent_row_serialises_byte_for_byte():
    r1 = _row(1, G.GENESIS_ROW_SHA256)
    intent = G.PublicationIntent(row=r1, target_generation_dir=f"gen-{'1' * 64}", candidate_path=None, candidate_receipt_sha256=None, prepared_at="t")
    d = intent.to_dict()
    assert canonical_json(d["row"]) == canonical_json(r1.to_dict())
    back = G.PublicationIntent.from_dict(d)
    assert back == intent and back.row == r1
    assert G.publication_row_bytes(back.row) == G.publication_row_bytes(r1)


def test_repair_intent_payload_and_row_reproduce_intent_sha():
    payload = G.RepairIntentPayload(generation_id="1" * 64, candidate_path="gen-x.candidate", candidate_receipt_sha256="2" * 64,
                                    quarantine_path="quarantine/gen-x.corrupt-1", corrupt_tree_sha256="3" * 64,
                                    replacement_tree_sha256="4" * 64, prior_repair_row_sha256=G.GENESIS_ROW_SHA256, prepared_at="t")
    assert "repair_row_sha256" not in payload.to_dict()                    # no field depends on the repair row hash
    sha = G.repair_intent_sha256(payload)
    row = G.RepairLineageRow(seq=1, intent=payload, intent_sha256=sha, previous_row_sha256=G.GENESIS_ROW_SHA256, repaired_at="t")
    assert G.RepairLineageRow.from_dict(row.to_dict()) == row
    assert G.repair_intent_sha256(G.RepairLineageRow.from_dict(row.to_dict()).intent) == sha
    with pytest.raises(G.GenerationIntegrityError):
        G.RepairLineageRow(seq=1, intent=payload, intent_sha256="0" * 64, previous_row_sha256=G.GENESIS_ROW_SHA256, repaired_at="t")
    ri = G.RepairIntent(payload=payload)
    assert G.RepairIntent.from_dict(ri.to_dict()) == ri


def test_generation_receipt_bytes_are_canonical_and_hashable(tmp_path):
    content = _content(); cm = _candidate_manifest(content); receipt = _receipt(cm)
    b = G.receipt_bytes(receipt)
    assert b == canonical_json(receipt.to_dict()).encode("utf-8")
    d = tmp_path / "gen-x"; d.mkdir(); (d / G.RECEIPT_FILE).write_bytes(b)
    assert G.generation_receipt_sha256(d) == _sha(b)
    assert G.read_receipt(d) == receipt
