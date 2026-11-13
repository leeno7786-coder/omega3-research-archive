from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import os
import time
from types import SimpleNamespace

import pytest

from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpOperationKind,
    McpOperationRequest,
    McpTransport,
)
from mcp_platform.plugin_backend import CurrentMcpPluginBackend
from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_connections import PluginConnectionService
from mcp_platform.plugin_execution_worker import (
    PluginExecutionAuthority,
    PluginExecutionWorker,
    PluginExecutionWorkerError,
    execute_mcp_operation,
)
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from tests.helpers.plugin_consent import full_connection_grant
from tests.mcp_platform.plugin_execution_worker_round1_fixture import (
    discovery_handler,
)
from tests.mcp_platform.test_plugin_connections import (
    ACCOUNT_KEY,
    CALENDAR_APP,
    CALENDAR_PLUGIN,
    TRACKER_APP,
    TRACKER_PLUGIN,
    _Broker,
    _DiscoveryWorker,
    _GrantService,
    _direct_submit,
    _remote_service,
)
from tests.mcp_platform.generic_plugin_install import (
    descriptor_from_installed,
    install_signed_northwind_plugins,
)
from tests.mcp_platform.test_plugin_catalog import _descriptor


def _remote_descriptor():
    return replace(
        _descriptor(),
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin="https://workspace.example",
        authorization_issuer="https://auth.workspace.example",
    )


def _authority(
    descriptor,
    *,
    server_id: str | None = None,
    server_version: str | None = None,
) -> PluginExecutionAuthority:
    values = dict(
        plugin_id="example.plugin",
        app_id=descriptor.app_id,
        account_key_sha256="1" * 64,
        package_revision="2" * 64,
        connection_revision="3" * 64,
        authority_revision="4" * 64,
        capability_revision=descriptor.capability_revision,
        capability_kind="tool",
        capability_id="search",
    )
    if server_id is not None or server_version is not None:
        values.update(server_id=server_id, server_version=server_version)
    return PluginExecutionAuthority(**values)


def _receipt(descriptor) -> dict[str, object]:
    return {
        "app_id": descriptor.app_id,
        "publisher_id": descriptor.publisher_id,
        "deployment": descriptor.deployment.value,
        "transport": descriptor.transport.value,
        "protocol_version": descriptor.protocol_version,
        "source": "live_authenticated",
        "server_id": "workspace-server",
        "server_version": "1.0.0",
        "tools": {
            "search": {
                "input_schema_sha256": descriptor.signed_capability_authority[
                    "tools"
                ]["search"]["input_schema_sha256"],
                "output_schema_sha256": descriptor.signed_capability_authority[
                    "tools"
                ]["search"]["output_schema_sha256"],
            }
        },
        "prompts": {},
        "resources": {},
        "resource_templates": {},
        "extensions": {},
        "cache_metadata": {},
        "capability_revision": descriptor.capability_revision,
        "signed_capability_authority": dict(
            descriptor.signed_capability_authority
        ),
    }


def test_child_dispatcher_owns_remote_start_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform import client as client_module
    from mcp_platform.transports import streamable_http as transport_module

    descriptor = _remote_descriptor()
    receipt = _receipt(descriptor)
    observed: list[str] = []

    class _Receipt:
        def to_dict(self) -> dict[str, object]:
            return receipt

    class _Client:
        def __init__(self, supplied_descriptor, supplied_factory) -> None:
            assert supplied_descriptor == descriptor
            assert supplied_factory == "worker-transport"

        async def start(self):
            observed.append("start")
            return _Receipt()

        async def close(self) -> None:
            observed.append("close")

    monkeypatch.setattr(client_module, "CurrentMcpClient", _Client)
    monkeypatch.setattr(
        transport_module,
        "build_streamable_http_transport",
        lambda *_args: "worker-transport",
    )

    result = execute_mcp_operation(
        {
            "descriptor": descriptor.to_dict(),
            "discovery": True,
            "worker_authority": _authority(descriptor).to_dict(),
        },
        {},
    )

    assert result == {"discovery": receipt}
    assert observed == ["start", "close"]


