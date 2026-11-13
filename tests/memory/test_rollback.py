"""
Test memory/rollback.py — canonical write rollback
====================================================

Per W-MEM work order: write integration tests that exercise a rollback
and prove state restoration. Red-green required.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import logging

log = logging.getLogger("omega.tests.memory.test_rollback")


# Module-scoped temp dir so the rollback tests share one OMEGA_ROOT without
# leaking that physical root into later test modules in the same process.
@pytest.fixture(scope="module")
def session_memory_tree():
    """Create a single temp directory for all rollback tests."""
    td = tempfile.mkdtemp()
    root = Path(td)
    mem_root = root / "memory"
    mem_root.mkdir(exist_ok=True)
    indexes_dir = mem_root / "indexes"
    indexes_dir.mkdir(exist_ok=True)
    idx_path = indexes_dir / "coord_index.json"
    idx_path.write_text("{}")

    import core.paths
    import memory.rollback as rollback
    import memory.engine.validate_write as vw

    # Own every imported copy of the physical root for exactly this fixture's
    # lifetime.  Importing the modules before patching also preserves their
    # real values for restoration after this module-scoped fixture finishes.
    with pytest.MonkeyPatch.context() as isolation:
        isolation.setattr(core.paths, "OMEGA_ROOT", root)
        isolation.setattr(rollback, "OMEGA_ROOT", root)
        isolation.setattr(vw, "MEMORY_ROOT", mem_root)
        isolation.setattr(vw, "INDEX_PATH", str(idx_path))

        yield root

    import shutil

    shutil.rmtree(td, ignore_errors=True)


class TestRollbackCanonicalWrite:
    """rollback_canonical_write must reverse atomic_write steps 1+2."""

    def test_rollback_removes_memory_file(self, session_memory_tree):
        """RED: file exists → GREEN: rollback deletes it."""
        from memory.rollback import rollback_canonical_write

        mem_root = session_memory_tree / "memory"
        final_path = mem_root / "test_mem.json"
        memory_dict = {"id": "test-123", "content": "hello"}
        final_path.write_text(json.dumps(memory_dict))
        assert final_path.exists()

        result = rollback_canonical_write(memory_dict, str(final_path))

        assert result["memory_file_removed"] is True
        assert not final_path.exists()
        assert result["errors"] == []

    def test_rollback_removes_index_entry(self, session_memory_tree):
        """RED: index has entry → GREEN: rollback removes it."""
        from memory.rollback import rollback_canonical_write
        from memory.engine.validate_write import patch_index

        mem_root = session_memory_tree / "memory"
        final_path = mem_root / "test_mem.json"
        memory_dict = {"id": "test-456", "content": "world", "coord": [1, 0, 0]}
        final_path.write_text(json.dumps(memory_dict))
        patch_index(memory_dict)

        result = rollback_canonical_write(memory_dict, str(final_path))

        assert result["index_entry_removed"] is True
        assert result["memory_file_removed"] is True
        assert result["errors"] == []

    def test_rollback_idempotent(self, session_memory_tree):
        """Second rollback on same memory is harmless."""
        from memory.rollback import rollback_canonical_write
        from memory.engine.validate_write import patch_index

        mem_root = session_memory_tree / "memory"
        final_path = mem_root / "test_mem.json"
        memory_dict = {"id": "test-789", "content": "!", "coord": [1, 0, 0]}
        final_path.write_text(json.dumps(memory_dict))
        patch_index(memory_dict)

        r1 = rollback_canonical_write(memory_dict, str(final_path))
        assert r1["memory_file_removed"] is True
        assert r1["index_entry_removed"] is True

        r2 = rollback_canonical_write(memory_dict, str(final_path))
        assert r2["memory_file_removed"] is False  # already gone
        assert r2["index_entry_removed"] is False  # already gone
        assert r2["errors"] == []

    def test_rollback_path_traversal_guard(self, session_memory_tree):
        """Rollback must refuse to delete outside memory tree."""
        from memory.rollback import rollback_canonical_write

        memory_dict = {"id": "evil", "content": "x"}
        outside = session_memory_tree / "outside.json"
        outside.write_text("{}")

        result = rollback_canonical_write(memory_dict, str(outside))

        assert result["memory_file_removed"] is False
        assert outside.exists()  # NOT deleted
        assert any("path-traversal" in e for e in result["errors"])

    def test_rollback_reports_unpatch_failure(self, session_memory_tree):
        """If unpatch_index raises, error is captured not propagated."""
        from memory.rollback import rollback_canonical_write

        mem_root = session_memory_tree / "memory"
        final_path = mem_root / "test_mem.json"
        memory_dict = {"id": "", "content": "no-id"}  # empty id may fail unpatch
        final_path.write_text(json.dumps(memory_dict))

        result = rollback_canonical_write(memory_dict, str(final_path))

        # Should not raise; errors captured in result
        assert "errors" in result
        assert result["memory_file_removed"] is True
