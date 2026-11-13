"""Verified local stdio transport tests for the current MCP client."""

from __future__ import annotations

import asyncio
import copy
import gc
import hashlib
import json
import os
import shutil
import sys
import threading
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import psutil
from mcp import types

from mcp_platform.canonical import canonical_bytes
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client import CurrentMcpClient
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.package_verifier import VerifiedPackage, verify_package
from mcp_platform.redaction import RedactingTextSink
from plugin_platform.contracts import AppRequirement
from tests.helpers.fs import link_or_copy
from tests.mcp_platform.test_package_verifier import (
    build_signed_package,
    ephemeral_ring,
    host_tag,
    rewrite_and_resign_manifest,
)


def _build_current_signed_package(root: Path, private: Any) -> Path:
    package = build_signed_package(root, private, signing_key_id="test-release")
    catalog_path = package / "tool_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    for tool in catalog["tools"]:
        tool["effect"] = "read"
    catalog_bytes = canonical_bytes(catalog)
    catalog_path.write_bytes(catalog_bytes)
    rewrite_and_resign_manifest(
        package,
        private,
        {"tool_catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest()},
    )
    return package


def _requirement(package: VerifiedPackage) -> AppRequirement:
    capabilities = [
        {
            "kind": "tool",
            "name": tool.name,
            "schema_sha256": tool.input_schema_sha256,
            "version": None,
        }
        for tool in package.tool_catalog.tools
    ]
    capabilities.extend(
        {
            "kind": "resource",
            "name": resource.name,
            "schema_sha256": None,
            "version": None,
        }
        for resource in package.resource_catalog.resources
    )
    capabilities.extend(
        {
            "kind": "resource",
            "name": template.name,
            "schema_sha256": None,
            "version": None,
        }
        for template in package.resource_catalog.resource_templates
    )
    return AppRequirement.from_dict(
        {
            "app_id": package.manifest.server_id,
            "publisher_id": package.manifest.publisher_id,
            "deployment": "local",
            "provider_id": None,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": ["stdio"],
            "remote_origin": None,
            "authorization_issuer": None,
            "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": capabilities,
        }
    )


def _descriptor(package: VerifiedPackage, **changes: object) -> McpConnectionDescriptor:
    projection = project_declared_capabilities(_requirement(package))
    requirement = _requirement(package)
    values: dict[str, object] = {
        "app_id": requirement.app_id,
        "publisher_id": requirement.publisher_id,
        "deployment": McpClientDeployment.LOCAL,
        "provider_id": None,
        "protocol_version": MCP_PROTOCOL_VERSION,
        "transport": McpTransport.STDIO,
        "verified_launch_sha256": package.manifest.artifact_sha256,
        "remote_origin": None,
        "authorization_issuer": None,
        "authentication": requirement.authentication,
        "declared_capabilities": {
            f"{capability.kind.value}:{capability.name}": capability.schema_sha256
            for capability in requirement.capabilities
        },
        "extension_versions": {},
        "capability_revision": projection.snapshot.capability_revision,
        "limits": McpClientLimits(),
    }
    if "signed_capability_authority" in McpConnectionDescriptor.__dataclass_fields__:
        values["signed_capability_authority"] = {
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
                for tool in package.tool_catalog.tools
            },
            "resources": {
                resource.name: {
                    "uri": resource.uri,
                    "mime_type": resource.mime_type,
                    "max_bytes": resource.max_bytes,
                }
                for resource in package.resource_catalog.resources
            },
            "resource_templates": {
                template.name: {
                    "uri_template": template.uri_template,
                    "mime_type": template.mime_type,
                    "max_bytes": template.max_bytes,
                }
                for template in package.resource_catalog.resource_templates
            },
        }
    values.update(changes)
    return McpConnectionDescriptor(**values)  # type: ignore[arg-type]


@pytest.fixture
def verified_package(tmp_path: Path) -> VerifiedPackage:
    private, ring = ephemeral_ring()
    package = _build_current_signed_package(tmp_path, private)
    return verify_package(
        package,
        ring,
        omega_version="3.0.0",
        platform_tag=host_tag(),
    )


