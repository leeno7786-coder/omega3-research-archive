"""Restricted discovery for publisher-verified and personal connections.

A publisher-verified or personal plugin begins in restricted discovery mode:
Omega may negotiate the protocol and enumerate capabilities, but it may never
call a discovered tool, fetch a resource, or read a prompt before the user
reviews and approves a connection scope. Every test here proves that
boundary -- the exact operation order, every fail-closed discovery cause, and
that approval only ever seals what the user actually scoped.
"""

from __future__ import annotations

import pytest

from mcp_platform.plugin_authority import RiskClass
from mcp_platform.plugin_discovery import (
    CapabilityDiscoveryBinding,
    CapabilityDiscoveryReview,
    LocalCapabilityEnvelope,
    PluginDiscoveryError,
    RestrictedPluginDiscovery,
)
from plugin_platform.local_trust import LocalTrustRoot, SealedCapabilityAuthority

_VAULT_KEY = b"k" * 32


def binding(**overrides: object) -> CapabilityDiscoveryBinding:
    fields: dict[str, object] = {
        "plugin_id": "io.github.example.weather",
        "app_id": "io.github.example.weather",
        "publisher_id": "io.github.example",
        "account_key_hash": "a" * 64,
        "endpoint": "https://mcp.weather.example.com/mcp/v1",
        "protocol_version": "2026-07-28",
    }
    fields.update(overrides)
    return CapabilityDiscoveryBinding(**fields)


