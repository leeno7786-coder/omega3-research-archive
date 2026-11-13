from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import Literal

import anyio
import pytest
from mcp import types
from mcp.server import CacheHint

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, ResourceCatalog, ToolCatalog
from mcp_platform.sdk import (
    FirstPartyMcpServer,
    ServerCatalogError,
    ServerCleanupError,
    ServerSchemaError,
)
from mcp_platform.sdk import server as server_module


class _ActiveServerBaseError(BaseException):
    pass


class _ClaimedResult(types.Result):
    result_type: Literal["claimed"] = "claimed"


def _schema(value_type: str) -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"text": {"type": value_type}},
        "required": ["text"],
        "additionalProperties": False,
    }


@pytest.fixture
def catalogs() -> tuple[ToolCatalog, ResourceCatalog]:
    input_schema = _schema("string")
    output_schema = _schema("string")
    tools = ToolCatalog.from_dict(
        {
            "catalog_version": 1,
            "server_id": "omega.fake",
            "server_version": "1.0.0",
            "tools": [
                {
                    "name": "omega.fake.echo",
                    "title": "Echo",
                    "input_schema": input_schema,
                    "output_schema": output_schema,
                    "input_schema_sha256": sha256_hex(canonical_bytes(input_schema)),
                    "output_schema_sha256": sha256_hex(canonical_bytes(output_schema)),
                    "capability_tags": ["fake.read"],
                    "provider_extension_tags": [],
                    "risk_class": "read_only",
                    "effect": "echo",
                    "requires_revision": False,
                    "max_result_bytes": 256,
                    "allowed_mime_types": ["application/json", "text/plain"],
                }
            ],
        }
    )
    resources = ResourceCatalog.from_dict(
        {
            "catalog_version": 1,
            "server_id": "omega.fake",
            "server_version": "1.0.0",
            "resources": [
                {
                    "uri": "omega://fake/help",
                    "name": "help",
                    "mime_type": "text/plain",
                    "max_bytes": 16,
                    "capability_tags": ["fake.read"],
                }
            ],
            "resource_templates": [
                {
                    "uri_template": "omega://fake/items/{id}",
                    "name": "item",
                    "mime_type": "application/json",
                    "max_bytes": 32,
                    "capability_tags": ["fake.read"],
                }
            ],
        }
    )
    return tools, resources


async def _echo(arguments: Mapping[str, object]) -> Mapping[str, object]:
    return {"text": arguments["text"]}


async def _resource(uri: str) -> bytes:
    return uri.rsplit("/", 1)[-1].encode()


