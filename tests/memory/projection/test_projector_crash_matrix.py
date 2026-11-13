"""Program 0 P0.2 — crash matrix through the drain (paper §17.4 + D-13-43/47/48/49/51/52/54/55/58/61/72/76):
death inside the build ⇒ G active, dirs GC'd; torn manifest ignored; second publisher refused; canonical write
during capture ⇒ reject/retry then publish; obligation at startup ⇒ blocked then next drain publishes; rotate-first
intervals incl. crash after rotation before the rebuild (the recovery receipt is written only after the rebuild);
repair intent + rebind intervals through the drain (cursor never advances on a partial repair; recovery completes,
cursor settles once); lineage class c ⇒ lineage_operator_required; same-id coverage intervals; coverage-clock reset
under 1/1 ⇒ regression, no publication, no settlement; receipt intervals (Model A)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import build as B
from memory.projection import generation as G
from memory.projection import projector as P
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only, write_record


@pytest.fixture(autouse=True)
def _env(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    G._crash_hook = None
    TK.reset_for_tests()


def _emit(profile, mid, **kw):
    write_record(profile, mid, **kw)
    path = profile / "memory" / "200_knowledge" / f"{mid}.json"
    out = PD.emit_delta_for_canonical(json.loads(path.read_text()), path, root=profile / "memory")
    assert out["durable"]
    return out["delta"]


def _established(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    assert P.drain_once(profile, reason="boot").status == "published_new"
    return profile, G.projection_root(profile), PD.outbox_path(profile / "memory")


def _crash_at(point):
    G._crash_hook = lambda p: (_ for _ in ()).throw(G.SimulatedCrash(p)) if p == point else None


def test_death_inside_the_build_leaves_g_active_and_cleans_dirs_cursor_unchanged(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    active = G.read_active_manifest(proj).generation_id; before = P.read_cursor(proj)
    import memory.dme.adapter as AD
    monkeypatch.setattr(AD, "_rebuild_generation", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("death during plane publication")))
    with pytest.raises(RuntimeError):
        P.drain_once(profile, reason="deepsleep")
    assert G.read_active_manifest(proj).generation_id == active and P.read_cursor(proj) == before
    monkeypatch.undo()
    rep = P.drain_once(profile, reason="deepsleep")                        # the wrapper recovers stale state, then ONE build
    assert rep.status == "published_new" and not any(p.name.startswith(B.WORK_DIR_PREFIX) or p.name.endswith(G.CANDIDATE_SUFFIX) for p in proj.iterdir())
    G.validate_published_generation(proj, G.read_active_manifest(proj))


def test_torn_manifest_ignored_and_canonical_write_during_capture_retries_then_publishes(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    (proj / "manifest.json.tmp").write_bytes(b'{"schema_version": "omega.projection_manifest.v1", "generation_id": "')
    assert P.drain_once(profile, reason="deepsleep").status == "no_work"    # torn temp manifest ignored; G stays authority
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    from memory.taxonomy import compiler as C
    real = C.compile_memory_graph
    n = {"i": 0}

    def compile_then_write_once(**kw):
        n["i"] += 1
        if n["i"] == 1:
            write_record(profile, "k6", title="Astro", summary="astrocytes")    # a canonical write DURING the closed-snapshot capture
        return real(**kw)

    monkeypatch.setattr(C, "compile_memory_graph", compile_then_write_once)
    rep = P.drain_once(profile, reason="deepsleep")
    assert n["i"] == 2 and rep.status == "published_new" and rep.full_builds == 1 and rep.cursor_settled   # reject/retry inside ONE build; then publish
    seats = [s["memory_id"] for s in json.loads((proj / f"gen-{rep.generation_id}" / "sram_index.json").read_text())]
    assert {"k5", "k6"} <= set(seats)


def test_obligation_at_startup_blocks_then_boot_recovery_lets_the_next_drain_publish(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    import memory.encoding_journal as EJ
    monkeypatch.setattr(EJ, "journal_status", lambda: {"clean": False, "unclosed_count": 1, "malformed_count": 0})
    before = P.read_cursor(proj)
    assert P.drain_once(profile, reason="boot").status == "blocked_on_obligations" and P.read_cursor(proj) == before
    monkeypatch.setattr(EJ, "journal_status", lambda: {"clean": True})   # boot recovery closed it
    rep = P.drain_once(profile, reason="boot")
    assert rep.status == "published_new" and rep.cursor_settled


def test_crash_after_rotation_before_the_rebuild_resumes_the_recovery_rebuild(tmp_path):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    P.drain_once(profile, reason="deepsleep")
    _emit(profile, "k6", title="Astro", summary="astrocytes")
    active = G.read_active_manifest(proj).generation_id
    ob.write_bytes(ob.read_bytes()[: P.read_cursor(proj).observed_size - 3])           # corrupt (truncated into observed bytes)
    _crash_at("p0:after_capture")                                                       # rotation durable ("rotated" row), rebuild NOT run
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    G._crash_hook = None
    rows = [json.loads(line) for line in (proj / PD.OUTBOX_RECOVERY_FILE).read_text().splitlines()]
    assert [r["stage"] for r in rows] == ["begin", "rotated"] and P.read_epoch(proj).epoch == 2
    assert G.read_active_manifest(proj).generation_id == active                          # no rebuild yet
    pending = PD.pending_outbox_recovery(profile / "memory")
    assert pending is not None and pending["rotated"] is True
    rep = P.drain_once(profile, reason="deepsleep")                                      # restart: skips rotation, performs the ONE recovery rebuild
    assert rep.status == "outbox_corrupt_recovered" and rep.full_builds == 1 and rep.cursor_settled and rep.generation_id != active
    rows = [json.loads(line) for line in (proj / PD.OUTBOX_RECOVERY_FILE).read_text().splitlines()]
    assert [r["stage"] for r in rows] == ["begin", "rotated", "completed"] and P.read_epoch(proj).epoch == 2   # exactly one epoch, one quarantine
    assert len(list(ob.parent.glob("*.quarantine-*"))) == 1 and PD.pending_outbox_recovery(profile / "memory") is None
    seats = [s["memory_id"] for s in json.loads((proj / f"gen-{rep.generation_id}" / "sram_index.json").read_text())]
    assert "k6" in seats
    # crash after the rebuild publication BEFORE the final receipt ⇒ next drain finishes the receipt with ONE more build (Model A)
    _emit(profile, "k7", title="Micro", summary="microglia")
    tmp = ob.with_suffix(".x"); tmp.write_bytes(b""); os.replace(tmp, ob)               # identity change ⇒ corrupt (observed_size 0 makes truncation lawful)
    _crash_at("p0:after_barrier")
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    G._crash_hook = None
    assert PD.pending_outbox_recovery(profile / "memory") is not None
    rep2 = P.drain_once(profile, reason="deepsleep")
    assert rep2.status == "outbox_corrupt_recovered" and rep2.full_builds == 1 and PD.pending_outbox_recovery(profile / "memory") is None
    assert P.read_epoch(proj).epoch == 3


@pytest.mark.parametrize("point", ["after_repair_intent", "after_quarantine", "after_candidate_rename", "after_repair_row", "after_rebind_intent", "after_rebind_row"])
def test_repair_and_rebind_intervals_through_the_drain_never_advance_the_cursor_on_a_partial_repair(tmp_path, point):
    profile, proj, ob = _established(tmp_path)
    gid = G.read_active_manifest(proj).generation_id
    (proj / f"gen-{gid}" / "sram.bin").write_bytes(b"\x00" * 64)                        # corrupt G; the clean rebuild is G again ⇒ repair path
    k1 = json.loads((profile / "memory" / "200_knowledge" / "k1.json").read_text())
    PD.emit_delta_for_canonical(k1, profile / "memory" / "200_knowledge" / "k1.json", root=profile / "memory")
    before = P.read_cursor(proj)
    _crash_at(point)
    try:
        P.drain_once(profile, reason="deepsleep")
        crashed = False
    except G.SimulatedCrash:
        crashed = True
    G._crash_hook = None
    if crashed:
        assert P.read_cursor(proj) == before                                            # cursor never advances on a partial repair
    rep = P.drain_once(profile, reason="deepsleep")                                      # restart: intents recovered under the token, then the drain settles ONCE
    assert rep.cursor_settled or rep.status == "no_work"
    G.validate_published_generation(proj, G.read_active_manifest(proj))
    assert G.read_active_manifest(proj).generation_id == gid
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert sum(1 for r in scan.rows if r.event == "replaced_corrupt") <= 1
    assert P.read_cursor(proj).outbox_byte_offset == ob.stat().st_size


def test_lineage_class_c_is_operator_required_and_cursor_unchanged(tmp_path):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    assert P.drain_once(profile, reason="deepsleep").status == "published_new"          # two chained rows
    _emit(profile, "k6", title="Astro", summary="astrocytes")
    before = P.read_cursor(proj)
    lineage = proj / G.LINEAGE_FILE
    rows = lineage.read_bytes().splitlines(keepends=True)
    assert len(rows) == 2
    tampered = json.loads(rows[0]); tampered["generation_receipt_sha256"] = "0" * 64     # a NON-head row rewritten ⇒ chain broken (class c)
    lineage.write_bytes(json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode() + b"\n" + b"".join(rows[1:]))
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "lineage_operator_required" and not rep.cursor_settled and P.read_cursor(proj) == before


@pytest.mark.parametrize("point", ["after_intent", "after_row", "after_manifest"])
def test_same_id_coverage_intervals_complete_from_the_intent_and_the_cursor_settles_once(tmp_path, point):
    profile, proj, ob = _established(tmp_path)
    from core.commit_region import serialized_commit
    from types import SimpleNamespace
    with serialized_commit(
        SimpleNamespace(
            episode_id="e1", pipeline_id=0, work_kind="chat", memory_epoch_start=0
        )
    ):
        pass                                                                             # coverage advances (100 → 101 shape) with unchanged content
    k1 = json.loads((profile / "memory" / "200_knowledge" / "k1.json").read_text())
    PD.emit_delta_for_canonical(k1, profile / "memory" / "200_knowledge" / "k1.json", root=profile / "memory")
    gid = G.read_active_manifest(proj).generation_id
    _crash_at(point)
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    G._crash_hook = None
    rep = P.drain_once(profile, reason="deepsleep")
    m = G.read_active_manifest(proj)
    assert m.generation_id == gid and m.coverage_clock.commit_seq == 1                    # 101 completed (from the intent or by this drain's barrier)
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert len(scan.rows) == 2 and scan.rows[-1].coverage_clock.commit_seq == 1          # no duplicate row
    assert P.read_cursor(proj).outbox_byte_offset == ob.stat().st_size and rep.status in ("reused_existing", "no_work")
    G.validate_published_generation(proj, m)


def test_coverage_clock_reset_under_program0_is_regression_no_publication_no_settlement(tmp_path):
    profile, proj, ob = _established(tmp_path)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    before = P.read_cursor(proj); active = G.read_active_manifest(proj).generation_id
    TK._state_path().unlink(); TK.reset_for_tests()                                     # a reset timekeeper ⇒ new epoch id
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.status == "coverage_clock_regression" and rep.full_builds == 1 and not rep.cursor_settled
    assert P.read_cursor(proj) == before and G.read_active_manifest(proj).generation_id == active
    assert rep.head_clock is None and rep.live_clock is not None and rep.live_clock.clock_epoch_id != G.read_active_manifest(proj).coverage_clock.clock_epoch_id


def test_receipt_intervals_crash_before_receipt_durability_never_advances(tmp_path, monkeypatch):
    profile, proj, ob = _established(tmp_path)
    with ob.open("ab") as fh:
        fh.write(b"garbage\n")
    before = P.read_cursor(proj)
    monkeypatch.setattr(P, "persist_malformed_receipts", lambda proj_, rows: (_ for _ in ()).throw(OSError("disk full before receipt durability")))
    with pytest.raises(OSError):
        P.drain_once(profile, reason="deepsleep")
    assert P.read_cursor(proj) == before and P.read_malformed_receipts(proj) == ()
    monkeypatch.undo()
    rep = P.drain_once(profile, reason="deepsleep")
    assert rep.malformed_row_count == 1 and rep.cursor_settled and len(P.read_malformed_receipts(proj)) == 1