def test_stdio_frame_limit_is_incremental_before_join_decode_or_parse() -> None:
    from mcp_platform.transports.stdio import _BoundedStdioFrameBuffer

    buffer = _BoundedStdioFrameBuffer(max_bytes=8)
    assert buffer.feed(b"1234") == ()
    assert buffer.feed(b"5678") == ()
    with pytest.raises(McpClientError) as raised:
        buffer.feed(b"9secret-without-newline")
    assert raised.value.reason is McpClientErrorReason.RESULT_LIMIT_EXCEEDED
    assert str(raised.value) == McpClientErrorReason.RESULT_LIMIT_EXCEEDED.value
    assert "secret" not in repr(raised.value)


def test_stdio_frame_limit_accepts_exact_boundary_and_resets_per_frame() -> None:
    from mcp_platform.transports.stdio import _BoundedStdioFrameBuffer

    buffer = _BoundedStdioFrameBuffer(max_bytes=4)
    assert buffer.feed(b"1234\n12") == (b"1234",)
    assert buffer.feed(b"34\n") == (b"1234",)
    assert buffer.finish() == ()


def test_stderr_tail_limit_is_constructor_validated_and_immutable(tmp_path: Path) -> None:
    sink = RedactingTextSink(omega_root=tmp_path, max_bytes=13)
    sink.write("0123456789abcdef\n")
    sink.finish()

    assert sink.max_bytes == 13
    assert len(sink.getvalue().encode("utf-8")) <= 13
    with pytest.raises(AttributeError):
        sink.max_bytes = 14  # type: ignore[misc]
    with pytest.raises(ValueError, match="max_bytes"):
        RedactingTextSink(omega_root=tmp_path, max_bytes=False)  # type: ignore[arg-type]


def test_stdio_surviving_process_is_a_bounded_cleanup_required_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_platform.transports.stdio as stdio_module

    class _Pipe:
        async def aclose(self) -> None:
            return None

    class _Process:
        stdin = _Pipe()
        stdout = _Pipe()
        returncode = None
        pid = 4242

    attempts: list[str] = []

    async def never_exits(_process: object, _timeout: float) -> bool:
        attempts.append("wait")
        return False

    async def terminate(_process: object) -> None:
        attempts.append("terminate")

    monkeypatch.setattr(stdio_module, "_wait_for_process_exit", never_exits)
    monkeypatch.setattr(stdio_module, "_terminate_process_tree", terminate)
    monkeypatch.setattr(stdio_module, "close_process_job", lambda _process: None)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await stdio_module._stop_server_process(
                _Process(),  # type: ignore[arg-type]
                deadline=anyio.current_time() + 0.02,
            )
        assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
        assert attempts == ["wait", "terminate", "wait"]

    import anyio

    asyncio.run(exercise())


def test_stdio_process_tree_termination_cannot_outlive_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anyio
    import mcp_platform.transports.stdio as stdio_module

    class _Pipe:
        async def aclose(self) -> None:
            return None

    class _Process:
        stdin = _Pipe()
        stdout = _Pipe()
        returncode = None
        pid = 4243

    async def exercise() -> None:
        release = asyncio.Event()

        async def never_exits(_process: object, _timeout: float) -> bool:
            return False

        async def cancellation_resistant_terminate(_process: object) -> None:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        monkeypatch.setattr(stdio_module, "_wait_for_process_exit", never_exits)
        monkeypatch.setattr(
            stdio_module,
            "_terminate_process_tree",
            cancellation_resistant_terminate,
        )
        monkeypatch.setattr(stdio_module, "close_process_job", lambda _process: None)

        cleanup = asyncio.create_task(
            stdio_module._stop_server_process(
                _Process(),  # type: ignore[arg-type]
                deadline=anyio.current_time() + 0.02,
            )
        )
        try:
            done, _pending = await asyncio.wait({cleanup}, timeout=0.2)
            assert cleanup in done, "process-tree termination exceeded absolute deadline"
            with pytest.raises(McpClientError) as raised:
                cleanup.result()
            assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
        finally:
            release.set()
            if not cleanup.done():
                try:
                    await cleanup
                except McpClientError:
                    pass

    asyncio.run(exercise())


