from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpDiscoveryReceipt,
    McpInputRequest,
    McpOperationKind,
    McpOperationRequest,
    McpOperationResult,
    McpOperationStatus,
    McpTaskHandle,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from plugin_platform.contracts import PluginAuthentication


REVISION = "a" * 64
OTHER_REVISION = "b" * 64
SCHEMA_DIGEST = "c" * 64


def _signed_authority() -> dict[str, object]:
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }
    output_schema = {
        "type": "object",
        "properties": {"items": {"type": "array"}},
    }
    return {
        "tools": {
            "search": {
                "title": "Search notes",
                "input_schema": input_schema,
                "input_schema_sha256": sha256_hex(canonical_bytes(input_schema)),
                "output_schema": output_schema,
                "output_schema_sha256": sha256_hex(canonical_bytes(output_schema)),
                "risk_class": "read_only",
                "effect": "read",
                "requires_revision": False,
                "max_result_bytes": 4096,
                "allowed_mime_types": ["application/json"],
            }
        },
        "resources": {
            "notes": {
                "uri": "omega://notes/all",
                "mime_type": "application/json",
                "max_bytes": 2048,
            }
        },
        "resource_templates": {
            "note": {
                "uri_template": "omega://notes/{id}",
                "mime_type": "application/json",
                "max_bytes": 1024,
            }
        },
        "mcp_apps_metadata": {
            "tools": {
                "search": {
                    "resourceUri": "ui://notes/search",
                    "title": "Search notes",
                }
            },
            "resources": {},
            "resource_templates": {},
        },
    }


def test_final_review_task_retention_limits_are_positive_and_immutable() -> None:
    limits = McpClientLimits(max_active_tasks=3, max_task_tombstones=5)

    assert limits.max_active_tasks == 3
    assert limits.max_task_tombstones == 5
    assert limits.to_dict()["max_active_tasks"] == 3
    with pytest.raises(FrozenInstanceError):
        limits.max_active_tasks = 4  # type: ignore[misc]
    with pytest.raises(ValueError, match="max_active_tasks"):
        McpClientLimits(max_active_tasks=0)
    with pytest.raises(ValueError, match="max_task_tombstones"):
        McpClientLimits(max_task_tombstones=True)  # type: ignore[arg-type]


class _EqualityCompatibleProtocol:
    def __eq__(self, other: object) -> bool:
        return other == MCP_PROTOCOL_VERSION

    def __ne__(self, other: object) -> bool:
        return not self == other


def _local_descriptor(**changes: object) -> McpConnectionDescriptor:
    values: dict[str, object] = {
        "app_id": "com.example.notes",
        "publisher_id": "example.publisher",
        "deployment": McpClientDeployment.LOCAL,
        "provider_id": None,
        "protocol_version": MCP_PROTOCOL_VERSION,
        "transport": McpTransport.STDIO,
        "verified_launch_sha256": "d" * 64,
        "remote_origin": None,
        "authorization_issuer": None,
        "authentication": PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        "declared_capabilities": {
            "prompt:compose": None,
            "tool:search": SCHEMA_DIGEST,
        },
        "extension_versions": {
            "io.modelcontextprotocol/tasks": None,
            "io.modelcontextprotocol/apps": "1.0.0",
        },
        "capability_revision": REVISION,
        "limits": McpClientLimits(),
    }
    values.update(changes)
    return McpConnectionDescriptor(**values)  # type: ignore[arg-type]


def test_signed_apps_authority_is_immutable_declared_and_identity_keyed() -> None:
    descriptor = _local_descriptor(signed_capability_authority=_signed_authority())
    authority = descriptor.signed_capability_authority
    assert authority is not None
    assert authority["mcp_apps_metadata"]["tools"]["search"]["title"] == "Search notes"
    with pytest.raises(TypeError):
        authority["mcp_apps_metadata"]["tools"]["search"]["title"] = "changed"

    with pytest.raises(ValueError, match="signed_capability_authority_invalid"):
        _local_descriptor(
            extension_versions={"io.modelcontextprotocol/tasks": None},
            signed_capability_authority=_signed_authority(),
        )
    orphan = _signed_authority()
    orphan["mcp_apps_metadata"]["tools"]["other"] = {"resourceUri": "ui://notes/other"}
    with pytest.raises(ValueError, match="signed_capability_authority_invalid"):
        _local_descriptor(signed_capability_authority=orphan)


def _remote_descriptor(**changes: object) -> McpConnectionDescriptor:
    values: dict[str, object] = {
        "app_id": "com.example.remote",
        "publisher_id": "example.publisher",
        "deployment": McpClientDeployment.REMOTE,
        "provider_id": "example",
        "protocol_version": MCP_PROTOCOL_VERSION,
        "transport": McpTransport.STREAMABLE_HTTP,
        "verified_launch_sha256": None,
        "remote_origin": "https://mcp.example.com",
        "authorization_issuer": "https://auth.example.com/oauth2",
        "authentication": PluginAuthentication.from_dict(
            {
                "kind": "oauth_authorization_code_pkce",
                "scopes": ["example.read"],
                "secret_fields": [],
            }
        ),
        "declared_capabilities": {"tool:lookup": SCHEMA_DIGEST},
        "extension_versions": {},
        "capability_revision": REVISION,
        "limits": McpClientLimits(),
    }
    values.update(changes)
    return McpConnectionDescriptor(**values)  # type: ignore[arg-type]


