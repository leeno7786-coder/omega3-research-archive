"""Gate writes use native projection and never depend on dense embeddings."""

from __future__ import annotations

import json
from pathlib import Path

from tests.memory.native_fixtures import isolated_memory  # noqa: F401 - pytest fixture

# Every gate write here commits a real canonical record, journal line and projection delta.
# Without the hermetic tree they landed in the operator's live memory store (found 2026-09-15).


def _native_gate(monkeypatch, *, forbid_embed=True):
    from memory.gates.base import MemoryGate

    class Gate(MemoryGate):
        READABLE_LOBES = [2]
        WRITABLE_LOBES = [2]
        SOURCE_TAG = "gate:test_native"

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    return Gate()


class _SI:
    def __init__(self):
        self.calls = []

    def register(self, mid, strength):
        self.calls.append((mid, strength))


def test_native_gate_write_is_embedding_free_registers_once_and_queues_projection(isolated_memory, monkeypatch):  # noqa: F811
    from memory.projection_delta import pending_projection_deltas, projection_dirty_state
    from memory.projection_receipt import memory_root_for_canonical_path

    gate = _native_gate(monkeypatch)
    si = _SI()
    monkeypatch.setattr(gate, "_get_strength_index", lambda: si)
    hooks = []
    monkeypatch.setattr(
        "memory.post_commit.run_post_commit_memory_hooks",
        lambda *a, **k: hooks.append((a, k)),
    )

    ids: list = []
    path = gate._write("a thought written natively without any vector", [2, 5, 0], None, None, id_out=ids)
    assert path and ids
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    assert not record.get("embeddings")
    assert len(si.calls) == 1 and si.calls[0][0] == ids[0]
    assert len(hooks) == 1
    root = memory_root_for_canonical_path(path)
    deltas, malformed = pending_projection_deltas(root=root)
    mine = [d for d in deltas if d.memory_id == ids[0]]
    assert len(mine) == 1 and malformed == ()
    assert mine[0].operation == "upsert" and mine[0].projection_class == "semantic"
    assert Path(mine[0].canonical_path).resolve() == Path(path).resolve()
    assert projection_dirty_state(root=root) == {"dirty": False}


def test_native_gate_outbox_failure_keeps_the_write_and_marks_dirty(isolated_memory, monkeypatch):  # noqa: F811
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path

    gate = _native_gate(monkeypatch)
    monkeypatch.setattr(
        pd,
        "_append_line_fsync",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )
    path = gate._write("native write whose outbox append fails", [2, 5, 0], None, None)
    assert path and Path(path).exists()
    root = memory_root_for_canonical_path(path)
    assert pd.projection_dirty_state(root=root)["dirty"] is True


def test_legacy_gate_flag_cannot_restore_dense_path(isolated_memory, monkeypatch):  # noqa: F811
    from memory.projection_delta import pending_projection_deltas
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.delenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", raising=False)
    gate = _native_gate(monkeypatch)
    path = gate._write("legacy flag write", [2, 5, 0], None, None)
    assert path
    deltas, _ = pending_projection_deltas(root=memory_root_for_canonical_path(path))
    assert len(deltas) == 1


def test_identity_gate_writes_are_native_too(isolated_memory, monkeypatch):  # noqa: F811
    from memory.gates.base import MemoryGate
    from memory.projection_delta import pending_projection_deltas
    from memory.projection_receipt import memory_root_for_canonical_path

    class IdGate(MemoryGate):
        READABLE_LOBES = [0]
        WRITABLE_LOBES = [0]
        SOURCE_TAG = "gate:test_identity"

    gate = IdGate()
    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    path = gate._write("an identity-lobe gate write", [0, 0, 0], None, None)
    assert path
    deltas, _ = pending_projection_deltas(root=memory_root_for_canonical_path(path))
    assert len(deltas) == 1
