from __future__ import annotations

import ast
import asyncio
import copy
import gc
import pickle
import weakref
from collections.abc import Mapping
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest
from httpx import URL
from mcp import Client, MCPError, types
from mcp.client import Transport
from mcp.client.caching import CacheConfig
from mcp.server import Server
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpDiscoveryReceipt,
    McpOperationKind,
    McpOperationRequest,
    McpOperationStatus,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from plugin_platform.contracts import AppRequirement


_SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
_CLIENT_NAME = "omega-mcp-host"
_CLIENT_VERSION = "3.0.0"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}
_TOOL_DIGEST = sha256_hex(canonical_bytes(_TOOL_SCHEMA))
_PROMPT_SCHEMA = {"arguments": [{"name": "topic", "required": True}]}
_PROMPT_DIGEST = sha256_hex(canonical_bytes(_PROMPT_SCHEMA))
_OWNER_TASK_PREFIX = "omega-current-mcp-client:"


def _descriptor(
    *,
    with_tool: bool = False,
    with_prompt: bool = False,
    with_resource: bool = False,
    limits: McpClientLimits | None = None,
    remote: bool = False,
    runtime_compatibility: dict[str, object] | None = None,
) -> McpConnectionDescriptor:
    capabilities: list[dict[str, object]] = []
    if with_tool:
        capabilities.append(
            {
                "kind": "tool",
                "name": "calendar.lookup",
                "schema_sha256": _TOOL_DIGEST,
                "version": None,
            }
        )
    if with_prompt:
        capabilities.append(
            {
                "kind": "prompt",
                "name": "calendar.compose",
                "schema_sha256": _PROMPT_DIGEST,
                "version": None,
            }
        )
    if with_resource:
        capabilities.append(
            {
                "kind": "resource",
                "name": "calendar.help",
                "schema_sha256": None,
                "version": None,
            }
        )
    requirement = AppRequirement.from_dict(
        {
            "app_id": "com.example.calendar",
            "publisher_id": "example.publisher",
            "deployment": "remote" if remote else "local",
            "provider_id": None,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": ["streamable_http" if remote else "stdio"],
            "remote_origin": "https://apps.example.com" if remote else None,
            "authorization_issuer": "https://auth.example.com/issuer" if remote else None,
            "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": capabilities,
        }
    )
    projection = project_declared_capabilities(requirement)
    return McpConnectionDescriptor(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=(McpClientDeployment.REMOTE if remote else McpClientDeployment.LOCAL),
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP if remote else McpTransport.STDIO,
        verified_launch_sha256=None if remote else "d" * 64,
        remote_origin=requirement.remote_origin,
        authorization_issuer=requirement.authorization_issuer,
        authentication=requirement.authentication,
        declared_capabilities={
            f"{capability['kind']}:{capability['name']}": capability["schema_sha256"]
            for capability in capabilities
        },
        extension_versions={},
        capability_revision=projection.snapshot.capability_revision,
        limits=limits or McpClientLimits(),
        runtime_compatibility=runtime_compatibility,
    )


def _metadata(
    descriptor: McpConnectionDescriptor,
    *,
    server_id: str = "example.calendar",
    server_version: str = "2.4.0",
    include_server_info: bool = True,
    **changes: object,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "appId": descriptor.app_id,
        "publisherId": descriptor.publisher_id,
        "deployment": descriptor.deployment.value,
        "providerId": descriptor.provider_id,
        "protocolVersion": descriptor.protocol_version,
        "transport": descriptor.transport.value,
        "remoteOrigin": descriptor.remote_origin,
        "authorizationIssuer": descriptor.authorization_issuer,
    }
    if include_server_info:
        metadata[_SERVER_INFO_META_KEY] = {
            "name": server_id,
            "version": server_version,
        }
    metadata.update(changes)
    return metadata


def _tool(metadata: Mapping[str, object]) -> types.Tool:
    return types.Tool(
        name="calendar.lookup",
        description="Calendar lookup.",
        inputSchema=_TOOL_SCHEMA,
        _meta=dict(metadata),
    )


class _FakeSdkSession:
    def __init__(self, descriptor: McpConnectionDescriptor) -> None:
        metadata = _metadata(descriptor)
        self.protocol_version = MCP_PROTOCOL_VERSION
        self.events: list[tuple[str, object]] = []
        self.errors: dict[str, BaseException] = {}
        self.gates: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}
        self.discover_result = types.DiscoverResult(
            supportedVersions=[MCP_PROTOCOL_VERSION],
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(),
                prompts=types.PromptsCapability(),
                resources=types.ResourcesCapability(),
            ),
            ttlMs=0,
            cacheScope="public",
            _meta=metadata,
        )
        self.pages: dict[str, list[object]] = {
            "tools": [
                types.ListToolsResult(
                    tools=[],
                    ttlMs=0,
                    cacheScope="private",
                    _meta=metadata,
                )
            ],
            "prompts": [
                types.ListPromptsResult(
                    prompts=[],
                    ttlMs=0,
                    cacheScope="private",
                    _meta=metadata,
                )
            ],
            "resources": [
                types.ListResourcesResult(
                    resources=[],
                    ttlMs=0,
                    cacheScope="private",
                    _meta=metadata,
                )
            ],
            "resource_templates": [
                types.ListResourceTemplatesResult(
                    resourceTemplates=[],
                    ttlMs=0,
                    cacheScope="private",
                    _meta=metadata,
                )
            ],
        }

    async def _seam(self, name: str) -> None:
        self.events.append((name, asyncio.current_task()))
        if name in self.gates:
            entered, release = self.gates[name]
            entered.set()
            await release.wait()
        error = self.errors.get(name)
        if error is not None:
            raise error

    async def send_request(
        self,
        request: types.ClientRequest,
        result_type: type[types.DiscoverResult],
    ) -> types.DiscoverResult:
        assert type(request) is types.DiscoverRequest
        assert result_type is types.DiscoverResult
        await self._seam("discover")
        return self.discover_result

    async def _page(self, surface: str, params: types.PaginatedRequestParams | None) -> object:
        await self._seam(f"list_{surface}")
        cursor = None if params is None else params.cursor
        self.events.append((f"cursor_{surface}", cursor))
        index = 0 if cursor is None else int(cursor)
        return self.pages[surface][index]

    async def list_tools(
        self, *, params: types.PaginatedRequestParams | None = None
    ) -> types.ListToolsResult:
        return await self._page("tools", params)  # type: ignore[return-value]

    async def list_prompts(
        self, *, params: types.PaginatedRequestParams | None = None
    ) -> types.ListPromptsResult:
        return await self._page("prompts", params)  # type: ignore[return-value]

    async def list_resources(
        self, *, params: types.PaginatedRequestParams | None = None
    ) -> types.ListResourcesResult:
        return await self._page("resources", params)  # type: ignore[return-value]

    async def list_resource_templates(
        self, *, params: types.PaginatedRequestParams | None = None
    ) -> types.ListResourceTemplatesResult:
        return await self._page("resource_templates", params)  # type: ignore[return-value]

    async def initialize(self) -> None:
        raise AssertionError("the current client must not initialize")

    async def initialized(self) -> None:
        raise AssertionError("the current client must not send initialized")