def test_runtime_compatibility_descriptor_accepts_only_the_supported_closed_shape() -> None:
    compatibility = {
        "wire_protocol_version": LATEST_HANDSHAKE_VERSION,
        "handshake_mode": "legacy",
        "missing_metadata": {
            "server_id": "StatelessServer",
            "server_version": "ESF",
        },
    }
    descriptor = _remote_descriptor(runtime_compatibility=compatibility)
    assert descriptor.to_dict()["runtime_compatibility"] == compatibility

    for invalid in (
        {**compatibility, "provider_id": "forbidden.branch"},
        {**compatibility, "wire_protocol_version": "2099-01-01"},
        {**compatibility, "handshake_mode": "current"},
        {
            **compatibility,
            "missing_metadata": {
                **compatibility["missing_metadata"],
                "remote_origin": "https://forged.example",
            },
        },
    ):
        with pytest.raises(ValueError, match="^runtime_compatibility_invalid$"):
            _remote_descriptor(runtime_compatibility=invalid)


def _input_request(**changes: object) -> McpInputRequest:
    values: dict[str, object] = {
        "request_id": "request-2",
        "capability_id": "tool:search",
        "field_ids": ("query", "scope"),
        "field_schemas": {
            "scope": {"type": "string"},
            "query": {"minLength": 1, "type": "string"},
        },
        "request_state": b"opaque-state\x00",
        "round_number": 1,
    }
    values.update(changes)
    return McpInputRequest(**values)  # type: ignore[arg-type]


def _task_handle(**changes: object) -> McpTaskHandle:
    values: dict[str, object] = {
        "task_id": "task-7",
        "app_id": "com.example.notes",
        "capability_id": "tool:search",
        "capability_revision": REVISION,
        "connection_revision": OTHER_REVISION,
        "authorization_context_revision": "e" * 64,
    }
    values.update(changes)
    return McpTaskHandle(**values)  # type: ignore[arg-type]


def _result(**changes: object) -> McpOperationResult:
    values: dict[str, object] = {
        "request_id": "request-1",
        "app_id": "com.example.notes",
        "capability_id": "tool:search",
        "capability_revision": REVISION,
        "status": McpOperationStatus.COMPLETED,
        "data": {"items": [{"id": "one"}]},
        "input_request": None,
        "task_handle": None,
        "error": None,
    }
    values.update(changes)
    return McpOperationResult(**values)  # type: ignore[arg-type]


def _live_receipt(**changes: object) -> McpDiscoveryReceipt:
    values: dict[str, object] = {
        "app_id": "com.example.notes",
        "publisher_id": "example.publisher",
        "deployment": McpClientDeployment.LOCAL,
        "transport": McpTransport.STDIO,
        "protocol_version": MCP_PROTOCOL_VERSION,
        "source": DiscoverySource.LIVE_OBSERVED,
        "server_id": "example.notes",
        "server_version": "2.1.0",
        "tools": {},
        "prompts": {},
        "resources": {},
        "resource_templates": {},
        "extensions": {},
        "cache_metadata": {},
        "capability_revision": REVISION,
    }
    values.update(changes)
    return McpDiscoveryReceipt(**values)  # type: ignore[arg-type]


def _operation_request(**changes: object) -> McpOperationRequest:
    values: dict[str, object] = {
        "request_id": "request-1",
        "app_id": "com.example.notes",
        "operation": McpOperationKind.TOOL_CALL,
        "capability_id": "tool:search",
        "arguments": {},
        "capability_revision": REVISION,
        "deadline_monotonic": 45.5,
        "cancellation_id": "cancel-1",
        "task_id": None,
        "input_request_id": None,
        "request_state": None,
    }
    values.update(changes)
    return McpOperationRequest(**values)  # type: ignore[arg-type]


def test_closed_enums_have_only_the_approved_members() -> None:
    """Catches adding a second deployment, transport, operation, source, or status path."""
    assert {item.value for item in McpClientDeployment} == {"local", "remote"}
    assert {item.value for item in McpTransport} == {"stdio", "streamable_http"}
    assert {item.value for item in DiscoverySource} == {
        "catalog_declared",
        "live_observed",
        "live_authenticated",
    }
    assert {item.value for item in McpOperationKind} == {
        "tools/list",
        "tools/call",
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "subscriptions/listen",
        "tasks/get",
        "tasks/update",
        "tasks/cancel",
    }
    assert {item.value for item in McpOperationStatus} == {
        "completed",
        "input_required",
        "task_started",
        "task_recovery_required",
        "task_updated",
        "progress",
        "not_modified",
        "failed",
    }


def test_local_descriptor_preserves_exact_identity_and_one_transport() -> None:
    """Catches dropping or normalizing an authority-bearing local identity value."""
    descriptor = _local_descriptor()

    assert descriptor.app_id == "com.example.notes"
    assert descriptor.publisher_id == "example.publisher"
    assert descriptor.deployment is McpClientDeployment.LOCAL
    assert descriptor.provider_id is None
    assert descriptor.protocol_version == "2026-07-28"
    assert descriptor.transport is McpTransport.STDIO
    assert descriptor.verified_launch_sha256 == "d" * 64
    assert descriptor.capability_revision == REVISION
    assert descriptor.to_dict()["transport"] == "stdio"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"verified_launch_sha256": None}, "local descriptor launch identity is invalid"),
        (
            {"remote_origin": "https://mcp.example.com"},
            "local descriptor remote identity is invalid",
        ),
        (
            {"authorization_issuer": "https://auth.example.com"},
            "local descriptor remote identity is invalid",
        ),
        ({"transport": McpTransport.STREAMABLE_HTTP}, "local descriptor transport is invalid"),
        ({"transport": (McpTransport.STDIO,)}, "descriptor transport is invalid"),
    ],
)
def test_local_descriptor_requires_only_verified_stdio_launch_identity(
    changes: dict[str, object], message: str
) -> None:
    """Catches accepting an unverified, remote, or multi-transport local launch."""
    with pytest.raises(ValueError, match=f"^{message}$"):
        _local_descriptor(**changes)


