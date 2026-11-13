"""The reflection-window archive is not a memory record (caught live 2026-09-05).

pressure_loop/reflection_window.py preserves whole episode bodies under
memory/600_journal/reflection_windows/. The strict coordinate-index build
("refusing partial coordinate index") walked that archive as records and
failed every boot after the first archived window. Named archive directories
are excluded from record discovery; a genuinely invalid record is still
rejected.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.required_substrate import RequiredSubstrateError
from memory.engine import reindex
from memory.projection.generation import current_view, projection_read_transaction

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_RECORD = (
    ROOT / "benchmarks" / "memory_organ" / "aprime_full_snapshot_468c5771" / "root" / "memory"
    / "000_identity" / "constitution" / "0.00.omega_identity.json"
)


def _omega_root(tmp_path: Path) -> Path:
    """reindex's ``root`` is the OMEGA root; it appends ``memory`` itself."""
    root = tmp_path / "root"
    lobe = root / "memory" / "000_identity" / "constitution"
    lobe.mkdir(parents=True)
    shutil.copy(SNAPSHOT_RECORD, lobe / SNAPSHOT_RECORD.name)
    (root / "memory" / "indexes").mkdir()
    return root


def _archive_a_window(memory_root: Path) -> Path:
    archive = memory_root / "600_journal" / "reflection_windows"
    archive.mkdir(parents=True)
    path = archive / "window_72fee0551828.json"
    path.write_text(json.dumps({"window": {"episodes": []}, "omega_outcome": {"ok": True}}), encoding="utf-8")
    return path


@pytest.mark.skipif(not SNAPSHOT_RECORD.is_file(), reason="A-prime snapshot record missing")
def test_archived_reflection_windows_are_not_walked_as_records(tmp_path: Path):
    root = _omega_root(tmp_path)
    archived = _archive_a_window(root / "memory")

    files = reindex.find_json_files(root=root)
    assert archived not in files
    assert any(p.name == SNAPSHOT_RECORD.name for p in files)

    reindex.build_index(root=root)
    with projection_read_transaction(root):
        view = current_view()
        assert view is not None
        index = json.loads(view.read_bytes("coord_index.snapshot.json"))
    record = json.loads(SNAPSHOT_RECORD.read_text(encoding="utf-8"))
    assert record["id"] in index


@pytest.mark.skipif(not SNAPSHOT_RECORD.is_file(), reason="A-prime snapshot record missing")
def test_a_genuinely_invalid_record_still_refuses_the_index(tmp_path: Path):
    root = _omega_root(tmp_path)
    broken = root / "memory" / "600_journal" / "deepsleep" / "not_a_record.json"
    broken.parent.mkdir(parents=True)
    broken.write_text(json.dumps({"window": {}}), encoding="utf-8")

    with pytest.raises(RequiredSubstrateError, match="canonical_membership_error"):
        reindex.build_index(root=root)


def test_non_record_dirs_are_named_not_inferred():
    assert "reflection_windows" in reindex.NON_RECORD_DIRS
    lobe = Path("memory") / "600_journal"
    assert reindex._is_record_path(lobe / "deepsleep" / "x.json", lobe) is True
    assert reindex._is_record_path(lobe / "reflection_windows" / "window_x.json", lobe) is False
    assert reindex._is_record_path(lobe / "reflection_windows" / "nested" / "y.json", lobe) is False


def test_project_anchors_are_metadata_only_in_the_exact_dev_store():
    lobe = Path("memory") / "1100_dev"
    anchor = lobe / "projects" / "gradient-checks" / "_omega_project.json"
    assert "_omega_project.json" in reindex.NON_RECORD_FILES
    assert reindex._is_record_path(anchor, lobe) is False

    same_name_elsewhere = lobe / "artifacts" / "_omega_project.json"
    assert reindex._is_record_path(same_name_elsewhere, lobe) is True
