"""Native chat must own the whole signed plugin tool/result exchange."""

from __future__ import annotations

import importlib
import json
from html import unescape
from dataclasses import replace

import pytest

from auto_loop.execute import brain_skill_executor as executor
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_runtime import PluginRuntimeError
from tests.auto_loop.test_plugin_plan import _binding, _FakeBackend, _Runtime
from tests.core.test_auto_execution import ToolRuntime, _call, _mem, _operator_pp, _pack, execute


def _tool_json(content):
    assert content.startswith("Source: ")
    body = content.split('<external-data trust="untrusted-human-authored">', 1)[1]
    return json.loads(unescape(body.rsplit("</external-data>", 1)[0]))


class _WarehouseBackend(_FakeBackend):
    def call_tool_sync(self, app_id, name, arguments):
        self.calls.append((app_id, name, dict(arguments)))
        return {"item": "amber-crate"}


class _NativeRuntime(ToolRuntime):
    """Only inference and the external provider transport are substituted."""

    def __init__(self, bindings, script, *, backend=None):
        super().__init__(script, final_text="The connected warehouse contains amber-crate.")
        self.bindings = bindings
        self.provider = backend or _WarehouseBackend()
        self.plugin_runtime = _Runtime(bindings, backend=self.provider).plugin_runtime

    def plugin_tool_bindings(self):
        return self.bindings


def _lookup(candidate="a", capability="lookup", *, risk=RiskClass.READ_ONLY):
    return _binding(
        candidate=candidate,
        capability=capability,
        title="Lookup warehouse inventory",
        risk=risk,
        properties={"query": {"type": "string", "description": "Warehouse query expression"}},
        required=["query"],
    )


def _execute(runtime, monkeypatch, tmp_path, *, message="Check the connected warehouse", permission_mode="always_allow"):
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "workspace"))
    return execute(
        _operator_pp(message), "TEXT_FAST",
        _pack(execution_inputs={"chat_permission_mode": permission_mode}), _mem(), "native-plugin-episode",
        runtime_client=runtime, opened_by="operator_query", chat_cycle_id="native-plugin-chat",
    )


@pytest.mark.parametrize("mode", ["read_only", "allow_edits", "always_allow"])
def test_connected_app_tools_are_reachable_in_every_workspace_mode(mode):
    names = {spec["function"]["name"] for spec in executor.operator_tool_specs(mode)}
    assert {"discover_plugin_tools", "call_plugin_tool"} <= names


def test_one_model_discovers_selects_and_reads_real_plugin_runtime_result(tmp_path, monkeypatch):
    binding = _lookup()
    other = _lookup("b", "events_archive")
    runtime = _NativeRuntime(
        (binding, other),
        [
            [_call("discover_plugin_tools", {"query": "warehouse inventory"}, "discover")],
            [_call("call_plugin_tool", {"candidate_id": binding.candidate_id, "arguments": {"query": "warehouse=amber"}}, "invoke")],
        ],
    )
    result = _execute(runtime, monkeypatch, tmp_path, permission_mode="read_only")

    assert runtime.provider.calls == [("app.synthetic", "lookup", {"query": "warehouse=amber"})]
    assert len(runtime.calls) == 3, "discovery and invocation do not start a nested model planner"
    discovery = runtime.calls[1][1]["messages"][-1]
    assert discovery["role"] == "tool" and discovery["tool_call_id"] == "discover"
    catalog = _tool_json(discovery["content"])
    assert {row["candidate_id"] for row in catalog["tools"]} == {"a" * 64, "b" * 64}
    assert catalog["tools"][0]["input_schema"]["properties"]["query"]["type"] == "string"
    last_turn = runtime.calls[2][1]["messages"][-1]
    assert last_turn["role"] == "tool" and last_turn["tool_call_id"] == "invoke"
    assert "amber-crate" in last_turn["content"]
    assert '<external-data trust="untrusted-human-authored">' in last_turn["content"]
    assert "_meta" not in last_turn["content"]
    assert result["outputs"]["response_text"] == "The connected warehouse contains amber-crate."
    assert result["outputs"]["plugin_result"]["data"] == {"item": "amber-crate"}
    assert result["outputs"]["plugin_plan"]["steps"][0]["arguments"] == {"query": "warehouse=amber"}
    assert result["outputs"]["plugin_step_results"][0]["status"] == "completed"