class _FakeSdkClient:
    def __init__(self, session: _FakeSdkSession) -> None:
        self.session = session
        self.protocol_version = session.protocol_version
        self.events: list[tuple[str, asyncio.Task[object] | None]] = []
        self.errors: dict[str, BaseException] = {}
        self.gates: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}
        self.owner: asyncio.Task[object] | None = None
        self.closed = False

    async def __aenter__(self) -> _FakeSdkClient:
        self.owner = asyncio.current_task()
        self.events.append(("enter", self.owner))
        if "enter" in self.gates:
            entered, release = self.gates["enter"]
            entered.set()
            await release.wait()
        error = self.errors.get("enter")
        if error is not None:
            raise error
        return self

    async def __aexit__(self, *_args: object) -> None:
        current = asyncio.current_task()
        self.events.append(("close", current))
        assert current is self.owner
        if "close" in self.gates:
            entered, release = self.gates["close"]
            entered.set()
            await release.wait()
        error = self.errors.get("close")
        if error is not None:
            raise error
        self.closed = True

    async def list_tools(
        self,
        *,
        cursor: str | None = None,
        cache_mode: str,
    ) -> types.ListToolsResult:
        assert cache_mode == "bypass"
        params = None if cursor is None else types.PaginatedRequestParams(cursor=cursor)
        return await self.session.list_tools(params=params)


class _FakeSdkClientFactory:
    def __init__(self, client: _FakeSdkClient) -> None:
        self.client = client
        self.calls: list[tuple[dict[str, object], asyncio.Task[object] | None]] = []

    def __call__(self, **options: object) -> _FakeSdkClient:
        self.calls.append((options, asyncio.current_task()))
        return self.client


_TEST_SDK_CLIENT_FACTORIES: dict[
    int,
    tuple[Server, _FakeSdkClientFactory],
] = {}


@pytest.fixture(autouse=True)
def _route_fake_clients_through_public_server_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    import mcp_platform.client as client_module

    public_client = client_module.Client

    def routed_client(server: object, **options: object) -> Any:
        route = _TEST_SDK_CLIENT_FACTORIES.get(id(server))
        if route is not None and route[0] is server:
            return route[1](**options)
        return public_client(server, **options)

    monkeypatch.setattr(client_module, "Client", routed_client)
    yield
    _TEST_SDK_CLIENT_FACTORIES.clear()


class _HttpsString(str):
    pass


class _CallableProbe:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_options: object) -> object:
        self.calls += 1
        return object()


class _ServerSubclass(Server):
    pass


class _PublicTransportShape:
    def __init__(self) -> None:
        self.enter_calls = 0

    async def __aenter__(self) -> tuple[object, object, object]:
        self.enter_calls += 1
        raise AssertionError("an unbound public Transport must never be entered")

    async def __aexit__(self, *_args: object) -> None:
        return None


def _public_transport_shape() -> Transport:
    return _PublicTransportShape()  # type: ignore[return-value]


def _raw_sdk_client() -> Client:
    return Client(
        object(),
        mode=MCP_PROTOCOL_VERSION,
        client_info=types.Implementation(name="raw-client", version="1.0.0"),
        extensions=(),
        cache=None,
    )


def _raw_https_sdk_client() -> Client:
    return Client(
        _HttpsString("https://apps.example.com/mcp"),
        mode=MCP_PROTOCOL_VERSION,
        client_info=types.Implementation(name="raw-client", version="1.0.0"),
        extensions=(),
        cache=None,
    )


def _https_client_lambda() -> Any:
    def unexpected_call() -> None:
        raise AssertionError("an arbitrary lambda must never be invoked")

    return lambda: (
        unexpected_call(),
        Client(
            _HttpsString("https://apps.example.com/mcp"),
            mode=MCP_PROTOCOL_VERSION,
            client_info=types.Implementation(name="raw-client", version="1.0.0"),
            extensions=(),
            cache=None,
        ),
    )[1]


def _public_lifecycle_server(
    descriptor: McpConnectionDescriptor,
    events: list[str],
) -> Server:
    metadata = _metadata(descriptor)

    async def discover(_ctx: Any, _params: types.RequestParams) -> types.DiscoverResult:
        events.append("discover")
        return types.DiscoverResult(
            supportedVersions=[MCP_PROTOCOL_VERSION],
            capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
            _meta=metadata,
        )

    async def list_tools(
        _ctx: Any,
        _params: types.PaginatedRequestParams,
    ) -> types.ListToolsResult:
        events.append("list_tools")
        return types.ListToolsResult(tools=[], _meta=metadata)

    server = Server(
        "example.calendar",
        version="2.4.0",
        on_list_tools=list_tools,
    )
    server.add_request_handler("server/discover", types.RequestParams, discover)
    return server


def _current_client(
    descriptor: McpConnectionDescriptor,
    factory: _FakeSdkClientFactory,
) -> Any:
    from mcp_platform.client import CurrentMcpClient, SdkClientFactory

    server = Server("injected-test-transport", version="1.0.0")
    _TEST_SDK_CLIENT_FACTORIES[id(server)] = (server, factory)

    return CurrentMcpClient(
        descriptor,
        SdkClientFactory.for_in_process_server(server),
    )


def _error_reason(error: BaseException) -> McpClientErrorReason:
    assert type(error) is McpClientError
    return error.reason


def _list_tools_request(
    descriptor: McpConnectionDescriptor, request_id: str
) -> McpOperationRequest:
    return McpOperationRequest(
        request_id=request_id,
        app_id=descriptor.app_id,
        operation=McpOperationKind.TOOLS_LIST,
        capability_id="tools/list",
        arguments={},
        capability_revision=descriptor.capability_revision,
        deadline_monotonic=asyncio.get_running_loop().time() + 5,
        cancellation_id=f"{request_id}-cancel",
        task_id=None,
        input_request_id=None,
        request_state=None,
    )


async def _assert_no_owned_tasks() -> None:
    await asyncio.sleep(0)
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and task.get_name().startswith(_OWNER_TASK_PREFIX)
    ]


def test_public_sdk_exact_typed_discover_and_request_identity_seam() -> None:
    observations: list[tuple[str, str, dict[str, object], dict[str, object]]] = []

    def observe(ctx: Any) -> None:
        params = ctx.session.client_params
        assert params is not None
        capabilities = ctx.session.client_capabilities
        assert capabilities is not None
        observations.append(
            (
                ctx.method,
                ctx.protocol_version,
                params.client_info.model_dump(
                    by_alias=True,
                    mode="json",
                    exclude_none=True,
                ),
                capabilities.model_dump(
                    by_alias=True,
                    mode="json",
                    exclude_none=True,
                ),
            )
        )

    async def discover(ctx: Any, _params: types.RequestParams) -> types.DiscoverResult:
        observe(ctx)
        return types.DiscoverResult(
            supportedVersions=[MCP_PROTOCOL_VERSION],
            capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
        )

    async def list_tools(
        ctx: Any,
        _params: types.PaginatedRequestParams,
    ) -> types.ListToolsResult:
        observe(ctx)
        return types.ListToolsResult(tools=[])

    server = Server(
        "public-seam-server",
        version="2.0.0",
        on_list_tools=list_tools,
    )
    server.add_request_handler("server/discover", types.RequestParams, discover)

    async def exercise() -> None:
        async with Client(
            server,
            mode=MCP_PROTOCOL_VERSION,
            client_info=types.Implementation(name=_CLIENT_NAME, version=_CLIENT_VERSION),
            extensions=(),
            cache=None,
        ) as client:
            result = await client.session.send_request(
                types.DiscoverRequest(),
                types.DiscoverResult,
            )
            page = await client.session.list_tools()

        assert result.supported_versions == [MCP_PROTOCOL_VERSION]
        assert result.meta is not None
        assert result.meta[_SERVER_INFO_META_KEY] == {
            "name": "public-seam-server",
            "version": "2.0.0",
        }
        assert page.tools == []

    asyncio.run(exercise())

    expected_info = {"name": _CLIENT_NAME, "version": _CLIENT_VERSION}
    assert observations == [
        ("server/discover", MCP_PROTOCOL_VERSION, expected_info, {}),
        ("tools/list", MCP_PROTOCOL_VERSION, expected_info, {}),
    ]


