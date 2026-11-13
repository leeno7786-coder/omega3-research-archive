"""D-13-45 — orphan adoption from the durable receipt alone (crash after rename, no
in-memory candidate on restart, no live intent)."""

from __future__ import annotations

from memory.projection import generation as G
from tests.memory.projection._fixtures import clock, make_candidate, publish


def test_adopted_orphan_from_receipt_alone(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = G.projection_root(root)
    orphan = make_candidate(root, clock(101), compiler_version="v8")
    receipt = G.GenerationReceipt(candidate=orphan.manifest, coverage_clock=clock(101), source_input_origins=orphan.source_input_origins)
    (orphan.candidate_dir / G.RECEIPT_FILE).write_bytes(G.receipt_bytes(receipt))
    orphan_dir = proj / f"gen-{orphan.manifest.generation_id}"
    orphan.candidate_dir.rename(orphan_dir)                                        # crash after rename, before any row; no intent
    with G.projection_publication_authority(root) as auth:
        rep = G.recover_generation_intents(root, authority=auth)                   # startup: retained, NOT adopted silently
    assert rep.status == "clean" and orphan_dir.is_dir() and G.read_active_manifest(proj).generation_id == r1.generation_id
    got = G.validate_generation_directory(orphan_dir, receipt)                     # valid orphan by its receipt
    assert got == receipt
    r2 = publish(root, clock(102), compiler_version="v8")                          # same id ⇒ adopted, current Candidate coverage
    assert r2.outcome == "adopted_orphan" and r2.generation_id == orphan.manifest.generation_id
    assert G.read_active_manifest(proj).coverage_clock == clock(102) and G.read_receipt(orphan_dir).coverage_clock == clock(101)
