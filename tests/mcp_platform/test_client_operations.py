from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from mcp import Client, MCPError, types
from mcp.client import ClientSession
from mcp.client.subscriptions import ResourceUpdated, SubscriptionLost, ToolsListChanged
from mcp.server import Server

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client import CurrentMcpClient, SdkClientFactory
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpInputRequest,
    McpOperationKind,
    McpOperationRequest,
    McpOperationResult,
    McpOperationStatus,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from plugin_platform.contracts import AppRequirement

_SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}
_TOOL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
    "additionalProperties": False,
}
_PROMPT_SCHEMA = {"arguments": [{"name": "topic", "required": True}]}


def _signed_authority(
    *,
    tool_max_result_bytes: int = 4096,
    tool_mime_types: tuple[str, ...] = ("application/json", "text/plain"),
    resource_max_bytes: int = 4096,
) -> dict[str, object]:
    return {
        "tools": {
            "calendar.lookup": {
                "title": "Calendar lookup",
                "input_schema": _TOOL_SCHEMA,
                "input_schema_sha256": sha256_hex(canonical_bytes(_TOOL_SCHEMA)),
                "output_schema": _TOOL_OUTPUT_SCHEMA,
                "output_schema_sha256": sha256_hex(canonical_bytes(_TOOL_OUTPUT_SCHEMA)),
                "risk_class": "read_only",
                "effect": "read",
                "requires_revision": False,
                "max_result_bytes": tool_max_result_bytes,
                "allowed_mime_types": list(tool_mime_types),
            }
        },
        "resources": {
            "calendar.help": {
                "uri": "docs://calendar/help",
                "mime_type": "text/plain",
                "max_bytes": resource_max_bytes,
            }
        },
        "resource_templates": {
            "calendar.entry": {
                "uri_template": "docs://calendar/entry/{id}",
                "mime_type": "text/plain",
                "max_bytes": resource_max_bytes,
            }
        },
    }


def _descriptor(
    *,
    limits: McpClientLimits | None = None,
    tool_schema: Mapping[str, object] = _TOOL_SCHEMA,
    signed_capability_authority: Mapping[str, object] | None = None,
) -> McpConnectionDescriptor:
    capabilities = [
        {
            "kind": "tool",
            "name": "calendar.lookup",
            "schema_sha256": sha256_hex(canonical_bytes(tool_schema)),
            "version": None,
        },
        {
            "kind": "prompt",
            "name": "calendar.compose",
            "schema_sha256": sha256_hex(canonical_bytes(_PROMPT_SCHEMA)),
            "version": None,
        },
        {
            "kind": "resource",
            "name": "calendar.help",
            "schema_sha256": None,
            "version": None,
        },
        {
            "kind": "resource",
            "name": "calendar.entry",
            "schema_sha256": None,
            "version": None,
        },
    ]
    requirement = AppRequirement.from_dict(
        {
            "app_id": "com.example.calendar",
            "publisher_id": "example.publisher",
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
    projection = project_declared_capabilities(requirement)
    values: dict[str, object] = {
        "app_id": requirement.app_id,
        "publisher_id": requirement.publisher_id,
        "deployment": McpClientDeployment.LOCAL,
        "provider_id": None,
        "protocol_version": MCP_PROTOCOL_VERSION,
        "transport": McpTransport.STDIO,
        "verified_launch_sha256": "d" * 64,
        "remote_origin": None,
        "authorization_issuer": None,
        "authentication": requirement.authentication,
        "declared_capabilities": {
            f"{capability['kind']}:{capability['name']}": capability["schema_sha256"]
            for capability in capabilities
        },
        "extension_versions": {},
        "capability_revision": projection.snapshot.capability_revision,
        "limits": limits or McpClientLimits(),
    }
    if signed_capability_authority is not None:
        values["signed_capability_authority"] = signed_capability_authority
    return McpConnectionDescriptor(**values)  # type: ignore[arg-type]


def _metadata(descriptor: McpConnectionDescriptor) -> dict[str, object]:
    return {
        "appId": descriptor.app_id,
        "publisherId": descriptor.publisher_id,
        "deployment": descriptor.deployment.value,
        "providerId": descriptor.provider_id,
        "protocolVersion": descriptor.protocol_version,
        "transport": descriptor.transport.value,
        "remoteOrigin": descriptor.remote_origin,
        "authorizationIssuer": descriptor.authorization_issuer,
    }


def _request(
    descriptor: McpConnectionDescriptor,
    operation: McpOperationKind,
    capability_id: str,
    arguments: Mapping[str, object] | None = None,
    **changes: object,
) -> McpOperationRequest:
    values: dict[str, object] = {
        "request_id": "operation-1",
        "app_id": descriptor.app_id,
        "operation": operation,
        "capability_id": capability_id,
        "arguments": {} if arguments is None else arguments,
        "capability_revision": descriptor.capability_revision,
        "deadline_monotonic": asyncio.get_running_loop().time() + 30,
        "cancellation_id": "cancel-1",
        "task_id": None,
        "input_request_id": None,
        "request_state": None,
    }
    values.update(changes)
    return McpOperationRequest(**values)  # type: ignore[arg-type]


class _ServerBehavior:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tool_result: types.CallToolResult | types.InputRequiredResult = types.CallToolResult(
            content=[types.TextContent(text="two matches")],
            structuredContent={"count": 2},
        )
        self.prompt_result: types.GetPromptResult | types.InputRequiredResult = (
            types.GetPromptResult(
                description="Compose a calendar note.",
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(text="Write about the topic."),
                    )
                ],
            )
        )
        self.resource_result: types.ReadResourceResult | types.InputRequiredResult = (
            types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri="docs://calendar/help",
                        mimeType="text/plain",
                        text="Calendar help.",
                    )
                ]
            )
        )
        self.list_override: dict[str, Callable[[str | None], object]] = {}
        self.call_override: (
            Callable[
                [Any, types.CallToolRequestParams],
                Awaitable[types.CallToolResult | types.InputRequiredResult],
            ]
            | None
        ) = None
        self.read_override: (
            Callable[
                [types.ReadResourceRequestParams],
                types.ReadResourceResult | types.InputRequiredResult,
            ]
            | None
        ) = None
        self.discovery_session: Any | None = None


class _LateAccessMapping(Mapping[str, object]):
    def __init__(self) -> None:
        self.late_accessed = False

    def __getitem__(self, key: str) -> object:
        if key == "answer":
            return "xx"
        if key == "zz_late":
            self.late_accessed = True
            return "must not be read"
        raise KeyError(key)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(("answer", "zz_late"))

    def __len__(self) -> int:
        return 2


def _server(descriptor: McpConnectionDescriptor, behavior: _ServerBehavior) -> Server:
    metadata = _metadata(descriptor)

    async def discover(ctx: Any, _params: types.RequestParams) -> types.DiscoverResult:
        behavior.discovery_session = ctx.session
        return types.DiscoverResult(
            supportedVersions=[MCP_PROTOCOL_VERSION],
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(),
                prompts=types.PromptsCapability(),
                resources=types.ResourcesCapability(),
            ),
            _meta=metadata,
        )

    async def list_tools(
        _ctx: Any,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        behavior.calls.append("tools/list")
        cursor = None if params is None else params.cursor
        override = behavior.list_override.get("tools")
        if override is not None:
            return override(cursor)  # type: ignore[return-value]
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="calendar.lookup",
                    description="Look up calendar entries.",
                    inputSchema=_TOOL_SCHEMA,
                    outputSchema=_TOOL_OUTPUT_SCHEMA,
                )
            ],
            _meta=metadata,
        )

    async def call_tool(
        ctx: Any,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult:
        behavior.calls.append("tools/call")
        if behavior.call_override is not None:
            return await behavior.call_override(ctx, params)
        return behavior.tool_result.model_copy(update={"meta": metadata})

    async def list_prompts(
        _ctx: Any,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListPromptsResult:
        behavior.calls.append("prompts/list")
        cursor = None if params is None else params.cursor
        override = behavior.list_override.get("prompts")
        if override is not None:
            return override(cursor)  # type: ignore[return-value]
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name="calendar.compose",
                    description="Compose a calendar note.",
                    arguments=[types.PromptArgument(name="topic", required=True)],
                )
            ],
            _meta=metadata,
        )

    async def get_prompt(
        _ctx: Any,
        _params: types.GetPromptRequestParams,
    ) -> types.GetPromptResult | types.InputRequiredResult:
        behavior.calls.append("prompts/get")
        return behavior.prompt_result.model_copy(update={"meta": metadata})

    async def list_resources(
        _ctx: Any,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListResourcesResult:
        behavior.calls.append("resources/list")
        cursor = None if params is None else params.cursor
        override = behavior.list_override.get("resources")
        if override is not None:
            return override(cursor)  # type: ignore[return-value]
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    name="calendar.help",
                    uri="docs://calendar/help",
                    description="Calendar help.",
                    mimeType="text/plain",
                )
            ],
            _meta=metadata,
        )

    async def list_templates(
        _ctx: Any,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListResourceTemplatesResult:
        behavior.calls.append("resources/templates/list")
        cursor = None if params is None else params.cursor
        override = behavior.list_override.get("templates")
        if override is not None:
            return override(cursor)  # type: ignore[return-value]
        return types.ListResourceTemplatesResult(
            resourceTemplates=[
                types.ResourceTemplate(
                    name="calendar.entry",
                    uriTemplate="docs://calendar/entry/{id}",
                    description="One calendar entry.",
                    mimeType="text/plain",
                )
            ],
            _meta=metadata,
        )

    async def read_resource(
        _ctx: Any,
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult | types.InputRequiredResult:
        behavior.calls.append("resources/read")
        if behavior.read_override is not None:
            return behavior.read_override(params)
        return behavior.resource_result.model_copy(update={"meta": metadata})

    server = Server(
        "example.calendar",
        version="2.4.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
        on_list_resources=list_resources,
        on_list_resource_templates=list_templates,
        on_read_resource=read_resource,
    )
    server.add_request_handler("server/discover", types.RequestParams, discover)
    return server


async def _started(
    *, limits: McpClientLimits | None = None
) -> tuple[CurrentMcpClient, McpConnectionDescriptor, _ServerBehavior]:
    descriptor = _descriptor(limits=limits)
    behavior = _ServerBehavior()
    client = CurrentMcpClient(
        descriptor,
        SdkClientFactory.for_in_process_server(_server(descriptor, behavior)),
    )
    await client.start()
    behavior.calls.clear()
    return client, descriptor, behavior


async def _started_configured(
    descriptor: McpConnectionDescriptor,
    behavior: _ServerBehavior,
) -> CurrentMcpClient:
    client = CurrentMcpClient(
        descriptor,
        SdkClientFactory.for_in_process_server(_server(descriptor, behavior)),
    )
    await client.start()
    behavior.calls.clear()
    return client


def _assert_failed(result: McpOperationResult, reason: McpClientErrorReason) -> None:
    assert result.status is McpOperationStatus.FAILED
    assert result.error is not None
    assert result.error.reason is reason
    assert result.data == {}


class _ScriptedSubscription:
    def __init__(self, script: list[object]) -> None:
        self._script = iter(script)

    def __aiter__(self) -> _ScriptedSubscription:
        return self

    async def __anext__(self) -> object:
        item = next(self._script, StopAsyncIteration)
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item


class _ScriptedListenContext:
    def __init__(self, subscription: _ScriptedSubscription) -> None:
        self._subscription = subscription

    async def __aenter__(self) -> _ScriptedSubscription:
        return self._subscription

    async def __aexit__(self, *_args: object) -> None:
        return None


class _ReplacingExitContext(_ScriptedListenContext):
    async def __aexit__(self, *_args: object) -> None:
        raise SubscriptionLost("cleanup replaced callback failure")


def test_public_operations_normalize_all_standard_primitives_into_frozen_results() -> None:
    async def exercise() -> None:
        client, descriptor, _behavior = await _started()
        results = [
            await client.list_tools(
                _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
            ),
            await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:calendar.lookup",
                    {"query": "planning"},
                )
            ),
            await client.list_prompts(
                _request(descriptor, McpOperationKind.PROMPTS_LIST, "prompts/list")
            ),
            await client.get_prompt(
                _request(
                    descriptor,
                    McpOperationKind.PROMPT_GET,
                    "prompt:calendar.compose",
                    {"topic": "planning"},
                )
            ),
            await client.list_resources(
                _request(descriptor, McpOperationKind.RESOURCES_LIST, "resources/list")
            ),
            await client.list_resource_templates(
                _request(
                    descriptor,
                    McpOperationKind.RESOURCE_TEMPLATES_LIST,
                    "resources/templates/list",
                )
            ),
            await client.read_resource(
                _request(
                    descriptor,
                    McpOperationKind.RESOURCE_READ,
                    "resource:calendar.help",
                )
            ),
        ]

        assert all(type(result) is McpOperationResult for result in results)
        assert all(result.status is McpOperationStatus.COMPLETED for result in results)
        assert results[0].data["tools"][0]["name"] == "calendar.lookup"
        assert results[1].data == {
            "content": ({"type": "text", "text": "two matches"},),
            "structured_content": {"count": 2},
            "is_error": False,
        }
        assert results[3].data["messages"][0]["role"] == "user"
        assert results[6].data["contents"][0] == {
            "uri": "docs://calendar/help",
            "mime_type": "text/plain",
            "text": "Calendar help.",
        }
        with pytest.raises(FrozenInstanceError):
            results[0].status = McpOperationStatus.FAILED  # type: ignore[misc]
        await client.close()

    asyncio.run(exercise())