def _verified_python_package(
    tmp_path: Path,
    *,
    script: str,
    script_arguments: list[str],
) -> VerifiedPackage:
    """Build a real signed executable package without relying on PATH or a shell."""
    private, ring = ephemeral_ring()
    package = _build_current_signed_package(tmp_path, private)
    original_artifact = package / "omega-mcp-fake.exe"
    original_artifact.unlink()
    scripts = package / "Scripts"
    scripts.mkdir()
    artifact = scripts / "omega-mcp-fake.exe"
    link_or_copy(sys.executable, artifact)
    for runtime_file in Path(sys.executable).parent.iterdir():
        if runtime_file.suffix.lower() in {".dll", ".pyd", ".zip"}:
            link_or_copy(runtime_file, scripts / runtime_file.name)
    venv_config = Path(sys.executable).parents[1] / "pyvenv.cfg"
    if venv_config.is_file():
        shutil.copyfile(venv_config, package / "pyvenv.cfg")
    rewrite_and_resign_manifest(
        package,
        private,
        {
            "entrypoint": {
                "artifact": artifact.relative_to(package).as_posix(),
                "arguments": ["-I", "-c", script, *script_arguments],
            },
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        },
    )
    return verify_package(
        package,
        ring,
        omega_version="3.0.0",
        platform_tag=host_tag(),
    )


def _metadata(descriptor: McpConnectionDescriptor, package: VerifiedPackage) -> dict[str, object]:
    return {
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


def _plain_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain_json(item) for item in value]
    return value


class _ConnectedClient:
    def __init__(
        self,
        server: Any,
        package: VerifiedPackage,
        descriptor: McpConnectionDescriptor,
        calls: list[dict[str, object]],
        **options: object,
    ) -> None:
        self._server = server
        self._package = package
        self._descriptor = descriptor
        self._calls = calls
        self._options = options
        self.protocol_version = MCP_PROTOCOL_VERSION
        self.session = self
        self.streams: tuple[Any, Any] | None = None

    async def __aenter__(self) -> _ConnectedClient:
        self._calls.append(dict(self._options))
        self.streams = await self._server.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._server.__aexit__(*args)

    async def send_request(
        self,
        request: types.ClientRequest,
        result_type: type[types.DiscoverResult],
    ) -> types.DiscoverResult:
        assert type(request) is types.DiscoverRequest
        assert result_type is types.DiscoverResult
        return types.DiscoverResult(
            supportedVersions=[MCP_PROTOCOL_VERSION],
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(),
                resources=types.ResourcesCapability(),
            ),
            ttlMs=0,
            cacheScope="private",
            _meta=_metadata(self._descriptor, self._package),
        )

    async def list_tools(
        self,
        *,
        params: types.PaginatedRequestParams | None = None,
    ) -> types.ListToolsResult:
        assert params is None
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool.name,
                    description=None,
                    inputSchema=_plain_json(tool.input_schema),
                    outputSchema=_plain_json(tool.output_schema),
                )
                for tool in self._package.tool_catalog.tools
            ],
            ttlMs=0,
            cacheScope="private",
            _meta=_metadata(self._descriptor, self._package),
        )

    async def list_resources(
        self,
        *,
        params: types.PaginatedRequestParams | None = None,
    ) -> types.ListResourcesResult:
        assert params is None
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=resource.uri,
                    name=resource.name,
                    mimeType=resource.mime_type,
                )
                for resource in self._package.resource_catalog.resources
            ],
            ttlMs=0,
            cacheScope="private",
            _meta=_metadata(self._descriptor, self._package),
        )

    async def list_resource_templates(
        self,
        *,
        params: types.PaginatedRequestParams | None = None,
    ) -> types.ListResourceTemplatesResult:
        assert params is None
        return types.ListResourceTemplatesResult(
            resourceTemplates=[
                types.ResourceTemplate(
                    uriTemplate=template.uri_template,
                    name=template.name,
                    mimeType=template.mime_type,
                )
                for template in self._package.resource_catalog.resource_templates
            ],
            ttlMs=0,
            cacheScope="private",
            _meta=_metadata(self._descriptor, self._package),
        )


