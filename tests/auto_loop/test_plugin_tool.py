from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from auto_loop.execute.plugin_tool import run_plugin_operation, run_plugin_plan_step
from auto_loop.execute.run import _default_tool_invoker
from mcp_platform.app_registry import InstalledMcpAppRecord
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.plugin_runtime import (
    PluginOperationRequest,
    PluginRuntime,
    PluginRuntimeError,
)
from mcp_platform.plugin_planning import PluginOperationPlan, PluginOperationStep
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.plugin_dispatch import canonical_plugin_tool_name


@dataclass
class _Runtime:
    seen: list[tuple[str, str, dict[str, Any]]]

    def call_tool_sync(self, app_id: str, name: str, arguments: dict[str, Any]):
        self.seen.append((app_id, name, arguments))
        return {"ok": True}


class _PluginRegistry:
    def get(self, plugin_id: str) -> object:
        return SimpleNamespace(status="installed", enabled=True)


class _AppRegistry:
    def __init__(self, record: InstalledMcpAppRecord) -> None:
        self._record = record

    def get(self, app_id: str) -> InstalledMcpAppRecord:
        return self._record


@dataclass
class _Client:
    plugin_runtime: object
    bindings: tuple[Any, ...] = ()

    def plugin_tool_bindings(self) -> tuple[Any, ...]:
        return self.bindings


def _binding(
    *,
    plugin_id: str,
    app_id: str,
    capability: str,
    risk: RiskClass,
    properties: dict[str, object],
    required: list[str],
    effect: str,
) -> PluginToolBinding:
    input_schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"item": {"type": "string"}},
        "required": ["item"],
        "additionalProperties": False,
    }
    return PluginToolBinding(
        candidate_id="a" * 64,
        tool_name=canonical_plugin_tool_name(plugin_id, capability),
        plugin_id=plugin_id,
        plugin_revision="b" * 64,
        app_id=app_id,
        account_key_hash="c" * 64,
        connection_revision="d" * 64,
        capability=capability,
        title=capability,
        description=capability,
        input_schema=input_schema,
        input_schema_sha256=sha256_hex(canonical_bytes(input_schema)),
        output_schema=output_schema,
        output_schema_sha256=sha256_hex(canonical_bytes(output_schema)),
        risk_class=risk,
        effect=effect,
        requires_revision=False,
        max_result_bytes=4096,
        allowed_mime_types=("application/json",),
    )


def _tracker_record() -> InstalledMcpAppRecord:
    from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
    from plugin_platform.contracts import AppRequirement
    from tests.mcp_platform.generic_plugin_peers import TRACKER_APP, catalogs_for
    from tests.mcp_platform.test_package_verifier import host_tag

    tool_catalog, _resources = catalogs_for("tracker")
    requirement = AppRequirement.from_dict(
        {
            "app_id": TRACKER_APP,
            "publisher_id": "omega.first_party",
            "deployment": "local",
            "provider_id": "northwind",
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": ["stdio"],
            "remote_origin": None,
            "authorization_issuer": None,
            "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": [
                {
                    "kind": "tool",
                    "name": tool.name,
                    "schema_sha256": tool.input_schema_sha256,
                    "version": None,
                }
                for tool in tool_catalog.tools
            ],
        }
    )
    return InstalledMcpAppRecord(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=requirement.deployment,
        provider_id=requirement.provider_id,
        protocol_version=requirement.protocol_version,
        transports=requirement.transports,
        remote_origin=requirement.remote_origin,
        authorization_issuer=requirement.authorization_issuer,
        authentication=requirement.authentication,
        capabilities=requirement.capabilities,
        local_package_version="1.0.0",
        local_package_platform=host_tag(),
        local_package_manifest_sha256="6" * 64,
        installed=True,
        trusted=True,
        enabled=False,
    )


def test_plugin_operation_is_explicit_and_provider_neutral(tmp_path) -> None:
    verified = PluginLifecycleService(state_root=tmp_path)._verified("google.workspace.drive")[2]
    record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=verified.manifest.required_apps[0])
    )
    backend = _Runtime([])
    runtime = PluginRuntime(
        plugin_registry=_PluginRegistry(),
        app_registry=_AppRegistry(record),
        backend=backend,
    )
    result = run_plugin_operation(
        {
            "plugin_id": "google.workspace.drive",
            "app_id": "google.workspace.drive.mcp",
            "capability": "search_files",
            "arguments": {"query": "quarterly plan"},
        },
        runtime_client=_Client(
            runtime,
            bindings=(
                _binding(
                    plugin_id="google.workspace.drive",
                    app_id="google.workspace.drive.mcp",
                    capability="search_files",
                    risk=RiskClass.READ_ONLY,
                    properties={"query": {"type": "string"}},
                    required=["query"],
                    effect="read",
                ),
            ),
        ),
    )
    assert result["status"] == "success"
    assert result["plugin_result"]["capability"] == "search_files"
    assert backend.seen[0][2]["query"] == "quarterly plan"


def test_plugin_operation_rejects_implicit_or_provider_specific_dispatch() -> None:
    with pytest.raises(PluginRuntimeError, match="plugin_operation_invalid"):
        run_plugin_operation("google drive", runtime_client=object())

    with pytest.raises(PluginRuntimeError, match="plugin_runtime_unavailable"):
        run_plugin_operation(
            {
                "plugin_id": "google.workspace.drive",
                "app_id": "google.workspace.drive.mcp",
                "capability": "search_files",
                "arguments": {},
            },
            runtime_client=object(),
        )


