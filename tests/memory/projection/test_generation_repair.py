"""Cut 13 Task A2 — two-stage same-id repair (D-13-25/47/51/55/61): RepairIntent before
the first destructive step, RepairLineageRow embedding the payload, a SEPARATE rebind
PublicationIntent, `replaced_corrupt` only after everything is durable; crash intervals
resume exactly once; tampered evidence fails loud."""

from __future__ import annotations

import json
import os

import pytest

from memory.projection import generation as G
from memory.taxonomy.normalize import canonical_json
from tests.memory.projection._fixtures import clock, dir_state, make_candidate, publish


def _proj(root):
    return G.projection_root(root)


@pytest.fixture(autouse=True)
def _no_crash_hook():
    yield
    G._crash_hook = None


def _crash_at(point):
    def hook(p):
        if p == point:
            raise G.SimulatedCrash(point)
    G._crash_hook = hook


def _corrupt_active(root):
    r1 = publish(root, clock(100))
    proj = _proj(root)
    gen_dir = proj / f"gen-{r1.generation_id}"
    (gen_dir / "sram.bin").write_bytes(b"corrupt-bytes")
    return r1, proj, gen_dir


def _recover(root):
    with G.projection_publication_authority(root) as auth:
        return G.recover_generation_intents(root, authority=auth)


def _assert_repaired(proj, gen_dir, gid, *, expected_clock, expect_rows=1):
    m = G.read_active_manifest(proj)
    assert m.generation_id == gid and m.coverage_clock == expected_clock
    G.validate_published_generation(proj, m)
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert scan.head.event == "replaced_corrupt" and scan.head.repair_row_sha256 is not None
    assert scan.head.generation_receipt_sha256 == G.generation_receipt_sha256(gen_dir)
    rscan = G.read_repair_lineage(proj)
    assert len(rscan.rows) == expect_rows
    assert G.repair_row_sha256(rscan.rows[-1]) == scan.head.repair_row_sha256
    assert not (proj / G.REPAIR_INTENT_FILE).exists() and not (proj / G.PUBLICATION_INTENT_FILE).exists()
    assert (proj / G.QUARANTINE_DIR).is_dir()


def test_repair_happy_path_two_stage(tmp_path):
    root = tmp_path / "mem"
    r1, proj, gen_dir = _corrupt_active(root)
    cand = make_candidate(root, clock(101))
    with G.projection_publication_authority(root) as auth:
        res = G.publish_generation(root, cand, authority=auth)
    assert res.outcome == "replaced_corrupt" and res.quarantined.startswith("quarantine/gen-") and res.repair_row_seq == 1
    assert res.publication_row_written and res.publication_row_seq == 2
    _assert_repaired(proj, gen_dir, r1.generation_id, expected_clock=clock(101))
    # quarantined evidence retained; the replacement receipt is the new one
    assert (proj / res.quarantined / "sram.bin").read_bytes() == b"corrupt-bytes"
    assert G.read_receipt(gen_dir).coverage_clock == clock(101)
    rrow = G.read_repair_lineage(proj).rows[0]
    assert rrow.intent.generation_id == r1.generation_id and "repair_row_sha256" not in rrow.intent.to_dict()
    assert G.repair_intent_sha256(rrow.intent) == rrow.intent_sha256      # reproducible after both intents are cleared


def test_equal_coverage_but_different_receipt_bytes_still_rebinds(tmp_path):
    root = tmp_path / "mem"
    r1, proj, gen_dir = _corrupt_active(root)
    cand = make_candidate(root, clock(100), origins={"canonical_corpus": "authority:R1", "source_ledger": "legacy:x"})
    with G.projection_publication_authority(root) as auth:
        res = G.publish_generation(root, cand, authority=auth)
    assert res.outcome == "replaced_corrupt" and res.publication_row_written and res.coverage_advanced is False
    _assert_repaired(proj, gen_dir, r1.generation_id, expected_clock=clock(100))