def test_operation_gate_rejects_wrong_surface_name_app_and_revision_before_sdk_work() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        requests = [
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:missing",
                {"query": "x"},
            ),
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "x"},
                app_id="com.example.other",
            ),
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "x"},
                capability_revision="f" * 64,
            ),
            _request(descriptor, McpOperationKind.PROMPTS_LIST, "tools/list"),
        ]
        results = [await client.call_tool(request) for request in requests[:3]]
        results.append(await client.list_tools(requests[3]))

        assert behavior.calls == []
        assert [result.error.reason for result in results if result.error is not None] == [
            McpClientErrorReason.CAPABILITY_DRIFT,
            McpClientErrorReason.IDENTITY_MISMATCH,
            McpClientErrorReason.CAPABILITY_DRIFT,
            McpClientErrorReason.RESULT_INVALID,
        ]
        await client.close()

    asyncio.run(exercise())


def test_tool_input_is_validated_before_call_and_structured_output_after_call() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        invalid_input = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": 7},
            )
        )
        assert "tools/call" not in behavior.calls
        _assert_failed(invalid_input, McpClientErrorReason.RESULT_INVALID)

        behavior.calls.clear()
        behavior.tool_result = types.CallToolResult(
            content=[types.TextContent(text="bad structured output")],
            structuredContent={"count": "two"},
        )
        invalid_output = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )
        assert behavior.calls.count("tools/call") == 1
        _assert_failed(invalid_output, McpClientErrorReason.RESULT_INVALID)
        await client.close()

    asyncio.run(exercise())


def test_tool_error_result_is_completed_but_binary_and_oversized_content_fail_closed() -> None:
    async def exercise() -> None:
        limits = McpClientLimits(max_result_document_bytes=256)
        client, descriptor, behavior = await _started(limits=limits)
        behavior.tool_result = types.CallToolResult(
            content=[types.TextContent(text="server declined")],
            isError=True,
        )
        error_result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )
        assert error_result.status is McpOperationStatus.COMPLETED
        assert error_result.data["is_error"] is True

        behavior.tool_result = types.CallToolResult(
            content=[types.ImageContent(data="AA==", mimeType="image/png")]
        )
        binary = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )
        _assert_failed(binary, McpClientErrorReason.RESULT_INVALID)

        behavior.tool_result = types.CallToolResult(
            content=[types.TextContent(text="x" * 300)],
            isError=True,
        )
        oversized = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )
        _assert_failed(oversized, McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("signed", [False, True])
def test_text_only_tool_result_without_provider_output_schema_follows_signed_authority(
    signed: bool,
) -> None:
    async def exercise() -> None:
        descriptor = _descriptor(
            signed_capability_authority=_signed_authority() if signed else None,
        )
        behavior = _ServerBehavior()
        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[types.Tool(name="calendar.lookup", inputSchema=_TOOL_SCHEMA)],
            _meta=_metadata(descriptor),
        )
        client = await _started_configured(descriptor, behavior)
        behavior.tool_result = types.CallToolResult(
            content=[types.TextContent(text='{"login":"operator"}')],
            structuredContent=None,
            isError=False,
        )

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "account"},
            )
        )
        if signed:
            _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        else:
            assert result.status is McpOperationStatus.COMPLETED, result.error
            assert result.data["structured_content"] is None
            assert result.data["content"][0]["text"] == '{"login":"operator"}'
        assert behavior.calls.count("tools/call") == 1
        await client.close()

    asyncio.run(exercise())


def test_provider_declared_output_schema_still_requires_structured_content() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        behavior.tool_result = types.CallToolResult(
            content=[types.TextContent(text="text cannot replace the declared count result")],
        )

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "account"},
            )
        )

        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        assert behavior.calls.count("tools/call") == 1
        await client.close()

    asyncio.run(exercise())


def test_signed_output_schema_cannot_be_bypassed_by_text_only_success() -> None:
    async def exercise() -> None:
        descriptor = _descriptor(signed_capability_authority=_signed_authority())
        behavior = _ServerBehavior()
        client = await _started_configured(descriptor, behavior)
        behavior.tool_result = types.CallToolResult(
            content=[types.TextContent(text="unstructured provider text")],
            structuredContent=None,
            isError=False,
        )
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "account"},
            )
        )
        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        assert behavior.calls.count("tools/call") == 1
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (
            [
                types.BlobResourceContents(
                    uri="docs://calendar/help", mimeType="application/octet-stream", blob="AA=="
                )
            ],
            McpClientErrorReason.RESULT_INVALID,
        ),
        (
            [
                types.TextResourceContents(
                    uri="docs://calendar/help", mimeType="image/svg+xml", text="not text"
                )
            ],
            McpClientErrorReason.RESULT_INVALID,
        ),
        (
            [
                types.TextResourceContents(
                    uri="docs://calendar/help", mimeType="text/plain", text="x" * 129
                )
            ],
            McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
        ),
    ],
)
def test_read_resource_rejects_binary_non_text_mime_and_text_over_byte_cap(
    contents: list[types.TextResourceContents | types.BlobResourceContents],
    reason: McpClientErrorReason,
) -> None:
    async def exercise() -> None:
        limits = McpClientLimits(max_resource_text_bytes=128)
        client, descriptor, behavior = await _started(limits=limits)
        behavior.resource_result = types.ReadResourceResult(contents=contents)
        result = await client.read_resource(
            _request(
                descriptor,
                McpOperationKind.RESOURCE_READ,
                "resource:calendar.help",
            )
        )
        _assert_failed(result, reason)
        await client.close()

    asyncio.run(exercise())


def test_read_resource_rejects_content_for_a_different_uri() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        behavior.resource_result = types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri="docs://calendar/other",
                    mimeType="text/plain",
                    text="Wrong resource.",
                )
            ]
        )
        result = await client.read_resource(
            _request(
                descriptor,
                McpOperationKind.RESOURCE_READ,
                "resource:calendar.help",
            )
        )
        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        await client.close()

    asyncio.run(exercise())


def test_read_resource_accepts_only_an_expansion_of_the_declared_template() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()

        def read(params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri=params.uri,
                        mimeType="text/plain",
                        text="Template entry.",
                    )
                ],
                _meta=_metadata(descriptor),
            )

        behavior.read_override = read
        valid = await client.read_resource(
            _request(
                descriptor,
                McpOperationKind.RESOURCE_READ,
                "resource:calendar.entry",
                {"uri": "docs://calendar/entry/42"},
            )
        )
        assert valid.status is McpOperationStatus.COMPLETED

        invalid = await client.read_resource(
            _request(
                descriptor,
                McpOperationKind.RESOURCE_READ,
                "resource:calendar.entry",
                {"uri": "docs://other/42"},
            )
        )
        _assert_failed(invalid, McpClientErrorReason.RESULT_INVALID)
        assert behavior.calls.count("resources/read") == 1
        await client.close()

    asyncio.run(exercise())


