"""Change 4a·1: rollback snapshots carry project+relpath so the rollback endpoint
can locate the lobe-11 store file to re-mirror."""

from __future__ import annotations

from core.dev_rollback_capture import record_file_snapshots
from memory.dev_project_store import FileChange


def test_snapshot_carries_project_and_relpath(tmp_path):
    ch = FileChange(
        file_path=str(tmp_path / "a.py"),
        project="proj",
        relpath="a.py",
        before_content="old",
        after_content="new",
        created=False,
    )
    recs = record_file_snapshots([ch], objective="proj", session_id="s", artifacts_dir=tmp_path)
    assert recs and recs[0]["project"] == "proj" and recs[0]["relpath"] == "a.py"
