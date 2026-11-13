"""Architecture and live-process acceptance for provider-neutral plugin dispatch."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
import uvicorn

from auto_loop._auto_state import AutoInput, MemoryEnvelope, PromptPack
from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
from auto_loop.fold.run import _fold_episode_auto, screen_plugin_outputs
from mcp_platform.app_registry import InstalledMcpAppRecord, McpAppRegistrySnapshot
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client import CurrentMcpClient, SdkClientFactory
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.operation_contracts import McpAuthoritySnapshot, McpConnectionSnapshot
from mcp_platform.oauth_profiles import OAuthProfileMatchInput
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, RiskClass
from mcp_platform.plugin_authority import plugin_plan_digest
from mcp_platform.plugin_backend import CurrentMcpPluginBackend
from mcp_platform.plugin_catalog import (
    PluginCatalogAppAuthority,
    PluginToolBinding,
    compile_plugin_tool_bindings,
)
from mcp_platform.plugin_connections import PluginConnectionService
from mcp_platform.plugin_execution_worker import PluginExecutionWorker
from mcp_platform.plugin_permissions import (
    PluginPermissionDisplay,
    PluginPermissionRequest,
)
from mcp_platform.plugin_planning import retrieve_plugin_candidates
from mcp_platform.plugin_runtime import PluginRuntime, PluginRuntimeError
from mcp_platform.transports.streamable_http import (
    StreamableHttpClientConfig,
    build_streamable_http_transport,
)
from plugin_platform.contracts import AppRequirement
from plugin_platform.registry import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginAppBinding,
    PluginRegistrySnapshot,
)
from tests.mcp_platform.generic_plugin_install import (
    descriptor_from_installed,
    install_signed_northwind_plugins,
)
from tests.mcp_platform.generic_plugin_peers import (
    CALENDAR_APP,
    CALENDAR_PLUGIN,
    LEDGER_APP,
    LEDGER_PLUGIN,
    TRACKER_APP,
    TRACKER_ORIGIN,
    TRACKER_PLUGIN,
    _EVENT_ITEM,
    _ISSUE_ITEM,
    build_server,
    catalogs_for,
)
from tests.helpers.plugin_consent import full_connection_grant
from tests.mcp_platform.test_package_verifier import host_tag
from tests.mcp_platform.test_plugin_connections import (
    _Broker as _TestOAuthBroker,
    _GrantService as _TestGrantService,
)

_ROOT = Path(__file__).resolve().parents[2]
_GENERIC_MODULES = (
    _ROOT / "mcp_platform" / "plugin_dispatch.py",
    _ROOT / "mcp_platform" / "plugin_planning.py",
    _ROOT / "mcp_platform" / "plugin_catalog.py",
    _ROOT / "auto_loop" / "execute" / "plugin_plan.py",
)
_EXECUTE_RUN = _ROOT / "auto_loop" / "execute" / "run.py"
_OPERATOR_ACCOUNT = "account-1"

_TEST_LOOP = None


def _shared_loop_submit(operation, *, timeout_s=None):
    global _TEST_LOOP
    if _TEST_LOOP is None or _TEST_LOOP.is_closed():
        _TEST_LOOP = asyncio.new_event_loop()
    return _TEST_LOOP.run_until_complete(operation())
_ACCOUNT = hashlib.sha256(_OPERATOR_ACCOUNT.encode("utf-8")).hexdigest()


def _spawned_acceptance_operation(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    """Spawn-safe provider stand-in for fixture packages signed by ephemeral rings."""

    del credential
    if body.get("discovery") is True:
        descriptor = dict(body["descriptor"])
        signed = dict(descriptor["signed_capability_authority"])
        return {
            "discovery": {
                "app_id": descriptor["app_id"],
                "publisher_id": descriptor["publisher_id"],
                "deployment": descriptor["deployment"],
                "transport": descriptor["transport"],
                "protocol_version": descriptor["protocol_version"],
                "source": (
                    "live_observed"
                    if descriptor["deployment"] == "local"
                    else "live_authenticated"
                ),
                "server_id": descriptor["app_id"],
                "server_version": "1.0.0",
                "tools": {name: {} for name in signed["tools"]},
                "prompts": {name: {} for name in signed["prompts"]},
                "resources": {name: {} for name in signed["resources"]},
                "resource_templates": {
                    name: {} for name in signed["resource_templates"]
                },
                "extensions": {},
                "cache_metadata": {},
                "capability_revision": descriptor["capability_revision"],
                "signed_capability_authority": signed,
            }
        }
    request = body["request"]
    item = {
        "tool:list_events": _EVENT_ITEM,
        "tool:create_issue": _ISSUE_ITEM,
    }[request["capability_id"]]
    return {
        "request_id": request["request_id"],
        "app_id": request["app_id"],
        "capability_id": request["capability_id"],
        "capability_revision": request["capability_revision"],
        "status": "completed",
        "data": {
            "content": [{"type": "text", "text": item}],
            "structured_content": {"item": item},
            "is_error": False,
        },
    }


class _ObservedExecutionWorker:
    def __init__(self) -> None:
        self.worker = PluginExecutionWorker(
            child_handler=_spawned_acceptance_operation,
            shutdown_timeout_seconds=10.0,
        )
        self.failures: list[str] = []

    def run_sync(self, **kwargs: Any) -> Mapping[str, Any]:
        try:
            return self.worker.run_sync(**kwargs)
        except Exception as exc:
            self.failures.append(f"{type(exc).__name__}: {exc}")
            raise

    async def run_async(self, **kwargs: Any) -> Mapping[str, Any]:
        try:
            return await self.worker.run_async(**kwargs)
        except Exception as exc:
            self.failures.append(f"{type(exc).__name__}: {exc}")
            raise

    def close(self) -> None:
        self.worker.close()


class _ObservedOAuthBroker(_TestOAuthBroker):
    async def _current_prepared_access_token(
        self,
        prepared: object,
        binding: object,
    ) -> str:
        self.authorize_prepared_transport(prepared, binding, object())
        return "worker-prepared-access-token"

    async def _issue_execution_worker_access_token(
        self,
        binding: object,
        authority: object,
        **kwargs: object,
    ) -> str:
        return await super()._issue_execution_worker_access_token(
            binding,
            authority,
            **kwargs,
        )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_generic_dispatch_has_no_provider_action_map() -> None:
    dispatch = _read(_ROOT / "mcp_platform" / "plugin_dispatch.py")
    tree = ast.parse(dispatch)
    assigned = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    assert "action_terms" not in assigned
    assert "_structured_search_query" not in {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert "fullText contains" not in dispatch
    assert "search_files" not in dispatch
    assert "list_recent_files" not in dispatch
    assert "google.workspace.drive" not in dispatch
    assert "app_ids_for_plugin" not in dispatch


def test_compiler_planner_and_executor_do_not_own_lifecycle_app_maps() -> None:
    forbidden = (
        "app_ids_for_plugin",
        "google.workspace.drive",
        "search_files",
        "list_recent_files",
        "google_drive_api",
    )
    for path in _GENERIC_MODULES:
        text = _read(path)
        for token in forbidden:
            assert token not in text, f"{path.name} still names {token}"


def test_auto_execute_has_one_plugin_invocation_path() -> None:
    text = _read(_EXECUTE_RUN)
    assert "plan_and_execute_plugin_request" in text
    assert "invoke_plugin_tool" not in text


def test_invoke_plugin_tool_is_not_a_live_invoker() -> None:
    dispatch = _read(_ROOT / "mcp_platform" / "plugin_dispatch.py")
    tree = ast.parse(dispatch)
    invoke = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "invoke_plugin_tool"
        ),
        None,
    )
    assert invoke is not None
    body = ast.get_source_segment(dispatch, invoke) or ""
    assert "compile_plugin_tool_bindings" not in body
    assert "plugin_tool_bindings" not in body
    assert "invoke_tool" not in body
    from mcp_platform.plugin_dispatch import invoke_plugin_tool
    from mcp_platform.plugin_runtime import PluginRuntimeError as DispatchRuntimeError

    with pytest.raises(DispatchRuntimeError, match="plugin_tool_requires_plugin_operation"):
        invoke_plugin_tool("plugin__northwind.calendar__list_events", {"start": "tomorrow"})


def test_acceptance_does_not_talk_to_raw_mcp_client() -> None:
    source = _read(Path(__file__))
    tree = ast.parse(source)
    assert "_LivePeerBackend" not in {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "mcp"
        for alias in node.names
    }
    assert "Client" not in imported
    assert not any(
        isinstance(node, ast.Import) and any(alias.name == "mcp" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_provider_clients_stay_outside_generic_cognition() -> None:
    for path in (
        *_GENERIC_MODULES,
        _EXECUTE_RUN,
        _ROOT / "auto_loop" / "execute" / "plugin_tool.py",
    ):
        tree = ast.parse(_read(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert "mcp_platform.google_drive_api" not in imported
        assert "google_drive_api" not in imported


def _app_binding(app_id: str) -> PluginAppBinding:
    required = (app_id,)
    receipt = "receipt-1"
    revision = sha256_hex(
        canonical_bytes(
            {
                "required_app_ids": list(required),
                "optional_app_ids": [],
                "dependency_receipt_id": receipt,
            }
        )
    )
    return PluginAppBinding(
        required_app_ids=required,
        optional_app_ids=(),
        dependency_receipt_id=receipt,
        revision=revision,
    )


def _plugin_record(plugin_id: str, app_id: str, *, enabled: bool = True) -> InstalledPluginRecord:
    return InstalledPluginRecord(
        plugin_id=plugin_id,
        publisher_id="test.publisher",
        kind="mcp_backed",
        selected_version="1.0.0",
        enabled=enabled,
        status="installed",
        blocker=None,
        versions=(
            InstalledPluginVersionRecord(
                plugin_version="1.0.0",
                signing_key_id="test-plugin-key",
                manifest_sha256="4" * 64,
                package_sha256="5" * 64,
                state="selected",
            ),
        ),
        app_binding=_app_binding(app_id),
    )


def _plugin_snapshot(
    records: tuple[InstalledPluginRecord, ...],
) -> PluginRegistrySnapshot:
    by_id = {record.plugin_id: record for record in records}
    ordered = tuple(by_id[key] for key in sorted(by_id))
    revision = sha256_hex(
        canonical_bytes(
            {
                "schema_version": 2,
                "plugins": {
                    plugin_id: by_id[plugin_id].to_dict() for plugin_id in sorted(by_id)
                },
            }
        )
    )
    snapshot = object.__new__(PluginRegistrySnapshot)
    object.__setattr__(snapshot, "records", ordered)
    object.__setattr__(snapshot, "revision", revision)
    return snapshot


def _app_snapshot(records: tuple[InstalledMcpAppRecord, ...]) -> McpAppRegistrySnapshot:
    snapshot = object.__new__(McpAppRegistrySnapshot)
    object.__setattr__(snapshot, "records", records)
    object.__setattr__(snapshot, "revision", "2" * 64)
    return snapshot


def _requirement_for(
    *,
    app_id: str,
    deployment: str,
    transports: list[str],
    remote_origin: str | None,
    authorization_issuer: str | None,
    capabilities: list[dict[str, object]],
    provider_id: str | None = None,
) -> AppRequirement:
    return AppRequirement.from_dict(
        {
            "app_id": app_id,
            "publisher_id": "omega.first_party",
            "deployment": deployment,
            "provider_id": provider_id,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": transports,
            "remote_origin": remote_origin,
            "authorization_issuer": authorization_issuer,
            "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": capabilities,
        }
    )


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _signed_authority(tool_catalog) -> dict[str, object]:
    return {
        "tools": {
            tool.name: {
                "title": tool.title,
                "input_schema": _plain_json(tool.input_schema),
                "input_schema_sha256": tool.input_schema_sha256,
                "output_schema": _plain_json(tool.output_schema),
                "output_schema_sha256": tool.output_schema_sha256,
                "risk_class": tool.risk_class.value,
                "effect": tool.effect,
                "requires_revision": tool.requires_revision,
                "max_result_bytes": tool.max_result_bytes,
                "allowed_mime_types": list(tool.allowed_mime_types),
            }
            for tool in tool_catalog.tools
        },
        "resources": {},
        "resource_templates": {},
    }


def _app_and_descriptor(
    *,
    plugin: str,
) -> tuple[InstalledMcpAppRecord, McpConnectionDescriptor]:
    tool_catalog, _resources = catalogs_for(plugin)
    capabilities = [
        {
            "kind": "tool",
            "name": tool.name,
            "schema_sha256": tool.input_schema_sha256,
            "version": None,
        }
        for tool in tool_catalog.tools
    ]
    requirement = _requirement_for(
        app_id=tool_catalog.server_id,
        deployment="local",
        transports=["stdio"],
        remote_origin=None,
        authorization_issuer=None,
        capabilities=capabilities,
        provider_id="northwind",
    )
    projection = project_declared_capabilities(requirement)
    record = InstalledMcpAppRecord(
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
    descriptor = McpConnectionDescriptor(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=McpClientDeployment.LOCAL,
        provider_id=requirement.provider_id,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STDIO,
        verified_launch_sha256="7" * 64,
        remote_origin=requirement.remote_origin,
        authorization_issuer=requirement.authorization_issuer,
        authentication=requirement.authentication,
        declared_capabilities={
            f"{capability.kind.value}:{capability.name}": capability.schema_sha256
            for capability in requirement.capabilities
        },
        extension_versions={},
        capability_revision=projection.snapshot.capability_revision,
        limits=McpClientLimits(),
        signed_capability_authority=_signed_authority(tool_catalog),
    )
    return record, descriptor


def _compile_bindings(
    plugins: tuple[tuple[str, str, str], ...],
) -> tuple[PluginToolBinding, ...]:
    plugin_records = []
    app_records = []
    descriptors: dict[str, McpConnectionDescriptor] = {}
    for plugin_id, app_id, peer in plugins:
        plugin_records.append(_plugin_record(plugin_id, app_id))
        record, descriptor = _app_and_descriptor(plugin=peer)
        assert record.app_id == app_id
        app_records.append(record)
        descriptors[app_id] = descriptor
    return compile_plugin_tool_bindings(
        plugin_snapshot=_plugin_snapshot(tuple(plugin_records)),
        app_snapshot=_app_snapshot(tuple(app_records)),
        descriptor_for_app=lambda _plugin_id, app_id: descriptors[app_id],
        account_key_hash=_ACCOUNT,
    )


class _AllowEvaluate:
    def evaluate_operation(self, proposal, *, connection, authority, tool, **_kwargs):
        del proposal, connection, authority, tool
        return SimpleNamespace(allowed=True, reason="", obligations=())


class _PermissionStore:
    def __init__(self, binding: PluginToolBinding) -> None:
        self.binding = binding
        self.issued: PluginPermissionRequest | None = None
        self.consumed = False
        self._plan = None

    def issue(self, plan, **_kwargs):
        request = PluginPermissionRequest(
            request_id="e" * 48,
            episode_id=plan.episode_id,
            prior_episode_id=plan.episode_id,
            task_id="task-northwind",
            plan_id=plan.plan_id,
            account_key_hash=_ACCOUNT,
            plan_digest=plugin_plan_digest(plan),
            authority_digest=plan.authority_digest,
            display=PluginPermissionDisplay(
                plugin_apps=((TRACKER_PLUGIN, "Tracker", TRACKER_APP, "Tracker"),),
                effect_summary="Create data",
                ordered_operations=(self.binding.title,),
                audience="",
                destination="",
                crosses_providers=False,
                field_names=("summary",),
                risks=(RiskClass.ADDITIVE_WRITE.value,),
            ),
            expires_monotonic=10_000.0,
            revision="f" * 64,
        )
        self.issued = request
        return request

    def consume(self, grant_id: str, *, plan, authority_digest: str, account_key_hash: str):
        del grant_id, authority_digest, account_key_hash
        self.consumed = True
        step = plan.steps[0]
        binding = self.binding
        return (
            McpAuthoritySnapshot(
                authority_kind="direct_turn",
                authority_id="a" * 64,
                authority_revision="b" * 64,
                episode_id=plan.episode_id,
                job_id="",
                job_revision="",
                connection_id="connection-northwind",
                connection_revision=binding.connection_revision,
                server_id=TRACKER_APP,
                server_version="1.0.0",
                artifact_sha256="d" * 64,
                tool_name=binding.tool_name,
                tool_schema_sha256=binding.input_schema_sha256,
                capability_tags=(f"tool:{binding.capability}",),
                source_constraints={},
                destination_constraints={},
                risk_classes=(RiskClass.ADDITIVE_WRITE.value,),
                direct_execution=True,
                standing_execution=False,
                allow_send_publish=False,
                allow_destructive=False,
                allow_cross_provider=False,
                plan_digest=plugin_plan_digest(plan),
                account_key_hash=_ACCOUNT,
                candidate_id=step.candidate_id,
                plan_step_id=step.step_id,
                arguments_sha256=sha256_hex(canonical_bytes(dict(step.arguments))),
                connection_authority_revision="9" * 64,
                effect=binding.effect,
            ),
        )

    def approve_plan(self, plan):
        self._plan = plan
        return SimpleNamespace(
            grant_id="g" * 48,
            request_id=self.issued.request_id if self.issued else "e" * 48,
            episode_id=plan.episode_id,
            plan_digest=plugin_plan_digest(plan),
            authority_digest=plan.authority_digest,
            revision="h" * 64,
        )


class _AcceptanceRuntime:
    def __init__(
        self,
        bindings: tuple[PluginToolBinding, ...],
        *,
        plugin_runtime: PluginRuntime,
        permission_store: Any | None = None,
        connection_snapshot: McpConnectionSnapshot | None = None,
    ) -> None:
        self.bindings = bindings
        self.permission_store = permission_store
        self.connection_snapshot = connection_snapshot
        self.authority_service = _AllowEvaluate()
        self.plugin_runtime = plugin_runtime

    def plugin_tool_bindings(self) -> tuple[PluginToolBinding, ...]:
        return self.bindings


def _connection_snapshot(binding: PluginToolBinding) -> McpConnectionSnapshot:
    ceiling = (f"tool:{binding.capability}",)
    schemas = {
        binding.capability: binding.input_schema_sha256,
        binding.tool_name: binding.input_schema_sha256,
    }
    return McpConnectionSnapshot(
        connection_id="connection-northwind",
        connection_revision=binding.connection_revision,
        server_id=TRACKER_APP,
        server_version="1.0.0",
        artifact_sha256="d" * 64,
        provider_id="northwind",
        account_ref=binding.account_key_hash,
        enabled=True,
        capability_ceiling=ceiling,
        allowed_tool_schemas=schemas,
        source_constraints={},
        destination_constraints={},
        authority_revision="9" * 64,
    )


def _available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve_http_peer(app: object) -> tuple[uvicorn.Server, threading.Thread, int]:
    port = _available_port()
    owner = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="critical",
        )
    )
    thread = threading.Thread(target=owner.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not owner.started:
        time.sleep(0.01)
    if not owner.started:
        raise AssertionError("loopback HTTP peer did not start")
    return owner, thread, port


def _stop_http_peer(owner: uvicorn.Server, thread: threading.Thread) -> None:
    owner.should_exit = True
    thread.join(timeout=5.0)


def _fold_outputs(outcome) -> dict[str, Any]:
    plan = outcome.plan
    return screen_plugin_outputs(
        {
            "plugin_plan": {
                "plan_id": plan.plan_id if plan is not None else "",
                "episode_id": plan.episode_id if plan is not None else "",
                "objective_sha256": plan.objective_sha256 if plan is not None else "a" * 64,
                "authority_digest": plan.authority_digest if plan is not None else "b" * 64,
                "steps": [
                    {
                        "step_id": step.step_id,
                        "candidate_id": step.candidate_id,
                        "arguments": dict(step.arguments),
                        "consumes_step_ids": list(step.consumes_step_ids),
                    }
                    for step in (plan.steps if plan is not None else ())
                ],
            },
            "plugin_step_results": [
                {
                    "step_id": item.step_id,
                    "candidate_id": item.candidate_id,
                    "status": item.status,
                    "summary": item.summary.value
                    if hasattr(item.summary, "value")
                    else str(item.summary),
                }
                for item in outcome.step_results
            ],
            "plugin_result": dict(outcome.plugin_result or {}),
            "plugin_permission_request": (
                outcome.permission_request.to_hud_dict()
                if outcome.permission_request is not None
                else None
            ),
            "plugin_failure": outcome.failure_reason,
        }
    )


def test_calendar_peer_starts_through_current_mcp_client() -> None:
    _record, descriptor = _app_and_descriptor(plugin="calendar")
    peer = build_server("calendar")
    factory = SdkClientFactory.for_in_process_server(peer._server)

    async def exercise() -> None:
        client = CurrentMcpClient(descriptor, factory)
        try:
            receipt = await client.start()
            assert receipt.app_id == CALENDAR_APP
            assert receipt.source is DiscoverySource.LIVE_OBSERVED
            assert "list_events" in receipt.tools
        finally:
            await client.close()

    asyncio.run(exercise())


def test_lifecycle_drive_install_plus_unfamiliar_plugins_complete_generic_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", _OPERATOR_ACCOUNT)
    lifecycle = PluginLifecycleService(state_root=tmp_path / "lifecycle")
    review = lifecycle.review("google.workspace.drive")
    install = lifecycle.install("google.workspace.drive", confirmation=review.sha256)
    assert install.record.plugin_id == "google.workspace.drive"
    assert install.record.status == "installed"
    drive_descriptor = lifecycle.descriptor_for_app("google.workspace.drive.mcp")
    assert drive_descriptor.signed_capability_authority is not None
    assert "search_files" in drive_descriptor.signed_capability_authority["tools"]
    assert (
        compile_plugin_tool_bindings(
            plugin_snapshot=lifecycle.plugin_registry.snapshot(),
            app_snapshot=lifecycle.app_registry.snapshot(),
            descriptor_for_app=lifecycle.descriptor_for_plugin_app,
            account_key_hash=_ACCOUNT,
        )
        == ()
    )
    lifecycle._set_enabled("google.workspace.drive", True)
    drive_bindings = compile_plugin_tool_bindings(
        plugin_snapshot=lifecycle.plugin_registry.snapshot(),
        app_snapshot=lifecycle.app_registry.snapshot(),
        descriptor_for_app=lifecycle.descriptor_for_plugin_app,
        account_key_hash=_ACCOUNT,
    )
    assert {item.capability for item in drive_bindings} >= {"search_files"}

    installed = install_signed_northwind_plugins(
        tmp_path / "northwind",
        tracker_authentication_kind="oauth_authorization_code_pkce",
    )
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    calendar_descriptor = descriptor_from_installed(
        installed["app_registry"],
        installed["package_registry"],
        CALENDAR_APP,
    )
    tracker_descriptor = descriptor_from_installed(
        installed["app_registry"],
        installed["package_registry"],
        TRACKER_APP,
    )
    assert calendar_descriptor.deployment is McpClientDeployment.LOCAL
    assert calendar_descriptor.transport is McpTransport.STDIO
    assert calendar_descriptor.signed_capability_authority is not None
    assert tracker_descriptor.deployment is McpClientDeployment.REMOTE
    assert tracker_descriptor.transport is McpTransport.STREAMABLE_HTTP
    assert tracker_descriptor.remote_origin == TRACKER_ORIGIN
    assert tracker_descriptor.signed_capability_authority is not None

    bindings = compile_plugin_tool_bindings(
        plugin_snapshot=installed["plugin_registry"].snapshot(),
        app_snapshot=installed["app_registry"].snapshot(),
        descriptor_for_app=lambda _plugin_id, app_id: descriptor_from_installed(
            installed["app_registry"],
            installed["package_registry"],
            app_id,
        ),
        account_key_hash=_ACCOUNT,
    )
    assert {item.plugin_id for item in bindings} >= {CALENDAR_PLUGIN, TRACKER_PLUGIN, LEDGER_PLUGIN}
    assert retrieve_plugin_candidates("list tomorrow's northwind events", bindings)
    assert retrieve_plugin_candidates("create an issue from this summary", bindings)
    assert retrieve_plugin_candidates("show the northwind ledger balance", bindings)
    calendar_binding = next(item for item in bindings if item.plugin_id == CALENDAR_PLUGIN)
    tracker_binding = next(item for item in bindings if item.plugin_id == TRACKER_PLUGIN)

    calendar_peer = build_server("calendar")
    tracker_peer = build_server("tracker")
    tracker_http, tracker_thread, tracker_port = _serve_http_peer(
        tracker_peer.streamable_http_app(host="127.0.0.1")
    )
    tracker_loopback = f"http://127.0.0.1:{tracker_port}/mcp"
    tracker_endpoint = f"{TRACKER_ORIGIN}/mcp"

    async def _forward_tracker(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) != tracker_endpoint:
            raise AssertionError(str(request.url))
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.casefold()
            in {
                "accept",
                "content-type",
                "mcp-method",
                "mcp-name",
                "mcp-protocol-version",
            }
        }
        async with httpx2.AsyncClient() as forward:
            response = await forward.request(
                request.method,
                tracker_loopback,
                content=request.content,
                headers=headers,
            )
        return httpx2.Response(
            response.status_code,
            headers={"content-type": response.headers.get("content-type", "application/json")},
            content=response.content,
        )

    def _factory(descriptor):
        if descriptor.app_id == CALENDAR_APP:
            return SdkClientFactory.for_in_process_server(calendar_peer._server)
        if descriptor.app_id == TRACKER_APP:
            factory = build_streamable_http_transport(
                descriptor,
                StreamableHttpClientConfig._for_https_test(
                    httpx2.MockTransport(_forward_tracker)
                ),
            )
            assert factory.discovery_source is DiscoverySource.LIVE_AUTHENTICATED
            return factory
        raise AssertionError(descriptor.app_id)

    def _descriptor_for_app(_plugin_id: str, app_id: str) -> McpConnectionDescriptor:
        return descriptor_from_installed(
            installed["app_registry"],
            installed["package_registry"],
            app_id,
        )

    plugin_for_app = {
        CALENDAR_APP: CALENDAR_PLUGIN,
        TRACKER_APP: TRACKER_PLUGIN,
        LEDGER_APP: LEDGER_PLUGIN,
    }

    def _requirement_for_record(
        plugin_id: str,
        record: InstalledMcpAppRecord,
    ) -> AppRequirement:
        package = installed["plugin_registry"].reverify_installed(plugin_id)
        return next(
            requirement
            for requirement in package.manifest.required_apps
            if requirement.app_id == record.app_id
        )

    def _profile_match(plugin_id: str, record: InstalledMcpAppRecord) -> OAuthProfileMatchInput:
        plugin = installed["plugin_registry"].get(plugin_id)
        return OAuthProfileMatchInput(
            catalog_signer="omega.release",
            plugin_publisher=plugin.publisher_id,
            app_publisher=record.publisher_id,
            provider_id=record.provider_id,
            authorization_issuer=str(record.authorization_issuer),
            resource_origin=str(record.remote_origin),
        )

    def _direct_submit(operation, **_kwargs):
        return operation()

    async def _local_connector(plugin_id: str, record: InstalledMcpAppRecord):
        descriptor = _descriptor_for_app(plugin_id, record.app_id)
        client = CurrentMcpClient(descriptor, _factory(descriptor))
        await client.start()
        return client

    def _remote_client(descriptor, _authorization):
        return CurrentMcpClient(descriptor, _factory(descriptor))

    broker = _ObservedOAuthBroker()
    execution_worker = _ObservedExecutionWorker()
    connection_service = PluginConnectionService(
        app_registry=installed["app_registry"],
        plugin_registry=installed["plugin_registry"],
        broker=broker,
        grant_service=_TestGrantService(broker),
        descriptor_provider=_descriptor_for_app,
        requirement_provider=_requirement_for_record,
        offered_scopes_provider=lambda _plugin_id, _app_id: (
            "issues.read",
            "issues.write",
        ),
        operation_submitter=_direct_submit,
        profile_match_provider=_profile_match,
        client_factory=_remote_client,
        local_connector=_local_connector,
        package_registry=installed["package_registry"],
        transport_authority=object(),
        execution_worker=execution_worker,
    )

    _shared_loop_submit(
        lambda: connection_service.start(
            CALENDAR_PLUGIN,
            app_id=CALENDAR_APP,
            account_key=_OPERATOR_ACCOUNT,
            **full_connection_grant(connection_service, CALENDAR_PLUGIN, CALENDAR_APP),
        )
    )
    tracker_start = _shared_loop_submit(
        lambda: connection_service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=_OPERATOR_ACCOUNT,
            **full_connection_grant(connection_service, TRACKER_PLUGIN, TRACKER_APP),
        )
    )
    _shared_loop_submit(
        lambda: connection_service.complete(
            tracker_start.request_id or "",
            (
                "http://127.0.0.1/oauth/northwind-oauth/callback"
                "?code=x&state=callback-state"
            ),
        )
    )
    calendar_preparation = _shared_loop_submit(
        lambda: connection_service.prepare_execution_worker(
            CALENDAR_PLUGIN,
            app_id=CALENDAR_APP,
            account_key=_OPERATOR_ACCOUNT,
            capability_kind=calendar_binding.capability_kind,
            capability_id=calendar_binding.capability,
        )
    )
    tracker_preparation = _shared_loop_submit(
        lambda: connection_service.prepare_execution_worker(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=_OPERATOR_ACCOUNT,
            capability_kind=tracker_binding.capability_kind,
            capability_id=tracker_binding.capability,
        )
    )
    execution_authorities = {
        preparation.authority.app_id: preparation.authority
        for preparation in (calendar_preparation, tracker_preparation)
    }

    def _active_app_authority(
        _plugin_id: str,
        app_id: str,
        account_key_hash: str,
        _descriptor: McpConnectionDescriptor,
    ) -> PluginCatalogAppAuthority | None:
        authority = execution_authorities.get(app_id)
        if authority is None:
            return None
        return PluginCatalogAppAuthority(
            app_id=app_id,
            account_key_hash=account_key_hash,
            connection_revision=authority.connection_revision,
            authority_revision=authority.authority_revision,
            capability_revision=authority.capability_revision,
        )

    bindings = compile_plugin_tool_bindings(
        plugin_snapshot=installed["plugin_registry"].snapshot(),
        app_snapshot=installed["app_registry"].snapshot(),
        descriptor_for_app=_descriptor_for_app,
        account_key_hash=_ACCOUNT,
        app_authority_provider=_active_app_authority,
    )
    calendar_binding = next(item for item in bindings if item.plugin_id == CALENDAR_PLUGIN)
    tracker_binding = next(item for item in bindings if item.plugin_id == TRACKER_PLUGIN)
    backend = CurrentMcpPluginBackend(
        connection_service=connection_service,
        descriptor_provider=_descriptor_for_app,
        plugin_id_for_app=plugin_for_app,
        account_key_provider=lambda: _OPERATOR_ACCOUNT,
        operation_submitter=_shared_loop_submit,
        execution_worker=execution_worker,
    )
    plugin_runtime = PluginRuntime(
        plugin_registry=installed["plugin_registry"],
        app_registry=installed["app_registry"],
        backend=backend,
    )
    runtime = _AcceptanceRuntime(
        bindings,
        plugin_runtime=plugin_runtime,
        permission_store=_PermissionStore(tracker_binding),
        connection_snapshot=_connection_snapshot(tracker_binding),
    )
    try:
        read = plan_and_execute_plugin_request(
            "list tomorrow's northwind events",
            episode_id="episode-calendar",
            runtime_client=runtime,
        )
        assert read.status == "completed", (
            read.failure_reason,
            execution_worker.failures,
            execution_worker.worker.terminal_outcomes,
        )
        assert read.plugin_result is not None
        assert _EVENT_ITEM in str(dict(read.plugin_result))
        folded_read = _fold_outputs(read)
        assert folded_read["plugin_result"] is not None
        _fold_episode_auto(
            AutoInput(
                episode_id="episode-calendar",
                pressure={},
                source_action_goal={"episode_id": "episode-calendar", "intent": "APPLY"},
                execution_pack={
                    "episode_id": "episode-calendar",
                    "action": "PLUGIN_OPERATION",
                    "tool": "plugin_operation",
                    "execution_intent": "plugin",
                },
                trajectory_snapshot={},
            ),
            status="success",
            cycle_kind="success",
            memory_envelope=MemoryEnvelope(False, "skipped", None, (), (), None),
            model_profile="TOOL_EXECUTOR",
            model_id="test",
            prompt_pack=PromptPack(
                prompt_type="TOOL_CALL_SYSTEM",
                system_prompt="system",
                developer_prompt=None,
                user_prompt="user",
                context_blocks={},
                response_mode="summary",
                tool_call_spec=None,
                image_spec=None,
            ),
            exec_result={"outputs": folded_read, "failure_kind": None},
            runtime_ms=1,
        )

        write = plan_and_execute_plugin_request(
            "create an issue from this summary",
            episode_id="episode-tracker",
            runtime_client=runtime,
        )
        assert write.status == "permission_required", write.failure_reason
        assert write.permission_request is not None
        store = runtime.permission_store
        assert isinstance(store, _PermissionStore)
        grant = store.approve_plan(write.plan)
        resumed = plan_and_execute_plugin_request(
            "__approved_plugin_plan__",
            episode_id="episode-tracker",
            runtime_client=runtime,
            grant=grant,
            plan_override=write.plan,
        )
        assert resumed.status == "completed", resumed.failure_reason
        assert store.consumed is True
        assert _ISSUE_ITEM in str(dict(resumed.plugin_result or {}))

        for plugin_id, app_id in (
            (CALENDAR_PLUGIN, CALENDAR_APP),
            (TRACKER_PLUGIN, TRACKER_APP),
        ):
            _shared_loop_submit(
                lambda: connection_service.revoke(
                    plugin_id,
                    app_id=app_id,
                    account_key=_OPERATOR_ACCOUNT,
                ),
                timeout_s=20.0,
            )
            installed["plugin_registry"].set_enabled(
                plugin_id,
                False,
                expected_revision=installed["plugin_registry"].snapshot().revision,
            )
        installed["plugin_registry"].set_enabled(
            LEDGER_PLUGIN,
            False,
            expected_revision=installed["plugin_registry"].snapshot().revision,
        )
        disconnected = compile_plugin_tool_bindings(
            plugin_snapshot=installed["plugin_registry"].snapshot(),
            app_snapshot=installed["app_registry"].snapshot(),
            descriptor_for_app=lambda _plugin_id, app_id: descriptor_from_installed(
                installed["app_registry"],
                installed["package_registry"],
                app_id,
            ),
            account_key_hash=_ACCOUNT,
        )
        assert retrieve_plugin_candidates("show tomorrow's events", disconnected) == ()
        runtime.bindings = disconnected
        lost = plan_and_execute_plugin_request(
            "show tomorrow's events",
            episode_id="episode-disconnected",
            runtime_client=runtime,
        )
        assert lost.status == "ask_user"
        assert lost.failure_reason == "No eligible plugin operation was found."
    finally:
        backend.close()
        asyncio.run(connection_service.close_all())
        lifecycle.close()
        _stop_http_peer(tracker_http, tracker_thread)


def test_acceptance_fails_without_durable_app_binding() -> None:
    calendar_app, calendar_descriptor = _app_and_descriptor(plugin="calendar")
    del calendar_descriptor
    plugin = replace(_plugin_record(CALENDAR_PLUGIN, CALENDAR_APP), app_binding=None)
    bindings = compile_plugin_tool_bindings(
        plugin_snapshot=_plugin_snapshot((plugin,)),
        app_snapshot=_app_snapshot((calendar_app,)),
        descriptor_for_app=lambda _plugin_id, _app_id: _app_and_descriptor(plugin="calendar")[1],
        account_key_hash=_ACCOUNT,
    )
    assert bindings == ()
    assert retrieve_plugin_candidates("show tomorrow's events", bindings) == ()


def test_acceptance_fails_without_risk_permission_checks() -> None:
    tracker_app, _descriptor = _app_and_descriptor(plugin="tracker")
    bindings = _compile_bindings(((TRACKER_PLUGIN, TRACKER_APP, "tracker"),))

    class _NoCallBackend:
        calls: list[tuple[str, str, dict[str, Any]]] = []

        def call_tool_sync_for_plugin(self, plugin_id, app_id, name, arguments):
            self.calls.append((app_id, name, dict(arguments)))
            raise AssertionError("write executed without a permission card")

    runtime = _AcceptanceRuntime(
        bindings,
        plugin_runtime=PluginRuntime(
            plugin_registry=SimpleNamespace(
                get=lambda plugin_id: SimpleNamespace(status="installed", enabled=True)
            ),
            app_registry=SimpleNamespace(get=lambda app_id: tracker_app),
            backend=_NoCallBackend(),
        ),
        permission_store=None,
    )
    outcome = plan_and_execute_plugin_request(
        "create an issue from this summary",
        episode_id="episode-no-permission",
        runtime_client=runtime,
    )
    assert outcome.status == "failed"
    assert outcome.failure_reason == "plugin_permission_store_unavailable"
    assert runtime.plugin_runtime._backend.calls == []