def test_list_pagination_rejects_repeated_cursor_page_and_item_caps() -> None:
    async def exercise() -> None:
        limits = McpClientLimits(max_list_pages=2, max_cursor_history=1, max_list_items=4)
        client, descriptor, behavior = await _started(limits=limits)
        metadata = _metadata(descriptor)
        tool = types.Tool(name="calendar.lookup", inputSchema=_TOOL_SCHEMA)

        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[tool], nextCursor="same", _meta=metadata
        )
        repeated = await client.list_tools(
            _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
        )
        _assert_failed(repeated, McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)

        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[tool, tool, tool, tool, tool], _meta=metadata
        )
        too_many = await client.list_tools(
            _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
        )
        _assert_failed(too_many, McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        await client.close()

    asyncio.run(exercise())


def test_progress_events_are_normalized_and_the_exact_cap_stops_delivery() -> None:
    async def exercise() -> None:
        limits = McpClientLimits(max_progress_events=1)
        client, descriptor, behavior = await _started(limits=limits)

        async def progress_call(
            _name: str,
            _arguments: dict[str, object],
            _read_timeout_seconds: float | None = None,
            progress_callback: Callable[[float, float | None, str | None], Awaitable[None]]
            | None = None,
            **_options: object,
        ) -> types.CallToolResult:
            assert progress_callback is not None
            await progress_callback(1, 2, "halfway")
            await progress_callback(2, 2, "done")
            return types.CallToolResult(
                content=[types.TextContent(text="done")],
                structuredContent={"count": 2},
                _meta=_metadata(descriptor),
            )

        connected = client._connected  # noqa: SLF001 - injected public SDK operation seam
        assert type(connected) is Client
        connected.session.call_tool = progress_call
        events: list[McpOperationResult] = []

        async def emit(event: McpOperationResult) -> None:
            events.append(event)

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            progress_callback=emit,
        )
        _assert_failed(result, McpClientErrorReason.PROGRESS_LIMIT_EXCEEDED)
        assert len(events) == 1
        assert events[0].status is McpOperationStatus.PROGRESS
        assert events[0].data == {"progress": 1.0, "total": 2.0, "message": "halfway"}
        await client.close()

    asyncio.run(exercise())


def test_public_sdk_driver_echoes_request_state_and_mints_fresh_round_request_ids() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        opaque_state = "opaque\x00state-雪"
        wire_ids: list[int | str | None] = []
        broker_requests: list[McpInputRequest] = []

        async def interactive(
            ctx: Any, params: types.CallToolRequestParams
        ) -> types.CallToolResult | types.InputRequiredResult:
            wire_ids.append(ctx.request_id)
            round_index = len(wire_ids)
            if round_index <= 2:
                if round_index == 1:
                    assert params.request_state is None
                    assert params.input_responses is None
                else:
                    assert params.request_state == opaque_state
                    assert params.input_responses is not None
                    assert set(params.input_responses) == {"ask-1"}
                return types.InputRequiredResult(
                    inputRequests={
                        f"ask-{round_index}": types.ElicitRequest(
                            params=types.ElicitRequestFormParams(
                                message=f"Question {round_index}",
                                requestedSchema={
                                    "type": "object",
                                    "properties": {"answer": {"type": "string"}},
                                    "required": ["answer"],
                                    "additionalProperties": False,
                                },
                            )
                        )
                    },
                    requestState=opaque_state,
                    _meta=_metadata(descriptor),
                )
            assert params.request_state == opaque_state
            assert params.input_responses is not None
            assert set(params.input_responses) == {"ask-2"}
            return types.CallToolResult(
                content=[types.TextContent(text="complete")],
                structuredContent={"count": 2},
                _meta=_metadata(descriptor),
            )

        behavior.call_override = interactive

        async def broker(request: McpInputRequest) -> Mapping[str, object]:
            broker_requests.append(request)
            return {"answer": f"round-{request.round_number}"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )

        assert result.status is McpOperationStatus.COMPLETED
        assert len(wire_ids) == 3
        assert len(set(wire_ids)) == 3
        assert [request.request_id for request in broker_requests] == ["ask-1", "ask-2"]
        assert [request.message for request in broker_requests] == ["Question 1", "Question 2"]
        assert all(request.field_ids == ("answer",) for request in broker_requests)
        await client.close()

    asyncio.run(exercise())


def test_input_response_identifiers_and_schema_are_closed_before_retry() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()

        async def interactive(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.InputRequiredResult:
            return types.InputRequiredResult(
                inputRequests={
                    "ask-1": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Question",
                            requestedSchema={
                                "type": "object",
                                "properties": {"answer": {"type": "string"}},
                                "required": ["answer"],
                                "additionalProperties": False,
                            },
                        )
                    )
                },
                requestState="state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = interactive

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            return {"answer": 7, "undeclared": "x"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        assert behavior.calls.count("tools/call") == 1
        await client.close()

    asyncio.run(exercise())


def test_input_request_identifier_message_schema_and_field_bounds_precede_broker() -> None:
    async def one_case(
        *,
        request_id: str = "ask-1",
        message: str = "Question",
        properties: Mapping[str, object] | None = None,
        limits: McpClientLimits,
    ) -> tuple[McpOperationResult, int]:
        client, descriptor, behavior = await _started(limits=limits)
        broker_calls = 0

        async def interactive(
            _ctx: Any, params: types.CallToolRequestParams
        ) -> types.CallToolResult | types.InputRequiredResult:
            if params.input_responses is not None:
                return types.CallToolResult(
                    content=[types.TextContent(text="complete")],
                    structuredContent={"count": 1},
                    _meta=_metadata(descriptor),
                )
            requested_properties = (
                {"answer": {"type": "string"}} if properties is None else dict(properties)
            )
            return types.InputRequiredResult(
                inputRequests={
                    request_id: types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message=message,
                            requestedSchema={
                                "type": "object",
                                "properties": requested_properties,
                            },
                        )
                    )
                },
                requestState="state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = interactive

        async def broker(request: McpInputRequest) -> Mapping[str, object]:
            nonlocal broker_calls
            broker_calls += 1
            return {field: "yes" for field in request.field_ids}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        await client.close()
        return result, broker_calls

    async def exercise() -> None:
        long_id, id_calls = await one_case(
            request_id="a" * 257,
            limits=McpClientLimits(max_result_document_bytes=256),
        )
        _assert_failed(long_id, McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
        assert id_calls == 0

        long_message, message_calls = await one_case(
            message="m" * 257,
            limits=McpClientLimits(max_result_document_bytes=256),
        )
        _assert_failed(long_message, McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
        assert message_calls == 0

        too_many_fields, field_calls = await one_case(
            properties={"a": {"type": "string"}, "b": {"type": "string"}},
            limits=McpClientLimits(max_input_fields=1),
        )
        _assert_failed(too_many_fields, McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
        assert field_calls == 0

    asyncio.run(exercise())


def test_repeated_input_request_identifier_is_rejected_before_second_broker_call() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        broker_calls = 0

        async def interactive(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.InputRequiredResult:
            return types.InputRequiredResult(
                inputRequests={
                    "ask-same": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Again",
                            requestedSchema={
                                "type": "object",
                                "properties": {"answer": {"type": "string"}},
                            },
                        )
                    )
                },
                requestState="state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = interactive

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            nonlocal broker_calls
            broker_calls += 1
            return {"answer": "yes"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        assert broker_calls == 1
        await client.close()

    asyncio.run(exercise())


def test_input_round_cap_and_capability_revision_are_checked_during_driver_rounds() -> None:
    async def exercise_round_cap() -> None:
        limits = McpClientLimits(max_input_rounds=1)
        client, descriptor, behavior = await _started(limits=limits)

        async def interactive(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.InputRequiredResult:
            return types.InputRequiredResult(
                inputRequests={
                    f"ask-{behavior.calls.count('tools/call')}": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Again",
                            requestedSchema={
                                "type": "object",
                                "properties": {"answer": {"type": "string"}},
                            },
                        )
                    )
                },
                requestState="same-state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = interactive

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            return {"answer": "yes"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        _assert_failed(result, McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
        assert behavior.calls.count("tools/call") == 2
        await client.close()

    async def exercise_revision() -> None:
        client, descriptor, behavior = await _started()

        async def interactive(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.InputRequiredResult:
            return types.InputRequiredResult(
                inputRequests={
                    "ask-1": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Question",
                            requestedSchema={
                                "type": "object",
                                "properties": {"answer": {"type": "string"}},
                            },
                        )
                    )
                },
                requestState="state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = interactive

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            receipt = client._receipt  # noqa: SLF001 - deliberate mutation protection proof
            assert receipt is not None
            object.__setattr__(receipt, "capability_revision", "f" * 64)
            return {"answer": "yes"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        _assert_failed(result, McpClientErrorReason.CAPABILITY_DRIFT)
        assert behavior.calls.count("tools/call") == 1
        await client.close()

    asyncio.run(exercise_round_cap())
    asyncio.run(exercise_revision())


def test_deadline_and_cancellation_while_waiting_for_input_preserve_public_behavior() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()

        async def interactive(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.InputRequiredResult:
            return types.InputRequiredResult(
                inputRequests={
                    "ask-1": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Question",
                            requestedSchema={
                                "type": "object",
                                "properties": {"answer": {"type": "string"}},
                            },
                        )
                    )
                },
                requestState="state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = interactive
        entered = asyncio.Event()
        release = asyncio.Event()

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            entered.set()
            await release.wait()
            return {"answer": "yes"}

        expired = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
                deadline_monotonic=asyncio.get_running_loop().time() - 0.001,
            ),
            input_callback=broker,
        )
        _assert_failed(expired, McpClientErrorReason.REQUEST_TIMEOUT)

        operation = asyncio.create_task(
            client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:calendar.lookup",
                    {"query": "planning"},
                ),
                input_callback=broker,
            )
        )
        await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        release.set()
        await client.close()

    asyncio.run(exercise())


def test_cancellation_during_state_only_retry_and_broker_base_exception_preserve_identity() -> None:
    async def exercise_retry() -> None:
        client, descriptor, behavior = await _started()

        async def state_only(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.InputRequiredResult:
            return types.InputRequiredResult(
                requestState="poll-state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = state_only
        operation = asyncio.create_task(
            client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:calendar.lookup",
                    {"query": "planning"},
                )
            )
        )
        while behavior.calls.count("tools/call") < 1:
            await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await client.close()

    async def exercise_base_exception() -> None:
        client, descriptor, behavior = await _started()
        sentinel = BaseException("broker sentinel")

        async def interactive(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.InputRequiredResult:
            return types.InputRequiredResult(
                inputRequests={
                    "ask-1": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Question",
                            requestedSchema={
                                "type": "object",
                                "properties": {"answer": {"type": "string"}},
                            },
                        )
                    )
                },
                requestState="state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = interactive

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            raise sentinel

        with pytest.raises(BaseException) as caught:
            await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:calendar.lookup",
                    {"query": "planning"},
                ),
                input_callback=broker,
            )
        assert caught.value is sentinel
        await client.close()

    asyncio.run(exercise_retry())
    asyncio.run(exercise_base_exception())


def test_transport_failure_is_a_content_free_failed_result() -> None:
    async def exercise() -> None:
        client, descriptor, _behavior = await _started()
        connected = client._connected  # noqa: SLF001 - injected public SDK operation seam
        assert type(connected) is Client

        async def unavailable(*_args: object, **_kwargs: object) -> types.CallToolResult:
            raise OSError("secret remote body")

        connected.session.call_tool = unavailable
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )
        _assert_failed(result, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        assert "secret remote body" not in repr(result)
        assert result.to_dict()["error"] == {"reason": "transport_unavailable"}
        await client.close()

    asyncio.run(exercise())


def test_unsolicited_server_elicitation_never_reaches_operation_broker() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        broker_calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def active_call(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.CallToolResult:
            entered.set()
            await release.wait()
            return types.CallToolResult(
                content=[types.TextContent(text="done")],
                structuredContent={"count": 1},
                _meta=_metadata(descriptor),
            )

        behavior.call_override = active_call

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            nonlocal broker_calls
            broker_calls += 1
            return {"answer": "yes"}

        operation = asyncio.create_task(
            client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:calendar.lookup",
                    {"query": "planning"},
                ),
                input_callback=broker,
            )
        )
        await entered.wait()
        assert behavior.discovery_session is not None
        with pytest.raises(Exception):
            await behavior.discovery_session.elicit_form(
                "Unsolicited",
                {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                },
            )
        assert broker_calls == 0
        release.set()
        result = await operation
        assert result.status is McpOperationStatus.COMPLETED
        await client.close()

    asyncio.run(exercise())


