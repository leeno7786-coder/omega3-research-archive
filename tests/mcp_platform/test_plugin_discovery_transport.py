"""The production ``DiscoveryPeer``: ``RestrictedDiscoveryPeer``/``build_restricted_discovery_peer``.

Every other ``DiscoveryPeer`` this branch ever built was a test-only fake
(``tests/mcp_platform/test_plugin_discovery.py``'s ``_FakePeer``). These tests
prove the one production implementation -- a discovery-only ``CurrentMcpClient``
wrapped to satisfy ``mcp_platform.plugin_discovery.DiscoveryPeer`` -- against a
real MCP server, driven through ``RestrictedPluginDiscovery`` itself (not just
``CurrentMcpClient.discover_restricted`` directly, which
``tests/mcp_platform/test_client_discovery.py`` already covers exhaustively:
exact operation order/bounds/timeout/redirect refusal at the client layer).
"""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import types
from mcp.server import Server

from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.plugin_discovery import (
    CapabilityDiscoveryBinding,
    PluginDiscoveryError,
    RestrictedPluginDiscovery,
)
import mcp_platform.plugin_discovery_transport as transport_module
from mcp_platform.plugin_discovery_transport import (
    RestrictedDiscoveryPeer,
    build_restricted_discovery_peer,
)
from mcp_platform.transports.streamable_http import StreamableHttpClientConfig
from plugin_platform.contracts import PluginAuthentication

_ORIGIN = "https://mcp.vendor.example.com"


def _descriptor(*, limits: McpClientLimits | None = None) -> McpConnectionDescriptor:
    return McpConnectionDescriptor(
        app_id="vendor.weather",
        publisher_id="vendor",
        deployment=McpClientDeployment.REMOTE,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin=_ORIGIN,
        authorization_issuer=_ORIGIN,
        authentication=PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        declared_capabilities={},
        extension_versions={},
        capability_revision="0" * 64,
        limits=limits or McpClientLimits(),
    )


def _binding(**overrides: object) -> CapabilityDiscoveryBinding:
    fields: dict[str, object] = {
        "plugin_id": "vendor.weather",
        "app_id": "vendor.weather",
        "publisher_id": "vendor",
        "account_key_hash": "a" * 64,
        "endpoint": _ORIGIN,
        "protocol_version": MCP_PROTOCOL_VERSION,
    }
    fields.update(overrides)
    return CapabilityDiscoveryBinding(**fields)


