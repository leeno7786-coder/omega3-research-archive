from __future__ import annotations

import importlib

import pytest

from pressure_loop import policy


def test_auto_invoker_refuses_namespaced_plugin_tools(monkeypatch) -> None:
    execute_run = importlib.import_module("auto_loop.execute.run")
    seen: list[tuple[str, dict]] = []

    def invoke(tool_name, arguments, **kwargs):
        seen.append((tool_name, dict(arguments)))
        return {"ok": True, "result": {}, "plugin_result": {}, "elapsed_ms": 1}

    monkeypatch.setattr("mcp_platform.plugin_dispatch.invoke_plugin_tool", invoke)
    monkeypatch.setattr(
        "runtime.runtime_tool_dispatch.invoke_runtime_backed_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy path used")),
    )
    with pytest.raises(RuntimeError, match="plugin_tool_requires_plugin_operation"):
        execute_run._default_tool_invoker(
            "plugin__northwind.calendar__list_events",
            {"start": "tomorrow"},
            episode_id="ep",
            runtime_client=object(),
        )
    assert seen == []


def test_auto_runtime_tool_invoker_preserves_lane_prediction_context(monkeypatch) -> None:
    execute_run = importlib.import_module("auto_loop.execute.run")
    captured = {}

    def invoke(_tool_name, _arguments, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "text": "vision result"}

    monkeypatch.setattr("runtime.runtime_tool_dispatch.invoke_runtime_backed_tool", invoke)

    result = execute_run._default_tool_invoker(
        "vision_reasoning_tool",
        {"image_ref": "scene.png"},
        episode_id="ep-auto",
        runtime_client=object(),
        pipeline_id=1,
        work_kind="operator_lane_work",
        cycle_id="browser-cycle",
        job_id="job-1",
    )

    assert result["ok"] is True
    assert {
        key: captured[key]
        for key in ("pipeline_id", "work_kind", "cycle_id", "job_id")
    } == {
        "pipeline_id": 1,
        "work_kind": "operator_lane_work",
        "cycle_id": "browser-cycle",
        "job_id": "job-1",
    }


def test_policy_registry_merges_signed_plugin_entries(monkeypatch) -> None:
    monkeypatch.setattr(
        "mcp_platform.plugin_dispatch.plugin_tool_registry_entries",
        lambda: {
            "plugin__northwind.calendar__list_events": {
                "plugin_id": "northwind.calendar",
                "app_id": "northwind.calendar.mcp",
                "capability": "list_events",
                "description": "List signed Northwind events.",
            }
        },
    )
    registered = policy.tool_registry()
    assert registered["plugin__northwind.calendar__list_events"]["plugin"]["plugin_id"] == (
        "northwind.calendar"
    )
    assert registered["plugin__northwind.calendar__list_events"]["capabilities"] == [
        "plugin",
        "mcp",
    ]


def test_plugin_tools_are_direct_auto_calls(monkeypatch) -> None:
    monkeypatch.setattr(
        "pressure_loop.policy.plugin_tool_names",
        lambda: ("plugin__northwind.calendar__list_events",),
    )
    from auto_loop.route.run import tool_call_is_direct

    assert (
        tool_call_is_direct(
            {
                "action": "USE_TOOL",
                "tool": "plugin__northwind.calendar__list_events",
                "execution_inputs": {"start": "tomorrow"},
            }
        )
        is False
    )
