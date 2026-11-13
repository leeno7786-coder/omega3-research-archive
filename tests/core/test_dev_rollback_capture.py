"""Autonomous fold-back -> dev-lobe rollback sandbox capture (content-driven)."""

import json

from core.dev_rollback_capture import record_file_snapshots
from memory.dev_project_store import FileChange


def _change(path, *, before, after, created):
    return FileChange(
        file_path=path,
        project="todo-app",
        relpath=path.split("/")[-1],
        before_content=before,
        after_content=after,
        created=created,
    )


def test_created_file_records_a_deletable_snapshot(tmp_path):
    artifacts = tmp_path / "artifacts"
    written = record_file_snapshots(
        [_change("C:/ws/todo.py", before=None, after="print('hi')", created=True)],
        objective="build a todo app",
        session_id="ep-42",
        artifacts_dir=artifacts,
    )
    assert len(written) == 1
    rec = written[0]
    assert rec["existed_before"] is False  # -> HUD renders "delete created file"
    assert rec["before_content"] is None
    assert rec["after_content"] == "print('hi')"
    assert rec["tool"] == "write_file"
    on_disk = json.loads(next(artifacts.glob("*.json")).read_text(encoding="utf-8"))
    assert on_disk["session_id"] == "ep-42"


def test_edited_file_records_a_restore_snapshot_with_before(tmp_path):
    artifacts = tmp_path / "artifacts"
    written = record_file_snapshots(
        [_change("C:/ws/app.py", before="v1", after="v2", created=False)],
        session_id="ep-9",
        artifacts_dir=artifacts,
    )
    assert len(written) == 1
    rec = written[0]
    assert rec["existed_before"] is True  # -> HUD renders "restore previous content"
    assert rec["before_content"] == "v1"  # rolls back to the prior persisted version
    assert rec["after_content"] == "v2"
    assert rec["tool"] == "edit_file"


def test_edit_without_before_is_skipped_not_a_destructive_delete(tmp_path):
    artifacts = tmp_path / "artifacts"
    # created=False but no before-content -> recording existed_before=False would DELETE on
    # rollback; the recorder must skip it rather than risk destroying operator work.
    written = record_file_snapshots(
        [_change("C:/ws/x.py", before=None, after="v2", created=False)],
        artifacts_dir=artifacts,
    )
    assert written == []


def test_reyield_refreshes_one_artifact_per_file(tmp_path):
    artifacts = tmp_path / "artifacts"
    record_file_snapshots(
        [_change("C:/ws/app.py", before=None, after="v1", created=True)],
        session_id="ep-1",
        artifacts_dir=artifacts,
    )
    record_file_snapshots(
        [_change("C:/ws/app.py", before="v1", after="v2", created=False)],
        session_id="ep-1",
        artifacts_dir=artifacts,
    )
    files = list(artifacts.glob("*.json"))
    assert len(files) == 1  # deterministic (session, path) id
    assert json.loads(files[0].read_text(encoding="utf-8"))["after_content"] == "v2"


def test_long_shared_path_prefixes_do_not_overwrite_each_other(tmp_path):
    artifacts = tmp_path / "artifacts"
    prefix = "C:/" + "/".join(["very-long-workspace-segment"] * 6)

    written = record_file_snapshots(
        [
            _change(f"{prefix}/src/stats.py", before=None, after="source", created=True),
            _change(f"{prefix}/tests/test_stats.py", before=None, after="test", created=True),
        ],
        session_id="ep-long",
        artifacts_dir=artifacts,
    )

    assert len(written) == 2
    assert len(list(artifacts.glob("*.json"))) == 2
    assert len({record["id"] for record in written}) == 2


def test_no_changes_is_a_noop(tmp_path):
    assert record_file_snapshots([], artifacts_dir=tmp_path / "a") == []
