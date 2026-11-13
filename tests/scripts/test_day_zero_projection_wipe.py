"""Memory-organ Cut 13 — day-zero projection wipe + quiescence (D-13-65/70/75): the complete
operator action removes every projection runtime surface AND the clock in one run, preserves
every package file and the four infrastructure lock files, refuses (before any deletion)
while a REAL child holds a reader lease / the publication authority / the outbox lock /
the lease namespace / the lifecycle interlock, and the verifier detects residue even when
guardian/state is gone."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import scripts.day_zero_reset as dz
from core import timekeeper as TK
from memory.projection import generation as G
from tests.memory.projection._fixtures import build_and_publish, redirect_timekeeper, seed_records_only

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    redirect_timekeeper(tmp_path)
    monkeypatch.setattr(dz, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(dz, "GUARDIAN_STATE_ROOT", tmp_path / "guardian" / "state")
    yield
    TK.reset_for_tests()


def _seed(tmp_path):
    profile = tmp_path
    seed_records_only(profile)                                                   # profile == OMEGA_ROOT (memory/ inside)
    cand, res = build_and_publish(profile)
    build_and_publish(profile)                                                   # a retained generation too
    proj = G.projection_root(profile)
    # extra runtime surfaces: quarantine, leases, intents, Cut-4 outbox + dirty marker, torn evidence, seal (already), infra locks
    (proj / "quarantine" / "gen-x.corrupt-1").mkdir(parents=True); (proj / "quarantine" / "gen-x.corrupt-1" / "sram.bin").write_bytes(b"x")
    (proj / "leases").mkdir(exist_ok=True); (proj / "leases" / ("a" * 64 + ".lease")).write_bytes(b"")
    (proj / "publication_intent.json").write_text("{}"); (proj / "lineage.torn-1").write_bytes(b"{")
    (proj / "work-abc").mkdir(); (proj / "work-abc" / "f").write_bytes(b"x")
    (proj / "manifest.json.tmp").write_bytes(b"{")
    idx = profile / "memory" / "indexes"; idx.mkdir(exist_ok=True)
    (idx / "projection_delta.jsonl").write_bytes(b'{"delta":1}\n'); (idx / "projection_dirty.json").write_text("{}")
    (idx / "projection_delta.lock").write_bytes(b""); (proj / "lease_namespace.lock").write_bytes(b"")
    (tmp_path / "guardian").mkdir(exist_ok=True); (tmp_path / "guardian" / "lifecycle.lock").write_bytes(b"")
    assert TK._state_path().exists()                                             # guardian/state/timekeeper.json
    return profile, proj, res


def _package_files(proj):
    return sorted(str(p.relative_to(proj)) for p in proj.rglob("*.py"))


def test_complete_reset_removes_runtime_state_and_clock_preserves_package_and_locks(tmp_path):
    profile, proj, res = _seed(tmp_path)
    (proj / "__init__.py").write_text("# package marker in a scratch profile\n")   # a package file that MUST survive
    with dz.projection_reset_exclusion(profile / "memory") as authority:
        assert len(authority.handles) >= 5                                        # lifecycle + publication + outbox + namespace + lease
        removed = dz.wipe_projection_runtime(profile / "memory")
        dz.wipe_guardian_state_completely()                                       # the clock goes in the SAME invocation
    assert removed and not (proj / G.MANIFEST_FILE).exists() and not any(p.name.startswith("gen-") for p in proj.iterdir())
    for name in ("quarantine", "leases", "registry_authority", "publication_intent.json", "lineage.torn-1", "work-abc",
                 "manifest.json.tmp", "coverage_seal.json", "lineage.jsonl", "repair_lineage.jsonl"):
        assert not (proj / name).exists(), name
    assert not (profile / "memory" / "indexes" / "projection_delta.jsonl").exists()
    assert not (profile / "memory" / "indexes" / "projection_dirty.json").exists()
    assert (proj / "__init__.py").exists()                                        # package file preserved
    for lock in ("publication.lock", "lease_namespace.lock"):
        assert (proj / lock).exists()                                             # infrastructure locks preserved
    assert (profile / "memory" / "indexes" / "projection_delta.lock").exists()
    assert (tmp_path / "guardian" / "lifecycle.lock").exists()
    assert not TK._state_path().exists()                                          # timekeeper.json removed with the projection state
    assert dz.projection_runtime_residue(profile / "memory") == []
    dz.verify_day_zero_surfaces()                                                 # projection surfaces checked even with guardian/state gone
    # next boot mints a NEW epoch and may publish an initial head (no old head survives)
    TK.reset_for_tests()
    seed_records_only(profile) if not (profile / "memory" / "200_knowledge").exists() else None
    cand, res2 = build_and_publish(profile)
    assert res2.outcome == "published_new" and res2.active_manifest.coverage_clock.clock_epoch_id != res.active_manifest.coverage_clock.clock_epoch_id


def test_verifier_detects_projection_residue_even_without_guardian_state(tmp_path):
    profile, proj, res = _seed(tmp_path)
    dz.wipe_guardian_state_completely()
    with pytest.raises(dz.DayZeroResetError, match="projection runtime state survived"):
        dz.verify_day_zero_surfaces()


def test_clock_only_and_projection_only_postures(tmp_path):
    profile, proj, res = _seed(tmp_path)
    # clock-only deletion with projection state intact ⇒ regression (never a new head over the old projection)
    TK._state_path().unlink(); TK.reset_for_tests()
    from memory.projection import build as B
    with G.projection_publication_authority(profile) as auth:
        cand = B.build_generation(profile, reason="x", authority=auth)
        with pytest.raises(G.CoverageClockRegression):
            G.publish_generation(profile, cand, authority=auth)
    # projection-only deletion with the OLD clock ⇒ a new initial head in the SAME epoch is lawful
    profile2 = tmp_path / "p2"; profile2.mkdir(); seed_records_only(profile2)
    c1, r1 = build_and_publish(profile2)
    epoch = r1.active_manifest.coverage_clock.clock_epoch_id
    with dz.projection_reset_exclusion(profile2 / "memory", lifecycle_lock=tmp_path / "guardian" / "lc2.lock"):
        dz.wipe_projection_runtime(profile2 / "memory")
    c2, r2 = build_and_publish(profile2)
    assert r2.outcome == "published_new" and r2.active_manifest.coverage_clock.clock_epoch_id == epoch


CHILD = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    sys.path.insert(0, %r)
    from memory.projection import generation as G
    mode, target = sys.argv[1], Path(sys.argv[2])
    if mode == "reader":
        with G.projection_read_transaction(target):
            print("HELD", flush=True); time.sleep(60)
    else:
        fd = G._open_lock_handle(target)
        assert G._try_lock(fd, exclusive=(mode == "exclusive"))
        print("HELD", flush=True); time.sleep(60)
    """
)


