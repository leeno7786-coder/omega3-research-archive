"""Cut 13 Task A2 — the global coverage-clock identity gate (D-13-58/59) and same-id
coverage publication (D-13-49/54/60): epoch mismatch or lower sequence is refused for
EVERY event before any authority / intent / rename / repair mutation.

The timekeeper reservation/landed machinery (D-13-73/74/77) is exercised in Task A4
where the builder captures the clock; here the gate is driven with Candidate clocks.
"""

from __future__ import annotations

import pytest

from memory.projection import generation as G
from memory.projection import registry_authority as RA
from tests.memory.projection._fixtures import EPOCH_A, EPOCH_B, clock, dir_state, make_candidate, publish


def _proj(root):
    return G.projection_root(root)


def _snapshot(proj):
    return {p.relative_to(proj).as_posix(): (p.stat().st_size if p.is_file() else None) for p in sorted(proj.rglob("*"))}


def _refused(root, clk, **kw):
    proj = _proj(root)
    before = _snapshot(proj)
    cand = make_candidate(root, clk, **kw)
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.CoverageClockRegression) as ei:
            G.publish_generation(root, cand, authority=auth)
    after = _snapshot(proj)
    # only the (receiptless) candidate directory may differ; no authority / intent / rename / repair / manifest / seal change
    diff = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert all(k.startswith(cand.candidate_dir.name) for k in diff), diff
    assert not (cand.candidate_dir / G.RECEIPT_FILE).exists()
    return ei.value


def test_gate_function_semantics():
    assert G.coverage_gate(None, clock(5), event="publish") is False
    assert G.coverage_gate(clock(100), clock(100), event="publish") is False
    assert G.coverage_gate(clock(100), clock(101), event="publish") is True
    with pytest.raises(G.CoverageClockRegression):
        G.coverage_gate(clock(100), clock(99), event="publish")
    with pytest.raises(G.CoverageClockRegression):
        G.coverage_gate(clock(100), clock(101, EPOCH_B), event="publish")      # larger number, different epoch ⇒ refused
    with pytest.raises(G.CoverageClockRegression):
        G.coverage_gate(clock(100), clock(100, EPOCH_B), event="publish")


def test_head_A100_candidate_A99_refused_before_any_mutation_for_every_event(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    # different-id new publication
    err = _refused(root, clock(99), compiler_version="v8")
    assert err.head == clock(100) and err.candidate == clock(99)
    # same-id (would be reuse) lower coverage
    _refused(root, clock(99))
    # retained generation lower coverage
    publish(root, clock(101), compiler_version="v8")                     # G1 retained
    _refused(root, clock(100))                                            # G1's content at 100 < head 101 ⇒ no activation row
    assert G.read_active_manifest(proj).coverage_clock == clock(101)
    # orphan lower coverage
    cand = make_candidate(root, clock(50), compiler_version="v9")
    receipt = G.GenerationReceipt(candidate=cand.manifest, coverage_clock=clock(50), source_input_origins=cand.source_input_origins)
    (cand.candidate_dir / G.RECEIPT_FILE).write_bytes(G.receipt_bytes(receipt))
    cand.candidate_dir.rename(proj / f"gen-{cand.manifest.generation_id}")
    _refused(root, clock(50), compiler_version="v9")
    assert (proj / f"gen-{cand.manifest.generation_id}").is_dir()          # orphan untouched
    # repair candidate lower coverage: corrupt G1's directory first
    g1 = proj / f"gen-{r1.generation_id}"
    (g1 / "sram.bin").write_bytes(b"corrupt")
    _refused(root, clock(100))                                            # no RepairIntent, no quarantine
    assert not (proj / G.REPAIR_INTENT_FILE).exists() and not (proj / G.QUARANTINE_DIR).exists()


def test_epoch_mismatch_refused_despite_larger_number_and_catch_up_never_clears(tmp_path):
    root = tmp_path / "mem"
    publish(root, clock(100))
    for seq in (101, 100, 150, 1000):
        _refused(root, clock(seq, EPOCH_B), compiler_version="v8")
    # a full clock+projection reset (new epoch, no projection state) establishes a new initial head
    import shutil
    shutil.rmtree(_proj(root))
    r = publish(root, clock(1, EPOCH_B))
    assert r.outcome == "published_new" and r.active_manifest.coverage_clock == clock(1, EPOCH_B)
    # a clock-only change with projection state intact stays refused
    _refused(root, clock(5, EPOCH_A), compiler_version="v8")


def test_same_epoch_equal_and_greater_are_lawful(tmp_path):
    root = tmp_path / "mem"
    publish(root, clock(100))
    r2 = publish(root, clock(100), compiler_version="v8")
    assert r2.outcome == "published_new" and r2.coverage_advanced is False   # equal coverage, new content
    r3 = publish(root, clock(101), compiler_version="v9")
    assert r3.coverage_advanced is True


def test_reactivated_retained_uses_candidate_coverage_and_recovers_from_receipt_plus_head(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    publish(root, clock(101), compiler_version="v8")
    r3 = publish(root, clock(102))
    proj = _proj(root)
    assert r3.outcome == "reactivated_retained"
    gen_dir = proj / f"gen-{r1.generation_id}"
    assert G.read_receipt(gen_dir).coverage_clock == clock(100)
    m = G.read_active_manifest(proj)
    assert m.coverage_clock == clock(102)
    # corrupt the outer manifest: recovery reconstructs from receipt + head (LATEST coverage, not the original)
    (proj / G.MANIFEST_FILE).write_bytes(b"{garbage")
    with pytest.raises(G.GenerationIntegrityError):
        G.read_active_manifest(proj)
    head = G.read_publication_lineage(proj / G.LINEAGE_FILE).head
    rebuilt = G.construct_manifest(G.read_receipt(gen_dir), head, builder="cut13", reason="recover", diagnostics=G._default_diagnostics("posix_fsync"))
    assert rebuilt.coverage_clock == clock(102)
    G._activate(proj, rebuilt)
    G.validate_published_generation(proj, G.read_active_manifest(proj))


def test_authority_rows_never_churn_on_reuse_or_refusal(tmp_path):
    root = tmp_path / "mem"
    publish(root, clock(100))
    proj = _proj(root)
    publish(root, clock(100)); publish(root, clock(101)); publish(root, clock(101))
    assert len(RA.read_authority_lineage(proj).rows) == 1
    # a changed finalized registry ⇒ exactly one transition
    publish(root, clock(102), registry=b'{"assignments":{"m1":[0,0,0],"m2":[0,0,1]}}')
    assert len(RA.read_authority_lineage(proj).rows) == 2
