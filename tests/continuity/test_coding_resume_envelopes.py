"""A cold coding prompt carries exact current evidence or the original failure."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import schema.qwen_skill_dispatch as qsd
from memory.execution_report import ExecutionReport
from organ.promptorgan.packs.agent.pack import AgentPack
from tests.schema.test_qwen_skill_dispatch_rehydrate import _dispatch


def _report(*paths):
    return ExecutionReport(
        episode_id="resume-evidence", build_task="Continue the project",
        pass_gate=("The requested change passes verification",),
        did=(), did_not=(), succeeded=(), failed=(), files_changed=paths,
    )


def _prompt(monkeypatch):
    return AgentPack().build(SimpleNamespace(
        kind="agent", data=_dispatch("Continue the project"), question="",
    ))


@pytest.mark.parametrize("boundary", ["report", "completed_recall", "workspace", "file_read"])
def test_cold_prompt_propagates_unavailable_evidence(monkeypatch, tmp_path, boundary):
    fault = OSError(f"resume-evidence-SENTINEL: {boundary} unavailable")

    def unavailable(*_args, **_kwargs):
        raise fault

    source = tmp_path / "app.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: tmp_path)
    monkeypatch.setattr("memory.execution_report.current_execution_report", lambda: None)
    if boundary == "report":
        monkeypatch.setattr("memory.execution_report.current_execution_report", unavailable)
    elif boundary == "completed_recall":
        monkeypatch.setattr("memory.taxonomy.config.taxonomy_enabled", lambda: True)
        monkeypatch.setattr("memory.taxonomy.meta_recall.recall_skill_context", unavailable)
    else:
        monkeypatch.setattr("memory.execution_report.current_execution_report", lambda: _report("app.py"))
        if boundary == "workspace":
            monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", unavailable)
        else:
            original_read = Path.read_text

            def fail_source(path, *args, **kwargs):
                if path == source:
                    raise fault
                return original_read(path, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", fail_source)
    with pytest.raises(OSError) as caught:
        _prompt(monkeypatch)
    assert caught.value is fault


def test_missing_exact_resume_path_never_reads_another_project_by_basename(monkeypatch, tmp_path):
    unrelated = tmp_path / "other-project"
    unrelated.mkdir()
    (unrelated / "app.py").write_text("UNRELATED_PROJECT_SENTINEL\n", encoding="utf-8")
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: tmp_path)

    state = qsd._workspace_file_state(["app.py"])

    assert "UNRELATED_PROJECT_SENTINEL" not in state
    assert "app.py" in state and "file missing" in state


def test_absolute_resume_paths_keep_each_workspace_identity(monkeypatch, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    sources = [first / "app.py", second / "app.py"]
    for index, path in enumerate(sources):
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: None)
    # Both recorded workspaces are still granted trusted folders.
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_roots", lambda: (first, second))

    state = qsd._workspace_file_state([str(path) for path in sources])

    normalized_state = os.path.normcase(state)
    assert all(os.path.normcase(str(path)) in normalized_state for path in sources)
    assert "VALUE = 0" in state and "VALUE = 1" in state


def test_relative_resume_paths_require_known_workspace(monkeypatch):
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: None)
    with pytest.raises(ValueError, match="workspace"):
        qsd._workspace_file_state(["app.py"])
    assert qsd._workspace_file_state([]) == ""