@pytest.mark.parametrize("mode,which", [("reader", "profile"), ("exclusive", "publication.lock"), ("exclusive", "projection_delta.lock"),
                                        ("shared", "lease_namespace.lock"), ("shared", "lifecycle.lock")])
def test_reset_refuses_before_any_deletion_while_a_real_child_holds_a_lock(tmp_path, mode, which):
    profile, proj, res = _seed(tmp_path)
    if which == "profile":
        target = profile
    elif which == "projection_delta.lock":
        target = profile / "memory" / "indexes" / which
    elif which == "lifecycle.lock":
        target = tmp_path / "guardian" / which
    else:
        target = proj / which
    child = subprocess.Popen([sys.executable, "-c", CHILD % str(REPO), mode, str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "HELD"
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    try:
        with pytest.raises(dz.DayZeroResetError, match="refusing day-zero"):
            with dz.projection_reset_exclusion(profile / "memory"):
                dz.wipe_projection_runtime(profile / "memory")
    finally:
        child.kill(); child.wait(timeout=10)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert after == before and TK._state_path().exists()                          # nothing deleted, clock intact
    # killed holder ⇒ the next reset succeeds
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with dz.projection_reset_exclusion(profile / "memory"):
                break
        except dz.DayZeroResetError:
            time.sleep(0.05)
    with dz.projection_reset_exclusion(profile / "memory"):
        dz.wipe_projection_runtime(profile / "memory")
    assert dz.projection_runtime_residue(profile / "memory") == []


def test_new_reader_cannot_enrol_while_reset_holds_the_namespace(tmp_path):
    profile, proj, res = _seed(tmp_path)
    (proj / "leases").mkdir(exist_ok=True)
    for p in (proj / "leases").glob("*.lease"):
        p.unlink()                                                                # no held leases
    with dz.projection_reset_exclusion(profile / "memory"):
        G._LEASE_NAMESPACE_WAIT, saved = 0.2, G._LEASE_NAMESPACE_WAIT
        try:
            with G.projection_read_transaction(profile) as ctx:
                assert ctx.mode == "unavailable" and "namespace" in ctx.reason  # enrolment refused, never a doomed generation
        finally:
            G._LEASE_NAMESPACE_WAIT = saved
    with G.projection_read_transaction(profile) as ctx:
        assert ctx.mode == "generation"                                           # after release enrolment resumes


def test_startup_racing_the_reset_is_excluded_by_the_lifecycle_interlock(tmp_path, monkeypatch):
    profile, proj, res = _seed(tmp_path)
    import core.paths as core_paths
    monkeypatch.setattr(core_paths, "OMEGA_ROOT", tmp_path)
    from memory.projection import dispatch as D
    D.release_lifecycle_lock()
    with dz.projection_reset_exclusion(profile / "memory"):
        assert D.hold_lifecycle_lock() is False                                    # boot must not proceed
    assert D.hold_lifecycle_lock() is True
    D.release_lifecycle_lock()


def test_reader_enrolling_in_the_gap_before_the_namespace_gate_still_blocks_reset(tmp_path):
    """Review 73646a37 §2 (D-13-75): reset pauses immediately BEFORE the lease-namespace
    acquisition; a REAL reader enrols in that gap (namespace still open); reset resumes —
    it must refuse on the reader's live generation lease, never yield quiescence over it.
    (Lease enumeration happens only AFTER the namespace is held exclusively.)"""
    profile, proj, res = _seed(tmp_path)
    for p in (proj / "leases").glob("*.lease"):
        p.unlink()                                                                # no pre-existing leases
    child_holder: dict = {}

    def pause_before_namespace(stage: str) -> None:
        if stage == "lease namespace" and "proc" not in child_holder:
            proc = subprocess.Popen([sys.executable, "-c", CHILD % str(REPO), "reader", str(profile)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            child_holder["proc"] = proc
            assert proc.stdout.readline().strip() == "HELD"                        # enrolled + holding a generation lease
            child_holder["stages"] = list(child_holder.get("stages", [])) + [stage]

    dz._exclusion_hook = pause_before_namespace
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    try:
        with pytest.raises(dz.DayZeroResetError, match="generation lease"):
            with dz.projection_reset_exclusion(profile / "memory"):
                dz.wipe_projection_runtime(profile / "memory")
    finally:
        dz._exclusion_hook = None
        proc = child_holder.get("proc")
        if proc is not None:
            proc.kill(); proc.wait(timeout=10)
    assert "proc" in child_holder                                                  # the reader really enrolled in the gap
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert [x for x in after if not x.endswith(".lease")] == [x for x in before if not x.endswith(".lease")]   # nothing deleted
    assert TK._state_path().exists()
    # killed reader ⇒ the next reset succeeds
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with dz.projection_reset_exclusion(profile / "memory"):
                dz.wipe_projection_runtime(profile / "memory")
            break
        except dz.DayZeroResetError:
            time.sleep(0.05)
    assert dz.projection_runtime_residue(profile / "memory") == []
