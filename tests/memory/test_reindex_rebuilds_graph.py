"""The compatibility reindex entry point delegates to the Program 0 publisher."""

from __future__ import annotations

import json

import pytest

from core.required_substrate import RequiredSubstrateError
from memory.engine import reindex
from memory.projection.dispatch import DispatchReport
from memory.projection.generation import current_view, projection_read_transaction


def test_reindex_and_compile_returns_the_published_graph_snapshot(tmp_path):
    root = tmp_path / "profile"
    (root / "memory" / "indexes").mkdir(parents=True)

    snapshot = reindex.reindex_and_compile(root=root)

    with projection_read_transaction(root):
        view = current_view()
        assert view is not None
        graph = json.loads(view.read_bytes("graph_snapshot.json"))
        assert graph["snapshot_hash"] == snapshot.snapshot_hash
        assert view.manifest.content.graph_snapshot_hash == snapshot.snapshot_hash
        assert json.loads(view.read_bytes("coord_index.snapshot.json")) == {}


def test_reindex_and_compile_forwards_root_to_program0(tmp_path, monkeypatch):
    seen = []

    def fail_dispatch(root=None, *, reason):
        seen.append((root, reason))
        return DispatchReport(
            owner="program0",
            action="failed",
            publication=None,
            drain_status="failed",
            generation_id=None,
            detail={"reason": "test failure"},
        )

    monkeypatch.setattr("memory.projection.dispatch.projection_dispatch", fail_dispatch)
    with pytest.raises(RequiredSubstrateError) as caught:
        reindex.reindex_and_compile(root=tmp_path)

    assert seen == [(tmp_path, "reindex")]
    assert caught.value.component == "memory_projection"
    assert caught.value.operation == "dispatch_execution"
    assert caught.value.reason == "failed"
    assert "test failure" in caught.value.detail


def test_reindex_and_compile_preserves_dispatch_exception(monkeypatch):
    def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("projection dispatcher unavailable")

    monkeypatch.setattr("memory.projection.dispatch.projection_dispatch", fail_dispatch)
    with pytest.raises(RuntimeError, match="projection dispatcher unavailable"):
        reindex.reindex_and_compile()


def test_reindex_and_compile_never_reports_a_failed_dispatch_as_success(monkeypatch):
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda *_args, **_kwargs: DispatchReport(
            owner="program0",
            action="failed",
            publication=None,
            drain_status="coverage_clock_unavailable",
            generation_id=None,
            detail={"reason": "coverage_clock_unavailable"},
        ),
    )

    with pytest.raises(RequiredSubstrateError) as caught:
        reindex.reindex_and_compile()

    assert caught.value.component == "memory_projection"
    assert caught.value.operation == "dispatch_execution"
    assert caught.value.reason == "coverage_clock_unavailable"
