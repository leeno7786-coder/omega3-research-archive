from __future__ import annotations

from types import SimpleNamespace

from auto_loop.skill.run import _build_skill_event_handler
from memory import episode_sram


def test_builder_ledger_event_holds_file_snapshot_while_build_is_running(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: tmp_path)
    source = tmp_path / "src" / "widget.py"
    source.parent.mkdir()
    source.write_text("VALUE = 42\n", encoding="utf-8")
    episode_sram.open_bucket("ep-artifact", pipeline_id=0)
    dispatch = SimpleNamespace(
        episode_id="ep-artifact",
        objective="Build the widget",
    )

    handler = _build_skill_event_handler(dispatch, narrator=None)
    handler(
        {
            "type": "ledger",
            "data": {"touched_files": [{"path": "src/widget.py"}]},
        }
    )

    memories = [item.envelope for item in episode_sram.pending() if item.kind == "memory"]
    assert len(memories) == 1
    envelope = memories[0]
    assert envelope.type == "dev_artifact_snapshot"
    assert envelope.coord == [11, 0, 0]
    assert envelope.payload["relative_path"] == "src/widget.py"
    assert envelope.payload["scope"] == "operator_workspace"
    assert envelope.payload["content"] == source.read_bytes().decode("utf-8")
    assert envelope.payload["workspace_root"] == str(tmp_path.resolve())
    assert envelope.payload["enrichment"]["workspace_root"] == str(tmp_path.resolve())