def test_plugin_operation_rejects_invoke_only_runtime_doubles() -> None:
    class InvokeOnly:
        def invoke_tool(self, _request):
            raise AssertionError("provider must not be reached")

    with pytest.raises(PluginRuntimeError, match="plugin_runtime_unavailable"):
        run_plugin_operation(
            {
                "plugin_id": "google.workspace.drive",
                "app_id": "google.workspace.drive.mcp",
                "capability": "search_files",
                "arguments": {},
            },
            runtime_client=InvokeOnly(),
        )


def test_run_plugin_plan_step_hands_one_step_to_runtime(monkeypatch, tmp_path) -> None:
    seen: list[dict[str, object]] = []

    verified = PluginLifecycleService(state_root=tmp_path)._verified("google.workspace.drive")[2]
    record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=verified.manifest.required_apps[0])
    )
    capability = next(
        item
        for item in record.capabilities
        if item.name == "search_files"
    )

    class FakeBackend:
        def call_tool_sync(self, _app_id, _name, arguments):
            seen.append(dict(arguments))
            return {
                "plugin_id": "google.workspace.drive",
                "app_id": record.app_id,
                "capability": "search_files",
                "status": "completed",
                "data": {"item": arguments["query"]},
            }

    runtime = PluginRuntime(
        plugin_registry=_PluginRegistry(),
        app_registry=_AppRegistry(record),
        backend=FakeBackend(),
    )
    monkeypatch.setattr(
        "auto_loop.execute.plugin_tool._runtime_from_client",
        lambda _client: runtime,
    )
    binding = SimpleNamespace(
        plugin_id="google.workspace.drive",
        app_id=record.app_id,
        capability="search_files",
        connection_revision="c" * 64,
        input_schema_sha256="d" * 64,
    )
    step = PluginOperationStep(
        "step-1",
        "e" * 64,
        {"query": "hello"},
        (),
    )
    plan = PluginOperationPlan(
        "f" * 64,
        "episode-1",
        "a" * 64,
        (step,),
        "b" * 64,
    )

    result = run_plugin_plan_step(
        binding,
        plan,
        step,
        runtime_client=object(),
    )

    assert result["status"] == "completed"
    assert seen == [{"query": "hello"}]


def test_explicit_mutation_requires_runtime_authority_not_drive_confirmation() -> None:
    record = _tracker_record()
    runtime = PluginRuntime(
        plugin_registry=_PluginRegistry(),
        app_registry=_AppRegistry(record),
        backend=_Runtime([]),
    )
    write_binding = _binding(
        plugin_id="northwind.tracker",
        app_id="northwind.tracker.mcp",
        capability="create_issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"summary": {"type": "string"}},
        required=["summary"],
        effect="create",
    )
    operation = {
        "plugin_id": "northwind.tracker",
        "app_id": "northwind.tracker.mcp",
        "capability": "create_issue",
        "arguments": {"summary": "broken printer"},
    }
    with pytest.raises(PluginRuntimeError, match="plugin_authority_required"):
        run_plugin_operation(
            operation,
            runtime_client=_Client(runtime, bindings=(write_binding,)),
        )
    operation["confirmation"] = "0" * 64
    with pytest.raises(PluginRuntimeError, match="plugin_authority_required"):
        run_plugin_operation(
            operation,
            runtime_client=_Client(runtime, bindings=(write_binding,)),
        )


def test_plan_step_rejects_mutation_without_authority():
    binding = SimpleNamespace(
        plugin_id="plugin",
        app_id="app",
        capability="write",
        risk_class=RiskClass.ADDITIVE_WRITE,
        connection_revision="c" * 64,
        input_schema_sha256="d" * 64,
    )
    step = PluginOperationStep("step-1", "e" * 64, {}, ())
    plan = PluginOperationPlan("f" * 64, "episode-1", "a" * 64, (step,), "b" * 64)

    with pytest.raises(PluginRuntimeError, match="plugin_authority_required"):
        run_plugin_plan_step(binding, plan, step, runtime_client=object())


def test_plugin_operation_rejects_when_signed_binding_is_unavailable(tmp_path) -> None:
    verified = PluginLifecycleService(state_root=tmp_path)._verified("google.workspace.drive")[2]
    record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=verified.manifest.required_apps[0])
    )
    backend = _Runtime([])
    runtime = PluginRuntime(
        plugin_registry=_PluginRegistry(),
        app_registry=_AppRegistry(record),
        backend=backend,
    )

    with pytest.raises(PluginRuntimeError, match="^plugin_tool_unavailable$"):
        run_plugin_operation(
            {
                "plugin_id": "northwind.tracker",
                "app_id": "northwind.tracker.mcp",
                "capability": "create_issue",
                "arguments": {"summary": "broken printer"},
            },
            runtime_client=_Client(runtime),
        )
    assert backend.seen == []


def test_auto_invoker_refuses_namespaced_plugin_tools(monkeypatch) -> None:
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
        _default_tool_invoker(
            "plugin__northwind.calendar__list_events",
            {"start": "tomorrow"},
            runtime_client=object(),
        )
    assert seen == []
