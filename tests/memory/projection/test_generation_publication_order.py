"""Cut 13 Task A2 — existence-first order (spy) and the publication-intent crash machine
(D-13-41/54/60): every interval resumes deterministically; a lost row is never invented."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memory.projection import generation as G
from memory.projection import registry_authority as RA
from tests.memory.projection._fixtures import clock, content, dir_state, make_candidate, publish


def _proj(root):
    return G.projection_root(root)


@pytest.fixture(autouse=True)
def _no_crash_hook():
    yield
    G._crash_hook = None


def _crash_at(point: str):
    def hook(p):
        if p == point:
            raise G.SimulatedCrash(point)
    G._crash_hook = hook


def _recover(root):
    with G.projection_publication_authority(root) as auth:
        return G.recover_generation_intents(root, authority=auth)


def test_order_spy_validate_gate_receipt_authority_seal_intent_rename_row_manifest(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    order: list[str] = []
    def wrap(name, fn):
        return lambda *a, **k: (order.append(name), fn(*a, **k))[1]
    monkeypatch.setattr(G, "validate_candidate", wrap("validate", G.validate_candidate))
    monkeypatch.setattr(G, "coverage_gate", wrap("gate", G.coverage_gate))
    real_atomic = G.__dict__.get("atomic_replace_bytes")
    import memory.atomic_replace as AR
    real_arb = AR.atomic_replace_bytes

    def arb(path, data):
        name = Path(path).name
        if name == G.RECEIPT_FILE:
            order.append("receipt")
        elif name == G.COVERAGE_SEAL_FILE:
            order.append("seal")
        elif name == G.PUBLICATION_INTENT_FILE:
            order.append("intent")
        elif name == G.MANIFEST_FILE and Path(path).parent.name != "registry_authority":
            order.append("manifest")
        elif name.startswith("registry-") or Path(path).parent.name == "registry_authority":
            order.append("authority")
        return real_arb(path, data)

    monkeypatch.setattr(AR, "atomic_replace_bytes", arb)
    monkeypatch.setattr(RA, "atomic_replace_bytes", arb)
    monkeypatch.setattr(RA, "persist_authority", wrap("persist_authority", RA.persist_authority))
    monkeypatch.setattr(os, "rename", wrap("rename", os.rename))
    monkeypatch.setattr(G, "append_publication_row", wrap("row", G.append_publication_row))
    monkeypatch.setattr(G, "_clear_intent", wrap("intent_clear", G._clear_intent))
    cand = make_candidate(root, clock(100))
    with G.projection_publication_authority(root) as auth:
        G.publish_generation(root, cand, authority=auth)
    seq = [o for o in order if o in ("validate", "gate", "receipt", "persist_authority", "seal", "intent", "rename", "row", "manifest", "intent_clear")]
    # dedupe consecutive repeats
    compact = [seq[0]] + [s for i, s in enumerate(seq[1:], 1) if s != seq[i - 1]]
    assert compact == ["validate", "gate", "receipt", "persist_authority", "seal", "intent", "rename", "row", "manifest", "intent_clear"]


def test_refused_candidate_advances_no_authority_head(tmp_path):
    root = tmp_path / "mem"
    publish(root, clock(100))
    proj = _proj(root)
    rows = len(RA.read_authority_lineage(proj).rows)
    cand = make_candidate(root, clock(99), compiler_version="v8", registry=b'{"assignments":{"m1":[0,0,0],"m2":[0,0,1]}}')
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.CoverageClockRegression):
            G.publish_generation(root, cand, authority=auth)
    assert len(RA.read_authority_lineage(proj).rows) == rows          # gate ran BEFORE persist_authority
    assert not (proj / G.PUBLICATION_INTENT_FILE).exists()
    assert not (cand.candidate_dir / G.RECEIPT_FILE).exists()         # gate ran BEFORE receipt persistence


# ── crash machine: published_new ────────────────────────────────────────────

@pytest.mark.parametrize("point", ["after_intent", "after_rename", "after_row", "after_manifest", "before_intent_clear"])
def test_published_new_intervals_resume_the_original_event(tmp_path, point):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    cand = make_candidate(root, clock(101), compiler_version="v8")
    gid = cand.manifest.generation_id
    _crash_at(point)
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    intent = G.read_publication_intent(proj)
    assert intent is not None and intent.row.event == "published_new" and intent.row.seq == 2
    if point == "after_intent":
        assert cand.candidate_dir.exists() and not (proj / f"gen-{gid}").exists()
        assert len(G.read_publication_lineage(proj / G.LINEAGE_FILE).rows) == 1   # no row while the target is absent
    if point == "after_rename":
        assert (proj / f"gen-{gid}").exists() and len(G.read_publication_lineage(proj / G.LINEAGE_FILE).rows) == 1
    assert G.read_active_manifest(proj).generation_id == (gid if point in ("after_manifest", "before_intent_clear") else r1.generation_id)
    rep = _recover(root)
    assert rep.status == "completed" and "published_new_completed" in rep.actions
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert [r.seq for r in scan.rows] == [1, 2] and scan.head.event == "published_new" and scan.head.generation_id == gid  # not adopted_orphan
    m = G.read_active_manifest(proj)
    assert m.generation_id == gid and m.coverage_clock == clock(101)
    G.validate_published_generation(proj, m)
    assert not (proj / G.PUBLICATION_INTENT_FILE).exists() and not cand.candidate_dir.exists()
    # idempotent second recovery: nothing to do, no duplicate row
    rep2 = _recover(root)
    assert rep2.status == "clean" and len(G.read_publication_lineage(proj / G.LINEAGE_FILE).rows) == 2


# ── crash machine: same-id later coverage (reused_existing) ─────────────────

@pytest.mark.parametrize("point", ["after_intent", "after_row", "after_manifest", "before_intent_clear"])
def test_later_coverage_reuse_intervals(tmp_path, point):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    gen_dir = proj / f"gen-{r1.generation_id}"
    before = dir_state(gen_dir)
    cand = make_candidate(root, clock(101))
    _crash_at(point)
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    intent = G.read_publication_intent(proj)
    assert intent is not None and intent.row.event == "reused_existing" and intent.row.coverage_clock == clock(101)
    assert intent.candidate_receipt_sha256 != intent.row.generation_receipt_sha256    # R101 evidence vs R100 target
    if point != "before_intent_clear":
        assert (proj / intent.candidate_path).exists()                                # candidate RETAINED until active validation
    if point == "after_intent":
        assert G.read_active_manifest(proj).coverage_clock == clock(100)             # 100 still active; 101 never invented
    rep = _recover(root)
    assert rep.status == "completed"
    m = G.read_active_manifest(proj)
    assert m.coverage_clock == clock(101) and dir_state(gen_dir) == before
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert len(scan.rows) == 2 and scan.head.generation_receipt_sha256 == G.generation_receipt_sha256(gen_dir)
    G.validate_published_generation(proj, m)
    assert not (proj / intent.candidate_path).exists() and not (proj / G.PUBLICATION_INTENT_FILE).exists()


def test_torn_tail_without_intent_restores_prior_chain_and_reports_incomplete(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    (proj / G.LINEAGE_FILE).open("ab").write(b'{"seq": 2, "event": "published_new", "generation_id": "')
    rep = _recover(root)
    assert rep.status == "publication_incomplete" and "torn_tail_settled" in rep.actions
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert len(scan.rows) == 1 and scan.torn_tail is None
    assert (proj / "lineage.torn-1").read_bytes().startswith(b'{"seq": 2')
    assert G.read_active_manifest(proj).generation_id == r1.generation_id
    # the same dispatch may now build a NEW closed candidate and publish it normally
    r2 = publish(root, clock(101), compiler_version="v8")
    assert r2.publication_row_seq == 2


def test_torn_tail_with_qualifying_intent_supplies_the_exact_row(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    cand = make_candidate(root, clock(101), compiler_version="v8")
    gid = cand.manifest.generation_id
    _crash_at("after_rename")
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    intent = G.read_publication_intent(proj)
    (proj / G.LINEAGE_FILE).open("ab").write(G.publication_row_bytes(intent.row)[:40])   # torn partial row
    rep = _recover(root)
    assert rep.status == "completed"
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert scan.head == intent.row and (proj / "lineage.torn-1").exists()
    G.validate_published_generation(proj, G.read_active_manifest(proj))


def test_intent_with_mismatched_target_receipt_fails_loud_without_inventing_evidence(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    cand = make_candidate(root, clock(101))
    _crash_at("after_intent")
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    # tamper the target receipt after the intent was written
    gen_dir = proj / f"gen-{r1.generation_id}"
    receipt = G.read_receipt(gen_dir)
    (gen_dir / G.RECEIPT_FILE).write_bytes(G.receipt_bytes(G.GenerationReceipt(candidate=receipt.candidate, coverage_clock=clock(100),
                                                                                source_input_origins={"canonical_corpus": "other"})))
    with pytest.raises(G.GenerationIntegrityError):
        _recover(root)
    assert len(G.read_publication_lineage(proj / G.LINEAGE_FILE).rows) == 1
    assert (proj / G.PUBLICATION_INTENT_FILE).exists()                              # left for the operator, not cleared


def test_stale_candidate_cleanup_protects_intent_named_candidate(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    stale = make_candidate(root, clock(101), compiler_version="stale")
    cand = make_candidate(root, clock(101), compiler_version="v8")
    _crash_at("after_intent")
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    assert stale.candidate_dir.exists() and cand.candidate_dir.exists()
    rep = _recover(root)
    assert any(a.startswith("removed_stale_candidate:") for a in rep.actions)
    assert not stale.candidate_dir.exists()
    assert G.read_active_manifest(proj).generation_id == cand.manifest.generation_id   # the intent-named candidate was completed, not deleted


def test_crash_after_gate_before_receipt_leaves_only_a_receiptless_candidate(tmp_path):
    root = tmp_path / "mem"
    publish(root, clock(100))
    proj = _proj(root)
    cand = make_candidate(root, clock(101), compiler_version="v8")
    _crash_at("after_gate")
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(root, cand, authority=auth)
    G._crash_hook = None
    assert not (cand.candidate_dir / G.RECEIPT_FILE).exists() and not (proj / G.PUBLICATION_INTENT_FILE).exists()
    rep = _recover(root)
    assert rep.status == "clean" and not cand.candidate_dir.exists()               # transient candidate cleaned
