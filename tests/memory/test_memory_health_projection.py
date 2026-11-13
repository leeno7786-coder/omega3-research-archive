"""Cut 4 — memory_health exposes projection backlog honestly.

Health must distinguish clean+no-pending, clean+pending, and dirty/rebuild-
required — and must never claim projection currentness (Cut 13 owns
generation identity). The compatibility-named embedding health field describes
the resident Ettin retina; it does not gate native write health.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def quiet_health(monkeypatch):
    """Keep the probe hermetic: no encoder load and no integrity scan."""
    from memory import api

    monkeypatch.setattr(api, "_probe_embedding_substrate_if_unknown", lambda: None)
    monkeypatch.setattr("memory.integrity.audit.run_integrity_audit_if_due", lambda: type("A", (), {"issues": []})())
    return api


def _delta(mid):
    from memory.projection_delta import ProjectionDelta

    return ProjectionDelta.build(operation="upsert", memory_id=mid, canonical_path=f"memory/{mid}.json",
                                 canonical_hash="h", coord=(2, 0, 1), commit_seq=1, projection_class="semantic")


def test_health_clean_no_pending(quiet_health, monkeypatch):
    monkeypatch.delenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", raising=False)
    h = quiet_health.memory_health()
    assert h.projection_pending_count == 0
    assert h.projection_dirty is False
    assert h.projection_dirty_reason == ""
    assert h.native_citizenship_enabled is False
    # Cut 13 owns generation identity: the field now EXISTS and a legacy (no-manifest) profile is
    # never "current" (revised from the pre-Cut-13 placeholder `not hasattr(...)`, 2026-08-17).
    assert h.projection_current is False and h.projection_generation == "legacy"
    assert not any("projection" in i for i in h.issues)


def test_health_clean_with_pending_backlog_is_named_not_hidden(quiet_health, monkeypatch):
    from memory.projection_delta import append_projection_delta

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    assert append_projection_delta(_delta("m-1")) and append_projection_delta(_delta("m-2"))
    h = quiet_health.memory_health()
    assert h.projection_pending_count == 2
    assert h.projection_dirty is False
    assert h.native_citizenship_enabled is True
    assert any("projection backlog" in i for i in h.issues)  # lag is legal but must be named


def test_health_dirty_rebuild_required_is_loud(quiet_health, monkeypatch):
    from memory.projection_delta import mark_projection_dirty

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    assert mark_projection_dirty(reason="projection outbox append failed after canonical commit", memory_id="m-9")
    h = quiet_health.memory_health()
    assert h.projection_dirty is True
    assert "append failed" in h.projection_dirty_reason
    assert any("rebuild required" in i for i in h.issues)


def test_health_malformed_outbox_lines_are_surfaced(quiet_health, monkeypatch):
    from memory.projection_delta import append_projection_delta, outbox_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    assert append_projection_delta(_delta("m-1"))
    with outbox_path().open("a", encoding="utf-8") as fh:
        fh.write("{broken\n")
    h = quiet_health.memory_health()
    assert h.projection_pending_count == 1
    assert h.projection_malformed_count == 1
    assert any("malformed" in i for i in h.issues)


def test_embedding_substrate_does_not_gate_native_write_health(quiet_health, monkeypatch):
    """A degraded Ettin seat is reported, independently of projection health."""
    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    monkeypatch.setattr(quiet_health, "_observed_embedding_substrate", lambda: ("degraded", "worker unreachable"))
    h = quiet_health.memory_health()
    assert h.embedding_substrate == "degraded"
    assert h.projection_dirty is False and h.projection_pending_count == 0