def test_child_operation_rejects_server_identity_drift_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform import client as client_module
    from mcp_platform.transports import streamable_http as transport_module

    descriptor = _remote_descriptor()
    observed: list[str] = []

    class _Receipt:
        server_id = "replacement-server"
        server_version = "2.0.0"

    class _Client:
        def __init__(self, supplied_descriptor, supplied_factory) -> None:
            assert supplied_descriptor == descriptor
            assert supplied_factory == "worker-transport"

        async def start(self):
            observed.append("start")
            return _Receipt()

        async def invoke_plugin_tool(self, _request):
            observed.append("execute")
            raise AssertionError("identity drift reached the provider operation")

        async def close(self) -> None:
            observed.append("close")

    monkeypatch.setattr(client_module, "CurrentMcpClient", _Client)
    monkeypatch.setattr(
        transport_module,
        "build_streamable_http_transport",
        lambda *_args: "worker-transport",
    )
    request = McpOperationRequest(
        request_id="request-server-fence",
        app_id=descriptor.app_id,
        operation=McpOperationKind.TOOL_CALL,
        capability_id="tool:search",
        arguments={},
        capability_revision=descriptor.capability_revision,
        deadline_monotonic=time.monotonic() + 10.0,
        cancellation_id="cancel-server-fence",
        task_id=None,
        input_request_id=None,
        request_state=None,
    )

    with pytest.raises(ValueError, match="plugin_worker_authority_invalid"):
        execute_mcp_operation(
            {
                "descriptor": descriptor.to_dict(),
                "request": request.to_dict(),
                "worker_authority": _authority(
                    descriptor,
                    server_id="workspace-server",
                    server_version="1.0.0",
                ).to_dict(),
            },
            {},
        )

    assert observed == ["start", "close"]


@pytest.mark.anyio
async def test_remote_connect_discovery_runs_outside_desktop_pid(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "discovery-worker.pid"
    monkeypatch.setenv("OMEGA_TEST_PLUGIN_DISCOVERY_PID_PATH", str(marker))
    worker = PluginExecutionWorker(
        child_handler=discovery_handler,
        shutdown_timeout_seconds=2.0,
    )

    def parent_client(*_args, **_kwargs):
        raise AssertionError("remote discovery constructed a parent MCP client")

    service, broker, installed = _remote_service(
        tmp_path,
        authentication_kind="none",
        client_factory=parent_client,
        execution_worker=worker,
    )
    try:
        started = await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )
        await service.close_all()
        restarted, _broker, _installed = _remote_service(
            tmp_path,
            installed=installed,
            broker=broker,
            grants=service._grants,
            authentication_kind="none",
            client_factory=parent_client,
            execution_worker=worker,
        )
        context = await restarted.prepare_authority_context(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
        )
    finally:
        await service.close_all()
        if "restarted" in locals():
            await restarted.close_all()
        worker.close()

    assert started.status == "connected"
    assert context.server_id == TRACKER_APP
    assert int(marker.read_text(encoding="utf-8")) != os.getpid()


@pytest.mark.anyio
async def test_execution_authority_binds_the_fresh_worker_server_identity(
    tmp_path,
) -> None:
    class _ChangingDiscoveryWorker(_DiscoveryWorker):
        def __init__(self) -> None:
            self.calls = 0

        def run_sync(self, **kwargs):
            self.calls += 1
            result = super().run_sync(**kwargs)
            result["discovery"]["server_version"] = f"{self.calls}.0.0"
            return result

    worker = _ChangingDiscoveryWorker()

    def parent_client(*_args, **_kwargs):
        raise AssertionError("execution preparation performed parent provider I/O")

    service, _broker, _installed = _remote_service(
        tmp_path,
        authentication_kind="none",
        client_factory=parent_client,
        execution_worker=worker,
    )
    try:
        await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )
        descriptor = service._descriptor(TRACKER_PLUGIN, TRACKER_APP)
        capability_id = next(iter(descriptor.signed_capability_authority["tools"]))
        preparation = await service.prepare_execution_worker(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            capability_kind="tool",
            capability_id=capability_id,
        )
    finally:
        await service.close_all()

    assert worker.calls == 2
    assert preparation.authority.server_id == TRACKER_APP
    assert preparation.authority.server_version == "2.0.0"


@pytest.mark.anyio
async def test_local_connect_discovery_runs_outside_desktop_pid(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "local-discovery-worker.pid"
    monkeypatch.setenv("OMEGA_TEST_PLUGIN_DISCOVERY_PID_PATH", str(marker))
    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    broker = _Broker()
    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(
        path=tmp_path / "local-pid-authority.json"
    )
    worker = PluginExecutionWorker(
        child_handler=discovery_handler,
        shutdown_timeout_seconds=2.0,
    )

    def parent_client(*_args, **_kwargs):
        raise AssertionError("local discovery constructed a parent MCP client")

    service = PluginConnectionService(
        app_registry=installed["app_registry"],
        plugin_registry=installed["plugin_registry"],
        package_registry=installed["package_registry"],
        broker=broker,
        grant_service=grants,
        descriptor_provider=lambda _plugin_id, app_id: descriptor_from_installed(
            installed["app_registry"], installed["package_registry"], app_id
        ),
        requirement_provider=lambda _plugin_id, _record: (
            installed["plugin_registry"]
            .reverify_installed(CALENDAR_PLUGIN)
            .manifest.required_apps[0]
        ),
        offered_scopes_provider=lambda _plugin_id, _app_id: (),
        operation_submitter=_direct_submit,
        client_factory=parent_client,
        transport_authority=object(),
        execution_worker=worker,
    )
    try:
        started = await service.start(
            CALENDAR_PLUGIN,
            app_id=CALENDAR_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, CALENDAR_PLUGIN, CALENDAR_APP),
        )
    finally:
        await service.close_all()
        worker.close()

    assert started.status == "connected"
    assert int(marker.read_text(encoding="utf-8")) != os.getpid()