def test_public_factory_capability_accepts_exact_server_and_current_client() -> None:
    from mcp_platform.client import CurrentMcpClient, SdkClientFactory

    descriptor = _descriptor()
    events: list[str] = []
    server = _public_lifecycle_server(descriptor, events)
    factory = SdkClientFactory.for_in_process_server(server)
    assert type(factory) is SdkClientFactory
    client = CurrentMcpClient(
        descriptor,
        factory,
    )

    async def exercise() -> None:
        receipt = await client.start()
        assert receipt.server_id == "example.calendar"
        assert receipt.server_version == "2.4.0"
        assert events == ["discover", "list_tools"]
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_start_uses_exact_sdk_options_and_publishes_one_live_receipt() -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    factory = _FakeSdkClientFactory(sdk_client)
    client = _current_client(descriptor, factory)

    async def exercise() -> None:
        receipt = await client.start()

        assert type(receipt) is McpDiscoveryReceipt
        assert receipt is client.receipt
        assert receipt.protocol_version == MCP_PROTOCOL_VERSION
        assert receipt.server_id == "example.calendar"
        assert receipt.server_version == "2.4.0"
        assert len(factory.calls) == 1
        options, factory_task = factory.calls[0]
        assert factory_task is sdk_client.owner
        assert set(options) == {"mode", "client_info", "extensions", "cache"}
        assert options["mode"] == MCP_PROTOCOL_VERSION
        assert options["mode"] not in {"auto", "legacy"}
        assert options["extensions"] == ()
        cache = options["cache"]
        assert type(cache) is CacheConfig
        assert cache.default_ttl_ms == 0
        assert cache.share_public is False
        client_info = options["client_info"]
        assert type(client_info) is types.Implementation
        assert client_info.model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        ) == {"name": _CLIENT_NAME, "version": _CLIENT_VERSION}
        assert [name for name, _value in session.events] == [
            "discover",
            "list_tools",
            "cursor_tools",
            "list_prompts",
            "cursor_prompts",
            "list_resources",
            "cursor_resources",
            "list_resource_templates",
            "cursor_resource_templates",
        ]
        assert not {"initialize", "initialized"} & {name for name, _value in session.events}

        await client.close()
        assert [name for name, _task in sdk_client.events] == ["enter", "close"]
        assert sdk_client.events[0][1] is sdk_client.events[1][1]
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_lifecycle_state_contract_is_one_shot_and_close_is_idempotent() -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        with pytest.raises(McpClientError) as before_start:
            _ = client.receipt
        assert before_start.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE

        receipt = await client.start()
        with pytest.raises(McpClientError) as start_twice:
            await client.start()
        assert start_twice.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
        assert client.receipt is receipt

        await client.close()
        await client.close()
        assert [name for name, _task in sdk_client.events] == ["enter", "close"]
        with pytest.raises(McpClientError) as after_close:
            _ = client.receipt
        assert after_close.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
        with pytest.raises(McpClientError) as restart:
            await client.start()
        assert restart.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("protocol", McpClientErrorReason.PROTOCOL_MISMATCH),
        ("missing_identity", McpClientErrorReason.IDENTITY_MISMATCH),
        ("conflicting_identity", McpClientErrorReason.IDENTITY_MISMATCH),
        ("capability_drift", McpClientErrorReason.CAPABILITY_DRIFT),
    ],
)
def test_failed_discovery_never_publishes_a_receipt(
    case: str,
    reason: McpClientErrorReason,
) -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    if case == "protocol":
        session.discover_result = session.discover_result.model_copy(
            update={"supported_versions": ["2099-01-01"]}
        )
    elif case == "missing_identity":
        session.discover_result = session.discover_result.model_copy(
            update={"meta": _metadata(descriptor, include_server_info=False)}
        )
    elif case == "conflicting_identity":
        conflicting = _metadata(descriptor, server_id="other.server")
        session.pages["tools"] = [
            types.ListToolsResult(
                tools=[],
                ttlMs=0,
                cacheScope="private",
                _meta=conflicting,
            )
        ]
    else:
        metadata = _metadata(descriptor)
        session.pages["tools"] = [
            types.ListToolsResult(
                tools=[_tool(metadata)],
                ttlMs=0,
                cacheScope="private",
                _meta=metadata,
            )
        ]
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is reason
        with pytest.raises(McpClientError) as unpublished:
            _ = client.receipt
        assert unpublished.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
        await client.close()
        assert [name for name, _task in sdk_client.events] == ["enter", "close"]
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_method_not_found_discovery_stays_current_and_uses_bounded_lists() -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    session.errors["discover"] = MCPError(
        code=types.METHOD_NOT_FOUND,
        message="method not found",
    )
    sdk_client = _FakeSdkClient(session)
    factory = _FakeSdkClientFactory(sdk_client)
    client = _current_client(descriptor, factory)

    async def exercise() -> None:
        receipt = await client.start()
        assert receipt.protocol_version == MCP_PROTOCOL_VERSION
        assert factory.calls[0][0]["mode"] == MCP_PROTOCOL_VERSION
        assert [name for name, _value in session.events].count("discover") == 1
        assert not {"initialize", "initialized"} & {name for name, _value in session.events}
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_signed_generic_compatibility_selects_pinned_handshake_and_normalizes_metadata() -> None:
    """Catches removal of a retained signed endpoint's bounded wire contract."""
    compatibility = {
        "wire_protocol_version": LATEST_HANDSHAKE_VERSION,
        "handshake_mode": "legacy",
        "missing_metadata": {
            "server_id": "StatelessServer",
            "server_version": "ESF",
        },
    }
    descriptor = _descriptor(remote=True, runtime_compatibility=compatibility)
    session = _FakeSdkSession(descriptor)
    session.protocol_version = LATEST_HANDSHAKE_VERSION
    session.discover_result = session.discover_result.model_copy(
        update={
            "supported_versions": [LATEST_HANDSHAKE_VERSION],
            "capabilities": types.ServerCapabilities(),
            "meta": None,
        }
    )
    sdk_client = _FakeSdkClient(session)
    sdk_client.protocol_version = LATEST_HANDSHAKE_VERSION
    builder = _FakeSdkClientFactory(sdk_client)
    from mcp_platform.client import (
        CurrentMcpClient,
        _issue_sdk_client_factory,
        _SDK_CLIENT_FACTORY_ISSUER,
    )

    factory = _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER,
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        builder=builder,
        builder_identity=builder,
        discovery_source=DiscoverySource.LIVE_AUTHENTICATED,
        descriptor=descriptor,
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        receipt = await client.start()
        assert builder.calls[0][0]["mode"] == "legacy"
        assert receipt.server_id == "StatelessServer"
        assert receipt.server_version == "ESF"
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("missing_required_prompt", [False, True])
def test_list_discovery_distinguishes_unadvertised_groups_from_missing_required_capabilities(
    missing_required_prompt: bool,
) -> None:
    """Tools-only servers must connect without pretending a required prompt exists."""
    descriptor = _descriptor(remote=True, with_tool=True, with_prompt=missing_required_prompt)
    session = _FakeSdkSession(descriptor)
    session.errors["discover"] = MCPError(types.METHOD_NOT_FOUND, "Not supported")
    session.pages["tools"][0].tools = [_tool(_metadata(descriptor))]
    sdk_client = _FakeSdkClient(session)
    sdk_client.server_capabilities = types.ServerCapabilities(tools=types.ToolsCapability())
    from mcp_platform.client import (
        CurrentMcpClient, _issue_sdk_client_factory, _SDK_CLIENT_FACTORY_ISSUER,
    )
    builder = _FakeSdkClientFactory(sdk_client)
    factory = _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER, deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP, builder=builder, builder_identity=builder,
        discovery_source=DiscoverySource.LIVE_AUTHENTICATED, descriptor=descriptor,
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        try:
            if missing_required_prompt:
                with pytest.raises(McpClientError) as raised:
                    await client.start()
                assert raised.value.reason is McpClientErrorReason.CAPABILITY_DRIFT
            else:
                receipt = await client.start()
                assert set(receipt.tools) == {"calendar.lookup"}
                assert not receipt.prompts and not receipt.resources and not receipt.resource_templates
            assert [name for name, _ in session.events] == [
                "discover", "list_tools", "cursor_tools",
            ]
        finally:
            await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_successful_discovery_calls_only_advertised_surfaces() -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    session.discover_result = session.discover_result.model_copy(
        update={"capabilities": types.ServerCapabilities(tools=types.ToolsCapability())}
    )
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        receipt = await client.start()
        assert receipt.tools == {}
        assert [name for name, _value in session.events] == [
            "discover",
            "list_tools",
            "cursor_tools",
        ]
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_discovery_accepts_supported_version_set_containing_negotiated_version() -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    session.discover_result = session.discover_result.model_copy(
        update={
            "supported_versions": [
                MCP_PROTOCOL_VERSION,
                "2027-11-25",
                "2028-06-18",
            ],
            "capabilities": types.ServerCapabilities(),
        }
    )
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        receipt = await client.start()
        assert receipt.protocol_version == MCP_PROTOCOL_VERSION
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("required_surface", "descriptor_options"),
    [
        pytest.param("prompt", {"with_prompt": True}, id="prompt"),
        pytest.param("resource", {"with_resource": True}, id="resource"),
    ],
)
def test_discovery_missing_declared_surface_fails_without_unadvertised_list_call(
    required_surface: str,
    descriptor_options: dict[str, bool],
) -> None:
    descriptor = _descriptor(**descriptor_options)
    session = _FakeSdkSession(descriptor)
    session.discover_result = session.discover_result.model_copy(
        update={"capabilities": types.ServerCapabilities()}
    )
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.CAPABILITY_DRIFT
        assert [name for name, _value in session.events] == ["discover"]
        assert not any(name == f"list_{required_surface}" for name, _value in session.events)
        with pytest.raises(McpClientError):
            _ = client.receipt
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("capabilities", "supported_versions", "reason"),
    [
        pytest.param(
            types.ServerCapabilities(logging=types.LoggingCapability()),
            [MCP_PROTOCOL_VERSION],
            McpClientErrorReason.RESULT_INVALID,
            id="logging",
        ),
        pytest.param(
            types.ServerCapabilities(completions=types.CompletionsCapability()),
            [MCP_PROTOCOL_VERSION],
            McpClientErrorReason.RESULT_INVALID,
            id="completions",
        ),
        pytest.param(
            types.ServerCapabilities(tasks=types.ServerTasksCapability()),
            [MCP_PROTOCOL_VERSION],
            McpClientErrorReason.RESULT_INVALID,
            id="legacy-core-tasks",
        ),
        pytest.param(
            types.ServerCapabilities(experimental={"unapproved": {}}),
            [MCP_PROTOCOL_VERSION],
            McpClientErrorReason.RESULT_INVALID,
            id="experimental",
        ),
        pytest.param(
            types.ServerCapabilities(
                extensions={
                    "io.example/unapproved": {
                        "version": "1.0.0",
                        "description": "Unapproved extension.",
                    }
                }
            ),
            [MCP_PROTOCOL_VERSION],
            McpClientErrorReason.CAPABILITY_DRIFT,
            id="undeclared-extension",
        ),
        pytest.param(
            types.ServerCapabilities(),
            ["2027-11-25", "2099-01-01"],
            McpClientErrorReason.PROTOCOL_MISMATCH,
            id="negotiated-version-missing",
        ),
    ],
)
def test_discovery_rejects_unsupported_or_contradictory_capabilities(
    capabilities: types.ServerCapabilities,
    supported_versions: list[str],
    reason: McpClientErrorReason,
) -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    session.discover_result = session.discover_result.model_copy(
        update={
            "capabilities": capabilities,
            "supported_versions": supported_versions,
        }
    )
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is reason
        assert [name for name, _value in session.events] == ["discover"]
        with pytest.raises(McpClientError):
            _ = client.receipt
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_remote_lifecycle_cannot_mint_authenticated_discovery_without_auth_context() -> None:
    from mcp_platform.client import (
        CurrentMcpClient,
        _issue_sdk_client_factory,
        _SDK_CLIENT_FACTORY_ISSUER,
    )

    descriptor = _descriptor(remote=True)
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    factory = _FakeSdkClientFactory(sdk_client)
    client = CurrentMcpClient(
        descriptor,
        _issue_sdk_client_factory(
            _SDK_CLIENT_FACTORY_ISSUER,
            deployment=McpClientDeployment.REMOTE,
            transport=McpTransport.STREAMABLE_HTTP,
            builder=factory,
            builder_identity=factory,
            descriptor=descriptor,
        ),
    )

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
        assert factory.calls == []
        assert session.events == []
        assert sdk_client.events == []
        with pytest.raises(McpClientError) as unpublished:
            _ = client.receipt
        assert unpublished.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_authenticated_remote_factory_publishes_only_live_authenticated_receipt() -> None:
    from mcp_platform.client import (
        CurrentMcpClient,
        _issue_sdk_client_factory,
        _SDK_CLIENT_FACTORY_ISSUER,
    )

    descriptor = _descriptor(remote=True)
    session = _FakeSdkSession(descriptor)
    session.discover_result = session.discover_result.model_copy(
        update={"capabilities": types.ServerCapabilities()}
    )
    sdk_client = _FakeSdkClient(session)
    builder = _FakeSdkClientFactory(sdk_client)
    factory = _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER,
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        builder=builder,
        builder_identity=builder,
        discovery_source=DiscoverySource.LIVE_AUTHENTICATED,
        descriptor=descriptor,
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        receipt = await client.start()
        assert receipt.source is DiscoverySource.LIVE_AUTHENTICATED
        assert builder.calls
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_authenticated_remote_accepts_standard_server_metadata_and_extra_capabilities() -> None:
    from mcp_platform.client import (
        CurrentMcpClient,
        _issue_sdk_client_factory,
        _SDK_CLIENT_FACTORY_ISSUER,
    )

    descriptor = _descriptor(remote=True)
    session = _FakeSdkSession(descriptor)
    standard_metadata = {
        _SERVER_INFO_META_KEY: {
            "name": "authoritative-remote-server",
            "version": "1.0.0",
        }
    }
    session.discover_result = session.discover_result.model_copy(
        update={
            "supported_versions": [MCP_PROTOCOL_VERSION, "2027-11-25"],
            "capabilities": types.ServerCapabilities(
                completions=types.CompletionsCapability(),
                resources=types.ResourcesCapability(),
            ),
            "meta": standard_metadata,
        }
    )
    sdk_client = _FakeSdkClient(session)
    builder = _FakeSdkClientFactory(sdk_client)
    factory = _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER,
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        builder=builder,
        builder_identity=builder,
        discovery_source=DiscoverySource.LIVE_AUTHENTICATED,
        descriptor=descriptor,
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        receipt = await client.start()
        assert receipt.source is DiscoverySource.LIVE_AUTHENTICATED
        assert receipt.server_id == "authoritative-remote-server"
        assert not any(
            name in {"list_resources", "list_resource_templates"}
            for name, _value in session.events
        )
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_in_flight_descriptor_mutation_cannot_rebind_remote_lifecycle() -> None:
    from mcp_platform.client import (
        CurrentMcpClient,
        _issue_sdk_client_factory,
        _SDK_CLIENT_FACTORY_ISSUER,
    )

    descriptor = _descriptor(remote=True)
    mutable_capabilities: dict[str, str | None] = {}
    object.__setattr__(descriptor, "declared_capabilities", mutable_capabilities)
    session = _FakeSdkSession(descriptor)
    session.discover_result = session.discover_result.model_copy(
        update={"capabilities": types.ServerCapabilities()}
    )
    sdk_client = _FakeSdkClient(session)
    builder = _FakeSdkClientFactory(sdk_client)
    factory = _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER,
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        builder=builder,
        builder_identity=builder,
        discovery_source=DiscoverySource.LIVE_AUTHENTICATED,
        descriptor=descriptor,
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        discover_entered = asyncio.Event()
        discover_release = asyncio.Event()
        session.gates["discover"] = (discover_entered, discover_release)
        start_task = asyncio.create_task(client.start())
        await discover_entered.wait()

        rebound = _descriptor(remote=True, with_tool=True)
        object.__setattr__(descriptor, "app_id", "com.example.rebound")
        object.__setattr__(descriptor, "remote_origin", "https://rebound.example.com")
        mutable_capabilities["tool:calendar.lookup"] = _TOOL_DIGEST
        object.__setattr__(
            descriptor,
            "capability_revision",
            rebound.capability_revision,
        )
        rebound_metadata = _metadata(descriptor)
        session.discover_result = session.discover_result.model_copy(
            update={
                "capabilities": types.ServerCapabilities(tools=types.ToolsCapability()),
                "meta": rebound_metadata,
            }
        )
        session.pages["tools"] = [
            types.ListToolsResult(
                tools=[_tool(rebound_metadata)],
                ttlMs=0,
                cacheScope="private",
                _meta=rebound_metadata,
            )
        ]
        discover_release.set()

        with pytest.raises(McpClientError) as raised:
            await start_task
        assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
        assert len(builder.calls) == 1
        assert [name for name, _value in session.events] == ["discover"]
        with pytest.raises(McpClientError) as unpublished:
            _ = client.receipt
        assert unpublished.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_in_flight_caller_mutation_does_not_change_owned_lifecycle_snapshot() -> None:
    from mcp_platform.client import (
        CurrentMcpClient,
        _issue_sdk_client_factory,
        _SDK_CLIENT_FACTORY_ISSUER,
    )

    descriptor = _descriptor(remote=True)
    original_app_id = descriptor.app_id
    original_revision = descriptor.capability_revision
    mutable_capabilities: dict[str, str | None] = {}
    object.__setattr__(descriptor, "declared_capabilities", mutable_capabilities)
    session = _FakeSdkSession(descriptor)
    session.discover_result = session.discover_result.model_copy(
        update={"capabilities": types.ServerCapabilities()}
    )
    sdk_client = _FakeSdkClient(session)

    async def cached_list_tools(
        *,
        cursor: str | None = None,
        cache_mode: str,
    ) -> types.ListToolsResult:
        assert cache_mode == "use"
        params = None if cursor is None else types.PaginatedRequestParams(cursor=cursor)
        return await session.list_tools(params=params)

    sdk_client.list_tools = cached_list_tools  # type: ignore[method-assign]
    builder = _FakeSdkClientFactory(sdk_client)
    factory = _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER,
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        builder=builder,
        builder_identity=builder,
        discovery_source=DiscoverySource.LIVE_AUTHENTICATED,
        descriptor=descriptor,
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        discover_entered = asyncio.Event()
        discover_release = asyncio.Event()
        session.gates["discover"] = (discover_entered, discover_release)
        start_task = asyncio.create_task(client.start())
        await discover_entered.wait()

        rebound = _descriptor(remote=True, with_tool=True)
        object.__setattr__(descriptor, "app_id", "com.example.rebound")
        object.__setattr__(descriptor, "remote_origin", "https://rebound.example.com")
        mutable_capabilities["tool:calendar.lookup"] = _TOOL_DIGEST
        object.__setattr__(descriptor, "capability_revision", rebound.capability_revision)
        discover_release.set()

        receipt = await start_task
        assert receipt.app_id == original_app_id
        assert receipt.capability_revision == original_revision
        assert receipt.tools == {}

        result = await client.list_tools(
            McpOperationRequest(
                request_id="snapshot-operation-1",
                app_id=original_app_id,
                operation=McpOperationKind.TOOLS_LIST,
                capability_id="tools/list",
                arguments={},
                capability_revision=original_revision,
                deadline_monotonic=asyncio.get_running_loop().time() + 5,
                cancellation_id="snapshot-cancel-1",
                task_id=None,
                input_request_id=None,
                request_state=None,
            )
        )
        assert result.status is McpOperationStatus.COMPLETED
        assert dict(result.data) == {"tools": ()}
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "seam",
    ["enter", "discover", "close"],
)
def test_remote_sdk_failures_cross_the_public_boundary_only_as_stable_errors(
    seam: str,
) -> None:
    from mcp_platform.client import (
        CurrentMcpClient,
        _issue_sdk_client_factory,
        _SDK_CLIENT_FACTORY_ISSUER,
    )

    descriptor = _descriptor(remote=True)
    session = _FakeSdkSession(descriptor)
    session.discover_result = session.discover_result.model_copy(
        update={"capabilities": types.ServerCapabilities()}
    )
    sdk_client = _FakeSdkClient(session)
    hostile = "https://user:secret@hostile.example/raw-body-sentinel"
    if seam == "discover":
        session.errors[seam] = MCPError(types.INTERNAL_ERROR, hostile)
    else:
        sdk_client.errors[seam] = RuntimeError(hostile)
    builder = _FakeSdkClientFactory(sdk_client)
    factory = _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER,
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        builder=builder,
        builder_identity=builder,
        discovery_source=DiscoverySource.LIVE_AUTHENTICATED,
        descriptor=descriptor,
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            if seam == "close":
                await client.start()
                await client.close()
            else:
                await client.start()
        assert raised.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
        assert hostile not in str(raised.value)
        assert hostile not in repr(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "target_factory",
    [
        pytest.param(
            lambda: _HttpsString("https://apps.example.com/mcp"),
            id="https-str-subclass",
        ),
        pytest.param(lambda: Path("remote-mcp-target"), id="path-like"),
        pytest.param(_public_transport_shape, id="public-sdk-transport"),
        pytest.param(lambda: URL("https://apps.example.com/mcp"), id="url-object"),
        pytest.param(_raw_sdk_client, id="sdk-client-object"),
        pytest.param(_raw_https_sdk_client, id="https-sdk-client-object"),
        pytest.param(object, id="arbitrary-object"),
        pytest.param(lambda: lambda: object(), id="arbitrary-callable"),
        pytest.param(_CallableProbe, id="callable-object"),
        pytest.param(_https_client_lambda, id="https-client-lambda"),
        pytest.param(lambda: _ServerSubclass("server-subclass"), id="server-subclass"),
    ],
)
def test_only_exact_public_server_can_request_public_factory(
    target_factory: Any,
) -> None:
    from mcp_platform.client import SdkClientFactory

    raw_target = target_factory()
    with pytest.raises(McpClientError) as raised:
        SdkClientFactory.for_in_process_server(raw_target)
    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    if type(raw_target) is _PublicTransportShape:
        assert raw_target.enter_calls == 0
    calls = getattr(raw_target, "calls", None)
    if calls is not None:
        assert calls in (0, [])