class _RecordingTransport:
    def __init__(
        self,
        *,
        errlog: Any,
        enter_error: BaseException | None = None,
        exit_error: BaseException | None = None,
        on_enter: Any | None = None,
    ) -> None:
        self.errlog = errlog
        self.enter_error = enter_error
        self.exit_error = exit_error
        self.on_enter = on_enter
        self.entered = False
        self.closed = False

    async def __aenter__(self) -> tuple[object, object]:
        self.entered = True
        if self.on_enter is not None:
            self.on_enter()
        if self.enter_error is not None:
            raise self.enter_error
        os.write(self.errlog.fileno(), b"Bearer opaque-stdio-secret\n")
        return object(), object()

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True
        if self.exit_error is not None:
            raise self.exit_error


def _launch_environment(package: VerifiedPackage) -> dict[str, str]:
    root = package.tool_catalog_path.parent.resolve()
    return {
        "OMEGA_MCP_SERVER_ID": package.manifest.server_id,
        "OMEGA_MCP_PACKAGE_ROOT": str(root),
    }


class _RetainedGuardDouble:
    def __init__(self, close_error: BaseException | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _TransportFatal(BaseException):
    pass


def test_launch_guard_constructor_copy_and_object_new_cannot_forge_authority(
    verified_package: VerifiedPackage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches nominal construction or copied proof reaching SDK factory issuance."""
    from mcp_platform.client_contracts import McpClientError
    from mcp_platform.transports import stdio as stdio_module

    descriptor = _descriptor(verified_package)
    retained = _RetainedGuardDouble()
    with pytest.raises(TypeError):
        stdio_module.VerifiedStdioLaunchGuard(
            verified_package=verified_package,
            package_root=verified_package.tool_catalog_path.parent.resolve(),
            environment=_launch_environment(verified_package),
            artifact_guard=retained,
        )

    monkeypatch.setattr(
        stdio_module.ArtifactLaunchGuard,
        "acquire",
        classmethod(lambda _cls, **_kwargs: retained),
    )
    issued = stdio_module.VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    forged = (copy.copy(issued), object.__new__(stdio_module.VerifiedStdioLaunchGuard))
    try:
        for candidate in forged:
            with pytest.raises(McpClientError, match="^identity_mismatch$"):
                stdio_module.build_verified_stdio_transport(
                    descriptor,
                    candidate,
                    RedactingTextSink(omega_root=tmp_path),
                )
    finally:
        issued.close()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("artifact_path", Path("forged.exe")),
        ("arguments", ("--forged",)),
        ("environment", {"FORGED": "1"}),
        ("package_root", Path("forged-root")),
    ],
)
def test_launch_material_is_not_assignable_after_guard_issuance(
    verified_package: VerifiedPackage,
    field: str,
    replacement: object,
) -> None:
    """Catches process creation reading caller-mutated command/args/env/cwd fields."""
    from mcp_platform.transports.stdio import VerifiedStdioLaunchGuard

    guard = VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    try:
        with pytest.raises(AttributeError):
            setattr(guard, field, replacement)
    finally:
        guard.close()


def test_object_level_guard_mutation_revokes_authority_and_closes_retained_handle(
    verified_package: VerifiedPackage,
    tmp_path: Path,
) -> None:
    """Catches object.__setattr__ bypassing normal immutable assignment checks."""
    from mcp_platform.client_contracts import McpClientError
    from mcp_platform.transports.stdio import (
        VerifiedStdioLaunchGuard,
        build_verified_stdio_transport,
    )

    guard = VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    object.__setattr__(guard, "_proof", object())

    with pytest.raises(McpClientError, match="^identity_mismatch$"):
        build_verified_stdio_transport(
            _descriptor(verified_package),
            guard,
            RedactingTextSink(omega_root=tmp_path),
        )

    assert guard.closed is True


def test_unconsumed_launch_guard_gc_releases_the_retained_native_guard(
    verified_package: VerifiedPackage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches abandoned one-shot launch authority retaining handles indefinitely."""
    from mcp_platform.transports import stdio as stdio_module

    retained = _RetainedGuardDouble()
    monkeypatch.setattr(
        stdio_module.ArtifactLaunchGuard,
        "acquire",
        classmethod(lambda _cls, **_kwargs: retained),
    )
    guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    reference = weakref.ref(guard)

    del guard
    gc.collect()

    assert reference() is None
    assert retained.close_calls == 1


def test_launch_guard_proof_cannot_be_reused_across_two_issued_guards(
    verified_package: VerifiedPackage,
    tmp_path: Path,
) -> None:
    """Catches copying one live guard's proof onto different retained launch state."""
    from mcp_platform.client_contracts import McpClientError
    from mcp_platform.transports.stdio import (
        VerifiedStdioLaunchGuard,
        build_verified_stdio_transport,
    )

    first = VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    second = VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    try:
        object.__setattr__(second, "_proof", getattr(first, "_proof", object()))
        with pytest.raises(McpClientError, match="^identity_mismatch$"):
            build_verified_stdio_transport(
                _descriptor(verified_package),
                second,
                RedactingTextSink(omega_root=tmp_path),
            )
        assert second.closed is True
    finally:
        first.close()
        second.close()


def test_verified_stdio_factory_uses_exact_launch_and_redacting_lifecycle(
    verified_package: VerifiedPackage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches command/args/env/cwd/encoding drift or bypassing stderr capture."""
    from mcp_platform.transports import stdio as stdio_module

    descriptor = _descriptor(
        verified_package,
        limits=McpClientLimits(
            max_discovery_document_bytes=8_192,
            max_result_document_bytes=16_384,
        ),
    )
    launch_guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    observed: dict[str, object] = {}
    transports: list[_RecordingTransport] = []
    client_calls: list[dict[str, object]] = []

    def transport_factory(
        params: object,
        *,
        errlog: Any,
        **_limits: object,
    ) -> _RecordingTransport:
        observed["params"] = params
        observed["limits"] = _limits
        transport = _RecordingTransport(errlog=errlog)
        transports.append(transport)
        return transport

    monkeypatch.setattr(stdio_module, "stdio_client", transport_factory)
    monkeypatch.setattr(
        stdio_module,
        "Client",
        lambda server, **options: _ConnectedClient(
            server,
            verified_package,
            descriptor,
            client_calls,
            **options,
        ),
    )
    sink = RedactingTextSink(omega_root=tmp_path)
    factory = stdio_module.build_verified_stdio_transport(descriptor, launch_guard, sink)

    async def exercise() -> None:
        client = CurrentMcpClient(descriptor, factory)
        await client.start()
        assert launch_guard.closed is True
        await client.close()

    asyncio.run(exercise())

    params = observed["params"]
    assert params.command == str(verified_package.artifact_path)
    assert params.args == list(verified_package.manifest.arguments)
    assert params.env == _launch_environment(verified_package)
    assert params.cwd == verified_package.tool_catalog_path.parent.resolve()
    assert params.encoding == "utf-8"
    assert params.encoding_error_handler == "strict"
    assert observed["limits"] == {
        "max_frame_bytes": 16_384,
        "shutdown_timeout_seconds": (descriptor.limits.max_shutdown_duration_ms / 1_000),
    }
    assert transports[0].entered is True
    assert transports[0].closed is True
    assert len(client_calls) == 1
    assert client_calls[0]["mode"] == MCP_PROTOCOL_VERSION
    assert client_calls[0]["read_timeout_seconds"] == (
        descriptor.limits.max_operation_duration_ms / 1_000
    )
    retained = sink.getvalue()
    assert "opaque-stdio-secret" not in retained
    assert "<redacted:token>" in retained
    assert not any(
        thread.name.startswith("omega-mcp-stderr-") and thread.is_alive()
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize(
    "change",
    [
        {"app_id": "other.app"},
        {"publisher_id": "other.publisher"},
        {"verified_launch_sha256": "f" * 64},
        {"declared_capabilities": {}},
    ],
)
def test_verified_stdio_factory_rejects_descriptor_package_drift_before_sdk_work(
    verified_package: VerifiedPackage,
    tmp_path: Path,
    change: Mapping[str, object],
) -> None:
    """Catches a self-labelled local descriptor escaping signed package authority."""
    from mcp_platform.client_contracts import McpClientError
    from mcp_platform.transports.stdio import (
        VerifiedStdioLaunchGuard,
        build_verified_stdio_transport,
    )

    descriptor = _descriptor(verified_package, **change)
    launch_guard = VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    with pytest.raises(McpClientError, match="^identity_mismatch$"):
        build_verified_stdio_transport(
            descriptor,
            launch_guard,
            RedactingTextSink(omega_root=tmp_path),
        )

    assert launch_guard.closed is True
    verified_package.artifact_path.write_bytes(b"released after descriptor rejection")


@pytest.mark.skipif(os.name != "nt", reason="Windows deny-sharing semantics")
def test_launch_guard_is_held_until_sdk_process_creation_and_then_released(
    verified_package: VerifiedPackage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches releasing retained artifact/directory handles before transport entry."""
    from mcp_platform.transports import stdio as stdio_module

    descriptor = _descriptor(verified_package)
    launch_guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    blocked: list[bool] = []
    client_calls: list[dict[str, object]] = []

    def process_creation() -> None:
        try:
            verified_package.artifact_path.write_bytes(b"unsigned replacement")
        except OSError:
            blocked.append(True)
        else:
            blocked.append(False)

    monkeypatch.setattr(
        stdio_module,
        "stdio_client",
        lambda _params, *, errlog, **_limits: _RecordingTransport(
            errlog=errlog,
            on_enter=process_creation,
        ),
    )
    monkeypatch.setattr(
        stdio_module,
        "Client",
        lambda server, **options: _ConnectedClient(
            server,
            verified_package,
            descriptor,
            client_calls,
            **options,
        ),
    )
    factory = stdio_module.build_verified_stdio_transport(
        descriptor,
        launch_guard,
        RedactingTextSink(omega_root=tmp_path),
    )

    async def exercise() -> None:
        client = CurrentMcpClient(descriptor, factory)
        await client.start()
        assert blocked == [True]
        verified_package.artifact_path.write_bytes(b"released after creation")
        await client.close()

    asyncio.run(exercise())
    assert launch_guard.closed is True
    assert verified_package.artifact_path.read_bytes() == b"released after creation"


@pytest.mark.parametrize("error_kind", ["error", "cancel", "keyboard", "fatal"])
def test_transport_entry_failure_preserves_identity_and_releases_every_handle(
    verified_package: VerifiedPackage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
) -> None:
    """Catches cancellation/process-exit cleanup masking or retained-handle leaks."""
    from mcp_platform.transports import stdio as stdio_module

    descriptor = _descriptor(verified_package)
    cleanup_error = RuntimeError("secondary retained-handle cleanup failure")
    retained = _RetainedGuardDouble(cleanup_error)
    monkeypatch.setattr(
        stdio_module.ArtifactLaunchGuard,
        "acquire",
        classmethod(lambda _cls, **_kwargs: retained),
    )
    launch_guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    if error_kind == "error":
        sentinel: BaseException = OSError("untrusted process detail")
    elif error_kind == "cancel":
        sentinel = asyncio.CancelledError("stdio-cancel")
    elif error_kind == "keyboard":
        sentinel = KeyboardInterrupt("stdio-keyboard")
    else:
        sentinel = _TransportFatal("stdio-fatal")
    transport: _RecordingTransport | None = None

    def transport_factory(
        _params: object,
        *,
        errlog: Any,
        **_limits: object,
    ) -> _RecordingTransport:
        nonlocal transport
        transport = _RecordingTransport(errlog=errlog, enter_error=sentinel)
        return transport

    monkeypatch.setattr(stdio_module, "stdio_client", transport_factory)
    monkeypatch.setattr(
        stdio_module,
        "Client",
        lambda server, **options: _ConnectedClient(
            server,
            verified_package,
            descriptor,
            [],
            **options,
        ),
    )
    factory = stdio_module.build_verified_stdio_transport(
        descriptor,
        launch_guard,
        RedactingTextSink(omega_root=tmp_path),
    )

    async def exercise() -> BaseException:
        client = CurrentMcpClient(descriptor, factory)
        try:
            await client.start()
        except BaseException as caught:
            return caught
        raise AssertionError("transport entry unexpectedly completed")

    caught = asyncio.run(exercise())
    if error_kind == "error":
        assert type(caught) is McpClientError
        assert caught.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
        assert "untrusted process detail" not in repr(caught)
    else:
        assert caught is sentinel
    assert launch_guard.closed is True
    assert transport is not None and transport.entered is True
    assert retained.close_calls == 1
    verified_package.artifact_path.write_bytes(b"released after failed entry")
    assert not any(
        thread.name.startswith("omega-mcp-stderr-") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_guard_release_failure_exits_entered_transport_and_preserves_primary(
    verified_package: VerifiedPackage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an entered process transport being orphaned by dual-fault cleanup."""
    from mcp_platform.transports import stdio as stdio_module

    primary = KeyboardInterrupt("retained guard release failed")
    secondary = _TransportFatal("transport exit also failed")
    retained = _RetainedGuardDouble(primary)
    monkeypatch.setattr(
        stdio_module.ArtifactLaunchGuard,
        "acquire",
        classmethod(lambda _cls, **_kwargs: retained),
    )
    descriptor = _descriptor(verified_package)
    launch_guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    transport = _RecordingTransport(
        errlog=Path(os.devnull).open("w", encoding="utf-8"),
        exit_error=secondary,
    )
    monkeypatch.setattr(
        stdio_module,
        "stdio_client",
        lambda _params, *, errlog, **_limits: transport,
    )
    monkeypatch.setattr(
        stdio_module,
        "Client",
        lambda server, **options: _ConnectedClient(
            server,
            verified_package,
            descriptor,
            [],
            **options,
        ),
    )
    factory = stdio_module.build_verified_stdio_transport(
        descriptor,
        launch_guard,
        RedactingTextSink(omega_root=tmp_path),
    )

    async def exercise() -> BaseException:
        client = CurrentMcpClient(descriptor, factory)
        try:
            await client.start()
        except BaseException as caught:
            return caught
        raise AssertionError("transport unexpectedly survived guard release failure")

    try:
        assert asyncio.run(exercise()) is primary
        assert transport.entered is True
        assert transport.closed is True
        assert launch_guard.closed is True
        assert retained.close_calls == 1
    finally:
        transport.errlog.close()


def test_stderr_capture_start_baseexception_releases_the_launch_guard(
    verified_package: VerifiedPackage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches retaining native handles when the stderr owner cannot start."""
    from mcp_platform.transports import stdio as stdio_module

    descriptor = _descriptor(verified_package)
    launch_guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    sentinel = KeyboardInterrupt("capture-start")
    monkeypatch.setattr(
        stdio_module.RedactingStderrCapture,
        "start",
        lambda _capture: (_ for _ in ()).throw(sentinel),
    )
    monkeypatch.setattr(
        stdio_module,
        "stdio_client",
        lambda _params, *, errlog, **_limits: _RecordingTransport(errlog=errlog),
    )
    monkeypatch.setattr(
        stdio_module,
        "Client",
        lambda server, **options: _ConnectedClient(
            server,
            verified_package,
            descriptor,
            [],
            **options,
        ),
    )
    factory = stdio_module.build_verified_stdio_transport(
        descriptor,
        launch_guard,
        RedactingTextSink(omega_root=tmp_path),
    )

    async def exercise() -> BaseException:
        client = CurrentMcpClient(descriptor, factory)
        try:
            await client.start()
        except BaseException as caught:
            return caught
        raise AssertionError("stderr capture unexpectedly started")

    assert asyncio.run(exercise()) is sentinel
    assert launch_guard.closed is True
    verified_package.artifact_path.write_bytes(b"released after capture failure")


def test_factory_issuance_baseexception_releases_the_launch_guard(
    verified_package: VerifiedPackage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches leaking the verified handles before a client factory is returned."""
    from mcp_platform.transports import stdio as stdio_module

    descriptor = _descriptor(verified_package)
    launch_guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        verified_package,
        package_root=verified_package.tool_catalog_path.parent,
        environment=_launch_environment(verified_package),
    )
    sentinel = KeyboardInterrupt("factory-issuance")
    monkeypatch.setattr(
        stdio_module,
        "_issue_sdk_client_factory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sentinel),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        stdio_module.build_verified_stdio_transport(
            descriptor,
            launch_guard,
            RedactingTextSink(omega_root=tmp_path),
        )

    assert caught.value is sentinel
    assert launch_guard.closed is True
    verified_package.artifact_path.write_bytes(b"released after issuance failure")


def test_verified_transport_gives_actual_child_only_the_closed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the SDK default-environment merge after parameter validation."""
    from mcp_platform.transports import stdio as stdio_module

    observed_environment = tmp_path / "actual-child-environment.json"
    script = (
        "import json,os,pathlib,sys,time;"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(os.environ)),encoding='utf-8');"
        "time.sleep(60)"
    )
    package = _verified_python_package(
        tmp_path,
        script=script,
        script_arguments=[str(observed_environment)],
    )
    descriptor = _descriptor(package)
    guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        package,
        package_root=package.tool_catalog_path.parent,
        environment=_launch_environment(package),
    )
    clients: list[_ConnectedClient] = []

    def client_factory(server: object, **options: object) -> _ConnectedClient:
        client = _ConnectedClient(server, package, descriptor, [], **options)
        clients.append(client)
        return client

    monkeypatch.setattr(stdio_module, "Client", client_factory)
    factory = stdio_module.build_verified_stdio_transport(
        descriptor,
        guard,
        RedactingTextSink(omega_root=tmp_path),
    )

    async def exercise() -> dict[str, str]:
        client = CurrentMcpClient(descriptor, factory)
        await client.start()
        for _attempt in range(300):
            if observed_environment.exists():
                break
            await asyncio.sleep(0.01)
        assert observed_environment.exists()
        environment = json.loads(observed_environment.read_text(encoding="utf-8"))
        await client.close()
        return environment

    actual = asyncio.run(exercise())
    assert actual == _launch_environment(package)
    assert clients and clients[0].streams is not None


def test_verified_stdio_shutdown_reaps_the_server_process_tree_without_a_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches parent-only cleanup or replacing the argv launch with a shell."""
    from mcp_platform.transports import stdio as stdio_module

    identities = tmp_path / "process-identities.txt"
    script = (
        "import os,pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text("
        "f'{os.getpid()} {child.pid}',encoding='ascii');"
        "time.sleep(60)"
    )
    package = _verified_python_package(
        tmp_path,
        script=script,
        script_arguments=[str(identities)],
    )
    descriptor = _descriptor(package)
    guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        package,
        package_root=package.tool_catalog_path.parent,
        environment=_launch_environment(package),
    )
    monkeypatch.setattr(
        stdio_module,
        "Client",
        lambda server, **options: _ConnectedClient(
            server,
            package,
            descriptor,
            [],
            **options,
        ),
    )
    factory = stdio_module.build_verified_stdio_transport(
        descriptor,
        guard,
        RedactingTextSink(omega_root=tmp_path),
    )

    async def exercise() -> tuple[int, int]:
        client = CurrentMcpClient(descriptor, factory)
        await client.start()
        for _attempt in range(300):
            if identities.exists():
                values = identities.read_text(encoding="ascii").split()
                if len(values) == 2:
                    await client.close()
                    return int(values[0]), int(values[1])
            await asyncio.sleep(0.01)
        await client.close()
        raise AssertionError("server did not publish process identities")

    parent_pid, child_pid = asyncio.run(exercise())

    processes = [psutil.Process(pid) for pid in (parent_pid, child_pid) if psutil.pid_exists(pid)]
    _gone, alive = psutil.wait_procs(processes, timeout=5.0)
    assert alive == []


def test_verified_stdio_surfaces_process_exit_as_stream_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the production wrapper hanging when its verified child exits early."""
    import anyio
    from mcp_platform.transports import stdio as stdio_module

    package = _verified_python_package(
        tmp_path,
        script="raise SystemExit(23)",
        script_arguments=[],
    )
    descriptor = _descriptor(package)
    guard = stdio_module.VerifiedStdioLaunchGuard.acquire(
        package,
        package_root=package.tool_catalog_path.parent,
        environment=_launch_environment(package),
    )
    clients: list[_ConnectedClient] = []

    def client_factory(server: object, **options: object) -> _ConnectedClient:
        client = _ConnectedClient(server, package, descriptor, [], **options)
        clients.append(client)
        return client

    monkeypatch.setattr(stdio_module, "Client", client_factory)
    factory = stdio_module.build_verified_stdio_transport(
        descriptor,
        guard,
        RedactingTextSink(omega_root=tmp_path),
    )

    async def exercise() -> None:
        client = CurrentMcpClient(descriptor, factory)
        await client.start()
        assert clients[0].streams is not None
        with pytest.raises(anyio.EndOfStream):
            async with asyncio.timeout(5.0):
                await clients[0].streams[0].receive()
        await client.close()

    asyncio.run(exercise())