def test_remote_descriptor_preserves_exact_identity() -> None:
    """Catches losing the signed remote origin or issuer binding."""
    descriptor = _remote_descriptor()

    assert descriptor.deployment is McpClientDeployment.REMOTE
    assert descriptor.transport is McpTransport.STREAMABLE_HTTP
    assert descriptor.verified_launch_sha256 is None
    assert descriptor.remote_origin == "https://mcp.example.com"
    assert descriptor.authorization_issuer == "https://auth.example.com/oauth2"
    assert descriptor.to_dict()["remote_origin"] == "https://mcp.example.com"


def test_remote_descriptor_may_carry_signed_capability_authority() -> None:
    """Catches forbidding signed tool policy on an honest REMOTE HTTP descriptor."""
    authority = {
        "tools": {
            "search": {
                "title": "Search notes",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
                "input_schema_sha256": sha256_hex(
                    canonical_bytes(
                        {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        }
                    )
                ),
                "output_schema": {
                    "type": "object",
                    "properties": {"items": {"type": "array"}},
                },
                "output_schema_sha256": sha256_hex(
                    canonical_bytes(
                        {
                            "type": "object",
                            "properties": {"items": {"type": "array"}},
                        }
                    )
                ),
                "risk_class": "read_only",
                "effect": "read",
                "requires_revision": False,
                "max_result_bytes": 4096,
                "allowed_mime_types": ["application/json"],
            }
        },
        "resources": {},
        "resource_templates": {},
    }

    descriptor = _remote_descriptor(signed_capability_authority=authority)

    assert descriptor.deployment is McpClientDeployment.REMOTE
    assert descriptor.transport is McpTransport.STREAMABLE_HTTP
    assert descriptor.signed_capability_authority is not None
    assert "search" in descriptor.signed_capability_authority["tools"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"remote_origin": None}, "remote_origin must be a canonical HTTPS origin"),
        ({"authorization_issuer": None}, "authorization_issuer must be a canonical HTTPS issuer"),
        ({"verified_launch_sha256": "d" * 64}, "remote descriptor launch identity is invalid"),
        ({"transport": McpTransport.STDIO}, "remote descriptor transport is invalid"),
        ({"transport": (McpTransport.STREAMABLE_HTTP,)}, "descriptor transport is invalid"),
    ],
)
def test_remote_descriptor_requires_only_streamable_http_remote_identity(
    changes: dict[str, object], message: str
) -> None:
    """Catches accepting a local, incomplete, or multi-transport remote connection."""
    with pytest.raises(ValueError, match=f"^{message}$"):
        _remote_descriptor(**changes)


@pytest.mark.parametrize(
    "origin",
    [
        "http://mcp.example.com",
        "https://MCP.example.com",
        "https://mcp.example.com/",
        "https://mcp.example.com/path/../other",
        "https://mcp.example.com//path",
        "https://mcp.example.com:443",
        "https://user@mcp.example.com",
        "https://mcp.example.com?query=1",
        "https://mcp.example.com#fragment",
    ],
)
def test_remote_origin_must_be_normalized_https(origin: str) -> None:
    """Catches alternate-origin spellings that would weaken exact request binding."""
    with pytest.raises(ValueError, match="^remote_origin must be a canonical HTTPS origin$"):
        _remote_descriptor(remote_origin=origin)


@pytest.mark.parametrize(
    "issuer",
    [
        "http://auth.example.com",
        "https://AUTH.example.com/oauth2",
        "https://auth.example.com:443/oauth2",
        "https://auth.example.com/oauth2?tenant=one",
        "https://auth.example.com/oauth2#fragment",
        "https://auth.example.com/oauth2//v1",
        "https://auth.example.com/oauth2/../v1",
    ],
)
def test_authorization_issuer_must_be_normalized_https(issuer: str) -> None:
    """Catches issuer aliases that would permit authorization-context drift."""
    with pytest.raises(ValueError, match="^authorization_issuer must be a canonical HTTPS issuer$"):
        _remote_descriptor(authorization_issuer=issuer)


@pytest.mark.parametrize(
    "issuer",
    [
        # Miro and Netlify publish their issuer with the terminating slash, and
        # it is not an alternate spelling of anything: RFC 8414 s3.1 says to
        # remove it before inserting the well-known path, which is exactly what
        # `_metadata_url` now does. Refusing it here made both unreachable.
        "https://mcp.miro.com/",
        "https://netlify-mcp.netlify.app/",
        "https://auth.example.com/oauth2/",
        "https://auth.example.com",
        "https://auth.monday.com/mcp",
    ],
)
def test_an_issuer_may_carry_a_terminating_slash(issuer: str) -> None:
    descriptor = _remote_descriptor(authorization_issuer=issuer)

    assert descriptor.authorization_issuer == issuer


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol_version": "2026-07-27"},
        {"deployment": "local"},
        {"transport": "stdio"},
    ],
)
def test_descriptor_accepts_only_exact_current_typed_identity(changes: dict[str, object]) -> None:
    """Catches stringly typed or retired live-connection identity."""
    with pytest.raises(ValueError):
        _local_descriptor(**changes)