def test_public_arbitrary_callable_issuance_path_is_retired() -> None:
    from mcp_platform.client import SdkClientFactory

    assert getattr(SdkClientFactory, "local", None) is None
    assert getattr(SdkClientFactory, "from_callable", None) is None
    assert getattr(SdkClientFactory, "for_target", None) is None


@pytest.mark.parametrize(
    "candidate_factory",
    [
        pytest.param(_CallableProbe, id="callable"),
        pytest.param(_raw_https_sdk_client, id="https-sdk-client"),
        pytest.param(lambda: Server("raw-server"), id="raw-server"),
    ],
)
def test_current_client_rejects_inputs_without_factory_capability(
    candidate_factory: Any,
) -> None:
    from mcp_platform.client import CurrentMcpClient

    candidate = candidate_factory()
    client = CurrentMcpClient(_descriptor(), candidate)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
        assert getattr(candidate, "calls", 0) == 0
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_direct_and_object_level_forgery_cannot_reuse_an_issued_proof() -> None:
    from mcp_platform.client import CurrentMcpClient, SdkClientFactory

    descriptor = _descriptor()
    events: list[str] = []
    factory = SdkClientFactory.for_in_process_server(_public_lifecycle_server(descriptor, events))
    direct = SdkClientFactory()
    forged = object.__new__(SdkClientFactory)
    for field in fields(factory):
        object.__setattr__(forged, field.name, getattr(factory, field.name))

    async def exercise() -> None:
        for candidate in (direct, forged):
            client = CurrentMcpClient(descriptor, candidate)
            with pytest.raises(McpClientError) as raised:
                await client.start()
            assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
            assert events == []
            await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "clone",
    [
        pytest.param(lambda factory: replace(factory), id="dataclasses-replace"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
        pytest.param(lambda factory: pickle.loads(pickle.dumps(factory)), id="pickle"),
    ],
)
def test_copied_or_serialized_factory_cannot_reuse_process_local_proof(
    clone: Any,
) -> None:
    from mcp_platform.client import CurrentMcpClient, SdkClientFactory

    descriptor = _descriptor()
    factory = SdkClientFactory.for_in_process_server(Server("copy-source", version="1.0.0"))
    try:
        copied = clone(factory)
    except (TypeError, pickle.PickleError):
        return
    client = CurrentMcpClient(descriptor, copied)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "field_name",
    [
        "builder",
        "deployment",
        "transport",
        "builder_identity",
        "proof",
        "discovery_source",
        "builder_server",
    ],
)
def test_post_issuance_mutation_invalidates_factory_fingerprint(field_name: str) -> None:
    from mcp_platform.client import CurrentMcpClient, SdkClientFactory

    descriptor = _descriptor()
    events: list[str] = []
    factory = SdkClientFactory.for_in_process_server(_public_lifecycle_server(descriptor, events))
    replacement = _CallableProbe()
    if field_name == "builder":
        object.__setattr__(factory, "_builder", replacement)
    elif field_name == "deployment":
        object.__setattr__(factory, "deployment", McpClientDeployment.REMOTE)
    elif field_name == "transport":
        object.__setattr__(factory, "transport", McpTransport.STREAMABLE_HTTP)
    elif field_name == "builder_identity":
        object.__setattr__(factory, "_builder_identity", object())
    elif field_name == "proof":
        object.__setattr__(factory, "_proof", object())
    elif field_name == "discovery_source":
        object.__setattr__(factory, "discovery_source", DiscoverySource.LIVE_AUTHENTICATED)
    else:
        object.__setattr__(factory._builder, "server", Server("replacement"))
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
        assert replacement.calls == 0
        assert events == []
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_gc_retirement_prevents_reusing_old_proof_or_factory_identity() -> None:
    from mcp_platform.client import CurrentMcpClient, SdkClientFactory

    descriptor = _descriptor()
    events: list[str] = []
    server = _public_lifecycle_server(descriptor, events)
    factory = SdkClientFactory.for_in_process_server(server)
    retired_values = {field.name: getattr(factory, field.name) for field in fields(factory)}
    factory_ref = weakref.ref(factory)
    del factory
    gc.collect()
    assert factory_ref() is None

    forged = object.__new__(SdkClientFactory)
    for name, value in retired_values.items():
        object.__setattr__(forged, name, value)
    client = CurrentMcpClient(descriptor, forged)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
        assert events == []
        await client.close()

        replacement = CurrentMcpClient(
            descriptor,
            SdkClientFactory.for_in_process_server(server),
        )
        receipt = await replacement.start()
        assert receipt.server_id == "example.calendar"
        await replacement.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_internal_factory_issuance_requires_exact_private_capability() -> None:
    from mcp_platform.client import _issue_sdk_client_factory

    factory = _CallableProbe()
    with pytest.raises(McpClientError) as raised:
        _issue_sdk_client_factory(
            object(),
            deployment=McpClientDeployment.LOCAL,
            transport=McpTransport.STDIO,
            builder=factory,
            builder_identity=factory,
        )
    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    assert factory.calls == 0