def test_corrupt_generation_json_itself_is_replaced_without_old_receipt(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    gen_dir = proj / f"gen-{r1.generation_id}"
    (gen_dir / G.RECEIPT_FILE).write_bytes(b"{not json")
    cand = make_candidate(root, clock(101))
    with G.projection_publication_authority(root) as auth:
        res = G.publish_generation(root, cand, authority=auth)
    assert res.outcome == "replaced_corrupt"
    _assert_repaired(proj, gen_dir, r1.generation_id, expected_clock=clock(101))


@pytest.mark.parametrize("point", ["after_repair_intent", "after_quarantine", "after_candidate_rename", "after_repair_row",
                                   "after_rebind_intent", "after_row", "after_manifest"])
def test_repair_intervals_resume_exactly_once(tmp_path, point):
    root = tmp_path / "mem"
    r1, proj, gen_dir = _corrupt_active(root)
    cand = make_candidate(root, clock(101))
    _crash_at(point)
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    ri = G.read_repair_intent(proj)
    assert ri is not None                                       # the RepairIntent is durable BEFORE the quarantine rename
    if point == "after_repair_intent":
        assert not (proj / ri.payload.quarantine_path).exists() and (gen_dir / "sram.bin").read_bytes() == b"corrupt-bytes"
    if point in ("after_repair_row", "after_rebind_intent", "after_row", "after_manifest"):
        assert len(G.read_repair_lineage(proj).rows) == 1
    rep = _recover(root)
    assert rep.status == "completed" and "repair_completed" in rep.actions
    _assert_repaired(proj, gen_dir, r1.generation_id, expected_clock=clock(101))
    # a second recovery / retry appends neither a duplicate repair row nor a duplicate rebind row
    rep2 = _recover(root)
    assert rep2.status == "clean"
    assert len(G.read_repair_lineage(proj).rows) == 1 and len(G.read_publication_lineage(proj / G.LINEAGE_FILE).rows) == 2


def test_crash_after_repair_intent_cancels_when_generation_revalidates(tmp_path):
    root = tmp_path / "mem"
    r1, proj, gen_dir = _corrupt_active(root)
    cand = make_candidate(root, clock(101))
    _crash_at("after_repair_intent")
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    from tests.memory.projection._fixtures import ARTIFACT_BYTES
    (gen_dir / "sram.bin").write_bytes(ARTIFACT_BYTES["sram.bin"])       # "the corruption was transient": gen-G re-validates
    before = dir_state(gen_dir)
    rep = _recover(root)
    assert rep.status == "cancelled" and "repair_cancelled" in rep.actions
    assert dir_state(gen_dir) == before and not (proj / G.REPAIR_INTENT_FILE).exists()
    assert not (proj / G.QUARANTINE_DIR).exists() and not cand.candidate_dir.exists()
    assert G.read_active_manifest(proj).generation_id == r1.generation_id


def test_two_historical_quarantine_dirs_are_unambiguous_and_bounded(tmp_path):
    root = tmp_path / "mem"
    r1, proj, gen_dir = _corrupt_active(root)
    with G.projection_publication_authority(root) as auth:
        G.publish_generation(root, make_candidate(root, clock(101)), authority=auth)
    for seq in (102, 103, 104):
        (gen_dir / "sram.bin").write_bytes(b"corrupt-again-%d" % seq)
        with G.projection_publication_authority(root) as auth:
            res = G.publish_generation(root, make_candidate(root, clock(seq)), authority=auth)
        assert res.outcome == "replaced_corrupt"
    qdirs = sorted(p.name for p in (proj / G.QUARANTINE_DIR).iterdir())
    assert len(qdirs) == G.QUARANTINE_KEEP                                  # bounded to the last 2
    assert len(G.read_repair_lineage(proj).rows) == 4
    G.validate_published_generation(proj, G.read_active_manifest(proj))


def test_tampered_repair_intent_or_row_fails_loud(tmp_path):
    root = tmp_path / "mem"
    r1, proj, gen_dir = _corrupt_active(root)
    cand = make_candidate(root, clock(101))
    _crash_at("after_repair_row")
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    # tamper the committed repair row's intent payload
    path = proj / G.REPAIR_LINEAGE_FILE
    row = json.loads(path.read_bytes().split(b"\n")[0])
    row["intent"]["corrupt_tree_sha256"] = "f" * 64
    path.write_bytes(canonical_json(row).encode() + b"\n")
    with pytest.raises(G.RecoveryOperatorRequired):
        _recover(root)
    assert G.read_publication_lineage(proj / G.LINEAGE_FILE).head.event == "published_new"   # no replaced_corrupt barrier
    # tampered repair intent file
    (proj / G.REPAIR_INTENT_FILE).write_bytes(b"{tampered")
    with pytest.raises(G.RecoveryOperatorRequired):
        _recover(root)


def test_repair_blocked_on_live_lease_is_bounded(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    r1, proj, gen_dir = _corrupt_active(root)
    lease = G.lease_path(proj, r1.generation_id)
    lease.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lease, os.O_RDWR | os.O_CREAT)
    assert G._try_lock(fd, exclusive=False)
    monkeypatch.setattr(G, "LEASE_WAIT_SECONDS", 0.3)
    try:
        cand = make_candidate(root, clock(101))
        with G.projection_publication_authority(root) as auth:
            with pytest.raises(G.PublicationBlockedOnLeases):
                G.publish_generation(root, cand, authority=auth)
        assert not (proj / G.REPAIR_INTENT_FILE).exists() and (gen_dir / "sram.bin").read_bytes() == b"corrupt-bytes"
    finally:
        G._unlock(fd); os.close(fd)
