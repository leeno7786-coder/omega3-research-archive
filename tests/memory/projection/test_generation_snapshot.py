"""Cut 13 Task A4 — the builder from a CLOSED canonical snapshot (D-13-20/26/36): strict
membership, profile-form input capture (SourceLedger + registry authority), coord index
built from captured files, compile + DME into the candidate only, closure proof + retry,
obligations at both boundaries, page-0 refusal, work_dir always removed, one clock seam."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import timekeeper as TK
from memory import association_strength
from memory.projection import build as B
from memory.projection import generation as G
from memory.projection import registry_authority as RA
from tests.memory.projection._fixtures import (
    build_and_publish, redirect_timekeeper, seed_legacy_profile, seed_records_only, write_record,
)

ARTIFACTS = {"coord_index.snapshot.json", "coordinate_registry.json", "graph.sqlite", "graph_snapshot.json", "sram.bin",
             "sram_meta.json", "sram_index.json", "dme_manifest.json", "retina_codes.bin", "retina_index.json",
             "planes/hop_plane_strong.bin", "planes/hop_plane_weak.bin"}


@pytest.fixture(autouse=True)
def _tk(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


def _profile(tmp_path, *, legacy=False):
    profile = tmp_path / "profile"; profile.mkdir()
    (seed_legacy_profile if legacy else seed_records_only)(profile)
    return profile


def _build(profile, **kw):
    with G.projection_publication_authority(profile) as auth:
        return B.build_generation(profile, reason="test", authority=auth, **kw)


def test_candidate_layout_and_work_dir_removed(tmp_path):
    profile = _profile(tmp_path)
    proj = G.projection_root(profile)
    cand = _build(profile)
    assert isinstance(cand, G.Candidate)
    files = {p.relative_to(cand.candidate_dir).as_posix() for p in cand.candidate_dir.rglob("*") if p.is_file()}
    assert files == ARTIFACTS                                                # exactly the future gen contents (receipt is the publisher's)
    assert not any(p.name.startswith(B.WORK_DIR_PREFIX) for p in proj.iterdir())   # work_dir removed on success
    assert cand.manifest.snapshot_closure == "proved" and cand.manifest.unscorable_coordinate_count == 0
    assert [s.kind for s in cand.manifest.content.source_inputs] == ["canonical_corpus", "source_ledger"]
    assert cand.source_input_origins["coordinate_registry"] == "empty"
    G.validate_candidate(cand.candidate_dir, cand.manifest)
    # no canonical hardlink / input-profile file / ledger copy inside the candidate
    for p in cand.candidate_dir.rglob("*"):
        if p.is_file():
            assert os.stat(p).st_nlink == 1, p
    # the coord index snapshot equals the index BUILT from the captured files (directory authority)
    snapshot = json.loads((cand.candidate_dir / "coord_index.snapshot.json").read_text())
    assert set(snapshot) == {"k1", "k2", "k3", "k4"} and all(snapshot[k]["recall_proj"] == 1 for k in snapshot)
    seats = json.loads((cand.candidate_dir / "sram_index.json").read_text())
    assert [s["memory_id"] for s in seats] == ["k1", "k2", "k3", "k4"]
    meta = json.loads((cand.candidate_dir / "sram_meta.json").read_text())
    assert "built_at" not in meta and "backend" not in meta                    # D-13-21 deterministic schema
    dme_manifest = json.loads((cand.candidate_dir / "dme_manifest.json").read_text())
    assert dme_manifest["sram_path"] == "sram.bin" and "backend" not in dme_manifest


def test_generation_content_pins_association_generation(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    association_generation = "b" * 64
    monkeypatch.setattr(association_strength, "association_generation", lambda: association_generation)

    cand = _build(profile)

    assert isinstance(cand, G.Candidate)
    assert cand.manifest.content.association_generation == association_generation


def test_generation_id_changes_when_association_sidecar_changes(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    sidecar_path = tmp_path / "guardian" / "state" / "association_strength.json"
    monkeypatch.setattr(association_strength, "association_path", lambda: sidecar_path)

    first = _build(profile)
    assert isinstance(first, G.Candidate)
    with association_strength.deep_sleep_mutation():
        association_strength.AssociationStrengthIndex(sidecar_path).register(
            "k1",
            "SUPPORTS",
            "k2",
            provenance_class=association_strength.STRUCTURAL_PROVENANCE_CLASS,
            structural_strength=1.0,
            support_count=1,
        )
    second = _build(profile)

    assert isinstance(second, G.Candidate)
    assert first.manifest.generation_id != second.manifest.generation_id
    assert first.manifest.content.association_generation != second.manifest.content.association_generation


def test_directory_authority_over_the_live_coord_index(tmp_path):
    profile = _profile(tmp_path)
    mem = profile / "memory"
    # a valid canonical file ABSENT from the live coord_index is still in the generation
    idx = mem / "indexes" / "coord_index.json"
    live = json.loads(idx.read_text()); live.pop("k4"); live["ghost"] = {"coord": [2, 0, 9], "path": "memory/200_knowledge/ghost.json"}
    idx.write_text(json.dumps(live))
    cand = _build(profile)
    snapshot = json.loads((cand.candidate_dir / "coord_index.snapshot.json").read_text())
    assert "k4" in snapshot and "ghost" not in snapshot                          # stale entry manufactures nothing
    seats = [s["memory_id"] for s in json.loads((cand.candidate_dir / "sram_index.json").read_text())]
    assert "k4" in seats and "ghost" not in seats


@pytest.mark.parametrize("kind,mutate", [
    ("unreadable", lambda mem: (mem / "200_knowledge" / "k1.json").write_text("{not json")),
    ("schema_invalid", lambda mem: (mem / "200_knowledge" / "k1.json").write_text(json.dumps({"id": "k1", "coord": [2, 0, 1]}))),
    ("duplicate", lambda mem: ((mem / "100_personas").mkdir(exist_ok=True),
                               (mem / "100_personas" / "k2.json").write_text(json.dumps({**json.loads((mem / "200_knowledge" / "k2.json").read_text()), "coord": [1, 0, 2]})))),
    ("ambiguous_name", lambda mem: (mem / "200_knowledge" / "renamed.json").write_text((mem / "200_knowledge" / "k1.json").read_text()) or (mem / "200_knowledge" / "k1.json").unlink()),
    ("ambiguous_lobe", lambda mem: ((mem / "100_personas").mkdir(exist_ok=True), (mem / "100_personas" / "k9.json").write_text(json.dumps({**json.loads((mem / "200_knowledge" / "k1.json").read_text()), "id": "k9"})))),
    ("ambiguous_no_id", lambda mem: (mem / "200_knowledge" / "noid.json").write_text(json.dumps({"coord": [2, 0, 1], "title": "x"}))),
])
def test_strict_membership_blocks_publication_never_skips(tmp_path, kind, mutate):
    profile = _profile(tmp_path)
    mutate(profile / "memory")
    out = _build(profile)
    assert isinstance(out, B.BuildRejected) and out.reason == "canonical_membership_error", out
    if kind == "duplicate":
        assert out.detail["kind"] == "duplicate" and out.detail["memory_id"] == "k2"
    else:
        assert out.detail["kind"] == kind, out.detail
    assert out.detail["path"].startswith("memory/")
    proj = G.projection_root(profile)
    assert not (proj / G.MANIFEST_FILE).exists() and not any(p.name.startswith(B.WORK_DIR_PREFIX) for p in proj.iterdir())


def test_schema_loader_failure_fails_closed_never_admits_membership(tmp_path, monkeypatch):
    """Review 73646a37 §1 (D-13-26): the strict-membership contract depends on the schema check;
    a schema that cannot be loaded (or is absent) BLOCKS the generation as
    canonical_membership_error{kind=schema_unavailable} — never a silent no-op that admits
    a schema-invalid record."""
    from memory.engine import reindex as RX
    profile = _profile(tmp_path)
    mem = profile / "memory"
    # a REAL schema-invalid record (the Cut-5A fixture shape: no provenance/flags/audit) is refused by the real schema
    (mem / "200_knowledge" / "k1.json").write_text(json.dumps({"id": "k1", "coord": [2, 0, 1], "title": "x", "summary": "y"}))
    out = _build(profile)
    assert isinstance(out, B.BuildRejected) and out.detail["kind"] == "schema_invalid" and out.detail["memory_id"] == "k1"
    # the schema LOADER itself raising ⇒ schema_unavailable, the record is NOT accepted, no candidate/work dir survives
    monkeypatch.setattr(RX, "load_schema", lambda: (_ for _ in ()).throw(RuntimeError("forced schema loader failure")))
    with pytest.raises(B.MembershipError) as ei:
        B.scan_canonical_membership(mem)
    assert ei.value.kind == "schema_unavailable" and "forced schema loader failure" in ei.value.path
    out = _build(profile)
    assert isinstance(out, B.BuildRejected) and out.reason == "canonical_membership_error" and out.detail["kind"] == "schema_unavailable"
    proj = G.projection_root(profile)
    assert not (proj / G.MANIFEST_FILE).exists()
    assert not any(p.name.startswith(B.WORK_DIR_PREFIX) or p.name.endswith(G.CANDIDATE_SUFFIX) for p in proj.iterdir())
    # an ABSENT schema file is the same mechanism failure ⇒ schema_unavailable (fail closed; not the legacy warn-and-skip)
    monkeypatch.undo()
    monkeypatch.setattr(RX, "SCHEMA_PATH", tmp_path / "missing_schema.json")
    with pytest.raises(B.MembershipError) as ei:
        B.scan_canonical_membership(mem)
    assert ei.value.kind == "schema_unavailable"
    # with the real schema restored a VALID corpus still builds (strict membership unchanged)
    monkeypatch.undo()
    (mem / "200_knowledge" / "k1.json").unlink()
    write_record(profile, "k1", title="Neuron", summary="Neurons pass electrical signals through synapses to other neurons.")
    cand = _build(profile)
    assert isinstance(cand, G.Candidate)


def test_inputs_are_read_under_the_captured_profile_and_writes_land_in_the_candidate(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    import memory.source_ledger.store as SL
    import memory.taxonomy.canonical_events as CE
    import memory.dme.adapter as A
    from memory.taxonomy import compiler as C
    roots: dict = {"ledger": [], "events": [], "canonical": []}
    real_read = SL.read_source
    monkeypatch.setattr(SL, "read_source", lambda sid, *, root=None: roots["ledger"].append(Path(root)) or real_read(sid, root=root))
    monkeypatch.setattr(C, "read_source", SL.read_source, raising=False)
    real_events = CE.read_events_from_canonical
    monkeypatch.setattr(C, "read_events_from_canonical", lambda **kw: roots["events"].append(Path(kw.get("root"))) or real_events(**kw))
    real_load = A._load_canonical_for_index_entry
    monkeypatch.setattr(A, "_load_canonical_for_index_entry", lambda entry, *, profile_root: roots["canonical"].append(Path(profile_root)) or real_load(entry, profile_root=profile_root))
    saves: list[Path] = []
    from memory.dme import coordinate_registry as CR
    real_save = CR.CoordinateRegistry._save
    monkeypatch.setattr(CR.CoordinateRegistry, "_save", lambda self: saves.append(self._path) or real_save(self))
    cand = _build(profile)
    assert isinstance(cand, G.Candidate)
    live_mem = (profile / "memory").resolve()
    for key, seen in roots.items():
        assert seen, key
        for r in seen:
            assert "input_profile" in r.resolve().parts, (key, r)                # never the live root during graph/DME construction
            assert r.resolve() not in (live_mem, live_mem.parent)
    assert saves and all(p.resolve().parent.name in ("candidate",) or p.resolve().parent.name.startswith("gen-") for p in saves)
    assert not (profile / "memory" / "dme" / "coordinate_registry.json").exists()   # the live registry is never written by the builder
    # the compiler resolved every ledger citation: no accidental quarantine from a wrong root
    import sqlite3
    conn = sqlite3.connect(str(cand.candidate_dir / "graph.sqlite"))
    statuses = {json.loads(r[1]).get("status") for r in conn.execute("SELECT node_id, payload_json FROM nodes WHERE node_type='memory_record'")}
    conn.close()
    assert statuses == {"active"}


def test_ledger_change_during_build_rejects_and_retries_and_id_changes_when_ledger_changes(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    from memory.taxonomy import compiler as C
    real = C.compile_memory_graph
    calls = {"n": 0}

    def compile_then_mutate(**kw):
        calls["n"] += 1
        snap = real(**kw)
        if calls["n"] == 1:
            from memory.source_ledger.store import record_source
            record_source("late ledger artifact", source_type="operator_chat", origin_id="late", root=profile)
        return snap

    monkeypatch.setattr("memory.projection.build.compile_memory_graph", compile_then_mutate, raising=False)
    import memory.projection.build as BB
    monkeypatch.setattr(C, "compile_memory_graph", compile_then_mutate)
    cand = _build(profile)
    assert isinstance(cand, G.Candidate) and calls["n"] == 2                       # first attempt: source_ledger_moved ⇒ retried
    monkeypatch.setattr(C, "compile_memory_graph", real)
    # the ledger input hash changed ⇒ a different id than a profile without the late artifact
    profile2 = tmp_path / "profile2"; profile2.mkdir(); seed_records_only(profile2)
    cand2 = _build(profile2)
    ledger_a = [s for s in cand.manifest.content.source_inputs if s.kind == "source_ledger"][0].sha256
    ledger_b = [s for s in cand2.manifest.content.source_inputs if s.kind == "source_ledger"][0].sha256
    assert ledger_a != ledger_b and cand.manifest.generation_id != cand2.manifest.generation_id


def test_canonical_write_during_capture_rejects_retries_and_reflects_the_new_file(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    from memory.taxonomy import compiler as C
    real = C.compile_memory_graph
    calls = {"n": 0}
    captured_bytes = {}

    def compile_then_write(**kw):
        calls["n"] += 1
        input_profile = Path(kw["source_root"])
        if calls["n"] == 1:
            captured_bytes["k1"] = (input_profile / "memory" / "200_knowledge" / "k1.json").read_bytes()
            write_record(profile, "k5", title="Glia", summary="Glial cells support neurons.")   # a canonical write mid-capture
            # the hardlink capture is point-in-time: the captured k1 is unchanged even after a live atomic rewrite
            live_k1 = profile / "memory" / "200_knowledge" / "k1.json"
            doc = json.loads(live_k1.read_text()); doc["summary"] = "rewritten live"
            tmp = live_k1.with_suffix(".tmp"); tmp.write_text(json.dumps(doc)); os.replace(tmp, live_k1)
            assert (input_profile / "memory" / "200_knowledge" / "k1.json").read_bytes() == captured_bytes["k1"]
        return real(**kw)

    monkeypatch.setattr(C, "compile_memory_graph", compile_then_write)
    cand = _build(profile)
    assert isinstance(cand, G.Candidate) and calls["n"] == 2                       # attempt 1 snapshot_moved ⇒ retry
    seats = [s["memory_id"] for s in json.loads((cand.candidate_dir / "sram_index.json").read_text())]
    assert "k5" in seats and cand.manifest.content.canonical_files == 5           # the retry's id reflects the new file


def test_file_deleted_mid_capture_every_attempt_rejects_snapshot_moved_after_max_attempts(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    from memory.taxonomy import compiler as C
    real = C.compile_memory_graph
    n = {"i": 0}

    def compile_then_delete(**kw):
        n["i"] += 1
        write_record(profile, f"z{n['i']}", title="Extra", summary=f"extra record {n['i']}")
        return real(**kw)

    monkeypatch.setattr(C, "compile_memory_graph", compile_then_delete)
    out = _build(profile, max_attempts=3)
    assert isinstance(out, B.BuildRejected) and out.reason == "snapshot_moved" and out.attempts == 3
    proj = G.projection_root(profile)
    assert not any(p.name.startswith(B.WORK_DIR_PREFIX) or p.name.endswith(".candidate") for p in proj.iterdir())


def test_obligations_block_at_either_boundary(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    import memory.encoding_journal as EJ
    monkeypatch.setattr(EJ, "journal_status", lambda: {"clean": False, "unclosed_count": 1, "malformed_count": 0})
    out = _build(profile)
    assert isinstance(out, B.BuildRejected) and out.reason == "blocked_on_obligations" and out.detail["unclosed_count"] == 1
    # clean at entry, open at the closing boundary
    seq = iter([{"clean": True}, {"clean": False, "unclosed_count": 2, "malformed_count": 0}])
    monkeypatch.setattr(EJ, "journal_status", lambda: next(seq))
    out = _build(profile)
    assert isinstance(out, B.BuildRejected) and out.reason == "blocked_on_obligations"


def test_page0_overflow_makes_coordinates_unscorable_and_rejects(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    from memory.dme import coordinate_registry as CR
    monkeypatch.setattr(CR, "PAGE0_SLOTS", 3)                                       # tiny page 0 ⇒ tokens spill to page 1
    out = _build(profile)
    assert isinstance(out, B.BuildRejected) and out.reason == "unscorable_coordinates"
    assert out.detail["unscorable_coordinate_count"] > 0 and out.detail["memory_ids"] and out.detail["page0_capacity_by_lobe"]["2"] == 3
    proj = G.projection_root(profile)
    assert not (proj / G.MANIFEST_FILE).exists() and not any(p.name.endswith(".candidate") for p in proj.iterdir())


def test_registry_authority_input_binding_and_convergence(tmp_path):
    profile = _profile(tmp_path, legacy=True)                                       # legacy registry exists (migration input)
    proj = G.projection_root(profile)
    legacy_reg = (profile / "memory" / "dme" / "coordinate_registry.json").read_bytes()
    cand, res = build_and_publish(profile)
    assert cand.source_input_origins["coordinate_registry"] == "legacy:memory/dme/coordinate_registry.json"
    gen_dir = proj / f"gen-{cand.manifest.generation_id}"
    finalized = (gen_dir / "coordinate_registry.json").read_bytes()
    assert cand.manifest.content.registry_sha256 == G.sha256_bytes(finalized)         # binds the FINALIZED effective registry
    legacy_addr = json.loads(legacy_reg)["addresses"]; final_addr = json.loads(finalized)["addresses"]
    assert all(final_addr[k] == v for k, v in legacy_addr.items())                    # existing assignments never move
    sel = RA.select_authority(proj)
    assert sel and sel[0] == cand.manifest.content.registry_sha256                    # R1 selected
    # unchanged rebuild from R1 ⇒ ONE generation id, no authority churn
    cand2, res2 = build_and_publish(profile)
    assert cand2.manifest.generation_id == cand.manifest.generation_id and res2.outcome == "reused_existing"
    assert cand2.source_input_origins["coordinate_registry"] == f"authority:{sel[0]}"
    assert len(RA.read_authority_lineage(proj).rows) == 1
    # a pending key that sorts BEFORE existing keys never moves an existing assignment
    write_record(profile, "aaa1", title="Aardvark", summary="an aardvark eats ants")
    cand3, res3 = build_and_publish(profile)
    addr3 = json.loads((proj / f"gen-{cand3.manifest.generation_id}" / "coordinate_registry.json").read_text())["addresses"]
    assert all(addr3[k] == v for k, v in final_addr.items()) and cand3.manifest.generation_id != cand.manifest.generation_id


def test_registry_authority_change_during_build_rejects_registry_moved(tmp_path, monkeypatch):
    profile = _profile(tmp_path, legacy=True)
    build_and_publish(profile)
    proj = G.projection_root(profile)
    from memory.taxonomy import compiler as C
    real = C.compile_memory_graph
    n = {"i": 0}

    def compile_then_reselect(**kw):
        n["i"] += 1
        if n["i"] == 1:
            # a different authority selection lands between capture and closure (simulated on disk)
            import hashlib
            alt = b'{"addresses":{},"counters":{},"page_counters":{}}'
            sha = hashlib.sha256(alt).hexdigest()
            (proj / "registry_authority" / f"registry-{sha}.json").write_bytes(alt)
            manifest = proj / "registry_authority" / "manifest.json"
            doc = json.loads(manifest.read_text())
            manifest.write_text(json.dumps({"registry_sha256": sha, "seq": doc["seq"] + 1}))
        return real(**kw)

    monkeypatch.setattr(C, "compile_memory_graph", compile_then_reselect)
    write_record(profile, "k7", title="Soma", summary="the soma is the neuron body")
    out = _build(profile, max_attempts=1)
    assert isinstance(out, B.BuildRejected) and out.reason == "registry_moved"


def test_one_clock_capture_seam_and_coverage_unavailable(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    calls = {"clock": 0, "seq": 0}
    real_clock = TK.last_coverage_clock
    monkeypatch.setattr(B, "last_coverage_clock", lambda: (calls.__setitem__("clock", calls["clock"] + 1), real_clock())[1])
    real_seq = TK.last_commit_seq
    monkeypatch.setattr(TK, "last_commit_seq", lambda: (calls.__setitem__("seq", calls["seq"] + 1), real_seq())[1])
    cand = _build(profile)
    assert calls["clock"] == 1 and calls["seq"] == 0                                # ONE capture, no separate seq read
    assert cand.coverage_clock == real_clock()
    proj = G.projection_root(profile)
    candidates_before = {p.name for p in proj.iterdir() if p.name.endswith(".candidate")}
    monkeypatch.setattr(B, "last_coverage_clock", lambda: (_ for _ in ()).throw(TK.CoverageClockUnavailable("no epoch")))
    out = _build(profile)
    assert isinstance(out, B.BuildRejected) and out.reason == "coverage_clock_unavailable"
    assert not any(p.name.startswith(B.WORK_DIR_PREFIX) for p in proj.iterdir())            # no Candidate, no mutation
    assert {p.name for p in proj.iterdir() if p.name.endswith(".candidate")} == candidates_before


def test_crash_inside_build_removes_work_dir(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    from memory.taxonomy import compiler as C
    monkeypatch.setattr(C, "compile_memory_graph", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom mid-DME")))
    with pytest.raises(RuntimeError):
        _build(profile)
    proj = G.projection_root(profile)
    assert not any(p.name.startswith(B.WORK_DIR_PREFIX) or p.name.endswith(".candidate") for p in proj.iterdir())
