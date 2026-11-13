"""The existing AUTO skill path is executed by the same brain seat as chat."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_loop.execute.brain_skill_executor import run_brain_skill_dispatch


@pytest.fixture(autouse=True)
def _granted_workspace(tmp_path):
    """The skill executor works only inside operator-granted trusted folders: grant this
    test's own folder (in the private per-test store tests/conftest.py provides)."""
    import core.trusted_folders as trusted

    trusted.grant_trusted_folder(tmp_path)
    trusted.set_active_trusted_folder(tmp_path)


def test_plain_text_shortfall_reaches_the_current_brain_result_boundary():
    from auto_loop.execute.brain_skill_executor import _model_summary

    _, _, blockers, _ = _model_summary("UNMET: README documents the CLI - not written yet")

    assert blockers == ["README documents the CLI - not written yet"]


def test_skill_job_stops_after_the_current_tool_when_asked(tmp_path, monkeypatch):
    from auto_loop.execute import job_control as jobs, brain_skill_executor as executor
    original = executor._TOOLS["write_file"]
    def write_and_stop(args, ctx):
        result = original.handler(args, ctx)
        jobs.request_stop("job", source_cycle_id="stop", arrival_seq=2)
        return result
    from dataclasses import replace
    monkeypatch.setitem(executor._TOOLS, "write_file", replace(original, handler=write_and_stop))
    brain = _ToolCallingBrain()
    run_brain_skill_dispatch("directive", _dispatch(), client=brain, workspace_root=tmp_path,
                             cycle_id="origin", job_id="job")
    job = jobs.get_job("job")
    assert job.status == "stopped" and job.tool_steps == 1
    assert job.execution_ceiling.allowed_tools == _dispatch().allowed_tools
    assert len(brain.calls) == 2, "no further model pass after the stop"


class _ToolCallingBrain:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_completion(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if len(self.calls) == 1:
            return {"ok": True, "tool_calls": [{"id": "claim", "function": {
                "name": "start_job", "arguments": '{"project_root":"."}'}}]}
        if len(self.calls) == 2:
            return {
                "ok": True,
                "text": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call-write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"hello.txt","content":"hello from Omega\\n"}',
                        },
                    }
                ],
            }
        return {"ok": True, "text": "Created hello.txt and verified the write.", "finish_reason": "stop"}


def _dispatch(**overrides):
    base = {
        "episode_id": "ep-1",
        "dispatch_id": "dispatch-1",
        "objective": "create hello.txt",
        "execution_route": "development",
        "allowed_tools": (
            "read_files",
            "search_repo",
            "patch_file",
            "edit_file",
            "write_file",
            "run_command",
            "run_tests",
            "inspect_git_diff",
        ),
        "forbidden_tools": (),
        "limits": {"max_steps": 5, "max_duration_ms": 30_000, "max_files_changed": 5},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_brain_uses_native_tool_calls_then_answers_on_the_same_chat_seat(tmp_path):
    from core import pipeline_state
    pipeline_state._write(2, state="INFERENCE", cycle_id="cycle-2", active_episode_id="ep-1")
    brain = _ToolCallingBrain()

    result = run_brain_skill_dispatch(
        "bounded directive",
        _dispatch(),
        client=brain,
        workspace_root=tmp_path,
        pipeline_id=2,
        work_kind="agent_continuation",
        cycle_id="cycle-2",
        job_id="job-2",
    )

    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello from Omega\n"
    assert len(brain.calls) == 3
    assert all(call["pipeline_id"] == 2 for call in brain.calls)
    assert all(call["work_kind"] == "agent_continuation" for call in brain.calls)
    assert all(call["cycle_id"] == "cycle-2" for call in brain.calls)
    assert all(call["job_id"] == "job-2" for call in brain.calls)
    assert brain.calls[0]["tools"]
    assert brain.calls[0]["tool_choice"] == "auto"
    assert brain.calls[2]["messages"][-1]["role"] == "tool"
    assert brain.calls[2]["messages"][-1]["tool_call_id"] == "call-write"
    assert result["ok"] is True
    assert result["summary"] == "Created hello.txt and verified the write."
    assert result["tool_calls"][0]["tool_name"] == "write_file"
    assert result["tool_calls"][0]["status"] == "success"
    assert result["ledger"]["touched_files"] == [
        {"path": str((tmp_path / "hello.txt").resolve()), "tool": "write_file", "status": "success"}
    ]


def test_write_safe_dispatch_keeps_dedicated_test_runner(tmp_path, tmp_path_factory, monkeypatch):
    from tests.helpers.project_runner import put_pytest_on_path

    # The project's own runner answers `pytest`, not whatever the host happens to have.
    put_pytest_on_path(tmp_path_factory.mktemp("project-runner"), monkeypatch)
    (tmp_path / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )

    class _TestBrain:
        def __init__(self):
            self.calls = 0

        def generate_completion(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "ok": True,
                    "tool_calls": [
                        {
                            "id": "claim",
                            "type": "function",
                            "function": {
                                "name": "start_job",
                                "arguments": '{"project_root":"."}',
                            },
                        }
                    ],
                }
            if self.calls == 2:
                return {
                    "ok": True,
                    "tool_calls": [
                        {
                            "id": "call-test",
                            "type": "function",
                            "function": {
                                "name": "run_tests",
                                "arguments": '{"command":"pytest -q test_smoke.py"}',
                            },
                        }
                    ],
                }
            return {"ok": True, "text": "The test passed."}

    result = run_brain_skill_dispatch(
        "bounded directive",
        _dispatch(
            safety={"safety_class": "write_safe"},
            allowed_tools=("read_files", "write_file", "run_command", "run_tests"),
        ),
        client=_TestBrain(),
        workspace_root=tmp_path,
    )

    test_call = next(call for call in result["tool_calls"] if call["tool_name"] == "run_tests")
    assert test_call["status"] == "success"
    assert test_call["result"]["exit_code"] == 0


def test_run_tests_rejects_shell_composition(tmp_path):
    from auto_loop.execute.brain_skill_executor import ExecutionContext, _run_tests

    result = _run_tests(
        {"command": "echo pytest; pytest -q"},
        ExecutionContext(tmp_path, "test", 30.0),
    )

    assert result.ok is False
    assert "recognized test runner" in result.error


def test_brain_executor_refuses_workspace_escape_and_reports_real_failure(tmp_path):
    class _EscapingBrain:
        def __init__(self):
            self.calls = 0

        def generate_completion(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "ok": True,
                    "text": "",
                    "tool_calls": [
                        {
                            "id": "call-escape",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path":"../escape.txt","content":"no"}',
                            },
                        }
                    ],
                }
            return {"ok": True, "text": "The write was refused: it is outside the trusted folders."}

    result = run_brain_skill_dispatch(
        "bounded directive",
        _dispatch(),
        client=_EscapingBrain(),
        workspace_root=tmp_path,
    )

    call = result["tool_calls"][0]
    assert call["status"] == "failure"
    assert "outside trusted folders" in call["stderr"]
    assert not (tmp_path.parent / "escape.txt").exists()


