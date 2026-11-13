"""Cut 13 Task A2 — content-addressed CoordinateRegistry authority (D-13-35/42/46):
idempotent persist, exactly one transition per changed registry, truthful provenance
rows, strict selection, retention set, torn-tail policy."""

from __future__ import annotations

import json

import pytest

from memory.projection import generation as G
from memory.projection import registry_authority as RA
from tests.memory.projection._fixtures import clock, make_candidate, publish, sha

R0 = b'{"assignments":{"m1":[0,0,0]}}'
R1 = b'{"assignments":{"m1":[0,0,0],"m2":[0,0,1]}}'


def test_persist_is_idempotent_and_transitions_once(tmp_path):
    root = tmp_path / "mem"; proj = G.projection_root(root); proj.mkdir(parents=True)
    with G.projection_publication_authority(root) as auth:
        a = RA.persist_authority(proj, R0, authority=auth, introduced_by_candidate_id="c" * 64, selected_at="t1")
        assert a.transition and a.snapshot_written and a.seq == 1
        b = RA.persist_authority(proj, R0, authority=auth, introduced_by_candidate_id="d" * 64, selected_at="t2")
        assert not b.transition and not b.snapshot_written and b.seq == 1       # unchanged ⇒ no snapshot, no row, no manifest
        c = RA.persist_authority(proj, R1, authority=auth, introduced_by_candidate_id="e" * 64, selected_at="t3")
        assert c.transition and c.seq == 2 and RA.select_authority(proj) == (sha(R1), 2)
        d = RA.persist_authority(proj, R0, authority=auth, introduced_by_candidate_id=None, selected_at="t4")
        assert d.transition and not d.snapshot_written and d.seq == 3           # snapshot exists but head differs ⇒ one transition
    rows = RA.read_authority_lineage(proj).rows
    assert [r.registry_sha256 for r in rows] == [sha(R0), sha(R1), sha(R0)]
    assert rows[0].introduced_by_candidate_id == "c" * 64 and rows[2].introduced_by_candidate_id is None   # provenance only
    assert (proj / "registry_authority" / f"registry-{sha(R0)}.json").read_bytes() == R0


def test_selection_is_strict_never_empty(tmp_path):
    root = tmp_path / "mem"; proj = G.projection_root(root); proj.mkdir(parents=True)
    with G.projection_publication_authority(root) as auth:
        RA.persist_authority(proj, R0, authority=auth, introduced_by_candidate_id=None, selected_at="t")
    (proj / "registry_authority" / f"registry-{sha(R0)}.json").write_bytes(b"corrupt")
    with pytest.raises(G.GenerationIntegrityError):
        RA.select_authority(proj)
    (proj / "registry_authority" / "manifest.json").write_bytes(b"{garbage")
    with pytest.raises(G.GenerationIntegrityError):
        RA.select_authority(proj)


def test_failed_generation_publication_keeps_truthful_row_and_next_build_reuses_R1(tmp_path, monkeypatch):
    root = tmp_path / "mem"; proj = G.projection_root(root)
    publish(root, clock(100), registry=R0)
    # publication that selects R1 then fails at the seal step
    monkeypatch.setattr(G, "ensure_coverage_seal", lambda *a, **k: (_ for _ in ()).throw(G.CoverageSealUnavailable("x")))
    cand = make_candidate(root, clock(101), registry=R1)
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.CoverageSealUnavailable):
            G.publish_generation(root, cand, authority=auth)
    rows = RA.read_authority_lineage(proj).rows
    assert len(rows) == 2 and RA.select_authority(proj)[0] == sha(R1)         # R1 selected + truthful provenance row
    assert G.read_publication_lineage(proj / G.LINEAGE_FILE).head.generation_id != cand.manifest.generation_id
    monkeypatch.undo()
    r = publish(root, clock(101), registry=R1)                                 # next build reuses R1: no new authority row
    assert r.outcome == "published_new" and len(RA.read_authority_lineage(proj).rows) == 2


def test_torn_final_authority_row_is_settled_by_the_frozen_policy(tmp_path):
    root = tmp_path / "mem"; proj = G.projection_root(root); proj.mkdir(parents=True)
    with G.projection_publication_authority(root) as auth:
        RA.persist_authority(proj, R0, authority=auth, introduced_by_candidate_id=None, selected_at="t")
        (proj / "registry_authority" / "lineage.jsonl").open("ab").write(b'{"seq": 2, "registry_sha256": "')
        scan = RA.read_authority_lineage(proj)
        assert scan.torn_tail is not None and len(scan.rows) == 1
        res = RA.persist_authority(proj, R1, authority=auth, introduced_by_candidate_id=None, selected_at="t2")
        assert res.seq == 2
    assert (proj / "registry_authority" / "lineage.torn-1").exists()
    assert [r.seq for r in RA.read_authority_lineage(proj).rows] == [1, 2]


def test_retention_set(tmp_path):
    root = tmp_path / "mem"; proj = G.projection_root(root); proj.mkdir(parents=True)
    with G.projection_publication_authority(root) as auth:
        RA.persist_authority(proj, R0, authority=auth, introduced_by_candidate_id=None, selected_at="t")
        RA.persist_authority(proj, R1, authority=auth, introduced_by_candidate_id=None, selected_at="t")
    keep = RA.retained_registry_hashes(proj, generation_bound={sha(R0)}, in_progress={"9" * 64})
    assert keep == {sha(R0), sha(R1), "9" * 64}