@pytest.mark.parametrize("target", ["descriptor", "receipt"])
def test_protocol_identity_rejects_equality_compatible_non_string(target: str) -> None:
    """Catches an object impersonating the exact protocol string through equality."""
    protocol = _EqualityCompatibleProtocol()

    with pytest.raises(ValueError):
        if target == "descriptor":
            _local_descriptor(protocol_version=protocol)
        else:
            _live_receipt(protocol_version=protocol)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_id", "e\N{COMBINING ACUTE ACCENT}"),
        ("publisher_id", " publisher"),
        ("provider_id", "provider\nname"),
    ],
)
def test_descriptor_identity_is_nfc_plain_text(field: str, value: str) -> None:
    """Catches ambiguous Unicode, surrounding whitespace, or control characters in identity."""
    with pytest.raises(ValueError):
        _remote_descriptor(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verified_launch_sha256", "D" * 64),
        ("verified_launch_sha256", "d" * 63),
        ("capability_revision", "A" * 64),
        ("capability_revision", "a" * 65),
    ],
)
def test_descriptor_digests_and_revision_are_exact_lowercase_sha256(field: str, value: str) -> None:
    """Catches ambiguous or non-SHA-256 launch and capability revisions."""
    with pytest.raises(ValueError):
        _local_descriptor(**{field: value})


def test_descriptor_canonicalizes_and_freezes_capability_mappings() -> None:
    """Catches insertion-order drift or mutable nested capability state."""
    source = {
        "tool:zeta": SCHEMA_DIGEST,
        "prompt:alpha": None,
    }
    versions = {"vendor/zeta": "2.0.0", "io.modelcontextprotocol/tasks": None}

    descriptor = _local_descriptor(
        declared_capabilities=source,
        extension_versions=versions,
    )
    source["tool:later"] = OTHER_REVISION
    versions["vendor/later"] = "1.0.0"

    assert isinstance(descriptor.declared_capabilities, MappingProxyType)
    assert tuple(descriptor.declared_capabilities) == ("prompt:alpha", "tool:zeta")
    assert tuple(descriptor.extension_versions) == (
        "io.modelcontextprotocol/tasks",
        "vendor/zeta",
    )
    assert "tool:later" not in descriptor.declared_capabilities
    with pytest.raises(TypeError):
        descriptor.declared_capabilities["tool:later"] = OTHER_REVISION  # type: ignore[index]


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        pytest.param("0.0.0", True, id="minimum"),
        pytest.param("1.2.3-RC.1+Build.5", True, id="prerelease-build-case"),
        pytest.param(None, False, id="versionless-non-tasks"),
        pytest.param("", False, id="empty"),
        pytest.param("1.0", False, id="incomplete"),
        pytest.param("01.0.0", False, id="leading-zero"),
        pytest.param("v1.0.0", False, id="prefix"),
        pytest.param("1.0.0+", False, id="empty-build"),
        pytest.param("1.0.0-01", False, id="numeric-prerelease-leading-zero"),
        pytest.param(" 1.0.0", False, id="leading-whitespace"),
        pytest.param("1.0.0 ", False, id="trailing-whitespace"),
    ],
)
def test_descriptor_non_tasks_extension_has_manifest_semver_schema_parity(
    version: object,
    accepted: bool,
) -> None:
    """Catches arbitrary strings replacing exact signed-manifest SemVer authority."""
    manifest_schema = json.loads(
        Path("schema/plugins/plugin_manifest.schema.json").read_text(encoding="utf-8")
    )
    schema_accepts = not tuple(
        Draft202012Validator(manifest_schema["$defs"]["semver"]).iter_errors(version)
    )
    try:
        descriptor = _local_descriptor(extension_versions={"vendor/extension": version})
    except (TypeError, ValueError):
        runtime_accepts = False
    else:
        runtime_accepts = True
        assert descriptor.extension_versions == {"vendor/extension": version}

    assert schema_accepts is accepted
    assert runtime_accepts is accepted


def test_descriptor_preserves_only_exact_tasks_as_versionless_across_copy_and_mutation() -> None:
    """Catches copy paths trusting a mutated or caller-owned extension-version mapping."""
    versions: dict[str, object] = {
        "io.modelcontextprotocol/tasks": None,
        "vendor/extension": "1.2.3",
    }
    descriptor = _local_descriptor(extension_versions=versions)
    versions["vendor/extension"] = "not-semver"

    copied = replace(descriptor)
    assert copied.extension_versions == {
        "io.modelcontextprotocol/tasks": None,
        "vendor/extension": "1.2.3",
    }
    with pytest.raises(ValueError):
        _local_descriptor(extension_versions=versions)
    with pytest.raises(ValueError):
        replace(descriptor, extension_versions={"vendor/extension": None})
    with pytest.raises(ValueError):
        replace(
            descriptor,
            extension_versions={"io.modelcontextprotocol/tasks": "1.0.0"},
        )

    object.__setattr__(descriptor, "extension_versions", {"vendor/extension": "not-semver"})
    with pytest.raises(ValueError):
        replace(descriptor)


def test_signed_local_authority_is_exact_deeply_frozen_and_survives_receipt_projection() -> None:
    """Catches dropping or mutating signed tool/resource policy after verification."""
    source = _signed_authority()
    descriptor = _local_descriptor(signed_capability_authority=source)
    receipt = _live_receipt(
        extensions={
            "io.modelcontextprotocol/apps": {
                "version": "1.0.0",
                "description": "MCP Apps metadata",
            }
        },
        signed_capability_authority=descriptor.signed_capability_authority,
    )

    source["tools"]["search"]["max_result_bytes"] = 1  # type: ignore[index]
    source["tools"]["search"]["input_schema"]["properties"]["query"][  # type: ignore[index]
        "type"
    ] = "integer"
    source["resources"]["notes"]["uri"] = "omega://forged"  # type: ignore[index]

    assert isinstance(descriptor.signed_capability_authority, MappingProxyType)
    assert descriptor.signed_capability_authority["tools"]["search"]["max_result_bytes"] == 4096
    assert (
        descriptor.signed_capability_authority["tools"]["search"]["input_schema"]
        ["properties"]["query"]["type"]
        == "string"
    )
    assert descriptor.signed_capability_authority["resources"]["notes"]["uri"] == (
        "omega://notes/all"
    )
    assert receipt.signed_capability_authority == descriptor.signed_capability_authority
    with pytest.raises(TypeError):
        descriptor.signed_capability_authority["tools"]["search"][  # type: ignore[index]
            "max_result_bytes"
        ] = 1


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("missing", "risk_class"),
        ("extra", "provider_policy"),
        ("input_digest", "0" * 64),
        ("output_digest", "0" * 64),
        ("risk_class", "unknown"),
        ("effect", "sideways"),
    ],
)
def test_signed_tool_authority_rejects_nonexact_or_drifting_policy(
    mutation: str,
    value: str,
) -> None:
    """Catches a planner-visible tool policy diverging from signed catalog authority."""
    authority = _signed_authority()
    policy = authority["tools"]["search"]  # type: ignore[index]
    if mutation == "missing":
        del policy[value]
    elif mutation == "extra":
        policy[value] = "forged"
    elif mutation == "input_digest":
        policy["input_schema_sha256"] = value
    elif mutation == "output_digest":
        policy["output_schema_sha256"] = value
    else:
        policy[mutation] = value

    with pytest.raises(ValueError, match="^signed_capability_authority_invalid$"):
        _local_descriptor(signed_capability_authority=authority)


