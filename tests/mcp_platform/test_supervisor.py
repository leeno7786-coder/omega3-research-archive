"""On-demand lifecycle tests for signed MCP child sessions."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import threading
from ctypes import wintypes
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp import types
from mcp.server import Server
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import mcp_platform.supervisor as supervisor_module
import mcp_platform.artifact_guard as artifact_guard_module
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client import CurrentMcpClient, SdkClientFactory
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpConnectionDescriptor,
    McpDiscoveryReceipt,
    McpOperationRequest,
    McpOperationResult,
    McpOperationStatus,
)
from mcp_platform.external_switch import ExternalMcpSwitch
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, NetworkClass
from mcp_platform.package_verifier import VerifiedPackage, verify_package
from mcp_platform.redaction import RedactingTextSink
from mcp_platform.registry import PackageRegistry
from mcp_platform.supervisor import (
    CRASH_BACKOFF_SECONDS,
    EXTERNAL_DRAIN_TIMEOUT_S,
    PERMANENT_BLOCK_CRASH_COUNT,
    SESSION_SHUTDOWN_TIMEOUT_S,
    SESSION_START_TIMEOUT_S,
    McpProcessSupervisor,
    McpServerBlocked,
    McpSupervisorError,
)
from mcp_platform.transports.stdio import VerifiedStdioLaunchGuard
from tests.mcp_platform.test_package_verifier import (
    build_signed_package as _build_signed_package,
    ephemeral_ring,
    host_tag,
    rewrite_and_resign_manifest,
)


_SIGNED_EFFECT_FOR_RISK = {
    "read_only": "read",
    "additive_write": "create",
    "mutating_write": "update",
    "destructive": "delete",
    "send_publish": "send",
    "cross_provider_transfer": "transfer",
}


def build_signed_package(
    root: Path,
    private: Ed25519PrivateKey,
    **kwargs: Any,
) -> Path:
    """Build the shared fixture with the current closed signed effect contract."""
    package = _build_signed_package(root, private, **kwargs)
    catalog_path = package / "tool_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    for tool in catalog["tools"]:
        tool["effect"] = _SIGNED_EFFECT_FOR_RISK[tool["risk_class"]]
    catalog_path.write_bytes(canonical_bytes(catalog))
    rewrite_and_resign_manifest(
        package,
        private,
        {"tool_catalog_sha256": sha256_hex(canonical_bytes(catalog))},
    )
    return package


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSession:
    def __init__(
        self,
        descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        errlog: RedactingTextSink,
        owner: FakeSessionFactory,
    ) -> None:
        self.descriptor = descriptor
        self.package = launch.verified_package
        self.launch = launch
        self.errlog = errlog
        self.owner = owner
        self.closed = False
        self.opened = False

    def _receipt(self) -> McpDiscoveryReceipt:
        return McpDiscoveryReceipt(
            app_id=self.descriptor.app_id,
            publisher_id=self.descriptor.publisher_id,
            deployment=self.descriptor.deployment,
            transport=self.descriptor.transport,
            protocol_version=MCP_PROTOCOL_VERSION,
            source=DiscoverySource.LIVE_OBSERVED,
            server_id=self.package.manifest.server_id,
            server_version=self.package.manifest.server_version,
            tools={},
            prompts={},
            resources={},
            resource_templates={},
            extensions={},
            cache_metadata={},
            capability_revision=self.descriptor.capability_revision,
        )

    async def start(self) -> McpDiscoveryReceipt:
        self.owner.start_threads.append(threading.current_thread())
        self.launch.close()
        self.owner.open_sessions += 1
        self.opened = True
        return self._receipt()

    async def call_tool(self, request: McpOperationRequest) -> McpOperationResult:
        return McpOperationResult(
            request_id=request.request_id,
            app_id=request.app_id,
            capability_id=request.capability_id,
            capability_revision=request.capability_revision,
            status=McpOperationStatus.COMPLETED,
            data={
                "content": [],
                "structured_content": {
                    "name": request.capability_id.removeprefix("tool:"),
                    "arguments": dict(request.arguments),
                },
                "is_error": False,
            },
            input_request=None,
            task_handle=None,
            error=None,
        )

    async def read_resource(self, request: McpOperationRequest) -> McpOperationResult:
        name = request.capability_id.removeprefix("resource:")
        static = next(
            (
                resource
                for resource in self.package.resource_catalog.resources
                if resource.name == name
            ),
            None,
        )
        uri = str(request.arguments.get("uri", static.uri if static else request.capability_id))
        return McpOperationResult(
            request_id=request.request_id,
            app_id=request.app_id,
            capability_id=request.capability_id,
            capability_revision=request.capability_revision,
            status=McpOperationStatus.COMPLETED,
            data={"contents": [{"uri": uri, "mime_type": "text/plain", "text": uri}]},
            input_request=None,
            task_handle=None,
            error=None,
        )

    async def close(self) -> None:
        self.launch.close()
        if not self.closed:
            self.closed = True
            if self.opened:
                self.owner.open_sessions -= 1


class FakeSessionFactory:
    def __init__(self) -> None:
        self.started: list[VerifiedStdioLaunchGuard] = []
        self.descriptors: list[McpConnectionDescriptor] = []
        self.start_threads: list[threading.Thread] = []
        self.open_sessions = 0

    def __call__(
        self,
        descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        errlog: RedactingTextSink,
    ) -> FakeSession:
        self.started.append(launch)
        self.descriptors.append(descriptor)
        return FakeSession(descriptor, launch, errlog, self)


class SignedPolicySessionFactory:
    """Run the real CurrentMcpClient against a controllable in-process signed server."""

    def __init__(self, fault: str) -> None:
        self.fault = fault
        self.descriptors: list[McpConnectionDescriptor] = []

    def __call__(
        self,
        descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        _errlog: RedactingTextSink,
    ) -> CurrentMcpClient:
        package = launch.verified_package
        launch.close()
        self.descriptors.append(descriptor)
        metadata = {
            "appId": descriptor.app_id,
            "publisherId": descriptor.publisher_id,
            "deployment": descriptor.deployment.value,
            "providerId": descriptor.provider_id,
            "protocolVersion": descriptor.protocol_version,
            "transport": descriptor.transport.value,
            "remoteOrigin": descriptor.remote_origin,
            "authorizationIssuer": descriptor.authorization_issuer,
            "io.modelcontextprotocol/serverInfo": {
                "name": package.manifest.server_id,
                "version": package.manifest.server_version,
            },
        }
        tool = package.tool_catalog.tools[0]
        resource = package.resource_catalog.resources[0]
        tool_document = tool.to_dict()

        async def discover(_ctx: Any, _params: types.RequestParams) -> types.DiscoverResult:
            return types.DiscoverResult(
                supportedVersions=[MCP_PROTOCOL_VERSION],
                capabilities=types.ServerCapabilities(
                    tools=types.ToolsCapability(),
                    resources=types.ResourcesCapability(),
                ),
                _meta=metadata,
            )

        async def list_tools(
            _ctx: Any,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListToolsResult:
            output_schema = (
                {"type": "object", "properties": {"forged": {"type": "boolean"}}}
                if self.fault == "output_schema"
                else tool_document["output_schema"]
            )
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name=tool.name,
                        inputSchema=tool_document["input_schema"],
                        outputSchema=output_schema,
                    )
                ],
                _meta=metadata,
            )

        async def call_tool(
            _ctx: Any,
            _params: types.CallToolRequestParams,
        ) -> types.CallToolResult:
            if self.fault == "tool_bytes":
                content: list[types.ContentBlock] = []
                structured_content = {"results": [{"value": "x" * 5000}]}
            else:
                content = [types.TextContent(text="x" * 8)]
                structured_content = {"results": []}
            return types.CallToolResult(
                content=content,
                structuredContent=structured_content,
                _meta=metadata,
            )

        async def list_resources(
            _ctx: Any,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListResourcesResult:
            return types.ListResourcesResult(
                resources=[
                    types.Resource(
                        name=resource.name,
                        uri=("omega://forged" if self.fault == "resource_uri" else resource.uri),
                        mimeType=(
                            "text/plain"
                            if self.fault == "resource_discovery_mime"
                            else resource.mime_type
                        ),
                    )
                ],
                _meta=metadata,
            )

        async def list_templates(
            _ctx: Any,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListResourceTemplatesResult:
            return types.ListResourceTemplatesResult(resourceTemplates=[], _meta=metadata)

        async def read_resource(
            _ctx: Any,
            params: types.ReadResourceRequestParams,
        ) -> types.ReadResourceResult:
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri=params.uri,
                        mimeType=(
                            "text/plain"
                            if self.fault == "resource_result_mime"
                            else resource.mime_type
                        ),
                        text="x" * (5000 if self.fault == "resource_bytes" else 8),
                    )
                ],
                _meta=metadata,
            )

        server = Server(
            package.manifest.server_id,
            version=package.manifest.server_version,
            on_list_tools=list_tools,
            on_call_tool=call_tool,
            on_list_resources=list_resources,
            on_list_resource_templates=list_templates,
            on_read_resource=read_resource,
        )
        server.add_request_handler("server/discover", types.RequestParams, discover)
        return CurrentMcpClient(
            descriptor,
            SdkClientFactory.for_in_process_server(server),
        )


class _LaunchFatal(BaseException):
    pass


class _FailingRetainedGuard:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("secondary retained-guard cleanup failure")


class _DualFaultSession:
    def __init__(self, primary: BaseException, launch: VerifiedStdioLaunchGuard) -> None:
        self.primary = primary
        self.launch = launch
        self.close_calls = 0

    async def start(self) -> McpDiscoveryReceipt:
        raise self.primary

    async def call_tool(self, _request: McpOperationRequest) -> McpOperationResult:
        raise AssertionError("unreachable")

    async def read_resource(self, _request: McpOperationRequest) -> McpOperationResult:
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.close_calls += 1
        self.launch.close()
        raise RuntimeError("secondary session cleanup failure")


class _DualFaultSessionFactory:
    def __init__(self, primary: BaseException) -> None:
        self.primary = primary
        self.session: _DualFaultSession | None = None

    def __call__(
        self,
        _descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        _errlog: RedactingTextSink,
    ) -> _DualFaultSession:
        self.session = _DualFaultSession(self.primary, launch)
        return self.session


class CancelledStartSession(FakeSession):
    async def start(self) -> McpDiscoveryReceipt:
        raise asyncio.CancelledError


class CancelledStartSessionFactory(FakeSessionFactory):
    def __call__(
        self,
        descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        errlog: RedactingTextSink,
    ) -> CancelledStartSession:
        self.started.append(launch)
        self.descriptors.append(descriptor)
        return CancelledStartSession(descriptor, launch, errlog, self)


class GatedSession(FakeSession):
    async def start(self) -> McpDiscoveryReceipt:
        self.owner.start_threads.append(threading.current_thread())
        gated_owner = self.owner
        assert isinstance(gated_owner, GatedSessionFactory)
        gated_owner.start_count += 1
        if gated_owner.start_count == 2:
            gated_owner.two_starts_entered.set()
        gated_owner.loop = asyncio.get_running_loop()
        if gated_owner.gate is None:
            gated_owner.gate = asyncio.Event()
        gated_owner.entered.set()
        await gated_owner.gate.wait()
        self.launch.close()
        gated_owner.open_sessions += 1
        self.opened = True
        return self._receipt()


class GatedSessionFactory(FakeSessionFactory):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.gate: asyncio.Event | None = None
        self.sessions: list[GatedSession] = []
        self.start_count = 0
        self.two_starts_entered = threading.Event()

    def __call__(
        self,
        descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        errlog: RedactingTextSink,
    ) -> GatedSession:
        self.started.append(launch)
        self.descriptors.append(descriptor)
        session = GatedSession(descriptor, launch, errlog, self)
        self.sessions.append(session)
        return session

    def release(self) -> None:
        assert self.loop is not None
        assert self.gate is not None
        self.loop.call_soon_threadsafe(self.gate.set)


class LateReadinessEvent:
    """Make wait report timeout only after the loop has published readiness."""

    def __init__(self) -> None:
        self.published = threading.Event()

    def clear(self) -> None:
        self.published.clear()

    def set(self) -> None:
        self.published.set()

    def is_set(self) -> bool:
        return self.published.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        assert self.published.wait(timeout=2.0)
        return False


class GatedCloseSession(FakeSession):
    async def close(self) -> None:
        if self.closed:
            return
        owner = self.owner
        assert isinstance(owner, GatedCloseSessionFactory)
        owner.close_started += 1
        if owner.close_started == owner.expected_closes:
            owner.all_closes_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            owner.cleanup_started += 1
            if owner.cleanup_started == owner.expected_closes:
                owner.all_cleanups_started.set()
            await asyncio.to_thread(owner.release_cleanup.wait, 2.0)
            await super().close()
            raise


class GatedCloseSessionFactory(FakeSessionFactory):
    def __init__(self, *, expected_closes: int) -> None:
        super().__init__()
        self.expected_closes = expected_closes
        self.close_started = 0
        self.cleanup_started = 0
        self.all_closes_started = threading.Event()
        self.all_cleanups_started = threading.Event()
        self.release_cleanup = threading.Event()

    def __call__(
        self,
        descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        errlog: RedactingTextSink,
    ) -> GatedCloseSession:
        self.started.append(launch)
        self.descriptors.append(descriptor)
        return GatedCloseSession(descriptor, launch, errlog, self)


class ShutdownGateSession(FakeSession):
    async def close(self) -> None:
        if self.closed:
            return
        owner = self.owner
        assert isinstance(owner, ShutdownGateSessionFactory)
        owner.close_calls += 1
        owner.loop = asyncio.get_running_loop()
        if owner.gate is None:
            owner.gate = asyncio.Event()
        owner.close_entered.set()
        await owner.gate.wait()
        await super().close()


class ShutdownGateSessionFactory(FakeSessionFactory):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0
        self.close_entered = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.gate: asyncio.Event | None = None

    def __call__(
        self,
        descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        errlog: RedactingTextSink,
    ) -> ShutdownGateSession:
        self.started.append(launch)
        self.descriptors.append(descriptor)
        return ShutdownGateSession(descriptor, launch, errlog, self)

    def release(self) -> None:
        if self.loop is not None and self.gate is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.gate.set)


class ReentrantShutdownSession(FakeSession):
    async def close(self) -> None:
        if self.closed:
            return
        owner = self.owner
        assert isinstance(owner, ReentrantShutdownSessionFactory)
        owner.close_entered.set()
        try:
            assert owner.supervisor is not None
            owner.callback_results.append(owner.supervisor.shutdown())
        except BaseException as exc:
            owner.callback_errors.append(exc)
        finally:
            owner.callback_returned.set()
        await super().close()


class ReentrantShutdownSessionFactory(FakeSessionFactory):
    def __init__(self) -> None:
        super().__init__()
        self.supervisor: McpProcessSupervisor | None = None
        self.close_entered = threading.Event()
        self.callback_returned = threading.Event()
        self.callback_results: list[dict[str, int]] = []
        self.callback_errors: list[BaseException] = []

    def __call__(
        self,
        descriptor: McpConnectionDescriptor,
        launch: VerifiedStdioLaunchGuard,
        errlog: RedactingTextSink,
    ) -> ReentrantShutdownSession:
        self.started.append(launch)
        self.descriptors.append(descriptor)
        return ReentrantShutdownSession(descriptor, launch, errlog, self)


Verifier = Callable[[Path], VerifiedPackage]


def _native_overwrite_existing(path: Path, payload: bytes) -> None:
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise ctypes.WinError()
    try:
        written = wintypes.DWORD()
        if not ctypes.windll.kernel32.WriteFile(
            handle,
            payload,
            len(payload),
            ctypes.byref(written),
            None,
        ):
            raise ctypes.WinError()
        if written.value != len(payload):
            raise OSError("short native write")
        if not ctypes.windll.kernel32.SetEndOfFile(handle):
            raise ctypes.WinError()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _native_replace_existing(path: Path, replacement: Path) -> None:
    if not ctypes.windll.kernel32.MoveFileExW(str(replacement), str(path), 0x1):
        raise ctypes.WinError()


def _native_delete_existing(path: Path) -> None:
    if not ctypes.windll.kernel32.DeleteFileW(str(path)):
        raise ctypes.WinError()


def _native_rename_existing(path: Path, destination: Path) -> None:
    if not ctypes.windll.kernel32.MoveFileExW(str(path), str(destination), 0):
        raise ctypes.WinError()


def _retarget_package(
    package: Path,
    private: Ed25519PrivateKey,
    *,
    server_id: str,
    network_class: NetworkClass,
    server_version: str | None = None,
) -> None:
    tools_path = package / "tool_catalog.json"
    tools = json.loads(tools_path.read_text(encoding="utf-8"))
    tools["server_id"] = server_id
    if server_version is not None:
        tools["server_version"] = server_version
    tools_path.write_bytes(canonical_bytes(tools))
    resources_path = package / "resource_catalog.json"
    resources = json.loads(resources_path.read_text(encoding="utf-8"))
    resources["server_id"] = server_id
    if server_version is not None:
        resources["server_version"] = server_version
    resources_path.write_bytes(canonical_bytes(resources))
    updates = {
        "server_id": server_id,
        "network_class": network_class.value,
        "tool_catalog_sha256": sha256_hex(canonical_bytes(tools)),
        "resource_catalog_sha256": sha256_hex(canonical_bytes(resources)),
    }
    if server_version is not None:
        updates["server_version"] = server_version
        updates["artifact_sha256"] = sha256_hex((package / "omega-mcp-fake.exe").read_bytes())
    rewrite_and_resign_manifest(
        package,
        private,
        updates,
    )


@pytest.fixture
def installed_packages(
    tmp_path: Path,
) -> tuple[PackageRegistry, ExternalMcpSwitch, list[Path]]:
    private, ring = ephemeral_ring()
    calls: list[Path] = []

    def verifier(package_dir: Path) -> VerifiedPackage:
        calls.append(Path(package_dir))
        return verify_package(
            package_dir,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )

    local_parent = tmp_path / "local-source"
    external_parent = tmp_path / "external-source"
    local_parent.mkdir()
    external_parent.mkdir()
    local = build_signed_package(local_parent, private, signing_key_id="test-release")
    external = build_signed_package(external_parent, private, signing_key_id="test-release")
    _retarget_package(
        local,
        private,
        server_id="omega.first_party.local",
        network_class=NetworkClass.LOCAL_ONLY,
    )
    _retarget_package(
        external,
        private,
        server_id="omega.first_party.external",
        network_class=NetworkClass.EXTERNAL,
    )
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(local)
    registry.install(external)
    registry.set_enabled(
        "omega.first_party.local",
        True,
        expected_revision=registry.snapshot().revision,
    )
    registry.set_enabled(
        "omega.first_party.external",
        True,
        expected_revision=registry.snapshot().revision,
    )
    calls.clear()
    switch = ExternalMcpSwitch(tmp_path / "mcp" / "external-switch.json")
    return registry, switch, calls


@pytest.fixture
def supervisor_parts(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
) -> Iterator[
    tuple[McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]]
]:
    registry, switch, verifier_calls = installed_packages
    session_factory = FakeSessionFactory()
    clock = FakeClock()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=session_factory,
        monotonic=clock,
    )
    supervisor.start()
    try:
        yield supervisor, registry, session_factory, clock, verifier_calls
    finally:
        if supervisor.thread_alive():
            supervisor.shutdown()


def _versioned_supervisor(
    tmp_path: Path,
    session_factory: FakeSessionFactory,
) -> tuple[
    McpProcessSupervisor,
    PackageRegistry,
    Ed25519PrivateKey,
    threading.Event,
]:
    private, ring = ephemeral_ring()
    replacement_verified = threading.Event()

    def verifier(package_dir: Path) -> VerifiedPackage:
        package = verify_package(
            package_dir,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )
        if package.manifest.server_version == "2.0.0":
            replacement_verified.set()
        return package

    source_parent = tmp_path / "version-1-source"
    source_parent.mkdir()
    source = build_signed_package(
        source_parent,
        private,
        signing_key_id="test-release",
    )
    _retarget_package(
        source,
        private,
        server_id="omega.first_party.local",
        network_class=NetworkClass.LOCAL_ONLY,
        server_version="1.0.0",
    )
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(source)
    registry.set_enabled(
        "omega.first_party.local",
        True,
        expected_revision=registry.snapshot().revision,
    )
    switch = ExternalMcpSwitch(tmp_path / "mcp" / "external-switch.json")
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=verifier,
        session_factory=session_factory,
        monotonic=FakeClock(),
    )
    supervisor.start()
    return supervisor, registry, private, replacement_verified


def _install_version_two(
    tmp_path: Path,
    registry: PackageRegistry,
    private: Ed25519PrivateKey,
) -> None:
    source_parent = tmp_path / "version-2-source"
    source_parent.mkdir()
    source = build_signed_package(
        source_parent,
        private,
        signing_key_id="test-release",
    )
    (source / "omega-mcp-fake.exe").write_bytes(b"version-two-artifact")
    _retarget_package(
        source,
        private,
        server_id="omega.first_party.local",
        network_class=NetworkClass.LOCAL_ONLY,
        server_version="2.0.0",
    )
    registry.install(source)
    registry.set_enabled(
        "omega.first_party.local",
        True,
        expected_revision=registry.snapshot().revision,
    )


def _external_supervisor(
    tmp_path: Path,
    *,
    server_count: int,
    session_factory: FakeSessionFactory,
) -> tuple[McpProcessSupervisor, PackageRegistry, ExternalMcpSwitch]:
    private, ring = ephemeral_ring()

    def verifier(package_dir: Path) -> VerifiedPackage:
        return verify_package(
            package_dir,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )

    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    for index in range(server_count):
        parent = tmp_path / f"external-{index}"
        parent.mkdir()
        package = build_signed_package(parent, private, signing_key_id="test-release")
        server_id = f"omega.first_party.external{index}"
        _retarget_package(
            package,
            private,
            server_id=server_id,
            network_class=NetworkClass.EXTERNAL,
        )
        registry.install(package)
        registry.set_enabled(
            server_id,
            True,
            expected_revision=registry.snapshot().revision,
        )
    switch = ExternalMcpSwitch(tmp_path / "mcp" / "external-switch.json")
    state = switch.load()
    switch.set_enabled(True, expected_revision=state.revision)
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=verifier,
        session_factory=session_factory,
        monotonic=FakeClock(),
    )
    supervisor.start()
    return supervisor, registry, switch


def test_lifecycle_constants_are_the_bounded_contract() -> None:
    assert SESSION_START_TIMEOUT_S == 10.0
    assert SESSION_SHUTDOWN_TIMEOUT_S == 2.0
    assert EXTERNAL_DRAIN_TIMEOUT_S == 2.0
    assert CRASH_BACKOFF_SECONDS == (1.0, 2.0, 4.0)
    assert PERMANENT_BLOCK_CRASH_COUNT == 3


def test_external_server_never_launches_while_switch_off(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, session_factory, _clock, _calls = supervisor_parts

    with pytest.raises(McpServerBlocked, match="^external_mcp_off$"):
        supervisor.ensure_session_sync("omega.first_party.external")

    assert session_factory.started == []


@pytest.mark.parametrize("operation", ["call_tool", "read_resource"])
def test_ready_external_session_is_closed_and_rejected_after_switch_turns_off(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
    operation: str,
) -> None:
    supervisor, registry, session_factory, _clock, _calls = supervisor_parts
    switch = ExternalMcpSwitch(registry.state_root / "external-switch.json")
    enabled = switch.set_enabled(True, expected_revision=switch.load().revision)
    supervisor.ensure_session_sync("omega.first_party.external")
    switch.set_enabled(False, expected_revision=enabled.revision)

    with pytest.raises(McpServerBlocked, match="^external_mcp_off$"):
        if operation == "call_tool":
            supervisor.call_tool_sync("omega.first_party.external", "omega.echo", {"value": 1})
        else:
            supervisor.read_resource_sync("omega.first_party.external", "omega://external/help")

    assert session_factory.open_sessions == 0
    assert "omega.first_party.external" not in supervisor.session_statuses_sync()


def test_local_session_remains_callable_while_external_switch_is_off(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, _factory, _clock, _calls = supervisor_parts
    supervisor.ensure_session_sync("omega.first_party.local")

    result = supervisor.call_tool_sync("omega.first_party.local", "omega.echo", {"value": 2})

    assert result == {"name": "omega.echo", "arguments": {"value": 2}}


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [("disable", "package_disabled"), ("block", "operator_blocked")],
)
def test_ready_session_rechecks_durable_enabled_and_blocker_before_dispatch(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
    mutation: str,
    reason: str,
) -> None:
    supervisor, registry, factory, _clock, _calls = supervisor_parts
    supervisor.ensure_session_sync("omega.first_party.local")
    if mutation == "disable":
        registry.set_enabled(
            "omega.first_party.local",
            False,
            expected_revision=registry.snapshot().revision,
        )
    else:
        registry.block("omega.first_party.local", "operator_blocked")

    with pytest.raises(McpServerBlocked, match=f"^{reason}$"):
        supervisor.call_tool_sync("omega.first_party.local", "omega.echo", {"value": 3})

    assert factory.open_sessions == 0


def test_local_only_server_launches_on_the_single_non_daemon_thread(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, session_factory, _clock, _calls = supervisor_parts

    snapshot = supervisor.ensure_session_sync("omega.first_party.local")

    assert snapshot.server_id == "omega.first_party.local"
    assert len(session_factory.start_threads) == 1
    assert session_factory.start_threads[0] is not threading.current_thread()
    assert session_factory.start_threads[0].daemon is False


def test_package_is_reverified_before_every_new_session(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, registry, session_factory, _clock, verifier_calls = supervisor_parts
    supervisor.ensure_session_sync("omega.first_party.local")
    supervisor.stop_server_sync("omega.first_party.local")
    record = registry.get("omega.first_party.local")
    (registry.package_path(record) / "omega-mcp-fake.exe").write_bytes(b"corrupt")

    with pytest.raises(McpServerBlocked, match="^artifact_digest_mismatch$"):
        supervisor.ensure_session_sync("omega.first_party.local")

    assert len(verifier_calls) == 3
    assert len(session_factory.started) == 1
    assert registry.get("omega.first_party.local").blocker == "artifact_digest_mismatch"


def test_supervisor_uses_registry_exact_tree_reverification_before_launch(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    """Catches launching an installed tree containing an undeclared executable or payload."""
    supervisor, registry, session_factory, _clock, _calls = supervisor_parts
    record = registry.get("omega.first_party.local")
    (registry.package_path(record) / "undeclared.bin").write_bytes(b"not signed")

    with pytest.raises(McpServerBlocked, match="^package_identity_collision$"):
        supervisor.ensure_session_sync("omega.first_party.local")

    assert session_factory.started == []
    assert registry.get("omega.first_party.local").blocker == "package_identity_collision"


def test_supervisor_projects_every_signed_tool_and_resource_policy_field(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    """Catches reducing a signed catalog to names and input-schema digests."""
    supervisor, _registry, session_factory, _clock, _calls = supervisor_parts

    supervisor.ensure_session_sync("omega.first_party.local")

    authority = session_factory.descriptors[0].signed_capability_authority
    package_tool = session_factory.started[0].verified_package.tool_catalog.tools[0]
    assert authority["tools"]["mail_search"] == {
        "title": package_tool.title,
        "input_schema": package_tool.input_schema,
        "input_schema_sha256": package_tool.input_schema_sha256,
        "output_schema": package_tool.output_schema,
        "output_schema_sha256": package_tool.output_schema_sha256,
        "risk_class": package_tool.risk_class.value,
        "effect": package_tool.effect,
        "requires_revision": package_tool.requires_revision,
        "max_result_bytes": package_tool.max_result_bytes,
        "allowed_mime_types": package_tool.allowed_mime_types,
    }
    assert authority["resources"]["Inbox"] == {
        "uri": "omega://mail/inbox",
        "mime_type": "application/json",
        "max_bytes": 4096,
    }


def test_signed_package_apps_metadata_projects_to_real_client_discovery(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        private, ring = ephemeral_ring()
        package_path = build_signed_package(
            tmp_path,
            private,
            signing_key_id="test-release",
        )
        apps_metadata = {
            "tools": {
                "mail_search": {
                    "resourceUri": "ui://mail/search",
                    "mimeType": "text/html",
                    "title": "Mail search",
                }
            },
            "resources": {"Inbox": {"resourceUri": "ui://mail/inbox", "title": "Inbox"}},
            "resource_templates": {},
        }
        rewrite_and_resign_manifest(
            package_path,
            private,
            {"mcp_apps_metadata": apps_metadata},
        )
        package = verify_package(
            package_path,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )
        descriptor = supervisor_module._local_descriptor(package)
        authority = descriptor.signed_capability_authority
        assert authority is not None
        assert (
            authority["mcp_apps_metadata"]["tools"]["mail_search"]
            == (apps_metadata["tools"]["mail_search"])
        )

        metadata = {
            "appId": descriptor.app_id,
            "publisherId": descriptor.publisher_id,
            "deployment": descriptor.deployment.value,
            "providerId": descriptor.provider_id,
            "protocolVersion": descriptor.protocol_version,
            "transport": descriptor.transport.value,
            "remoteOrigin": descriptor.remote_origin,
            "authorizationIssuer": descriptor.authorization_issuer,
        }
        tool = package.tool_catalog.tools[0]
        resource = package.resource_catalog.resources[0]

        async def discover(_ctx: Any, _params: types.RequestParams) -> types.DiscoverResult:
            return types.DiscoverResult(
                supportedVersions=[MCP_PROTOCOL_VERSION],
                capabilities=types.ServerCapabilities(
                    tools=types.ToolsCapability(),
                    resources=types.ResourcesCapability(),
                    extensions={
                        "io.modelcontextprotocol/apps": {
                            "version": "1.0.0",
                            "description": "MCP Apps metadata",
                        }
                    },
                ),
                _meta=metadata,
            )

        async def list_tools(
            _ctx: Any,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListToolsResult:
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name=tool.name,
                        inputSchema=tool.to_dict()["input_schema"],
                        outputSchema=tool.to_dict()["output_schema"],
                        _meta={"ui": apps_metadata["tools"][tool.name]},
                    )
                ],
                _meta=metadata,
            )

        async def list_resources(
            _ctx: Any,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListResourcesResult:
            return types.ListResourcesResult(
                resources=[
                    types.Resource(
                        name=resource.name,
                        uri=resource.uri,
                        mimeType=resource.mime_type,
                        _meta={"ui": apps_metadata["resources"][resource.name]},
                    )
                ],
                _meta=metadata,
            )

        async def list_resource_templates(
            _ctx: Any,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListResourceTemplatesResult:
            return types.ListResourceTemplatesResult(resourceTemplates=[], _meta=metadata)

        server = Server(
            package.manifest.server_id,
            version=package.manifest.server_version,
            on_list_tools=list_tools,
            on_list_resources=list_resources,
            on_list_resource_templates=list_resource_templates,
        )
        server.add_request_handler("server/discover", types.RequestParams, discover)
        client = CurrentMcpClient(
            descriptor,
            SdkClientFactory.for_in_process_server(server),
        )
        receipt = await client.start()
        assert receipt.tools[tool.name]["ui"] == apps_metadata["tools"][tool.name]
        assert receipt.resources[resource.name]["ui"] == (apps_metadata["resources"][resource.name])
        await client.close()

    asyncio.run(exercise())


def test_supervisor_rejects_signed_apps_metadata_for_an_orphan_catalog_identity(
    tmp_path: Path,
) -> None:
    private, ring = ephemeral_ring()
    package_path = build_signed_package(
        tmp_path,
        private,
        signing_key_id="test-release",
    )
    rewrite_and_resign_manifest(
        package_path,
        private,
        {
            "mcp_apps_metadata": {
                "tools": {"undeclared": {"resourceUri": "ui://mail/undeclared"}},
                "resources": {},
                "resource_templates": {},
            }
        },
    )
    package = verify_package(
        package_path,
        ring,
        omega_version="3.0.0",
        platform_tag=host_tag(),
    )

    with pytest.raises(McpSupervisorError, match="^signed_apps_metadata_invalid$"):
        supervisor_module._local_descriptor(package)


def test_empty_signed_apps_metadata_preserves_the_existing_descriptor_semantics(
    tmp_path: Path,
) -> None:
    private, ring = ephemeral_ring()
    package_path = build_signed_package(
        tmp_path,
        private,
        signing_key_id="test-release",
    )
    rewrite_and_resign_manifest(
        package_path,
        private,
        {
            "mcp_apps_metadata": {
                "tools": {},
                "resources": {},
                "resource_templates": {},
            }
        },
    )
    package = verify_package(
        package_path,
        ring,
        omega_version="3.0.0",
        platform_tag=host_tag(),
    )
    descriptor = supervisor_module._local_descriptor(package)

    assert descriptor.extension_versions == {}
    assert descriptor.signed_capability_authority is not None
    assert "mcp_apps_metadata" not in descriptor.signed_capability_authority


@pytest.mark.parametrize(
    "fault",
    ["output_schema", "resource_uri", "resource_discovery_mime"],
)
def test_supervisor_current_client_rejects_signed_discovery_drift(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
    fault: str,
) -> None:
    """Catches runtime catalogs replacing signed output-schema, URI, or MIME authority."""
    supervisor, _registry, _factory, _clock, _calls = supervisor_parts
    supervisor._session_factory = SignedPolicySessionFactory(fault)

    with pytest.raises(McpServerBlocked, match="^capability_drift$"):
        supervisor.ensure_session_sync("omega.first_party.local")


@pytest.mark.parametrize(
    ("fault", "operation", "reason"),
    [
        ("tool_bytes", "tool", "result_limit_exceeded"),
        ("tool_mime", "tool", "result_invalid"),
        ("resource_bytes", "resource", "result_limit_exceeded"),
        ("resource_result_mime", "resource", "result_invalid"),
    ],
)
def test_supervisor_current_client_enforces_signed_result_policy(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
    fault: str,
    operation: str,
    reason: str,
) -> None:
    """Catches falling back to generic byte caps or trusting result MIME at dispatch."""
    supervisor, _registry, _factory, _clock, _calls = supervisor_parts
    supervisor._session_factory = SignedPolicySessionFactory(fault)
    supervisor.ensure_session_sync("omega.first_party.local")

    with pytest.raises(McpSupervisorError, match=f"^{reason}$"):
        if operation == "tool":
            supervisor.call_tool_sync(
                "omega.first_party.local",
                "mail_search",
                {"query": "planning"},
            )
        else:
            supervisor.read_resource_sync(
                "omega.first_party.local",
                "omega://mail/inbox",
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle semantics")
def test_post_verification_native_artifact_mutation_fails_before_session_factory(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, session_factory, _clock, _calls = supervisor_parts
    verify_installed = supervisor._verify_installed_record

    def mutate_after_verification(record: object) -> VerifiedPackage:
        package = verify_installed(record)  # type: ignore[arg-type]
        _native_overwrite_existing(package.artifact_path, b"unsigned replacement")
        return package

    supervisor._verify_installed_record = mutate_after_verification  # type: ignore[method-assign]

    with pytest.raises(McpServerBlocked, match="^package_identity_collision$"):
        supervisor.ensure_session_sync("omega.first_party.local")

    assert session_factory.started == []


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle semantics")
def test_post_verification_native_artifact_delete_fails_content_free(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, session_factory, _clock, _calls = supervisor_parts
    verify_installed = supervisor._verify_installed_record

    def delete_after_verification(record: object) -> VerifiedPackage:
        package = verify_installed(record)  # type: ignore[arg-type]
        _native_delete_existing(package.artifact_path)
        return package

    supervisor._verify_installed_record = delete_after_verification  # type: ignore[method-assign]

    with pytest.raises(McpServerBlocked, match="^package_identity_collision$"):
        supervisor.ensure_session_sync("omega.first_party.local")

    assert session_factory.started == []


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle semantics")
def test_artifact_guard_releases_handle_when_native_identity_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "guarded.exe"
    artifact.write_bytes(b"signed artifact")
    expected_component_identities = (
        artifact_guard_module._capture_artifact_identity_chain(tmp_path, artifact)
    )
    original = artifact_guard_module._file_information
    calls = 0

    def fail_artifact_identity(handle: int) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise artifact_guard_module.ArtifactGuardError("artifact_guard_unavailable")
        return original(handle)

    monkeypatch.setattr(artifact_guard_module, "_file_information", fail_artifact_identity)

    with pytest.raises(
        artifact_guard_module.ArtifactGuardError,
        match="^artifact_guard_unavailable$",
    ):
        artifact_guard_module.ArtifactLaunchGuard.acquire(
            package_root=tmp_path,
            artifact_path=artifact,
            expected_sha256=sha256_hex(artifact.read_bytes()),
            expected_identity=(0, 0),
            expected_component_identities=expected_component_identities,
        )

    _native_overwrite_existing(artifact, b"released after failure")
    assert artifact.read_bytes() == b"released after failure"


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle semantics")
def test_guard_blocks_native_mutation_until_session_start_returns(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
    tmp_path: Path,
) -> None:
    supervisor, registry, _factory, _clock, _calls = supervisor_parts
    gated = GatedSessionFactory()
    supervisor._session_factory = gated
    record = registry.get("omega.first_party.local")
    artifact = registry.package_path(record) / "omega-mcp-fake.exe"
    replacement = tmp_path / "replacement.exe"
    replacement.write_bytes(b"unsigned replacement")
    outcomes: list[object] = []

    def launch() -> None:
        try:
            outcomes.append(supervisor.ensure_session_sync("omega.first_party.local"))
        except BaseException as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=launch)
    thread.start()
    try:
        assert gated.entered.wait(2.0)
        with pytest.raises(OSError):
            _native_overwrite_existing(artifact, b"unsigned in-place")
        with pytest.raises(OSError):
            _native_replace_existing(artifact, replacement)
        with pytest.raises(OSError):
            _native_delete_existing(artifact)
        with pytest.raises(OSError):
            _native_rename_existing(
                artifact.parent,
                artifact.parent.with_name("renamed-package"),
            )

        gated.release()
        thread.join(3.0)
        assert not thread.is_alive()
        assert len(outcomes) == 1
        assert getattr(outcomes[0], "state", None) == "ready"

        _native_overwrite_existing(artifact, b"guard released")
        assert artifact.read_bytes() == b"guard released"
    finally:
        if thread.is_alive():
            gated.release()
            thread.join(3.0)


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle semantics")
def test_artifact_guard_releases_when_session_start_is_cancelled(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, registry, _factory, _clock, _calls = supervisor_parts
    supervisor._session_factory = CancelledStartSessionFactory()
    record = registry.get("omega.first_party.local")
    artifact = registry.package_path(record) / "omega-mcp-fake.exe"

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await supervisor.ensure_session("omega.first_party.local")

    asyncio.run(exercise())

    _native_overwrite_existing(artifact, b"released after cancellation")
    assert artifact.read_bytes() == b"released after cancellation"


@pytest.mark.parametrize("kind", ["error", "cancel", "keyboard", "fatal"])
def test_launch_dual_fault_preserves_primary_and_attempts_every_cleanup(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Catches guard cleanup replacing the start failure or skipping session cleanup."""
    supervisor, _registry, _factory, _clock, _calls = supervisor_parts
    primary: BaseException
    if kind == "error":
        primary = OSError("untrusted child failure")
    elif kind == "cancel":
        primary = asyncio.CancelledError("launch cancelled")
    elif kind == "keyboard":
        primary = KeyboardInterrupt("launch interrupted")
    else:
        primary = _LaunchFatal("fatal launch boundary")
    factory = _DualFaultSessionFactory(primary)
    retained = _FailingRetainedGuard()
    supervisor._session_factory = factory
    monkeypatch.setattr(
        artifact_guard_module.ArtifactLaunchGuard,
        "acquire",
        classmethod(lambda _cls, **_kwargs: retained),
    )

    async def exercise() -> BaseException:
        try:
            await supervisor.ensure_session("omega.first_party.local")
        except BaseException as caught:
            return caught
        raise AssertionError("launch unexpectedly succeeded")

    caught = asyncio.run(exercise())
    if kind == "error":
        assert type(caught) is McpServerBlocked
        assert str(caught) == "session_start_failed"
    else:
        assert caught is primary
    assert factory.session is not None
    assert factory.session.close_calls == 1
    assert retained.close_calls >= 1
    assert supervisor.session_statuses_sync() == {}


