"""Prompt construction is write-free. Restoring a project's files is mutable work, so it
runs only through the restore_project_from_memory tool, after start_job(project_root)
selects the project (AGENTS.md §7, project ownership)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import schema.qwen_skill_dispatch as qsd


def _dispatch(objective: str) -> SimpleNamespace:
    return SimpleNamespace(
        episode_id="ep",
        dispatch_id="d",
        skill_name="s",
        skill_class="c",
        execution_kind="auto",
        objective=objective,
        skill_markdown="# skill",
        evidence_required=(),
        completion_criteria=(),
        motor_program={},
        allowed_tools=(),
        forbidden_tools=(),
        safety={},
        limits={},
        expected_output_schema={},
    )


def _forbid_restore(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("prompt construction must not restore files")

    monkeypatch.setattr("memory.dev_project_store.restore_project_files", forbidden)


def test_build_directive_never_restores_project_files(monkeypatch):
    """No project is selected while the prompt is built, so a restore here could
    write into whichever workspace an earlier job recorded."""
    _forbid_restore(monkeypatch)
    directive = qsd.build_directive(_dispatch("build a todo app"))
    assert "## Objective\nbuild a todo app" in directive


def test_agent_prompt_accepts_legitimate_absence_of_artifact_history(monkeypatch):
    monkeypatch.setattr("memory.dev_project_store._canonical_snapshot_records", lambda _project: {})
    directive = qsd.build_directive(_dispatch("Start a new project"))
    assert "## Objective\nStart a new project" in directive


def test_read_only_prompt_does_not_attempt_artifact_restore(monkeypatch):
    _forbid_restore(monkeypatch)
    dispatch = _dispatch("Inspect the project")
    dispatch.execution_route = "read_only"
    assert "## Objective\nInspect the project" in qsd.build_directive(dispatch)
