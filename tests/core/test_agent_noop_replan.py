"""Agent-routed work may not complete as a text-only promise."""

import json

from auto_loop.execute.run import _operator_tools_exchange


def _call(name, args, call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_agent_replans_once_when_first_pass_promises_work_without_tools(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(workspace))
    passes = []

    def generate(**kwargs):
        passes.append(kwargs)
        if len(passes) == 1:
            return {"text": "I'll create the todo app files now.", "tool_calls": []}
        return {
            "text": "Creating the files now.",
            "tool_calls": [
                _call("start_job", {"project_root": "."}, "claim"),
                _call("task_plan", {"steps": ["create the todo app files"]}, "plan"),
                _call("write_file", {"path": "app.py", "content": "todos = []\n"}, "write"),
            ],
        }
    result, records, stop_reason = _operator_tools_exchange(
        generate,
        system="sys",
        prompt="yes, create those files",
        objective="yes, create those files",
        episode_id="ep-agent-replan",
        max_tokens=100,
        enable_thinking=False,
        timeout=None,
        chat_cycle_id="cycle-agent-replan",
        warnings=[],
        operator_route="agent",
        permission_mode="allow_edits",
        runtime_client=object(),
        session={"records": [], "owned": False, "job_id": "", "cursor": 0},
    )

    assert stop_reason == ""
    assert len(passes) == 2
    assert "text-only" in passes[1]["messages"][-1]["content"].lower()
    assert (workspace / "app.py").read_text(encoding="utf-8") == "todos = []\n"
    assert [(row["tool_name"], row["status"]) for row in records] == [
        ("start_job", "success"),
        ("task_plan", "success"),
        ("write_file", "success"),
    ]
    assert result["tool_calls"]


def test_read_only_agent_may_explain_the_permission_block_without_replanning(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(workspace))
    passes = []

    def generate(**kwargs):
        passes.append(kwargs)
        return {
            "text": "Read-only mode blocks workspace edits; use /allow-edits to permit them.",
            "tool_calls": [],
        }

    result, records, stop_reason = _operator_tools_exchange(
        generate,
        system="sys",
        prompt="create app.py",
        objective="create app.py",
        episode_id="ep-agent-read-only",
        max_tokens=100,
        enable_thinking=False,
        timeout=None,
        chat_cycle_id="cycle-agent-read-only",
        warnings=[],
        operator_route="agent",
        permission_mode="read_only",
        runtime_client=object(),
        session={"records": [], "owned": False, "job_id": "", "cursor": 0},
    )
    assert stop_reason == ""
    assert records == []
    assert len(passes) == 1
    assert result == {
        "text": "Read-only mode blocks workspace edits; use /allow-edits to permit them.",
        "tool_calls": [],
    }