class _Server:
    """A real loopback MCP server (real socket, background thread) recording
    every JSON-RPC method it receives -- forwarded to via a ``MockTransport``
    so the descriptor never needs a resolvable/TLS-real ``remote_origin``."""

    def __init__(self, *, list_delay_seconds: float | None = None, many_tools: bool = False) -> None:
        self.calls: list[str] = []
        self._list_delay_seconds = list_delay_seconds
        self._many_tools = many_tools
        server = Server(
            "vendor.weather",
            version="1.0.0",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_prompts=self._list_prompts,
            on_get_prompt=self._get_prompt,
            on_list_resources=self._list_resources,
            on_list_resource_templates=self._list_templates,
            on_read_resource=self._read_resource,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self._port = int(probe.getsockname()[1])
        self._peer = uvicorn.Server(
            uvicorn.Config(
                server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1"),
                host="127.0.0.1",
                port=self._port,
                access_log=False,
                log_level="critical",
            )
        )
        self._loopback_url = f"http://127.0.0.1:{self._port}/mcp"
        self._thread = threading.Thread(target=lambda: asyncio.run(self._peer.serve()), daemon=True)

    async def _list_tools(self, _ctx: Any, _params: Any) -> types.ListToolsResult:
        self.calls.append("tools/list")
        if self._list_delay_seconds is not None:
            await asyncio.sleep(self._list_delay_seconds)
        tools = [
            types.Tool(
                name="forecast",
                title="Forecast",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            )
        ]
        if self._many_tools:
            tools.append(
                types.Tool(
                    name="alerts",
                    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                )
            )
        return types.ListToolsResult(tools=tools)

    async def _list_prompts(self, _ctx: Any, _params: Any) -> types.ListPromptsResult:
        self.calls.append("prompts/list")
        return types.ListPromptsResult(
            prompts=[types.Prompt(name="summarize", arguments=[types.PromptArgument(name="topic", required=True)])]
        )

    async def _list_resources(self, _ctx: Any, _params: Any) -> types.ListResourcesResult:
        self.calls.append("resources/list")
        return types.ListResourcesResult(
            resources=[types.Resource(name="almanac", uri="docs://weather/almanac", mimeType="text/plain")]
        )

    async def _list_templates(self, _ctx: Any, _params: Any) -> types.ListResourceTemplatesResult:
        self.calls.append("resources/templates/list")
        return types.ListResourceTemplatesResult(resourceTemplates=[])

    async def _call_tool(self, _ctx: Any, _params: Any) -> types.CallToolResult:
        self.calls.append("tools/call")
        raise AssertionError("a discovery-only client must never call a tool")

    async def _get_prompt(self, _ctx: Any, _params: Any) -> types.GetPromptResult:
        self.calls.append("prompts/get")
        raise AssertionError("a discovery-only client must never fetch a prompt")

    async def _read_resource(self, _ctx: Any, _params: Any) -> types.ReadResourceResult:
        self.calls.append("resources/read")
        raise AssertionError("a discovery-only client must never read a resource")

    def __enter__(self) -> "_Server":
        self._thread.start()
        while not self._peer.started:
            pass
        return self

    def __exit__(self, *_exc: object) -> None:
        self._peer.should_exit = True
        self._thread.join(timeout=5)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route ``StreamableHttpClientConfig.unauthenticated()`` (what
        ``build_restricted_discovery_peer`` always calls) to this real
        socket for the duration of one test."""

        async def route(request: httpx2.Request) -> httpx2.Response:
            async with httpx2.AsyncClient() as forward:
                response = await forward.post(
                    self._loopback_url,
                    content=request.content,
                    headers={
                        name: value
                        for name, value in request.headers.items()
                        if name.casefold()
                        in {"accept", "content-type", "mcp-protocol-version", "mcp-method", "mcp-name"}
                    },
                )
            return httpx2.Response(
                response.status_code,
                headers={"content-type": response.headers["content-type"]},
                content=response.content,
            )

        loopback_config = StreamableHttpClientConfig._for_loopback_test(httpx2.MockTransport(route))
        monkeypatch.setattr(
            transport_module.StreamableHttpClientConfig,
            "unauthenticated",
            classmethod(lambda cls: loopback_config),
        )


def test_adapter_issues_exactly_the_four_lists_through_restricted_plugin_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _Server() as fake:
        fake.install(monkeypatch)
        peer = build_restricted_discovery_peer(_descriptor())
        assert type(peer) is RestrictedDiscoveryPeer
        assert peer.endpoint == _ORIGIN

        review = asyncio.run(RestrictedPluginDiscovery(peer).discover(_binding()))

    assert fake.calls == ["tools/list", "prompts/list", "resources/list", "resources/templates/list"]
    assert review.server_id == "vendor.weather"
    assert review.server_version == "1.0.0"
    assert len(review.tools) == 1
    assert review.tools[0].name == "forecast"
    assert review.tools[0].risk_class == "external_unreviewed"
    assert len(review.prompts) == 1
    assert review.prompts[0].name == "summarize"
    assert len(review.resources) == 1
    assert review.resources[0].name == "almanac"


def test_build_restricted_discovery_peer_rejects_a_non_remote_descriptor() -> None:
    local = McpConnectionDescriptor(
        app_id="vendor.weather",
        publisher_id="vendor",
        deployment=McpClientDeployment.LOCAL,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STDIO,
        verified_launch_sha256="a" * 64,
        remote_origin=None,
        authorization_issuer=None,
        authentication=PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        declared_capabilities={},
        extension_versions={},
        capability_revision="0" * 64,
        limits=McpClientLimits(),
    )
    with pytest.raises(PluginDiscoveryError, match="^plugin_discovery_transport_unavailable$"):
        build_restricted_discovery_peer(local)


def test_adapter_surfaces_a_timeout_as_a_stable_plugin_discovery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = McpClientLimits(max_connection_duration_ms=100)
    with _Server(list_delay_seconds=5.0) as fake:
        fake.install(monkeypatch)
        peer = build_restricted_discovery_peer(_descriptor(limits=limits))

        with pytest.raises(PluginDiscoveryError, match="^plugin_discovery_transport_unavailable$"):
            asyncio.run(RestrictedPluginDiscovery(peer).discover(_binding()))


def test_adapter_surfaces_an_oversized_listing_as_a_stable_plugin_discovery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = McpClientLimits(max_list_items=1)
    with _Server(many_tools=True) as fake:
        fake.install(monkeypatch)
        peer = build_restricted_discovery_peer(_descriptor(limits=limits))

        with pytest.raises(PluginDiscoveryError, match="^plugin_discovery_transport_unavailable$"):
            asyncio.run(RestrictedPluginDiscovery(peer).discover(_binding()))


def test_adapter_surfaces_a_redirect_as_a_stable_plugin_discovery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _Server() as fake:
        sent = {"done": False}

        async def redirecting_route(request: httpx2.Request) -> httpx2.Response:
            if not sent["done"]:
                sent["done"] = True
                return httpx2.Response(307, headers={"location": "https://evil.example.com/mcp"})
            async with httpx2.AsyncClient() as forward:
                response = await forward.post(
                    f"http://127.0.0.1:{fake._port}/mcp",
                    content=request.content,
                    headers={
                        name: value
                        for name, value in request.headers.items()
                        if name.casefold()
                        in {"accept", "content-type", "mcp-protocol-version", "mcp-method", "mcp-name"}
                    },
                )
            return httpx2.Response(
                response.status_code,
                headers={"content-type": response.headers["content-type"]},
                content=response.content,
            )

        loopback_config = StreamableHttpClientConfig._for_loopback_test(
            httpx2.MockTransport(redirecting_route)
        )
        monkeypatch.setattr(
            transport_module.StreamableHttpClientConfig,
            "unauthenticated",
            classmethod(lambda cls: loopback_config),
        )
        peer = build_restricted_discovery_peer(_descriptor())

        with pytest.raises(PluginDiscoveryError, match="^plugin_discovery_transport_unavailable$"):
            asyncio.run(RestrictedPluginDiscovery(peer).discover(_binding()))

    assert fake.calls == []
