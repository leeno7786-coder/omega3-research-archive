"""Program 0 P0.1 — coherent OutboxSnapshot (D-13-48): ONE read of exactly captured_size bytes under
the exclusion (a writer appending during capture cannot change the captured bytes/size; an append after
release is a lawful tail next time), short read / identity change / size inconsistency ⇒ corruption,
settlement persists the snapshot's values (never re-hashes a moving file), only complete lines parsed."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import generation as G
from memory.projection import projector as P
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only, write_record


@pytest.fixture(autouse=True)
def _env(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


def _row(mid, n=0):
    return PD.ProjectionDelta.build(operation="upsert", memory_id=mid, canonical_path=f"memory/200_knowledge/{mid}.json", canonical_hash=f"{n:064x}",
                                    coord=(2, 0, 1), commit_seq=n, projection_class="semantic")


def test_capture_reads_exactly_captured_size_and_a_concurrent_appender_lands_after(tmp_path):
    profile = tmp_path / "p"; profile.mkdir(); seed_records_only(profile)
    root = profile / "memory"
    for i in range(3):
        assert PD.append_projection_delta(_row("k1", i), root=root)
    ob = PD.outbox_path(root)
    size0 = ob.stat().st_size
    started = threading.Event(); results = {}

    def writer():
        started.set()
        results["ok"] = PD.append_projection_delta(_row("k2", 9), root=root)      # attempted WHILE capture holds ⇒ refused immediately

    with PD.outbox_exclusion(root, blocking=True) as oa:
        t = threading.Thread(target=writer); t.start(); started.wait(2); t.join(5)
        snap = PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=None, consumed_prefix_expected=None, outbox_authority=oa)
    assert results["ok"] is False and snap.captured_size == size0 == ob.stat().st_size   # captured bytes/size unaffected; nothing appended during capture
    assert snap.observed_prefix_sha256 == hashlib.sha256(ob.read_bytes()).hexdigest()
    assert PD.append_projection_delta(_row("k2", 9), root=root)                       # after release ⇒ lawful tail beyond captured_size
    assert ob.stat().st_size > size0
    deltas, malformed = P.parse_snapshot(snap)
    assert len(deltas) == 3 and malformed == [] and snap.complete_boundary == size0


def test_capture_verifies_prior_observed_bytes_prefix_and_offset(tmp_path):
    profile = tmp_path / "p"; profile.mkdir(); seed_records_only(profile)
    root = profile / "memory"
    for i in range(2):
        PD.append_projection_delta(_row("k1", i), root=root)
    ob = PD.outbox_path(root); data = ob.read_bytes()
    first_end = data.find(b"\n") + 1
    with PD.outbox_exclusion(root, blocking=True) as oa:
        # lawful: offset at a line boundary, prefix hash matches, observed bytes intact
        snap = PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=first_end, prior_observed=(len(data), hashlib.sha256(data).hexdigest()),
                                                 consumed_prefix_expected=hashlib.sha256(data[:first_end]).hexdigest(), outbox_authority=oa)
        assert snap.bytes_to_parse == data[first_end:] and snap.saved_offset == first_end
        # mid-line offset ⇒ corrupt
        with pytest.raises(PD.OutboxCorrupt):
            PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=first_end - 1, prior_observed=None, consumed_prefix_expected=None, outbox_authority=oa)
        # consumed-prefix mismatch ⇒ corrupt
        with pytest.raises(PD.OutboxCorrupt):
            PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=first_end, prior_observed=None, consumed_prefix_expected="0" * 64, outbox_authority=oa)
        # previously observed bytes mutated ⇒ corrupt
        with pytest.raises(PD.OutboxCorrupt):
            PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=(len(data), "f" * 64), consumed_prefix_expected=None, outbox_authority=oa)
        # truncated below observed_size ⇒ corrupt
        with pytest.raises(PD.OutboxCorrupt):
            PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=(len(data) + 10, hashlib.sha256(data).hexdigest()), consumed_prefix_expected=None, outbox_authority=oa)
        # offset beyond the file ⇒ corrupt; missing file ⇒ corrupt
        with pytest.raises(PD.OutboxCorrupt):
            PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=len(data) + 1, prior_observed=None, consumed_prefix_expected=None, outbox_authority=oa)
        ob.unlink()
        with pytest.raises(PD.OutboxCorrupt):
            PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=None, consumed_prefix_expected=None, outbox_authority=oa)


def test_capture_requires_the_live_exclusion_token(tmp_path):
    profile = tmp_path / "p"; profile.mkdir(); seed_records_only(profile)
    root = profile / "memory"
    PD.outbox_path(root).parent.mkdir(parents=True, exist_ok=True); PD.outbox_path(root).write_bytes(b"")
    with pytest.raises(PD.OutboxBusy):
        PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=None, consumed_prefix_expected=None, outbox_authority=None)
    with PD.outbox_exclusion(root, blocking=True) as oa:
        pass
    with pytest.raises(PD.OutboxBusy):                                            # a released token is dead
        PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=None, consumed_prefix_expected=None, outbox_authority=oa)
    other = tmp_path / "other"; other.mkdir(); (other / "memory" / "indexes").mkdir(parents=True)
    with PD.outbox_exclusion(other / "memory", blocking=True) as oa2:
        with pytest.raises(PD.OutboxBusy):                                        # a token for another root is refused
            PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=None, consumed_prefix_expected=None, outbox_authority=oa2)


def test_short_read_or_size_inconsistency_during_capture_is_corruption(tmp_path, monkeypatch):
    profile = tmp_path / "p"; profile.mkdir(); seed_records_only(profile)
    root = profile / "memory"
    PD.append_projection_delta(_row("k1", 1), root=root)
    real_read = os.read
    calls = {"n": 0}

    def short_read(fd, n):
        calls["n"] += 1
        return b"" if calls["n"] == 1 else real_read(fd, n)

    monkeypatch.setattr(PD.os, "read", short_read)
    with PD.outbox_exclusion(root, blocking=True) as oa:
        with pytest.raises(PD.OutboxCorrupt, match="short read"):
            PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=None, consumed_prefix_expected=None, outbox_authority=oa)


def test_settlement_persists_snapshot_values_never_rehashing_a_moving_file(tmp_path, monkeypatch):
    profile = tmp_path / "p"; profile.mkdir(); seed_records_only(profile)
    root = profile / "memory"
    P.drain_once(profile, reason="boot")
    write_record(profile, "k5", title="Glia", summary="glial support cells")
    PD.append_projection_delta(_row("k5", 1), root=root)
    ob = PD.outbox_path(root)
    captured = {}
    real_capture = P.capture_outbox_snapshot_locked

    def spy(**kw):
        snap = real_capture(**kw)
        captured["snap"] = snap
        return snap

    monkeypatch.setattr(P, "capture_outbox_snapshot_locked", spy)
    real_build = P.build_generation

    def build_and_append(*a, **k):
        out = real_build(*a, **k)
        with ob.open("ab") as fh:                                                   # a writer lands DURING the long build
            fh.write(PD.canonical_json(_row("k9", 2).to_dict()).encode() + b"\n")
        return out

    monkeypatch.setattr(P, "build_generation", build_and_append)
    rep = P.drain_once(profile, reason="deepsleep")
    snap = captured["snap"]
    cur = P.read_cursor(G.projection_root(profile))
    assert cur.observed_size == snap.captured_size < ob.stat().st_size            # the snapshot's values, not the moving file's
    assert cur.observed_prefix_sha256 == snap.observed_prefix_sha256 and cur.outbox_byte_offset == snap.complete_boundary
    assert rep.deltas_seen == 1
    monkeypatch.setattr(P, "build_generation", real_build)
    rep2 = P.drain_once(profile, reason="deepsleep")                                # the mid-build append is a lawful tail next time
    assert rep2.deltas_seen == 1 and rep2.saved_offset == snap.complete_boundary
    time.sleep(0)
    assert json.loads((G.projection_root(profile) / PD.OUTBOX_CURSOR_FILE).read_text())["outbox_byte_offset"] == ob.stat().st_size