def test_disconnect_after_discovery_is_a_tool_failure_not_a_provider_call(tmp_path, monkeypatch):
    binding = _lookup()

    class DisconnectedRuntime(_NativeRuntime):
        def generate_completion(self, **kwargs):
            if len(self.calls) == 1:
                self.bindings = ()
            return super().generate_completion(**kwargs)

    runtime = DisconnectedRuntime((binding,), [
        [_call("discover_plugin_tools", {"query": "inventory"}, "discover")],
        [_call("call_plugin_tool", {"candidate_id": binding.candidate_id, "arguments": {"query": "warehouse=amber"}}, "invoke")],
    ])
    result = _execute(runtime, monkeypatch, tmp_path)

    assert runtime.provider.calls == []
    returned = runtime.calls[-1][1]["messages"][-1]["content"]
    assert "plugin_candidate_unavailable" in returned
    assert result["outputs"]["plugin_failure"] == "plugin_candidate_unavailable"


def test_provider_authorization_failure_reaches_same_model_and_episode(tmp_path, monkeypatch):
    class DeniedBackend(_FakeBackend):
        def call_tool_sync(self, *_args, **_kwargs):
            raise PluginRuntimeError("plugin_connection_unavailable")

    binding = _lookup()
    runtime = _NativeRuntime((binding,), [[_call("call_plugin_tool", {
        "candidate_id": binding.candidate_id, "arguments": {"query": "inventory"},
    }, "invoke")]], backend=DeniedBackend())
    result = _execute(runtime, monkeypatch, tmp_path)

    assert runtime.provider.calls == []
    assert "plugin_connection_unavailable" in runtime.calls[-1][1]["messages"][-1]["content"]
    assert result["outputs"]["plugin_failure"] == "plugin_connection_unavailable"


def test_write_without_authority_does_not_call_provider(tmp_path, monkeypatch):
    binding = _lookup(capability="create_issue", risk=RiskClass.ADDITIVE_WRITE)
    runtime = _NativeRuntime((binding,), [[_call("call_plugin_tool", {
        "candidate_id": binding.candidate_id, "arguments": {"query": "approved stock request"},
    }, "invoke")]])
    result = _execute(runtime, monkeypatch, tmp_path)

    assert runtime.provider.calls == []
    assert "plugin_permission_store_unavailable" in runtime.calls[-1][1]["messages"][-1]["content"]
    assert result["outputs"]["plugin_failure"] == "plugin_permission_store_unavailable"


def test_direct_connection_scope_executes_native_write_without_second_prompt(tmp_path, monkeypatch):
    from tests.auto_loop.test_plugin_plan import _LifecycleDirectRuntime

    binding = _lookup(capability="create_issue", risk=RiskClass.ADDITIVE_WRITE)
    direct = _LifecycleDirectRuntime(binding)
    runtime = _NativeRuntime((binding,), [[_call("call_plugin_tool", {
        "candidate_id": binding.candidate_id, "arguments": {"query": "approved stock request"},
    }, "invoke")]])
    runtime.plugin_runtime = direct.plugin_runtime
    runtime.resolve_plugin_authority = direct.resolve_plugin_authority
    result = _execute(runtime, monkeypatch, tmp_path, permission_mode="read_only")

    assert direct.calls == [("app.synthetic", "create_issue", {"query": "approved stock request"})]
    assert result["failure_kind"] is None
    assert "plugin_permission_request" not in result["outputs"]
    assert result["outputs"]["plugin_result"]["data"] == {"item": "event"}


