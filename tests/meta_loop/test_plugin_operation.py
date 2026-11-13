from __future__ import annotations

from meta_loop.action.run import run
from meta_loop.intake.run import MetaAInput
from meta_loop.tool.run import run as select_tool
from meta_loop._meta_a_state import MetaAState


def test_plugin_directive_carries_explicit_operation_to_auto() -> None:
    operation = {
        "plugin_id": "google.workspace.drive",
        "app_id": "google.workspace.drive.mcp",
        "capability": "search_files",
        "arguments": {"query": "quarterly plan"},
    }
    state = run(
        MetaAInput(
            episode_id="ep-plugin",
            pressure={
                "aggregate_pressure": 1.0,
                "contributors": [
                    {
                        "metadata": {
                            "message": "search Drive",
                            "directive_intent": "PLUGIN_OPERATION",
                            "plugin_operation": operation,
                        }
                    }
                ],
            },
            trajectory_snapshot={},
            action_goal={"intent": "PLUGIN_OPERATION", "episode_id": "ep-plugin"},
        )
    )
    assert state.action == "PLUGIN_OPERATION"
    assert state.execution_inputs["plugin_operation"] == operation


def test_plugin_permission_decision_carries_continuation_metadata_to_auto() -> None:
    metadata = {
        "message": "",
        "directive_intent": "PLUGIN_OPERATION",
        "ingress_class": "authority_request",
        "decision": "allow",
        "request_id": "a" * 48,
        "expected_revision": "b" * 64,
        "cycle_id": f"plugin-permission:{'a' * 48}",
        "prior_episode_id": "episode-prior",
        "task_id": "task-synthetic",
        "plan_id": "c" * 64,
        "plan_digest": "d" * 64,
        "arrival_seq": 1,
    }
    state = run(
        MetaAInput(
            episode_id="ep-continuation",
            pressure={"aggregate_pressure": 1.0, "contributors": [{"metadata": metadata}]},
            trajectory_snapshot={},
            action_goal={"intent": "PLUGIN_OPERATION", "episode_id": "ep-continuation"},
        )
    )

    assert state.execution_inputs["plugin_permission_contributor"] == metadata


def test_use_tool_selection_does_not_pre_authorize_a_plugin_tool(monkeypatch) -> None:
    monkeypatch.setattr(
        "mcp_platform.plugin_dispatch.resolve_plugin_tool_request",
        lambda _text: (_ for _ in ()).throw(
            AssertionError("Meta must not resolve or dispatch plugin tools")
        ),
    )
    state = select_tool(
        MetaAState(
            mi=None,
            episode_id="ep-plugin",
            intent="USE_TOOL",
            action="USE_TOOL",
            source={},
            text="Find the plan in Google Drive",
            target_str="",
            constraints={},
            meta={},
        )
    )
    assert state.tool != "plugin__google.workspace.drive__search_files"
    assert state.execution_inputs["query"] == "Find the plan in Google Drive"


def test_native_response_carries_ui_plugin_selector_through_meta_tool_selection() -> None:
    state = select_tool(run(MetaAInput(
        episode_id="ep-native-selector",
        pressure={"aggregate_pressure": 1.0, "contributors": [{"metadata": {
            "message": "Inspect the selected app", "plugin_operation": {"plugin_id": "warehouse.app"},
        }}]},
        trajectory_snapshot={}, action_goal={"intent": "RESPOND", "episode_id": "ep-native-selector"},
    )))
    assert state.action == "RESPONSE"
    assert state.execution_inputs["plugin_operation"] == {"plugin_id": "warehouse.app"}