def test_operation_path_uses_only_public_client_driver_methods() -> None:
    assert ClientSession.call_tool.__module__ == "mcp.client.session"
    assert ClientSession.get_prompt.__module__ == "mcp.client.session"
    assert ClientSession.read_resource.__module__ == "mcp.client.session"


def test_state_only_intermediate_metadata_drift_stops_before_public_session_retry() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()

        async def state_only_drift(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.CallToolResult | types.InputRequiredResult:
            if behavior.calls.count("tools/call") == 1:
                metadata = _metadata(descriptor)
                metadata["capabilityRevision"] = "f" * 64
                return types.InputRequiredResult(requestState="opaque-state", _meta=metadata)
            return types.CallToolResult(
                content=[types.TextContent(text="forbidden retry")],
                structuredContent={"count": 1},
                _meta=_metadata(descriptor),
            )

        behavior.call_override = state_only_drift
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )

        _assert_failed(result, McpClientErrorReason.CAPABILITY_DRIFT)
        assert behavior.calls.count("tools/call") == 1
        await client.close()

    asyncio.run(exercise())


def test_close_cancels_and_joins_active_operation_before_sdk_owner_exit() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        finish_cancel_cleanup = asyncio.Event()

        async def blocked(_ctx: Any, _params: types.CallToolRequestParams) -> types.CallToolResult:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await finish_cancel_cleanup.wait()
                raise
            return types.CallToolResult(
                content=[types.TextContent(text="late")],
                structuredContent={"count": 1},
                _meta=_metadata(descriptor),
            )

        behavior.call_override = blocked
        operation = asyncio.create_task(
            client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:calendar.lookup",
                    {"query": "planning"},
                )
            ),
            name="task-6-active-operation",
        )
        await entered.wait()
        closing = asyncio.create_task(client.close())
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            await asyncio.sleep(0)
            assert not closing.done()
            refused = await client.list_tools(
                _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
            )
            _assert_failed(refused, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
            finish_cancel_cleanup.set()
            await asyncio.wait_for(closing, timeout=1)
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert operation.done()
        finally:
            release.set()
            finish_cancel_cleanup.set()
            if not closing.done():
                closing.cancel()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(closing, operation, return_exceptions=True)

    asyncio.run(exercise())


def test_tool_schemas_are_meta_validated_with_a_closed_supported_draft() -> None:
    async def one_case(
        input_schema: Mapping[str, object],
        output_schema: Mapping[str, object] | None,
    ) -> McpOperationResult:
        descriptor = _descriptor(tool_schema=input_schema)
        behavior = _ServerBehavior()
        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[
                types.Tool(
                    name="calendar.lookup",
                    inputSchema=dict(input_schema),
                    outputSchema=None if output_schema is None else dict(output_schema),
                )
            ],
            _meta=_metadata(descriptor),
        )
        client = await _started_configured(descriptor, behavior)
        result = await client.list_tools(
            _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
        )
        await client.close()
        return result

    async def exercise() -> None:
        invalid_input = await one_case(
            {
                "type": "object",
                "properties": {"query": {"type": "not-a-json-schema-type"}},
            },
            None,
        )
        _assert_failed(invalid_input, McpClientErrorReason.RESULT_INVALID)

        invalid_output = await one_case(_TOOL_SCHEMA, {"required": "count"})
        _assert_failed(invalid_output, McpClientErrorReason.RESULT_INVALID)

        unsupported_draft = await one_case(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
            },
            None,
        )
        _assert_failed(unsupported_draft, McpClientErrorReason.RESULT_INVALID)

        unknown_draft = await one_case(
            {
                "$schema": "https://example.invalid/unknown-json-schema",
                "type": "object",
            },
            None,
        )
        _assert_failed(unknown_draft, McpClientErrorReason.RESULT_INVALID)

        supported = await one_case(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
            },
            None,
        )
        assert supported.status is McpOperationStatus.COMPLETED

    asyncio.run(exercise())


def test_nested_tool_schema_drafts_cannot_escape_the_closed_root_draft() -> None:
    async def one_case(
        input_schema: Mapping[str, object],
        output_schema: Mapping[str, object] | None,
    ) -> McpOperationResult:
        descriptor = _descriptor(tool_schema=input_schema)
        behavior = _ServerBehavior()
        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[
                types.Tool(
                    name="calendar.lookup",
                    inputSchema=dict(input_schema),
                    outputSchema=None if output_schema is None else dict(output_schema),
                )
            ],
            _meta=_metadata(descriptor),
        )
        client = await _started_configured(descriptor, behavior)
        result = await client.list_tools(
            _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
        )
        await client.close()
        return result

    async def exercise() -> None:
        nested_unsupported = await one_case(
            {
                "type": "object",
                "properties": {
                    "query": {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "type": "string",
                    }
                },
            },
            None,
        )
        mixed_output = await one_case(
            _TOOL_SCHEMA,
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "count": {
                        "$schema": "https://json-schema.org/draft/2019-09/schema",
                        "type": "integer",
                    }
                },
            },
        )

        _assert_failed(nested_unsupported, McpClientErrorReason.RESULT_INVALID)
        _assert_failed(mixed_output, McpClientErrorReason.RESULT_INVALID)

    asyncio.run(exercise())


def test_recursive_reference_authority_is_closed_before_schema_use() -> None:
    async def tool_case(schema: dict[str, object]) -> tuple[McpOperationResult, int]:
        descriptor = _descriptor(tool_schema=schema)
        behavior = _ServerBehavior()
        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[types.Tool(name="calendar.lookup", inputSchema=schema)],
            _meta=_metadata(descriptor),
        )
        client = await _started_configured(descriptor, behavior)
        result = await client.list_tools(
            _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
        )
        wire_calls = behavior.calls.count("tools/list")
        await client.close()
        return result, wire_calls

    async def elicitation_case(
        schema: dict[str, object],
    ) -> tuple[McpOperationResult, int, int]:
        client, descriptor, _behavior = await _started()
        broker_calls = 0
        wire_calls = 0
        receipt = client.receipt
        result_metadata = _metadata(descriptor)
        result_metadata[_SERVER_INFO_META_KEY] = {
            "name": receipt.server_id,
            "version": receipt.server_version,
        }
        result_metadata["capabilityRevision"] = receipt.capability_revision

        async def needs_input(
            _name: str,
            _arguments: dict[str, Any],
            **_kwargs: Any,
        ) -> types.InputRequiredResult:
            nonlocal wire_calls
            wire_calls += 1
            return types.InputRequiredResult(
                inputRequests={
                    "ask": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Question",
                            requestedSchema=schema,
                        )
                    )
                },
                requestState="state",
                _meta=result_metadata,
            )

        connected = client._connected  # noqa: SLF001 - public session result seam
        assert connected is not None
        connected.session.call_tool = needs_input

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            nonlocal broker_calls
            broker_calls += 1
            return {"answer": "yes"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        await client.close()
        return result, broker_calls, wire_calls

    async def exercise() -> None:
        draft = "https://json-schema.org/draft/2019-09/schema"
        root_external = await tool_case(
            {
                "$schema": draft,
                "$recursiveRef": "https://example.invalid/external-schema",
                "type": "object",
            }
        )
        nested_dialect_escape = await tool_case(
            {
                "$schema": draft,
                "type": "object",
                "properties": {
                    "query": {"$recursiveRef": "http://json-schema.org/draft-07/schema#"}
                },
            }
        )
        elicitation_external = await elicitation_case(
            {
                "$schema": draft,
                "$recursiveRef": "https://example.invalid/external-schema",
                "type": "object",
                "properties": {"answer": {"type": "string"}},
            }
        )

        result, broker_calls, wire_calls = elicitation_external
        assert [
            (
                "root-external",
                root_external[0].status,
                None if root_external[0].error is None else root_external[0].error.reason,
                root_external[1],
            ),
            (
                "nested-dialect-escape",
                nested_dialect_escape[0].status,
                None
                if nested_dialect_escape[0].error is None
                else nested_dialect_escape[0].error.reason,
                nested_dialect_escape[1],
            ),
            (
                "elicitation-external",
                result.status,
                None if result.error is None else result.error.reason,
                broker_calls,
                wire_calls,
            ),
        ] == [
            (
                "root-external",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                1,
            ),
            (
                "nested-dialect-escape",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                1,
            ),
            (
                "elicitation-external",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                0,
                1,
            ),
        ]

    asyncio.run(exercise())


def test_local_reference_targets_are_authority_validated_before_tool_exposure() -> None:
    async def one_case(
        schema: dict[str, object],
    ) -> tuple[McpOperationResult, tuple[str, ...]]:
        descriptor = _descriptor(tool_schema=schema)
        behavior = _ServerBehavior()
        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[types.Tool(name="calendar.lookup", inputSchema=schema)],
            _meta=_metadata(descriptor),
        )
        client = await _started_configured(descriptor, behavior)
        result = await client.list_tools(
            _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
        )
        calls = tuple(behavior.calls)
        await client.close()
        return result, calls

    async def exercise() -> None:
        cases = (
            (
                "recursive-reference",
                {
                    "$schema": "https://json-schema.org/draft/2019-09/schema",
                    "type": "object",
                    "$ref": "#/x-schema",
                    "x-schema": {
                        "$recursiveRef": "https://example.invalid/external-schema",
                        "type": "object",
                    },
                },
            ),
            (
                "dynamic-reference",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "$ref": "#/x-schema",
                    "x-schema": {
                        "$dynamicRef": "https://example.invalid/external-schema",
                        "type": "object",
                    },
                },
            ),
            (
                "plain-reference",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "$ref": "#/x-schema",
                    "x-schema": {
                        "$ref": "https://example.invalid/external-schema",
                        "type": "object",
                    },
                },
            ),
        )
        observed = []
        for case, schema in cases:
            result, calls = await one_case(schema)
            observed.append(
                (
                    case,
                    result.status,
                    None if result.error is None else result.error.reason,
                    calls,
                )
            )

        assert observed == [
            (
                "recursive-reference",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                ("tools/list",),
            ),
            (
                "dynamic-reference",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                ("tools/list",),
            ),
            (
                "plain-reference",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                ("tools/list",),
            ),
        ]

    asyncio.run(exercise())


