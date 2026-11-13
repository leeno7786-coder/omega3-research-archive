"""The operator's stop reaches the work, and project commands use the machine's toolchain."""
from __future__ import annotations


def test_project_commands_use_the_machines_toolchain(monkeypatch):
    """Omega's portable interpreter is embeddable: the working directory is not on its sys.path
    and its pip.exe exits 1 printing nothing, so a project's own module never imported and every
    install failed silently (live 2026-09-15, a whole build spent on it)."""
    from auto_loop.execute.brain_skill_executor import _portable_subprocess_env
    from core.paths import OMEGA_ROOT

    host_path = "C:\\Python312;C:\\Python312\\Scripts"
    monkeypatch.setenv("PATH", host_path)
    monkeypatch.setenv("PYTHONHOME", "J:\\omega-embedded")
    monkeypatch.setenv("PYTHONPATH", "J:\\omega-embedded\\lib")
    monkeypatch.setenv("VIRTUAL_ENV", "J:\\omega-embedded")

    env = _portable_subprocess_env()

    assert env["PATH"] == host_path, "the machine's PATH answers, unprefixed"
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        assert name not in env, f"{name} would drag Omega's interpreter into the host python"
    assert env["OMEGA_ROOT"] == str(OMEGA_ROOT)


def test_run_tests_runs_the_projects_own_runner(monkeypatch, tmp_path):
    from auto_loop.execute import brain_skill_executor as executor

    seen: dict[str, object] = {}

    def fake_run_command(args, ctx):
        seen.update(args)
        return executor.ToolResult(True, output="ran")

    monkeypatch.setattr(executor, "_run_command", fake_run_command)
    ctx = executor.ExecutionContext(tmp_path, "test it", 10, trusted_roots=(tmp_path,))

    assert executor._run_tests({"command": "pytest -q"}, ctx).ok
    assert seen["command"] == "pytest -q", "the project's runner, not Omega's interpreter"


def test_only_the_agent_lane_holds_job_control():
    """Spec section 11 (T12): a stop is resolved by ambiguity, not by lane. Chat held
    ``stop_job`` from 2026-09-15 because a plain "stop" spent a model turn looking for a
    tool — ingress answers that deterministically instead (the halt test below), so the
    capability goes back to the lane that owns it. Boundaries in
    tests/pressure_loop/test_stop_doctrine.py."""
    from auto_loop.execute.brain_skill_executor import lane_tool_names

    assert "stop_job" not in lane_tool_names("chat")
    assert "stop_job" in lane_tool_names("agent")


def test_halt_stops_the_running_job_and_interrupts_the_batch_in_flight(tmp_path):
    from auto_loop.execute import job_control as jobs
    from pressure_loop.operator_chat import halt_operator_work
    from pressure_loop.operator_chat_responses import work_halt_requested

    project = tmp_path / "todo_app"
    project.mkdir(parents=True)
    jobs.open_job("build", "bubble-1", "build a todo app", tmp_path, project_root=project)

    halted = halt_operator_work("bubble-2")

    assert halted["stopped_jobs"] == ["build"]
    assert work_halt_requested("bubble-1"), "the batch in flight polls its own turn's stop"
    assert any(row.kind == "stop" for row in jobs.get_job("build").revisions)


def test_the_agent_batch_polls_the_work_halt_not_the_playback_interrupt():
    """Cancelling speech must never stop cognition, so the work has its own signal."""
    from auto_loop.execute.run import _operator_turn_should_stop
    from pressure_loop.operator_chat_responses import (
        clear_work_halt,
        request_interrupt,
        request_work_halt,
    )

    stopped = _operator_turn_should_stop("bubble-9")
    assert stopped is not None and stopped() is False

    request_interrupt("bubble-9")
    assert stopped() is False, "a playback interrupt leaves the work running"

    request_work_halt("bubble-9")
    try:
        assert stopped() is True
    finally:
        clear_work_halt("bubble-9")
    assert _operator_turn_should_stop("") is None
