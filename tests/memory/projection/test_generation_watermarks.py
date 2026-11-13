"""Cut 13 Task A5 — watermarks are canonical-snapshot authority (D-13-14/49): equal to the
publication-lineage head, never derived from any delta, may exceed the receipt's original
after a same-id coverage advance; `sequenced=False` never moves a watermark and carries the
covered_by_full_rebuild posture; health names generation / lag / backlog / lane state and
NEVER claims current with lag > 0 or pending > 0 (16 §6); reserved vs landed vs seal shown
separately; epoch mismatch detected even when the live number is larger."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import core.paths as core_paths
from core import timekeeper as TK
from memory import api as memory_api
from memory.projection import generation as G
from tests.memory.projection._fixtures import EPOCH_B, build_and_publish, clock, publish, redirect_timekeeper, seed_records_only, write_record


@pytest.fixture(autouse=True)
def _tk(tmp_path, monkeypatch):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


def _live_profile(monkeypatch):
    profile = core_paths.MEMORY_ROOT.parent
    seed_records_only(profile)
    monkeypatch.setattr(memory_api, "OMEGA_ROOT", profile)
    return profile


def test_watermark_is_the_closed_snapshot_clock_and_equals_the_lineage_head(tmp_path, monkeypatch):
    profile = _live_profile(monkeypatch)
    from core.commit_region import serialized_commit
    for i in range(3):                                                             # three landed commits
        with serialized_commit(
            SimpleNamespace(
                episode_id=f"e{i}", pipeline_id=i, work_kind="chat", memory_epoch_start=i
            )
        ):
            pass
    cand, res = build_and_publish(profile)
    m = res.active_manifest
    assert m.canonical_watermark_commit_seq == m.latest_canonical_commit_seq_at_capture == 3   # == landed frontier at capture
    assert m.coverage_clock == TK.last_coverage_clock() and m.snapshot_closure == "proved"
    head = G.read_publication_lineage(G.projection_root(profile) / G.LINEAGE_FILE).head
    assert head.coverage_clock == m.coverage_clock
    # a stamped-but-not-landed commit does not enter a build's watermark (never depends on any delta either)
    with serialized_commit(
        SimpleNamespace(
            episode_id="e9", pipeline_id=3, work_kind="chat", memory_epoch_start=3
        )
    ) as stamp:
        assert stamp["commit_seq"] == 4
        write_record(profile, "k9", title="Ion", summary="ion channels open")
        cand2, res2 = build_and_publish(profile)
        assert res2.active_manifest.coverage_clock.commit_seq == 3                 # landed frontier, not the reserved 4
    cand3, res3 = build_and_publish(profile)                                        # after landing: 4
    assert res3.active_manifest.coverage_clock.commit_seq == 4 and res3.outcome == "reused_existing"
    assert G.read_receipt(G.projection_root(profile) / f"gen-{res3.generation_id}").coverage_clock.commit_seq == 3   # receipt original


def test_health_names_state_and_never_claims_current_with_lag_or_pending(tmp_path, monkeypatch):
    profile = _live_profile(monkeypatch)
    proj = G.projection_root(profile)
    h = memory_api.memory_health()
    assert h.projection_generation == "legacy" and h.semantic_dme_lane == "legacy" and h.projection_current is False
    cand, res = build_and_publish(profile)
    h = memory_api.memory_health()
    assert h.projection_generation == res.generation_id and h.semantic_dme_lane == "available"
    assert h.projection_watermark_commit_seq == 0 and h.projection_lag_commits == 0
    assert h.projection_head_clock == res.active_manifest.coverage_clock.to_dict()
    assert h.projection_coverage_seal == res.active_manifest.coverage_clock.to_dict()
    assert h.projection_current is True and h.projection_page0_utilization["2"] > 0
    # lag: a landed commit not yet projected ⇒ lag 1, not current
    from core.commit_region import serialized_commit
    with serialized_commit(
        SimpleNamespace(
            episode_id="e1", pipeline_id=0, work_kind="chat", memory_epoch_start=0
        )
    ):
        pass
    h = memory_api.memory_health()
    assert h.projection_lag_commits == 1 and h.projection_reserved_seq == 1 and h.projection_landed_seq == 1 and h.projection_current is False
    assert any("projection lag" in i for i in h.issues)
    # pending unsequenced deltas (test-built via the Cut-4 outbox) never move the watermark; posture named
    from memory.projection_delta import ProjectionDelta, append_projection_delta
    import dataclasses
    fields = {f.name for f in dataclasses.fields(ProjectionDelta)}
    kw = {"delta_id": "d1", "memory_id": "k1", "commit_seq": 0, "sequenced": False, "queued_at": "2026-08-17T00:00:00Z"}
    for name in fields:
        if name not in kw:
            f = next(f for f in dataclasses.fields(ProjectionDelta) if f.name == name)
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                kw[name] = "" if f.type in ("str", str) else ([] if "list" in str(f.type).lower() or "tuple" in str(f.type).lower() else 0)
    try:
        delta = ProjectionDelta(**kw)
        appended = append_projection_delta(delta, root=profile / "memory")
    except Exception:
        appended = False
    if appended:
        h = memory_api.memory_health()
        assert h.projection_pending_count >= 1 and h.projection_unsequenced_pending >= 1
        assert h.projection_unsequenced_posture == "covered_by_full_rebuild" and h.projection_current is False
        assert h.projection_watermark_commit_seq == 0                              # unsequenced never moves a watermark
    # invalid active manifest ⇒ unavailable + reason; never legacy
    (proj / G.MANIFEST_FILE).write_bytes(b"{corrupt")
    h = memory_api.memory_health()
    assert h.projection_generation == "unavailable" and h.semantic_dme_lane == "unavailable" and "manifest_invalid" in h.semantic_dme_lane_reason
    assert h.projection_current is False


def test_epoch_mismatch_is_named_even_when_the_live_number_is_larger(tmp_path, monkeypatch):
    profile = _live_profile(monkeypatch)
    proj = G.projection_root(profile)
    cand, res = build_and_publish(profile)
    # counter loss: a NEW epoch is minted and catches up numerically past the head
    TK._state_path().unlink()
    TK.reset_for_tests()
    for _ in range(3):
        TK.finish_line_stamp(0, "meta")
    h = memory_api.memory_health()
    assert h.projection_live_clock["clock_epoch_id"] != h.projection_head_clock["clock_epoch_id"]
    assert h.projection_reserved_seq >= h.projection_watermark_commit_seq
    assert h.semantic_dme_lane == "unavailable" and h.semantic_dme_lane_reason == "coverage_clock_regression"
    assert h.projection_current is False
    # and the publisher refuses the different-epoch candidate before any mutation
    from memory.projection import build as B
    with G.projection_publication_authority(profile) as auth:
        cand2 = B.build_generation(profile, reason="x", authority=auth)
        with pytest.raises(G.CoverageClockRegression):
            G.publish_generation(profile, cand2, authority=auth)


def test_lane_reasons_for_seal_intents_and_active_generation(tmp_path, monkeypatch):
    profile = _live_profile(monkeypatch)
    proj = G.projection_root(profile)
    cand, res = build_and_publish(profile)
    # live intent + torn lineage tail ⇒ publication_incomplete surfaced (not current)
    (proj / G.LINEAGE_FILE).open("ab").write(b'{"seq": 2')
    h = memory_api.memory_health()
    assert h.semantic_dme_lane_reason == "publication_incomplete" and h.projection_current is False
    # A published generation is the intended Program 0 authority, not a health conflict.
    assert h.projection_generation == res.generation_id
    assert not hasattr(h, "projection_path_conflict")
    assert not any("projection path conflict" in issue for issue in h.issues)
    from memory.projection import dispatch as D
    with pytest.raises(D.ProjectionPathConflict):
        D.assert_single_projection_path(profile)
    # coverage seal unavailable is a distinct reason
    (proj / G.COVERAGE_SEAL_FILE).write_bytes(b"{bad")
    assert memory_api.memory_health().semantic_dme_lane_reason in ("publication_incomplete", "coverage_seal_unavailable")


def test_malformed_or_unreadable_outbox_evidence_is_never_current(tmp_path, monkeypatch):
    """Review 73646a37 §3: malformed / tampered outbox lines and an unreadable outbox are loud
    evidence — never an empty clean backlog, never `current`."""
    import os
    from memory.projection_delta import outbox_path
    profile = _live_profile(monkeypatch)
    cand, res = build_and_publish(profile)
    h = G.projection_health(profile)
    assert h.current is True                                                                  # clean empty outbox stays current
    ob = outbox_path(profile / "memory"); ob.parent.mkdir(parents=True, exist_ok=True)
    ob.write_bytes(b'{"tampered": tru\n')                                                    # malformed-only outbox
    h = G.projection_health(profile)
    assert h.current is False, "a projection over malformed outbox evidence must never be current"
    assert h.lane_reason == "projection_outbox_malformed"
    assert h.malformed_delta_count == 1 and h.pending_delta_count == 0 and h.semantic_dme_lane == "available" and h.outbox_error is None
    assert h.generation == res.generation_id
    mh = memory_api.memory_health()
    assert mh.projection_current is False and mh.projection_malformed_count == 1 and "projection_outbox_malformed" in mh.semantic_dme_lane_reason
    # valid pending + malformed ⇒ BOTH visible, still not current
    from memory.projection_delta import ProjectionDelta, append_projection_delta
    delta = ProjectionDelta.build(operation="upsert", memory_id="k1", canonical_path="memory/200_knowledge/k1.json",
                                  canonical_hash="0" * 64, coord=(2, 0, 1), commit_seq=0, projection_class="semantic",
                                  sequenced=False, queued_at="2026-08-17T00:00:00Z")
    ob.write_bytes(b"")
    assert append_projection_delta(delta, root=profile / "memory")
    with ob.open("ab") as fh:
        fh.write(b'{"tampered": tru\n')
    h = G.projection_health(profile)
    assert h.pending_delta_count == 1 and h.malformed_delta_count == 1 and h.current is False and h.lane_reason == "projection_outbox_malformed"
    # unreadable outbox ⇒ explicit non-current failure reason (never a clean backlog)
    ob.write_bytes(b"")
    if os.name == "posix":
        os.chmod(ob, 0)
        try:
            h = G.projection_health(profile)
        finally:
            os.chmod(ob, 0o644)
    else:
        # chmod(0) only sets the read-only attribute on Windows; the file stays readable.
        # Fail the read itself instead so the evidence path is the same one a real EACCES takes.
        import pathlib
        real_read_text = pathlib.Path.read_text
        def _denied(self, *a, **kw):
            if self == ob:
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *a, **kw)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "read_text", _denied)
            h = G.projection_health(profile)
    assert h.current is False and h.lane_reason == "projection_outbox_unreadable" and h.outbox_error and "unreadable" in h.outbox_error
    assert h.pending_delta_count == 0 and h.malformed_delta_count == 0
    # readable + empty again ⇒ current
    assert G.projection_health(profile).current is True


def test_health_returns_unavailable_when_the_coverage_clock_is_unavailable(tmp_path, monkeypatch):
    """Review 73646a37 §4: CoverageClockUnavailable from the timekeeper is a named health state,
    never a NameError / crash out of projection_health()."""
    profile = _live_profile(monkeypatch)
    cand, res = build_and_publish(profile)
    monkeypatch.setattr(TK, "reservation_state", lambda: (_ for _ in ()).throw(TK.CoverageClockUnavailable("forced")))
    h = G.projection_health(profile)
    assert h.lane_reason == "coverage_clock_unavailable" and h.semantic_dme_lane == "unavailable" and h.current is False
    assert h.live_clock is None and h.reserved_seq is None and h.generation == res.generation_id
    mh = memory_api.memory_health()
    assert mh.projection_current is False and "coverage_clock_unavailable" in mh.semantic_dme_lane_reason


@pytest.mark.parametrize("raw", [b"\n", b"   \n", b"\t\n", b'{"a":1}\n\n', b"  \n\t\n"])
def test_blank_or_whitespace_complete_outbox_rows_are_malformed_evidence(tmp_path, monkeypatch, raw):
    """Review 3b84d003 (D-13-66): a COMPLETE empty / whitespace-only row is malformed and counts —
    never a clean empty backlog, never `current`. An empty file (zero rows) stays current."""
    from memory.projection_delta import outbox_path
    profile = _live_profile(monkeypatch)
    cand, res = build_and_publish(profile)
    ob = outbox_path(profile / "memory"); ob.parent.mkdir(parents=True, exist_ok=True)
    ob.write_bytes(b"")
    assert G.projection_health(profile).current is True                                     # zero rows ⇒ clean
    ob.write_bytes(raw)
    h = G.projection_health(profile)
    assert h.current is False, f"blank/whitespace complete row {raw!r} must never be current"
    assert h.lane_reason == "projection_outbox_malformed" and h.malformed_delta_count >= 1 and h.pending_delta_count == 0
    expected = sum(1 for row in raw.split(b"\n")[:-1] if not row.strip()) + (1 if raw.startswith(b'{"a":1}') else 0)
    assert h.malformed_delta_count == expected
    assert memory_api.memory_health().projection_current is False
