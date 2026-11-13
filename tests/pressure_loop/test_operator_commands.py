from __future__ import annotations


def test_help_is_painted_deterministically_without_model_or_queue():
    from pressure_loop.operator_chat import _screen_and_enqueue_operator_chat

    result = _screen_and_enqueue_operator_chat("/help", cycle_id="help-cycle")

    assert result["ok"] is True
    assert result["queued"] is False
    assert result["delivery"]["rendered_by"] == "operator_command"
    assert "/trust <directory>" in result["message"]
    assert "/cd <directory>" in result["message"]


def test_workspace_commands_are_exposed_in_skill_picker_catalog():
    from hud.qwen_skills import built_in_commands

    names = {item["name"] for item in built_in_commands()}
    assert {"/trust", "/cd", "/trusted", "/untrust"}.issubset(names)


def test_explicit_stop_control_halts_the_build(tmp_path, monkeypatch):
    """The explicit stop control halts work; natural-language "stop" stays a chat turn."""
    from auto_loop.execute import job_control as jobs
    from pressure_loop.operator_chat import halt_operator_work

    monkeypatch.setattr(jobs, "jobs_root", lambda: tmp_path / "jobs")
    jobs.reset_for_tests()
    project = tmp_path / "todo_app"
    project.mkdir(parents=True)
    jobs.open_job("running-build", "bubble-1", "build a todo app", tmp_path, project_root=project)

    result = halt_operator_work("bubble-1")

    assert "running-build" in result["stopped_jobs"]
    assert any(row.kind == "stop" for row in jobs.get_job("running-build").revisions)