def unreviewed_tool(name: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "name": name,
        "title": name.title(),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    fields.update(overrides)
    return fields


def unreviewed_resource(name: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {"name": name, "title": name.title()}
    fields.update(overrides)
    return fields


class _FakePeer:
    """A minimal restricted-discovery peer: enumerates, and refuses to execute."""

    def __init__(
        self,
        *,
        tools: list[dict[str, object]] | None = None,
        prompts: list[dict[str, object]] | None = None,
        resources: list[dict[str, object]] | None = None,
        resource_templates: list[dict[str, object]] | None = None,
        endpoint: str = "https://mcp.weather.example.com/mcp/v1",
        server_id: str = "io.github.example.weather",
        server_version: str = "1.2.0",
        list_changed: bool = False,
        identity_error: BaseException | None = None,
        transport_error: BaseException | None = None,
    ) -> None:
        self.operations: list[str] = []
        self._pages: dict[str, list[dict[str, object]]] = {
            "tools/list": list(tools or ()),
            "prompts/list": list(prompts or ()),
            "resources/list": list(resources or ()),
            "resources/templates/list": list(resource_templates or ()),
        }
        self.endpoint = endpoint
        self._server_id = server_id
        self._server_version = server_version
        self._list_changed = list_changed
        self._identity_error = identity_error
        self._transport_error = transport_error

    async def negotiate(self, operation: str) -> object:
        self.operations.append(operation)
        if operation == "initialize":
            if self._identity_error is not None:
                raise self._identity_error
            return {
                "server_id": self._server_id,
                "server_version": self._server_version,
                "capabilities": {
                    "tools": {"listChanged": self._list_changed},
                    "prompts": {"listChanged": self._list_changed},
                    "resources": {"listChanged": self._list_changed},
                },
            }
        if self._transport_error is not None:
            raise self._transport_error
        return self._pages[operation]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        raise PluginDiscoveryError("plugin_review_required")

    async def get_prompt(self, name: str, arguments: dict[str, object]) -> object:
        raise PluginDiscoveryError("plugin_review_required")

    async def read_resource(self, uri: str) -> object:
        raise PluginDiscoveryError("plugin_review_required")


def discovery_peer(**kwargs: object) -> _FakePeer:
    return _FakePeer(**kwargs)


@pytest.mark.anyio
async def test_discovery_cannot_call_tools_before_user_approval() -> None:
    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    review = await RestrictedPluginDiscovery(peer).discover(binding())
    assert type(review) is CapabilityDiscoveryReview
    assert peer.operations == [
        "initialize",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    ]
    with pytest.raises(PluginDiscoveryError, match="plugin_review_required"):
        await peer.call_tool("forecast", {})
    assert review.tools[0].risk_class == "external_unreviewed"


@pytest.mark.anyio
async def test_discovered_tool_is_external_unreviewed_and_annotations_never_upgrade_it() -> None:
    """Catches a publisher annotation (e.g. readOnlyHint) upgrading the risk class."""
    peer = discovery_peer(
        tools=[
            unreviewed_tool(
                "forecast",
                annotations={"readOnlyHint": True, "destructiveHint": False},
            )
        ]
    )
    review = await RestrictedPluginDiscovery(peer).discover(binding())
    tool = review.tools[0]
    assert tool.risk_class == RiskClass.EXTERNAL_UNREVIEWED.value
    assert dict(tool.publisher_annotations) == {"readOnlyHint": True, "destructiveHint": False}


@pytest.mark.anyio
async def test_schema_limits_fail_closed() -> None:
    """Catches an oversized/too-deep publisher schema being accepted as-is."""
    nested: dict[str, object] = {"type": "object"}
    cursor = nested
    for _ in range(40):
        child: dict[str, object] = {"type": "object", "properties": {}}
        cursor["properties"] = {"next": child}
        cursor = child
    peer = discovery_peer(tools=[unreviewed_tool("forecast", input_schema=nested)])
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_schema_limit_exceeded"):
        await RestrictedPluginDiscovery(peer).discover(binding())


@pytest.mark.anyio
async def test_duplicate_identities_fail_closed() -> None:
    peer = discovery_peer(tools=[unreviewed_tool("forecast"), unreviewed_tool("forecast")])
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_duplicate_identity"):
        await RestrictedPluginDiscovery(peer).discover(binding())


@pytest.mark.anyio
async def test_mutable_discovery_pages_fail_closed() -> None:
    """Catches a server that reserves the right to change its own capability list."""
    peer = discovery_peer(tools=[unreviewed_tool("forecast")], list_changed=True)
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_mutable_pages"):
        await RestrictedPluginDiscovery(peer).discover(binding())


@pytest.mark.anyio
async def test_unsupported_json_schema_fails_closed() -> None:
    peer = discovery_peer(
        tools=[unreviewed_tool("forecast", input_schema={"type": "object", "$ref": "#/definitions/x"})]
    )
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_schema_unsupported"):
        await RestrictedPluginDiscovery(peer).discover(binding())


@pytest.mark.anyio
async def test_malformed_json_schema_fails_closed() -> None:
    peer = discovery_peer(
        tools=[unreviewed_tool("forecast", input_schema={"type": "not-a-real-type"})]
    )
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_schema_unsupported"):
        await RestrictedPluginDiscovery(peer).discover(binding())


@pytest.mark.anyio
async def test_mime_limits_fail_closed() -> None:
    peer = discovery_peer(resources=[unreviewed_resource("brief", mime_type="not a mime")])
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_mime_unsupported"):
        await RestrictedPluginDiscovery(peer).discover(binding())


@pytest.mark.anyio
async def test_byte_limits_fail_closed() -> None:
    peer = discovery_peer(resources=[unreviewed_resource("brief", max_bytes=-1)])
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_result_limit_invalid"):
        await RestrictedPluginDiscovery(peer).discover(binding())


@pytest.mark.anyio
async def test_changed_server_identity_fails_closed() -> None:
    """Catches a reconnect silently accepting a different server than was reviewed."""
    peer = discovery_peer(tools=[unreviewed_tool("forecast")], server_id="io.github.example.weather")
    rebind = binding(expected_server_id="io.github.example.impostor")
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_identity_changed"):
        await RestrictedPluginDiscovery(peer).discover(rebind)


@pytest.mark.anyio
async def test_changed_server_version_fails_closed() -> None:
    peer = discovery_peer(tools=[unreviewed_tool("forecast")], server_version="1.2.0")
    rebind = binding(expected_server_version="9.9.9")
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_identity_changed"):
        await RestrictedPluginDiscovery(peer).discover(rebind)


@pytest.mark.anyio
async def test_matching_expected_identity_is_accepted() -> None:
    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    rebind = binding(
        expected_server_id="io.github.example.weather",
        expected_server_version="1.2.0",
    )
    review = await RestrictedPluginDiscovery(peer).discover(rebind)
    assert review.server_id == "io.github.example.weather"


@pytest.mark.anyio
async def test_changed_endpoint_fails_closed_on_discover() -> None:
    """Catches a reconnect silently discovering against a different endpoint than reviewed."""
    peer = discovery_peer(
        tools=[unreviewed_tool("forecast")],
        endpoint="https://mcp.impostor.example.com/mcp/v1",
    )
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_endpoint_changed"):
        await RestrictedPluginDiscovery(peer).discover(binding())


@pytest.mark.anyio
async def test_changed_endpoint_never_issues_any_operation() -> None:
    """Catches the endpoint check happening after a wasted round trip."""
    peer = discovery_peer(
        tools=[unreviewed_tool("forecast")],
        endpoint="https://mcp.impostor.example.com/mcp/v1",
    )
    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_endpoint_changed"):
        await RestrictedPluginDiscovery(peer).discover(binding())
    assert peer.operations == []


def test_binding_endpoint_must_be_a_canonical_https_url() -> None:
    for bad_endpoint in (
        "http://mcp.weather.example.com/mcp/v1",
        "https://mcp.weather.example.com:443/mcp/v1",
        "https://user@mcp.weather.example.com/mcp/v1",
        "https://mcp.weather.example.com/mcp/v1?query=1",
        "https://mcp.weather.example.com/mcp/v1#frag",
        "https://mcp.weather.example.com/../escape",
    ):
        with pytest.raises(PluginDiscoveryError, match="plugin_review_required"):
            binding(endpoint=bad_endpoint)


@pytest.mark.anyio
async def test_changed_endpoint_fails_closed_on_approve() -> None:
    """Catches a review taken against one endpoint being approved for another.

    ``approve`` re-checks the same endpoint binding ``discover`` already
    proved, so a review captured against one live peer can never be sealed
    through a different discovery instance bound to a different endpoint.
    """
    reviewed_peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    trust_root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    review = await RestrictedPluginDiscovery(reviewed_peer).discover(binding())

    rebound_peer = discovery_peer(
        tools=[unreviewed_tool("forecast")],
        endpoint="https://mcp.impostor.example.com/mcp/v1",
    )
    rebound_discovery = RestrictedPluginDiscovery(rebound_peer, trust_root=trust_root)

    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_endpoint_changed"):
        rebound_discovery.approve(review, ["tool:forecast"])


@pytest.mark.anyio
async def test_transport_failure_is_stable_and_content_free() -> None:
    peer = discovery_peer(transport_error=RuntimeError("dns lookup failed: 10.0.0.7 leaked"))
    with pytest.raises(PluginDiscoveryError) as exc_info:
        await RestrictedPluginDiscovery(peer).discover(binding())
    assert "10.0.0.7" not in str(exc_info.value)


@pytest.mark.anyio
async def test_approve_seals_only_the_exact_connection_scope() -> None:
    peer = discovery_peer(tools=[unreviewed_tool("forecast"), unreviewed_tool("radar")])
    trust_root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    discovery = RestrictedPluginDiscovery(peer, trust_root=trust_root)
    review = await discovery.discover(binding())

    envelope = discovery.approve(review, ["tool:forecast"])

    assert type(envelope) is LocalCapabilityEnvelope
    assert set(envelope.capability_authority["tools"]) == {"forecast"}
    assert envelope.scopes == ("tool:forecast",)
    trust_root.verify_authority(dict(envelope.capability_authority), _sealed(envelope))


@pytest.mark.anyio
async def test_approve_rejects_a_capability_outside_the_review() -> None:
    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    trust_root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    discovery = RestrictedPluginDiscovery(peer, trust_root=trust_root)
    review = await discovery.discover(binding())

    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_scope_unavailable"):
        discovery.approve(review, ["tool:does-not-exist"])


@pytest.mark.anyio
async def test_approve_without_a_trust_root_requires_review() -> None:
    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    discovery = RestrictedPluginDiscovery(peer)
    review = await discovery.discover(binding())

    with pytest.raises(PluginDiscoveryError, match="plugin_review_required"):
        discovery.approve(review, ["tool:forecast"])


@pytest.mark.anyio
async def test_approve_rejects_a_review_from_a_different_discovery_flow() -> None:
    trust_root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    discovery = RestrictedPluginDiscovery(discovery_peer(), trust_root=trust_root)
    with pytest.raises(PluginDiscoveryError, match="plugin_review_required"):
        discovery.approve(object(), ["tool:forecast"])


# --- Task 6c: literal schemas and the closed-schema compatibility gate -----


@pytest.mark.anyio
async def test_discovered_tool_carries_its_literal_input_and_output_schema() -> None:
    """The sealed envelope carries the exact literal schema, not only a digest."""
    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    review = await RestrictedPluginDiscovery(peer).discover(binding())

    tool = review.tools[0]

    assert dict(tool.input_schema) == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    # No output schema was declared -- the blessed open-object MCP result
    # shape (mcp_platform.plugin_planning's one closed-schema exemption)
    # fills in by default, never a wider or unbounded schema of its own.
    assert dict(tool.output_schema) == {
        "type": "object",
        "additionalProperties": True,
        "properties": {},
    }
    assert tool.compatible is True


@pytest.mark.anyio
async def test_a_schema_the_planner_cannot_close_is_incompatible_and_unsealed() -> None:
    """An open (additionalProperties: true) input schema can never become a
    closed planner candidate -- the capability is discovered and reviewable,
    but never approvable, rather than sealed with a looser schema."""
    peer = discovery_peer(
        tools=[
            unreviewed_tool(
                "forecast",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
            )
        ]
    )
    trust_root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    discovery = RestrictedPluginDiscovery(peer, trust_root=trust_root)
    review = await discovery.discover(binding())

    assert review.tools[0].compatible is False

    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_scope_unavailable"):
        discovery.approve(review, ["tool:forecast"])


@pytest.mark.anyio
async def test_a_schema_over_the_planner_byte_bound_is_incompatible_and_unsealed() -> None:
    """A schema under this module's own 16KB discovery bound can still exceed
    the planner's tighter 8KB closed-schema bound -- still incompatible."""
    padded_properties = {
        f"field_{index:03d}": {"type": "string", "maxLength": 32}
        for index in range(220)
    }
    oversized_schema = {
        "type": "object",
        "properties": padded_properties,
        "additionalProperties": False,
        "required": [],
    }
    peer = discovery_peer(
        tools=[unreviewed_tool("forecast", input_schema=oversized_schema)]
    )
    trust_root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    discovery = RestrictedPluginDiscovery(peer, trust_root=trust_root)
    review = await discovery.discover(binding())

    assert review.tools[0].compatible is False

    with pytest.raises(PluginDiscoveryError, match="plugin_discovery_scope_unavailable"):
        discovery.approve(review, ["tool:forecast"])


@pytest.mark.anyio
async def test_incompatible_capability_never_widens_the_auto_computed_scope() -> None:
    """mcp_platform.plugin_lifecycle._discovered_capability_scope (the auto-
    scope a fresh publisher-verified install approves) never names an
    incompatible tool -- install proceeds with the compatible remainder
    instead of failing the whole install on one bad capability."""
    from mcp_platform.plugin_lifecycle import _discovered_capability_scope

    peer = discovery_peer(
        tools=[
            unreviewed_tool("forecast"),
            unreviewed_tool(
                "radar",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
            ),
        ]
    )
    review = await RestrictedPluginDiscovery(peer).discover(binding())

    assert _discovered_capability_scope(review) == ("tool:forecast",)


def _sealed(envelope: LocalCapabilityEnvelope) -> SealedCapabilityAuthority:
    return SealedCapabilityAuthority(
        document_sha256=envelope.capability_schema_sha256,
        mac=envelope.mac,
        mac_key_sha256=envelope.mac_key_sha256,
    )