@pytest.mark.parametrize(
    ("descriptor", "transport_case", "reason"),
    [
        pytest.param(
            _descriptor(),
            "remote-url",
            McpClientErrorReason.IDENTITY_MISMATCH,
            id="url-with-local-descriptor",
        ),
        pytest.param(
            _descriptor(remote=True),
            "local-input",
            McpClientErrorReason.IDENTITY_MISMATCH,
            id="local-input-with-remote-descriptor",
        ),
    ],
)
def test_factory_descriptor_identity_mismatch_rejects_before_builder(
    descriptor: McpConnectionDescriptor,
    transport_case: str,
    reason: McpClientErrorReason,
) -> None:
    from mcp_platform.client import CurrentMcpClient, SdkClientFactory

    events: list[str] = []
    sdk_factory_input: object = (
        "https://apps.example.com/mcp"
        if transport_case == "remote-url"
        else SdkClientFactory.for_in_process_server(_public_lifecycle_server(descriptor, events))
    )
    client = CurrentMcpClient(
        descriptor,
        sdk_factory_input,
    )

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is reason
        assert events == []
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_pagination_uses_public_params_and_stops_at_the_limit() -> None:
    descriptor = _descriptor(
        with_tool=True,
        limits=replace(McpClientLimits(), max_list_pages=1),
    )
    session = _FakeSdkSession(descriptor)
    metadata = _metadata(descriptor)
    session.pages["tools"] = [
        types.ListToolsResult(
            tools=[],
            nextCursor="1",
            ttlMs=0,
            cacheScope="private",
            _meta=metadata,
        ),
        types.ListToolsResult(
            tools=[_tool(metadata)],
            ttlMs=0,
            cacheScope="private",
            _meta=metadata,
        ),
    ]
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED
        assert [value for name, value in session.events if name == "cursor_tools"] == [None]
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


