"""Builder project history is sourced from SRAM and verified canonical snapshots."""

from pathlib import Path

import pytest

from memory import episode_sram
from memory.dev_artifact_intake import hold_dev_artifact_snapshots
from memory.dev_project_store import (
    _drop_vendored,
    derive_project_name,
    persist_project_files,
    project_artifact_paths,
    restore_project_files,
    sanitize_project,
)


def _capture(episode_id: str, objective: str, path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    episode_sram.open_bucket(episode_id, pipeline_id=0)
    hold_dev_artifact_snapshots(
        episode_id=episode_id,
        objective=objective,
        paths=[str(path)],
    )


def test_capture_pairs_episode_sram_with_prior_verified_snapshot_without_mirror(
    tmp_path, monkeypatch, deepsleep_sram_drain
):
    from memory import dev_project_store

    workspace = tmp_path / "workspace"
    source = workspace / "src" / "widget.py"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(workspace))
    isolated_omega = tmp_path / "omega"
    monkeypatch.setattr(dev_project_store, "OMEGA_ROOT", isolated_omega)
    objective = "Build the widget"

    _capture("widget-1", objective, source, "VALUE = 1\n")
    first = persist_project_files(
        [str(source)], objective=objective, episode_id="widget-1"
    )
    assert len(first) == 1
    assert first[0].before_content is None
    assert first[0].after_content == "VALUE = 1\n"
    assert episode_sram.flush("widget-1")["memories"]

    source.write_bytes(b"VALUE = 2\n")
    episode_sram.open_bucket("widget-2", pipeline_id=0)
    hold_dev_artifact_snapshots(
        episode_id="widget-2", objective=objective, paths=[str(source)]
    )
    second = persist_project_files(
        [str(source)], objective=objective, episode_id="widget-2"
    )
    assert len(second) == 1
    assert second[0].before_content == "VALUE = 1\n"
    # The source changed after intake; after_content still comes from its held SRAM payload.
    source.write_bytes(b"UNSNAPSHOTTED = True\n")
    assert second[0].after_content == "VALUE = 2\n"

    assert not (isolated_omega / "memory" / "1100_dev" / "projects").exists()
    assert episode_sram.flush("widget-2")["memories"]
    assert project_artifact_paths(objective) == ["src/widget.py"]
    assert not (isolated_omega / "memory" / "1100_dev" / "projects").exists()


def test_rehydrate_uses_later_verified_snapshot_after_workspace_clear(
    tmp_path, monkeypatch, deepsleep_sram_drain
):
    from memory import dev_project_store

    workspace = tmp_path / "workspace"
    source = workspace / "app" / "main.py"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(workspace))
    isolated_omega = tmp_path / "omega"
    monkeypatch.setattr(dev_project_store, "OMEGA_ROOT", isolated_omega)
    objective = "Todo app"

    _capture("todo-1", objective, source, "VERSION = 'first'\n")
    assert episode_sram.flush("todo-1")["memories"]
    source.write_bytes(b"VERSION = 'second'\n")
    episode_sram.open_bucket("todo-2", pipeline_id=0)
    hold_dev_artifact_snapshots(
        episode_id="todo-2", objective=objective, paths=[str(source)]
    )
    assert episode_sram.flush("todo-2")["memories"]

    source.unlink()
    assert [relative for relative, _ in restore_project_files(objective)] == ["app/main.py"]
    assert source.read_text(encoding="utf-8") == "VERSION = 'second'\n"
    assert not (isolated_omega / "memory" / "1100_dev" / "projects").exists()


def test_project_naming_and_vendored_filter_remain_stable():
    assert derive_project_name(["todo-app/main.py", "todo-app/tests/t.py"], "") == "todo-app"
    assert derive_project_name(["a/x.py", "b/y.py"], "Build A Todo App") == "build-a-todo-app"
    assert sanitize_project("My Cool App!!") == "my-cool-app"

    targets = [
        (Path("src/main.py"), "src/main.py"),
        (Path("colorama/ansi.py"), "colorama/ansi.py"),
        (Path("colorama-0.4.6.dist-info/RECORD"), "colorama-0.4.6.dist-info/RECORD"),
    ]
    assert [rel for _path, rel in _drop_vendored(targets)] == ["src/main.py"]


@pytest.mark.parametrize(
    ("coord", "record", "resolved_path", "message"),
    [
        ([10, 1, 1], {"id": "snapshot-1"}, "record", "invalid coordinate"),
        ([11, 0, 1], {"id": "snapshot-1"}, None, "no resolvable canonical path"),
        ([11, 0, 1], {"id": "snapshot-1"}, "record", "missing canonical metadata"),
        (
            [11, 0, 1],
            {
                "id": "snapshot-1",
                "enrichment": {"objective": "Build the widget", "scope": "system"},
            },
            "record",
            "unsupported scope",
        ),
    ],
)
def test_indexed_dev_artifact_corruption_fails_loudly(
    tmp_path, monkeypatch, coord, record, resolved_path, message
):
    import json

    from memory import dev_project_store
    from memory.engine import walker

    record_path = tmp_path / "snapshot.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(walker, "load_coord_index", lambda: None)
    monkeypatch.setattr(
        walker,
        "coord_index_snapshot",
        lambda: {
            "snapshot-1": {
                "type": "dev_artifact_snapshot",
                "coord": coord,
                "path": "memory/snapshot.json",
            }
        },
    )
    monkeypatch.setattr(
        walker,
        "resolve_coord_index_path",
        lambda _path: record_path if resolved_path == "record" else None,
    )

    with pytest.raises(ValueError, match=message):
        dev_project_store._canonical_snapshot_records("build-the-widget")