@pytest.mark.parametrize(
    "authority",
    [
        {},
        {"tools": {}, "resources": {}},
        {
            "tools": {"search": {"input_schema_sha256": SCHEMA_DIGEST}},
            "resources": {},
            "resource_templates": {},
        },
    ],
)
def test_signed_local_authority_rejects_partial_or_unknown_policy(
    authority: dict[str, object],
) -> None:
    """Catches permissive defaults replacing omitted signed enforcement fields."""
    with pytest.raises(ValueError, match="^signed_capability_authority_invalid$"):
        _local_descriptor(signed_capability_authority=authority)


def test_limits_cover_every_approved_positive_integer_bound() -> None:
    """Catches an unbounded client loop, document, event stream, or lifetime."""
    limits = McpClientLimits()
    expected = {
        "max_discovery_document_bytes",
        "max_result_document_bytes",
        "max_list_pages",
        "max_list_items",
        "max_cursor_history",
        "max_tool_argument_bytes",
        "max_structured_result_bytes",
        "max_resource_text_bytes",
        "max_embedded_resource_bytes",
        "max_input_rounds",
        "max_input_fields",
        "max_task_polls",
        "max_task_updates",
        "max_task_duration_ms",
        "max_progress_events",
        "max_notification_events",
        "max_cache_entries",
        "max_cache_ttl_seconds",
        "max_active_tasks",
        "max_task_tombstones",
        "max_http_redirects",
        "max_connection_duration_ms",
        "max_operation_duration_ms",
        "max_stderr_bytes",
        "max_shutdown_duration_ms",
    }

    assert {item.name for item in fields(McpClientLimits)} == expected
    assert all(
        type(getattr(limits, name)) is int and getattr(limits, name) > 0 for name in expected
    )
    json.dumps(limits.to_dict(), allow_nan=False)


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "1"])
def test_every_limit_rejects_non_positive_or_non_integer_values(invalid: object) -> None:
    """Catches a disabled cap or a value with non-deterministic integer semantics."""
    for field in fields(McpClientLimits):
        with pytest.raises(ValueError, match=f"^{field.name} must be a positive integer$"):
            replace(McpClientLimits(), **{field.name: invalid})


def test_discovery_receipt_preserves_source_and_canonical_observation() -> None:
    """Catches source confusion or mutable live capability evidence."""
    tools = {"zeta": {"schema_sha256": SCHEMA_DIGEST}, "alpha": {"title": "Alpha"}}
    receipt = McpDiscoveryReceipt(
        app_id="com.example.notes",
        publisher_id="example.publisher",
        deployment=McpClientDeployment.LOCAL,
        transport=McpTransport.STDIO,
        protocol_version=MCP_PROTOCOL_VERSION,
        source=DiscoverySource.LIVE_OBSERVED,
        server_id="example.notes",
        server_version="2.1.0",
        tools=tools,
        prompts={},
        resources={},
        resource_templates={},
        extensions={"io.modelcontextprotocol/tasks": {"version": "1.0.0"}},
        cache_metadata={"scope": "public", "ttl_seconds": 30},
        capability_revision=REVISION,
    )
    tools["later"] = {"title": "Later"}

    assert receipt.source is DiscoverySource.LIVE_OBSERVED
    assert tuple(receipt.tools) == ("alpha", "zeta")
    assert isinstance(receipt.tools["zeta"], MappingProxyType)
    assert "later" not in receipt.tools
    document = receipt.to_dict()
    assert document["source"] == "live_observed"
    assert tuple(document["tools"]) == ("alpha", "zeta")
    json.dumps(document, allow_nan=False)


def test_catalog_receipt_forbids_claiming_observed_server_identity() -> None:
    """Catches dormant catalog evidence being represented as a live observation."""
    with pytest.raises(ValueError, match="^catalog discovery server identity is invalid$"):
        McpDiscoveryReceipt(
            app_id="com.example.notes",
            publisher_id="example.publisher",
            deployment=McpClientDeployment.LOCAL,
            transport=McpTransport.STDIO,
            protocol_version=MCP_PROTOCOL_VERSION,
            source=DiscoverySource.CATALOG_DECLARED,
            server_id="unexpected-live-server",
            server_version=None,
            tools={},
            prompts={},
            resources={},
            resource_templates={},
            extensions={},
            cache_metadata={},
            capability_revision=REVISION,
        )