@pytest.mark.parametrize("discovery", [False, 1, "yes", {}, []])
def test_child_discovery_operation_rejects_non_exact_marker(discovery: object) -> None:
    descriptor = _remote_descriptor()

    with pytest.raises(ValueError, match="plugin_worker_payload_invalid"):
        execute_mcp_operation(
            {
                "descriptor": descriptor.to_dict(),
                "discovery": discovery,
                "worker_authority": _authority(descriptor).to_dict(),
            },
            {},
        )


class _CloseService:
    def __init__(self) -> None:
        self.close_calls = 0

    async def prepare_execution_worker(self, *_args, **_kwargs):
        raise AssertionError("close must not prepare an operation")

    async def close_all(self) -> None:
        self.close_calls += 1


class _RetryCloseWorker:
    def __init__(self) -> None:
        self.close_calls = 0

    def run_sync(self, **_kwargs):
        raise AssertionError("close must not execute an operation")

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise PluginExecutionWorkerError("plugin_worker_reap_failed")


def _submit(operation, **_kwargs):
    import asyncio

    return asyncio.run(operation())


def test_backend_reap_failure_remains_retryable_and_defers_connection_close() -> None:
    descriptor = _remote_descriptor()
    service = _CloseService()
    worker = _RetryCloseWorker()
    backend = CurrentMcpPluginBackend(
        connection_service=service,
        descriptor_provider=lambda _plugin_id, _app_id: descriptor,
        plugin_id_for_app={descriptor.app_id: "example.plugin"},
        account_key_provider=lambda: "account",
        operation_submitter=_submit,
        execution_worker=worker,
    )

    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_failed$",
    ):
        backend.close()

    assert worker.close_calls == 1
    assert service.close_calls == 0
    assert backend._closed is False

    backend.close()

    assert worker.close_calls == 2
    assert service.close_calls == 1
    assert backend._closed is True


def test_lifecycle_reap_failure_retains_owner_lease_until_exact_retry(
    tmp_path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    worker = _RetryCloseWorker()

    class _Backend:
        def close(self) -> None:
            worker.close()

    service.plugin_backend = _Backend()

    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_failed$",
    ):
        service.close()

    assert service._closed is False
    with pytest.raises(ValueError, match="plugin_lifecycle_operation_in_progress"):
        PluginLifecycleService(state_root=tmp_path)

    service.close()
    successor = PluginLifecycleService(state_root=tmp_path)
    successor.close()


def test_lifecycle_authority_context_uses_durable_grant_without_parent_client() -> None:
    descriptor = _remote_descriptor()
    account_key = "operator-account"
    account_digest = sha256(account_key.encode("utf-8")).hexdigest()
    durable = SimpleNamespace(
        connection_revision="3" * 64,
        revision="4" * 64,
        capability_ceiling=("tool:search",),
        source_constraints={},
        destination_constraints={},
    )
    grant = SimpleNamespace(
        authority=durable,
        activation=SimpleNamespace(package_revision="2" * 64),
    )
    binding = SimpleNamespace(
        plugin_id="example.plugin",
        app_id=descriptor.app_id,
        account_key_hash=account_digest,
        connection_revision=durable.connection_revision,
        authority_revision=durable.revision,
        tool_name="search",
        input_schema_sha256=descriptor.signed_capability_authority["tools"][
            "search"
        ]["input_schema_sha256"],
    )

    class _Connections:
        async def ensure_client(self, *_args, **_kwargs):
            raise AssertionError("authority resolution performed parent provider I/O")

        async def prepare_authority_context(self, *_args, **_kwargs):
            return SimpleNamespace(
                authority=durable,
                package_revision="2" * 64,
                server_id="worker-server",
                server_version="1.0.0",
            )

    service = object.__new__(PluginLifecycleService)
    service._connection_service = _Connections()
    service.plugin_grants = SimpleNamespace(current=lambda *_args, **_kwargs: grant)
    service.operator_account_key = lambda: account_key
    service.descriptor_for_plugin_app = lambda *_args: descriptor
    service.plugin_tool_bindings = lambda: (binding,)
    service._submit_connection_operation = lambda operation: __import__(
        "asyncio"
    ).run(operation())
    service.authority_service = object()

    connection, snapshot, authority_service, contributor = (
        service._authority_context_locked(None, binding, None)
    )

    assert connection is durable
    assert snapshot.connection_revision == durable.connection_revision
    assert snapshot.server_id == "worker-server"
    assert snapshot.server_version == "1.0.0"
    assert authority_service is service.authority_service
    assert contributor is None
