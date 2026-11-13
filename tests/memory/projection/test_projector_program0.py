"""Program 0 P0.1 — drain_once (D-13-7/31/57/62/66/67/72/76): established empty ⇒ no_work; virgin
⇒ bootstrapped / one publication; malformed-only ⇒ one forced rebuild then advancement; single acquirer
(dispatcher token, no nested acquisition, real second process refused); N deltas for M memories ⇒
latest-per-id; cursor advances ONLY after the barrier; re-drain no_work; active G + prefix rebuilding
G ⇒ reused_existing + cursor advance; corrupt G ⇒ replaced_corrupt; sequenced=False posture; stale /
tombstone classification against the closed snapshot; obligations ⇒ blocked; dirty compare-and-clear;
zero embedding calls; never inside serialized_commit; snapshot_moved leaves the cursor; every stage
asserts the token."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import build as B
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
    assert P.drain_once(profile, reason="boot").status == "published_new"
    return profile, G.projection_root(profile), PD.outbox_path(profile / "memory")


def test_established_empty_no_dirty_is_no_work_and_virgin_paths(tmp_path):
    profile, proj, ob = _established(tmp_path)
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "no_work" and rep.full_builds == 0 and rep.needs_build is False and rep.bootstrap_state == "established"
    assert rep.generation_id == G.read_active_manifest(proj).generation_id and rep.lag == 0 and rep.head_clock == G.read_active_manifest(proj).coverage_clock
    p2 = tmp_path / "p2"; p2.mkdir(); seed_records_only(p2); build_and_publish(p2)
    assert P.drain_once(p2, reason="boot").status == "bootstrapped"                                    # virgin WITH manifest
    p3 = tmp_path / "p3"; p3.mkdir(); seed_records_only(p3)
    r3 = P.drain_once(p3, reason="boot")
    assert r3.status == "published_new" and r3.bootstrap_state == "virgin" and r3.full_builds == 1        # virgin WITHOUT manifest ⇒ one publication outcome


def test_malformed_only_forces_exactly_one_rebuild_then_cursor_advances(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    for raw in (b"not json\n", b"\n", b"   \n", b"\t\n"):                                                # incl. empty / whitespace-only rows (D-13-66)
        with ob.open("ab") as fh:
            fh.write(raw)
    builds = []
    real = B.build_generation
    monkeypatch.setattr(P, "build_generation", lambda *a, **k: (builds.append(1), real(*a, **k))[1])
    before = P.read_cursor(proj)
    rep = P.drain_once(profile, reason="deepsleep")
    assert builds == [1] and rep.malformed_row_count == 4 and rep.malformed_forced_build and rep.deltas_seen == 0
    assert rep.status == "reused_existing" and rep.cursor_settled                                       # rebuild of unchanged truth ⇒ reused; cursor advanced ACROSS the malformed rows
    after = P.read_cursor(proj)
    assert after.outbox_byte_offset == ob.stat().st_size > before.outbox_byte_offset
    assert len(P.read_malformed_receipts(proj)) == 4
    assert P.drain_once(profile, reason="deepsleep").status == "no_work" and builds == [1]              # never reprocessed forever
    # health keeps the receipts visible after settlement
    assert len(P.consumption_state(profile)["malformed_receipts"]) == 4


def test_n_deltas_for_m_memories_latest_per_id_cursor_only_after_barrier_and_redrain_no_work(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    ids = []
    write_record(profile, "k5", title="Glia", summary="glial support cells")
    for i in range(3):                                                                                       # 3 rows for k5 (append order decides)
        d = PD.ProjectionDelta.build(operation="upsert", memory_id="k5", canonical_path="memory/200_knowledge/k5.json", canonical_hash=f"{i:064x}",
                                     coord=(2, 0, 5), commit_seq=i, projection_class="semantic", sequenced=True)
        assert PD.append_projection_delta(d, root=profile / "memory"); ids.append(d.delta_id)
    ids.append(_emit(profile, "k6", title="Astro", summary="astrocytes").delta_id)
    order = []
    real_pub, real_write = P.publish_generation, P._write_cursor
    monkeypatch.setattr(P, "publish_generation", lambda *a, **k: (order.append("publish"), real_pub(*a, **k))[1])
    monkeypatch.setattr(P, "_write_cursor", lambda *a, **k: (order.append("cursor"), real_write(*a, **k))[1])
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.deltas_seen == 4 and rep.latest_per_memory == 2 and rep.status == "published_new" and rep.full_builds == 1
    assert order == ["publish", "cursor"]                                                                  # cursor ONLY after the barrier
    cur = P.read_cursor(proj)
    assert cur.last_delta_id == ids[-1] and cur.generation_id == rep.generation_id and cur.outbox_byte_offset == ob.stat().st_size
    assert cur.observed_size == ob.stat().st_size and cur.outbox_file_epoch == 1
    seats = [s["memory_id"] for s in json.loads((proj / f"gen-{rep.generation_id}" / "sram_index.json").read_text())]
    assert {"k5", "k6"} <= set(seats)
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.status == "no_work" and rep2.full_builds == 0


def test_active_g_with_pending_prefix_rebuilding_g_is_reused_existing_and_corrupt_g_is_replaced(tmp_path):
    profile, proj, ob = _established(tmp_path)
    gid = G.read_active_manifest(proj).generation_id
    # a delta for a record whose canonical state is unchanged (the clean rebuild is G again)
    k1 = json.loads((profile / "memory" / "200_knowledge" / "k1.json").read_text())
    out = PD.emit_delta_for_canonical(k1, profile / "memory" / "200_knowledge" / "k1.json", root=profile / "memory")
    assert out["durable"]
    gen_dir = proj / f"gen-{gid}"
    before = {p.name: p.read_bytes() for p in gen_dir.iterdir() if p.is_file()}
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "reused_existing" and rep.generation_id == gid and rep.cursor_settled and rep.deltas_seen == 1
    assert {p.name: p.read_bytes() for p in gen_dir.iterdir() if p.is_file()} == before                # no byte under gen-G changes
    assert P.read_cursor(proj).outbox_byte_offset == ob.stat().st_size
    # corrupt G + a pending prefix whose rebuild is G ⇒ replaced_corrupt, cursor advances
    (gen_dir / "sram.bin").write_bytes(b"\x00" * 64)
    out = PD.emit_delta_for_canonical(k1, profile / "memory" / "200_knowledge" / "k1.json", root=profile / "memory")
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.status == "replaced_corrupt" and rep2.generation_id == gid and rep2.cursor_settled
    G.validate_published_generation(proj, G.read_active_manifest(proj))
    assert P.read_cursor(proj).outbox_byte_offset == ob.stat().st_size


def test_unsequenced_stale_and_tombstone_classification_against_the_closed_snapshot(tmp_path):
    profile, proj, ob = _established(tmp_path)
    write_record(profile, "k5", title="Glia", summary="glial support cells")
    path = profile / "memory" / "200_knowledge" / "k5.json"
    d = PD.ProjectionDelta.build(operation="upsert", memory_id="k5", canonical_path="memory/200_knowledge/k5.json", canonical_hash="0" * 64,   # STALE hash
                                 coord=(2, 0, 5), commit_seq=0, projection_class="semantic", sequenced=False)               # sequenced=False
    assert PD.append_projection_delta(d, root=profile / "memory")
    d2 = PD.ProjectionDelta.build(operation="tombstone", memory_id="ghost", canonical_path="memory/200_knowledge/ghost.json", canonical_hash="1" * 64,
                                  coord=(2, 0, 9), commit_seq=0, projection_class="semantic", sequenced=True)              # missing canonical ⇒ tombstone
    assert PD.append_projection_delta(d2, root=profile / "memory")
    wm_before = G.read_active_manifest(proj).coverage_clock
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "published_new" and rep.unsequenced_consumed == 1 and rep.sequenced_consumed == 1
    assert rep.stale_delta_count == 1 and rep.tombstone_count == 1                                       # classified vs the SAME closed snapshot
    assert rep.watermark == wm_before.commit_seq and G.read_active_manifest(proj).coverage_clock == wm_before   # unsequenced never moved the watermark
    assert G.read_active_manifest(proj).unsequenced_posture == "covered_by_full_rebuild"
    seats = [s["memory_id"] for s in json.loads((proj / f"gen-{rep.generation_id}" / "sram_index.json").read_text())]
    assert "k5" in seats and "ghost" not in seats                                                          # canonical newer than delta ⇒ file wins
    assert path.exists()


def test_obligations_block_dirty_compare_and_clear_and_snapshot_moved(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    import memory.encoding_journal as EJ
    monkeypatch.setattr(EJ, "journal_status", lambda: {"clean": False, "unclosed_count": 1, "malformed_count": 0})
    before = P.read_cursor(proj)
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "blocked_on_obligations" and not rep.cursor_settled and P.read_cursor(proj) == before
    monkeypatch.setattr(EJ, "journal_status", lambda: {"clean": True})
    # dirty marker: cleared ONLY if its bytes equal the pre-capture value AND after the barrier
    PD.mark_projection_dirty(reason="first", root=profile / "memory")
    real_build = P.build_generation

    def rewrite_mid_build(*a, **k):
        out = real_build(*a, **k)
        PD.mark_projection_dirty(reason="rewritten mid-build", root=profile / "memory")
        return out

    monkeypatch.setattr(P, "build_generation", rewrite_mid_build)
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "published_new" and rep.dirty_before_sha256 and rep.dirty_cleared is False          # rewritten mid-build ⇒ NOT cleared
    assert PD.projection_dirty_state(root=profile / "memory")["dirty"] is True
    monkeypatch.setattr(P, "build_generation", real_build)
    rep = P.drain_once(profile, reason="deepsleep")                                                            # next drain rebuilds and clears
    assert rep.needs_build and rep.dirty_cleared is True and PD.projection_dirty_state(root=profile / "memory")["dirty"] is False
    # snapshot_moved after bounded retries ⇒ nothing published, cursor unchanged
    _emit(profile, "k6", title="Astro", summary="astrocytes")
    before = P.read_cursor(proj)
    active = G.read_active_manifest(proj).generation_id
    from memory.taxonomy import compiler as C
    real_compile = C.compile_memory_graph
    n = {"i": 0}

    def compile_then_write(**kw):
        n["i"] += 1
        write_record(profile, f"kz{n['i']}", title="Moving", summary=f"moving target {n['i']}")
        return real_compile(**kw)

    monkeypatch.setattr(C, "compile_memory_graph", compile_then_write)
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "snapshot_moved" and rep.full_builds == 1 and not rep.cursor_settled
    assert P.read_cursor(proj) == before and G.read_active_manifest(proj).generation_id == active


def test_projection_build_never_runs_inside_serialized_commit(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    from core import commit_region as CR
    inside = []
    real_build = P.build_generation
    monkeypatch.setattr(P, "build_generation", lambda *a, **k: (inside.append(PD.in_serialized_commit_region()), real_build(*a, **k))[1])
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "published_new" and inside == [False]
    assert CR is not None


CHILD_DRAIN = textwrap.dedent(
    """
    import sys, json; sys.path.insert(0, %r)
    from pathlib import Path
    from memory.projection import projector as P
    from tests.memory.projection._fixtures import redirect_timekeeper
    redirect_timekeeper(Path(sys.argv[2]))
    rep = P.drain_once(Path(sys.argv[1]), reason="second-process")
    print(json.dumps({"status": rep.status}), flush=True)
    """
)


def test_single_acquirer_and_a_real_second_process_is_refused(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    acquisitions = []
    real_auth = G.projection_publication_authority
    monkeypatch.setattr(P, "projection_publication_authority", lambda root: (acquisitions.append(1), real_auth(root))[1])
    tokens = []
    real_build, real_pub, real_clear = P.build_generation, P.publish_generation, P.clear_projection_dirty
    monkeypatch.setattr(P, "build_generation", lambda root, **kw: (tokens.append(id(kw["authority"])), real_build(root, **kw))[1])
    monkeypatch.setattr(P, "publish_generation", lambda root, cand, **kw: (tokens.append(id(kw["authority"])), real_pub(root, cand, **kw))[1])
    PD.mark_projection_dirty(reason="x", root=profile / "memory")
    monkeypatch.setattr(P, "clear_projection_dirty", lambda **kw: (tokens.append(id(kw["authority"])), real_clear(**kw))[1])
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "published_new" and acquisitions == [1] and len(set(tokens)) == 1 and len(tokens) == 3   # ONE token through build/publish/dirty settlement
    # every stage asserts the token: the _locked entry point refuses without a live authority
    with pytest.raises(G.PublicationAuthorityRequired):
        P.drain_once_locked(profile, reason="x", authority=None)
    with real_auth(profile) as auth:
        pass
    with pytest.raises(G.PublicationAuthorityRequired):
        P.drain_once_locked(profile, reason="x", authority=auth)                                          # a released token is dead
    # a REAL second process attempting a drain while the first holds authority ⇒ refused_authority
    _emit(profile, "k6", title="Astro", summary="astrocytes")
    with real_auth(profile):
        proc = subprocess.run([sys.executable, "-c", CHILD_DRAIN % str(REPO), str(profile), str(tmp_path)], capture_output=True, text=True, timeout=300)
        assert json.loads(proc.stdout.strip().splitlines()[-1])["status"] == "refused_authority", proc.stderr[-500:]
    # the public wrapper acquires exactly once and delegates
    acquisitions.clear()
    monkeypatch.setattr(P, "drain_once_locked", lambda root, *, reason, authority: (tokens.append(("locked", id(authority))), P._report(status="no_work"))[1])
    P.drain_once(profile, reason="x")
    assert acquisitions == [1] and tokens[-1][0] == "locked"


def test_crash_matrix_intervals_inside_the_drain(tmp_path):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    before = P.read_cursor(proj); active = G.read_active_manifest(proj).generation_id
    # death after capturing the snapshot before writing derived state ⇒ G active, cursor NOT advanced, re-drain re-reads the same range
    G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == "p0:after_capture" else None
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    G._crash_hook = None
    assert P.read_cursor(proj) == before and G.read_active_manifest(proj).generation_id == active
    # death after receipts before build ⇒ same
    with ob.open("ab") as fh:
        fh.write(b"bad\n")
    G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == "p0:after_receipts" else None
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    G._crash_hook = None
    assert P.read_cursor(proj) == before and len(P.read_malformed_receipts(proj)) == 1
    # death after the cursor before the dirty clear ⇒ next drain finds nothing pending (dirty still set ⇒ one more rebuild + clear)
    PD.mark_projection_dirty(reason="x", root=profile / "memory")
    G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == "p0:after_cursor" else None
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    G._crash_hook = None
    assert P.read_cursor(proj).outbox_byte_offset == ob.stat().st_size and PD.projection_dirty_state(root=profile / "memory")["dirty"]
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.deltas_seen == 0 and rep.needs_build and rep.dirty_cleared and rep.status == "reused_existing"
    assert P.drain_once(profile, reason="deepsleep").status == "no_work"


def test_two_generation_publisher_mid_batch_is_refused_and_outbox_append_during_drain_is_pending(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    real_build = P.build_generation
    seen = {}

    def build_and_probe(*a, **k):
        out = real_build(*a, **k)
        with pytest.raises(G.PublicationAuthorityBusy):                                                    # a second publisher mid-batch ⇒ refused
            with G.projection_publication_authority(profile):
                pass
        _emit(profile, "k6", title="Astro", summary="astrocytes")                                          # an append DURING the drain
        seen["size_after_append"] = ob.stat().st_size
        return out

    monkeypatch.setattr(P, "build_generation", build_and_probe)
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status in ("published_new",) and rep.deltas_seen == 1
    assert P.read_cursor(proj).outbox_byte_offset < seen["size_after_append"]                              # the mid-drain append stays pending, never partial-parsed
    monkeypatch.setattr(P, "build_generation", real_build)
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.deltas_seen == 1 and rep2.status in ("published_new", "reused_existing")