def test_closed_cyclic_local_schema_references_remain_bounded_and_usable() -> None:
    async def one_case(schema: dict[str, object]) -> tuple[McpOperationResult, tuple[str, ...]]:
        descriptor = _descriptor(tool_schema=schema)
        behavior = _ServerBehavior()
        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[types.Tool(name="calendar.lookup", inputSchema=schema)],
            _meta=_metadata(descriptor),
        )
        client = await _started_configured(descriptor, behavior)
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning", "child": {"query": "review"}},
            )
        )
        calls = tuple(behavior.calls)
        await client.close()
        return result, calls

    async def exercise() -> None:
        schemas = (
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "$defs": {
                    "node": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "child": {"$ref": "#/$defs/node"},
                        },
                        "required": ["query"],
                    }
                },
                "$ref": "#/$defs/node",
            },
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "$defs": {
                    "node": {
                        "$dynamicAnchor": "node",
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "child": {"$dynamicRef": "#node"},
                        },
                        "required": ["query"],
                    }
                },
                "$ref": "#node",
            },
            {
                "$schema": "https://json-schema.org/draft/2019-09/schema",
                "$recursiveAnchor": True,
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "child": {"$recursiveRef": "#"},
                },
                "required": ["query"],
            },
        )
        observed = [await one_case(schema) for schema in schemas]

        assert [result.status for result, _calls in observed] == [
            McpOperationStatus.COMPLETED,
            McpOperationStatus.COMPLETED,
            McpOperationStatus.COMPLETED,
        ]
        assert [calls for _result, calls in observed] == [
            ("tools/list", "tools/call"),
            ("tools/list", "tools/call"),
            ("tools/list", "tools/call"),
        ]

    asyncio.run(exercise())


def test_elicitation_schema_is_meta_validated_before_any_broker_work() -> None:
    async def one_case(schema: dict[str, object]) -> tuple[McpOperationResult, int, int]:
        client, descriptor, behavior = await _started()
        broker_calls = 0

        async def invalid_input(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.InputRequiredResult:
            return types.InputRequiredResult(
                inputRequests={
                    "ask-invalid": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Question",
                            requestedSchema=schema,
                        )
                    )
                },
                requestState="state",
                _meta=_metadata(descriptor),
            )

        behavior.call_override = invalid_input

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            nonlocal broker_calls
            broker_calls += 1
            return {"answer": "yes"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        wire_calls = behavior.calls.count("tools/call")
        await client.close()
        return result, broker_calls, wire_calls

    async def exercise() -> None:
        schemas = (
            {
                "type": "object",
                "properties": {"answer": {"type": "not-a-type"}},
            },
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["not-exposed"],
            },
        )
        for schema in schemas:
            result, broker_calls, wire_calls = await one_case(schema)
            _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
            assert broker_calls == 0
            assert wire_calls == 1

    asyncio.run(exercise())


def test_nested_elicitation_schema_authority_is_closed_before_broker() -> None:
    async def one_case(
        schema: dict[str, object],
    ) -> tuple[McpOperationResult, int, int]:
        client, descriptor, behavior = await _started()
        broker_calls = 0
        wire_calls = 0

        async def needs_input(
            _name: str,
            _arguments: dict[str, Any],
            **_kwargs: Any,
        ) -> types.InputRequiredResult:
            nonlocal wire_calls
            wire_calls += 1
            return types.InputRequiredResult(
                inputRequests={
                    "ask": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Question",
                            requestedSchema=schema,
                        )
                    )
                },
                requestState="state",
                _meta=_metadata(descriptor),
            )

        connected = client._connected  # noqa: SLF001 - public session result seam
        assert connected is not None
        receipt = client._receipt  # noqa: SLF001 - authenticated result metadata fixture
        assert receipt is not None
        result_metadata = _metadata(descriptor)
        result_metadata[_SERVER_INFO_META_KEY] = {
            "name": receipt.server_id,
            "version": receipt.server_version,
        }
        result_metadata["capabilityRevision"] = receipt.capability_revision

        original_needs_input = needs_input

        async def needs_input_with_metadata(
            name: str,
            arguments: dict[str, Any],
            **kwargs: Any,
        ) -> types.InputRequiredResult:
            result = await original_needs_input(name, arguments, **kwargs)
            return result.model_copy(update={"meta": result_metadata})

        connected.session.call_tool = needs_input_with_metadata

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            nonlocal broker_calls
            broker_calls += 1
            return {"answer": "yes"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        await client.close()
        return result, broker_calls, wire_calls

    async def exercise() -> None:
        nested_unsupported = await one_case(
            {
                "type": "object",
                "properties": {
                    "answer": {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "type": "string",
                    }
                },
            }
        )
        nested_hidden_required = await one_case(
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "allOf": [{"required": ["hidden"]}],
            }
        )
        referenced_hidden_required = await one_case(
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "$defs": {"closed": {"required": ["hidden"]}},
                "allOf": [{"$ref": "#/$defs/closed"}],
            }
        )
        dependency_hidden_required = await one_case(
            {
                "$schema": "https://json-schema.org/draft/2019-09/schema",
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "dependencies": {"answer": {"required": ["hidden"]}},
            }
        )

        for case, (result, broker_calls, wire_calls) in (
            ("nested-unsupported", nested_unsupported),
            ("nested-hidden-required", nested_hidden_required),
            ("referenced-hidden-required", referenced_hidden_required),
            ("dependency-hidden-required", dependency_hidden_required),
        ):
            _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
            assert broker_calls == 0, case
            assert wire_calls == 1, case

    asyncio.run(exercise())


def test_embedded_schema_resources_and_property_dependencies_stop_before_broker() -> None:
    async def one_case(
        schema: dict[str, object],
    ) -> tuple[McpOperationResult, int, int]:
        client, descriptor, _behavior = await _started()
        broker_calls = 0
        wire_calls = 0
        receipt = client.receipt
        result_metadata = _metadata(descriptor)
        result_metadata[_SERVER_INFO_META_KEY] = {
            "name": receipt.server_id,
            "version": receipt.server_version,
        }
        result_metadata["capabilityRevision"] = receipt.capability_revision

        async def needs_input(
            _name: str,
            _arguments: dict[str, Any],
            **kwargs: Any,
        ) -> types.CallToolResult | types.InputRequiredResult:
            nonlocal wire_calls
            wire_calls += 1
            if kwargs.get("input_responses") is not None:
                return types.CallToolResult(
                    content=[types.TextContent(text="complete")],
                    structuredContent={"count": 1},
                    _meta=result_metadata,
                )
            return types.InputRequiredResult(
                inputRequests={
                    "ask": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Question",
                            requestedSchema=schema,
                        )
                    )
                },
                requestState="state",
                _meta=result_metadata,
            )

        connected = client._connected  # noqa: SLF001 - public session result seam
        assert connected is not None
        connected.session.call_tool = needs_input

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            nonlocal broker_calls
            broker_calls += 1
            return {"answer": "yes"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        await client.close()
        return result, broker_calls, wire_calls

    async def exercise() -> None:
        embedded_resource = await one_case(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "$defs": {
                    "closed": {},
                    "embedded": {
                        "$id": "urn:embedded",
                        "$defs": {"closed": {"required": ["hidden"]}},
                        "$ref": "#/$defs/closed",
                    },
                },
                "allOf": [{"$ref": "#/$defs/embedded"}],
            }
        )
        dependent_required_2020 = await one_case(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "dependentRequired": {"answer": ["hidden"]},
            }
        )
        dependent_required_2019 = await one_case(
            {
                "$schema": "https://json-schema.org/draft/2019-09/schema",
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "dependentRequired": {"answer": ["hidden"]},
            }
        )
        exposed_dependency = await one_case(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "dependentRequired": {"answer": ["answer"]},
            }
        )

        assert [
            (
                case,
                result.status,
                None if result.error is None else result.error.reason,
                broker_calls,
                wire_calls,
            )
            for case, (result, broker_calls, wire_calls) in (
                ("embedded-resource", embedded_resource),
                ("dependent-required-2020", dependent_required_2020),
                ("dependent-required-2019", dependent_required_2019),
            )
        ] == [
            (
                "embedded-resource",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                0,
                1,
            ),
            (
                "dependent-required-2020",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                0,
                1,
            ),
            (
                "dependent-required-2019",
                McpOperationStatus.FAILED,
                McpClientErrorReason.RESULT_INVALID,
                0,
                1,
            ),
        ]
        valid_result, broker_calls, wire_calls = exposed_dependency
        assert valid_result.status is McpOperationStatus.COMPLETED
        assert broker_calls == 1
        assert wire_calls == 2

    asyncio.run(exercise())