@pytest.fixture
def complete_server(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> FirstPartyMcpServer:
    server = FirstPartyMcpServer(*catalogs)
    server.register_tool("omega.fake.echo", _echo)
    server.register_resource("omega://fake/help", _resource)
    server.register_resource("omega://fake/items/{id}", _resource)
    server.assert_complete()
    return server


def test_host_identity_stamps_current_protocol_discover(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    from mcp_platform.sdk.server import ServerHostIdentity

    tools, resources = catalogs
    server = FirstPartyMcpServer(
        tools,
        resources,
        host_identity=ServerHostIdentity(
            publisher_id="example.publisher",
            deployment="local",
            transport="stdio",
        ),
    )
    server.register_tool("omega.fake.echo", _echo)
    server.register_resource("omega://fake/help", _resource)
    server.register_resource("omega://fake/items/{id}", _resource)

    async def inspect() -> None:
        discovered = await server._handle_discover(None, None)
        listed = await server._handle_list_tools(None, None)
        assert discovered.supported_versions == [MCP_PROTOCOL_VERSION]
        assert discovered.meta["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert discovered.meta["appId"] == "omega.fake"
        assert discovered.meta["publisherId"] == "example.publisher"
        assert discovered.meta["deployment"] == "local"
        assert discovered.meta["transport"] == "stdio"
        assert listed.meta["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert listed.meta["io.modelcontextprotocol/serverInfo"] == {
            "name": "omega.fake",
            "version": "1.0.0",
        }

    asyncio.run(inspect())


def test_handler_must_exist_for_every_signed_entry(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches starting a server whose signed catalog cannot actually be served.
    server = FirstPartyMcpServer(*catalogs)
    with pytest.raises(ServerCatalogError, match="missing handlers"):
        server.assert_complete()


def test_unknown_or_duplicate_handler_cannot_change_signed_catalog(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches dynamic expansion and last-registration-wins replacement.
    server = FirstPartyMcpServer(*catalogs)
    with pytest.raises(ServerCatalogError, match="not signed"):
        server.register_tool("omega.fake.unknown", _echo)
    server.register_tool("omega.fake.echo", _echo)
    with pytest.raises(ServerCatalogError, match="duplicate"):
        server.register_tool("omega.fake.echo", _echo)
    with pytest.raises(ServerCatalogError, match="not signed"):
        server.register_resource("omega://fake/unknown", _resource)
    server.register_resource("omega://fake/help", _resource)
    with pytest.raises(ServerCatalogError, match="duplicate"):
        server.register_resource("omega://fake/help", _resource)


def test_catalog_identity_mismatch_is_rejected(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches composing handler surfaces from catalogs signed for different servers.
    tools, resources = catalogs
    mismatched = ResourceCatalog.from_dict({**resources.to_dict(), "server_version": "2.0.0"})
    with pytest.raises(ServerCatalogError, match="identity"):
        FirstPartyMcpServer(tools, mismatched)


def test_signed_tool_must_allow_official_text_result_mime(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches signing a tool the official mapping normalizer makes impossible to consume.
    tools, resources = catalogs
    document = tools.to_dict()
    document["tools"][0]["allowed_mime_types"] = ["application/json"]  # type: ignore[index]

    with pytest.raises(ServerCatalogError, match="text/plain"):
        FirstPartyMcpServer(ToolCatalog.from_dict(document), resources)


def test_only_exact_async_handler_signatures_are_accepted(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches implicit thread execution and async functions with incompatible arity.
    server = FirstPartyMcpServer(*catalogs)
    with pytest.raises(ServerCatalogError, match="async"):
        server.register_tool("omega.fake.echo", lambda arguments: arguments)

    async def wrong(first: object, second: object) -> Mapping[str, object]:
        return {"text": f"{first}{second}"}

    with pytest.raises(ServerCatalogError, match="signature"):
        server.register_tool("omega.fake.echo", wrong)  # type: ignore[arg-type]
    with pytest.raises(ServerCatalogError, match="signature"):
        server.register_resource("omega://fake/help", wrong)  # type: ignore[arg-type]


def test_input_and_output_use_draft_2020_12_signed_schemas(
    complete_server: FirstPartyMcpServer,
) -> None:
    # Catches accepting unknown input or returning provider data outside the output contract.
    with pytest.raises(ServerSchemaError, match="input"):
        anyio.run(complete_server.call_tool, "omega.fake.echo", {"extra": True})

    async def invalid_output(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        del arguments
        return {"unexpected": True}

    complete_server.handlers["omega.fake.echo"] = invalid_output
    with pytest.raises(ServerSchemaError, match="output"):
        anyio.run(complete_server.call_tool, "omega.fake.echo", {"text": "hello"})


def test_runtime_rejects_sync_or_oversize_results(
    complete_server: FirstPartyMcpServer,
) -> None:
    # Catches post-registration mutation bypassing async and maximum-result checks.
    complete_server.handlers["omega.fake.echo"] = lambda arguments: {  # type: ignore[assignment]
        "text": arguments["text"]
    }
    with pytest.raises(ServerCatalogError, match="async"):
        anyio.run(complete_server.call_tool, "omega.fake.echo", {"text": "hello"})

    async def oversized(arguments: Mapping[str, object]) -> Mapping[str, object]:
        del arguments
        return {"text": "x" * 300}

    complete_server.handlers["omega.fake.echo"] = oversized
    with pytest.raises(ServerSchemaError, match="maximum"):
        anyio.run(complete_server.call_tool, "omega.fake.echo", {"text": "hello"})


def test_provider_exceptions_are_replaced_without_losing_cancellation(
    complete_server: FirstPartyMcpServer,
) -> None:
    # Catches provider exception text crossing the SDK boundary or cancellation being swallowed.
    sentinel = "Bear" + "er task8-in-process-provider-token-sentinel"

    async def fail_tool(arguments: Mapping[str, object]) -> Mapping[str, object]:
        del arguments
        raise RuntimeError(sentinel)

    async def fail_resource(uri: str) -> bytes:
        del uri
        raise RuntimeError(sentinel)

    complete_server.handlers["omega.fake.echo"] = fail_tool
    with pytest.raises(ServerSchemaError, match="tool handler failed") as tool_error:
        anyio.run(complete_server.call_tool, "omega.fake.echo", {"text": "hello"})
    assert sentinel not in str(tool_error.value)
    complete_server.resource_handlers["omega://fake/help"] = fail_resource
    with pytest.raises(ServerSchemaError, match="resource handler failed") as resource_error:
        anyio.run(complete_server.read_resource, "omega://fake/help")
    assert sentinel not in str(resource_error.value)

    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(sentinel)

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise RuntimeError(sentinel)

        def __len__(self) -> int:
            return 1

    async def hostile_result(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        del arguments
        return HostileMapping()

    complete_server.handlers["omega.fake.echo"] = hostile_result
    with pytest.raises(ServerSchemaError, match="tool handler failed") as mapping_error:
        anyio.run(complete_server.call_tool, "omega.fake.echo", {"text": "hello"})
    assert sentinel not in str(mapping_error.value)

    async def cancellation_exercise() -> None:
        entered = anyio.Event()
        cancellation_types: list[type[BaseException]] = []

        async def wait_tool(
            arguments: Mapping[str, object],
        ) -> Mapping[str, object]:
            del arguments
            entered.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        async def invoke() -> None:
            try:
                await complete_server.call_tool("omega.fake.echo", {"text": "hello"})
            except BaseException as error:
                cancellation_types.append(type(error))
                raise

        complete_server.handlers["omega.fake.echo"] = wait_tool
        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            await entered.wait()
            group.cancel_scope.cancel()
        assert cancellation_types == [anyio.get_cancelled_exc_class()]

    anyio.run(cancellation_exercise)


def test_server_maximum_matches_task5_content_envelope_boundary(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches counting the outer CallToolResult wrapper instead of Task 5's payload envelopes.
    structured = {"text": "hello"}
    content = types.TextContent(type="text", text=json.dumps(structured, indent=2))
    actual_result = types.CallToolResult(
        content=[content], structuredContent=structured, isError=False
    )
    structured_size = len(canonical_bytes(actual_result.structured_content))
    content_size = len(
        canonical_bytes(
            actual_result.content[0].model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    )
    task5_maximum = structured_size + content_size
    tools, resources = catalogs

    def server_with_limit(limit: int) -> FirstPartyMcpServer:
        document = tools.to_dict()
        document["tools"][0]["max_result_bytes"] = limit  # type: ignore[index]
        server = FirstPartyMcpServer(ToolCatalog.from_dict(document), resources)
        server.register_tool("omega.fake.echo", _echo)
        server.register_resource("omega://fake/help", _resource)
        server.register_resource("omega://fake/items/{id}", _resource)
        return server

    assert (
        anyio.run(
            server_with_limit(task5_maximum).call_tool,
            "omega.fake.echo",
            structured,
        )
        == structured
    )
    with pytest.raises(ServerSchemaError, match="maximum"):
        anyio.run(
            server_with_limit(task5_maximum - 1).call_tool,
            "omega.fake.echo",
            structured,
        )


def test_signed_catalog_order_and_schemas_are_emitted_exactly(
    complete_server: FirstPartyMcpServer,
) -> None:
    # Catches sorting or schema/metadata mutation on the official SDK surface.
    tools = anyio.run(complete_server.list_tools)
    resources = anyio.run(complete_server.list_resources)
    templates = anyio.run(complete_server.list_resource_templates)

    assert [tool.name for tool in tools] == ["omega.fake.echo"]
    assert tools[0].input_schema == _schema("string")
    assert tools[0].output_schema == _schema("string")
    assert [str(resource.uri) for resource in resources] == ["omega://fake/help"]
    assert [template.uri_template for template in templates] == ["omega://fake/items/{id}"]
    assert resources[0].mime_type == "text/plain"
    assert templates[0].mime_type == "application/json"


def test_resources_are_catalog_bound_and_size_limited(
    complete_server: FirstPartyMcpServer,
) -> None:
    # Catches undeclared reads, MIME drift, and resource handlers exceeding signed limits.
    content = anyio.run(complete_server.read_resource, "omega://fake/help")
    assert content == [
        types.TextResourceContents(uri="omega://fake/help", mimeType="text/plain", text="help")
    ]
    with pytest.raises(ServerCatalogError, match="not signed"):
        anyio.run(complete_server.read_resource, "omega://fake/missing")

    async def oversized(uri: str) -> bytes:
        del uri
        return b"x" * 17

    complete_server.resource_handlers["omega://fake/help"] = oversized
    with pytest.raises(ServerSchemaError, match="maximum"):
        anyio.run(complete_server.read_resource, "omega://fake/help")


def test_server_advertises_only_static_tools_and_resources(
    complete_server: FirstPartyMcpServer,
) -> None:
    # Catches accidentally advertising prompts, tasks, logging, or list-change expansion.
    capabilities = complete_server.capabilities
    assert capabilities.tools == types.ToolsCapability(listChanged=False)
    assert capabilities.resources == types.ResourcesCapability(subscribe=False, listChanged=False)
    assert capabilities.prompts is None
    assert capabilities.tasks is None
    assert capabilities.logging is None
    assert capabilities.completions is None
    assert capabilities.experimental is None
    assert capabilities.extensions is None


def test_public_handler_types_are_awaitable() -> None:
    # Static contract assertion catches widening the documented provider boundary.
    tool_result: Awaitable[Mapping[str, object]] = _echo({"text": "ok"})
    resource_result: Awaitable[bytes] = _resource("omega://fake/help")
    anyio.run(lambda: tool_result)
    anyio.run(lambda: resource_result)


def test_current_server_surface_has_no_initialization_options(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches routing first-party peers back through handshake-era server startup.
    server = FirstPartyMcpServer(*catalogs)

    assert not hasattr(server, "initialization_options")
    assert callable(getattr(server, "streamable_http_app", None))


def test_current_capabilities_include_exact_prompts_extensions_cache_and_listen(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches discovery drifting from the immutable first-party peer surface.
    prompt = types.Prompt(
        name="omega.fake.summarize",
        title="Summarize",
        arguments=[types.PromptArgument(name="text", required=True)],
    )
    server = FirstPartyMcpServer(
        *catalogs,
        prompts=(prompt,),
        extensions={"io.modelcontextprotocol/tasks": {}},
        cache_hints={"tools/list": CacheHint(ttl_ms=1_250, scope="public")},
        enable_subscriptions=True,
    )

    capabilities = server.capabilities

    assert capabilities.tools == types.ToolsCapability(listChanged=True)
    assert capabilities.prompts == types.PromptsCapability(listChanged=True)
    assert capabilities.resources == types.ResourcesCapability(
        subscribe=True,
        listChanged=True,
    )
    assert capabilities.extensions == {"io.modelcontextprotocol/tasks": {}}
    assert capabilities.tasks is None
    assert server.cache_hints == {"tools/list": CacheHint(ttl_ms=1_250, scope="public")}
    assert anyio.run(server.list_prompts) == [prompt]


def test_input_required_retry_preserves_state_and_closed_responses(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches dropping the current requestState/inputResponses fields at the signed handler seam.
    tools, resources = catalogs
    document = tools.to_dict()
    document["tools"][0]["max_result_bytes"] = 1_024  # type: ignore[index]
    server = FirstPartyMcpServer(ToolCatalog.from_dict(document), resources)
    observed: list[tuple[str | None, tuple[str, ...]]] = []

    async def input_tool(request: Mapping[str, object]) -> object:
        request_state = getattr(request, "request_state")
        input_responses = getattr(request, "input_responses")
        observed.append((request_state, tuple(input_responses)))
        if request_state is None:
            return types.InputRequiredResult(
                requestState="opaque\x00state-雪",
                inputRequests={
                    "confirm": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Confirm echo",
                            requestedSchema={
                                "type": "object",
                                "properties": {"approved": {"type": "boolean"}},
                                "required": ["approved"],
                                "additionalProperties": False,
                            },
                        )
                    )
                },
            )
        assert input_responses == {
            "confirm": types.ElicitResult(
                action="accept",
                content={"approved": True},
            )
        }
        return {"text": request["text"]}

    server.register_tool("omega.fake.echo", input_tool)  # type: ignore[arg-type]
    first = anyio.run(server.call_tool, "omega.fake.echo", {"text": "hello"})
    assert type(first) is types.InputRequiredResult
    assert first.request_state == "opaque\x00state-雪"

    async def retry() -> object:
        return await server.call_tool(
            "omega.fake.echo",
            {"text": "hello"},
            request_state=first.request_state,
            input_responses={
                "confirm": types.ElicitResult(
                    action="accept",
                    content={"approved": True},
                )
            },
        )

    assert anyio.run(retry) == {"text": "hello"}
    assert observed == [
        (None, ()),
        ("opaque\x00state-雪", ("confirm",)),
    ]


def test_prompt_and_extension_handlers_are_closed_at_registration(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches an unsigned prompt or unadvertised extension method entering the runtime surface.
    prompt = types.Prompt(name="omega.fake.summarize", title="Summarize")
    server = FirstPartyMcpServer(
        *catalogs,
        prompts=(prompt,),
        extensions={"io.modelcontextprotocol/tasks": {}},
    )

    async def prompt_handler(request: object) -> types.GetPromptResult:
        del request
        return types.GetPromptResult(messages=[])

    async def extension_handler(params: types.RequestParams) -> types.EmptyResult:
        del params
        return types.EmptyResult()

    with pytest.raises(ServerCatalogError, match="prompt handler is not declared"):
        server.register_prompt("omega.fake.unknown", prompt_handler)
    server.register_prompt("omega.fake.summarize", prompt_handler)
    with pytest.raises(ServerCatalogError, match="duplicate prompt"):
        server.register_prompt("omega.fake.summarize", prompt_handler)
    with pytest.raises(ServerCatalogError, match="extension is not advertised"):
        server.register_extension_method(
            "io.modelcontextprotocol/unknown",
            "unknown/get",
            types.RequestParams,
            extension_handler,
        )
    server.register_extension_method(
        "io.modelcontextprotocol/tasks",
        "tasks/get",
        types.RequestParams,
        extension_handler,
    )
    with pytest.raises(ServerCatalogError, match="duplicate extension method"):
        server.register_extension_method(
            "io.modelcontextprotocol/tasks",
            "tasks/get",
            types.RequestParams,
            extension_handler,
        )


@pytest.mark.parametrize(
    "active_error",
    (
        asyncio.CancelledError("active-cancel"),
        KeyboardInterrupt("active-interrupt"),
        _ActiveServerBaseError("active-base"),
    ),
)
def test_stdio_cleanup_preserves_active_base_exception_identity(
    complete_server: FirstPartyMcpServer,
    monkeypatch: pytest.MonkeyPatch,
    active_error: BaseException,
) -> None:
    # Catches cleanup replacing control-flow exceptions or leaking cleanup text.
    cleanup_error = RuntimeError("Bear" + "er cleanup-provider-token-sentinel")

    @asynccontextmanager
    async def fake_stdio() -> object:
        try:
            yield object(), object()
        finally:
            raise cleanup_error

    class FailingDispatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> None:
            raise active_error

    monkeypatch.setattr(server_module, "stdio_server", fake_stdio)
    monkeypatch.setattr(server_module, "JSONRPCDispatcher", FailingDispatcher)

    with pytest.raises(type(active_error)) as raised:
        anyio.run(complete_server.run_stdio)
    assert raised.value is active_error
    assert "provider-token-sentinel" not in str(raised.value)


def test_stdio_cleanup_is_bounded(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches a stuck transport context leaving shutdown unbounded.
    server = FirstPartyMcpServer(*catalogs, shutdown_timeout_seconds=0.01)
    server.register_tool("omega.fake.echo", _echo)
    server.register_resource("omega://fake/help", _resource)
    server.register_resource("omega://fake/items/{id}", _resource)

    @asynccontextmanager
    async def stuck_stdio() -> object:
        try:
            yield object(), object()
        finally:
            await anyio.sleep_forever()

    class CompleteDispatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> None:
            return

    monkeypatch.setattr(server_module, "stdio_server", stuck_stdio)
    monkeypatch.setattr(server_module, "JSONRPCDispatcher", CompleteDispatcher)

    with pytest.raises(ServerCleanupError, match="timed out"):
        anyio.run(server.run_stdio)


def test_http_gate_requires_one_exact_protocol_header_without_downstream_entry(
    complete_server: FirstPartyMcpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches duplicate/header-folding ambiguity selecting the SDK's legacy route.
    downstream_headers: list[list[tuple[bytes, bytes]]] = []

    async def downstream(scope: object, _receive: object, send: object) -> None:
        downstream_headers.append(list(scope["headers"]))  # type: ignore[index]
        await send({"type": "http.response.start", "status": 204, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    monkeypatch.setattr(
        complete_server._server,  # type: ignore[attr-defined]
        "streamable_http_app",
        lambda **_kwargs: downstream,
    )
    app = complete_server.streamable_http_app()

    async def request(headers: list[tuple[bytes, bytes]]) -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "query_string": b"",
                "server": ("127.0.0.1", 80),
                "client": ("127.0.0.1", 1),
                "headers": headers,
            },  # type: ignore[arg-type]
            receive,
            send,  # type: ignore[arg-type]
        )
        return sent

    invalid_headers = (
        [
            (b"mcp-protocol-version", b"2026-07-27"),
            (b"mcp-protocol-version", b"2026-07-28"),
        ],
        [
            (b"mcp-protocol-version", b"2026-07-28"),
            (b"mcp-protocol-version", b"2026-07-27"),
        ],
        [
            (b"mcp-protocol-version", b"2026-07-28"),
            (b"mcp-protocol-version", b"2026-07-28"),
        ],
        [(b"mcp-protocol-version", b"2026-07-28, 2026-07-27")],
        [
            (b"Mcp-Protocol-Version", b"2026-07-27"),
            (b"mCP-pROTOCOL-vERSION", b"2026-07-28"),
        ],
    )

    for headers in invalid_headers:
        sent = anyio.run(request, headers)
        assert sent[0]["status"] == 400
        assert downstream_headers == []

    valid = anyio.run(
        request,
        [(b"McP-PrOtOcOl-VeRsIoN", b"2026-07-28")],
    )
    assert valid[0]["status"] == 204
    assert len(downstream_headers) == 1


def _tool_server_with_extension(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
    *,
    provider_tags: list[str],
    extensions: dict[str, dict[str, object]],
) -> FirstPartyMcpServer:
    tools, resources = catalogs
    document = tools.to_dict()
    document["tools"][0]["provider_extension_tags"] = provider_tags  # type: ignore[index]
    return FirstPartyMcpServer(
        ToolCatalog.from_dict(document),
        resources,
        extensions=extensions,
    )


def test_extension_tagged_call_tool_result_still_obeys_signed_output_schema(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches an extension tag exempting an ordinary core tool result from its schema.
    server = _tool_server_with_extension(
        catalogs,
        provider_tags=["io.example/alpha"],
        extensions={"io.example/alpha": {}},
    )

    async def invalid_result(_arguments: Mapping[str, object]) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(text="invalid")],
            structuredContent={"unexpected": True},
            isError=False,
        )

    server.register_tool("omega.fake.echo", invalid_result)

    with pytest.raises(ServerSchemaError, match="output schema"):
        anyio.run(server.call_tool, "omega.fake.echo", {"text": "hello"})


def test_claimed_result_requires_exact_registered_extension_binding(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches accepting an arbitrary result merely because some extension tag intersects.
    alpha = "io.example/alpha"
    beta = "io.example/beta"

    async def claimed(_arguments: Mapping[str, object]) -> _ClaimedResult:
        return _ClaimedResult()

    unbound = _tool_server_with_extension(
        catalogs,
        provider_tags=[alpha],
        extensions={alpha: {}, beta: {}},
    )
    unbound.register_tool("omega.fake.echo", claimed)
    with pytest.raises(ServerSchemaError, match="extension result is not registered"):
        anyio.run(unbound.call_tool, "omega.fake.echo", {"text": "hello"})

    wrong = _tool_server_with_extension(
        catalogs,
        provider_tags=[alpha],
        extensions={alpha: {}, beta: {}},
    )
    wrong.register_tool("omega.fake.echo", claimed)
    register_wrong = getattr(wrong, "register_extension_result", None)
    assert callable(register_wrong)
    register_wrong(beta, "claimed", _ClaimedResult)
    with pytest.raises(ServerSchemaError, match="extension result is not signed"):
        anyio.run(wrong.call_tool, "omega.fake.echo", {"text": "hello"})

    bound = _tool_server_with_extension(
        catalogs,
        provider_tags=[alpha],
        extensions={alpha: {}},
    )
    bound.register_tool("omega.fake.echo", claimed)
    register_bound = getattr(bound, "register_extension_result", None)
    assert callable(register_bound)
    register_bound(alpha, "claimed", _ClaimedResult)
    result = anyio.run(bound.call_tool, "omega.fake.echo", {"text": "hello"})
    assert type(result) is _ClaimedResult


def test_input_required_result_obeys_exact_signed_byte_boundary(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
) -> None:
    # Catches interim current results bypassing the signed maximum.
    interim = types.InputRequiredResult(
        requestState="opaque\x00state-雪",
        inputRequests={
            "confirm": types.ElicitRequest(
                params=types.ElicitRequestFormParams(
                    message="Confirm echo",
                    requestedSchema={
                        "type": "object",
                        "properties": {"approved": {"type": "boolean"}},
                        "required": ["approved"],
                        "additionalProperties": False,
                    },
                )
            )
        },
    )
    exact_size = len(
        canonical_bytes(interim.model_dump(mode="json", by_alias=True, exclude_none=True))
    )
    tools, resources = catalogs

    async def input_required(_arguments: Mapping[str, object]) -> types.InputRequiredResult:
        return interim

    def server_with_limit(limit: int) -> FirstPartyMcpServer:
        document = tools.to_dict()
        document["tools"][0]["max_result_bytes"] = limit  # type: ignore[index]
        server = FirstPartyMcpServer(ToolCatalog.from_dict(document), resources)
        server.register_tool("omega.fake.echo", input_required)
        return server

    at_boundary = anyio.run(
        server_with_limit(exact_size).call_tool,
        "omega.fake.echo",
        {"text": "hello"},
    )
    assert at_boundary is interim
    with pytest.raises(ServerSchemaError, match="maximum"):
        anyio.run(
            server_with_limit(exact_size - 1).call_tool,
            "omega.fake.echo",
            {"text": "hello"},
        )


def test_stdio_cleanup_only_failure_is_content_free(
    complete_server: FirstPartyMcpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches a sensitive ordinary context-exit failure reaching process stderr.
    sensitive = "Bear" + "er standalone-cleanup-provider-token-sentinel"

    @asynccontextmanager
    async def failing_stdio() -> object:
        try:
            yield object(), object()
        finally:
            raise RuntimeError(sensitive)

    class CompleteDispatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> None:
            return

    monkeypatch.setattr(server_module, "stdio_server", failing_stdio)
    monkeypatch.setattr(server_module, "JSONRPCDispatcher", CompleteDispatcher)

    with pytest.raises(ServerCleanupError, match="server cleanup failed") as raised:
        anyio.run(complete_server.run_stdio)
    assert sensitive not in str(raised.value)


@pytest.mark.parametrize(
    "active_error",
    (
        asyncio.CancelledError("http-cancel"),
        KeyboardInterrupt("http-interrupt"),
        _ActiveServerBaseError("http-base"),
    ),
)
def test_http_cleanup_preserves_active_base_exception_identity(
    complete_server: FirstPartyMcpServer,
    monkeypatch: pytest.MonkeyPatch,
    active_error: BaseException,
) -> None:
    # Catches HTTP cleanup replacing active control-flow errors during dual failure.
    class DualFailureOwner:
        shutdown_entered = False

        def __init__(self, _config: object) -> None:
            pass

        async def serve(self) -> None:
            raise active_error

        @contextmanager
        def capture_signals(self) -> object:
            yield

        async def startup(self) -> None:
            return

        async def main_loop(self) -> None:
            raise active_error

        async def shutdown(self) -> None:
            self.shutdown_entered = True
            raise RuntimeError("Bear" + "er http-cleanup-provider-token-sentinel")

    owner = DualFailureOwner(object())
    monkeypatch.setattr(server_module.uvicorn, "Server", lambda _config: owner)

    with pytest.raises(type(active_error)) as raised:
        anyio.run(complete_server.run_streamable_http)
    assert raised.value is active_error
    assert owner.shutdown_entered is True


def test_http_cleanup_only_failure_is_content_free(
    complete_server: FirstPartyMcpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches an ordinary sensitive HTTP cleanup error escaping the stable surface.
    sensitive = "Bear" + "er http-cleanup-provider-token-sentinel"

    class CleanupFailureOwner:
        def __init__(self, _config: object) -> None:
            pass

        async def serve(self) -> None:
            return

        @contextmanager
        def capture_signals(self) -> object:
            yield

        async def startup(self) -> None:
            return

        async def main_loop(self) -> None:
            return

        async def shutdown(self) -> None:
            raise RuntimeError(sensitive)

    monkeypatch.setattr(server_module.uvicorn, "Server", CleanupFailureOwner)

    with pytest.raises(ServerCleanupError, match="server cleanup failed") as raised:
        anyio.run(complete_server.run_streamable_http)
    assert sensitive not in str(raised.value)


def test_http_cleanup_is_bounded_and_finishes_owned_finalizers(
    catalogs: tuple[ToolCatalog, ResourceCatalog],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches stuck request/lifespan shutdown ignoring the configured server bound.
    server = FirstPartyMcpServer(*catalogs, shutdown_timeout_seconds=0.01)
    server.register_tool("omega.fake.echo", _echo)
    server.register_resource("omega://fake/help", _resource)
    server.register_resource("omega://fake/items/{id}", _resource)

    class StuckCleanupOwner:
        shutdown_entered = False
        finalizer_finished = False

        def __init__(self, _config: object) -> None:
            pass

        async def serve(self) -> None:
            return

        @contextmanager
        def capture_signals(self) -> object:
            yield

        async def startup(self) -> None:
            return

        async def main_loop(self) -> None:
            return

        async def shutdown(self) -> None:
            self.shutdown_entered = True
            try:
                await anyio.sleep_forever()
            finally:
                self.finalizer_finished = True

    owner = StuckCleanupOwner(object())
    monkeypatch.setattr(server_module.uvicorn, "Server", lambda _config: owner)

    async def exercise() -> None:
        started = anyio.current_time()
        with anyio.fail_after(0.5):
            with pytest.raises(ServerCleanupError, match="timed out"):
                await server.run_streamable_http()
        assert anyio.current_time() - started < 0.2

    anyio.run(exercise)
    assert owner.shutdown_entered is True
    assert owner.finalizer_finished is True
