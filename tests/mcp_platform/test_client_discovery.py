"""Restricted discovery inside ``CurrentMcpClient`` itself.

A discovery-only client (``CurrentMcpClient.for_restricted_discovery``) is
the sole production path a not-yet-reviewed publisher-verified or personal
MCP connection may use: it negotiates the protocol and enumerates
capabilities against a real server, and is structurally incapable of
anything else. Every test here proves that boundary against a real MCP
server over the real streamable-HTTP transport (in-memory ASGI loopback, no
socket) -- exact operation order, stable bounded-failure causes, and that
every other public method fails closed before ever touching the wire.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from mcp_platform.client import CurrentMcpClient
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpOperationKind,
    McpOperationRequest,
    McpTransport,
    RestrictedDiscoveryReceipt,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.transports.streamable_http import (
    StreamableHttpClientConfig,
    build_streamable_http_transport,
)
from plugin_platform.contracts import PluginAuthentication

_ORIGIN = "https://mcp.vendor.example.com"


def _discovery_descriptor(
    *,
    origin: str = _ORIGIN,
    limits: McpClientLimits | None = None,
) -> McpConnectionDescriptor:
    return McpConnectionDescriptor(
        app_id="vendor.weather",
        publisher_id="vendor",
        deployment=McpClientDeployment.REMOTE,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin=origin,
        authorization_issuer=origin,
        authentication=PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        declared_capabilities={},
        extension_versions={},
        capability_revision="0" * 64,
        limits=limits or McpClientLimits(),
    )


class _DiscoveryServerBehavior:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tools_result: types.ListToolsResult | None = None
        self.list_delay_seconds: float | None = None


def _discovery_server(behavior: _DiscoveryServerBehavior) -> Server:
    async def list_tools(
        _ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        behavior.calls.append("tools/list")
        if behavior.list_delay_seconds is not None:
            await asyncio.sleep(behavior.list_delay_seconds)
        if behavior.tools_result is not None:
            return behavior.tools_result
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="forecast",
                    title="Forecast",
                    description="Look up the forecast.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    outputSchema={
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False),
                )
            ],
        )

    async def list_prompts(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        behavior.calls.append("prompts/list")
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name="summarize",
                    title="Summarize",
                    arguments=[types.PromptArgument(name="topic", required=True)],
                )
            ]
        )

    async def list_resources(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        behavior.calls.append("resources/list")
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    name="almanac",
                    title="Almanac",
                    uri="docs://weather/almanac",
                    mimeType="text/plain",
                    annotations=types.Annotations(audience=["user"]),
                )
            ]
        )

    async def list_templates(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListResourceTemplatesResult:
        behavior.calls.append("resources/templates/list")
        return types.ListResourceTemplatesResult(
            resourceTemplates=[
                types.ResourceTemplate(
                    name="station",
                    title="Weather station",
                    uriTemplate="docs://weather/station/{id}",
                    mimeType="text/plain",
                )
            ]
        )

    async def call_tool(
        _ctx: Any, _params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        behavior.calls.append("tools/call")
        raise AssertionError("a discovery-only client must never call a tool")

    async def get_prompt(
        _ctx: Any, _params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        behavior.calls.append("prompts/get")
        raise AssertionError("a discovery-only client must never fetch a prompt")

    async def read_resource(
        _ctx: Any, _params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        behavior.calls.append("resources/read")
        raise AssertionError("a discovery-only client must never read a resource")

    return Server(
        "vendor.weather",
        version="1.0.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
        on_list_resources=list_resources,
        on_list_resource_templates=list_templates,
        on_read_resource=read_resource,
    )


def _discovery_loopback(
    behavior: _DiscoveryServerBehavior,
    *,
    limits: McpClientLimits | None = None,
    app_wrapper: Callable[[Any], Any] | None = None,
) -> tuple[StreamableHTTPSessionManager, McpConnectionDescriptor, Any]:
    server = _discovery_server(behavior)
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    wired_app = app if app_wrapper is None else app_wrapper(app)
    descriptor = _discovery_descriptor(limits=limits)
    config = StreamableHttpClientConfig._for_loopback_test(httpx2.ASGITransport(app=wired_app))
    factory = build_streamable_http_transport(descriptor, config)
    return manager, descriptor, factory


def _unwrap(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _discover(
    manager: StreamableHTTPSessionManager,
    descriptor: McpConnectionDescriptor,
    factory: Any,
) -> RestrictedDiscoveryReceipt:
    """Run ``discover_restricted`` inside the real loopback server's own
    ``manager.run()`` task group and surface whatever it raises cleanly.

    ``manager.run()`` (the in-process ASGI server side of this fixture --
    real production traffic has no local equivalent) re-wraps any exception
    that propagates through it in its own ``ExceptionGroup`` on exit, on top
    of whatever ``discover_restricted`` itself already unwrapped from the
    client session's task group. This is a test-fixture artifact only, not
    something callers of ``discover_restricted`` see in production -- caught
    and unwrapped here so assertions can match the exact exception
    ``discover_restricted`` actually raised.
    """
    outcome: dict[str, object] = {}

    async def exercise() -> None:
        async with manager.run():
            client = CurrentMcpClient.for_restricted_discovery(descriptor, factory)
            try:
                outcome["receipt"] = await client.discover_restricted()
            except BaseException as error:
                outcome["error"] = error

    try:
        asyncio.run(exercise())
    except BaseException as error:
        raise _unwrap(error) from None
    if "error" in outcome:
        raise _unwrap(outcome["error"])  # type: ignore[arg-type]
    return outcome["receipt"]  # type: ignore[return-value]


# --- the happy path: exact operation order, literal capture --------------


def test_discover_restricted_issues_exactly_the_four_lists_in_order() -> None:
    """``initialize`` itself is the SDK's own implicit session handshake --
    never a literal call this test's handlers could record -- but its
    success is proven by the receipt's own server_id/server_version, which
    can only come from that handshake succeeding first."""
    behavior = _DiscoveryServerBehavior()
    manager, descriptor, factory = _discovery_loopback(behavior)

    receipt = _discover(manager, descriptor, factory)

    assert behavior.calls == [
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    ]
    assert type(receipt) is RestrictedDiscoveryReceipt
    assert receipt.server_id == "vendor.weather"
    assert receipt.server_version == "1.0.0"
    assert receipt.tools_list_changed is False
    assert receipt.prompts_list_changed is False
    assert receipt.resources_list_changed is False


def test_discover_restricted_captures_literal_tool_schema_title_and_annotations() -> None:
    behavior = _DiscoveryServerBehavior()
    manager, descriptor, factory = _discovery_loopback(behavior)

    receipt = _discover(manager, descriptor, factory)

    assert len(receipt.tools) == 1
    tool = receipt.tools[0]
    assert tool["name"] == "forecast"
    assert tool["title"] == "Forecast"
    assert dict(tool["input_schema"]) == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert dict(tool["output_schema"]) == {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "additionalProperties": False,
    }
    assert dict(tool["annotations"]) == {"readOnlyHint": True, "destructiveHint": False}


def test_discover_restricted_captures_prompt_arguments_and_no_annotations() -> None:
    behavior = _DiscoveryServerBehavior()
    manager, descriptor, factory = _discovery_loopback(behavior)

    receipt = _discover(manager, descriptor, factory)

    assert len(receipt.prompts) == 1
    prompt = receipt.prompts[0]
    assert prompt["name"] == "summarize"
    assert prompt["title"] == "Summarize"
    assert list(prompt["arguments"]) == [{"name": "topic", "required": True}]
    assert dict(prompt["annotations"]) == {}


def test_discover_restricted_captures_resource_uri_mime_and_annotations() -> None:
    behavior = _DiscoveryServerBehavior()
    manager, descriptor, factory = _discovery_loopback(behavior)

    receipt = _discover(manager, descriptor, factory)

    assert len(receipt.resources) == 1
    resource = receipt.resources[0]
    assert resource["name"] == "almanac"
    assert resource["title"] == "Almanac"
    assert resource["uri"] == "docs://weather/almanac"
    assert resource["mime_type"] == "text/plain"
    assert dict(resource["annotations"]) == {"audience": ["user"]}

    assert len(receipt.resource_templates) == 1
    template = receipt.resource_templates[0]
    assert template["name"] == "station"
    assert template["uri_template"] == "docs://weather/station/{id}"


def test_discover_restricted_title_defaults_to_name_when_publisher_omits_it() -> None:
    behavior = _DiscoveryServerBehavior()
    behavior.tools_result = types.ListToolsResult(
        tools=[
            types.Tool(
                name="untitled",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            )
        ]
    )
    manager, descriptor, factory = _discovery_loopback(behavior)

    receipt = _discover(manager, descriptor, factory)

    assert receipt.tools[0]["title"] == "untitled"


# --- bounded failures with stable causes ----------------------------------


def test_discover_restricted_fails_closed_on_a_timeout() -> None:
    behavior = _DiscoveryServerBehavior()
    behavior.list_delay_seconds = 5.0
    limits = McpClientLimits(max_connection_duration_ms=100)
    manager, descriptor, factory = _discovery_loopback(behavior, limits=limits)

    with pytest.raises(McpClientError) as raised:
        _discover(manager, descriptor, factory)
    assert raised.value.reason is McpClientErrorReason.REQUEST_TIMEOUT


def test_discover_restricted_fails_closed_on_an_oversized_listing() -> None:
    behavior = _DiscoveryServerBehavior()
    behavior.tools_result = types.ListToolsResult(
        tools=[
            types.Tool(
                name="a",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            types.Tool(
                name="b",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ]
    )
    limits = McpClientLimits(max_list_items=1)
    manager, descriptor, factory = _discovery_loopback(behavior, limits=limits)

    with pytest.raises(McpClientError) as raised:
        _discover(manager, descriptor, factory)
    assert raised.value.reason is McpClientErrorReason.RESULT_LIMIT_EXCEEDED


def test_discover_restricted_fails_closed_on_a_redirect() -> None:
    behavior = _DiscoveryServerBehavior()
    sent: dict[str, bool] = {"done": False}

    def redirect_once(inner_app: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(scope: Any, receive: Any, send: Any) -> None:
            if not sent["done"] and scope["type"] == "http":
                sent["done"] = True
                await send(
                    {
                        "type": "http.response.start",
                        "status": 307,
                        "headers": [(b"location", b"https://evil.example.com/mcp")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            await inner_app(scope, receive, send)

        return wrapped

    manager, descriptor, factory = _discovery_loopback(behavior, app_wrapper=redirect_once)

    with pytest.raises(McpClientError) as raised:
        _discover(manager, descriptor, factory)
    assert raised.value.reason is McpClientErrorReason.TRANSPORT_UNAVAILABLE
    assert behavior.calls == []


# --- construction guards ---------------------------------------------------


def test_for_restricted_discovery_rejects_a_non_remote_descriptor() -> None:
    behavior = _DiscoveryServerBehavior()
    _manager, descriptor, factory = _discovery_loopback(behavior)
    local_descriptor = McpConnectionDescriptor(
        app_id=descriptor.app_id,
        publisher_id=descriptor.publisher_id,
        deployment=McpClientDeployment.LOCAL,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STDIO,
        verified_launch_sha256="a" * 64,
        remote_origin=None,
        authorization_issuer=None,
        authentication=descriptor.authentication,
        declared_capabilities={},
        extension_versions={},
        capability_revision="0" * 64,
        limits=descriptor.limits,
    )
    with pytest.raises(McpClientError) as raised:
        CurrentMcpClient.for_restricted_discovery(local_descriptor, factory)
    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH


def test_for_restricted_discovery_rejects_a_descriptor_with_declared_capabilities() -> None:
    behavior = _DiscoveryServerBehavior()
    _manager, descriptor, factory = _discovery_loopback(behavior)
    from dataclasses import replace

    declared = replace(descriptor, declared_capabilities={"tool:forecast": None})
    with pytest.raises(McpClientError) as raised:
        CurrentMcpClient.for_restricted_discovery(declared, factory)
    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH


def test_for_restricted_discovery_rejects_a_descriptor_with_signed_authority() -> None:
    behavior = _DiscoveryServerBehavior()
    _manager, descriptor, factory = _discovery_loopback(behavior)
    from dataclasses import replace

    signed = replace(
        descriptor,
        signed_capability_authority={
            "tools": {},
            "resources": {},
            "resource_templates": {},
            "prompts": {},
        },
    )
    with pytest.raises(McpClientError) as raised:
        CurrentMcpClient.for_restricted_discovery(signed, factory)
    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH


# --- a discovery-only client is structurally incapable of anything else ---


def _capability_request(descriptor: McpConnectionDescriptor, **changes: object) -> McpOperationRequest:
    values: dict[str, object] = {
        "request_id": "discovery-guard-1",
        "app_id": descriptor.app_id,
        "operation": McpOperationKind.TOOLS_LIST,
        "capability_id": "tools/list",
        "arguments": {},
        "capability_revision": descriptor.capability_revision,
        "deadline_monotonic": 1.0,
        "cancellation_id": "cancel-1",
        "task_id": None,
        "input_request_id": None,
        "request_state": None,
    }
    values.update(changes)
    return McpOperationRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("operation", "capability_id"),
    [
        (McpOperationKind.TOOLS_LIST, "tools/list"),
        (McpOperationKind.TOOL_CALL, "tool:forecast"),
        (McpOperationKind.PROMPTS_LIST, "prompts/list"),
        (McpOperationKind.PROMPT_GET, "prompt:summarize"),
        (McpOperationKind.RESOURCES_LIST, "resources/list"),
        (McpOperationKind.RESOURCE_TEMPLATES_LIST, "resources/templates/list"),
        (McpOperationKind.RESOURCE_READ, "resource:almanac"),
    ],
)
def test_discovery_only_client_refuses_every_operation_before_touching_the_wire(
    operation: McpOperationKind, capability_id: str
) -> None:
    behavior = _DiscoveryServerBehavior()
    manager, descriptor, factory = _discovery_loopback(behavior)

    async def exercise() -> McpClientErrorReason:
        async with manager.run():
            client = CurrentMcpClient.for_restricted_discovery(descriptor, factory)
            request = _capability_request(descriptor, operation=operation, capability_id=capability_id)
            with pytest.raises(McpClientError) as raised:
                await client._run_operation(request, operation, lambda _ctx: None)  # type: ignore[arg-type]
            return raised.value.reason

    reason = asyncio.run(exercise())
    assert reason is McpClientErrorReason.REVIEW_REQUIRED
    # The guard fires before the operation ever reaches the network -- the
    # server recorded nothing.
    assert behavior.calls == []


def test_discovery_only_client_cannot_start_an_ordinary_reconciling_session() -> None:
    behavior = _DiscoveryServerBehavior()
    manager, descriptor, factory = _discovery_loopback(behavior)

    async def exercise() -> McpClientErrorReason:
        async with manager.run():
            client = CurrentMcpClient.for_restricted_discovery(descriptor, factory)
            with pytest.raises(McpClientError) as raised:
                await client.start()
            return raised.value.reason

    assert asyncio.run(exercise()) is McpClientErrorReason.REVIEW_REQUIRED


def test_ordinary_client_cannot_call_discover_restricted() -> None:
    behavior = _DiscoveryServerBehavior()
    manager, descriptor, factory = _discovery_loopback(behavior)

    async def exercise() -> McpClientErrorReason:
        async with manager.run():
            client = CurrentMcpClient(descriptor, factory)
            with pytest.raises(McpClientError) as raised:
                await client.discover_restricted()
            return raised.value.reason

    assert asyncio.run(exercise()) is McpClientErrorReason.IDENTITY_MISMATCH


def test_discover_restricted_can_only_be_called_once() -> None:
    behavior = _DiscoveryServerBehavior()
    manager, descriptor, factory = _discovery_loopback(behavior)

    async def exercise() -> McpClientErrorReason:
        async with manager.run():
            client = CurrentMcpClient.for_restricted_discovery(descriptor, factory)
            await client.discover_restricted()
            with pytest.raises(McpClientError) as raised:
                await client.discover_restricted()
            return raised.value.reason

    assert asyncio.run(exercise()) is McpClientErrorReason.TRANSPORT_UNAVAILABLE
