"""Program 0 P0.1 — outbox bootstrap / migration state machine (D-13-52/56/62/72): virgin,
legacy Cut-4 (incl. 0 bytes and a partial final row), established, partial-bootstrap resume,
metadata-corrupt ⇒ rotate-first; one build per invocation; single exclusion; report on every path."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import build as B
from memory.projection import generation as G
from memory.projection import projector as P
from tests.memory.projection._fixtures import build_and_publish, redirect_timekeeper, seed_records_only, write_record


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


def _files(profile):
    proj = G.projection_root(profile)
    return {name: (proj / name).exists() for name in (PD.OUTBOX_EPOCH_FILE, PD.OUTBOX_CURSOR_FILE, PD.OUTBOX_BOOTSTRAP_INTENT_FILE)}


def test_virgin_profile_bootstraps_epoch_1_zero_cursor_and_publishes_the_initial_generation_once(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    ob = PD.outbox_path(profile / "memory")
    assert not ob.exists()
    builds = []
    real = B.build_generation
    monkeypatch.setattr(P, "build_generation", lambda *a, **k: (builds.append(1), real(*a, **k))[1])
    rep = P.drain_once(profile, reason="boot")
    assert ob.exists() and ob.stat().st_size == 0                                     # ONE empty writable outbox, fsynced
    ep, cur = P.read_epoch(G.projection_root(profile)), P.read_cursor(G.projection_root(profile))
    assert ep.epoch == 1 and ep.origin == "virgin" and cur.outbox_file_epoch == 1 and cur.outbox_byte_offset == 0
    assert cur.observed_prefix_sha256 == P.ZERO_SHA256 and cur.consumed_prefix_sha256 == P.ZERO_SHA256
    assert cur.outbox_file_identity == ep.file_identity
    assert rep.bootstrap_state == "virgin" and rep.status == "published_new" and rep.full_builds == 1 and builds == [1]   # initial_generation_required ⇒ ONE build
    assert rep.cursor_settled and P.read_cursor(G.projection_root(profile)).generation_id == rep.generation_id
    assert not (G.projection_root(profile) / PD.OUTBOX_BOOTSTRAP_INTENT_FILE).exists()
    # virgin WITH an active manifest and clean ⇒ bootstrapped, zero builds
    profile2 = tmp_path / "p2"; profile2.mkdir(); seed_records_only(profile2)
    build_and_publish(profile2)
    rep2 = P.drain_once(profile2, reason="boot")
    assert rep2.status == "bootstrapped" and rep2.full_builds == 0 and rep2.bootstrap_state == "virgin"
    assert P.drain_once(profile2, reason="deepsleep").status == "no_work"


def test_legacy_cut4_outbox_with_rows_and_no_sidecars_is_adopted_and_drained_in_the_same_invocation(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    d1 = _emit(profile, "k5", title="Glia", summary="glial support cells")           # the live seat's shape: rows, no sidecars
    d2 = _emit(profile, "k6", title="Astro", summary="astrocytes")
    ob = PD.outbox_path(profile / "memory")
    before = ob.read_bytes()
    assert before.endswith(b"\n") and _files(profile) == {PD.OUTBOX_EPOCH_FILE: False, PD.OUTBOX_CURSOR_FILE: False, PD.OUTBOX_BOOTSTRAP_INTENT_FILE: False}
    builds = []
    real = B.build_generation
    monkeypatch.setattr(P, "build_generation", lambda *a, **k: (builds.append(1), real(*a, **k))[1])
    rep = P.drain_once(profile, reason="boot")
    proj = G.projection_root(profile)
    ep, cur = P.read_epoch(proj), P.read_cursor(proj)
    assert ep.epoch == 1 and ep.origin == "legacy_cut4"
    assert rep.bootstrap_state == "legacy_cut4" and rep.full_builds == 1 and builds == [1]     # SAME invocation: parsed, built ONCE, published, settled
    assert rep.status == "published_new" and rep.deltas_seen == 2 and rep.latest_per_memory == 2 and rep.cursor_settled
    assert cur.outbox_byte_offset == len(before) and cur.observed_size == len(before) and cur.last_delta_id == d2.delta_id
    assert cur.consumed_prefix_sha256 == P.hashlib.sha256(before).hexdigest()
    assert ob.read_bytes() == before                                                   # NOTHING quarantined, no delimiter, no corruption receipt
    assert not list(ob.parent.glob("*.quarantine-*")) and not (proj / PD.OUTBOX_RECOVERY_FILE).exists()
    assert P.read_malformed_receipts(proj) == ()
    seats = [s["memory_id"] for s in json.loads((proj / f"gen-{rep.generation_id}" / "sram_index.json").read_text())]
    assert {"k5", "k6"} <= set(seats)
    assert P.drain_once(profile, reason="deepsleep").status == "no_work"
    assert d1.memory_id == "k5"


def test_legacy_partial_final_row_gets_one_receipt_one_delimiter_one_rebuild_and_settles(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    build_and_publish(profile)                                                          # an existing active generation
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    ob = PD.outbox_path(profile / "memory")
    with ob.open("ab") as fh:
        fh.write(b'{"partial": "row without LF')                                        # torn producer tail
    raw = ob.read_bytes()
    boundary = raw.rfind(b"\n") + 1
    rep = P.drain_once(profile, reason="boot")
    proj = G.projection_root(profile)
    now = ob.read_bytes()
    assert now == raw + b"\n"                                                           # ONE LF delimiter appended + fsynced
    receipts = P.read_malformed_receipts(proj)
    assert len(receipts) == 1 and receipts[0].byte_start == boundary and receipts[0].byte_end == len(raw)   # ONE receipt (LF excluded)
    assert rep.bootstrap_state == "legacy_cut4" and rep.malformed_row_count == 1 and rep.full_builds == 1
    assert rep.status in ("published_new", "reused_existing") and rep.cursor_settled and rep.deltas_seen == 1
    cur = P.read_cursor(proj)
    assert cur.observed_size == len(now) and cur.outbox_byte_offset == len(now)        # delimiter included; malformed row consumed AFTER the barrier
    # a later producer append yields TWO LF-framed records: the malformed legacy row + one independently parseable new row
    _emit(profile, "k6", title="Astro", summary="astrocytes")
    tail = ob.read_bytes()[len(now):]
    assert tail.endswith(b"\n") and tail.count(b"\n") == 1
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.deltas_seen == 1 and rep2.malformed_row_count == 0 and rep2.status in ("published_new", "reused_existing")
    assert P.read_malformed_receipts(proj) == receipts                                  # receipt visible after settlement + restart


def test_empty_legacy_outbox_vs_virgin_differ_only_in_origin(tmp_path):
    p1 = _profile(tmp_path); (tmp_path / "p2").mkdir(); p2 = tmp_path / "p2"; seed_records_only(p2)
    PD.outbox_path(p2 / "memory").parent.mkdir(parents=True, exist_ok=True); PD.outbox_path(p2 / "memory").write_bytes(b"")
    r1, r2 = P.drain_once(p1, reason="x"), P.drain_once(p2, reason="x")
    e1, e2 = P.read_epoch(G.projection_root(p1)), P.read_epoch(G.projection_root(p2))
    assert (e1.origin, e2.origin) == ("virgin", "legacy_cut4") and e1.epoch == e2.epoch == 1
    assert r1.bootstrap_state == "virgin" and r2.bootstrap_state == "legacy_cut4" and r1.status == r2.status == "published_new"


def test_bootstrap_holds_one_exclusion_and_a_real_writer_fails_fast_while_held(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    real_excl = PD.outbox_exclusion                              # Lock methods are read-only; spy through outbox_exclusion
    entered = []
    inside = {"writer_result": None}
    delta = PD.ProjectionDelta.build(operation="upsert", memory_id="k1", canonical_path="memory/200_knowledge/k1.json", canonical_hash="0" * 64,
                                     coord=(2, 0, 1), commit_seq=0, projection_class="semantic")

    from contextlib import contextmanager

    @contextmanager
    def spy_excl(root, *, blocking):
        entered.append(blocking)
        with real_excl(root, blocking=blocking) as oa:
            if blocking and inside["writer_result"] is None:
                # a REAL producer append (nonblocking) attempted from another thread while the exclusion is held ⇒ False immediately
                res = {}
                t = threading.Thread(target=lambda: res.__setitem__("r", PD.append_projection_delta(delta, root=profile / "memory")))
                t.start(); t.join(timeout=5)
                inside["writer_result"] = res.get("r")
            yield oa

    monkeypatch.setattr(P, "outbox_exclusion", spy_excl)
    with G.projection_publication_authority(profile) as auth:
        report = P.bootstrap_outbox(profile, authority=auth)
    assert report.state == "virgin" and report.action == "created" and report.epoch == 1
    assert entered == [True]                                                           # exactly ONE blocking exclusion for the whole transition
    assert inside["writer_result"] is False                                            # the writer failed fast; it never waited on the projector
    assert P.read_cursor(G.projection_root(profile)).outbox_byte_offset == 0


@pytest.mark.parametrize("point", ["p0:bootstrap_after_intent", "p0:bootstrap_after_outbox", "p0:bootstrap_after_epoch", "p0:bootstrap_after_cursor"])
def test_virgin_bootstrap_crash_intervals_resume_the_one_intent_path(tmp_path, point):
    profile = _profile(tmp_path)
    build_and_publish(profile)
    G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == point else None
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="boot")
    G._crash_hook = None
    proj = G.projection_root(profile)
    intent_before = (proj / PD.OUTBOX_BOOTSTRAP_INTENT_FILE).exists()
    assert intent_before                                                                # the intent survives every interval
    rep = P.drain_once(profile, reason="boot")
    assert rep.bootstrap_state == "partial_bootstrap" and rep.status == "bootstrapped" and rep.full_builds == 0
    ep = P.read_epoch(proj); cur = P.read_cursor(proj)
    assert ep.epoch == 1 and cur.outbox_file_epoch == 1 and ep.file_identity == cur.outbox_file_identity
    assert not (proj / PD.OUTBOX_BOOTSTRAP_INTENT_FILE).exists() and not list(PD.outbox_path(profile / "memory").parent.glob("*.quarantine-*"))
    assert P.consumption_state(profile)["bootstrap_state"] == "established"


@pytest.mark.parametrize("point", ["p0:bootstrap_after_intent", "p0:bootstrap_after_tail_receipt", "p0:bootstrap_after_delimiter", "p0:bootstrap_after_epoch", "p0:bootstrap_after_cursor"])
def test_legacy_bootstrap_crash_intervals_keep_the_backlog_and_one_epoch(tmp_path, point):
    profile = _profile(tmp_path)
    build_and_publish(profile)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    ob = PD.outbox_path(profile / "memory")
    with ob.open("ab") as fh:
        fh.write(b'{"torn":')
    G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == point else None
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="boot")
    G._crash_hook = None
    rep = P.drain_once(profile, reason="boot")
    proj = G.projection_root(profile)
    assert rep.bootstrap_state == "partial_bootstrap" and rep.full_builds == 1 and rep.cursor_settled     # backlog intact, ONE build
    assert rep.deltas_seen == 1 and rep.malformed_row_count == 1
    assert ob.read_bytes().count(b"\n") == 2                                            # exactly ONE delimiter ever appended
    assert len(P.read_malformed_receipts(proj)) == 1
    assert P.read_epoch(proj).epoch == 1 and P.read_epoch(proj).origin == "legacy_cut4"
    assert not list(ob.parent.glob("*.quarantine-*"))


@pytest.mark.parametrize("subset", ["epoch_only", "cursor_only"])
def test_partial_metadata_without_intent_is_corrupt_and_rotates_first_never_inferred_bootstrap(tmp_path, subset):
    profile = _profile(tmp_path)
    build_and_publish(profile)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    P.drain_once(profile, reason="boot")                                                 # established
    proj = G.projection_root(profile)
    ob = PD.outbox_path(profile / "memory")
    _emit(profile, "k6", title="Astro", summary="astrocytes")                            # pending row in the (soon corrupt-metadata) epoch
    (proj / (PD.OUTBOX_CURSOR_FILE if subset == "epoch_only" else PD.OUTBOX_EPOCH_FILE)).unlink()
    assert P.consumption_state(profile)["bootstrap_state"] == "metadata_corrupt"
    old_bytes = ob.read_bytes()
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "outbox_metadata_corrupt_recovered" and rep.bootstrap_state == "metadata_corrupt" and rep.full_builds == 1
    quarantined = list(ob.parent.glob("*.quarantine-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == old_bytes           # rotate FIRST: old file quarantined intact
    ep, cur = P.read_epoch(proj), P.read_cursor(proj)
    assert ep.epoch == 2 and ep.origin == "rotation" and cur.outbox_file_epoch == 2 and cur.observed_size == 0 and cur.outbox_byte_offset == 0
    assert ob.exists() and ob.stat().st_size == 0                                       # new empty writable outbox
    rows = [json.loads(line) for line in (proj / PD.OUTBOX_RECOVERY_FILE).read_text().splitlines()]
    assert [r["stage"] for r in rows] == ["begin", "rotated", "completed"] and rows[0]["reason"] == "outbox_metadata_corrupt"
    # the recovery rebuild covered the canonical state (k6 present) — the outbox is evidence, not truth
    seats = [s["memory_id"] for s in json.loads((proj / f"gen-{rep.generation_id}" / "sram_index.json").read_text())]
    assert "k6" in seats
    # once epoch + cursor exist, deleting the outbox is corruption (D-13-43), never bootstrap
    ob.unlink()
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.status == "outbox_corrupt_recovered" and P.read_epoch(proj).epoch == 3


def test_bootstrap_report_on_every_return_path(tmp_path):
    profile = _profile(tmp_path)
    with G.projection_publication_authority(profile) as auth:
        r1 = P.bootstrap_outbox(profile, authority=auth)                                # virgin ⇒ created
        r2 = P.bootstrap_outbox(profile, authority=auth)                                # established ⇒ none
    assert (r1.state, r1.action, r1.initial_generation_required) == ("virgin", "created", True)
    assert (r2.state, r2.action) == ("established", "none") and r2.cursor is not None and r2.epoch == 1
    for r in (r1, r2):
        assert isinstance(r, P.OutboxBootstrapReport)


def test_resumed_legacy_intent_refuses_a_replaced_outbox_identity_and_rotates_first(tmp_path):
    """Review a045ec4a §1 (D-13-52): a resumed legacy Cut-4 intent must find the SAME PHYSICAL FILE it
    bound. A replacement inode — even with identical bytes — is never adopted into epoch 1; it is
    recovery-required ⇒ rotate-first (quarantine → new epoch → ONE recovery rebuild → settle)."""
    import os
    profile = _profile(tmp_path)
    build_and_publish(profile)                                                       # an active generation (so the rebuild is meaningful)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    ob = PD.outbox_path(profile / "memory")
    proj = G.projection_root(profile)
    G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == "p0:bootstrap_after_intent" else None
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="boot")
    G._crash_hook = None
    intent = P.read_bootstrap_intent(proj)
    assert intent is not None and intent.kind == "legacy_cut4" and intent.file_identity
    data = ob.read_bytes()
    tmp = ob.with_suffix(".replacement"); tmp.write_bytes(data); os.replace(tmp, ob)   # identical BYTES, different inode
    fd = os.open(ob, os.O_RDONLY)
    try:
        assert PD.outbox_file_identity_of(fd) != intent.file_identity
    finally:
        os.close(fd)
    rep = P.drain_once(profile, reason="boot")
    assert rep.status == "outbox_metadata_corrupt_recovered" and rep.recovery_rotated and rep.full_builds == 1 and rep.cursor_settled
    ep, cur = P.read_epoch(proj), P.read_cursor(proj)
    assert ep.origin == "rotation" and ep.epoch >= 1 and cur.outbox_file_epoch == ep.epoch and cur.outbox_byte_offset == 0
    quarantined = list(ob.parent.glob("*.quarantine-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == data              # the replacement is the quarantined evidence surface
    assert ob.exists() and ob.stat().st_size == 0 and P.read_bootstrap_intent(proj) is None
    rows = [json.loads(line) for line in (proj / PD.OUTBOX_RECOVERY_FILE).read_text().splitlines()]
    assert [r["stage"] for r in rows] == ["begin", "rotated", "completed"]
    seats = [s["memory_id"] for s in json.loads((proj / f"gen-{rep.generation_id}" / "sram_index.json").read_text())]
    assert "k5" in seats                                                              # the recovery rebuild covered canonical truth


def test_resumed_legacy_intent_refuses_a_missing_or_appended_replacement_outbox(tmp_path):
    """Same law, the other two shapes: the outbox is GONE after the intent, or replaced with a
    different inode AND different bytes."""
    import os
    for shape in ("missing", "different_bytes"):
        profile = tmp_path / f"p-{shape}"; profile.mkdir(); seed_records_only(profile)
        build_and_publish(profile)
        _emit(profile, "k5", title="Glia", summary="glial support cells")
        ob = PD.outbox_path(profile / "memory"); proj = G.projection_root(profile)
        G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == "p0:bootstrap_after_intent" else None
        with pytest.raises(G.SimulatedCrash):
            P.drain_once(profile, reason="boot")
        G._crash_hook = None
        if shape == "missing":
            ob.unlink()
        else:
            tmp = ob.with_suffix(".replacement"); tmp.write_bytes(ob.read_bytes() + b'{"extra":1}\n'); os.replace(tmp, ob)
        rep = P.drain_once(profile, reason="boot")
        assert rep.status == "outbox_metadata_corrupt_recovered" and rep.recovery_rotated and rep.full_builds == 1, (shape, rep.status)
        assert P.read_epoch(proj).origin == "rotation" and P.read_bootstrap_intent(proj) is None
        assert PD.outbox_path(profile / "memory").exists()
        TK.reset_for_tests()