@pytest.mark.parametrize("missing", ["server_id", "server_version"])
def test_live_receipt_requires_complete_observed_server_identity(missing: str) -> None:
    """Catches live authority published without current response identity."""
    values = {
        "app_id": "com.example.notes",
        "publisher_id": "example.publisher",
        "deployment": McpClientDeployment.LOCAL,
        "transport": McpTransport.STDIO,
        "protocol_version": MCP_PROTOCOL_VERSION,
        "source": DiscoverySource.LIVE_OBSERVED,
        "server_id": "example.notes",
        "server_version": "2.1.0",
        "tools": {},
        "prompts": {},
        "resources": {},
        "resource_templates": {},
        "extensions": {},
        "cache_metadata": {},
        "capability_revision": REVISION,
    }
    values[missing] = None

    with pytest.raises(ValueError, match="^live discovery server identity is invalid$"):
        McpDiscoveryReceipt(**values)  # type: ignore[arg-type]


def test_operation_request_is_closed_canonical_and_absolute_deadlined() -> None:
    """Catches unknown operations, mutable arguments, or relative/non-finite deadlines."""
    arguments = {"zeta": [2, 1], "alpha": {"enabled": True}}
    request = McpOperationRequest(
        request_id="request-1",
        app_id="com.example.notes",
        operation=McpOperationKind.TOOL_CALL,
        capability_id="tool:search",
        arguments=arguments,
        capability_revision=REVISION,
        deadline_monotonic=45.5,
        cancellation_id="cancel-1",
        task_id=None,
        input_request_id=None,
        request_state=None,
    )
    arguments["later"] = False

    assert request.operation is McpOperationKind.TOOL_CALL
    assert tuple(request.arguments) == ("alpha", "zeta")
    assert request.arguments["zeta"] == (2, 1)
    assert "later" not in request.arguments
    assert request.deadline_monotonic == 45.5
    json.dumps(request.to_dict(), allow_nan=False)


def test_operation_request_rejects_non_nfc_json_keys() -> None:
    """Catches canonically ambiguous keys in arguments or other public mappings."""
    with pytest.raises(ValueError, match="^arguments keys must be NFC plain strings$"):
        McpOperationRequest(
            request_id="request-1",
            app_id="com.example.notes",
            operation=McpOperationKind.TOOL_CALL,
            capability_id="tool:search",
            arguments={"e\N{COMBINING ACUTE ACCENT}": True},
            capability_revision=REVISION,
            deadline_monotonic=45.5,
            cancellation_id="cancel-1",
            task_id=None,
            input_request_id=None,
            request_state=None,
        )


def test_operation_request_allows_only_one_continuation_identity() -> None:
    """Catches a request ambiguously resuming both a Task and input-required round."""
    with pytest.raises(ValueError, match="^operation continuation identity is invalid$"):
        McpOperationRequest(
            request_id="request-1",
            app_id="com.example.notes",
            operation=McpOperationKind.TOOL_CALL,
            capability_id="tool:search",
            arguments={},
            capability_revision=REVISION,
            deadline_monotonic=45.5,
            cancellation_id="cancel-1",
            task_id="task-1",
            input_request_id="request-previous",
            request_state=b"opaque-state",
        )


@pytest.mark.parametrize("operation", ["tools/call", "unknown", None])
def test_operation_request_rejects_untyped_or_unknown_operation(operation: object) -> None:
    """Catches an operation escaping the closed dispatch vocabulary."""
    with pytest.raises(ValueError, match="^operation is invalid$"):
        McpOperationRequest(
            request_id="request-1",
            app_id="com.example.notes",
            operation=operation,  # type: ignore[arg-type]
            capability_id="tool:search",
            arguments={},
            capability_revision=REVISION,
            deadline_monotonic=45.5,
            cancellation_id="cancel-1",
            task_id=None,
            input_request_id=None,
            request_state=None,
        )


@pytest.mark.parametrize("deadline", [0, -1, math.nan, math.inf, -math.inf, True, "45"])
def test_operation_request_requires_positive_finite_absolute_deadline(deadline: object) -> None:
    """Catches missing, relative-zero, or non-orderable operation deadlines."""
    with pytest.raises(ValueError, match="^deadline_monotonic must be a positive finite number$"):
        McpOperationRequest(
            request_id="request-1",
            app_id="com.example.notes",
            operation=McpOperationKind.TOOL_CALL,
            capability_id="tool:search",
            arguments={},
            capability_revision=REVISION,
            deadline_monotonic=deadline,  # type: ignore[arg-type]
            cancellation_id="cancel-1",
            task_id=None,
            input_request_id=None,
            request_state=None,
        )


def test_input_request_preserves_bytes_and_closes_field_ids() -> None:
    """Catches request-state rewriting or a schema for an undeclared response field."""
    request = _input_request()

    assert request.request_state == b"opaque-state\x00"
    assert request.field_ids == ("query", "scope")
    assert tuple(request.field_schemas) == ("query", "scope")
    assert request.to_dict()["request_state_base64"] == "b3BhcXVlLXN0YXRlAA=="
    json.dumps(request.to_dict(), allow_nan=False)

    with pytest.raises(ValueError, match="^input field schemas do not match field_ids$"):
        _input_request(field_schemas={"query": {"type": "string"}})


def test_opaque_request_state_is_excluded_from_repr() -> None:
    """Catches opaque continuation state entering ordinary diagnostics."""
    input_request = _input_request(request_state=b"opaque-marker")
    operation_request = McpOperationRequest(
        request_id="request-3",
        app_id="com.example.notes",
        operation=McpOperationKind.TOOL_CALL,
        capability_id="tool:search",
        arguments={},
        capability_revision=REVISION,
        deadline_monotonic=45.5,
        cancellation_id="cancel-3",
        task_id=None,
        input_request_id="request-2",
        request_state=b"opaque-marker",
    )

    assert "opaque-marker" not in repr(input_request)
    assert "opaque-marker" not in repr(operation_request)