def test_independent_structured_prompt_and_broker_response_byte_caps() -> None:
    async def structured_case(cap: int) -> McpOperationResult:
        client, descriptor, behavior = await _started(
            limits=McpClientLimits(
                max_structured_result_bytes=cap,
                max_result_document_bytes=1_024,
            )
        )
        behavior.tool_result = types.CallToolResult(
            content=[types.TextContent(text="ok")],
            structuredContent={"count": 2},
        )
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "x"},
            )
        )
        await client.close()
        return result

    async def prompt_case(topic: str) -> tuple[McpOperationResult, int]:
        client, descriptor, behavior = await _started(
            limits=McpClientLimits(max_tool_argument_bytes=13)
        )
        result = await client.get_prompt(
            _request(
                descriptor,
                McpOperationKind.PROMPT_GET,
                "prompt:calendar.compose",
                {"topic": topic},
            )
        )
        calls = behavior.calls.count("prompts/get")
        await client.close()
        return result, calls

    async def broker_case(
        answer: str | Mapping[str, object],
    ) -> tuple[McpOperationResult, int]:
        client, descriptor, behavior = await _started(
            limits=McpClientLimits(max_tool_argument_bytes=14)
        )

        async def needs_input(
            _ctx: Any, params: types.CallToolRequestParams
        ) -> types.CallToolResult | types.InputRequiredResult:
            if params.input_responses is None:
                return types.InputRequiredResult(
                    inputRequests={
                        "ask": types.ElicitRequest(
                            params=types.ElicitRequestFormParams(
                                message="Question",
                                requestedSchema={
                                    "type": "object",
                                    "properties": {"answer": {"type": "string"}},
                                },
                            )
                        )
                    },
                    requestState="state",
                    _meta=_metadata(descriptor),
                )
            return types.CallToolResult(
                content=[types.TextContent(text="complete")],
                structuredContent={"count": 1},
                _meta=_metadata(descriptor),
            )

        behavior.call_override = needs_input

        async def broker(_request: McpInputRequest) -> Mapping[str, object]:
            return {"answer": answer} if type(answer) is str else answer

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "x"},
            ),
            input_callback=broker,
        )
        calls = behavior.calls.count("tools/call")
        await client.close()
        return result, calls

    async def exercise() -> None:
        assert (await structured_case(11)).status is McpOperationStatus.COMPLETED
        _assert_failed(
            await structured_case(10),
            McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
        )

        exact_prompt, exact_prompt_calls = await prompt_case("x")
        assert exact_prompt.status is McpOperationStatus.COMPLETED
        assert exact_prompt_calls == 1
        over_prompt, over_prompt_calls = await prompt_case("xx")
        _assert_failed(over_prompt, McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        assert over_prompt_calls == 0

        exact_broker, exact_broker_calls = await broker_case("x")
        assert exact_broker.status is McpOperationStatus.COMPLETED
        assert exact_broker_calls == 2
        over_broker, over_broker_calls = await broker_case("xx")
        _assert_failed(over_broker, McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
        assert over_broker_calls == 1

        late = _LateAccessMapping()
        early_broker, early_broker_calls = await broker_case(late)
        _assert_failed(early_broker, McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
        assert early_broker_calls == 1
        assert late.late_accessed is False

    asyncio.run(exercise())


def test_rfc6570_template_matching_supports_operators_and_rejects_ambiguity() -> None:
    async def one_case(template: str, uri: str) -> tuple[McpOperationResult, int]:
        client, descriptor, behavior = await _started()
        behavior.list_override["templates"] = lambda _cursor: types.ListResourceTemplatesResult(
            resourceTemplates=[
                types.ResourceTemplate(
                    name="calendar.entry",
                    uriTemplate=template,
                    mimeType="text/plain",
                )
            ],
            _meta=_metadata(descriptor),
        )
        receipt = client._receipt  # noqa: SLF001 - injected reviewed template authority
        assert receipt is not None
        object.__setattr__(
            receipt,
            "resource_templates",
            {
                "calendar.entry": {
                    "name": "calendar.entry",
                    "uri_template": template,
                    "description": None,
                    "mime_type": "text/plain",
                }
            },
        )

        def read(params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri=params.uri,
                        mimeType="text/plain",
                        text="Template entry.",
                    )
                ],
                _meta=_metadata(descriptor),
            )

        behavior.read_override = read
        result = await client.read_resource(
            _request(
                descriptor,
                McpOperationKind.RESOURCE_READ,
                "resource:calendar.entry",
                {"uri": uri},
            )
        )
        calls = behavior.calls.count("resources/read")
        await client.close()
        return result, calls

    async def exercise() -> None:
        valid = [
            (
                "docs://calendar{/segments*}{?filter,limit}{#section}",
                "docs://calendar/one/two?filter=active&limit=2#today",
            ),
            ("docs://calendar/{value:1}", "docs://calendar/%C3%A9"),
            ("docs://calendar{;keys*}", "docs://calendar;lang=en;tz=utc"),
            ("docs://calendar{/first:1,second:2}", "docs://calendar/a/bb"),
            ("docs://calendar{.value:3}", "docs://calendar.a.b"),
        ]
        for template, uri in valid:
            result, calls = await one_case(template, uri)
            assert result.status is McpOperationStatus.COMPLETED
            assert calls == 1

        ambiguous, calls = await one_case(
            "docs://calendar/{left}{right}",
            "docs://calendar/ab",
        )
        _assert_failed(ambiguous, McpClientErrorReason.RESULT_INVALID)
        assert calls == 0

        invalid_prefixes = [
            ("docs://calendar/{+path:12}", "docs://calendar/a/b%20c"),
            ("{+uri}", "docs://calendar/entry?view=full#today"),
            ("docs://calendar/{value:1}", "docs://calendar/ab"),
            ("docs://calendar/{+value:1}", "docs://calendar/ab"),
            ("docs://calendar/{#value:1}", "docs://calendar/#ab"),
            ("docs://calendar{.value:1}", "docs://calendar.ab"),
            ("docs://calendar{/value:1}", "docs://calendar/ab"),
            ("docs://calendar{;value:1}", "docs://calendar;value=ab"),
            ("docs://calendar{?value:1}", "docs://calendar?value=ab"),
            (
                "docs://calendar?fixed=yes{&value:1}",
                "docs://calendar?fixed=yes&value=ab",
            ),
            (
                "docs://calendar{/first:1,second:2}",
                "docs://calendar/aa/bb",
            ),
        ]
        for template, uri in invalid_prefixes:
            result, calls = await one_case(template, uri)
            _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
            assert calls == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("surface", ["tool_output", "resource_uri", "resource_mime", "template"])
def test_signed_discovery_authority_rejects_runtime_catalog_drift(surface: str) -> None:
    """Catches live discovery replacing signed schemas, endpoints, or MIME authority."""

    async def exercise() -> None:
        descriptor = _descriptor(signed_capability_authority=_signed_authority())
        behavior = _ServerBehavior()
        metadata = _metadata(descriptor)
        if surface == "tool_output":
            behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="calendar.lookup",
                        inputSchema=_TOOL_SCHEMA,
                        outputSchema={"type": "string"},
                    )
                ],
                _meta=metadata,
            )
        elif surface in {"resource_uri", "resource_mime"}:
            behavior.list_override["resources"] = lambda _cursor: types.ListResourcesResult(
                resources=[
                    types.Resource(
                        name="calendar.help",
                        uri=(
                            "docs://calendar/forged"
                            if surface == "resource_uri"
                            else "docs://calendar/help"
                        ),
                        mimeType=(
                            "application/octet-stream"
                            if surface == "resource_mime"
                            else "text/plain"
                        ),
                    )
                ],
                _meta=metadata,
            )
        else:
            behavior.list_override["templates"] = lambda _cursor: types.ListResourceTemplatesResult(
                resourceTemplates=[
                    types.ResourceTemplate(
                        name="calendar.entry",
                        uriTemplate="docs://forged/{id}",
                        mimeType="text/plain",
                    )
                ],
                _meta=metadata,
            )
        client = CurrentMcpClient(
            descriptor,
            SdkClientFactory.for_in_process_server(_server(descriptor, behavior)),
        )
        with pytest.raises(McpClientError, match="^capability_drift$"):
            await client.start()
        await client.close()

    asyncio.run(exercise())


def test_signed_output_policy_accepts_provider_that_omits_output_schema() -> None:
    """Catches rejecting an official provider that leaves output policy to Omega."""

    async def exercise() -> None:
        descriptor = _descriptor(signed_capability_authority=_signed_authority())
        behavior = _ServerBehavior()
        behavior.list_override["tools"] = lambda _cursor: types.ListToolsResult(
            tools=[
                types.Tool(
                    name="calendar.lookup",
                    inputSchema=_TOOL_SCHEMA,
                    outputSchema=None,
                )
            ],
            _meta=_metadata(descriptor),
        )
        client = await _started_configured(descriptor, behavior)

        behavior.tool_result = types.CallToolResult(
            content=[types.TextContent(text="bad structured output")],
            structuredContent={"count": "two"},
        )
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )

        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("policy", ["tool_bytes", "tool_mime"])
def test_signed_tool_result_policy_beats_generic_runtime_limits(policy: str) -> None:
    """Catches replacing signed per-tool byte/MIME policy with the generic 1 MiB cap."""

    async def exercise() -> None:
        authority = (
            _signed_authority(tool_max_result_bytes=96)
            if policy == "tool_bytes"
            else _signed_authority(tool_mime_types=("application/json",))
        )
        descriptor = _descriptor(signed_capability_authority=authority)
        behavior = _ServerBehavior()
        client = await _started_configured(descriptor, behavior)
        behavior.tool_result = types.CallToolResult(
            content=[
                types.TextContent(text="x" * 256 if policy == "tool_bytes" else "not signed MIME")
            ],
            structuredContent={"count": 2},
        )

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )

        _assert_failed(
            result,
            (
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED
                if policy == "tool_bytes"
                else McpClientErrorReason.RESULT_INVALID
            ),
        )
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("capability", ["static_mime", "static_bytes", "template_bytes"])
def test_signed_resource_policy_enforces_mime_uri_template_and_byte_cap(capability: str) -> None:
    """Catches trusting runtime resource metadata or only the generic resource cap."""

    async def exercise() -> None:
        descriptor = _descriptor(
            signed_capability_authority=_signed_authority(resource_max_bytes=32)
        )
        behavior = _ServerBehavior()
        client = await _started_configured(descriptor, behavior)

        def read(params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri=params.uri,
                        mimeType=(
                            "application/octet-stream"
                            if capability == "static_mime"
                            else "text/plain"
                        ),
                        text="x" * 128,
                    )
                ],
                _meta=_metadata(descriptor),
            )

        behavior.read_override = read
        is_template = capability == "template_bytes"
        result = await client.read_resource(
            _request(
                descriptor,
                McpOperationKind.RESOURCE_READ,
                "resource:calendar.entry" if is_template else "resource:calendar.help",
                {"uri": "docs://calendar/entry/42"} if is_template else {},
            )
        )

        _assert_failed(
            result,
            (
                McpClientErrorReason.RESULT_INVALID
                if capability == "static_mime"
                else McpClientErrorReason.RESULT_LIMIT_EXCEEDED
            ),
        )
        await client.close()

    asyncio.run(exercise())


