"""Cut 4 — ProjectionDelta outbox: durable projection WORK, never truth.

Governing: 16 (INV-MO-018 canonical clock != projection clock), 17 §5.2
(ProjectionDelta contract, LAW-EO-003/004), the Cut 4 plan Task 1.

The outbox is derivative and reconstructable. Canonical files remain
sufficient for a clean full rebuild. If append fails after a canonical write,
the canonical write stays valid and a loud dirty/rebuild-required marker is
set — never a canonical rollback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory.projection_delta import (
    PROJECTION_CLASSES,
    PROJECTION_DELTA_SCHEMA_VERSION,
    PROJECTION_DIRTY_SCHEMA_VERSION,
    PROJECTION_OPERATIONS,
    ProjectionDelta,
    append_projection_delta,
    mark_projection_dirty,
    native_citizenship_enabled,
    outbox_path,
    pending_projection_deltas,
    projection_delta_id,
    projection_dirty_path,
    projection_dirty_state,
)

# ── Task 1: schema / determinism ────────────────────────────────────────────


def test_projection_delta_id_is_deterministic():
    args = dict(
        operation="upsert",
        memory_id="m-1",
        canonical_path="memory/200_knowledge/a.json",
        canonical_hash="abc123",
        coord=(2, 4, 8),
        commit_seq=17,
        projection_class="semantic",
    )
    assert projection_delta_id(**args) == projection_delta_id(**args)


def test_projection_delta_identity_changes_with_canonical_content():
    common = dict(
        operation="upsert",
        memory_id="m-1",
        canonical_path="memory/200_knowledge/a.json",
        coord=(2, 4, 8),
        commit_seq=17,
        projection_class="semantic",
    )
    assert projection_delta_id(canonical_hash="a", **common) != projection_delta_id(
        canonical_hash="b", **common
    )


def test_projection_delta_identity_ignores_wall_clock():
    """queued_at is diagnostic; two records queued at different times for the
    same canonical change carry the same delta_id."""
    a = ProjectionDelta.build(
        operation="upsert", memory_id="m-1", canonical_path="memory/a.json", canonical_hash="h",
        coord=(2, 0, 1), commit_seq=3, projection_class="semantic", queued_at="2026-08-15T00:00:00+00:00",
    )
    b = ProjectionDelta.build(
        operation="upsert", memory_id="m-1", canonical_path="memory/a.json", canonical_hash="h",
        coord=(2, 0, 1), commit_seq=3, projection_class="semantic", queued_at="2026-08-16T12:34:56+00:00",
    )
    assert a.delta_id == b.delta_id and a.queued_at != b.queued_at
    payload = a.to_dict()
    for key in ("delta_id", "operation", "memory_id", "canonical_path", "canonical_hash", "coord",
                "commit_seq", "projection_class", "queued_at", "schema_version"):
        assert key in payload, key
    assert payload["schema_version"] == PROJECTION_DELTA_SCHEMA_VERSION == "omega.projection_delta.v1"


def test_projection_delta_vocabulary_is_frozen():
    assert PROJECTION_OPERATIONS == {"upsert", "supersede", "tombstone", "delete"}
    assert PROJECTION_CLASSES == {"semantic", "filesystem_only"}
    with pytest.raises(ValueError):
        ProjectionDelta.build(operation="mutate", memory_id="m", canonical_path="p", canonical_hash="h",
                              coord=(2, 0, 1), commit_seq=1, projection_class="semantic")
    with pytest.raises(ValueError):
        ProjectionDelta.build(operation="upsert", memory_id="m", canonical_path="p", canonical_hash="h",
                              coord=(2, 0, 1), commit_seq=1, projection_class="vector")
    with pytest.raises(ValueError):
        ProjectionDelta.build(operation="upsert", memory_id="", canonical_path="p", canonical_hash="h",
                              coord=(2, 0, 1), commit_seq=1, projection_class="semantic")


def test_projection_delta_has_no_truth_fields():
    d = ProjectionDelta.build(operation="upsert", memory_id="m", canonical_path="p", canonical_hash="h",
                              coord=(2, 0, 1), commit_seq=1, projection_class="semantic")
    payload = d.to_dict()
    for forbidden in ("verified", "status", "embeddings", "vector", "score", "projected", "current"):
        assert forbidden not in payload


# ── Task 1: durable outbox ──────────────────────────────────────────────────


def _delta(mid="m-1", h="h1", seq=1, op="upsert"):
    return ProjectionDelta.build(operation=op, memory_id=mid, canonical_path=f"memory/{mid}.json",
                                 canonical_hash=h, coord=(2, 0, 1), commit_seq=seq, projection_class="semantic")


def test_outbox_paths_are_profile_relative_under_indexes(tmp_path):
    assert outbox_path(tmp_path) == tmp_path / "memory" / "indexes" / "projection_delta.jsonl"
    assert projection_dirty_path(tmp_path) == tmp_path / "memory" / "indexes" / "projection_dirty.json"
    # a root that IS the memory dir maps the same way (adapter._memory_root convention)
    assert outbox_path(tmp_path / "memory") == tmp_path / "memory" / "indexes" / "projection_delta.jsonl"


def test_append_survives_reopen_and_returns_true(tmp_path):
    assert append_projection_delta(_delta("m-1", "h1", 1), root=tmp_path) is True
    assert append_projection_delta(_delta("m-2", "h2", 2), root=tmp_path) is True
    deltas, malformed = pending_projection_deltas(root=tmp_path)
    assert [d.memory_id for d in deltas] == ["m-1", "m-2"]
    assert malformed == ()
    # re-open from disk (fresh reader) sees the same, and lines are canonical JSON
    lines = outbox_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["memory_id"] == "m-1"


def test_identical_deltas_may_exist_physically_but_share_delta_id(tmp_path):
    d = _delta("m-1", "h1", 1)
    assert append_projection_delta(d, root=tmp_path)
    assert append_projection_delta(d, root=tmp_path)  # no O(N) dedupe scan at append time
    deltas, _ = pending_projection_deltas(root=tmp_path)
    assert len(deltas) == 2 and deltas[0].delta_id == deltas[1].delta_id == d.delta_id


def test_malformed_line_is_surfaced_not_silently_clean(tmp_path):
    assert append_projection_delta(_delta("m-1", "h1", 1), root=tmp_path)
    p = outbox_path(tmp_path)
    with p.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps({"delta_id": "x", "operation": "upsert"}) + "\n")  # missing fields
    assert append_projection_delta(_delta("m-2", "h2", 2), root=tmp_path)
    deltas, malformed = pending_projection_deltas(root=tmp_path)
    assert [d.memory_id for d in deltas] == ["m-1", "m-2"]
    assert len(malformed) == 2
    assert all("line" in m and "error" in m for m in malformed)


def test_reader_rejects_tampered_delta_id(tmp_path):
    d = _delta("m-1", "h1", 1)
    row = d.to_dict()
    row["canonical_hash"] = "tampered"  # id no longer matches content
    outbox_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    outbox_path(tmp_path).write_text(json.dumps(row) + "\n", encoding="utf-8")
    deltas, malformed = pending_projection_deltas(root=tmp_path)
    assert deltas == () and len(malformed) == 1 and "delta_id" in malformed[0]["error"]


def test_dirty_marker_survives_restart_and_is_loud(tmp_path):
    assert projection_dirty_state(root=tmp_path) == {"dirty": False}
    assert mark_projection_dirty(reason="outbox append failed", memory_id="m-9", root=tmp_path) is True
    state = projection_dirty_state(root=tmp_path)
    assert state["dirty"] is True
    assert state["reason"] == "outbox append failed"
    assert state["memory_id"] == "m-9"
    assert state["schema_version"] == PROJECTION_DIRTY_SCHEMA_VERSION == "omega.projection_dirty.v1"
    # survives a fresh read (restart)
    raw = json.loads(projection_dirty_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["dirty"] is True
    # a second mark keeps it dirty and records the newest reason without losing dirtiness
    assert mark_projection_dirty(reason="second failure", memory_id="m-10", root=tmp_path)
    assert projection_dirty_state(root=tmp_path)["dirty"] is True


def test_roots_do_not_cross_contaminate(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert append_projection_delta(_delta("m-a", "ha", 1), root=a)
    assert mark_projection_dirty(reason="only a", root=a)
    assert pending_projection_deltas(root=b) == ((), ())
    assert projection_dirty_state(root=b) == {"dirty": False}
    assert [d.memory_id for d in pending_projection_deltas(root=a)[0]] == ["m-a"]


def test_append_failure_returns_false_and_never_raises(tmp_path, monkeypatch):
    import memory.projection_delta as pd

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(pd, "_append_line_fsync", _boom)
    assert append_projection_delta(_delta(), root=tmp_path) is False
    assert not outbox_path(tmp_path).exists() or outbox_path(tmp_path).read_text() == ""


def test_native_citizenship_flag_defaults_off_until_boot_profile(monkeypatch):
    monkeypatch.delenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", raising=False)
    assert native_citizenship_enabled() is False
    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    assert native_citizenship_enabled() is True
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", off)
        assert native_citizenship_enabled() is False
