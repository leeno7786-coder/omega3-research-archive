"""Program 0 settles rebuild markers and outbox bytes only at publication."""

from __future__ import annotations

import pytest

import memory.engine.reindex as reindex
from memory.projection.generation import projection_root
from memory.projection.projector import read_cursor, read_malformed_receipts
from memory.projection_delta import (
    mark_projection_dirty,
    outbox_path,
    pending_projection_deltas,
    projection_dirty_state,
)


@pytest.fixture
def rebuild_root(tmp_path):
    root = tmp_path / "memory"
    (root / "indexes").mkdir(parents=True)
    return root


def test_full_rebuild_clears_the_marker_it_satisfied(rebuild_root):
    assert mark_projection_dirty(
        reason="entity-link hook mutated canonical peer files",
        memory_id="1.00.x",
        root=rebuild_root,
    )
    assert projection_dirty_state(root=rebuild_root)["dirty"] is True

    snapshot = reindex.reindex_and_compile(root=rebuild_root)

    assert snapshot.snapshot_hash
    assert projection_dirty_state(root=rebuild_root)["dirty"] is False


def test_no_marker_means_nothing_to_clear(rebuild_root):
    snapshot = reindex.reindex_and_compile(root=rebuild_root)

    assert snapshot.snapshot_hash
    assert projection_dirty_state(root=rebuild_root)["dirty"] is False


def test_a_marker_rewritten_during_the_rebuild_survives(rebuild_root, monkeypatch):
    mark_projection_dirty(reason="standing before the rebuild", memory_id="1.00.a", root=rebuild_root)
    from memory.taxonomy import compiler

    compile_real = compiler.compile_memory_graph

    def _compile_then_mark(*, source_root, output_dir):
        mark_projection_dirty(
            reason="marked during the rebuild",
            memory_id="1.00.b",
            root=rebuild_root,
        )
        return compile_real(source_root=source_root, output_dir=output_dir)

    monkeypatch.setattr(compiler, "compile_memory_graph", _compile_then_mark)
    reindex.reindex_and_compile(root=rebuild_root)

    state = projection_dirty_state(root=rebuild_root)
    assert state["dirty"] is True and state["reason"] == "marked during the rebuild"


def test_a_failed_rebuild_keeps_the_marker(rebuild_root, monkeypatch):
    mark_projection_dirty(reason="standing", memory_id="1.00.c", root=rebuild_root)
    from memory.taxonomy import compiler

    def _boom(*, source_root, output_dir):
        raise RuntimeError("graph compile exploded")

    monkeypatch.setattr(compiler, "compile_memory_graph", _boom)
    with pytest.raises(RuntimeError, match="graph compile exploded"):
        reindex.reindex_and_compile(root=rebuild_root)

    assert projection_dirty_state(root=rebuild_root)["dirty"] is True


def test_full_rebuild_settles_outbox_without_truncating_its_evidence(rebuild_root):
    outbox = outbox_path(rebuild_root)
    outbox.parent.mkdir(parents=True, exist_ok=True)
    raw = b'{"not": "a delta"}\n' * 3
    outbox.write_bytes(raw)

    reindex.reindex_and_compile(root=rebuild_root)

    cursor = read_cursor(projection_root(rebuild_root))
    assert cursor is not None
    assert cursor.outbox_byte_offset == outbox.stat().st_size
    assert outbox.read_bytes().startswith(raw)
    assert pending_projection_deltas(root=rebuild_root)[0] == ()
    assert len(read_malformed_receipts(projection_root(rebuild_root))) == 3


def test_a_failed_rebuild_keeps_the_outbox(rebuild_root, monkeypatch):
    outbox = outbox_path(rebuild_root)
    outbox.parent.mkdir(parents=True, exist_ok=True)
    raw = b'{"not": "a delta"}\n'
    outbox.write_bytes(raw)
    from memory.taxonomy import compiler

    def _boom(*, source_root, output_dir):
        raise RuntimeError("graph compile exploded")

    monkeypatch.setattr(compiler, "compile_memory_graph", _boom)
    with pytest.raises(RuntimeError, match="graph compile exploded"):
        reindex.reindex_and_compile(root=rebuild_root)

    assert outbox.read_bytes() == raw
    cursor = read_cursor(projection_root(rebuild_root))
    assert cursor is None or cursor.outbox_byte_offset == 0