def test_signed_resource_limit_counts_utf8_payload_across_all_returned_items() -> None:
    """Catches resetting the signed resource byte budget for every returned item."""

    async def exercise() -> None:
        descriptor = _descriptor(
            signed_capability_authority=_signed_authority(resource_max_bytes=10)
        )
        behavior = _ServerBehavior()
        client = await _started_configured(descriptor, behavior)

        def read(params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri=params.uri,
                        mimeType="text/plain",
                        text="éé",  # 4 UTF-8 payload bytes.
                    ),
                    types.TextResourceContents(
                        uri=params.uri,
                        mimeType="text/plain",
                        text="🙂🙂",  # 8 UTF-8 payload bytes; aggregate is 12.
                    ),
                ],
                _meta=_metadata(descriptor),
            )

        behavior.read_override = read
        try:
            result = await client.read_resource(
                _request(
                    descriptor,
                    McpOperationKind.RESOURCE_READ,
                    "resource:calendar.help",
                )
            )
            _assert_failed(result, McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        finally:
            await client.close()

    asyncio.run(exercise())


def test_signed_resource_aggregate_limit_accepts_exact_utf8_boundary() -> None:
    """Catches double-counting item or envelope bytes in the signed payload budget."""

    async def exercise() -> None:
        descriptor = _descriptor(
            signed_capability_authority=_signed_authority(resource_max_bytes=10)
        )
        behavior = _ServerBehavior()
        client = await _started_configured(descriptor, behavior)

        def read(params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(uri=params.uri, mimeType="text/plain", text="abc"),
                    types.TextResourceContents(uri=params.uri, mimeType="text/plain", text="é"),
                    types.TextResourceContents(uri=params.uri, mimeType="text/plain", text="🙂x"),
                ],
                _meta=_metadata(descriptor),
            )

        behavior.read_override = read
        try:
            result = await client.read_resource(
                _request(
                    descriptor,
                    McpOperationKind.RESOURCE_READ,
                    "resource:calendar.help",
                )
            )
            assert result.status is McpOperationStatus.COMPLETED
            assert [item["text"] for item in result.data["contents"]] == [
                "abc",
                "é",
                "🙂x",
            ]
        finally:
            await client.close()

    asyncio.run(exercise())


def test_multiple_input_requests_share_one_round_and_total_caps_precede_broker() -> None:
    async def one_case(
        limits: McpClientLimits,
    ) -> tuple[McpOperationResult, list[McpInputRequest], int]:
        client, descriptor, behavior = await _started(limits=limits)
        broker_requests: list[McpInputRequest] = []

        async def two_requests(
            _ctx: Any, params: types.CallToolRequestParams
        ) -> types.CallToolResult | types.InputRequiredResult:
            if params.input_responses is None:
                return types.InputRequiredResult(
                    inputRequests={
                        request_id: types.ElicitRequest(
                            params=types.ElicitRequestFormParams(
                                message=f"Question {request_id}",
                                requestedSchema={
                                    "type": "object",
                                    "properties": {"answer": {"type": "string"}},
                                },
                            )
                        )
                        for request_id in ("ask-a", "ask-b")
                    },
                    requestState="round-state",
                    _meta=_metadata(descriptor),
                )
            assert set(params.input_responses) == {"ask-a", "ask-b"}
            assert params.request_state == "round-state"
            return types.CallToolResult(
                content=[types.TextContent(text="complete")],
                structuredContent={"count": 2},
                _meta=_metadata(descriptor),
            )

        behavior.call_override = two_requests

        async def broker(request: McpInputRequest) -> Mapping[str, object]:
            broker_requests.append(request)
            return {"answer": "yes"}

        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            ),
            input_callback=broker,
        )
        calls = behavior.calls.count("tools/call")
        await client.close()
        return result, broker_requests, calls

    async def exercise() -> None:
        completed, requests, calls = await one_case(
            McpClientLimits(max_input_rounds=1, max_input_fields=2)
        )
        assert completed.status is McpOperationStatus.COMPLETED
        assert calls == 2
        assert [request.round_number for request in requests] == [1, 1]
        assert all(request.request_state == b"round-state" for request in requests)

        capped, requests, calls = await one_case(
            McpClientLimits(max_input_rounds=1, max_input_fields=1)
        )
        _assert_failed(capped, McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
        assert requests == []
        assert calls == 1

    asyncio.run(exercise())


def test_thrown_sdk_mcp_error_is_external_failure_not_invalid_result() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()

        async def protocol_failure(
            _ctx: Any, _params: types.CallToolRequestParams
        ) -> types.CallToolResult:
            raise MCPError(types.INTERNAL_ERROR, "secret external failure body")

        behavior.call_override = protocol_failure
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:calendar.lookup",
                {"query": "planning"},
            )
        )

        _assert_failed(result, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        assert "secret external failure body" not in repr(result)
        assert result.to_dict()["error"] == {"reason": "transport_unavailable"}
        await client.close()

    asyncio.run(exercise())


def test_subscription_listen_reconnect_is_explicit_bounded_and_event_precedes_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        connected = client._connected
        assert connected is not None
        calls = 0
        ordering: list[str] = []
        description = ["stale"]

        def list_tools(_cursor: str | None) -> types.ListToolsResult:
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="calendar.lookup",
                        description=description[0],
                        inputSchema=_TOOL_SCHEMA,
                        outputSchema=_TOOL_OUTPUT_SCHEMA,
                    )
                ],
                ttlMs=30_000,
                cacheScope="private",
                _meta=_metadata(descriptor),
            )

        behavior.list_override["tools"] = list_tools
        primed = await client.list_tools(
            _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
        )
        assert primed.data["tools"][0]["description"] == "stale"
        description[0] = "fresh"

        def listen(**options: object) -> _ScriptedListenContext:
            nonlocal calls
            calls += 1
            assert options == {
                "tools_list_changed": True,
                "prompts_list_changed": False,
                "resources_list_changed": False,
                "resource_subscriptions": (),
            }
            if calls == 1:
                return _ScriptedListenContext(_ScriptedSubscription([SubscriptionLost("dropped")]))
            ordering.append("reopened")
            return _ScriptedListenContext(_ScriptedSubscription([ToolsListChanged()]))

        monkeypatch.setattr(connected, "listen", listen)

        async def consume(event: McpOperationResult) -> None:
            assert event.status is McpOperationStatus.PROGRESS
            if event.data == {"kind": "subscription_gap", "reconnect": 1}:
                ordering.append("gap")
                refreshed = await client.list_tools(
                    _request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")
                )
                assert refreshed.data["tools"][0]["description"] == "fresh"
                ordering.append("gap_refetched")
                return
            assert event.data == {"kind": "tools_list_changed"}
            await client.list_tools(_request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list"))
            ordering.append("refetched")

        result = await client.listen(
            _request(
                descriptor,
                McpOperationKind.SUBSCRIPTIONS_LISTEN,
                "subscriptions/listen",
                {"tools_list_changed": True, "max_reconnects": 1},
            ),
            event_callback=consume,
        )

        assert result.status is McpOperationStatus.COMPLETED
        assert result.data == {"events": 1, "reconnects": 1}
        assert calls == 2
        assert ordering == ["gap", "gap_refetched", "reopened", "refetched"]
        assert behavior.calls == ["tools/list", "tools/list"]
        await client.close()

    asyncio.run(exercise())


def test_close_cancels_outer_listener_after_nested_callback_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        client, descriptor, _behavior = await _started()
        connected = client._connected
        assert connected is not None
        callback_blocked = asyncio.Event()

        class OneThenBlock:
            def __init__(self) -> None:
                self.sent = False

            def __aiter__(self) -> OneThenBlock:
                return self

            async def __anext__(self) -> object:
                if not self.sent:
                    self.sent = True
                    return ToolsListChanged()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        monkeypatch.setattr(
            connected,
            "listen",
            lambda **_options: _ScriptedListenContext(OneThenBlock()),
        )

        async def consume(_event: McpOperationResult) -> None:
            await client.list_tools(_request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list"))
            callback_blocked.set()

        listener = asyncio.create_task(
            client.listen(
                _request(
                    descriptor,
                    McpOperationKind.SUBSCRIPTIONS_LISTEN,
                    "subscriptions/listen",
                    {"tools_list_changed": True},
                ),
                event_callback=consume,
            )
        )
        await callback_blocked.wait()
        await asyncio.wait_for(client.close(), timeout=1)
        assert listener.done()
        with pytest.raises(asyncio.CancelledError):
            await listener

    asyncio.run(exercise())


@pytest.mark.parametrize("callback_kind", ["runtime", "cancelled", "base"])
def test_subscription_callback_error_has_identity_precedence_over_exit_and_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    callback_kind: str,
) -> None:
    class CallbackBaseError(BaseException):
        pass

    async def exercise() -> None:
        client, descriptor, _behavior = await _started()
        connected = client._connected
        assert connected is not None
        calls = 0
        marker: BaseException
        if callback_kind == "runtime":
            marker = RuntimeError("callback marker")
        elif callback_kind == "cancelled":
            marker = asyncio.CancelledError("callback marker")
        else:
            marker = CallbackBaseError("callback marker")

        def listen(**_options: object) -> _ReplacingExitContext:
            nonlocal calls
            calls += 1
            return _ReplacingExitContext(_ScriptedSubscription([ToolsListChanged()]))

        monkeypatch.setattr(connected, "listen", listen)

        async def fail_callback(_event: McpOperationResult) -> None:
            raise marker

        with pytest.raises(type(marker)) as raised:
            await client.listen(
                _request(
                    descriptor,
                    McpOperationKind.SUBSCRIPTIONS_LISTEN,
                    "subscriptions/listen",
                    {"tools_list_changed": True, "max_reconnects": 2},
                ),
                event_callback=fail_callback,
            )
        assert raised.value is marker
        assert calls == 1
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "uri",
    [
        "docs://calendar/other",
        "docs://CALENDAR/help",
        "docs://calendar/%68elp",
        "docs://calendar/entry/../help",
        "docs://calendar/help\\alias",
        "docs://calendar/help\r",
    ],
)
def test_subscription_uri_must_be_canonical_exact_declared_authority(
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    async def exercise() -> None:
        descriptor = _descriptor(signed_capability_authority=_signed_authority())
        client = await _started_configured(descriptor, _ServerBehavior())
        connected = client._connected
        assert connected is not None
        listen_called = False

        def listen(**_options: object) -> _ScriptedListenContext:
            nonlocal listen_called
            listen_called = True
            return _ScriptedListenContext(_ScriptedSubscription([]))

        monkeypatch.setattr(connected, "listen", listen)
        result = await client.listen(
            _request(
                descriptor,
                McpOperationKind.SUBSCRIPTIONS_LISTEN,
                "subscriptions/listen",
                {"resource_subscriptions": [uri]},
            ),
            event_callback=lambda _event: asyncio.sleep(0),
        )
        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        assert listen_called is False
        await client.close()

    asyncio.run(exercise())


def test_subscription_accepts_only_exact_resource_or_template_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        descriptor = _descriptor(signed_capability_authority=_signed_authority())
        client = await _started_configured(descriptor, _ServerBehavior())
        connected = client._connected
        assert connected is not None
        seen: list[object] = []

        def listen(**options: object) -> _ScriptedListenContext:
            seen.append(options["resource_subscriptions"])
            return _ScriptedListenContext(_ScriptedSubscription([]))

        monkeypatch.setattr(connected, "listen", listen)
        result = await client.listen(
            _request(
                descriptor,
                McpOperationKind.SUBSCRIPTIONS_LISTEN,
                "subscriptions/listen",
                {
                    "resource_subscriptions": [
                        "docs://calendar/help",
                        "docs://calendar/entry/one",
                    ]
                },
            ),
            event_callback=lambda _event: asyncio.sleep(0),
        )
        assert result.status is McpOperationStatus.COMPLETED
        assert seen == [("docs://calendar/help", "docs://calendar/entry/one")]
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "event_uri",
    [
        "docs://CALENDAR/help",
        "docs://calendar/help\rserver-secret",
        "docs://calendar/other",
        "docs://calendar/entry/two",
    ],
)
def test_resource_updated_event_is_canonical_and_bound_to_exact_subscription(
    monkeypatch: pytest.MonkeyPatch,
    event_uri: str,
) -> None:
    async def exercise() -> None:
        descriptor = _descriptor(signed_capability_authority=_signed_authority())
        client = await _started_configured(descriptor, _ServerBehavior())
        connected = client._connected
        assert connected is not None
        monkeypatch.setattr(
            connected,
            "listen",
            lambda **_options: _ScriptedListenContext(
                _ScriptedSubscription([ResourceUpdated(uri=event_uri)])
            ),
        )
        observed: list[McpOperationResult] = []
        result = await client.listen(
            _request(
                descriptor,
                McpOperationKind.SUBSCRIPTIONS_LISTEN,
                "subscriptions/listen",
                {"resource_subscriptions": ["docs://calendar/help"]},
            ),
            event_callback=lambda event: observed.append(event) or asyncio.sleep(0),
        )
        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        assert observed == []
        assert "server-secret" not in repr(result)
        await client.close()

    asyncio.run(exercise())