def test_one_native_turn_cannot_dispatch_more_than_four_plugin_calls(tmp_path, monkeypatch):
    binding = _lookup()
    runtime = _NativeRuntime((binding,), [[
        _call("call_plugin_tool", {"candidate_id": binding.candidate_id, "arguments": {"query": f"batch-{index}"}}, f"invoke-{index}")
        for index in range(6)
    ]])
    result = _execute(runtime, monkeypatch, tmp_path)

    assert [call[2] for call in runtime.provider.calls] == [
        {"query": "batch-0"}, {"query": "batch-1"}, {"query": "batch-2"}, {"query": "batch-3"},
    ]
    assert result["outputs"]["operator_tool_calls"][-1]["error"] == "plugin_call_budget_exhausted"


def test_permission_request_folds_exact_plan_and_stops_further_tools(tmp_path, monkeypatch):
    from tests.mcp_platform.test_generic_plugin_acceptance import _PermissionStore

    binding = _lookup(capability="create_issue", risk=RiskClass.ADDITIVE_WRITE)
    runtime = _NativeRuntime((binding,), [[
        _call("call_plugin_tool", {"candidate_id": binding.candidate_id, "arguments": {"query": "approved stock request"}}, "request"),
        _call("call_plugin_tool", {"candidate_id": binding.candidate_id, "arguments": {"query": "another write"}}, "later"),
    ]])

    class PermissionStore(_PermissionStore):
        def issue(self, plan, **kwargs):
            request = super().issue(plan, **kwargs)
            self.issued = replace(request, task_id=kwargs["task_id"], account_key_hash=binding.account_key_hash)
            return self.issued

    runtime.permission_store = PermissionStore(binding)
    result = _execute(runtime, monkeypatch, tmp_path, permission_mode="read_only")

    assert runtime.provider.calls == []
    assert result["failure_kind"] == "pending_input"
    permission = result["outputs"]["plugin_permission_request"]
    assert permission["request_id"] == runtime.permission_store.issued.request_id
    assert runtime.permission_store.issued.task_id == "native-plugin-chat"
    assert result["outputs"]["plugin_plan"]["steps"][0]["arguments"] == {"query": "approved stock request"}
    assert len(result["outputs"]["operator_tool_calls"]) == 1
    assert runtime.calls[-1][1].get("tools") is None


@pytest.mark.parametrize("opened_by", ["operator_query", "operator_command"])
def test_ordinary_plugin_request_reaches_native_model_without_catalog_prerouting(monkeypatch, opened_by):
    intent = importlib.import_module("global_loop.intent.run")
    monkeypatch.setattr("mcp_platform.plugin_dispatch.plugin_tool_bindings", lambda: (_lookup(),))
    assert intent._map_opened_by_to_intent(opened_by, "lookup warehouse inventory") == "RESPOND"


def test_two_plugin_calls_keep_both_plans_and_results_through_episode_fold(tmp_path, monkeypatch):
    from auto_loop._auto_state import AutoInput
    from auto_loop.fold.run import _fold_episode_auto
    from auto_loop.promptpack.run import build_prompt_pack

    binding = _lookup()
    runtime = _NativeRuntime((binding,), [
        [_call("call_plugin_tool", {"candidate_id": binding.candidate_id, "arguments": {"query": "warehouse=amber"}}, "first")],
        [_call("call_plugin_tool", {"candidate_id": binding.candidate_id, "arguments": {"query": "warehouse=blue"}}, "second")],
    ])
    result = _execute(runtime, monkeypatch, tmp_path)
    envelope = _mem()
    pack = _pack()
    folded = _fold_episode_auto(
        AutoInput("native-plugin-episode", {}, {}, pack, {}, None),
        status="success", cycle_kind="success", memory_envelope=envelope,
        model_profile=None, model_id=None, prompt_pack=build_prompt_pack(pack, envelope),
        exec_result=result, runtime_ms=1,
    )

    records = folded["outputs"]["operator_tool_calls"]
    assert [row["result"]["plugin_plan"]["steps"][0]["arguments"] for row in records] == [
        {"query": "warehouse=amber"}, {"query": "warehouse=blue"},
    ]
    assert [row["result"]["plugin_result"]["data"] for row in records] == [
        {"item": "amber-crate"}, {"item": "amber-crate"},
    ]
    assert len(folded["outputs"]["plugin_step_results"]) == 2
    assert folded["outputs"]["plugin_plan"]["steps"][0]["arguments"] == {"query": "warehouse=blue"}


