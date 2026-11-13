"""Cut 13 Task A2 — publication under one authority token: existence-first order,
classification (D-13-45/49), coverage seal (D-13-77), lease-aware GC (D-13-4)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from memory.projection import generation as G
from memory.projection import registry_authority as RA
from tests.memory.projection._fixtures import (
    ARTIFACT_BYTES, EPOCH_A, EPOCH_B, clock, content, dir_state, make_candidate, publish, sha,
)


def _proj(root: Path) -> Path:
    return G.projection_root(root)


def test_publish_new_creates_generation_receipt_row_manifest_and_seal(tmp_path):
    root = tmp_path / "mem"
    cand = make_candidate(root, clock(100))
    gid = cand.manifest.generation_id
    with G.projection_publication_authority(root) as auth:
        res = G.publish_generation(root, cand, authority=auth)
    proj = _proj(root)
    assert res.outcome == "published_new" and res.generation_id == gid
    assert res.publication_row_written and res.publication_row_seq == 1 and res.coverage_advanced is False
    gen_dir = proj / f"gen-{gid}"
    assert gen_dir.is_dir() and not cand.candidate_dir.exists()
    for name in ARTIFACT_BYTES:
        assert (gen_dir / name).is_file()
    receipt = G.read_receipt(gen_dir)
    assert receipt.candidate == cand.manifest and receipt.coverage_clock == clock(100)
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert scan.head.event == "published_new" and scan.head.generation_receipt_sha256 == G.generation_receipt_sha256(gen_dir)
    assert scan.head.coverage_clock == clock(100)
    manifest = G.read_active_manifest(proj)
    assert manifest == res.active_manifest and manifest.generation_dir == f"gen-{gid}" and manifest.coverage_clock == clock(100)
    assert manifest.canonical_watermark_commit_seq == 100
    G.validate_published_generation(proj, manifest)
    assert G.read_coverage_seal(proj) == clock(100)
    assert (proj / G.PUBLICATION_INTENT_FILE).exists() is False
    # registry authority selected + one truthful provenance row
    sel = RA.select_authority(proj)
    assert sel is not None and sel[0] == sha(ARTIFACT_BYTES["coordinate_registry.json"]) and sel[1] == 1
    assert len(RA.read_authority_lineage(proj).rows) == 1


def test_publisher_constructs_manifest_no_second_manifest_argument(tmp_path):
    import inspect
    sig = inspect.signature(G.publish_generation)
    assert list(sig.parameters) == ["root", "candidate", "authority", "publication_diagnostics"]


def test_rejected_candidate_removed_and_nothing_else_touched(tmp_path):
    root = tmp_path / "mem"
    cand = make_candidate(root, clock(100))
    (cand.candidate_dir / "sram.bin").write_bytes(b"tampered")
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.GenerationIntegrityError):
            G.publish_generation(root, cand, authority=auth)
    proj = _proj(root)
    assert not cand.candidate_dir.exists()
    assert not (proj / G.MANIFEST_FILE).exists() and not (proj / G.LINEAGE_FILE).exists()
    assert RA.select_authority(proj) is None and G.read_coverage_seal(proj) is None


def test_second_publish_keeps_previous_and_removes_older(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    r2 = publish(root, clock(101), compiler_version="v8")
    r3 = publish(root, clock(102), compiler_version="v9")
    proj = _proj(root)
    dirs = sorted(p.name for p in proj.iterdir() if p.name.startswith("gen-"))
    assert dirs == sorted([f"gen-{r2.generation_id}", f"gen-{r3.generation_id}"])   # active + 1 previous
    assert r3.active_manifest.parent_generation_id == r2.generation_id
    assert r2.coverage_advanced and r3.coverage_advanced
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert [r.seq for r in scan.rows] == [1, 2, 3]


def test_same_id_equal_coverage_is_reused_existing_without_row_or_seal(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    gen_dir = proj / f"gen-{r1.generation_id}"
    before = dir_state(gen_dir)
    seal_before = (proj / G.COVERAGE_SEAL_FILE).stat().st_mtime_ns
    r2 = publish(root, clock(100), reason="rebuild")
    assert r2.outcome == "reused_existing" and r2.publication_row_written is False and r2.coverage_advanced is False
    assert r2.publication_row_seq == 1
    assert dir_state(gen_dir) == before                                   # no byte under gen-G changes
    assert len(G.read_publication_lineage(proj / G.LINEAGE_FILE).rows) == 1
    assert (proj / G.COVERAGE_SEAL_FILE).stat().st_mtime_ns == seal_before   # equal coverage ⇒ no seal write
    assert G.read_active_manifest(proj).reason == "rebuild"               # non-authoritative diagnostics refreshed
    assert G.read_active_manifest(proj).coverage_clock == clock(100)
    assert len(RA.read_authority_lineage(proj).rows) == 1                   # no authority churn


def test_same_id_later_coverage_advances_lineage_once_and_manifest_reports_101(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    gen_dir = proj / f"gen-{r1.generation_id}"
    before = dir_state(gen_dir)
    r100_sha = G.generation_receipt_sha256(gen_dir)
    r2 = publish(root, clock(101))
    assert r2.outcome == "reused_existing" and r2.publication_row_written and r2.coverage_advanced
    assert r2.publication_row_seq == 2
    assert dir_state(gen_dir) == before                                   # immutable receipt keeps its original 100
    assert G.read_receipt(gen_dir).coverage_clock == clock(100)
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert len(scan.rows) == 2 and scan.head.generation_receipt_sha256 == r100_sha   # row binds the TARGET receipt R100
    assert scan.head.coverage_clock == clock(101)
    m = G.read_active_manifest(proj)
    assert m.coverage_clock == clock(101) and m.canonical_watermark_commit_seq == 101
    G.validate_published_generation(proj, m)
    assert G.read_coverage_seal(proj) == clock(101)
    assert not any(p.name.endswith(".candidate") for p in proj.iterdir())  # duplicate candidate deleted after validation
    # a third equal-coverage publish is a no-row reuse at seq 2
    r3 = publish(root, clock(101))
    assert r3.outcome == "reused_existing" and not r3.publication_row_written and r3.publication_row_seq == 2


def test_retained_reactivation_and_orphan_adoption(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))                       # G1
    r2 = publish(root, clock(101), compiler_version="v8")  # G2 active, G1 retained (previous)
    proj = _proj(root)
    r3 = publish(root, clock(102))                       # same content as G1 ⇒ retained + valid ⇒ reactivated
    assert r3.generation_id == r1.generation_id and r3.outcome == "reactivated_retained" and r3.publication_row_written
    m = G.read_active_manifest(proj)
    assert m.generation_id == r1.generation_id and m.coverage_clock == clock(102) and m.parent_generation_id == r2.generation_id
    assert G.read_receipt(proj / f"gen-{r1.generation_id}").coverage_clock == clock(100)   # receipt original, active = Candidate coverage
    G.validate_published_generation(proj, m)
    # orphan: a receipted directory with no lineage claim (simulate crash after rename with intent cleared/absent)
    c = content(compiler_version="v10")
    cand = make_candidate(root, clock(103), compiler_version="v10")
    orphan_dir = proj / f"gen-{cand.manifest.generation_id}"
    receipt = G.GenerationReceipt(candidate=cand.manifest, coverage_clock=clock(103), source_input_origins=cand.source_input_origins)
    (cand.candidate_dir / G.RECEIPT_FILE).write_bytes(G.receipt_bytes(receipt))
    cand.candidate_dir.rename(orphan_dir)
    # a different-id publish does NOT GC the orphan immediately? — the plan: retained until the next publish, then GC'd as unclaimed
    r4 = publish(root, clock(104), compiler_version="v11")
    assert not orphan_dir.exists()                       # unclaimed orphan GC'd at the next (different-id) publish
    # rebuild the orphan and adopt it with a same-id publish
    cand = make_candidate(root, clock(105), compiler_version="v10")
    (cand.candidate_dir / G.RECEIPT_FILE).write_bytes(G.receipt_bytes(receipt))
    cand.candidate_dir.rename(orphan_dir)
    r5 = publish(root, clock(105), compiler_version="v10")
    assert r5.outcome == "adopted_orphan" and r5.generation_id == cand.manifest.generation_id
    m = G.read_active_manifest(proj)
    assert m.coverage_clock == clock(105) and G.read_receipt(orphan_dir).coverage_clock == clock(103)
    G.validate_published_generation(proj, m)


def test_unreceipted_directory_is_quarantined_and_rebuilt_never_adopted(tmp_path):
    root = tmp_path / "mem"
    proj = _proj(root); proj.mkdir(parents=True)
    cand = make_candidate(root, clock(100))
    gid = cand.manifest.generation_id
    unreceipted = proj / f"gen-{gid}"
    import shutil
    shutil.copytree(cand.candidate_dir, unreceipted)
    (unreceipted / G.RECEIPT_FILE).write_text(json.dumps({"content": cand.manifest.content.to_dict(), "generation_id": gid}))
    with G.projection_publication_authority(root) as auth:
        res = G.publish_generation(root, cand, authority=auth)
    assert res.outcome == "replaced_corrupt" and res.quarantined is not None
    assert (proj / res.quarantined).is_dir() and G.read_receipt(unreceipted).coverage_clock == clock(100)
    G.validate_published_generation(proj, G.read_active_manifest(proj))


def test_leased_generation_is_never_gcd(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    lease = G.lease_path(proj, r1.generation_id)
    lease.parent.mkdir(parents=True, exist_ok=True)
    import os
    fd = os.open(lease, os.O_RDWR | os.O_CREAT)
    assert G._try_lock(fd, exclusive=False)          # a reader holds a shared lease on G1
    try:
        publish(root, clock(101), compiler_version="v8")
        publish(root, clock(102), compiler_version="v9")
        assert (proj / f"gen-{r1.generation_id}").is_dir()   # would be GC'd (older than active+1) but is leased
    finally:
        G._unlock(fd); os.close(fd)
    publish(root, clock(103), compiler_version="v12")
    assert not (proj / f"gen-{r1.generation_id}").exists()  # lease released ⇒ deferred GC proceeds


def test_publish_requires_live_authority_token(tmp_path):
    root = tmp_path / "mem"
    cand = make_candidate(root, clock(100))
    with pytest.raises(G.PublicationAuthorityRequired):
        G.publish_generation(root, cand, authority=None)
    with G.projection_publication_authority(root) as auth:
        pass
    with pytest.raises(G.PublicationAuthorityRequired):          # closed token is not live
        G.publish_generation(root, cand, authority=auth)
    other = tmp_path / "other"
    with G.projection_publication_authority(other) as auth2:
        with pytest.raises(G.PublicationAuthorityRequired):      # token for a different root
            G.publish_generation(root, cand, authority=auth2)


def test_coverage_seal_failure_leaves_old_manifest_and_authority_intact(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    rows_before = len(RA.read_authority_lineage(proj).rows)

    real = G._strict_write_json

    def failing(path, doc):
        if Path(path).name == G.COVERAGE_SEAL_FILE:
            raise OSError("disk full")
        return real(path, doc)

    monkeypatch.setattr(G, "_strict_write_json", failing)
    cand = make_candidate(root, clock(101), compiler_version="v8")
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.CoverageSealUnavailable):
            G.publish_generation(root, cand, authority=auth)
    assert G.read_active_manifest(proj).generation_id == r1.generation_id
    assert G.read_coverage_seal(proj) == clock(100)
    assert len(G.read_publication_lineage(proj / G.LINEAGE_FILE).rows) == 1
    assert not (proj / G.PUBLICATION_INTENT_FILE).exists()