def test_resource_updated_event_delivers_only_the_exact_subscribed_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        descriptor = _descriptor(signed_capability_authority=_signed_authority())
        client = await _started_configured(descriptor, _ServerBehavior())
        connected = client._connected
        assert connected is not None
        monkeypatch.setattr(
            connected,
            "listen",
            lambda **_options: _ScriptedListenContext(
                _ScriptedSubscription([ResourceUpdated(uri="docs://calendar/help")])
            ),
        )
        observed: list[Mapping[str, object]] = []

        async def capture(event: McpOperationResult) -> None:
            observed.append(event.data)

        result = await client.listen(
            _request(
                descriptor,
                McpOperationKind.SUBSCRIPTIONS_LISTEN,
                "subscriptions/listen",
                {"resource_subscriptions": ["docs://calendar/help"]},
            ),
            event_callback=capture,
        )
        assert result.status is McpOperationStatus.COMPLETED
        assert observed == [{"kind": "resource_updated", "uri": "docs://calendar/help"}]
        await client.close()

    asyncio.run(exercise())


def test_subscription_uri_rejects_unsigned_discovery_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        client, descriptor, _behavior = await _started()
        connected = client._connected
        assert connected is not None
        listen_called = False

        def listen(**_options: object) -> _ScriptedListenContext:
            nonlocal listen_called
            listen_called = True
            return _ScriptedListenContext(_ScriptedSubscription([]))

        monkeypatch.setattr(connected, "listen", listen)
        result = await client.listen(
            _request(
                descriptor,
                McpOperationKind.SUBSCRIPTIONS_LISTEN,
                "subscriptions/listen",
                {"resource_subscriptions": ["docs://calendar/help"]},
            ),
            event_callback=lambda _event: asyncio.sleep(0),
        )
        _assert_failed(result, McpClientErrorReason.RESULT_INVALID)
        assert listen_called is False
        await client.close()

    asyncio.run(exercise())


def test_subscription_listen_enforces_event_cap_and_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        limits = McpClientLimits(max_notification_events=1)
        client, descriptor, _behavior = await _started(limits=limits)
        connected = client._connected
        assert connected is not None

        def over_limit(**_options: object) -> _ScriptedListenContext:
            return _ScriptedListenContext(
                _ScriptedSubscription([ToolsListChanged(), ToolsListChanged()])
            )

        monkeypatch.setattr(connected, "listen", over_limit)

        async def capture(event: McpOperationResult) -> None:
            events.append(event)

        events: list[McpOperationResult] = []
        capped = await client.listen(
            _request(
                descriptor,
                McpOperationKind.SUBSCRIPTIONS_LISTEN,
                "subscriptions/listen",
                {"tools_list_changed": True},
            ),
            event_callback=capture,
        )
        _assert_failed(capped, McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        assert len(events) == 1

        entered = asyncio.Event()

        class BlockingSubscription:
            def __aiter__(self) -> BlockingSubscription:
                return self

            async def __anext__(self) -> object:
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        monkeypatch.setattr(
            connected,
            "listen",
            lambda **_options: _ScriptedListenContext(BlockingSubscription()),
        )
        pending = asyncio.create_task(
            client.listen(
                _request(
                    descriptor,
                    McpOperationKind.SUBSCRIPTIONS_LISTEN,
                    "subscriptions/listen",
                    {"tools_list_changed": True},
                ),
                event_callback=capture,
            )
        )
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await client.close()

    asyncio.run(exercise())


def test_subscription_loss_after_explicit_reconnect_budget_is_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        client, descriptor, _behavior = await _started()
        connected = client._connected
        assert connected is not None
        calls = 0

        def lost(**_options: object) -> _ScriptedListenContext:
            nonlocal calls
            calls += 1
            return _ScriptedListenContext(
                _ScriptedSubscription([SubscriptionLost("external stream detail")])
            )

        monkeypatch.setattr(connected, "listen", lost)

        gaps: list[Mapping[str, object]] = []

        async def consume(event: McpOperationResult) -> None:
            gaps.append(event.data)

        result = await client.listen(
            _request(
                descriptor,
                McpOperationKind.SUBSCRIPTIONS_LISTEN,
                "subscriptions/listen",
                {"tools_list_changed": True, "max_reconnects": 1},
            ),
            event_callback=consume,
        )

        _assert_failed(result, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        assert calls == 2
        assert gaps == [{"kind": "subscription_gap", "reconnect": 1}]
        assert "external stream detail" not in repr(result)
        await client.close()

    asyncio.run(exercise())


def _signed_tool_policy(allowed_mime_types: object) -> dict[str, object]:
    return {"allowed_mime_types": allowed_mime_types, "max_result_bytes": 1_000_000}


def _text_result() -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": '{"teams":[]}'}],
        "structured_content": None,
        "is_error": False,
    }


def test_signed_result_policy_accepts_the_list_a_signed_package_actually_carries() -> None:
    """The guard demanded a tuple, and no signed policy is ever spelled that way.

    A package's authority is JSON inside the verifier's mappingproxy, and
    `_installed_package_authority` builds the same field with `list(...)`. So
    the check refused every policy Omega constructs, on its TYPE, before looking
    at a single content block -- every plugin tool call answered `result_invalid`
    whatever the provider returned.

    It hid behind a real bug with the same reason: 425 of 431 tools were signed
    `application/json` only while providers answer with a text block. Re-releasing
    every package fixed that one, and a clean 200 from Linear with one allowed
    text block still failed until this.
    """

    from mcp_platform.client import _enforce_signed_tool_result

    allowed = ["application/json", "text/plain"]
    enforced = _enforce_signed_tool_result(_text_result(), _signed_tool_policy(allowed))
    assert enforced["content"][0]["type"] == "text"
    # A tuple keeps working; the point is that both spellings mean the same set.
    assert _enforce_signed_tool_result(
        _text_result(),
        _signed_tool_policy(tuple(allowed)),
    ) == enforced


def test_signed_result_policy_still_refuses_a_mime_type_outside_the_signed_set() -> None:
    """Widening the accepted spelling must not widen the accepted content."""

    from mcp_platform.client import _enforce_signed_tool_result

    with pytest.raises(McpClientError) as raised:
        _enforce_signed_tool_result(
            _text_result(),
            _signed_tool_policy(["application/json"]),
        )

    assert raised.value.reason is McpClientErrorReason.RESULT_INVALID


def test_signed_result_policy_refuses_a_collection_that_is_not_mime_type_strings() -> None:
    """A bare string is not a set of mime types -- iterating one yields letters."""

    from mcp_platform.client import _enforce_signed_tool_result

    for malformed in ("application/json", ["application/json", 7], None, {"application/json"}):
        with pytest.raises(McpClientError) as raised:
            _enforce_signed_tool_result(_text_result(), _signed_tool_policy(malformed))
        assert raised.value.reason is McpClientErrorReason.RESULT_INVALID, malformed