def test_discovery_pages_complete_schemas_without_truncating_model_json(tmp_path, monkeypatch):
    from mcp_platform.canonical import canonical_bytes, sha256_hex

    bindings = []
    for candidate in "abcd":
        binding = _lookup(candidate)
        schema = {"type": "object", "required": ["query"], "additionalProperties": False,
                  "properties": {"query": {"type": "string", "description": "Warehouse " * 349 + "inventory."}}}
        bindings.append(replace(binding, input_schema=schema, input_schema_sha256=sha256_hex(canonical_bytes(schema))))
    runtime = _NativeRuntime(tuple(bindings), [
        [_call("discover_plugin_tools", {}, "page-one")],
        [_call("discover_plugin_tools", {"offset": 2}, "page-two")],
    ])
    _execute(runtime, monkeypatch, tmp_path)

    first = _tool_json(runtime.calls[1][1]["messages"][-1]["content"])
    second = _tool_json(runtime.calls[2][1]["messages"][-1]["content"])
    assert first["next_offset"] == 2 and second["next_offset"] is None
    assert [row["candidate_id"] for row in first["tools"] + second["tools"]] == [
        "a" * 64, "b" * 64, "c" * 64, "d" * 64,
    ]
    assert all(len(row["input_schema"]["properties"]["query"]["description"]) == 3500
               for row in first["tools"] + second["tools"])


def test_catalog_outage_is_reported_not_returned_as_empty_discovery(tmp_path, monkeypatch):
    class UnavailableRuntime(_NativeRuntime):
        def plugin_tool_bindings(self):
            raise PluginRuntimeError("plugin_catalog_unavailable")

    runtime = UnavailableRuntime((), [[_call("discover_plugin_tools", {}, "discover")]])
    result = _execute(runtime, monkeypatch, tmp_path)
    assert runtime.calls[-1][1]["messages"][-1]["content"] == "ERROR: plugin_catalog_unavailable"
    assert result["outputs"]["plugin_failure"] == "plugin_catalog_unavailable"


def test_machine_minted_continuation_does_not_gain_direct_turn_write_authority(tmp_path, monkeypatch):
    from auto_loop.execute.run import _operator_tools_reply
    from tests.auto_loop.test_plugin_plan import _LifecycleDirectRuntime

    binding = _lookup(capability="create_issue", risk=RiskClass.ADDITIVE_WRITE)
    direct = _LifecycleDirectRuntime(binding)
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "workspace"))
    passes = []

    def generate(**kwargs):
        passes.append(kwargs)
        if len(passes) == 1:
            return {"tool_calls": [_call("call_plugin_tool", {
                "candidate_id": binding.candidate_id, "arguments": {"query": "autonomous stock request"},
            })]}
        return {"text": "No fresh operator authority was available."}

    _, records, _ = _operator_tools_reply(
        generate, system="sys", prompt="continue", objective="continue", episode_id="machine-episode",
        max_tokens=100, enable_thinking=False, timeout=None, chat_cycle_id="", warnings=[],
        permission_mode="always_allow", runtime_client=direct, work_kind="job_continuation",
    )
    assert direct.calls == []
    assert records[0]["result"]["plugin_status"] == "failed"
    assert "plugin_permission_store_unavailable" in passes[-1]["messages"][-1]["content"]