class _SeamBaseException(BaseException):
    pass


class _TruthSensitiveBaseException(BaseException):
    def __init__(self, message: str, *, raises: bool) -> None:
        super().__init__(message)
        self.raises = raises
        self.bool_calls = 0

    def __bool__(self) -> bool:
        self.bool_calls += 1
        if self.raises:
            raise AssertionError("BaseException truthiness must not be evaluated")
        return False


@pytest.mark.parametrize("seam", ["enter", "discover", "list_tools", "close"])
@pytest.mark.parametrize("raises", [False, True], ids=["falsey", "raising-bool"])
def test_truth_sensitive_base_exception_preserves_identity_without_bool(
    seam: str,
    raises: bool,
) -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    sentinel = _TruthSensitiveBaseException("truth-sensitive-sentinel", raises=raises)
    if seam in {"enter", "close"}:
        sdk_client.errors[seam] = sentinel
    else:
        session.errors[seam] = sentinel
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        raised: BaseException | None = None
        try:
            if seam == "close":
                await client.start()
                await client.close()
            else:
                await client.start()
        except BaseException as error:
            raised = error
        finally:
            await client.close()
        assert raised is sentinel
        assert sentinel.bool_calls == 0
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("raises", [False, True], ids=["falsey", "raising-bool"])
def test_truth_sensitive_active_error_wins_cleanup_without_bool(raises: bool) -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    active_error = _TruthSensitiveBaseException("active-sentinel", raises=raises)
    cleanup_error = _TruthSensitiveBaseException("cleanup-sentinel", raises=raises)
    session.errors["discover"] = active_error
    sdk_client.errors["close"] = cleanup_error
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        raised: BaseException | None = None
        try:
            await client.start()
        except BaseException as error:
            raised = error
        finally:
            await client.close()
        assert raised is active_error
        assert active_error.bool_calls == 0
        assert cleanup_error.bool_calls == 0
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("seam", ["enter", "discover", "list_tools", "close"])
@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: asyncio.CancelledError("cancel-sentinel"), id="cancelled"),
        pytest.param(lambda: KeyboardInterrupt("interrupt-sentinel"), id="keyboard-interrupt"),
        pytest.param(lambda: _SeamBaseException("base-sentinel"), id="base-exception"),
    ],
)
def test_every_sdk_lifecycle_seam_preserves_base_exception_identity(
    seam: str,
    error_factory: Any,
) -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    sentinel = error_factory()
    if seam in {"enter", "close"}:
        sdk_client.errors[seam] = sentinel
    else:
        session.errors[seam] = sentinel
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        try:
            if seam == "close":
                await client.start()
                await client.close()
            else:
                await client.start()
        except BaseException as raised:
            assert raised is sentinel
        else:
            pytest.fail("the exact seam error did not escape")
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_discovery_error_wins_over_cleanup_error_without_identity_loss() -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    discovery_error = _SeamBaseException("discover-sentinel")
    cleanup_error = _SeamBaseException("cleanup-sentinel")
    session.errors["discover"] = discovery_error
    sdk_client.errors["close"] = cleanup_error
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        try:
            await client.start()
        except BaseException as raised:
            assert raised is discovery_error
        else:
            pytest.fail("discovery error was lost")
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("seam", ["enter", "discover"])
def test_start_cancellation_joins_owner_cleanup_without_survivors(seam: str) -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    entered = asyncio.Event()
    release = asyncio.Event()
    if seam == "enter":
        sdk_client.gates[seam] = (entered, release)
    else:
        session.gates[seam] = (entered, release)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        start_task = asyncio.create_task(client.start())
        await entered.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        if seam == "discover":
            assert [name for name, _task in sdk_client.events] == ["enter", "close"]
        else:
            assert [name for name, _task in sdk_client.events] == ["enter"]
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_close_cancellation_waits_for_owner_cleanup_and_preserves_cancellation() -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    sdk_client.gates["close"] = (close_entered, close_release)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        await client.start()
        close_task = asyncio.create_task(client.close())
        await close_entered.wait()
        close_task.cancel()
        close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert sdk_client.closed
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_repeated_close_cancellation_stays_shielded_until_owner_cleanup_finishes() -> None:
    descriptor = _descriptor()
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    sdk_client.gates["close"] = (close_entered, close_release)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        await client.start()
        propagated: list[asyncio.CancelledError] = []

        async def close_and_capture() -> None:
            try:
                await client.close()
            except asyncio.CancelledError as error:
                propagated.append(error)
                raise

        close_task = asyncio.create_task(close_and_capture())
        await close_entered.wait()
        for message in ("first-cancel", "second-cancel", "third-cancel", "fourth-cancel"):
            close_task.cancel(message)
            await asyncio.sleep(0)
        assert not close_task.done()
        assert not sdk_client.closed

        close_release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await close_task
        assert len(propagated) == 1
        assert raised.value is propagated[0]
        assert raised.value.args == ("first-cancel",)
        assert sdk_client.closed
        assert [name for name, task in sdk_client.events if task is sdk_client.owner] == [
            "enter",
            "close",
        ]
        await client.close()
        await _assert_no_owned_tasks()

    asyncio.run(exercise())


