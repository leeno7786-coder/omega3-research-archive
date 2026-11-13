"""Program 0 P0.1 — verified cursor + rotate-first recovery (D-13-31/43/56): epoch + physical
identity + every observed byte + consumed prefix verified; mid-line offset ⇒ corrupt; only complete
lines parsed; lawful appends beyond observed_size; atomic replacement / same-size / mutation
detected; recovery rotates FIRST (intent → quarantine → new file → epoch + zero cursor → rebuild);
receipts before build; crash intervals; a REAL second producer cannot append during rotation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import generation as G
from memory.projection import projector as P
from tests.memory.projection._fixtures import build_and_publish, redirect_timekeeper, seed_records_only, write_record

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _env(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    G._crash_hook = None
    TK.reset_for_tests()


def _profile(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    return profile


def _emit(profile, mid, **kw):
    write_record(profile, mid, **kw)
    path = profile / "memory" / "200_knowledge" / f"{mid}.json"
    out = PD.emit_delta_for_canonical(json.loads(path.read_text()), path, root=profile / "memory")
    assert out["durable"] and out["delta"] is not None
    return out["delta"]


def _established(tmp_path):
    profile = _profile(tmp_path)
    rep = P.drain_once(profile, reason="boot")                 # virgin ⇒ epoch 1, initial generation
    assert rep.status == "published_new"
    return profile, G.projection_root(profile), PD.outbox_path(profile / "memory")


def test_snapshot_capture_hashes_boundary_and_settlement_persist_snapshot_values(tmp_path):
    profile, proj, ob = _established(tmp_path)
    d1 = _emit(profile, "k5", title="Glia", summary="glial support cells")
    d2 = _emit(profile, "k6", title="Astro", summary="astrocytes")
    raw = ob.read_bytes()
    with ob.open("ab") as fh:
        fh.write(b'{"partial')                                  # final partial line: pending, never parsed
    cur = P.read_cursor(proj)
    with G.projection_publication_authority(profile) as auth:
        snap = PD.capture_outbox_snapshot(root=profile / "memory", cursor=cur, authority=auth)
    assert snap.captured_size == len(raw) + len(b'{"partial') and snap.complete_boundary == len(raw)
    assert snap.observed_prefix_sha256 == hashlib.sha256(ob.read_bytes()).hexdigest()
    assert snap.consumed_prefix_sha256 == hashlib.sha256(raw).hexdigest() and snap.bytes_to_parse == raw
    deltas, malformed = P.parse_snapshot(snap)
    assert [d.delta_id for d, _s, _e in deltas] == [d1.delta_id, d2.delta_id] and malformed == []
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "published_new" and rep.deltas_seen == 2 and rep.boundary == len(raw)
    cur2 = P.read_cursor(proj)
    # settlement persists THIS snapshot's values (observed_size = captured_size incl. the partial tail; offset = boundary)
    assert cur2.observed_size == snap.captured_size and cur2.outbox_byte_offset == len(raw) and cur2.consumed_prefix_sha256 == snap.consumed_prefix_sha256
    assert cur2.observed_prefix_sha256 == snap.observed_prefix_sha256 and cur2.last_delta_id == d2.delta_id and cur2.generation_id == rep.generation_id
    # completing the partial line + a new row ⇒ lawful append strictly beyond observed_size; the partial line is now a complete (malformed) row
    with ob.open("ab") as fh:
        fh.write(b'"}\n')
    d3 = _emit(profile, "k7", title="Micro", summary="microglia")
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.deltas_seen == 1 and rep2.malformed_row_count == 1 and rep2.status in ("published_new", "reused_existing")
    assert P.read_cursor(proj).last_delta_id == d3.delta_id


@pytest.mark.parametrize("corruption", ["truncate", "mutate_observed", "replace_same_size_same_prefix", "replace_identity", "mid_line_offset", "missing"])
def test_established_verification_failures_rotate_first_then_rebuild(tmp_path, corruption):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    P.drain_once(profile, reason="deepsleep")                    # consumed; cursor binds identity/size/prefix
    _emit(profile, "k6", title="Astro", summary="astrocytes")    # a pending row (part of the file to be quarantined)
    cur = P.read_cursor(proj)
    data = ob.read_bytes()
    if corruption == "truncate":
        ob.write_bytes(data[: cur.observed_size - 3])
    elif corruption == "mutate_observed":
        fd = os.open(ob, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            os.lseek(fd, 5, os.SEEK_SET)                         # a byte inside the previously observed range
            os.write(fd, b"X")                                   # (os.pwrite does not exist on Windows)
        finally:
            os.close(fd)
    elif corruption == "replace_same_size_same_prefix":
        new = bytearray(data); new[-2] = ord("X") if new[-2] != ord("X") else ord("Y")
        tmp = ob.with_suffix(".new"); tmp.write_bytes(bytes(new)); os.replace(tmp, ob)     # same size, first bytes preserved, NEW inode
    elif corruption == "replace_identity":
        tmp = ob.with_suffix(".new"); tmp.write_bytes(data); os.replace(tmp, ob)          # byte-identical, different physical identity
    elif corruption == "mid_line_offset":
        bad = json.loads((proj / PD.OUTBOX_CURSOR_FILE).read_text()); bad["outbox_byte_offset"] -= 1
        (proj / PD.OUTBOX_CURSOR_FILE).write_text(json.dumps(bad))
    elif corruption == "missing":
        ob.unlink()
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "outbox_corrupt_recovered" and rep.recovery_rotated and rep.full_builds == 1, rep.status
    ep, cur2 = P.read_epoch(proj), P.read_cursor(proj)
    assert ep.epoch == 2 and ep.origin == "rotation" and cur2.outbox_file_epoch == 2 and cur2.observed_size == 0 and cur2.outbox_byte_offset == 0
    assert cur2.outbox_file_identity == ep.file_identity and ob.exists() and ob.stat().st_size == 0
    q = list(ob.parent.glob("*.quarantine-1*"))
    assert len(q) == (0 if corruption == "missing" else 1)
    rows = [json.loads(line) for line in (proj / PD.OUTBOX_RECOVERY_FILE).read_text().splitlines()]
    assert [r["stage"] for r in rows] == ["begin", "rotated", "completed"] and rows[0]["old_epoch"] == 1 and rows[0]["new_epoch"] == 2
    # the recovery rebuild covered canonical truth (k6 present) even though its delta was in the quarantined file
    seats = [s["memory_id"] for s in json.loads((proj / f"gen-{rep.generation_id}" / "sram_index.json").read_text())]
    assert "k6" in seats
    # a canonical write after the rebuild appends to the NEW epoch and stays pending until the next drain
    _emit(profile, "k7", title="Micro", summary="microglia")
    assert P.read_cursor(proj).outbox_byte_offset == 0
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.deltas_seen == 1 and rep2.outbox_file_epoch == 2 and rep2.status in ("published_new", "reused_existing")


@pytest.mark.parametrize("point", ["begin", "after_quarantine", "after_create", "after_epoch"])
def test_rotation_crash_intervals_resume_to_one_writable_outbox_and_one_epoch(tmp_path, point, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    P.drain_once(profile, reason="deepsleep")
    _emit(profile, "k6", title="Astro", summary="astrocytes")
    old = ob.read_bytes()
    ob.write_bytes(old[: P.read_cursor(proj).observed_size - 3])  # truncation INTO the observed range ⇒ corrupt
    real = PD.rotate_outbox_epoch_locked

    def crashing(*, root, old_state, outbox_authority):
        if point == "begin":
            # crash right after the begin row: emulate by writing the row then raising
            PD._append_recovery_row(root, {"stage": "begin", "old_epoch": old_state["old_epoch"], "old_identity": old_state["old_identity"],
                                           "new_epoch": int(old_state["old_epoch"]) + 1, "reason": "test", "at": "t"})
            raise G.SimulatedCrash(point)
        # drive the real helper but crash at the named interval by patching the filesystem step
        if point == "after_quarantine":
            PD._append_recovery_row(root, {"stage": "begin", "old_epoch": old_state["old_epoch"], "old_identity": old_state["old_identity"],
                                           "new_epoch": int(old_state["old_epoch"]) + 1, "reason": "test", "at": "t"})
            os.replace(PD.outbox_path(root), PD.outbox_quarantine_path(root, old_state["old_epoch"]))
            raise G.SimulatedCrash(point)
        if point == "after_create":
            PD._append_recovery_row(root, {"stage": "begin", "old_epoch": old_state["old_epoch"], "old_identity": old_state["old_identity"],
                                           "new_epoch": int(old_state["old_epoch"]) + 1, "reason": "test", "at": "t"})
            os.replace(PD.outbox_path(root), PD.outbox_quarantine_path(root, old_state["old_epoch"]))
            PD.outbox_path(root).write_bytes(b"")
            raise G.SimulatedCrash(point)
        if point == "after_epoch":
            PD._append_recovery_row(root, {"stage": "begin", "old_epoch": old_state["old_epoch"], "old_identity": old_state["old_identity"],
                                           "new_epoch": int(old_state["old_epoch"]) + 1, "reason": "test", "at": "t"})
            os.replace(PD.outbox_path(root), PD.outbox_quarantine_path(root, old_state["old_epoch"]))
            PD.outbox_path(root).write_bytes(b"")
            fd = os.open(PD.outbox_path(root), os.O_RDONLY); ident = PD.outbox_file_identity_of(fd); os.close(fd)
            G._strict_write_json(proj / PD.OUTBOX_EPOCH_FILE, {"epoch": 2, "file_identity": ident, "origin": "rotation", "established_at": "t"})
            raise G.SimulatedCrash(point)
        return real(root=root, old_state=old_state, outbox_authority=outbox_authority)

    monkeypatch.setattr(P, "rotate_outbox_epoch_locked", crashing)
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    monkeypatch.setattr(P, "rotate_outbox_epoch_locked", real)
    assert PD.pending_outbox_recovery(profile / "memory") is not None          # the intent drives restart completion
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "outbox_corrupt_recovered" and rep.full_builds == 1
    assert ob.exists() and P.read_epoch(proj).epoch == 2 and P.read_cursor(proj).outbox_file_epoch == 2
    assert len(list(ob.parent.glob("*.quarantine-*"))) == 1                     # exactly one quarantine, one new epoch
    assert PD.pending_outbox_recovery(profile / "memory") is None
    assert P.read_cursor(proj).outbox_file_identity == P.read_epoch(proj).file_identity
    _emit(profile, "k7", title="Micro", summary="microglia")
    assert P.drain_once(profile, reason="deepsleep").deltas_seen == 1


CHILD_APPEND = textwrap.dedent(
    """
    import sys, json, time; sys.path.insert(0, %r)
    from pathlib import Path
    from memory import projection_delta as PD
    root = Path(sys.argv[1])
    d = PD.ProjectionDelta.build(operation="upsert", memory_id="kX", canonical_path="memory/200_knowledge/kX.json", canonical_hash="0"*64,
                                 coord=(2,0,9), commit_seq=0, projection_class="semantic")
    t0 = time.monotonic(); ok = PD.append_projection_delta(d, root=root); dt = time.monotonic() - t0
    print(json.dumps({"ok": ok, "seconds": dt}), flush=True)
    """
)


def test_a_real_second_producer_cannot_append_to_the_old_outbox_during_rotation_and_fails_fast(tmp_path):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    old = ob.read_bytes()
    with G.projection_publication_authority(profile) as auth:
        with PD.outbox_exclusion(profile / "memory", blocking=True) as oa:
            # while rotation holds the exclusion a REAL producer process is refused immediately
            proc = subprocess.run([sys.executable, "-c", CHILD_APPEND % str(REPO), str(profile / "memory")], capture_output=True, text=True, timeout=120)
            res = json.loads(proc.stdout.strip().splitlines()[-1])
            assert res["ok"] is False and res["seconds"] < 1.0, res
            PD.rotate_outbox_epoch_locked(root=profile / "memory", old_state={"old_epoch": 1, "old_identity": P.read_cursor(proj).outbox_file_identity, "reason": "test"}, outbox_authority=oa)
            proc = subprocess.run([sys.executable, "-c", CHILD_APPEND % str(REPO), str(profile / "memory")], capture_output=True, text=True, timeout=120)
            assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is False
        assert auth.live
    q = list(ob.parent.glob("*.quarantine-1"))
    assert len(q) == 1 and q[0].read_bytes() == old and ob.read_bytes() == b""       # nothing appended to the old file
    # after release: the producer appends to the NEW file (the new file existed before release)
    proc = subprocess.run([sys.executable, "-c", CHILD_APPEND % str(REPO), str(profile / "memory")], capture_output=True, text=True, timeout=120)
    assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True and ob.stat().st_size > 0 and q[0].read_bytes() == old


def test_receipt_order_and_crash_before_cursor_is_model_a(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    with ob.open("ab") as fh:
        fh.write(b"not json\n")
    # receipts durable BEFORE the build (spy order)
    order = []
    real_persist, real_build = P.persist_malformed_receipts, P.build_generation
    monkeypatch.setattr(P, "persist_malformed_receipts", lambda proj_, rows: (order.append("receipts"), real_persist(proj_, rows))[1])
    monkeypatch.setattr(P, "build_generation", lambda *a, **k: (order.append("build"), real_build(*a, **k))[1])
    # crash after the barrier before the cursor
    G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == "p0:after_barrier" else None
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    G._crash_hook = None
    assert order == ["receipts", "build"]
    assert P.read_cursor(proj).outbox_byte_offset == 0 or P.read_cursor(proj).last_delta_id is None   # cursor NOT advanced
    receipts = P.read_malformed_receipts(proj)
    assert len(receipts) == 1                                                                             # receipt retained
    published = G.read_active_manifest(proj).generation_id                                                # the barrier WAS durable
    _emit(profile, "k6", title="Astro", summary="astrocytes")                                             # more rows land meanwhile
    builds = []
    monkeypatch.setattr(P, "build_generation", lambda *a, **k: (builds.append(1), real_build(*a, **k))[1])
    rep = P.drain_once(profile, reason="deepsleep")                                                       # Model A: CURRENT range, ONE build, any lawful barrier
    assert builds == [1] and rep.deltas_seen == 2 and rep.malformed_row_count == 1 and rep.status == "published_new"   # truthfully reported (k6 is new)
    assert rep.cursor_settled and P.read_cursor(proj).outbox_byte_offset == rep.boundary == ob.stat().st_size
    assert rep.generation_id != published
    assert P.read_malformed_receipts(proj) == receipts                                                    # visible after settlement
    # a plain unchanged re-drain is no_work, never mislabeled as a retry
    assert P.drain_once(profile, reason="deepsleep").status == "no_work"


def test_build_failure_after_receipts_keeps_the_cursor_and_the_next_drain_retries_one_build(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    with ob.open("ab") as fh:
        fh.write(b"\n")                                                    # empty complete row ⇒ malformed (D-13-66)
    import memory.projection.build as B
    monkeypatch.setattr(P, "build_generation", lambda *a, **k: B.BuildRejected(reason="snapshot_moved", detail={}, attempts=3))
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "snapshot_moved" and rep.malformed_row_count == 1 and rep.malformed_forced_build and not rep.cursor_settled
    assert P.read_cursor(proj).outbox_byte_offset == P.read_cursor(proj).observed_size == 0 or P.read_cursor(proj).outbox_byte_offset < ob.stat().st_size
    assert len(P.read_malformed_receipts(proj)) == 1
    monkeypatch.setattr(P, "build_generation", B.build_generation)
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.full_builds == 1 and rep2.cursor_settled and rep2.status == "reused_existing" and rep2.malformed_forced_build
    assert P.read_cursor(proj).outbox_byte_offset == ob.stat().st_size


def test_settlement_after_a_publication_failure_leaves_the_cursor_unchanged(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    before = P.read_cursor(proj)
    clk = TK.last_coverage_clock()
    monkeypatch.setattr(P, "publish_generation", lambda *a, **k: (_ for _ in ()).throw(G.CoverageClockRegression(head=clk, candidate=clk, event="forced")))
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "coverage_clock_regression" and rep.full_builds == 1 and not rep.cursor_settled
    assert P.read_cursor(proj) == before
    monkeypatch.setattr(P, "publish_generation", lambda *a, **k: (_ for _ in ()).throw(G.PublicationBlockedOnLeases("forced")))
    assert P.drain_once(profile, reason="deepsleep").status == "blocked_on_leases" and P.read_cursor(proj) == before
    monkeypatch.setattr(P, "publish_generation", lambda *a, **k: (_ for _ in ()).throw(G.PublicationLineageTampered("forced")))
    assert P.drain_once(profile, reason="deepsleep").status == "lineage_operator_required" and P.read_cursor(proj) == before
