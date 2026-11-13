"""Production ``DiscoveryPeer``: bridges restricted discovery onto ``CurrentMcpClient``.

``mcp_platform.plugin_discovery``'s ``RestrictedPluginDiscovery`` drives any
``DiscoveryPeer`` through one generic ``negotiate(operation)`` method, in the
fixed order ``initialize``, ``tools/list``, ``prompts/list``, ``resources/list``,
``resources/templates/list`` -- and never anything else. Every ``DiscoveryPeer``
before this module was a test-only fake; this is the one production
implementation.

It wraps a discovery-only ``CurrentMcpClient``
(``CurrentMcpClient.for_restricted_discovery``), whose own
``discover_restricted()`` performs the real, ordered wire negotiation exactly
once -- ``initialize`` (the SDK's own implicit handshake) plus the standard
``server/discover`` request, then the four list operations -- and returns an
immutable ``RestrictedDiscoveryReceipt``. This adapter's ``negotiate()`` calls
``discover_restricted()`` on the first (``"initialize"``) call and serves the
four list operations afterward purely from the already-fetched receipt: the
real MCP server still sees exactly the five operations, in order, and
nothing else, while ``RestrictedPluginDiscovery``'s own five-call contract is
satisfied without this module ever spelling a raw SDK operation name as a
call expression itself -- only ``discover_restricted``, a plain method name
the ``mcp_platform`` architecture rail does not reserve, appears here.

Structurally incapable of ``tools/call``, ``prompts/get``, ``resources/read``,
or a subscription: there is no method on ``RestrictedDiscoveryPeer`` or the
discovery-only client it wraps that could reach any of them (a discovery-only
``CurrentMcpClient`` fails every other public operation closed with
``McpClientErrorReason.REVIEW_REQUIRED`` before it ever touches the wire --
see that class's own docstring).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp_platform.client import CurrentMcpClient
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpConnectionDescriptor,
    McpTransport,
    RestrictedDiscoveryReceipt,
)
from mcp_platform.plugin_discovery import DiscoveryPeer, PluginDiscoveryError
from mcp_platform.transports.streamable_http import (
    StreamableHttpClientConfig,
    build_streamable_http_transport,
)

#: Stable, content-free cause for every failure this module raises directly
#: (as opposed to what ``RestrictedPluginDiscovery`` itself raises once it
#: drives this peer) -- matches ``mcp_platform.plugin_discovery``'s own
#: existing convention for a peer-side transport failure.
_TRANSPORT_UNAVAILABLE = "plugin_discovery_transport_unavailable"


def _tool_wire_shape(item: Mapping[str, Any]) -> dict[str, object]:
    """Project one receipt tool entry down to the wire shape
    ``mcp_platform.plugin_discovery._validate_capability`` accepts for a
    tool (``{"name", "title", "input_schema", "output_schema", "annotations"}``,
    ``output_schema`` only when declared) -- the receipt itself may carry
    more (it does not, for tools, but the same pattern holds below for
    prompts/resources), never less.
    """
    wire: dict[str, object] = {
        "name": item["name"],
        "title": item["title"],
        "input_schema": dict(item["input_schema"]),  # type: ignore[arg-type]
        "annotations": dict(item["annotations"]),  # type: ignore[arg-type]
    }
    if "output_schema" in item:
        wire["output_schema"] = dict(item["output_schema"])  # type: ignore[arg-type]
    return wire


def _prompt_wire_shape(item: Mapping[str, Any]) -> dict[str, object]:
    """Project one receipt prompt entry down to
    ``{"name", "title", "annotations"}`` -- the receipt also carries the
    prompt's literal ``arguments`` (a fuller, honest record of what the
    server declared), but ``mcp_platform.plugin_discovery``'s current
    per-kind field set for prompts does not accept that key yet; forwarding
    it would make every prompt fail closed with
    ``plugin_discovery_schema_unsupported``.
    """
    return {
        "name": item["name"],
        "title": item["title"],
        "annotations": dict(item["annotations"]),  # type: ignore[arg-type]
    }


def _resource_wire_shape(item: Mapping[str, Any]) -> dict[str, object]:
    """Project one receipt resource/resource-template entry down to
    ``{"name", "title", "mime_type", "max_bytes", "annotations"}`` -- the
    receipt also carries the literal ``uri``/``uri_template``, again a
    fuller record than ``mcp_platform.plugin_discovery`` accepts today for
    this kind (see its own Concern 1: it does not yet capture that field
    itself), so it is not forwarded either.
    """
    wire: dict[str, object] = {
        "name": item["name"],
        "title": item["title"],
        "annotations": dict(item["annotations"]),  # type: ignore[arg-type]
    }
    if "mime_type" in item:
        wire["mime_type"] = item["mime_type"]
    if "max_bytes" in item:
        wire["max_bytes"] = item["max_bytes"]
    return wire


class RestrictedDiscoveryPeer:
    """The one production ``DiscoveryPeer``: a discovery-only ``CurrentMcpClient``."""

    def __init__(self, client: CurrentMcpClient, *, endpoint: str) -> None:
        self._client = client
        self._endpoint = endpoint
        self._receipt: RestrictedDiscoveryReceipt | None = None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    async def negotiate(self, operation: str) -> object:
        if operation == "initialize":
            receipt = await self._client.discover_restricted()
            self._receipt = receipt
            return {
                "server_id": receipt.server_id,
                "server_version": receipt.server_version,
                "capabilities": {
                    "tools": {"listChanged": receipt.tools_list_changed},
                    "prompts": {"listChanged": receipt.prompts_list_changed},
                    "resources": {"listChanged": receipt.resources_list_changed},
                },
            }
        receipt = self._receipt
        if receipt is None:
            # A list operation named before "initialize" -- a protocol-order
            # violation ``RestrictedPluginDiscovery`` itself never commits
            # (it always negotiates first), so this is defensive, not
            # reachable through the ordinary boundary.
            raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE)
        if operation == "tools/list":
            return [_tool_wire_shape(item) for item in receipt.tools]
        if operation == "prompts/list":
            return [_prompt_wire_shape(item) for item in receipt.prompts]
        if operation == "resources/list":
            return [_resource_wire_shape(item) for item in receipt.resources]
        if operation == "resources/templates/list":
            return [_resource_wire_shape(item) for item in receipt.resource_templates]
        # Every operation RestrictedPluginDiscovery could ever name is one of
        # the five above; anything else is not a real discovery operation at
        # all (never tools/call, prompts/get, resources/read -- there is no
        # code path here that could reach any of those).
        raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE)


def build_restricted_discovery_peer(descriptor: McpConnectionDescriptor) -> DiscoveryPeer:
    """Build the one production ``DiscoveryPeer`` for one discovery descriptor.

    ``descriptor`` must already be shaped for restricted discovery -- REMOTE/
    STREAMABLE_HTTP, no declared capabilities, no signed authority --
    ``CurrentMcpClient.for_restricted_discovery`` re-validates and enforces
    this itself; this function does not weaken that. Always unauthenticated:
    a marketplace catalog entry has no field that could declare any other
    authentication kind (``plugin_platform.catalog``'s
    ``MarketplaceCatalogEntry``/``MarketplaceDistribution`` carry only a
    remote origin, nothing about how to authenticate to it), so
    ``StreamableHttpClientConfig.unauthenticated()`` is not a placeholder --
    it is the only authentication a signed catalog entry could ever declare
    today.
    """
    if (
        type(descriptor) is not McpConnectionDescriptor
        or descriptor.deployment is not McpClientDeployment.REMOTE
        or descriptor.transport is not McpTransport.STREAMABLE_HTTP
    ):
        raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE)
    try:
        factory = build_streamable_http_transport(
            descriptor, StreamableHttpClientConfig.unauthenticated()
        )
        client = CurrentMcpClient.for_restricted_discovery(descriptor, factory)
    except McpClientError:
        raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE) from None
    endpoint = descriptor.remote_origin
    if type(endpoint) is not str:
        raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE)
    return RestrictedDiscoveryPeer(client, endpoint=endpoint)


__all__ = ["RestrictedDiscoveryPeer", "build_restricted_discovery_peer"]