@pytest.mark.parametrize("model_failed", [False, True])
def test_structured_plugin_result_returns_to_same_model_without_more_tools(tmp_path, monkeypatch, model_failed):
    binding = _lookup()

    class StructuredRuntime(_NativeRuntime):
        def generate_completion(self, **kwargs):
            if model_failed:
                self.calls.append(("generate", kwargs))
                return {"ok": False, "reason": "synthetic_inference_unavailable"}
            return super().generate_completion(**kwargs)

    runtime = StructuredRuntime((binding,), [])
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "workspace"))
    result = execute(
        _operator_pp("Summarize the warehouse inventory"), "TEXT_FAST",
        _pack(action="PLUGIN_OPERATION", execution_inputs={
            "plugin_objective": "Summarize the warehouse inventory",
            "plugin_operation": {"plugin_id": binding.plugin_id, "app_id": binding.app_id,
                                 "capability": binding.capability, "candidate_id": binding.candidate_id,
                                 "arguments": {"query": "warehouse=amber"}},
        }), _mem(), "structured-plugin-episode", runtime_client=runtime,
        opened_by="operator_query", chat_cycle_id="structured-plugin-chat",
    )

    assert len(runtime.calls) == 1
    assert runtime.provider.calls == [("app.synthetic", "lookup", {"query": "warehouse=amber"})]
    request = runtime.calls[0][1]
    assert request.get("tools") is None
    assert "Summarize the warehouse inventory" in request["prompt"]
    assert "amber-crate" in request["prompt"]
    assert '<external-data trust="untrusted-human-authored">' in request["prompt"]
    assert result["outputs"]["plugin_result"]["data"] == {"item": "amber-crate"}
    if model_failed:
        assert result["failure_kind"] == "response_generation_failed"
        assert "synthetic_inference_unavailable" in result["failure_reason"]
    else:
        assert result["outputs"]["response_text"] == "The connected warehouse contains amber-crate."


@pytest.mark.parametrize("opened_by", ["operator_query", "operator_command"])
def test_ui_selector_stays_on_native_model_but_full_operation_keeps_explicit_rail(opened_by):
    intent = importlib.import_module("global_loop.intent.run")
    assert intent._map_opened_by_to_intent(opened_by, "inspect inventory", metadata={
        "plugin_operation": {"plugin_id": "warehouse.app"}, "directive_intent": "PLUGIN_OPERATION",
    }) == "RESPOND"
    assert intent._map_opened_by_to_intent(opened_by, "inspect inventory", metadata={
        "plugin_operation": {"plugin_id": "warehouse.app", "app_id": "warehouse.mcp", "capability": "lookup", "arguments": {}},
    }) == "PLUGIN_OPERATION"


def test_ui_selector_limits_discovery_and_rejects_another_apps_candidate(tmp_path, monkeypatch):
    selected = _lookup()
    other = replace(_lookup("b"), plugin_id="other.app")
    runtime = _NativeRuntime((selected, other), [
        [_call("discover_plugin_tools", {"query": "inventory"}, "discover")],
        [_call("call_plugin_tool", {"candidate_id": other.candidate_id, "arguments": {"query": "inventory"}}, "wrong-app")],
    ])
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "workspace"))
    result = execute(
        _operator_pp("Inspect the selected app"), "TEXT_FAST", _pack(execution_inputs={
            "plugin_operation": {"plugin_id": "plugin.synthetic"},
        }), _mem(), "selector-episode", runtime_client=runtime,
        opened_by="operator_query", chat_cycle_id="selector-chat",
    )

    discovered = _tool_json(runtime.calls[1][1]["messages"][-1]["content"])
    assert discovered["plugins"] == ["plugin.synthetic"]
    assert [item["candidate_id"] for item in discovered["tools"]] == ["a" * 64]
    assert runtime.provider.calls == []
    assert result["outputs"]["plugin_failure"] == "plugin_selector_mismatch"