def test_task_handle_binds_app_capability_authorization_and_connection_revisions() -> None:
    """Catches an unbound Task handle that could cross app or connection authority."""
    handle = _task_handle()

    assert handle.to_dict() == {
        "task_id": "task-7",
        "app_id": "com.example.notes",
        "capability_id": "tool:search",
        "capability_revision": REVISION,
        "connection_revision": OTHER_REVISION,
        "authorization_context_revision": "e" * 64,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"capability_revision": "A" * 64},
        {"connection_revision": "b" * 63},
        {"authorization_context_revision": None},
    ],
)
def test_task_handle_rejects_unbound_or_malformed_revisions(changes: dict[str, object]) -> None:
    """Catches a Task handle without exact authority-bearing revisions."""
    with pytest.raises(ValueError):
        _task_handle(**changes)


@pytest.mark.parametrize(
    ("status", "changes"),
    [
        (
            McpOperationStatus.INPUT_REQUIRED,
            {"data": {}, "input_request": _input_request()},
        ),
        (
            McpOperationStatus.TASK_STARTED,
            {"data": {}, "task_handle": _task_handle()},
        ),
        (
            McpOperationStatus.TASK_RECOVERY_REQUIRED,
            {"data": {}, "task_handle": _task_handle()},
        ),
        (
            McpOperationStatus.TASK_UPDATED,
            {"data": {}, "task_handle": _task_handle()},
        ),
        (McpOperationStatus.PROGRESS, {"data": {"completed": 1, "total": 2}}),
        (McpOperationStatus.NOT_MODIFIED, {"data": {}}),
        (
            McpOperationStatus.FAILED,
            {"data": {}, "error": McpClientError(McpClientErrorReason.REQUEST_TIMEOUT)},
        ),
    ],
)
def test_operation_result_accepts_each_closed_status_variant(
    status: McpOperationStatus, changes: dict[str, object]
) -> None:
    """Catches removing an approved result variant or its typed payload."""
    result = _result(status=status, **changes)

    assert result.status is status
    json.dumps(result.to_dict(), allow_nan=False)


@pytest.mark.parametrize(
    ("status", "changes"),
    [
        (McpOperationStatus.COMPLETED, {"input_request": _input_request()}),
        (McpOperationStatus.INPUT_REQUIRED, {"data": {}, "input_request": None}),
        (McpOperationStatus.TASK_STARTED, {"data": {}, "task_handle": None}),
        (McpOperationStatus.TASK_RECOVERY_REQUIRED, {"data": {}, "task_handle": None}),
        (McpOperationStatus.TASK_UPDATED, {"data": {}, "task_handle": None}),
        (McpOperationStatus.PROGRESS, {"data": {}}),
        (McpOperationStatus.NOT_MODIFIED, {"data": {"unexpected": True}}),
        (McpOperationStatus.FAILED, {"data": {}, "error": None}),
        (
            McpOperationStatus.FAILED,
            {
                "data": {"raw": "must-not-survive"},
                "error": McpClientError(McpClientErrorReason.RESULT_INVALID),
            },
        ),
        (McpOperationStatus.COMPLETED, {"input_request": "not-an-input-request"}),
        (McpOperationStatus.COMPLETED, {"task_handle": "not-a-task-handle"}),
        (McpOperationStatus.COMPLETED, {"error": "not-an-error"}),
    ],
)
def test_operation_result_rejects_status_payload_mismatch(
    status: McpOperationStatus, changes: dict[str, object]
) -> None:
    """Catches a status that could be mistaken for a different result variant."""
    with pytest.raises(ValueError, match="^operation result status payload is invalid$"):
        _result(status=status, **changes)


@pytest.mark.parametrize("status", ["completed", "failed", "unknown", None])
def test_operation_result_rejects_untyped_or_unknown_status(status: object) -> None:
    """Catches open string statuses entering the public result boundary."""
    with pytest.raises(ValueError, match="^operation result status is invalid$"):
        _result(status=status)


@pytest.mark.parametrize(
    "status",
    [
        McpOperationStatus.TASK_STARTED,
        McpOperationStatus.TASK_RECOVERY_REQUIRED,
        McpOperationStatus.TASK_UPDATED,
    ],
)
@pytest.mark.parametrize(
    "handle_changes",
    [
        {"app_id": "com.example.other"},
        {"capability_id": "tool:other"},
        {"capability_revision": OTHER_REVISION},
    ],
)
def test_task_result_rejects_mismatched_repeated_authority_identity(
    status: McpOperationStatus, handle_changes: dict[str, object]
) -> None:
    """Catches a Task handle crossing the result's app, capability, or revision binding."""
    with pytest.raises(ValueError, match="^operation result authority identity is invalid$"):
        _result(
            status=status,
            data={},
            task_handle=_task_handle(**handle_changes),
        )


def test_input_result_rejects_mismatched_repeated_capability_identity() -> None:
    """Catches input-required state crossing the result's capability binding."""
    with pytest.raises(ValueError, match="^operation result authority identity is invalid$"):
        _result(
            status=McpOperationStatus.INPUT_REQUIRED,
            data={},
            input_request=_input_request(capability_id="tool:other"),
        )


def test_failure_error_and_serialization_are_closed_and_content_free() -> None:
    """Catches raw causes, bodies, or credential-bearing detail crossing a failure boundary."""
    error = McpClientError(McpClientErrorReason.RESULT_INVALID)
    result = _result(status=McpOperationStatus.FAILED, data={}, error=error)

    assert str(error) == "result_invalid"
    assert repr(error) == "McpClientError(reason='result_invalid')"
    assert error.to_dict() == {"reason": "result_invalid"}
    assert result.to_dict()["error"] == {"reason": "result_invalid"}
    serialized = json.dumps(result.to_dict(), sort_keys=True)
    assert "must-not-survive" not in serialized
    with pytest.raises(TypeError):
        McpClientError(McpClientErrorReason.RESULT_INVALID, "raw server body")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="^mcp client error reason is invalid$"):
        McpClientError("raw server body")  # type: ignore[arg-type]