def test_client_source_has_no_private_sdk_legacy_or_cross_layer_path() -> None:
    source_path = Path("mcp_platform/client.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(
        module.startswith("mcp.client._") or module.startswith("mcp.shared") for module in imports
    )
    assert not imports & {
        "mcp.client.sse",
        "mcp.client.stdio",
        "mcp.client.streamable_http",
        "mcp_platform.session",
        "mcp_platform.registry",
        "mcp_platform.supervisor",
        "hud",
        "memory",
        "runtime",
    }
    forbidden_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and (node.attr.startswith("_") or node.attr in {"initialize", "initialized"})
    }
    assert forbidden_attributes.isdisjoint(
        {
            "_probe",
            "_transport",
            "__aenter__",
            "__aexit__",
            "initialize",
            "initialized",
        }
    )
    source = source_path.read_text(encoding="utf-8")
    for forbidden in (
        'mode="auto"',
        'mode="legacy"',
        "2026-07-27",
        "sampling_callback",
        "list_roots_callback",
        "logging_callback",
    ):
        assert forbidden not in source


def test_sdk_factory_is_a_public_sealed_capability_type() -> None:
    from mcp_platform.client import SdkClientFactory

    assert SdkClientFactory.__module__ == "mcp_platform.client"
    assert not SdkClientFactory.__name__.startswith("_")
    assert not getattr(SdkClientFactory, "_is_protocol", False)


