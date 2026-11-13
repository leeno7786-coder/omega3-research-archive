from __future__ import annotations

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.plugin_authority import (
    PluginCapabilityBindingIdentity,
    PluginConnectionAuthority,
    PluginConnectionAuthorityKind,
    PluginExecutionPolicy,
    plugin_connection_authority_revision,
    plugin_connection_binding_revision,
)
from mcp_platform.plugin_connections import PluginConnectionService
from mcp_platform.plugin_grants import PluginGrantActivation
from plugin_platform.contracts import (
    AppRequirement,
    AppDeployment,
    McpTransport as PluginMcpTransport,
    CapabilityRequirement,
    CapabilityKind,
    PluginAuthentication,
    PluginAuthenticationKind,
)


def _digest(label: str) -> str:
    return sha256_hex(canonical_bytes({"label": label}))


def _resource_identity() -> PluginCapabilityBindingIdentity:
    return PluginCapabilityBindingIdentity(
        capability_kind="resource",
        binding_sha256=_digest("resource-binding"),
    )


def _resource_descriptor() -> McpConnectionDescriptor:
    policy = {
        "uri": "records://current",
        "mime_type": "application/json",
        "max_bytes": 4096,
    }
    return McpConnectionDescriptor(
        app_id="suite.records.mcp",
        publisher_id="suite.publisher",
        deployment=McpClientDeployment.REMOTE,
        provider_id="suite.provider",
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin="https://records.invalid",
        authorization_issuer="https://issuer.invalid",
        authentication=PluginAuthentication(
            kind=PluginAuthenticationKind.OAUTH_AUTHORIZATION_CODE_PKCE,
            scopes=("records.read",),
            secret_fields=(),
        ),
        declared_capabilities={"resource:records.current": None},
        extension_versions={},
        capability_revision=_digest("capabilities"),
        limits=McpClientLimits(),
        signed_capability_authority={
            "tools": {},
            "resources": {"records.current": policy},
            "resource_templates": {},
            "prompts": {},
        },
    )


def test_resource_only_activation_uses_one_explicit_generic_binding_schema() -> None:
    identity = _resource_identity()
    activation = PluginGrantActivation(
        plugin_id="suite.records",
        app_id="suite.records.mcp",
        account_key_sha256=_digest("account"),
        profile_id="suite.profile",
        profile_revision="sha256:" + _digest("profile"),
        token_partition_key=_digest("partition"),
        connection_revision=_digest("connection"),
        capability_revision=_digest("capabilities"),
        scopes=("records.read",),
        capability_ceiling=(),
        allowed_tool_schemas={},
        source_constraints={},
        destination_constraints={},
        capability_binding_identities=(identity,),
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
    )

    document = activation.to_dict()

    assert document["capability_binding_identities"] == [identity.to_dict()]
    assert document["capability_binding_revision"] == activation.capability_binding_revision
    assert "tool_binding_digests" not in document
    assert "tool_binding_revision" not in document
    assert PluginGrantActivation.from_dict(document) == activation


def test_connection_binding_revision_accepts_a_resource_only_authority() -> None:
    identity = _resource_identity()
    authority_revision = plugin_connection_authority_revision(
        plugin_id="suite.records",
        app_id="suite.records.mcp",
        account_key_hash=_digest("account"),
        connection_revision=_digest("connection"),
        provider_scopes=("records.read",),
        capability_ceiling=(),
        allowed_tool_schemas={},
        source_constraints={},
        destination_constraints={},
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
        authority_kind=PluginConnectionAuthorityKind.OAUTH,
    )
    authority = PluginConnectionAuthority(
        plugin_id="suite.records",
        app_id="suite.records.mcp",
        account_key_hash=_digest("account"),
        connection_revision=_digest("connection"),
        provider_scopes=("records.read",),
        capability_ceiling=(),
        allowed_tool_schemas={},
        source_constraints={},
        destination_constraints={},
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
        authority_kind=PluginConnectionAuthorityKind.OAUTH,
        revision=authority_revision,
    )
    binding_revision = sha256_hex(
        canonical_bytes(
            {"capability_binding_identities": [identity.to_dict()]}
        )
    )

    assert plugin_connection_binding_revision(
        authority,
        capability_revision=_digest("capabilities"),
        capability_binding_revision=binding_revision,
        capability_binding_identities=(identity,),
    ) == sha256_hex(
        canonical_bytes(
            {
                "authority_revision": authority.revision,
                "capability_revision": _digest("capabilities"),
                "capability_binding_revision": binding_revision,
            }
        )
    )


def test_resource_only_descriptor_compiles_without_a_synthetic_tool() -> None:
    ceiling, schemas, identities = PluginConnectionService._compile_capability_bindings(
        _resource_descriptor(),
        (),
    )

    assert ceiling == ()
    assert schemas == {}
    assert [item.capability_kind for item in identities] == ["resource"]


def test_resource_only_candidate_settings_do_not_require_a_synthetic_tool() -> None:
    requirement = AppRequirement(
        app_id="suite.records.mcp",
        publisher_id="suite.publisher",
        deployment=AppDeployment.REMOTE,
        provider_id="suite.provider",
        protocol_version=MCP_PROTOCOL_VERSION,
        transports=(PluginMcpTransport.STREAMABLE_HTTP,),
        remote_origin="https://records.invalid",
        authorization_issuer="https://issuer.invalid",
        authentication=PluginAuthentication(
            kind=PluginAuthenticationKind.OAUTH_AUTHORIZATION_CODE_PKCE,
            scopes=("records.read",),
            secret_fields=(),
        ),
        capabilities=(
            CapabilityRequirement(
                kind=CapabilityKind.RESOURCE,
                name="records.current",
                schema_sha256=None,
                version=None,
            ),
        ),
    )

    settings = PluginConnectionService._candidate_settings(
        requirement,
        _resource_descriptor(),
        scopes=("records.read",),
        capability_ceiling=(),
        allowed_tool_schemas={},
        execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
        source_constraints={},
        destination_constraints={},
    )

    assert settings.capability_ceiling == ()
    assert settings.allowed_tool_schemas == {}