def test_client_error_is_immutable_after_construction() -> None:
    """Catches post-construction replacement of a stable reason with raw content."""
    error = McpClientError(McpClientErrorReason.RESULT_INVALID)

    with pytest.raises(AttributeError):
        error.args = ("raw server body",)
    with pytest.raises(AttributeError):
        error._reason = McpClientErrorReason.REQUEST_TIMEOUT  # type: ignore[attr-defined]
    assert str(error) == "result_invalid"


def test_error_reason_vocabulary_is_closed_and_content_free() -> None:
    """Catches arbitrary prose replacing stable machine-readable failures."""
    assert {item.value for item in McpClientErrorReason} == {
        "protocol_mismatch",
        "identity_mismatch",
        "capability_drift",
        "authorization_required",
        "authorization_invalid",
        "authorization_store_unavailable",
        "transport_unavailable",
        "request_timeout",
        "document_limit_exceeded",
        "pagination_limit_exceeded",
        "result_limit_exceeded",
        "input_limit_exceeded",
        "task_invalid",
        "progress_limit_exceeded",
        "notification_limit_exceeded",
        "redirect_limit_exceeded",
        "result_invalid",
        "cleanup_required",
        "plugin_review_required",
    }


def test_public_records_are_frozen_and_nested_values_are_immutable() -> None:
    """Catches post-construction authority or result mutation."""
    descriptor = _local_descriptor()
    result = _result()

    with pytest.raises(FrozenInstanceError):
        descriptor.app_id = "com.example.changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.status = McpOperationStatus.FAILED  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.data["later"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        result.data["items"][0]["id"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("record", "authority_field", "replacement"),
    [
        pytest.param(
            McpClientLimits(),
            "max_list_pages",
            0,
            id="limits",
        ),
        pytest.param(
            _local_descriptor(),
            "app_id",
            "com.example.changed",
            id="descriptor",
        ),
        pytest.param(
            _live_receipt(),
            "capability_revision",
            "not-a-revision",
            id="receipt",
        ),
        pytest.param(
            _operation_request(),
            "deadline_monotonic",
            0,
            id="operation-request",
        ),
        pytest.param(
            _result(),
            "status",
            "failed",
            id="operation-result",
        ),
        pytest.param(
            _input_request(),
            "round_number",
            0,
            id="input-request",
        ),
        pytest.param(
            _task_handle(),
            "capability_revision",
            "not-a-revision",
            id="task-handle",
        ),
        pytest.param(
            McpClientError(McpClientErrorReason.RESULT_INVALID),
            "_reason",
            "raw server body",
            id="client-error",
        ),
    ],
)
def test_public_contracts_expose_no_writable_instance_dictionary(
    record: object, authority_field: str, replacement: object
) -> None:
    """Catches forbidden fields or invalid authority state bypassing constructor checks."""
    with pytest.raises((AttributeError, TypeError)):
        record.__dict__["secret"] = "forbidden"  # type: ignore[attr-defined]
    with pytest.raises((AttributeError, TypeError)):
        record.__dict__[authority_field] = replacement  # type: ignore[attr-defined]


def test_public_contract_fields_exclude_credential_path_and_personal_identity_slots() -> None:
    """Catches adding private material or host-path carriers to the public boundary."""
    record_types = (
        McpClientLimits,
        McpConnectionDescriptor,
        McpDiscoveryReceipt,
        McpOperationRequest,
        McpOperationResult,
        McpInputRequest,
        McpTaskHandle,
    )
    forbidden = {
        "token",
        "code",
        "secret",
        "header",
        "cookie",
        "path",
        "email",
        "account_id",
        "user_id",
        "person_id",
        "personal_id",
    }

    public_fields = {field.name for record_type in record_types for field in fields(record_type)}
    assert not any(term in name.casefold() for name in public_fields for term in forbidden)


def test_a_client_error_survives_a_generator_context_manager_unwind() -> None:
    """The freeze must not eat the failure it is carrying.

    ``contextlib._GeneratorContextManager.__exit__`` assigns
    ``value.__traceback__`` while unwinding, and anyio's cancel scopes do the
    same. Freezing every attribute turned any McpClientError that crossed one
    into ``AttributeError: McpClientError is immutable`` -- the HUD showed
    "AttributeError" for Miro and Stripe instead of why they refused (live
    2026-09-07).

    Nothing is given up: ``raise X from Y`` and implicit chaining set
    ``__cause__``/``__context__`` through C slots that never reach
    ``__setattr__``, so the freeze was not keeping a raw cause off the error --
    only breaking the unwind.
    """
    import contextlib

    from mcp_platform.client_contracts import McpClientError, McpClientErrorReason

    @contextlib.contextmanager
    def scope():
        yield

    with pytest.raises(McpClientError) as caught:
        with scope():
            raise McpClientError(McpClientErrorReason.CAPABILITY_DRIFT)

    assert caught.value.reason is McpClientErrorReason.CAPABILITY_DRIFT


@pytest.mark.parametrize("name", ["reason", "_reason", "_frozen", "anything"])
def test_a_client_error_is_still_frozen_against_everything_else(name: str) -> None:
    from mcp_platform.client_contracts import McpClientError, McpClientErrorReason

    error = McpClientError(McpClientErrorReason.RESULT_INVALID)

    with pytest.raises(AttributeError, match="immutable"):
        setattr(error, name, "tampered")
    assert error.reason is McpClientErrorReason.RESULT_INVALID