def test_non_boolean_tool_error_flag_is_invalid_not_a_size_failure() -> None:
    from mcp_platform.client import _normalize_tool_result

    malformed = types.CallToolResult.model_construct(content=[], is_error="false")
    with pytest.raises(McpClientError) as raised:
        _normalize_tool_result(malformed, McpClientLimits())
    assert raised.value.reason is McpClientErrorReason.RESULT_INVALID


def test_local_ordinary_transport_error_is_content_free_and_chain_free() -> None:
    from mcp_platform.client import _public_client_error

    secret = RuntimeError("local secret path C:\\private\\token.txt")
    public = _public_client_error(_descriptor(), secret)

    assert type(public) is McpClientError
    assert public.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
    assert str(public) == McpClientErrorReason.TRANSPORT_UNAVAILABLE.value
    assert public.__cause__ is None
    assert public.__context__ is None
    assert "secret" not in repr(public)


def test_connection_deadline_retires_a_start_owner_that_resists_cancellation() -> None:
    limits = replace(
        McpClientLimits(),
        max_connection_duration_ms=20,
        max_shutdown_duration_ms=20,
    )
    descriptor = _descriptor(limits=limits)
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_enter = sdk_client.__class__.__aenter__

    async def resistant_enter() -> _FakeSdkClient:
        sdk_client.owner = asyncio.current_task()
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return sdk_client

    sdk_client.__class__.__aenter__ = lambda self: resistant_enter()  # type: ignore[method-assign]
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        start_task = asyncio.create_task(client.start())
        try:
            await entered.wait()
            done, _pending = await asyncio.wait({start_task}, timeout=0.25)
            assert start_task in done, "connection deadline did not retire the owner"
            with pytest.raises(McpClientError) as raised:
                await start_task
            assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert client._state.name == "CLOSED"
            survivors = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith(_OWNER_TASK_PREFIX)
                and not task.done()
            ]
            assert len(survivors) == 1
        finally:
            release.set()
            if not start_task.done():
                await start_task
                await client.close()
            await asyncio.sleep(0.02)
            sdk_client.__class__.__aenter__ = original_enter  # type: ignore[method-assign]

    asyncio.run(exercise())


def test_operation_survivor_keeps_close_recovery_required_until_joined() -> None:
    limits = replace(
        McpClientLimits(),
        max_operation_duration_ms=20,
        max_shutdown_duration_ms=20,
    )
    descriptor = _descriptor(limits=limits)
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resistant_list_tools(
        *,
        cursor: str | None = None,
        cache_mode: str,
    ) -> types.ListToolsResult:
        del cursor, cache_mode
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return session.pages["tools"][0]  # type: ignore[return-value]

    async def exercise() -> None:
        survivors: list[asyncio.Task[object]] = []
        try:
            await client.start()
            sdk_client.list_tools = resistant_list_tools  # type: ignore[method-assign]
            result = await client.list_tools(_list_tools_request(descriptor, "survivor-op"))
            assert entered.is_set()
            assert result.status is McpOperationStatus.FAILED
            assert result.error is not None
            assert result.error.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert client._cleanup_recovery_required is True

            survivors = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith("omega-mcp-operation:")
                and not task.done()
            ]
            assert len(survivors) == 1
            survivor = survivors[0]
            assert survivor in client._active_operations

            for _attempt in range(2):
                with pytest.raises(McpClientError) as raised:
                    await client.close()
                assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
                assert client._state.name == "CLOSING"
                assert survivor in client._active_operations

            release.set()
            await asyncio.gather(survivor, return_exceptions=True)
            await asyncio.sleep(0)
            assert client._active_operations == {}
            await client.close()
            assert client._state.name == "CLOSED"
        finally:
            release.set()
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)
            await asyncio.sleep(0)
            if client._state.name != "CLOSED":
                await client.close()

    asyncio.run(exercise())


def test_cancelled_operation_preserves_exact_identity_while_survivor_is_owned() -> None:
    limits = replace(
        McpClientLimits(),
        max_operation_duration_ms=5_000,
        max_shutdown_duration_ms=20,
    )
    descriptor = _descriptor(limits=limits)
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resistant_list_tools(
        *,
        cursor: str | None = None,
        cache_mode: str,
    ) -> types.ListToolsResult:
        del cursor, cache_mode
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return session.pages["tools"][0]  # type: ignore[return-value]

    async def exercise() -> None:
        propagated: list[asyncio.CancelledError] = []
        survivors: list[asyncio.Task[object]] = []
        try:
            await client.start()
            sdk_client.list_tools = resistant_list_tools  # type: ignore[method-assign]

            async def invoke() -> None:
                try:
                    await client.list_tools(_list_tools_request(descriptor, "cancelled-op"))
                except asyncio.CancelledError as error:
                    propagated.append(error)
                    raise

            caller = asyncio.create_task(invoke())
            await entered.wait()
            caller.cancel("operation-cancel-sentinel")
            with pytest.raises(asyncio.CancelledError) as raised:
                await caller
            assert len(propagated) == 1
            assert raised.value is propagated[0]
            assert raised.value.args == ("operation-cancel-sentinel",)

            survivors = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith("omega-mcp-operation:")
                and not task.done()
            ]
            assert len(survivors) == 1
            assert survivors[0] in client._active_operations
        finally:
            release.set()
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)
            await asyncio.sleep(0)
            await client.close()

    asyncio.run(exercise())


def test_shutdown_deadline_retires_a_close_owner_and_reports_survivor() -> None:
    limits = replace(McpClientLimits(), max_shutdown_duration_ms=20)
    descriptor = _descriptor(limits=limits)
    session = _FakeSdkSession(descriptor)
    sdk_client = _FakeSdkClient(session)
    close_entered = asyncio.Event()
    release = asyncio.Event()

    original_exit = sdk_client.__class__.__aexit__

    async def resistant_exit(self: _FakeSdkClient, *_args: object) -> None:
        close_entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        await original_exit(self, *_args)

    sdk_client.__class__.__aexit__ = resistant_exit  # type: ignore[method-assign]
    client = _current_client(descriptor, _FakeSdkClientFactory(sdk_client))

    async def exercise() -> None:
        close_task: asyncio.Task[None] | None = None
        try:
            await client.start()
            close_task = asyncio.create_task(client.close())
            await close_entered.wait()
            done, _pending = await asyncio.wait({close_task}, timeout=0.25)
            assert close_task in done, "shutdown deadline did not retire the owner"
            with pytest.raises(McpClientError) as raised:
                await close_task
            assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert close_entered.is_set()
            assert client._state.name == "CLOSED"
            assert any(
                task.get_name().startswith(_OWNER_TASK_PREFIX) and not task.done()
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            )
        finally:
            release.set()
            if close_task is not None and not close_task.done():
                await close_task
            await asyncio.sleep(0.02)
            sdk_client.__class__.__aexit__ = original_exit  # type: ignore[method-assign]

    asyncio.run(exercise())