def test_last_pass_disables_tools_so_the_loop_always_terminates(tmp_path):
    class _PersistentBrain:
        def __init__(self):
            self.calls: list[dict] = []

        def generate_completion(self, prompt, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("tools"):
                return {
                    "ok": True,
                    "text": "",
                    "tool_calls": [
                        {
                            "id": "call-read",
                            "type": "function",
                            "function": {
                                "name": "read_files",
                                "arguments": '{"path":"missing.txt"}',
                            },
                        }
                    ],
                }
            return {"ok": True, "text": "Stopped at the bounded step limit."}

    brain = _PersistentBrain()
    result = run_brain_skill_dispatch(
        "bounded directive",
        _dispatch(limits={"max_steps": 1, "max_duration_ms": 30_000}),
        client=brain,
        workspace_root=tmp_path,
    )

    assert len(brain.calls) == 2
    assert brain.calls[0]["tools"]
    assert brain.calls[1]["tools"] is None
    assert result["summary"] == "Stopped at the bounded step limit."
    assert result["rounds_used"] == 2


def test_operator_interrupt_stops_before_the_next_side_effect(tmp_path):
    stopped = {"value": False}

    class _InterruptingBrain:
        def generate_completion(self, prompt, **kwargs):
            stopped["value"] = True
            return {
                "ok": True,
                "text": "",
                "tool_calls": [
                    {
                        "id": "call-after-stop",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"must-not-exist.txt","content":"no"}',
                        },
                    }
                ],
            }

    result = run_brain_skill_dispatch(
        "bounded directive",
        _dispatch(),
        client=_InterruptingBrain(),
        workspace_root=tmp_path,
        should_stop=lambda: stopped["value"],
    )

    assert result["status"] == "partial"
    assert "operator" in result["summary"].lower()
    assert not (tmp_path / "must-not-exist.txt").exists()


def test_write_safe_skill_does_not_expose_shell_tools_in_allow_edits(tmp_path):
    class _AdversarialBrain:
        def __init__(self):
            self.calls = []

        def generate_completion(self, prompt, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {
                    "ok": True,
                    "tool_calls": [
                        {
                            "id": "shell",
                            "function": {
                                "name": "run_command",
                                "arguments": '{"command":"git status"}',
                            },
                        }
                    ],
                }
            return {"ok": True, "text": "blocked"}

    dispatch = _dispatch(
        execution_route="development",
        safety={"safety_class": "write_safe"},
        allowed_tools=("read_files", "write_file", "run_command", "run_tests"),
    )
    brain = _AdversarialBrain()
    result = run_brain_skill_dispatch(
        "bounded directive",
        dispatch,
        client=brain,
        workspace_root=tmp_path,
        permission_mode="allow_edits",
    )

    assert not any(call["function"]["name"] == "run_command" for call in brain.calls[0]["tools"])
    assert result["tool_calls"][0]["status"] == "failure"
    assert "outside this dispatch" in result["tool_calls"][0]["stderr"]


def test_read_only_skill_does_not_expose_or_execute_start_job(tmp_path):
    class _AdversarialBrain:
        def __init__(self):
            self.calls = []

        def generate_completion(self, prompt, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {
                    "ok": True,
                    "tool_calls": [
                        {
                            "id": "start",
                            "function": {
                                "name": "start_job",
                                "arguments": '{"project_root":"."}',
                            },
                        }
                    ],
                }
            return {"ok": True, "text": "read-only"}

    dispatch = _dispatch(
        execution_route="read_only",
        safety={"safety_class": "read_only"},
        allowed_tools=("read_files",),
    )
    brain = _AdversarialBrain()
    result = run_brain_skill_dispatch(
        "bounded directive",
        dispatch,
        client=brain,
        workspace_root=tmp_path,
        permission_mode="read_only",
    )

    assert not any(call["function"]["name"] == "start_job" for call in brain.calls[0]["tools"])
    assert result["status"] == "success"