def test_reconcile_package_reverifies_ready_session_and_blocks_changed_bytes(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, registry, session_factory, _clock, verifier_calls = supervisor_parts
    supervisor.ensure_session_sync("omega.first_party.local")
    record = registry.get("omega.first_party.local")
    (registry.package_path(record) / "omega-mcp-fake.exe").write_bytes(b"corrupt")

    with pytest.raises(McpServerBlocked, match="^artifact_digest_mismatch$"):
        supervisor.reconcile_package_sync("omega.first_party.local")

    assert len(verifier_calls) == 3
    assert len(session_factory.started) == 1
    assert session_factory.open_sessions == 0
    assert supervisor.session_statuses_sync() == {}
    assert registry.get("omega.first_party.local").blocker == "artifact_digest_mismatch"


def test_ineligibility_cleanup_rethrows_exact_non_exception_baseexception(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, registry, _factory, _clock, _calls = supervisor_parts
    supervisor.ensure_session_sync("omega.first_party.local")
    record = registry.get("omega.first_party.local")
    registry.set_enabled(
        record.server_id,
        False,
        expected_revision=registry.snapshot().revision,
    )
    sentinel = KeyboardInterrupt("eligibility-cleanup-sentinel")
    session = supervisor._sessions[record.server_id].session

    async def fail_close() -> None:
        raise sentinel

    session.close = fail_close  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt) as raised:
        supervisor.ensure_session_sync(record.server_id)
    assert raised.value is sentinel


def test_unexpected_exit_cleanup_rethrows_exact_non_exception_baseexception(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, registry, _factory, _clock, _calls = supervisor_parts
    supervisor.ensure_session_sync("omega.first_party.local")
    record = registry.get("omega.first_party.local")
    sentinel = _LaunchFatal("unexpected-exit-cleanup-sentinel")
    session = supervisor._sessions[record.server_id].session

    async def fail_close() -> None:
        raise sentinel

    session.close = fail_close  # type: ignore[method-assign]
    with pytest.raises(_LaunchFatal) as raised:
        supervisor.record_unexpected_exit_sync(record.server_id, exit_code=9)
    assert raised.value is sentinel


def test_supervisor_constructs_stderr_sink_with_exact_descriptor_limit(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    supervisor, _registry, factory, _clock, _calls = supervisor_parts
    original = supervisor_module._local_descriptor

    def bounded_descriptor(package: VerifiedPackage) -> McpConnectionDescriptor:
        descriptor = original(package)
        return replace(
            descriptor,
            limits=replace(descriptor.limits, max_stderr_bytes=17),
        )

    monkeypatch.setattr(supervisor_module, "_local_descriptor", bounded_descriptor)
    supervisor.ensure_session_sync("omega.first_party.local")
    session = supervisor._sessions["omega.first_party.local"].session
    assert isinstance(session, FakeSession)
    assert session.errlog.max_bytes == 17


def test_reconcile_package_blocks_ready_session_when_verifier_errors(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, registry, session_factory, _clock, _calls = supervisor_parts
    supervisor.ensure_session_sync("omega.first_party.local")

    def failed_verifier(_package_root: Path) -> VerifiedPackage:
        raise RuntimeError("secret verifier detail")

    supervisor._verifier = failed_verifier

    with pytest.raises(McpServerBlocked, match="^package_verification_failed$"):
        supervisor.reconcile_package_sync("omega.first_party.local")

    assert session_factory.open_sessions == 0
    assert supervisor.session_statuses_sync() == {}
    assert registry.get("omega.first_party.local").blocker == "package_verification_failed"


def test_reconcile_package_reuses_valid_ready_session_without_restart(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, session_factory, _clock, verifier_calls = supervisor_parts
    original = supervisor.ensure_session_sync("omega.first_party.local")

    reconciled = supervisor.reconcile_package_sync("omega.first_party.local")

    assert reconciled == original
    assert len(verifier_calls) == 3
    assert len(session_factory.started) == 1
    assert session_factory.open_sessions == 1


def test_reconcile_package_replaces_ready_session_when_launch_identity_changes(
    tmp_path: Path,
) -> None:
    factory = FakeSessionFactory()
    supervisor, registry, private, _verified = _versioned_supervisor(
        tmp_path,
        factory,
    )
    try:
        original = supervisor.ensure_session_sync("omega.first_party.local")
        _install_version_two(tmp_path, registry, private)
        replacement = registry.get("omega.first_party.local")

        reconciled = supervisor.reconcile_package_sync("omega.first_party.local")

        assert original.tool_catalog_sha256 != replacement.tool_catalog_sha256
        assert reconciled.tool_catalog_sha256 == replacement.tool_catalog_sha256
        assert reconciled.resource_catalog_sha256 == replacement.resource_catalog_sha256
        assert len(factory.started) == 2
        assert factory.started[0].verified_package.manifest.server_version == "1.0.0"
        assert factory.started[1].verified_package.manifest.server_version == "2.0.0"
        assert factory.open_sessions == 1
    finally:
        supervisor.shutdown()


def test_inflight_launch_identity_change_closes_old_launch_and_fails_closed(
    tmp_path: Path,
) -> None:
    factory = GatedSessionFactory()
    supervisor, registry, private, replacement_verified = _versioned_supervisor(
        tmp_path,
        factory,
    )
    first_errors: list[BaseException] = []
    reconcile_errors: list[BaseException] = []

    def first_launch() -> None:
        try:
            supervisor.ensure_session_sync("omega.first_party.local")
        except BaseException as exc:
            first_errors.append(exc)

    def reconcile() -> None:
        try:
            supervisor.reconcile_package_sync("omega.first_party.local")
        except BaseException as exc:
            reconcile_errors.append(exc)

    first = threading.Thread(target=first_launch)
    retry = threading.Thread(target=reconcile)
    try:
        first.start()
        assert factory.entered.wait(2.0)
        _install_version_two(tmp_path, registry, private)
        replacement_verified.clear()
        retry.start()
        assert replacement_verified.wait(2.0)
        factory.release()
        first.join(3.0)
        retry.join(3.0)

        assert not first.is_alive()
        assert not retry.is_alive()
        assert [str(error) for error in first_errors] == ["package_identity_changed"]
        assert [str(error) for error in reconcile_errors] == ["package_identity_changed"]
        assert factory.open_sessions == 0
        assert supervisor.session_statuses_sync() == {}

        reconciled = supervisor.reconcile_package_sync("omega.first_party.local")
        replacement = registry.get("omega.first_party.local")
        assert reconciled.tool_catalog_sha256 == replacement.tool_catalog_sha256
        assert len(factory.started) == 2
        assert factory.open_sessions == 1
    finally:
        if first.is_alive() or retry.is_alive():
            factory.release()
            first.join(3.0)
            retry.join(3.0)
        supervisor.shutdown()


def test_launch_environment_has_identity_and_root_but_no_credentials(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, registry, session_factory, _clock, _calls = supervisor_parts
    monkeypatch.setenv("OMEGA_TEST_TOKEN", "must-not-cross-boundary")

    supervisor.ensure_session_sync("omega.first_party.local")

    launch = session_factory.started[0]
    record = registry.get("omega.first_party.local")
    assert dict(launch.environment) == {
        "OMEGA_MCP_SERVER_ID": "omega.first_party.local",
        "OMEGA_MCP_PACKAGE_ROOT": str(registry.package_path(record).resolve()),
    }
    assert "must-not-cross-boundary" not in repr(launch.environment)


def test_three_unexpected_exits_persistently_block_package(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, registry, _factory, clock, _calls = supervisor_parts
    for expected_count in (1, 2, 3):
        supervisor.ensure_session_sync("omega.first_party.local")
        supervisor.record_unexpected_exit_sync("omega.first_party.local", exit_code=7)
        record = registry.get("omega.first_party.local")
        assert record.crash_count == expected_count
        assert record.blocker == ("repeated_crash" if expected_count == 3 else None)
        if expected_count < 3:
            with pytest.raises(McpServerBlocked, match="^crash_backoff$"):
                supervisor.ensure_session_sync("omega.first_party.local")
        clock.advance(2 ** (expected_count - 1))

    with pytest.raises(McpServerBlocked, match="^repeated_crash$"):
        supervisor.ensure_session_sync("omega.first_party.local")


def test_explicit_registry_clear_removes_persistent_block_and_backoff(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, registry, _factory, _clock, _calls = supervisor_parts
    for _attempt in range(PERMANENT_BLOCK_CRASH_COUNT):
        supervisor.ensure_session_sync("omega.first_party.local")
        supervisor.record_unexpected_exit_sync("omega.first_party.local", exit_code=7)
        if registry.get("omega.first_party.local").blocker is None:
            _clock.advance(CRASH_BACKOFF_SECONDS[_attempt])
    registry.clear_block("omega.first_party.local")

    snapshot = supervisor.ensure_session_sync("omega.first_party.local")

    assert snapshot.state == "ready"
    assert registry.get("omega.first_party.local").crash_count == 0


def test_external_drain_closes_external_sessions_but_preserves_local_sessions(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, session_factory, _clock, _calls = supervisor_parts
    switch = ExternalMcpSwitch(_registry.state_root / "external-switch.json")
    state = switch.load()
    enabled = switch.set_enabled(True, expected_revision=state.revision)
    supervisor.ensure_session_sync("omega.first_party.local")
    supervisor.ensure_session_sync("omega.first_party.external")
    switch.set_enabled(False, expected_revision=enabled.revision)

    result = supervisor.stop_external_sync(EXTERNAL_DRAIN_TIMEOUT_S)

    statuses = supervisor.session_statuses_sync()
    assert result == {"sessions_closed": 1, "sessions_forced": 0, "errors": 0}
    assert set(statuses) == {"omega.first_party.local"}
    assert session_factory.open_sessions == 1


def test_external_drain_cancels_inflight_external_but_preserves_local_launch(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = GatedSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=factory,
        monotonic=FakeClock(),
    )
    supervisor.start()
    enabled = switch.set_enabled(True, expected_revision=switch.load().revision)

    async def exercise() -> None:
        external = asyncio.create_task(supervisor.ensure_session("omega.first_party.external"))
        assert await asyncio.to_thread(factory.entered.wait, 2.0)
        local = asyncio.create_task(supervisor.ensure_session("omega.first_party.local"))
        assert await asyncio.to_thread(factory.two_starts_entered.wait, 2.0)
        switch.set_enabled(False, expected_revision=enabled.revision)
        result = await supervisor.stop_external(EXTERNAL_DRAIN_TIMEOUT_S)
        try:
            assert result == {
                "sessions_closed": 0,
                "sessions_forced": 0,
                "errors": 0,
            }
            assert external.done()
            assert external.cancelled()
            assert local.done() is False
            assert "omega.first_party.external" not in supervisor._launches
            assert "omega.first_party.local" in supervisor._launches
        finally:
            factory.release()
            await asyncio.gather(external, local, return_exceptions=True)

    try:
        asyncio.run(exercise())
        statuses = supervisor.session_statuses_sync()
        assert set(statuses) == {"omega.first_party.local"}
        assert factory.open_sessions == 1
    finally:
        if supervisor.thread_alive():
            supervisor.shutdown()


def test_external_launch_rechecks_switch_before_publishing_ready_session(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = GatedSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=factory,
        monotonic=FakeClock(),
    )
    supervisor.start()
    enabled = switch.set_enabled(True, expected_revision=switch.load().revision)

    async def exercise() -> None:
        launch = asyncio.create_task(supervisor.ensure_session("omega.first_party.external"))
        assert await asyncio.to_thread(factory.entered.wait, 2.0)
        switch.set_enabled(False, expected_revision=enabled.revision)
        factory.release()
        with pytest.raises(McpServerBlocked, match="^external_mcp_off$"):
            await launch

    try:
        asyncio.run(exercise())
        assert supervisor.session_statuses_sync() == {}
        assert factory.open_sessions == 0
    finally:
        if supervisor.thread_alive():
            supervisor.shutdown()


def test_external_drain_uses_one_deadline_and_awaits_forced_sdk_cleanup(
    tmp_path: Path,
) -> None:
    factory = GatedCloseSessionFactory(expected_closes=2)
    supervisor, _registry, switch = _external_supervisor(
        tmp_path,
        server_count=2,
        session_factory=factory,
    )
    supervisor.ensure_session_sync("omega.first_party.external0")
    supervisor.ensure_session_sync("omega.first_party.external1")
    enabled = switch.load()
    switch.set_enabled(False, expected_revision=enabled.revision)

    async def exercise() -> dict[str, int]:
        drain = asyncio.create_task(supervisor.stop_external(0.1))
        try:
            assert await asyncio.to_thread(factory.all_closes_started.wait, 0.2)
            assert await asyncio.to_thread(factory.all_cleanups_started.wait, 0.3)
            assert drain.done() is False
        finally:
            factory.release_cleanup.set()
        return await drain

    try:
        result = asyncio.run(exercise())
        assert result == {"sessions_closed": 0, "sessions_forced": 2, "errors": 0}
        assert factory.open_sessions == 0
        assert supervisor.session_statuses_sync() == {}
    finally:
        factory.release_cleanup.set()
        if supervisor.thread_alive():
            supervisor.shutdown()


def test_async_operations_run_on_the_owned_transport_loop(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, _factory, _clock, _calls = supervisor_parts

    async def exercise() -> None:
        snapshot = await supervisor.ensure_session("omega.first_party.local")
        assert snapshot.server_id == "omega.first_party.local"
        assert await supervisor.call_tool(
            "omega.first_party.local", "omega.echo", {"value": 3}
        ) == {"name": "omega.echo", "arguments": {"value": 3}}
        assert await supervisor.read_resource("omega.first_party.local", "omega://mail/inbox") == (
            "omega://mail/inbox",
        )
        assert await supervisor.stop_server("omega.first_party.local") is True

    asyncio.run(exercise())


def test_sync_timeout_joins_cancelled_operation_cleanup(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, _factory, _clock, _calls = supervisor_parts
    entered = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()
    returned = threading.Event()
    errors: list[BaseException] = []

    async def operation() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.to_thread(release_cleanup.wait)
            cleanup_finished.set()

    def submit() -> None:
        try:
            supervisor.submit_plugin_operation_sync(operation, timeout_s=0.01)
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    caller = threading.Thread(target=submit)
    caller.start()
    try:
        assert entered.wait(2.0)
        assert cleanup_started.wait(2.0)
        assert returned.wait(0.1) is False
        release_cleanup.set()
        caller.join(2.0)
        assert returned.is_set()
        assert cleanup_finished.is_set()
        assert len(errors) == 1
        assert isinstance(errors[0], McpSupervisorError)
        assert str(errors[0]) == "supervisor_operation_timeout"
    finally:
        release_cleanup.set()
        caller.join(2.0)


def test_sync_timeout_abandons_operation_that_never_started(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, _factory, _clock, _calls = supervisor_parts
    loop_blocked = threading.Event()
    release_loop = threading.Event()
    operation_started = threading.Event()
    returned = threading.Event()
    errors: list[BaseException] = []

    async def block_loop() -> None:
        loop_blocked.set()
        release_loop.wait(2.0)

    blocker = supervisor._submit(block_loop)
    assert loop_blocked.wait(2.0)

    async def operation() -> None:
        operation_started.set()

    def submit() -> None:
        try:
            supervisor.submit_plugin_operation_sync(operation, timeout_s=0.01)
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    caller = threading.Thread(target=submit)
    caller.start()
    try:
        assert returned.wait(0.2)
        assert operation_started.is_set() is False
        assert len(errors) == 1
        assert isinstance(errors[0], McpSupervisorError)
        assert str(errors[0]) == "supervisor_operation_timeout"
    finally:
        release_loop.set()
        caller.join(2.0)
        blocker.result(timeout=2.0)
    assert operation_started.is_set() is False


def test_concurrent_ensure_calls_share_one_inflight_launch(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = GatedSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=factory,
        monotonic=FakeClock(),
    )
    supervisor.start()

    async def exercise() -> None:
        first = asyncio.create_task(supervisor.ensure_session("omega.first_party.local"))
        assert await asyncio.to_thread(factory.entered.wait, 2.0)
        second = asyncio.create_task(supervisor.ensure_session("omega.first_party.local"))
        try:
            await asyncio.sleep(0.05)
            assert len(factory.started) == 1
        finally:
            factory.release()
            await asyncio.gather(first, second, return_exceptions=True)

    try:
        asyncio.run(exercise())
    finally:
        supervisor.shutdown()
        for session in factory.sessions:
            if not session.closed:
                asyncio.run(session.close())


def test_shutdown_waits_for_a_starting_loop_and_does_not_orphan_it(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    real_new_event_loop = asyncio.new_event_loop
    constructing = threading.Event()
    release = threading.Event()

    def delayed_new_event_loop() -> asyncio.AbstractEventLoop:
        constructing.set()
        assert release.wait(timeout=2.0)
        return real_new_event_loop()

    monkeypatch.setattr(supervisor_module.asyncio, "new_event_loop", delayed_new_event_loop)
    monkeypatch.setattr(supervisor_module, "SESSION_SHUTDOWN_TIMEOUT_S", 0.1)
    start_errors: list[Exception] = []

    def start_supervisor() -> None:
        try:
            supervisor.start()
        except Exception as exc:
            start_errors.append(exc)

    starter = threading.Thread(target=start_supervisor)
    starter.start()
    assert constructing.wait(timeout=2.0)
    releaser = threading.Timer(0.05, release.set)
    releaser.start()
    try:
        supervisor.shutdown()
        starter.join(timeout=2.0)
        releaser.join(timeout=2.0)
        assert supervisor.thread_alive() is False
        assert len(start_errors) == 1
        assert str(start_errors[0]) == "supervisor_shutdown"
    finally:
        if supervisor.thread_alive():
            loop = supervisor._loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread = supervisor._thread
            if thread is not None:
                thread.join(timeout=2.0)


def test_concurrent_start_callers_wait_for_the_same_loop_readiness(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    real_new_event_loop = asyncio.new_event_loop
    constructing = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    def delayed_new_event_loop() -> asyncio.AbstractEventLoop:
        constructing.set()
        assert release.wait(timeout=2.0)
        return real_new_event_loop()

    monkeypatch.setattr(supervisor_module.asyncio, "new_event_loop", delayed_new_event_loop)
    first = threading.Thread(target=supervisor.start)
    second = threading.Thread(target=lambda: (supervisor.start(), returned.set()))
    first.start()
    assert constructing.wait(timeout=2.0)
    second.start()
    try:
        assert returned.wait(timeout=0.05) is False
        release.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        assert returned.is_set()
    finally:
        release.set()
        if supervisor.thread_alive():
            supervisor.shutdown()


def test_start_timeout_stops_and_joins_loop_that_publishes_readiness_late(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    readiness = LateReadinessEvent()
    supervisor._loop_ready = readiness  # type: ignore[assignment]
    monkeypatch.setattr(supervisor_module, "SESSION_SHUTDOWN_TIMEOUT_S", 0.1)

    try:
        with pytest.raises(McpSupervisorError, match="^supervisor_start_timeout$"):
            supervisor.start()
        assert supervisor.thread_alive() is False
    finally:
        if supervisor.thread_alive():
            loop = supervisor._loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread = supervisor._thread
            if thread is not None:
                thread.join(timeout=2.0)


def test_start_timeout_retains_live_thread_for_retryable_cleanup(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    loop = asyncio.new_event_loop()
    real_call_soon_threadsafe = loop.call_soon_threadsafe
    stop_requests = 0

    def ignore_initial_stops(callback, *args):
        nonlocal stop_requests
        if callback == loop.stop:
            stop_requests += 1
            if stop_requests <= 2:
                return None
        return real_call_soon_threadsafe(callback, *args)

    monkeypatch.setattr(loop, "call_soon_threadsafe", ignore_initial_stops)
    monkeypatch.setattr(supervisor_module.asyncio, "new_event_loop", lambda: loop)
    supervisor._loop_ready = LateReadinessEvent()  # type: ignore[assignment]
    monkeypatch.setattr(supervisor_module, "SESSION_SHUTDOWN_TIMEOUT_S", 0.05)
    start_error = ""

    try:
        try:
            supervisor.start()
        except McpSupervisorError as exc:
            start_error = str(exc)
        alive_after_start_timeout = supervisor.thread_alive()
        first_shutdown = supervisor.shutdown()
        alive_after_first_retry = supervisor.thread_alive()
        second_shutdown = supervisor.shutdown()

        assert start_error == "supervisor_stop_timeout"
        assert alive_after_start_timeout is True
        assert first_shutdown == {
            "sessions_closed": 0,
            "sessions_forced": 0,
            "errors": 1,
        }
        assert alive_after_first_retry is True
        assert second_shutdown == {
            "sessions_closed": 0,
            "sessions_forced": 0,
            "errors": 0,
        }
        assert stop_requests >= 3
        assert supervisor.thread_alive() is False
    finally:
        if supervisor.thread_alive():
            real_call_soon_threadsafe(loop.stop)
            thread = supervisor._thread
            if thread is not None:
                thread.join(timeout=2.0)


def test_shutdown_closes_every_session_joins_thread_and_rejects_submissions(
    supervisor_parts: tuple[
        McpProcessSupervisor, PackageRegistry, FakeSessionFactory, FakeClock, list[Path]
    ],
) -> None:
    supervisor, _registry, session_factory, _clock, _calls = supervisor_parts
    supervisor.ensure_session_sync("omega.first_party.local")

    result = supervisor.shutdown()

    assert result == {"sessions_closed": 1, "sessions_forced": 0, "errors": 0}
    assert session_factory.open_sessions == 0
    assert supervisor.thread_alive() is False
    with pytest.raises(McpSupervisorError, match="^supervisor_shutdown$"):
        supervisor.ensure_session_sync("omega.first_party.local")


def test_concurrent_shutdown_callers_serialize_one_complete_attempt(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = ShutdownGateSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=factory,
        monotonic=FakeClock(),
    )
    supervisor.start()
    supervisor.ensure_session_sync("omega.first_party.local")
    results: dict[str, dict[str, int]] = {}
    errors: list[BaseException] = []
    second_returned = threading.Event()

    def shut_down(name: str) -> None:
        try:
            results[name] = supervisor.shutdown()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if name == "second":
                second_returned.set()

    first = threading.Thread(target=shut_down, args=("first",))
    second = threading.Thread(target=shut_down, args=("second",))
    first.start()
    assert factory.close_entered.wait(timeout=2.0)
    second.start()
    try:
        assert second_returned.wait(timeout=0.1) is False
        assert factory.close_calls == 1
    finally:
        factory.release()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

    assert errors == []
    assert results == {
        "first": {"sessions_closed": 1, "sessions_forced": 0, "errors": 0},
        "second": {"sessions_closed": 0, "sessions_forced": 0, "errors": 0},
    }
    assert factory.open_sessions == 0
    assert supervisor.thread_alive() is False


def test_loop_thread_shutdown_during_external_close_returns_without_deadlock(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, switch, _calls = installed_packages
    factory = ReentrantShutdownSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=factory,
        monotonic=FakeClock(),
    )
    factory.supervisor = supervisor
    supervisor.start()
    supervisor.ensure_session_sync("omega.first_party.local")
    monkeypatch.setattr(supervisor_module, "SESSION_SHUTDOWN_TIMEOUT_S", 0.2)
    results: list[dict[str, int]] = []
    errors: list[BaseException] = []

    def shut_down() -> None:
        try:
            results.append(supervisor.shutdown())
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=shut_down)
    caller.start()
    try:
        assert factory.close_entered.wait(timeout=2.0)
        assert factory.callback_returned.wait(timeout=0.2)
        caller.join(timeout=2.0)

        assert caller.is_alive() is False
        assert errors == []
        assert factory.callback_errors == []
        assert factory.callback_results == [
            {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        ]
        assert results == [{"sessions_closed": 1, "sessions_forced": 0, "errors": 0}]
        assert factory.open_sessions == 0
        assert supervisor.thread_alive() is False
    finally:
        caller.join(timeout=2.0)
        if supervisor.thread_alive():
            loop = supervisor._loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread = supervisor._thread
            if thread is not None:
                thread.join(timeout=2.0)


def test_direct_loop_thread_shutdown_hands_off_one_tracked_cleanup(
    installed_packages: tuple[PackageRegistry, ExternalMcpSwitch, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, switch, _calls = installed_packages
    factory = ShutdownGateSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,  # type: ignore[arg-type]
        session_factory=factory,
        monotonic=FakeClock(),
    )
    supervisor.start()
    supervisor.ensure_session_sync("omega.first_party.local")
    monkeypatch.setattr(supervisor_module, "SESSION_SHUTDOWN_TIMEOUT_S", 0.2)
    callback_results: list[dict[str, int]] = []
    callback_errors: list[BaseException] = []
    first_returned = threading.Event()
    second_returned = threading.Event()

    def loop_shutdown(returned: threading.Event) -> None:
        try:
            callback_results.append(supervisor.shutdown())
        except BaseException as exc:
            callback_errors.append(exc)
        finally:
            returned.set()

    loop = supervisor._loop
    assert loop is not None
    loop.call_soon_threadsafe(loop_shutdown, first_returned)
    try:
        assert first_returned.wait(timeout=0.2)
        assert factory.close_entered.wait(timeout=2.0)
        helper = supervisor._shutdown_helper
        assert helper is not None
        assert helper.daemon is False
        assert helper.is_alive()

        loop.call_soon_threadsafe(loop_shutdown, second_returned)
        assert second_returned.wait(timeout=0.2)
        assert supervisor._shutdown_helper is helper
        assert callback_errors == []
        assert callback_results == [
            {"sessions_closed": 0, "sessions_forced": 0, "errors": 1},
            {"sessions_closed": 0, "sessions_forced": 0, "errors": 1},
        ]

        factory.release()
        result = supervisor.shutdown()

        assert result == {
            "sessions_closed": 1,
            "sessions_forced": 0,
            "errors": 0,
        }
        assert helper.is_alive() is False
        assert factory.open_sessions == 0
        assert supervisor.thread_alive() is False
    finally:
        factory.release()
        if supervisor.thread_alive():
            loop = supervisor._loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread = supervisor._thread
            if thread is not None:
                thread.join(timeout=2.0)
