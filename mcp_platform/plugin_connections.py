"""Provider-neutral connection lifecycle for signed plugin MCP apps."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from mcp import types

from mcp_platform.app_registry import InstalledMcpAppRecord, McpAppRegistry
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client import CurrentMcpClient
from mcp_platform.client_contracts import (
    McpClientError,
    McpClientErrorReason,
    McpClientDeployment,
    McpClientLimits,
    McpConnectionDescriptor,
    McpOperationKind,
    McpOperationRequest,
    McpOperationStatus,
    McpTransport,
)
from mcp_platform.oauth_broker import (
    BrokerTransportAuthority,
    BrokerTransportBinding,
    OAuthAuthorizationAttempt,
    OAuthAuthorizationStart,
    OAuthBroker,
    OAuthConnectionRequest,
    OAuthPreparedAuthorization,
    _issue_broker_transport_authority,
)
from mcp_platform.oauth_profiles import OAuthProfileMatchInput
from mcp_platform.oauth_profiles import OAuthAuthorizationFlow
from mcp_platform.plugin_authority import (
    PluginAuthorityStore,
    PluginCapabilityBindingIdentity,
    PluginConnectionAuthority,
    PluginConnectionAuthorityKind,
    PluginExecutionPolicy,
    plugin_connection_authority_revision,
    plugin_connection_binding_revision,
)
from mcp_platform.plugin_discovery import LocalCapabilityEnvelope
from mcp_platform.plugin_grants import (
    PluginGrantActivation,
    PluginGrantRecord,
    PluginGrantService,
)
from mcp_platform.plugin_execution_worker import (
    PluginExecutionAuthority,
    PluginExecutionWorkerError,
    _LocalMcpWorkerExecution,
)
from mcp_platform.plugin_state import (
    ConnectionStart,
    PluginAppState,
    PluginLifecycleSnapshot,
    PluginLifecycleState,
)
from mcp_platform.package_verifier import VerifiedPackage, _is_verifier_issued_package
from mcp_platform.registry import (
    InstalledPackageRecord,
    McpPackageNotInstalled,
    PackageRegistry,
)
from mcp_platform.transports.streamable_http import (
    StreamableHttpClientConfig,
    build_streamable_http_transport,
)
from plugin_platform.contracts import (
    AppRequirement,
    CapabilityKind,
    PluginAuthenticationKind,
    PluginManifest,
)
from plugin_platform.local_trust import LocalTrustError, LocalTrustRoot
from plugin_platform.records import InstalledPluginRecord, PluginAppBinding
from plugin_platform.package_verifier import VerifiedPlugin


def _emit_discovery_failure(
    *,
    plugin_id: str,
    app_id: str,
    reason: str,
) -> None:
    """Persist a provider-discovery failure before it crosses the HUD boundary."""
    try:
        from guardian.terminal_feedback import emit_data

        safe_reason = str(reason or "plugin_capability_invalid")
        if not safe_reason.startswith(("plugin_", "oauth_")):
            safe_reason = "plugin_capability_invalid"
        emit_data(
            "guardian",
            "plugin_discovery",
            status="fail",
            ok=False,
            summary=safe_reason,
            reason=safe_reason,
            plugin_id=plugin_id,
            app_id=app_id,
            stage="discovery",
        )
    except Exception:
        pass


class _ProviderDiscoveryError(McpClientError):
    """Typed discovery failure retained across the worker boundary."""

    __slots__ = ("phase",)

    def __init__(self, reason: McpClientErrorReason) -> None:
        super().__init__(reason)
        object.__setattr__(self, "phase", "discovery")


def _lifecycle_mutation(method: Callable[..., Any]) -> Callable[..., Any]:
    """Fence every public connection/session mutation at its real entry."""

    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def guarded_async(
            self: object,
            *args: object,
            **kwargs: object,
        ) -> Any:
            guard = getattr(self, "_lifecycle_mutation_async_guard", None)
            if not callable(guard):
                return await method(self, *args, **kwargs)
            async with guard():
                return await method(self, *args, **kwargs)

        return guarded_async

    @wraps(method)
    def guarded(self: object, *args: object, **kwargs: object) -> Any:
        guard = getattr(self, "_lifecycle_mutation_guard", None)
        scope = nullcontext() if not callable(guard) else guard()
        with scope:
            return method(self, *args, **kwargs)

    return guarded


class _PluginRegistry(Protocol):
    def get(self, plugin_id: str) -> object: ...

    def reverify_installed(self, plugin_id: str) -> object: ...

    def snapshot(self) -> object: ...

    def verified_installed_snapshot(self, plugin_id: str) -> object: ...

    def set_enabled(
        self,
        plugin_id: str,
        enabled: bool,
        *,
        expected_revision: str,
    ) -> object: ...


ProfileMatchProvider = Callable[[str, InstalledMcpAppRecord], OAuthProfileMatchInput]
RequirementProvider = Callable[[str, InstalledMcpAppRecord], AppRequirement]
DescriptorProvider = Callable[[str, str], McpConnectionDescriptor]
CandidateDescriptorProvider = Callable[
    [VerifiedPlugin, PluginAppBinding, str], McpConnectionDescriptor
]
CandidateRecordProvider = Callable[
    [VerifiedPlugin, PluginAppBinding, str], InstalledMcpAppRecord
]
OfferedScopesProvider = Callable[[str, str], tuple[str, ...]]
ExecutionPolicyProvider = Callable[[str], PluginExecutionPolicy]
ClientFactory = Callable[[McpConnectionDescriptor, object], CurrentMcpClient]
OperationSubmitter = Callable[[Callable[[], Awaitable[Any]]], Any]
LocalConnector = Callable[[str, InstalledMcpAppRecord], Awaitable[CurrentMcpClient]]


@dataclass(frozen=True, slots=True)
class AuthorizationStart:
    plugin_id: str
    app_id: str
    account_key_sha256: str
    status: str
    request_id: str | None
    authorization_url: str | None
    expires_at: float | None
    authorization_context_revision: str
    provider_scopes: tuple[str, ...]
    execution_policy: PluginExecutionPolicy
    capability_ceiling: tuple[str, ...] = ()
    allowed_tool_schemas: Mapping[str, str] = field(default_factory=dict)
    source_constraints: Mapping[str, object] = field(default_factory=dict)
    destination_constraints: Mapping[str, object] = field(default_factory=dict)
    authorization_flow: str | None = None
    user_code: str | None = None
    verification_uri: str | None = None
    poll_interval_seconds: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "app_id": self.app_id,
            "account_key_sha256": self.account_key_sha256,
            "status": self.status,
            "request_id": self.request_id,
            "authorization_url": self.authorization_url,
            "expires_at": self.expires_at,
            "authorization_context_revision": self.authorization_context_revision,
            "provider_scopes": list(self.provider_scopes),
            "execution_policy": self.execution_policy.value,
            "capability_ceiling": list(self.capability_ceiling),
            "allowed_tool_schemas": dict(self.allowed_tool_schemas),
            "source_constraints": dict(self.source_constraints),
            "destination_constraints": dict(self.destination_constraints),
            "authorization_flow": self.authorization_flow,
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "poll_interval_seconds": self.poll_interval_seconds,
        }


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    plugin_id: str
    app_id: str
    account_key_sha256: str
    status: str
    authorization_context_revision: str
    token_revision: int | None
    provider_scopes: tuple[str, ...]
    execution_policy: PluginExecutionPolicy
    authority_revision: str
    capability_ceiling: tuple[str, ...] = ()
    allowed_tool_schemas: Mapping[str, str] = field(default_factory=dict)
    source_constraints: Mapping[str, object] = field(default_factory=dict)
    destination_constraints: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "app_id": self.app_id,
            "account_key_sha256": self.account_key_sha256,
            "status": self.status,
            "authorization_context_revision": self.authorization_context_revision,
            "token_revision": self.token_revision,
            "provider_scopes": list(self.provider_scopes),
            "execution_policy": self.execution_policy.value,
            "authority_revision": self.authority_revision,
            "capability_ceiling": list(self.capability_ceiling),
            "allowed_tool_schemas": dict(self.allowed_tool_schemas),
            "source_constraints": dict(self.source_constraints),
            "destination_constraints": dict(self.destination_constraints),
        }


@dataclass(frozen=True, slots=True)
class _ConnectionSettings:
    scopes: tuple[str, ...]
    capability_ceiling: tuple[str, ...]
    allowed_tool_schemas: Mapping[str, str]
    execution_policy: PluginExecutionPolicy
    source_constraints: Mapping[str, object]
    destination_constraints: Mapping[str, object]


@dataclass(slots=True)
class _ActiveConnection:
    client: CurrentMcpClient
    tools: tuple[types.Tool, ...]
    grant: PluginGrantRecord | None
    binding: BrokerTransportBinding | None
    capability_revision: str
    capability_binding_identities: tuple[PluginCapabilityBindingIdentity, ...]
    authorization_context_revision: str
    descriptor_revision: str
    package_revision: str
    credentialless_authority: PluginConnectionAuthority | None = None
    server_id: str | None = None
    server_version: str | None = None


@dataclass(frozen=True, slots=True)
class _WorkerDiscovery:
    tools: tuple[types.Tool, ...]
    server_id: str
    server_version: str


@dataclass(frozen=True, slots=True)
class _PreparedAuthorityContext:
    authority: PluginConnectionAuthority
    package_revision: str
    server_id: str
    server_version: str


class _WorkerOnlyLocalClient:
    """Non-provider parent marker; all local MCP I/O belongs to the worker."""

    async def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _BindingExpectation:
    identity: OAuthPreparedAuthorization | PluginGrantRecord
    descriptor_revision: str
    package_revision: str
    candidate_verified: VerifiedPlugin | None = None
    candidate_binding: PluginAppBinding | None = None
    registry_revision: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateConnectionProbe:
    """Version-scoped readiness proof that is not live selected authority."""

    status: str
    connection_revision: str

    def __post_init__(self) -> None:
        if (
            self.status != "connected"
            or type(self.connection_revision) is not str
            or len(self.connection_revision) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.connection_revision
            )
        ):
            raise ValueError("plugin_connection_state_invalid")


@dataclass(frozen=True, slots=True, repr=False)
class PluginExecutionWorkerPreparation:
    """Parent-validated launch material for one killable provider operation."""

    authority: PluginExecutionAuthority
    descriptor: McpConnectionDescriptor
    credential: Mapping[str, object]
    local_execution: _LocalMcpWorkerExecution | None = None

    def __post_init__(self) -> None:
        try:
            if (
                type(self.authority) is not PluginExecutionAuthority
                or type(self.descriptor) is not McpConnectionDescriptor
                or self.descriptor.app_id != self.authority.app_id
                or self.descriptor.capability_revision
                != self.authority.capability_revision
                or not isinstance(self.credential, Mapping)
                or any(type(key) is not str for key in self.credential)
            ):
                raise ValueError
            credential = dict(self.credential)
            canonical_bytes(credential)
            if (
                self.descriptor.deployment is McpClientDeployment.LOCAL
                and (credential or type(self.local_execution) is not _LocalMcpWorkerExecution)
                or self.descriptor.deployment is McpClientDeployment.REMOTE
                and self.local_execution is not None
            ):
                raise ValueError
            if set(credential) not in {frozenset(), frozenset({"access_token"})}:
                raise ValueError
            if "access_token" in credential and (
                type(credential["access_token"]) is not str
                or not credential["access_token"]
                or len(credential["access_token"].encode("utf-8")) > 16_384
                or any(ord(character) < 0x20 for character in credential["access_token"])
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_worker_preparation_invalid") from None
        object.__setattr__(self, "credential", MappingProxyType(credential))

    def __repr__(self) -> str:
        return (
            "PluginExecutionWorkerPreparation("
            f"authority={self.authority!r}, descriptor={self.descriptor!r}, "
            "credential=<redacted>, local_execution=<sealed>)"
        )


@dataclass(frozen=True, slots=True)
class CandidateConnectionPreparation:
    """Compiled candidate OAuth authority held dormant until promotion."""

    request_id: str
    plugin_id: str
    app_id: str
    target_version: str
    target_manifest_sha256: str
    target_package_sha256: str
    candidate_binding_revision: str
    activation: PluginGrantActivation

    def __post_init__(self) -> None:
        try:
            if (
                type(self.request_id) is not str
                or len(self.request_id) != 64
                or any(character not in "0123456789abcdef" for character in self.request_id)
                or type(self.plugin_id) is not str
                or not self.plugin_id
                or type(self.app_id) is not str
                or not self.app_id
                or type(self.target_version) is not str
                or not self.target_version
                or type(self.activation) is not PluginGrantActivation
                or self.activation.plugin_id != self.plugin_id
                or self.activation.app_id != self.app_id
                or self.activation.attempt_state_sha256 != self.request_id
            ):
                raise ValueError
            for value in (
                self.target_manifest_sha256,
                self.target_package_sha256,
                self.candidate_binding_revision,
            ):
                if type(value) is not str or len(value) != 64 or any(
                    character not in "0123456789abcdef" for character in value
                ):
                    raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ValueError("plugin_connection_state_invalid") from None


@dataclass(frozen=True, slots=True)
class PluginUpdateConnectionRecovery:
    """Locally sealed predecessor authority needed for reversible cutover."""

    checkpoint_id: str
    plugin_id: str
    account_key_sha256: str
    predecessor_app_ids: tuple[str, ...]
    candidate_app_ids: tuple[str, ...]
    grant_recovery: Mapping[str, object]
    authority_lineages: tuple[Mapping[str, object], ...]
    live_sessions: tuple[Mapping[str, object], ...]
    revision: str

    def __post_init__(self) -> None:
        try:
            if (
                type(self.checkpoint_id) is not str
                or not self.checkpoint_id.startswith("plugin-update-recovery-")
                or type(self.plugin_id) is not str
                or not self.plugin_id
                or type(self.account_key_sha256) is not str
                or len(self.account_key_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.account_key_sha256
                )
            ):
                raise ValueError
            predecessors = tuple(self.predecessor_app_ids)
            candidates = tuple(self.candidate_app_ids)
            if any(
                type(values) is not tuple
                or values != tuple(sorted(values))
                or len(values) != len(set(values))
                or any(type(item) is not str or not item for item in values)
                for values in (predecessors, candidates)
            ):
                raise ValueError
            grant_recovery = PluginGrantService._update_recovery(
                dict(self.grant_recovery)
            )
            if (
                grant_recovery["checkpoint_id"] != self.checkpoint_id
                or grant_recovery["plugin_id"] != self.plugin_id
                or grant_recovery["account_key_sha256"] != self.account_key_sha256
                or tuple(grant_recovery["app_ids"])
                != tuple(sorted(set(predecessors + candidates)))
            ):
                raise ValueError
            lineages = tuple(
                PluginAuthorityStore._connection_lineage_checkpoint(dict(item))
                for item in self.authority_lineages
            )
            expected_apps = tuple(sorted(set(predecessors + candidates)))
            if tuple(str(item["app_id"]) for item in lineages) != expected_apps or any(
                item["plugin_id"] != self.plugin_id
                or item["account_key_hash"] != self.account_key_sha256
                for item in lineages
            ):
                raise ValueError
            sessions: list[dict[str, object]] = []
            for raw in self.live_sessions:
                if type(raw) is not dict or set(raw) != {
                    "app_id",
                    "had_live_session",
                    "connection_revision",
                }:
                    raise ValueError
                app_id = raw["app_id"]
                had_live = raw["had_live_session"]
                connection_revision = raw["connection_revision"]
                if (
                    app_id not in predecessors
                    or type(had_live) is not bool
                    or type(connection_revision) is not str
                    or (had_live and len(connection_revision) != 64)
                    or had_live
                    and any(
                        character not in "0123456789abcdef"
                        for character in connection_revision
                    )
                    or (not had_live and connection_revision)
                ):
                    raise ValueError
                sessions.append(dict(raw))
            if tuple(str(item["app_id"]) for item in sessions) != predecessors:
                raise ValueError
            material = {
                "checkpoint_id": self.checkpoint_id,
                "plugin_id": self.plugin_id,
                "account_key_sha256": self.account_key_sha256,
                "predecessor_app_ids": list(predecessors),
                "candidate_app_ids": list(candidates),
                "grant_recovery": grant_recovery,
                "authority_lineages": list(lineages),
                "live_sessions": sessions,
            }
            revision = sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.plugin-connection-update-recovery.v1",
                        "material": material,
                    }
                )
            )
            if self.revision != revision:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError("plugin_update_recovery_required") from None
        object.__setattr__(self, "predecessor_app_ids", predecessors)
        object.__setattr__(self, "candidate_app_ids", candidates)
        object.__setattr__(self, "grant_recovery", grant_recovery)
        object.__setattr__(self, "authority_lineages", lineages)
        object.__setattr__(self, "live_sessions", tuple(sessions))
        object.__setattr__(self, "revision", revision)

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "plugin_id": self.plugin_id,
            "account_key_sha256": self.account_key_sha256,
            "predecessor_app_ids": list(self.predecessor_app_ids),
            "candidate_app_ids": list(self.candidate_app_ids),
            "grant_recovery": dict(self.grant_recovery),
            "authority_lineages": [dict(item) for item in self.authority_lineages],
            "live_sessions": [dict(item) for item in self.live_sessions],
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginUpdateConnectionRecovery:
        if type(value) is not dict or set(value) != {
            "checkpoint_id",
            "plugin_id",
            "account_key_sha256",
            "predecessor_app_ids",
            "candidate_app_ids",
            "grant_recovery",
            "authority_lineages",
            "live_sessions",
            "revision",
        }:
            raise ValueError("plugin_update_recovery_required")
        if any(
            type(value[field]) is not list
            for field in (
                "predecessor_app_ids",
                "candidate_app_ids",
                "authority_lineages",
                "live_sessions",
            )
        ):
            raise ValueError("plugin_update_recovery_required")
        try:
            return cls(
                checkpoint_id=value["checkpoint_id"],
                plugin_id=value["plugin_id"],
                account_key_sha256=value["account_key_sha256"],
                predecessor_app_ids=tuple(value["predecessor_app_ids"]),
                candidate_app_ids=tuple(value["candidate_app_ids"]),
                grant_recovery=value["grant_recovery"],
                authority_lineages=tuple(value["authority_lineages"]),
                live_sessions=tuple(value["live_sessions"]),
                revision=value["revision"],
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_update_recovery_required") from None


def _account_digest(account_key: str) -> str:
    if (
        type(account_key) is not str
        or not account_key
        or account_key.strip() != account_key
        or len(account_key) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in account_key)
    ):
        raise ValueError("plugin_account_invalid")
    return hashlib.sha256(account_key.encode("utf-8")).hexdigest()


def _record_requirement(record: InstalledMcpAppRecord) -> AppRequirement:
    return AppRequirement.from_dict(
        {
            "app_id": record.app_id,
            "publisher_id": record.publisher_id,
            "deployment": record.deployment.value,
            "provider_id": record.provider_id,
            "protocol_version": record.protocol_version,
            "transports": [transport.value for transport in record.transports],
            "remote_origin": record.remote_origin,
            "authorization_issuer": record.authorization_issuer,
            "authentication": record.authentication.to_dict(),
            "capabilities": [capability.to_dict() for capability in record.capabilities],
        }
    )


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _verified_plugin_authority(
    verified: VerifiedPlugin,
    app_id: str,
) -> dict[str, object] | None:
    """Return one signed app's capability authority as the verifier parsed it.

    The package verifier is the only parser of ``tools/authority.json``; re-
    deriving it here would be a second, weaker view of the same signed bytes.
    """

    if type(verified) is not VerifiedPlugin:
        raise ValueError("plugin_catalog_authority_missing")
    authority = verified.capability_authority
    if authority is None:
        return None
    if type(app_id) is not str or app_id not in authority:
        raise ValueError("plugin_catalog_authority_missing")
    return dict(authority[app_id])


def _installed_package_authority(package: object) -> dict[str, object]:
    tools: dict[str, object] = {}
    for tool in package.tool_catalog.tools:
        tools[tool.name] = {
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
    resources = {
        item.name: {
            "uri": item.uri,
            "mime_type": item.mime_type,
            "max_bytes": item.max_bytes,
        }
        for item in package.resource_catalog.resources
    }
    templates = {
        item.name: {
            "uri_template": item.uri_template,
            "mime_type": item.mime_type,
            "max_bytes": item.max_bytes,
        }
        for item in package.resource_catalog.resource_templates
    }
    return {
        "tools": tools,
        "resources": resources,
        "resource_templates": templates,
        "prompts": {},
    }


def signed_capability_authority_from_package(
    package_registry: PackageRegistry,
    record: InstalledMcpAppRecord,
    *,
    plugin_registry: Any | None = None,
    plugin_id: str | None = None,
) -> dict[str, object] | None:
    """Load signed tool policy from a verified package, or None if absent."""

    if plugin_registry is not None:
        if type(plugin_id) is not str or not plugin_id:
            raise ValueError("plugin_catalog_authority_missing")
        verified = plugin_registry.reverify_installed(plugin_id)
        authority = _verified_plugin_authority(verified, record.app_id)
        if authority is not None:
            return authority
    try:
        package = package_registry.reverify_installed(record.app_id)
    except Exception:
        package = None
    if package is not None:
        return _installed_package_authority(package)
    return None


def signed_capability_authority_for_candidate(
    package_registry: PackageRegistry,
    record: InstalledMcpAppRecord,
    candidate_verified: VerifiedPlugin,
) -> dict[str, object] | None:
    """Load authority for an exact unselected candidate without reading selected state."""

    authority = _verified_plugin_authority(candidate_verified, record.app_id)
    if authority is not None:
        return authority
    try:
        package = package_registry.reverify_installed(record.app_id)
    except McpPackageNotInstalled:
        package = None
    except Exception:
        raise ValueError("plugin_catalog_authority_missing") from None
    if package is not None:
        return _installed_package_authority(package)
    return None


def locally_sealed_capability_authority(
    envelope: LocalCapabilityEnvelope, trust_root: LocalTrustRoot
) -> dict[str, object]:
    """Project one locally sealed discovery envelope into the descriptor's authority shape.

    A publisher-verified or personal connection has no Omega signature to
    load the way ``signed_capability_authority_from_package`` and
    ``signed_capability_authority_for_candidate`` do: its authority is the
    operator's own restricted-discovery review, sealed on-device by
    ``LocalTrustRoot`` (``mcp_platform.plugin_discovery``). ``envelope``'s
    own construction seal only proves it came from that module's code, not
    that its ``capability_authority`` still agrees with its ``mac`` -- this
    re-verifies the MAC against ``trust_root`` before projecting anything,
    so a fabricated or corrupted envelope is refused rather than trusted.
    The projection mirrors the two signed loaders' exact ``{"tools",
    "resources", "resource_templates", "prompts"}`` return shape so
    ``descriptor_for_record`` never needs to know which lane produced it.
    """

    if type(envelope) is not LocalCapabilityEnvelope:
        raise ValueError("plugin_catalog_authority_missing")
    try:
        trust_root.verify_authority(envelope.capability_authority, envelope.sealed_authority())
    except LocalTrustError:
        raise ValueError("plugin_catalog_authority_missing") from None
    return {
        group: dict(envelope.capability_authority.get(group, {}))
        for group in ("tools", "resources", "resource_templates", "prompts")
    }


def _receipt_revision_admissible(
    receipt: Mapping[str, object],
    descriptor: McpConnectionDescriptor,
) -> bool:
    """Whether this discovery's capability revision is one the descriptor allows.

    The revision identifies the capability set the worker actually reconciled.
    When that set matches the installed record, the revision must match too --
    anything else is drift. When the operator granted fewer capabilities than
    the record declares, the revision differs by construction, and demanding
    equality is what refused every partial grant.
    """
    if receipt["capability_revision"] == descriptor.capability_revision:
        return True
    live: set[str] = set()
    for group, kind in (
        ("tools", "tool"),
        ("prompts", "prompt"),
        ("resources", "resource"),
        ("resource_templates", "resource"),
    ):
        entries = receipt.get(group)
        if not isinstance(entries, Mapping):
            return False
        live |= {f"{kind}:{name}" for name in entries}
    declared = set(descriptor.declared_capabilities)
    return bool(live < declared)


def descriptor_for_record(
    record: InstalledMcpAppRecord,
    *,
    signed_capability_authority: Mapping[str, object] | None = None,
) -> McpConnectionDescriptor:
    runtime_compatibility = None
    if signed_capability_authority is not None:
        runtime_compatibility = signed_capability_authority.get("runtime_compatibility")
        signed_capability_authority = {
            key: value
            for key, value in signed_capability_authority.items()
            if key != "runtime_compatibility"
        }
    requirement = _record_requirement(record)
    snapshot = project_declared_capabilities(requirement).snapshot
    declared = {
        f"{capability.kind.value}:{capability.name}": capability.schema_sha256
        for capability in record.capabilities
        if capability.kind in {CapabilityKind.TOOL, CapabilityKind.RESOURCE, CapabilityKind.PROMPT}
    }
    extensions = {
        capability.name: capability.version
        for capability in record.capabilities
        if capability.kind is CapabilityKind.EXTENSION
    }
    if record.deployment.value == "local":
        if (
            record.transports != (McpTransport.STDIO,)
            or type(record.local_package_manifest_sha256) is not str
        ):
            raise ValueError("plugin_connection_transport_invalid")
        return McpConnectionDescriptor(
            app_id=record.app_id,
            publisher_id=record.publisher_id,
            deployment=McpClientDeployment.LOCAL,
            provider_id=record.provider_id,
            protocol_version=record.protocol_version,
            transport=McpTransport.STDIO,
            verified_launch_sha256=record.local_package_manifest_sha256,
            remote_origin=None,
            authorization_issuer=None,
            authentication=record.authentication,
            declared_capabilities=declared,
            extension_versions=extensions,
            capability_revision=snapshot.capability_revision,
            limits=McpClientLimits(),
            signed_capability_authority=signed_capability_authority,
            runtime_compatibility=runtime_compatibility,
        )
    if record.deployment.value != "remote" or record.transports != (McpTransport.STREAMABLE_HTTP,):
        raise ValueError("plugin_connection_transport_invalid")
    return McpConnectionDescriptor(
        app_id=record.app_id,
        publisher_id=record.publisher_id,
        deployment=McpClientDeployment.REMOTE,
        provider_id=record.provider_id,
        protocol_version=record.protocol_version,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin=record.remote_origin,
        authorization_issuer=record.authorization_issuer,
        authentication=record.authentication,
        declared_capabilities=declared,
        extension_versions=extensions,
        capability_revision=snapshot.capability_revision,
        limits=McpClientLimits(),
        signed_capability_authority=signed_capability_authority,
        runtime_compatibility=runtime_compatibility,
    )


def _default_client_factory(
    descriptor: McpConnectionDescriptor,
    authorization: object,
) -> CurrentMcpClient:
    factory = build_streamable_http_transport(
        descriptor,
        (
            StreamableHttpClientConfig.unauthenticated()
            if authorization is None
            else StreamableHttpClientConfig.authenticated(authorization)
        ),
    )
    return CurrentMcpClient(descriptor, factory)


def _stable_scopes(value: object) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or len(value) != len(set(value))
        or any(type(item) is not str or not item or item.strip() != item for item in value)
    ):
        raise ValueError("plugin_scopes_invalid")
    return tuple(sorted(value))


def _resolved_issuer_matches_declared(declared: str, resolved: str) -> bool:
    """Allow a signed OAuth profile to specialize only a declared root origin."""

    if declared == resolved:
        return True
    try:
        declared_url = urlsplit(declared)
        resolved_url = urlsplit(resolved)
    except (TypeError, ValueError):
        return False
    return bool(
        declared_url.scheme == "https"
        and declared_url.path in {"", "/"}
        and not declared_url.query
        and not declared_url.fragment
        and resolved_url.scheme == declared_url.scheme
        and resolved_url.netloc == declared_url.netloc
        and not resolved_url.query
        and not resolved_url.fragment
    )


def _terminal_prepared_failure(error: BaseException) -> bool:
    reason = getattr(getattr(error, "reason", None), "value", None)
    return str(error) in {
        "oauth_prepared_authorization_invalid",
        "oauth_authorization_flow_mismatch",
        "oauth_reauthorization_required",
        "authorization_required",
        "authorization_invalid",
    } or reason in {"authorization_required", "authorization_invalid"}


# The service implementation follows below. It intentionally has no provider branches,
# credential inputs, token-store inputs, redirect overrides, or private event loop.


class PluginConnectionService:
    """Coordinate exact broker grants with live supervisor-owned MCP sessions."""

    def __init__(
        self,
        *,
        app_registry: McpAppRegistry,
        plugin_registry: _PluginRegistry,
        broker: OAuthBroker,
        grant_service: PluginGrantService,
        descriptor_provider: DescriptorProvider,
        candidate_descriptor_provider: CandidateDescriptorProvider | None = None,
        candidate_record_provider: CandidateRecordProvider | None = None,
        requirement_provider: RequirementProvider,
        offered_scopes_provider: OfferedScopesProvider,
        operation_submitter: OperationSubmitter,
        profile_match_provider: ProfileMatchProvider | None = None,
        execution_policy_provider: ExecutionPolicyProvider | None = None,
        client_factory: ClientFactory = _default_client_factory,
        local_connector: LocalConnector | None = None,
        package_registry: PackageRegistry | None = None,
        transport_authority: BrokerTransportAuthority | object | None = None,
        execution_worker: object | None = None,
    ) -> None:
        dependencies = (
            getattr(app_registry, "get", None),
            getattr(plugin_registry, "get", None),
            getattr(plugin_registry, "reverify_installed", None),
            getattr(plugin_registry, "snapshot", None),
            getattr(plugin_registry, "verified_installed_snapshot", None),
            getattr(plugin_registry, "set_enabled", None),
            getattr(broker, "begin", None),
            getattr(broker, "complete", None),
            getattr(broker, "recover", None),
            getattr(broker, "callback_state_sha256", None),
            getattr(broker, "authorization_attempt", None),
            getattr(broker, "discard_attempt", None),
            getattr(broker, "discard_connection_attempts", None),
            getattr(grant_service, "current", None),
            getattr(grant_service, "current_for_attempt", None),
            getattr(grant_service, "pending_disconnect", None),
            getattr(grant_service, "suspend", None),
            getattr(grant_service, "activate", None),
            getattr(grant_service, "begin_disconnect", None),
            getattr(grant_service, "finish_disconnect", None),
            descriptor_provider,
            requirement_provider,
            offered_scopes_provider,
            operation_submitter,
            client_factory,
        )
        if any(not callable(item) for item in dependencies):
            raise ValueError("plugin_connection_configuration_invalid")
        if candidate_descriptor_provider is not None and not callable(
            candidate_descriptor_provider
        ):
            raise ValueError("plugin_connection_configuration_invalid")
        if candidate_record_provider is not None and not callable(candidate_record_provider):
            raise ValueError("plugin_connection_configuration_invalid")
        if package_registry is not None and type(package_registry) is not PackageRegistry:
            raise ValueError("plugin_connection_configuration_invalid")
        self._app_registry = app_registry
        self._plugin_registry = plugin_registry
        self._broker = broker
        self._grants = grant_service
        self._descriptor_provider = descriptor_provider
        self._candidate_descriptor_provider = candidate_descriptor_provider
        self._candidate_record_provider = candidate_record_provider
        self._requirement_provider = requirement_provider
        self._offered_scopes_provider = offered_scopes_provider
        self._profile_match_provider = profile_match_provider
        self._execution_policy_provider = execution_policy_provider
        self._client_factory = client_factory
        self._operation_submitter = operation_submitter
        self._local_connector = local_connector
        self._package_registry = package_registry
        # The connection service never builds its own worker.  A privately
        # constructed worker would execute provider operations behind a
        # second, unfenced lifecycle authority whose failed reaps are
        # invisible to the shared worker's global admission and close
        # accounting; production composition always injects the single
        # lifecycle-fenced worker from ``mcp_platform/plugin_lifecycle.py``.
        # Discovery, restart, and refresh all cross the worker boundary
        # through ``run_async``; an injected worker that only offers
        # ``run_sync`` -- or is omitted entirely -- is a configuration error,
        # not a late AttributeError or a silently unfenced fallback worker.
        if execution_worker is None or not all(
            callable(getattr(execution_worker, method, None))
            for method in ("run_sync", "run_async", "close")
        ):
            raise ValueError("plugin_connection_configuration_invalid")
        self._execution_worker = execution_worker
        self._sessions: dict[tuple[str, str, str], _ActiveConnection] = {}
        self._states: dict[tuple[str, str, str], str] = {}
        recovered, _receipts = self._broker.recover()
        self._prepared_attempts = {prepared.state_sha256: prepared for prepared in recovered}
        if len(self._prepared_attempts) != len(recovered):
            raise ValueError("plugin_connection_recovery_invalid")
        self._callback_locks: dict[str, asyncio.Lock] = {}
        self._lifecycle_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._probe_bindings: dict[BrokerTransportBinding, _BindingExpectation] = {}
        self._launch_bindings: dict[BrokerTransportBinding, _BindingExpectation] = {}
        self._update_session_stashes: dict[
            str,
            dict[tuple[str, str, str], _ActiveConnection],
        ] = {}
        self._restored_update_checkpoints: set[str] = set()
        self._transport_authority = (
            _issue_broker_transport_authority(
                broker,
                current_binding=self._current_transport_binding,
            )
            if transport_authority is None
            else transport_authority
        )

    def __repr__(self) -> str:
        return "PluginConnectionService(<sealed-broker>)"

    @property
    def authority_store(self):
        return self._grants.authority_store

    async def _submit(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        handoff = getattr(self, "_lifecycle_mutation_handoff", None)
        submitted = handoff(operation) if callable(handoff) else operation
        result = self._operation_submitter(submitted)
        return await result if inspect.isawaitable(result) else result

    def _lifecycle_lock(self, key: tuple[str, str, str]) -> asyncio.Lock:
        lock = self._lifecycle_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._lifecycle_locks[key] = lock
        return lock

    def _installed(
        self,
        plugin_id: str,
        app_id: str,
        *,
        allow_blocked: bool = False,
    ) -> InstalledMcpAppRecord:
        if type(plugin_id) is not str or not plugin_id or type(app_id) is not str or not app_id:
            raise ValueError("plugin_connection_invalid")
        try:
            plugin = self._plugin_registry.get(plugin_id)
            self._plugin_registry.reverify_installed(plugin_id)
            record = self._app_registry.get(app_id)
        except Exception:
            raise ValueError("plugin_not_installed") from None
        binding = getattr(plugin, "app_binding", None)
        owned_apps = ()
        if binding is not None:
            owned_apps = tuple(getattr(binding, "required_app_ids", ())) + tuple(
                getattr(binding, "optional_app_ids", ())
            )
        if (
            getattr(plugin, "status", None) != "installed"
            or (not allow_blocked and getattr(plugin, "blocker", None) is not None)
            or app_id not in owned_apps
            or type(record) is not InstalledMcpAppRecord
            or not record.installed
            or not record.trusted
        ):
            raise ValueError("plugin_not_installed")
        return record

    def _verified_requirement(
        self,
        plugin_id: str,
        record: InstalledMcpAppRecord,
    ) -> AppRequirement:
        requirement = self._requirement_provider(plugin_id, record)
        if type(requirement) is not AppRequirement or requirement != _record_requirement(record):
            raise ValueError("plugin_requirement_invalid")
        try:
            verified = self._plugin_registry.reverify_installed(plugin_id)
            manifest = verified.manifest
            matches = tuple(
                app
                for app in (*manifest.required_apps, *manifest.optional_apps)
                if app.app_id == record.app_id
            )
        except (AttributeError, TypeError, ValueError):
            raise ValueError("plugin_requirement_invalid") from None
        if len(matches) != 1 or matches[0] != requirement:
            raise ValueError("plugin_requirement_invalid")
        return requirement

    def _mutation_definition(
        self,
        plugin_id: str,
        app_id: str,
        *,
        expected_registry_revision: str | None,
    ) -> tuple[InstalledPluginRecord, InstalledMcpAppRecord, AppRequirement, str, str]:
        if type(plugin_id) is not str or not plugin_id or type(app_id) is not str or not app_id:
            raise ValueError("plugin_connection_invalid")
        try:
            observed = self._plugin_registry.verified_installed_snapshot(plugin_id)
            plugin = getattr(observed, "record")
            verified = getattr(observed, "verified")
            registry_revision = getattr(observed, "revision")
            manifest = getattr(verified, "manifest")
            package_revision = getattr(verified, "package_sha256")
            record = self._app_registry.get(app_id)
            binding = plugin.app_binding
            owned_apps = tuple(binding.required_app_ids) + tuple(binding.optional_app_ids)
            matches = tuple(
                app
                for app in (*manifest.required_apps, *manifest.optional_apps)
                if app.app_id == app_id
            )
            requirement = self._requirement_provider(plugin_id, record)
            if (
                type(plugin) is not InstalledPluginRecord
                or type(manifest) is not PluginManifest
                or type(binding) is not PluginAppBinding
                or type(record) is not InstalledMcpAppRecord
                or type(requirement) is not AppRequirement
                or len(matches) != 1
                or matches[0] != requirement
                or requirement != _record_requirement(record)
                or plugin.status != "installed"
                or plugin.blocker is not None
                or app_id not in owned_apps
                or not record.installed
                or not record.trusted
                or type(registry_revision) is not str
                or len(registry_revision) != 64
                or any(character not in "0123456789abcdef" for character in registry_revision)
                or type(package_revision) is not str
                or len(package_revision) != 64
                or any(character not in "0123456789abcdef" for character in package_revision)
            ):
                raise ValueError
        except Exception:
            raise ValueError("plugin_not_installed") from None
        if (
            expected_registry_revision is not None
            and registry_revision != expected_registry_revision
        ):
            raise ValueError("plugin_registry_conflict")
        return plugin, record, requirement, package_revision, registry_revision

    def _require_registry_revision(self, plugin_id: str, expected_revision: str) -> None:
        try:
            observed = self._plugin_registry.verified_installed_snapshot(plugin_id)
            revision = getattr(observed, "revision")
        except Exception:
            raise ValueError("plugin_registry_conflict") from None
        if revision != expected_revision:
            raise ValueError("plugin_registry_conflict")

    @staticmethod
    def _credentialless_connection_revision(
        *,
        plugin_id: str,
        app_id: str,
        account_key_sha256: str,
        descriptor: McpConnectionDescriptor,
        authorization_context_revision: str,
        package_revision: str,
        capability_binding_identities: tuple[PluginCapabilityBindingIdentity, ...],
    ) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "kind": "credentialless",
                    "plugin_id": plugin_id,
                    "app_id": app_id,
                    "account_key_sha256": account_key_sha256,
                    "descriptor": descriptor.to_dict(),
                    "authorization_context_revision": authorization_context_revision,
                    "package_revision": package_revision,
                    "capability_binding_identities": [
                        item.to_dict() for item in capability_binding_identities
                    ],
                }
            )
        )

    @classmethod
    def _credentialless_authority(
        cls,
        *,
        plugin_id: str,
        app_id: str,
        account_key_sha256: str,
        descriptor: McpConnectionDescriptor,
        settings: _ConnectionSettings,
        authorization_context_revision: str,
        package_revision: str,
        capability_binding_identities: tuple[PluginCapabilityBindingIdentity, ...],
    ) -> PluginConnectionAuthority:
        connection_revision = cls._credentialless_connection_revision(
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256=account_key_sha256,
            descriptor=descriptor,
            authorization_context_revision=authorization_context_revision,
            package_revision=package_revision,
            capability_binding_identities=capability_binding_identities,
        )
        authority_values = {
            "plugin_id": plugin_id,
            "app_id": app_id,
            "account_key_hash": account_key_sha256,
            "connection_revision": connection_revision,
            "provider_scopes": (),
            "capability_ceiling": settings.capability_ceiling,
            "allowed_tool_schemas": settings.allowed_tool_schemas,
            "source_constraints": settings.source_constraints,
            "destination_constraints": settings.destination_constraints,
            "execution_policy": settings.execution_policy,
            "authority_kind": PluginConnectionAuthorityKind.CREDENTIALLESS,
        }
        authority = PluginConnectionAuthority(
            **authority_values,
            revision=plugin_connection_authority_revision(**authority_values),
        )
        return authority

    def _publish_credentialless_authority(
        self,
        *,
        plugin_id: str,
        app_id: str,
        account_key_sha256: str,
        descriptor: McpConnectionDescriptor,
        settings: _ConnectionSettings,
        authorization_context_revision: str,
        package_revision: str,
        capability_binding_identities: tuple[PluginCapabilityBindingIdentity, ...],
    ) -> PluginConnectionAuthority:
        authority = self._credentialless_authority(
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256=account_key_sha256,
            descriptor=descriptor,
            settings=settings,
            authorization_context_revision=authorization_context_revision,
            package_revision=package_revision,
            capability_binding_identities=capability_binding_identities,
        )
        try:
            current = self.authority_store.connection(
                plugin_id,
                app_id,
                account_key_sha256,
            )
        except ValueError as exc:
            if str(exc) != "plugin_connection_authority_not_found":
                raise
            expected_revision = ""
        else:
            expected_revision = current.revision
        self.authority_store.publish_connection(
            authority,
            expected_revision=expected_revision,
        )
        return authority

    def _credentialless_authority_is_current(
        self,
        authority: PluginConnectionAuthority,
        *,
        plugin_id: str,
        descriptor: McpConnectionDescriptor,
    ) -> tuple[
        str,
        tuple[PluginCapabilityBindingIdentity, ...],
    ] | None:
        try:
            package_revision = self._package_revision(plugin_id)
            tools = self._signed_local_tools(descriptor)
            ceiling, schemas, identities = self._compile_capability_bindings(
                descriptor,
                tools,
                capability_ceiling=authority.capability_ceiling,
                allowed_tool_schemas=authority.allowed_tool_schemas,
            )
            settings = _ConnectionSettings(
                scopes=(),
                capability_ceiling=ceiling,
                allowed_tool_schemas=schemas,
                execution_policy=authority.execution_policy,
                source_constraints=dict(authority.source_constraints),
                destination_constraints=dict(authority.destination_constraints),
            )
            context_revision = self._context_revision(
                descriptor,
                account_key_sha256=authority.account_key_hash,
                settings=settings,
                package_revision=package_revision,
            )
            expected = self._credentialless_connection_revision(
                plugin_id=plugin_id,
                app_id=descriptor.app_id,
                account_key_sha256=authority.account_key_hash,
                descriptor=descriptor,
                authorization_context_revision=context_revision,
                package_revision=package_revision,
                capability_binding_identities=identities,
            )
            if (
                authority.plugin_id != plugin_id
                or authority.app_id != descriptor.app_id
                or authority.connection_revision != expected
            ):
                return None
            return context_revision, identities
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _held_consent(authority: PluginConnectionAuthority) -> dict[str, object]:
        """``start`` keywords restating the selection a credentialless authority holds."""
        return {
            "scopes": (),
            "capability_ceiling": tuple(authority.capability_ceiling),
            "allowed_tool_schemas": dict(authority.allowed_tool_schemas),
            "execution_policy": authority.execution_policy,
            "source_constraints": dict(authority.source_constraints),
            "destination_constraints": dict(authority.destination_constraints),
        }

    def _durable_credentialless_consent(
        self,
        plugin_id: str,
        *,
        descriptor: McpConnectionDescriptor,
        account: str,
    ) -> dict[str, object]:
        """The consent a credentialless connection durably holds, for restarting its session.

        A cold session restarts only under the selection the operator already made; with no
        current durable authority there is nothing to restart.
        """
        try:
            authority = self.authority_store.connection(plugin_id, descriptor.app_id, account)
        except ValueError:
            raise ValueError("plugin_connection_not_live") from None
        if (
            self._credentialless_authority_is_current(
                authority, plugin_id=plugin_id, descriptor=descriptor
            )
            is None
        ):
            raise ValueError("plugin_connection_not_live")
        return self._held_consent(authority)

    async def _connect_credentialless_remote(
        self,
        *,
        plugin_id: str,
        app_id: str,
        account_key_sha256: str,
        descriptor: McpConnectionDescriptor,
        settings: _ConnectionSettings,
        authorization_context_revision: str,
        descriptor_revision: str,
        package_revision: str,
        registry_revision: str,
    ) -> AuthorizationStart:
        if self._execution_worker is not None:
            tools = self._signed_local_tools(descriptor)
            ceiling, schemas, binding_digests = self._compile_capability_bindings(
                descriptor,
                tools,
                capability_ceiling=settings.capability_ceiling,
                allowed_tool_schemas=settings.allowed_tool_schemas,
            )
            effective_settings = replace(
                settings,
                capability_ceiling=ceiling,
                allowed_tool_schemas=schemas,
            )
            proposed = self._credentialless_authority(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account_key_sha256,
                descriptor=descriptor,
                settings=effective_settings,
                authorization_context_revision=authorization_context_revision,
                package_revision=package_revision,
                capability_binding_identities=binding_digests,
            )
            worker_authority = self._discovery_authority(
                plugin_id=plugin_id,
                descriptor=descriptor,
                account_key_sha256=account_key_sha256,
                package_revision=package_revision,
                connection_revision=proposed.connection_revision,
                authority_revision=proposed.revision,
            )
            discovered = await self._discover_in_worker(
                authority=worker_authority,
                descriptor=descriptor,
                credential={},
            )
            if self._compile_capability_bindings(
                descriptor,
                discovered.tools,
                capability_ceiling=ceiling,
                allowed_tool_schemas=schemas,
            ) != (ceiling, schemas, binding_digests):
                raise ValueError("plugin_capability_invalid")
            self._require_registry_revision(plugin_id, registry_revision)
            authority = self._publish_credentialless_authority(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account_key_sha256,
                descriptor=descriptor,
                settings=effective_settings,
                authorization_context_revision=authorization_context_revision,
                package_revision=package_revision,
                capability_binding_identities=binding_digests,
            )
            if authority != proposed:
                self.authority_store.invalidate_connection(
                    plugin_id,
                    app_id,
                    account_key_sha256,
                )
                raise ValueError("plugin_connection_drift")
            try:
                self._require_registry_revision(plugin_id, registry_revision)
            except ValueError:
                self.authority_store.invalidate_connection(
                    plugin_id,
                    app_id,
                    account_key_sha256,
                )
                raise
            key = (plugin_id, app_id, account_key_sha256)
            self._sessions[key] = _ActiveConnection(
                _WorkerOnlyLocalClient(),
                discovered.tools,
                None,
                None,
                descriptor.capability_revision,
                binding_digests,
                authorization_context_revision,
                descriptor_revision,
                package_revision,
                authority,
                discovered.server_id,
                discovered.server_version,
            )
            self._states[key] = "connected"
            return AuthorizationStart(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account_key_sha256,
                status="connected",
                request_id=None,
                authorization_url=None,
                expires_at=None,
                authorization_context_revision=authorization_context_revision,
                provider_scopes=(),
                execution_policy=settings.execution_policy,
                capability_ceiling=ceiling,
                allowed_tool_schemas=schemas,
                source_constraints=settings.source_constraints,
                destination_constraints=settings.destination_constraints,
            )
        client = self._client_factory(descriptor, None)
        try:
            await self._submit(client.start)
            tools = await self._submit(lambda: self.snapshot_plugin_tools(client, descriptor))
            ceiling, schemas, binding_digests = self._compile_capability_bindings(
                descriptor,
                tools,
                capability_ceiling=settings.capability_ceiling,
                allowed_tool_schemas=settings.allowed_tool_schemas,
            )
            self._require_registry_revision(plugin_id, registry_revision)
            authority = self._publish_credentialless_authority(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account_key_sha256,
                descriptor=descriptor,
                settings=replace(
                    settings,
                    capability_ceiling=ceiling,
                    allowed_tool_schemas=schemas,
                ),
                authorization_context_revision=authorization_context_revision,
                package_revision=package_revision,
                capability_binding_identities=binding_digests,
            )
            try:
                self._require_registry_revision(plugin_id, registry_revision)
            except ValueError:
                self.authority_store.invalidate_connection(
                    plugin_id,
                    app_id,
                    account_key_sha256,
                )
                raise
        except BaseException:
            with suppress(BaseException):
                await self._submit(client.close)
            raise
        key = (plugin_id, app_id, account_key_sha256)
        previous = self._sessions.pop(key, None)
        if previous is not None:
            with suppress(BaseException):
                await self._submit(previous.client.close)
        self._sessions[key] = _ActiveConnection(
            client,
            tools,
            None,
            None,
            descriptor.capability_revision,
            binding_digests,
            authorization_context_revision,
            descriptor_revision,
            package_revision,
            authority,
        )
        self._states[key] = "connected"
        return AuthorizationStart(
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256=account_key_sha256,
            status="connected",
            request_id=None,
            authorization_url=None,
            expires_at=None,
            authorization_context_revision=authorization_context_revision,
            provider_scopes=(),
            execution_policy=settings.execution_policy,
            capability_ceiling=ceiling,
            allowed_tool_schemas=schemas,
            source_constraints=settings.source_constraints,
            destination_constraints=settings.destination_constraints,
        )

    def _disable_plugin(self, plugin_id: str) -> None:
        plugin = self._plugin_registry.get(plugin_id)
        if getattr(plugin, "enabled", None) is not True:
            return
        snapshot = self._plugin_registry.snapshot()
        self._plugin_registry.set_enabled(
            plugin_id,
            False,
            expected_revision=getattr(snapshot, "revision"),
        )

    def _descriptor(self, plugin_id: str, app_id: str) -> McpConnectionDescriptor:
        descriptor = self._descriptor_provider(plugin_id, app_id)
        if type(descriptor) is not McpConnectionDescriptor or descriptor.app_id != app_id:
            raise ValueError("plugin_connection_invalid")
        return descriptor

    def _package_revision(self, plugin_id: str) -> str:
        try:
            verified = self._plugin_registry.reverify_installed(plugin_id)
            revision = getattr(verified, "package_sha256")
        except Exception:
            raise ValueError("plugin_not_installed") from None
        if (
            type(revision) is not str
            or len(revision) != 64
            or any(character not in "0123456789abcdef" for character in revision)
        ):
            raise ValueError("plugin_not_installed")
        return revision

    @staticmethod
    def _descriptor_revision(descriptor: McpConnectionDescriptor) -> str:
        return sha256_hex(canonical_bytes(descriptor.to_dict()))

    @staticmethod
    def _signed_local_tools(
        descriptor: McpConnectionDescriptor,
    ) -> tuple[types.Tool, ...]:
        authority = descriptor.signed_capability_authority
        policies = authority.get("tools") if isinstance(authority, Mapping) else None
        if not isinstance(policies, Mapping):
            raise ValueError("plugin_capability_invalid")
        tools: list[types.Tool] = []
        try:
            for name in sorted(policies):
                policy = policies[name]
                if type(name) is not str or not isinstance(policy, Mapping):
                    raise ValueError
                tools.append(
                    types.Tool(
                        name=name,
                        title=policy.get("title"),
                        description=policy.get("title"),
                        inputSchema=_plain_json(policy["input_schema"]),
                        outputSchema=_plain_json(policy.get("output_schema")),
                    )
                )
        except (KeyError, TypeError, ValueError):
            raise ValueError("plugin_capability_invalid") from None
        return tuple(tools)

    @staticmethod
    def _discovery_anchor(
        descriptor: McpConnectionDescriptor,
    ) -> tuple[str, str]:
        authority = descriptor.signed_capability_authority
        if not isinstance(authority, Mapping):
            raise ValueError("plugin_capability_invalid")
        for kind, group_name in (
            ("tool", "tools"),
            ("resource", "resources"),
            ("resource_template", "resource_templates"),
            ("prompt", "prompts"),
        ):
            group = authority.get(group_name)
            if not isinstance(group, Mapping):
                raise ValueError("plugin_capability_invalid")
            names = tuple(sorted(group))
            if names:
                name = names[0]
                if type(name) is not str or not isinstance(group[name], Mapping):
                    raise ValueError("plugin_capability_invalid")
                declared_kind = "resource" if kind == "resource_template" else kind
                if f"{declared_kind}:{name}" not in descriptor.declared_capabilities:
                    raise ValueError("plugin_capability_invalid")
                return kind, name
        raise ValueError("plugin_capability_invalid")

    @classmethod
    def _discovery_authority(
        cls,
        *,
        plugin_id: str,
        descriptor: McpConnectionDescriptor,
        account_key_sha256: str,
        package_revision: str,
        connection_revision: str,
        authority_revision: str,
    ) -> PluginExecutionAuthority:
        capability_kind, capability_id = cls._discovery_anchor(descriptor)
        return PluginExecutionAuthority(
            plugin_id=plugin_id,
            app_id=descriptor.app_id,
            account_key_sha256=account_key_sha256,
            package_revision=package_revision,
            connection_revision=connection_revision,
            authority_revision=authority_revision,
            capability_revision=descriptor.capability_revision,
            capability_kind=capability_kind,
            capability_id=capability_id,
        )

    @staticmethod
    def _validate_worker_discovery(
        descriptor: McpConnectionDescriptor,
        result: object,
    ) -> _WorkerDiscovery:
        expected_fields = {
            "app_id",
            "publisher_id",
            "deployment",
            "transport",
            "protocol_version",
            "source",
            "server_id",
            "server_version",
            "tools",
            "prompts",
            "resources",
            "resource_templates",
            "extensions",
            "cache_metadata",
            "capability_revision",
            "signed_capability_authority",
        }
        signed = descriptor.signed_capability_authority
        try:
            if not isinstance(result, Mapping) or set(result) != {"discovery"}:
                raise ValueError
            receipt = result["discovery"]
            if type(receipt) is not dict or set(receipt) != expected_fields:
                raise ValueError
            if (
                receipt["app_id"] != descriptor.app_id
                or receipt["publisher_id"] != descriptor.publisher_id
                or receipt["deployment"] != descriptor.deployment.value
                or receipt["transport"] != descriptor.transport.value
                or receipt["protocol_version"] != descriptor.protocol_version
                or not _receipt_revision_admissible(receipt, descriptor)
                or receipt["signed_capability_authority"] != _plain_json(signed)
                or receipt["source"]
                != (
                    "live_observed"
                    if descriptor.deployment is McpClientDeployment.LOCAL
                    else "live_authenticated"
                )
                or type(receipt["server_id"]) is not str
                or not receipt["server_id"]
                or type(receipt["server_version"]) is not str
                or not receipt["server_version"]
                or not isinstance(signed, Mapping)
            ):
                raise ValueError
            for receipt_group, signed_group in (
                ("tools", "tools"),
                ("resources", "resources"),
                ("resource_templates", "resource_templates"),
                ("prompts", "prompts"),
            ):
                observed = receipt[receipt_group]
                policies = signed.get(signed_group)
                if (
                    type(observed) is not dict
                    or not isinstance(policies, Mapping)
                    # A name outside the signed authority is surface nobody
                    # reviewed, and stays refused in every group.
                    or set(observed) - set(policies)
                    # Fewer is the operator's choice only for TOOLS, because a
                    # consent checklist is a tool checklist. A prompt or
                    # resource that has stopped being served is the server
                    # changing under a signed package.
                    or (receipt_group != "tools" and set(policies) - set(observed))
                    # And granting no tools at all is not a grant: it is a
                    # server that has not finished warming, or nothing ticked.
                    # Either way there is no connection worth making.
                    or (receipt_group == "tools" and policies and not observed)
                ):
                    raise ValueError
            if any(
                type(receipt[field]) is not dict
                for field in ("extensions", "cache_metadata")
            ):
                raise ValueError
            canonical_bytes(receipt)
        except (KeyError, TypeError, UnicodeError, ValueError):
            raise ValueError("plugin_capability_invalid") from None
        return _WorkerDiscovery(
            tools=PluginConnectionService._signed_local_tools(descriptor),
            server_id=str(receipt["server_id"]),
            server_version=str(receipt["server_version"]),
        )

    async def _discover_in_worker(
        self,
        *,
        authority: PluginExecutionAuthority,
        descriptor: McpConnectionDescriptor,
        credential: Mapping[str, object],
        local_execution: _LocalMcpWorkerExecution | None = None,
    ) -> _WorkerDiscovery:
        worker = self._execution_worker
        if worker is None:
            raise ValueError("plugin_worker_unavailable")
        body: dict[str, object] = {
            "descriptor": descriptor.to_dict(),
            "discovery": True,
        }
        if local_execution is not None:
            body["local_execution"] = local_execution.to_dict()
        timeout_seconds = max(
            1.0,
            (
                descriptor.limits.max_connection_duration_ms
                + descriptor.limits.max_shutdown_duration_ms
            )
            / 1_000,
        )
        try:
            result = await worker.run_async(
                authority=authority,
                body=body,
                credential=dict(credential),
                timeout_seconds=timeout_seconds,
            )
        except PluginExecutionWorkerError as exc:
            reason = str(exc)
            _emit_discovery_failure(
                plugin_id=authority.plugin_id,
                app_id=authority.app_id,
                reason=reason,
            )
            if reason in {
                "plugin_worker_reap_failed",
                "plugin_worker_reap_pending",
            }:
                raise
            raise ValueError(reason) from None
        if isinstance(result, Mapping) and set(result) == {"discovery_error"}:
            error = result["discovery_error"]
            if isinstance(error, Mapping) and set(error) == {"reason", "phase"}:
                try:
                    reason = McpClientErrorReason(error["reason"])
                except (TypeError, ValueError):
                    pass
                else:
                    if error["phase"] == "discovery":
                        _emit_discovery_failure(
                            plugin_id=authority.plugin_id,
                            app_id=authority.app_id,
                            reason=reason.value,
                        )
                        raise _ProviderDiscoveryError(reason)
            _emit_discovery_failure(
                plugin_id=authority.plugin_id,
                app_id=authority.app_id,
                reason="plugin_capability_invalid",
            )
            raise ValueError("plugin_capability_invalid")
        try:
            return self._validate_worker_discovery(descriptor, result)
        except Exception as exc:
            _emit_discovery_failure(
                plugin_id=authority.plugin_id,
                app_id=authority.app_id,
                reason=str(exc),
            )
            raise

    def _local_worker_material(
        self,
        *,
        plugin_id: str,
        record: InstalledMcpAppRecord,
        descriptor: McpConnectionDescriptor,
        connection_authority: PluginConnectionAuthority,
    ) -> tuple[
        McpConnectionDescriptor,
        _LocalMcpWorkerExecution,
        str,
        str,
        str,
        tuple[str, ...],
        dict[str, str],
    ]:
        """Reverify exact local launch pointers before sealing a worker envelope."""

        package_registry = self._package_registry
        if package_registry is None:
            raise ValueError("plugin_local_connection_unavailable")
        try:
            requirement = self._verified_requirement(plugin_id, record)
            observed_plugin = self._plugin_registry.verified_installed_snapshot(plugin_id)
            plugin_record = observed_plugin.record
            verified_plugin = observed_plugin.verified
            plugin_revision = observed_plugin.revision
            app_snapshot = self._app_registry.snapshot()
            package_snapshot = package_registry.snapshot()
            app_records = tuple(
                item for item in app_snapshot.records if item.app_id == record.app_id
            )
            package_records = tuple(
                item
                for item in package_snapshot.records
                if item.server_id == record.app_id
            )
            if len(app_records) != 1 or len(package_records) != 1:
                raise ValueError
            package_record = package_records[0]
            verified_package = package_registry.reverify_installed(record.app_id)
            signed_authority = _verified_plugin_authority(
                verified_plugin,
                record.app_id,
            )
            if signed_authority is None:
                raise ValueError
            execution_descriptor = replace(
                descriptor,
                verified_launch_sha256=verified_package.manifest.artifact_sha256,
            )
            expected_descriptor = replace(
                descriptor_for_record(
                    app_records[0],
                    signed_capability_authority=signed_authority,
                ),
                verified_launch_sha256=verified_package.manifest.artifact_sha256,
            )
            plugin_root = Path(self._plugin_registry.state_root).resolve(strict=True)
            app_root = Path(self._app_registry.state_root).resolve(strict=True)
            package_state_root = Path(package_registry.state_root).resolve(strict=True)
            plugin_package_root = verified_plugin.package_root.resolve(strict=True)
            mcp_package_root = package_registry.package_path(package_record).resolve(
                strict=True
            )
            executable_path = verified_package.artifact_path.resolve(strict=True)
            if (
                type(plugin_record) is not InstalledPluginRecord
                or type(verified_plugin) is not VerifiedPlugin
                or type(package_record) is not InstalledPackageRecord
                or type(verified_package) is not VerifiedPackage
                or not _is_verifier_issued_package(verified_package)
                or requirement.authentication.kind is not PluginAuthenticationKind.NONE
                or requirement.deployment.value != "local"
                or plugin_record.status != "installed"
                or plugin_record.blocker is not None
                or not plugin_record.enabled
                or execution_descriptor != expected_descriptor
                or record != app_records[0]
                or record.local_package_version != package_record.server_version
                or record.local_package_platform != package_record.platform
                or record.local_package_manifest_sha256
                != package_record.manifest_digest
                or not package_record.enabled
                or package_record.blocker is not None
                or verified_package.manifest_sha256 != package_record.manifest_digest
                or verified_package.manifest.server_id != record.app_id
                or verified_package.manifest.publisher_id != requirement.publisher_id
                or verified_package.manifest.artifact_sha256
                != package_record.artifact_sha256
                or verified_package.tool_catalog_path.parent.resolve(strict=True)
                != mcp_package_root
                or app_root != package_state_root
                or any(
                    type(value) is not int or value <= 0
                    for value in verified_package.artifact_identity
                )
            ):
                raise ValueError
            durable = self._credentialless_authority_is_current(
                connection_authority,
                plugin_id=plugin_id,
                descriptor=execution_descriptor,
            )
            if durable is None:
                raise ValueError
            _context_revision, binding_digests = durable
            capability_ceiling = connection_authority.capability_ceiling
            allowed_tool_schemas = dict(connection_authority.allowed_tool_schemas)
            local_execution = _LocalMcpWorkerExecution(
                plugin_registry_root=str(plugin_root),
                plugin_registry_revision=plugin_revision,
                plugin_package_root=str(plugin_package_root),
                mcp_state_root=str(package_state_root),
                app_registry_revision=app_snapshot.revision,
                package_registry_revision=package_snapshot.revision,
                mcp_package_root=str(mcp_package_root),
                mcp_package_manifest_sha256=verified_package.manifest_sha256,
                executable_path=str(executable_path),
                executable_sha256=verified_package.manifest.artifact_sha256,
                executable_identity=verified_package.artifact_identity,
            )
            if (
                self._plugin_registry.snapshot().revision != plugin_revision
                or self._app_registry.snapshot().revision != app_snapshot.revision
                or package_registry.snapshot().revision != package_snapshot.revision
            ):
                raise ValueError
        except ValueError as exc:
            if str(exc) == "plugin_local_connection_unavailable":
                raise
            raise ValueError("plugin_connection_not_live") from None
        except Exception:
            raise ValueError("plugin_connection_not_live") from None
        return (
            execution_descriptor,
            local_execution,
            verified_plugin.package_sha256,
            connection_authority.connection_revision,
            connection_authority.revision,
            capability_ceiling,
            allowed_tool_schemas,
        )

    def _candidate_local_worker_material(
        self,
        *,
        plugin_id: str,
        record: InstalledMcpAppRecord,
        descriptor: McpConnectionDescriptor,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        expected_registry_revision: str,
    ) -> tuple[McpConnectionDescriptor, _LocalMcpWorkerExecution]:
        """Seal one exact published candidate for child-side re-verification."""

        package_registry = self._package_registry
        if package_registry is None:
            raise ValueError("plugin_update_failed")
        try:
            plugin_snapshot = self._plugin_registry.snapshot()
            plugin_records = tuple(
                item
                for item in plugin_snapshot.records
                if item.plugin_id == plugin_id
            )
            if (
                plugin_snapshot.revision != expected_registry_revision
                or len(plugin_records) != 1
            ):
                raise ValueError
            plugin_record = plugin_records[0]
            candidate_records = tuple(
                item
                for item in plugin_record.versions
                if item.state == "candidate"
                and item.plugin_version == candidate_verified.manifest.plugin_version
            )
            if len(candidate_records) != 1:
                raise ValueError
            reverified = self._plugin_registry.reverify_candidate(
                plugin_id,
                candidate_records[0],
            )
            if (
                reverified.manifest != candidate_verified.manifest
                or reverified.manifest_sha256
                != candidate_verified.manifest_sha256
                or reverified.package_sha256 != candidate_verified.package_sha256
            ):
                raise ValueError
            requirements = tuple(
                item
                for item in (
                    reverified.manifest.required_apps
                    + reverified.manifest.optional_apps
                )
                if item.app_id == record.app_id
            )
            app_snapshot = self._app_registry.snapshot()
            app_records = tuple(
                item for item in app_snapshot.records if item.app_id == record.app_id
            )
            package_snapshot = package_registry.snapshot()
            package_records = tuple(
                item
                for item in package_snapshot.records
                if item.server_id == record.app_id
            )
            if (
                len(requirements) != 1
                or len(app_records) != 1
                or len(package_records) != 1
            ):
                raise ValueError
            requirement = requirements[0]
            package_record = package_records[0]
            verified_package = package_registry.reverify_installed(record.app_id)
            signed_authority = _verified_plugin_authority(
                reverified,
                record.app_id,
            )
            if signed_authority is None:
                raise ValueError
            execution_descriptor = replace(
                descriptor,
                verified_launch_sha256=verified_package.manifest.artifact_sha256,
            )
            expected_descriptor = replace(
                descriptor_for_record(
                    app_records[0],
                    signed_capability_authority=signed_authority,
                ),
                verified_launch_sha256=verified_package.manifest.artifact_sha256,
            )
            plugin_root = Path(self._plugin_registry.state_root).resolve(strict=True)
            app_root = Path(self._app_registry.state_root).resolve(strict=True)
            package_state_root = Path(package_registry.state_root).resolve(strict=True)
            plugin_package_root = reverified.package_root.resolve(strict=True)
            mcp_package_root = package_registry.package_path(package_record).resolve(
                strict=True
            )
            executable_path = verified_package.artifact_path.resolve(strict=True)
            if (
                type(plugin_record) is not InstalledPluginRecord
                or type(reverified) is not VerifiedPlugin
                or type(package_record) is not InstalledPackageRecord
                or type(verified_package) is not VerifiedPackage
                or not _is_verifier_issued_package(verified_package)
                or requirement.authentication.kind is not PluginAuthenticationKind.NONE
                or requirement.deployment.value != "local"
                or plugin_record.status != "installed"
                or plugin_record.blocker is not None
                or not plugin_record.enabled
                or type(candidate_binding) is not PluginAppBinding
                or record.app_id
                not in (
                    candidate_binding.required_app_ids
                    + candidate_binding.optional_app_ids
                )
                or execution_descriptor != expected_descriptor
                or record != app_records[0]
                or record.local_package_version != package_record.server_version
                or record.local_package_platform != package_record.platform
                or record.local_package_manifest_sha256
                != package_record.manifest_digest
                or not package_record.enabled
                or package_record.blocker is not None
                or verified_package.manifest_sha256 != package_record.manifest_digest
                or verified_package.manifest.server_id != record.app_id
                or verified_package.manifest.publisher_id != requirement.publisher_id
                or verified_package.manifest.artifact_sha256
                != package_record.artifact_sha256
                or app_root != package_state_root
                or any(
                    type(value) is not int or value <= 0
                    for value in verified_package.artifact_identity
                )
            ):
                raise ValueError
            execution = _LocalMcpWorkerExecution(
                plugin_registry_root=str(plugin_root),
                plugin_registry_revision=plugin_snapshot.revision,
                plugin_package_root=str(plugin_package_root),
                mcp_state_root=str(package_state_root),
                app_registry_revision=app_snapshot.revision,
                package_registry_revision=package_snapshot.revision,
                mcp_package_root=str(mcp_package_root),
                mcp_package_manifest_sha256=verified_package.manifest_sha256,
                executable_path=str(executable_path),
                executable_sha256=verified_package.manifest.artifact_sha256,
                executable_identity=verified_package.artifact_identity,
            )
            if (
                self._plugin_registry.snapshot().revision
                != expected_registry_revision
                or self._app_registry.snapshot().revision != app_snapshot.revision
                or package_registry.snapshot().revision != package_snapshot.revision
            ):
                raise ValueError
            return execution_descriptor, execution
        except Exception:
            raise ValueError("plugin_update_failed") from None

    def _settings(
        self,
        plugin_id: str,
        *,
        descriptor: McpConnectionDescriptor,
        scopes: tuple[str, ...] | None,
        capability_ceiling: tuple[str, ...] | None,
        allowed_tool_schemas: Mapping[str, str] | None,
        execution_policy: PluginExecutionPolicy | str | None,
        source_constraints: Mapping[str, object] | None,
        destination_constraints: Mapping[str, object] | None,
        oauth: bool,
    ) -> _ConnectionSettings:
        # A local or unauthenticated app has no OAuth: it offers no provider scopes,
        # and its connection never consults the OAuth offer.
        offered = (
            _stable_scopes(self._offered_scopes_provider(plugin_id, descriptor.app_id))
            if oauth
            else ()
        )
        if scopes is None:
            raise ValueError("plugin_scopes_invalid")
        selected = _stable_scopes(scopes)
        if not set(selected).issubset(offered):
            raise ValueError("plugin_scopes_invalid")
        declared = {
            capability: digest
            for capability, digest in descriptor.declared_capabilities.items()
            if capability.startswith("tool:") and type(digest) is str
        }
        if (
            type(capability_ceiling) is not tuple
            or len(capability_ceiling) != len(set(capability_ceiling))
            or any(type(item) is not str or item not in declared for item in capability_ceiling)
            or not isinstance(allowed_tool_schemas, Mapping)
        ):
            raise ValueError("plugin_capability_scope_invalid")
        selected_capabilities = tuple(sorted(capability_ceiling))
        selected_schemas = {
            capability.removeprefix("tool:"): declared[capability]
            for capability in selected_capabilities
        }
        if dict(allowed_tool_schemas) != selected_schemas:
            raise ValueError("plugin_capability_scope_invalid")
        default_policy = (
            PluginExecutionPolicy.CONFIRM_EACH
            if self._execution_policy_provider is None
            else PluginExecutionPolicy(self._execution_policy_provider(plugin_id))
        )
        policy = (
            default_policy if execution_policy is None else PluginExecutionPolicy(execution_policy)
        )
        try:
            source = json.loads(canonical_bytes(dict(source_constraints or {})).decode("utf-8"))
            destination = json.loads(
                canonical_bytes(dict(destination_constraints or {})).decode("utf-8")
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_connection_settings_invalid") from None
        return _ConnectionSettings(
            scopes=selected,
            capability_ceiling=selected_capabilities,
            allowed_tool_schemas=selected_schemas,
            execution_policy=policy,
            source_constraints=source,
            destination_constraints=destination,
        )

    @staticmethod
    def _context_revision(
        descriptor: McpConnectionDescriptor,
        *,
        account_key_sha256: str,
        settings: _ConnectionSettings,
        package_revision: str,
    ) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "descriptor": descriptor.to_dict(),
                    "account_key_sha256": account_key_sha256,
                    "scopes": list(settings.scopes),
                    "capability_ceiling": list(settings.capability_ceiling),
                    "allowed_tool_schemas": dict(settings.allowed_tool_schemas),
                    "execution_policy": settings.execution_policy.value,
                    "source_constraints": settings.source_constraints,
                    "destination_constraints": settings.destination_constraints,
                    "package_revision": package_revision,
                }
            )
        )

    @staticmethod
    def _attempt_context(
        *,
        plugin_id: str,
        app_id: str,
        account_key_sha256: str,
        settings: _ConnectionSettings,
        authorization_context_revision: str,
        descriptor_revision: str,
        package_revision: str,
        registry_revision: str,
        authorization_flow: OAuthAuthorizationFlow,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "plugin_id": plugin_id,
            "app_id": app_id,
            "account_key_sha256": account_key_sha256,
            "scopes": list(settings.scopes),
            "capability_ceiling": list(settings.capability_ceiling),
            "allowed_tool_schemas": dict(settings.allowed_tool_schemas),
            "execution_policy": settings.execution_policy.value,
            "source_constraints": dict(settings.source_constraints),
            "destination_constraints": dict(settings.destination_constraints),
            "authorization_context_revision": authorization_context_revision,
            "descriptor_revision": descriptor_revision,
            "package_revision": package_revision,
            "registry_revision": registry_revision,
            "authorization_flow": authorization_flow.value,
        }

    @staticmethod
    def _prepared_settings(
        prepared: OAuthPreparedAuthorization,
    ) -> tuple[_ConnectionSettings, str, str, str, OAuthAuthorizationFlow]:
        context = dict(prepared.attempt_context)
        expected = {
            "schema_version",
            "plugin_id",
            "app_id",
            "account_key_sha256",
            "scopes",
            "capability_ceiling",
            "allowed_tool_schemas",
            "execution_policy",
            "source_constraints",
            "destination_constraints",
            "authorization_context_revision",
            "descriptor_revision",
            "package_revision",
            "registry_revision",
            "authorization_flow",
            "omega_authorization_context_revision",
        }
        try:
            if set(context) != expected or context["schema_version"] != 1:
                raise ValueError
            scopes = tuple(context["scopes"])
            settings = _ConnectionSettings(
                scopes=_stable_scopes(scopes),
                capability_ceiling=tuple(context["capability_ceiling"]),
                allowed_tool_schemas=json.loads(
                    canonical_bytes(context["allowed_tool_schemas"]).decode("utf-8")
                ),
                execution_policy=PluginExecutionPolicy(context["execution_policy"]),
                source_constraints=json.loads(
                    canonical_bytes(context["source_constraints"]).decode("utf-8")
                ),
                destination_constraints=json.loads(
                    canonical_bytes(context["destination_constraints"]).decode("utf-8")
                ),
            )
            if (
                context["plugin_id"] != prepared.plugin_id
                or context["app_id"] != prepared.app_id
                or context["account_key_sha256"] != prepared.account_key_sha256
                or settings.scopes != prepared.scopes
                or len(settings.capability_ceiling) != len(set(settings.capability_ceiling))
                or set(settings.allowed_tool_schemas)
                != {capability.removeprefix("tool:") for capability in settings.capability_ceiling}
                or context["authorization_context_revision"]
                != prepared.authorization_context_revision
                or context["omega_authorization_context_revision"]
                != prepared.authorization_context_revision
            ):
                raise ValueError
            descriptor_revision = str(context["descriptor_revision"])
            package_revision = str(context["package_revision"])
            registry_revision = str(context["registry_revision"])
            authorization_flow = OAuthAuthorizationFlow(context["authorization_flow"])
            for revision in (descriptor_revision, package_revision, registry_revision):
                if len(revision) != 64 or any(
                    character not in "0123456789abcdef" for character in revision
                ):
                    raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("plugin_connection_drift") from None
        return (
            settings,
            descriptor_revision,
            package_revision,
            registry_revision,
            authorization_flow,
        )

    def _candidate_prepared_settings(
        self,
        prepared: OAuthPreparedAuthorization,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        *,
        expected_registry_revision: str,
    ) -> tuple[_ConnectionSettings, str, OAuthAuthorizationFlow]:
        context = dict(prepared.attempt_context)
        candidate_identity = context.pop("candidate_identity", None)
        context["schema_version"] = 1
        try:
            normalized = OAuthPreparedAuthorization(
                profile_id=prepared.profile_id,
                plugin_id=prepared.plugin_id,
                app_id=prepared.app_id,
                account_key_sha256=prepared.account_key_sha256,
                state_sha256=prepared.state_sha256,
                partition_key=prepared.partition_key,
                profile_revision=prepared.profile_revision,
                authorization_context_revision=prepared.authorization_context_revision,
                scopes=prepared.scopes,
                attempt_context=context,
            )
            (
                settings,
                descriptor_revision,
                package_revision,
                registry_revision,
                authorization_flow,
            ) = self._prepared_settings(normalized)
            if (
                prepared.attempt_context.get("schema_version") != 2
                or candidate_identity
                != self._candidate_identity(candidate_verified, candidate_binding)
                or package_revision != candidate_verified.package_sha256
                or registry_revision != expected_registry_revision
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_connection_drift") from None
        return settings, descriptor_revision, authorization_flow

    def _signed_oauth_flow(self, plugin_id: str, app_id: str) -> OAuthAuthorizationFlow:
        try:
            verified = self._plugin_registry.reverify_installed(plugin_id)
            manifest = verified.manifest
            matches = tuple(
                requirement
                for requirement in (*manifest.required_apps, *manifest.optional_apps)
                if requirement.app_id == app_id
            )
            if len(matches) != 1:
                raise ValueError
            flow = _OAUTH_FLOW_BY_AUTHENTICATION[matches[0].authentication.kind]
            return OAuthAuthorizationFlow(flow)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError("oauth_authorization_flow_mismatch") from None

    def _validate_attempt_flow(
        self,
        *,
        plugin_id: str,
        app_id: str,
        attempt_context: Mapping[str, object],
    ) -> OAuthAuthorizationFlow:
        try:
            flow = OAuthAuthorizationFlow(attempt_context["authorization_flow"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("oauth_authorization_flow_mismatch") from None
        if flow is not self._signed_oauth_flow(plugin_id, app_id):
            raise ValueError("oauth_authorization_flow_mismatch")
        return flow

    @staticmethod
    def _connection_revision(
        descriptor: McpConnectionDescriptor,
        identity: OAuthPreparedAuthorization | PluginGrantActivation,
    ) -> str:
        partition = (
            identity.partition_key
            if type(identity) is OAuthPreparedAuthorization
            else identity.token_partition_key
        )
        return sha256_hex(
            canonical_bytes(
                {
                    "descriptor": descriptor.to_dict(),
                    "plugin_id": identity.plugin_id,
                    "account_key_sha256": identity.account_key_sha256,
                    "profile_id": identity.profile_id,
                    "profile_revision": identity.profile_revision,
                    "token_partition_key": partition,
                    "scopes": list(identity.scopes),
                    "authorization_context_revision": (identity.authorization_context_revision),
                }
            )
        )

    def _current_transport_binding(
        self,
        binding: BrokerTransportBinding,
    ) -> BrokerTransportBinding:
        expectation = self._probe_bindings.get(binding) or self._launch_bindings.get(binding)
        if expectation is not None:
            self._validate_binding_expectation(binding, expectation)
            return binding
        self._installed(binding.plugin_id, binding.app_id)
        descriptor = self._descriptor(binding.plugin_id, binding.app_id)
        try:
            committed = self._broker._validate_transport_grant(binding)
        except ValueError:
            raise ValueError("plugin_connection_not_live") from None
        grant = self._grants.current(
            binding.plugin_id,
            app_id=binding.app_id,
            account_key_sha256=committed.account_key_sha256,
        )
        if (
            not self._durable_grant_is_current(grant, descriptor)
            or grant is None
            or self._grant_binding(grant, descriptor) != binding
        ):
            raise ValueError("plugin_connection_not_live")
        return binding

    def _validate_binding_expectation(
        self,
        binding: BrokerTransportBinding,
        expectation: _BindingExpectation,
    ) -> None:
        identity = expectation.identity
        if expectation.candidate_verified is not None:
            if (
                type(identity) is not OAuthPreparedAuthorization
                or type(expectation.candidate_binding) is not PluginAppBinding
                or type(expectation.registry_revision) is not str
            ):
                raise ValueError("plugin_connection_drift")
            _current, _record, _requirement, descriptor = self._candidate_definition(
                binding.plugin_id,
                binding.app_id,
                candidate_verified=expectation.candidate_verified,
                candidate_binding=expectation.candidate_binding,
                expected_registry_revision=expectation.registry_revision,
            )
            if (
                identity.plugin_id != binding.plugin_id
                or identity.app_id != binding.app_id
                or identity.profile_id != binding.profile_id
                or identity.profile_revision != binding.profile_revision
                or identity.partition_key != binding.partition_key
                or self._probe_binding(identity, descriptor) != binding
                or self._descriptor_revision(descriptor) != expectation.descriptor_revision
                or expectation.candidate_verified.package_sha256
                != expectation.package_revision
            ):
                raise ValueError("plugin_connection_drift")
            self._candidate_prepared_settings(
                identity,
                expectation.candidate_verified,
                expectation.candidate_binding,
                expected_registry_revision=expectation.registry_revision,
            )
            return
        self._installed(binding.plugin_id, binding.app_id)
        descriptor = self._descriptor(binding.plugin_id, binding.app_id)
        if (
            self._descriptor_revision(descriptor) != expectation.descriptor_revision
            or self._package_revision(binding.plugin_id) != expectation.package_revision
            or str(descriptor.remote_origin) != binding.resource_url
        ):
            raise ValueError("plugin_connection_drift")
        if type(identity) is OAuthPreparedAuthorization:
            if (
                identity.plugin_id != binding.plugin_id
                or identity.app_id != binding.app_id
                or identity.profile_id != binding.profile_id
                or identity.profile_revision != binding.profile_revision
                or identity.partition_key != binding.partition_key
                or self._probe_binding(identity, descriptor) != binding
            ):
                raise ValueError("plugin_connection_drift")
            _settings, descriptor_revision, package_revision, _registry_revision, _flow = (
                self._prepared_settings(identity)
            )
            self._require_registry_revision(binding.plugin_id, _registry_revision)
            if (
                descriptor_revision != expectation.descriptor_revision
                or package_revision != expectation.package_revision
            ):
                raise ValueError("plugin_connection_drift")
            return
        grant = self._grants.current(
            binding.plugin_id,
            app_id=binding.app_id,
            account_key_sha256=identity.activation.account_key_sha256,
        )
        if grant != identity or self._grant_binding(identity, descriptor) != binding:
            raise ValueError("plugin_connection_drift")

    def _probe_binding(
        self,
        prepared: OAuthPreparedAuthorization,
        descriptor: McpConnectionDescriptor,
    ) -> BrokerTransportBinding:
        material = {
            "state_sha256": prepared.state_sha256,
            "descriptor": descriptor.to_dict(),
            "kind": "prepared_probe",
        }
        return BrokerTransportBinding(
            plugin_id=prepared.plugin_id,
            app_id=prepared.app_id,
            profile_id=prepared.profile_id,
            profile_revision=prepared.profile_revision,
            partition_key=prepared.partition_key,
            resource_url=str(descriptor.remote_origin),
            connection_revision=self._connection_revision(descriptor, prepared),
            capability_revision=descriptor.capability_revision,
            authority_revision=sha256_hex(canonical_bytes({**material, "revision": "authority"})),
            capability_binding_revision=sha256_hex(
                canonical_bytes({**material, "revision": "capabilities"})
            ),
        )

    @staticmethod
    def _grant_binding(
        grant: PluginGrantRecord,
        descriptor: McpConnectionDescriptor,
    ) -> BrokerTransportBinding:
        activation = grant.activation
        return BrokerTransportBinding(
            plugin_id=activation.plugin_id,
            app_id=activation.app_id,
            profile_id=activation.profile_id,
            profile_revision=activation.profile_revision,
            partition_key=activation.token_partition_key,
            resource_url=str(descriptor.remote_origin),
            connection_revision=activation.connection_revision,
            capability_revision=activation.capability_revision,
            authority_revision=activation.authority_revision,
            capability_binding_revision=activation.capability_binding_revision,
        )

    def _durable_grant_is_current(
        self,
        grant: PluginGrantRecord | None,
        descriptor: McpConnectionDescriptor,
    ) -> bool:
        """Validate a committed grant from durable signed evidence only."""

        if grant is None:
            return False
        try:
            plugin_connection_binding_revision(
                grant.authority,
                capability_revision=grant.activation.capability_revision,
                capability_binding_revision=grant.activation.capability_binding_revision,
                capability_binding_identities=grant.activation.capability_binding_identities,
            )
        except ValueError:
            return False
        return (
            bool(grant.activation.capability_binding_identities)
            and grant.activation.capability_revision == descriptor.capability_revision
            and grant.activation.descriptor_revision == self._descriptor_revision(descriptor)
            and grant.activation.package_revision
            == self._package_revision(grant.activation.plugin_id)
            and self._connection_revision(descriptor, grant.activation)
            == grant.activation.connection_revision
        )

    def _active_grant_is_current(
        self,
        active: _ActiveConnection | None,
        grant: PluginGrantRecord | None,
        descriptor: McpConnectionDescriptor,
    ) -> bool:
        if active is None or grant is None:
            return False
        return (
            self._durable_grant_is_current(grant, descriptor)
            and active.grant == grant
            and bool(active.capability_binding_identities)
            and active.capability_binding_identities == grant.activation.capability_binding_identities
            and active.capability_revision == descriptor.capability_revision
            and active.descriptor_revision == self._descriptor_revision(descriptor)
            and active.package_revision == grant.activation.package_revision
            and active.authorization_context_revision
            == grant.activation.authorization_context_revision
        )

    async def snapshot_plugin_tools(
        self,
        client: CurrentMcpClient,
        descriptor: McpConnectionDescriptor,
    ) -> tuple[types.Tool, ...]:
        request = McpOperationRequest(
            request_id=uuid4().hex,
            app_id=descriptor.app_id,
            operation=McpOperationKind.TOOLS_LIST,
            capability_id="tools/list",
            arguments={},
            capability_revision=descriptor.capability_revision,
            deadline_monotonic=(
                asyncio.get_running_loop().time()
                + descriptor.limits.max_operation_duration_ms / 1_000
            ),
            cancellation_id=uuid4().hex,
            task_id=None,
            input_request_id=None,
            request_state=None,
        )
        result = await client.snapshot_plugin_tools(request)
        if result.status is not McpOperationStatus.COMPLETED:
            raise ValueError("plugin_transport_unavailable")
        raw_tools = result.data.get("tools") if isinstance(result.data, Mapping) else None
        if (
            not isinstance(raw_tools, (list, tuple))
            or len(raw_tools) > descriptor.limits.max_list_items
        ):
            raise ValueError("plugin_capability_invalid")
        converted: list[types.Tool] = []
        for raw in raw_tools:
            if not isinstance(raw, Mapping):
                raise ValueError("plugin_capability_invalid")
            try:
                converted.append(
                    types.Tool(
                        name=raw["name"],
                        title=raw.get("title"),
                        description=(
                            None
                            if raw.get("description") is None
                            else " ".join(str(raw["description"]).split())[:1024]
                        ),
                        inputSchema=raw["input_schema"],
                        outputSchema=raw.get("output_schema"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                raise ValueError("plugin_capability_invalid") from None
        return tuple(converted)

    @staticmethod
    def _compile_capability_bindings(
        descriptor: McpConnectionDescriptor,
        tools: tuple[types.Tool, ...],
        *,
        capability_ceiling: tuple[str, ...] | None = None,
        allowed_tool_schemas: Mapping[str, str] | None = None,
    ) -> tuple[
        tuple[str, ...],
        dict[str, str],
        tuple[PluginCapabilityBindingIdentity, ...],
    ]:
        if len(tools) > descriptor.limits.max_list_items:
            raise ValueError("plugin_capability_invalid")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("plugin_capability_invalid")
        authority = descriptor.signed_capability_authority
        if not isinstance(authority, Mapping):
            raise ValueError("plugin_capability_invalid")
        groups = {
            kind: authority.get(group)
            for kind, group in (
                ("tool", "tools"),
                ("resource", "resources"),
                ("resource_template", "resource_templates"),
                ("prompt", "prompts"),
            )
        }
        if any(not isinstance(group, Mapping) for group in groups.values()):
            raise ValueError("plugin_capability_invalid")
        policies = groups["tool"]
        declared = {
            kind: {
                key.removeprefix(f"{prefix}:"): digest
                for key, digest in descriptor.declared_capabilities.items()
                if key.startswith(f"{prefix}:")
            }
            for kind, prefix in (
                ("tool", "tool"),
                ("resource", "resource"),
                ("prompt", "prompt"),
            )
        }
        schemas: dict[str, str] = {}
        binding_digests: dict[str, list[str]] = {
            kind: [] for kind in groups
        }
        for tool in tools:
            capability = f"tool:{tool.name}"
            if capability_ceiling is not None and capability not in capability_ceiling:
                continue
            policy = policies.get(tool.name)
            if not isinstance(policy, Mapping) or tool.name not in declared["tool"]:
                continue
            schema = _plain_json(tool.input_schema)
            if not isinstance(schema, Mapping):
                continue
            schema_digest = sha256_hex(canonical_bytes(schema))
            if (
                declared["tool"][tool.name] != schema_digest
                or policy.get("input_schema_sha256") != schema_digest
                or _plain_json(policy.get("input_schema")) != schema
            ):
                continue
            schemas[tool.name] = schema_digest
            binding_digests["tool"].append(
                sha256_hex(
                    canonical_bytes(
                        {
                            "name": tool.name,
                            "schema": schema_digest,
                            "policy": _plain_json(policy),
                        }
                    )
                )
            )
        compiled_ceiling = tuple(f"tool:{name}" for name in sorted(schemas))
        if (
            capability_ceiling is not None and compiled_ceiling != tuple(sorted(capability_ceiling))
        ) or (allowed_tool_schemas is not None and schemas != dict(allowed_tool_schemas)):
            raise ValueError("plugin_capability_invalid")
        resource_names = set(groups["resource"]) | set(groups["resource_template"])
        if (
            set(groups["resource"]) & set(groups["resource_template"])
            or resource_names != set(declared["resource"])
            or set(groups["prompt"]) != set(declared["prompt"])
        ):
            raise ValueError("plugin_capability_invalid")
        for kind in ("resource", "resource_template", "prompt"):
            for name in sorted(groups[kind]):
                policy = groups[kind][name]
                if not isinstance(policy, Mapping):
                    raise ValueError("plugin_capability_invalid")
                if kind == "prompt" and declared["prompt"].get(name) != policy.get(
                    "schema_sha256"
                ):
                    raise ValueError("plugin_capability_invalid")
                binding_digests[kind].append(
                    sha256_hex(
                        canonical_bytes(
                            {"name": name, "policy": _plain_json(policy)}
                        )
                    )
                )
        identities = tuple(
            PluginCapabilityBindingIdentity(
                capability_kind=kind,
                binding_sha256=sha256_hex(
                    canonical_bytes(
                        {
                            f"{kind}_binding_digests": sorted(
                                binding_digests[kind]
                            )
                        }
                    )
                ),
            )
            for kind in sorted(binding_digests)
            if binding_digests[kind]
        )
        if not identities:
            raise ValueError("plugin_capability_invalid")
        return (
            compiled_ceiling,
            schemas,
            identities,
        )

    def _compile_activation(
        self,
        prepared: OAuthPreparedAuthorization,
        descriptor: McpConnectionDescriptor,
        tools: tuple[types.Tool, ...],
        settings: _ConnectionSettings,
        *,
        descriptor_revision: str,
        package_revision: str,
    ) -> PluginGrantActivation:
        ceiling, schemas, binding_digests = self._compile_capability_bindings(
            descriptor,
            tools,
            capability_ceiling=settings.capability_ceiling,
            allowed_tool_schemas=settings.allowed_tool_schemas,
        )
        return PluginGrantActivation(
            plugin_id=prepared.plugin_id,
            app_id=prepared.app_id,
            account_key_sha256=prepared.account_key_sha256,
            profile_id=prepared.profile_id,
            profile_revision=prepared.profile_revision,
            token_partition_key=prepared.partition_key,
            connection_revision=self._connection_revision(descriptor, prepared),
            capability_revision=descriptor.capability_revision,
            scopes=prepared.scopes,
            capability_ceiling=ceiling,
            allowed_tool_schemas=schemas,
            source_constraints=settings.source_constraints,
            destination_constraints=settings.destination_constraints,
            capability_binding_identities=binding_digests,
            execution_policy=settings.execution_policy,
            authorization_context_revision=prepared.authorization_context_revision,
            descriptor_revision=descriptor_revision,
            package_revision=package_revision,
            attempt_state_sha256=prepared.state_sha256,
        )

    def _compile_grant_snapshot(
        self,
        grant: PluginGrantRecord,
        descriptor: McpConnectionDescriptor,
        tools: tuple[types.Tool, ...],
    ) -> PluginGrantActivation:
        activation = grant.activation
        return self._compile_activation(
            OAuthPreparedAuthorization(
                profile_id=activation.profile_id,
                plugin_id=activation.plugin_id,
                app_id=activation.app_id,
                account_key_sha256=activation.account_key_sha256,
                state_sha256=str(activation.attempt_state_sha256),
                partition_key=activation.token_partition_key,
                profile_revision=activation.profile_revision,
                authorization_context_revision=str(activation.authorization_context_revision),
                scopes=activation.scopes,
                attempt_context={
                    "schema_version": 1,
                    "plugin_id": activation.plugin_id,
                    "app_id": activation.app_id,
                    "account_key_sha256": activation.account_key_sha256,
                    "scopes": list(activation.scopes),
                    "capability_ceiling": list(activation.capability_ceiling),
                    "allowed_tool_schemas": dict(activation.allowed_tool_schemas),
                    "execution_policy": activation.execution_policy.value,
                    "source_constraints": dict(activation.source_constraints),
                    "destination_constraints": dict(activation.destination_constraints),
                    "authorization_context_revision": (activation.authorization_context_revision),
                    "descriptor_revision": activation.descriptor_revision,
                    "package_revision": activation.package_revision,
                    "omega_authorization_context_revision": (
                        activation.authorization_context_revision
                    ),
                },
            ),
            descriptor,
            tools,
            _ConnectionSettings(
                scopes=activation.scopes,
                capability_ceiling=activation.capability_ceiling,
                allowed_tool_schemas=activation.allowed_tool_schemas,
                execution_policy=activation.execution_policy,
                source_constraints=activation.source_constraints,
                destination_constraints=activation.destination_constraints,
            ),
            descriptor_revision=str(activation.descriptor_revision),
            package_revision=str(activation.package_revision),
        )

    async def _probe_prepared(
        self,
        prepared: OAuthPreparedAuthorization,
        descriptor: McpConnectionDescriptor,
        *,
        descriptor_revision: str,
        package_revision: str,
    ) -> tuple[types.Tool, ...]:
        binding = self._probe_binding(prepared, descriptor)
        expectation = _BindingExpectation(
            prepared,
            descriptor_revision,
            package_revision,
        )
        self._probe_bindings[binding] = expectation
        if self._execution_worker is not None:
            try:
                self._validate_binding_expectation(binding, expectation)
                token = await self._broker._current_prepared_access_token(
                    prepared,
                    binding,
                )
                return (
                    await self._discover_in_worker(
                    authority=self._discovery_authority(
                        plugin_id=prepared.plugin_id,
                        descriptor=descriptor,
                        account_key_sha256=prepared.account_key_sha256,
                        package_revision=package_revision,
                        connection_revision=binding.connection_revision,
                        authority_revision=binding.authority_revision,
                    ),
                    descriptor=descriptor,
                    credential={"access_token": token},
                    )
                ).tools
            finally:
                self._probe_bindings.pop(binding, None)
        client: CurrentMcpClient | None = None
        try:
            authorization = self._broker.authorize_prepared_transport(
                prepared,
                binding,
                self._transport_authority,
            )
            client = self._client_factory(descriptor, authorization)

            async def run() -> tuple[types.Tool, ...]:
                assert client is not None
                await client.start()
                return await self.snapshot_plugin_tools(client, descriptor)

            tools = await self._submit(run)
            return tools
        finally:
            if client is not None:
                with suppress(BaseException):
                    await self._submit(client.close)
            self._probe_bindings.pop(binding, None)

    async def _start_committed(
        self,
        grant: PluginGrantRecord,
        descriptor: McpConnectionDescriptor,
    ) -> _ActiveConnection:
        activation = grant.activation
        if (
            descriptor.capability_revision != activation.capability_revision
            or self._descriptor_revision(descriptor) != activation.descriptor_revision
            or self._package_revision(activation.plugin_id) != activation.package_revision
            or self._connection_revision(descriptor, activation) != activation.connection_revision
        ):
            raise ValueError("plugin_connection_drift")
        binding = self._grant_binding(grant, descriptor)
        self._launch_bindings[binding] = _BindingExpectation(
            grant,
            str(activation.descriptor_revision),
            str(activation.package_revision),
        )
        if self._execution_worker is not None:
            try:
                expectation = self._launch_bindings[binding]
                self._validate_binding_expectation(binding, expectation)
                worker_authority = self._discovery_authority(
                    plugin_id=activation.plugin_id,
                    descriptor=descriptor,
                    account_key_sha256=activation.account_key_sha256,
                    package_revision=str(activation.package_revision),
                    connection_revision=activation.connection_revision,
                    authority_revision=activation.authority_revision,
                )
                token = await self._broker._issue_execution_worker_access_token(
                    binding,
                    self._transport_authority,
                    execution_worker=self._execution_worker,
                    worker_authority=worker_authority,
                    timeout_seconds=max(
                        1.0,
                        descriptor.limits.max_connection_duration_ms / 1_000,
                    ),
                )
                discovery = await self._discover_in_worker(
                    authority=worker_authority,
                    descriptor=descriptor,
                    credential={"access_token": token},
                )
                compiled = self._compile_grant_snapshot(
                    grant, descriptor, discovery.tools
                )
                if compiled != activation:
                    raise ValueError("plugin_capability_invalid")
                return _ActiveConnection(
                    _WorkerOnlyLocalClient(),
                    discovery.tools,
                    grant,
                    binding,
                    activation.capability_revision,
                    activation.capability_binding_identities,
                    str(activation.authorization_context_revision),
                    str(activation.descriptor_revision),
                    str(activation.package_revision),
                    None,
                    discovery.server_id,
                    discovery.server_version,
                )
            finally:
                self._launch_bindings.pop(binding, None)
        client: CurrentMcpClient | None = None
        try:
            authorization = self._broker.authorize_transport(
                binding,
                self._transport_authority,
            )
            client = self._client_factory(descriptor, authorization)

            async def run() -> tuple[types.Tool, ...]:
                assert client is not None
                await client.start()
                return await self.snapshot_plugin_tools(client, descriptor)

            tools = await self._submit(run)
            compiled = self._compile_grant_snapshot(grant, descriptor, tools)
            if compiled != activation:
                raise ValueError("plugin_capability_invalid")
            return _ActiveConnection(
                client,
                tools,
                grant,
                binding,
                activation.capability_revision,
                activation.capability_binding_identities,
                str(activation.authorization_context_revision),
                str(activation.descriptor_revision),
                str(activation.package_revision),
            )
        except BaseException:
            if client is not None:
                with suppress(BaseException):
                    await self._submit(client.close)
            raise
        finally:
            self._launch_bindings.pop(binding, None)

    async def _evict_session(
        self,
        key: tuple[str, str, str],
        *,
        state: str,
        suspend_grant: bool,
    ) -> None:
        if suspend_grant:
            self._grants.suspend(
                key[0],
                app_id=key[1],
                account_key_sha256=key[2],
            )
        active = self._sessions.pop(key, None)
        if active is not None:
            active.tools = ()
            active.capability_binding_identities = ()
            active.grant = None
            active.binding = None
        self._states[key] = state
        if active is not None:
            with suppress(BaseException):
                await self._submit(active.client.close)

    async def _evict_failed_session(
        self,
        key: tuple[str, str, str],
        exc: BaseException,
        *,
        credentialless: bool,
    ) -> None:
        # Availability is not consent. A failed worker or unreachable provider
        # invalidates this live session, not the operator's durable approval.
        # Retry still has to rediscover and revalidate the exact signed binding;
        # no operation is permitted using stale tools after the failure.
        transient = isinstance(exc, asyncio.CancelledError) or str(exc) in {
            "oauth_provider_unreachable",
            "plugin_transport_unavailable",
            "plugin_operation_timeout",
            "plugin_operation_cancelled",
            "plugin_operation_in_progress",
            "plugin_runtime_closed",
            "plugin_worker_unavailable",
            "plugin_worker_reap_failed",
            "plugin_worker_reap_pending",
        }
        await self._evict_session(
            key,
            state=self._failure_state(exc),
            suspend_grant=not credentialless and not transient,
        )
        if credentialless and not transient:
            self.authority_store.invalidate_connection(*key)

    async def _finish_pending_disconnect_locked(
        self,
        key: tuple[str, str, str],
    ) -> None:
        pending = self._grants.pending_disconnect(
            key[0],
            app_id=key[1],
            account_key_sha256=key[2],
        )
        if pending is not None:
            # A disconnect lets go: a provider that refuses the revocation
            # -- usually because the token already expired -- must not leave
            # the plugin installed with its credential still held.
            await self._grants.finish_disconnect(
                pending,
                tolerate_provider_refusal=True,
            )

    async def _refresh_active(
        self,
        active: _ActiveConnection,
        descriptor: McpConnectionDescriptor,
        grant: PluginGrantRecord | None,
    ) -> None:
        if self._execution_worker is not None:
            local_execution: _LocalMcpWorkerExecution | None = None
            if grant is None:
                durable = active.credentialless_authority
                if durable is None:
                    raise ValueError("plugin_connection_not_live")
                worker_descriptor = descriptor
                package_revision = active.package_revision
                if descriptor.deployment is McpClientDeployment.LOCAL:
                    record = self._installed(durable.plugin_id, durable.app_id)
                    (
                        worker_descriptor,
                        local_execution,
                        package_revision,
                        _connection_revision,
                        _authority_revision,
                        _ceiling,
                        _schemas,
                    ) = self._local_worker_material(
                        plugin_id=durable.plugin_id,
                        record=record,
                        descriptor=descriptor,
                        connection_authority=durable,
                    )
                credential: dict[str, object] = {}
                authority = self._discovery_authority(
                    plugin_id=durable.plugin_id,
                    descriptor=worker_descriptor,
                    account_key_sha256=durable.account_key_hash,
                    package_revision=package_revision,
                    connection_revision=durable.connection_revision,
                    authority_revision=durable.revision,
                )
            else:
                activation = grant.activation
                binding = self._grant_binding(grant, descriptor)
                authority = self._discovery_authority(
                    plugin_id=activation.plugin_id,
                    descriptor=descriptor,
                    account_key_sha256=activation.account_key_sha256,
                    package_revision=str(activation.package_revision),
                    connection_revision=activation.connection_revision,
                    authority_revision=activation.authority_revision,
                )
                token = await self._broker._issue_execution_worker_access_token(
                    binding,
                    self._transport_authority,
                    execution_worker=self._execution_worker,
                    worker_authority=authority,
                    timeout_seconds=max(
                        1.0,
                        descriptor.limits.max_connection_duration_ms / 1_000,
                    ),
                )
                credential = {"access_token": token}
                worker_descriptor = descriptor
            discovery = await self._discover_in_worker(
                authority=authority,
                descriptor=worker_descriptor,
                credential=credential,
                local_execution=local_execution,
            )
            if grant is None:
                _ceiling, _schemas, digests = self._compile_capability_bindings(
                    worker_descriptor,
                    discovery.tools,
                )
                if digests != active.capability_binding_identities:
                    raise ValueError("plugin_capability_invalid")
            elif (
                self._compile_grant_snapshot(grant, descriptor, discovery.tools)
                != grant.activation
            ):
                raise ValueError("plugin_capability_invalid")
            active.tools = discovery.tools
            active.server_id = discovery.server_id
            active.server_version = discovery.server_version
            return
        if type(active.client) is _WorkerOnlyLocalClient:
            if grant is not None or descriptor.deployment is not McpClientDeployment.LOCAL:
                raise ValueError("plugin_connection_not_live")
            _ceiling, _schemas, digests = self._compile_capability_bindings(
                descriptor,
                active.tools,
            )
            if digests != active.capability_binding_identities:
                raise ValueError("plugin_capability_invalid")
            return
        if grant is not None:
            if active.binding is None:
                raise ValueError("plugin_connection_not_live")
            self._broker.authorize_transport(active.binding, self._transport_authority)
        tools = await self._submit(lambda: self.snapshot_plugin_tools(active.client, descriptor))
        if grant is None:
            _ceiling, _schemas, digests = self._compile_capability_bindings(descriptor, tools)
            if digests != active.capability_binding_identities:
                raise ValueError("plugin_capability_invalid")
        elif self._compile_grant_snapshot(grant, descriptor, tools) != grant.activation:
            raise ValueError("plugin_capability_invalid")
        active.tools = tools

    @staticmethod
    def _activation_matches_request(
        grant: PluginGrantRecord,
        *,
        context_revision: str,
        descriptor_revision: str,
        package_revision: str,
        settings: _ConnectionSettings,
    ) -> bool:
        activation = grant.activation
        return (
            activation.authorization_context_revision == context_revision
            and activation.descriptor_revision == descriptor_revision
            and activation.package_revision == package_revision
            and activation.scopes == settings.scopes
            and activation.capability_ceiling == settings.capability_ceiling
            and dict(activation.allowed_tool_schemas) == dict(settings.allowed_tool_schemas)
            and activation.execution_policy == settings.execution_policy
            and dict(activation.source_constraints) == dict(settings.source_constraints)
            and dict(activation.destination_constraints) == dict(settings.destination_constraints)
        )

    @_lifecycle_mutation
    async def start(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        scopes: tuple[str, ...] | None = None,
        capability_ceiling: tuple[str, ...] | None = None,
        allowed_tool_schemas: Mapping[str, str] | None = None,
        execution_policy: PluginExecutionPolicy | str | None = None,
        source_constraints: Mapping[str, object] | None = None,
        destination_constraints: Mapping[str, object] | None = None,
        expected_registry_revision: str | None = None,
    ) -> AuthorizationStart:
        key = (plugin_id, app_id, _account_digest(account_key))
        async with self._lifecycle_lock(key):
            return await self._start_locked(
                plugin_id,
                app_id=app_id,
                account_key=account_key,
                scopes=scopes,
                capability_ceiling=capability_ceiling,
                allowed_tool_schemas=allowed_tool_schemas,
                execution_policy=execution_policy,
                source_constraints=source_constraints,
                destination_constraints=destination_constraints,
                expected_registry_revision=expected_registry_revision,
            )

    @_lifecycle_mutation
    async def start_compatible_update(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        activation: PluginGrantActivation,
        expected_registry_revision: str | None = None,
    ) -> AuthorizationStart:
        """Start a rebound grant without recomputing OAuth context.

        A package update necessarily changes the package-bound context digest,
        but an authority-equivalent update must not turn that identity change
        into a new consent flow.  The durable grant remains the sole source of
        account, profile, partition, scopes, and capability constraints.
        """

        if type(activation) is not PluginGrantActivation:
            raise ValueError("plugin_connection_drift")
        key = (plugin_id, app_id, _account_digest(account_key))
        async with self._lifecycle_lock(key):
            _plugin, _record, _requirement, package_revision, registry_revision = (
                self._mutation_definition(
                    plugin_id,
                    app_id,
                    expected_registry_revision=expected_registry_revision,
                )
            )
            descriptor = self._descriptor(plugin_id, app_id)
            grant = self._grants.current(
                plugin_id,
                app_id=app_id,
                account_key_sha256=activation.account_key_sha256,
            )
            if (
                grant is None
                or grant.activation != activation
                or activation.account_key_sha256 != key[2]
                or activation.package_revision != package_revision
                or not self._durable_grant_is_current(grant, descriptor)
            ):
                raise ValueError("plugin_connection_drift")
            active = await self._start_committed(grant, descriptor)
            previous = self._sessions.pop(key, None)
            self._sessions[key] = active
            self._states[key] = "connected"
            if previous is not None:
                with suppress(BaseException):
                    await self._submit(previous.client.close)
            return AuthorizationStart(
                plugin_id,
                app_id,
                activation.account_key_sha256,
                "connected",
                None,
                None,
                None,
                activation.authorization_context_revision,
                activation.scopes,
                activation.execution_policy,
                activation.capability_ceiling,
                dict(activation.allowed_tool_schemas),
                dict(activation.source_constraints),
                dict(activation.destination_constraints),
            )

    def _candidate_definition(
        self,
        plugin_id: str,
        app_id: str,
        *,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        expected_registry_revision: str,
    ) -> tuple[
        InstalledPluginRecord,
        InstalledMcpAppRecord,
        AppRequirement,
        McpConnectionDescriptor,
    ]:
        if (
            type(candidate_verified) is not VerifiedPlugin
            or type(candidate_binding) is not PluginAppBinding
            or type(expected_registry_revision) is not str
        ):
            raise ValueError("plugin_update_failed")
        snapshot = self._plugin_registry.snapshot()
        current = next(
            (
                record
                for record in getattr(snapshot, "records", ())
                if getattr(record, "plugin_id", None) == plugin_id
            ),
            None,
        )
        manifest = candidate_verified.manifest
        target = next(
            (
                version
                for version in getattr(current, "versions", ())
                if version.plugin_version == manifest.plugin_version
            ),
            None,
        )
        matches = tuple(
            requirement
            for requirement in manifest.required_apps + manifest.optional_apps
            if requirement.app_id == app_id
        )
        try:
            record = (
                self._app_registry.get(app_id)
                if self._candidate_record_provider is None
                else self._candidate_record_provider(
                    candidate_verified,
                    candidate_binding,
                    app_id,
                )
            )
        except Exception:
            raise ValueError("plugin_update_failed") from None
        if (
            getattr(snapshot, "revision", None) != expected_registry_revision
            or type(current) is not InstalledPluginRecord
            or current.selected_version == manifest.plugin_version
            or current.blocker is not None
            or target is None
            or target.state != "candidate"
            or target.manifest_sha256 != candidate_verified.manifest_sha256
            or target.package_sha256 != candidate_verified.package_sha256
            or target.signing_key_id != manifest.signing_key_id
            or manifest.plugin_id != plugin_id
            or manifest.publisher_id != current.publisher_id
            or len(matches) != 1
            or type(record) is not InstalledMcpAppRecord
            or matches[0] != _record_requirement(record)
            or app_id
            not in candidate_binding.required_app_ids + candidate_binding.optional_app_ids
            or not record.installed
            or not record.trusted
        ):
            raise ValueError("plugin_update_failed")
        if self._candidate_descriptor_provider is None:
            authority = signed_capability_authority_for_candidate(
                PackageRegistry(state_root=self._app_registry.state_root),
                record,
                candidate_verified,
            )
            if authority is None:
                raise ValueError("plugin_catalog_authority_missing")
            descriptor = descriptor_for_record(
                record,
                signed_capability_authority=authority,
            )
        else:
            descriptor = self._candidate_descriptor_provider(
                candidate_verified,
                candidate_binding,
                app_id,
            )
        if type(descriptor) is not McpConnectionDescriptor or descriptor.app_id != app_id:
            raise ValueError("plugin_update_failed")
        return current, record, matches[0], descriptor

    @staticmethod
    def _candidate_settings(
        requirement: AppRequirement,
        descriptor: McpConnectionDescriptor,
        *,
        scopes: tuple[str, ...] | None,
        capability_ceiling: tuple[str, ...] | None,
        allowed_tool_schemas: Mapping[str, str] | None,
        execution_policy: PluginExecutionPolicy | str | None,
        source_constraints: Mapping[str, object] | None,
        destination_constraints: Mapping[str, object] | None,
    ) -> _ConnectionSettings:
        offered = tuple(sorted(requirement.authentication.scopes))
        if scopes is None:
            raise ValueError("plugin_scopes_invalid")
        selected_scopes = _stable_scopes(scopes)
        if not set(selected_scopes).issubset(offered):
            raise ValueError("plugin_scopes_invalid")
        declared = {
            capability: digest
            for capability, digest in descriptor.declared_capabilities.items()
            if capability.startswith("tool:") and type(digest) is str
        }
        if (
            type(capability_ceiling) is not tuple
            or len(capability_ceiling) != len(set(capability_ceiling))
            or any(type(item) is not str or item not in declared for item in capability_ceiling)
            or not isinstance(allowed_tool_schemas, Mapping)
        ):
            raise ValueError("plugin_capability_scope_invalid")
        capabilities = tuple(sorted(capability_ceiling))
        schemas = {
            capability.removeprefix("tool:"): declared[capability]
            for capability in capabilities
        }
        if dict(allowed_tool_schemas) != schemas:
            raise ValueError("plugin_capability_scope_invalid")
        try:
            source = json.loads(canonical_bytes(dict(source_constraints or {})).decode("utf-8"))
            destination = json.loads(
                canonical_bytes(dict(destination_constraints or {})).decode("utf-8")
            )
            policy = (
                PluginExecutionPolicy.CONFIRM_EACH
                if execution_policy is None
                else PluginExecutionPolicy(execution_policy)
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_connection_settings_invalid") from None
        return _ConnectionSettings(
            scopes=selected_scopes,
            capability_ceiling=capabilities,
            allowed_tool_schemas=schemas,
            execution_policy=policy,
            source_constraints=source,
            destination_constraints=destination,
        )

    @staticmethod
    def _candidate_identity(
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
    ) -> dict[str, object]:
        return {
            "plugin_id": candidate_verified.manifest.plugin_id,
            "target_version": candidate_verified.manifest.plugin_version,
            "target_manifest_sha256": candidate_verified.manifest_sha256,
            "target_package_sha256": candidate_verified.package_sha256,
            "target_signing_key_id": candidate_verified.manifest.signing_key_id,
            "candidate_binding_revision": candidate_binding.revision,
        }

    @_lifecycle_mutation
    async def start_candidate(
        self,
        plugin_id: str,
        *,
        app_id: str,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        account_key: str,
        expected_registry_revision: str,
        scopes: tuple[str, ...] | None = None,
        capability_ceiling: tuple[str, ...] | None = None,
        allowed_tool_schemas: Mapping[str, str] | None = None,
        execution_policy: PluginExecutionPolicy | str | None = None,
        source_constraints: Mapping[str, object] | None = None,
        destination_constraints: Mapping[str, object] | None = None,
    ) -> AuthorizationStart | CandidateConnectionProbe:
        """Begin exact candidate setup without touching selected authority."""

        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        async with self._lifecycle_lock(key):
            current, record, requirement, descriptor = self._candidate_definition(
                plugin_id,
                app_id,
                candidate_verified=candidate_verified,
                candidate_binding=candidate_binding,
                expected_registry_revision=expected_registry_revision,
            )
            settings = self._candidate_settings(
                requirement,
                descriptor,
                scopes=scopes,
                capability_ceiling=capability_ceiling,
                allowed_tool_schemas=allowed_tool_schemas,
                execution_policy=execution_policy,
                source_constraints=source_constraints,
                destination_constraints=destination_constraints,
            )
            descriptor_revision = self._descriptor_revision(descriptor)
            context_revision = self._context_revision(
                descriptor,
                account_key_sha256=account,
                settings=settings,
                package_revision=candidate_verified.package_sha256,
            )
            authentication_kind = requirement.authentication.kind
            if authentication_kind is PluginAuthenticationKind.NONE:
                return await self.probe_candidate(
                    plugin_id,
                    app_id=app_id,
                    candidate_verified=candidate_verified,
                    candidate_binding=candidate_binding,
                    account_key=account_key,
                    expected_registry_revision=expected_registry_revision,
                    settings=settings,
                )
            try:
                authorization_flow = OAuthAuthorizationFlow(
                    _OAUTH_FLOW_BY_AUTHENTICATION[authentication_kind]
                )
            except KeyError:
                raise ValueError("plugin_secret_required") from None
            if self._profile_match_provider is None:
                raise ValueError("oauth_profile_catalog_unavailable")
            match = self._profile_match_provider(plugin_id, record)
            if (
                type(match) is not OAuthProfileMatchInput
                or match.plugin_publisher != current.publisher_id
                or match.app_publisher != record.publisher_id
                or match.provider_id != record.provider_id
                or not _resolved_issuer_matches_declared(
                    str(record.authorization_issuer),
                    match.authorization_issuer,
                )
                or match.resource_origin != record.remote_origin
            ):
                raise ValueError("oauth_profile_catalog_unavailable")
            attempt_context = self._attempt_context(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                settings=settings,
                authorization_context_revision=context_revision,
                descriptor_revision=descriptor_revision,
                package_revision=candidate_verified.package_sha256,
                registry_revision=expected_registry_revision,
                authorization_flow=authorization_flow,
            )
            attempt_context["schema_version"] = 2
            attempt_context["candidate_identity"] = self._candidate_identity(
                candidate_verified,
                candidate_binding,
            )
            started = await self._broker.begin(
                OAuthConnectionRequest(
                    profile_match=match,
                    plugin_id=plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                    scopes=settings.scopes,
                    authorization_context_revision=context_revision,
                    expected_authorization_flow=authorization_flow,
                    attempt_context=attempt_context,
                )
            )
            if getattr(self._plugin_registry.snapshot(), "revision", None) != (
                expected_registry_revision
            ):
                self._broker.discard_attempt(started.state_sha256)
                raise ValueError("plugin_registry_conflict")
            return AuthorizationStart(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                status="authorizing",
                request_id=started.state_sha256,
                authorization_url=started.authorization_url,
                expires_at=started.expires_at,
                authorization_context_revision=context_revision,
                provider_scopes=settings.scopes,
                execution_policy=settings.execution_policy,
                capability_ceiling=settings.capability_ceiling,
                allowed_tool_schemas=settings.allowed_tool_schemas,
                source_constraints=dict(settings.source_constraints),
                destination_constraints=dict(settings.destination_constraints),
                authorization_flow=started.authorization_flow.value,
                user_code=started.user_code,
                verification_uri=started.verification_uri,
                poll_interval_seconds=started.poll_interval_seconds,
            )

    @_lifecycle_mutation
    async def probe_candidate(
        self,
        plugin_id: str,
        *,
        app_id: str,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        account_key: str,
        expected_registry_revision: str,
        settings: _ConnectionSettings | None = None,
        scopes: tuple[str, ...] | None = None,
        capability_ceiling: tuple[str, ...] | None = None,
        allowed_tool_schemas: Mapping[str, str] | None = None,
        execution_policy: PluginExecutionPolicy | str | None = None,
        source_constraints: Mapping[str, object] | None = None,
        destination_constraints: Mapping[str, object] | None = None,
    ) -> CandidateConnectionProbe:
        """Compile exact candidate authority without publishing or replacing live state."""

        if (
            type(candidate_verified) is not VerifiedPlugin
            or type(candidate_binding) is not PluginAppBinding
            or type(expected_registry_revision) is not str
        ):
            raise ValueError("plugin_update_failed")
        snapshot = self._plugin_registry.snapshot()
        if getattr(snapshot, "revision", None) != expected_registry_revision:
            raise ValueError("plugin_registry_conflict")
        current = next(
            (
                record
                for record in getattr(snapshot, "records", ())
                if getattr(record, "plugin_id", None) == plugin_id
            ),
            None,
        )
        manifest = candidate_verified.manifest
        candidate = next(
            (
                version
                for version in getattr(current, "versions", ())
                if version.plugin_version == manifest.plugin_version
            ),
            None,
        )
        requirements = manifest.required_apps + manifest.optional_apps
        matches = tuple(item for item in requirements if item.app_id == app_id)
        record = self._app_registry.get(app_id)
        if (
            type(current) is not InstalledPluginRecord
            or current.selected_version == manifest.plugin_version
            or current.enabled is not True
            or candidate is None
            or candidate.state != "candidate"
            or candidate.manifest_sha256 != candidate_verified.manifest_sha256
            or candidate.package_sha256 != candidate_verified.package_sha256
            or candidate.signing_key_id != manifest.signing_key_id
            or manifest.plugin_id != plugin_id
            or len(matches) != 1
            or matches[0] != _record_requirement(record)
            or app_id
            not in (
                candidate_binding.required_app_ids
                + candidate_binding.optional_app_ids
            )
            or not record.installed
            or not record.trusted
        ):
            raise ValueError("plugin_update_failed")
        requirement = matches[0]
        if requirement.authentication.kind is not PluginAuthenticationKind.NONE:
            raise ValueError("plugin_authorization_required")
        if self._candidate_descriptor_provider is None:
            try:
                authority = _verified_plugin_authority(candidate_verified, app_id)
            except ValueError:
                raise ValueError("plugin_catalog_authority_missing") from None
            if authority is None:
                raise ValueError("plugin_catalog_authority_missing")
            descriptor = descriptor_for_record(
                record,
                signed_capability_authority=authority,
            )
        else:
            descriptor = self._candidate_descriptor_provider(
                candidate_verified,
                candidate_binding,
                app_id,
            )
            if type(descriptor) is not McpConnectionDescriptor or descriptor.app_id != app_id:
                raise ValueError("plugin_update_failed")
        if settings is not None:
            if type(settings) is not _ConnectionSettings or any(
                value is not None
                for value in (
                    scopes,
                    capability_ceiling,
                    allowed_tool_schemas,
                    execution_policy,
                    source_constraints,
                    destination_constraints,
                )
            ):
                raise ValueError("plugin_connection_settings_invalid")
            settings = self._candidate_settings(
                requirement,
                descriptor,
                scopes=settings.scopes,
                capability_ceiling=settings.capability_ceiling,
                allowed_tool_schemas=settings.allowed_tool_schemas,
                execution_policy=settings.execution_policy,
                source_constraints=settings.source_constraints,
                destination_constraints=settings.destination_constraints,
            )
        else:
            settings = self._candidate_settings(
                requirement,
                descriptor,
                scopes=scopes,
                capability_ceiling=capability_ceiling,
                allowed_tool_schemas=allowed_tool_schemas,
                execution_policy=execution_policy,
                source_constraints=source_constraints,
                destination_constraints=destination_constraints,
            )
        if self._execution_worker is not None:
            worker_descriptor = descriptor
            local_execution: _LocalMcpWorkerExecution | None = None
            if descriptor.deployment is McpClientDeployment.LOCAL:
                worker_descriptor, local_execution = (
                    self._candidate_local_worker_material(
                        plugin_id=plugin_id,
                        record=record,
                        descriptor=descriptor,
                        candidate_verified=candidate_verified,
                        candidate_binding=candidate_binding,
                        expected_registry_revision=expected_registry_revision,
                    )
                )
            elif descriptor.deployment is not McpClientDeployment.REMOTE:
                raise ValueError("plugin_update_failed")
            signed_authority = worker_descriptor.signed_capability_authority
            if not isinstance(signed_authority, Mapping):
                raise ValueError("plugin_update_failed")
            discovered = self._signed_local_tools(worker_descriptor)
            if settings is None:
                raise ValueError("plugin_capability_scope_invalid")
            _ceiling, _schemas, binding_digests = self._compile_capability_bindings(
                worker_descriptor,
                discovered,
                capability_ceiling=settings.capability_ceiling,
                allowed_tool_schemas=settings.allowed_tool_schemas,
            )
            revision = sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.plugin-candidate-connection.v1",
                        "plugin_id": plugin_id,
                        "app_id": app_id,
                        "account_key_sha256": _account_digest(account_key),
                        "package_sha256": candidate_verified.package_sha256,
                        "descriptor": worker_descriptor.to_dict(),
                        "settings": {
                            "scopes": list(settings.scopes),
                            "capability_ceiling": list(settings.capability_ceiling),
                            "allowed_tool_schemas": dict(settings.allowed_tool_schemas),
                            "execution_policy": settings.execution_policy.value,
                            "source_constraints": dict(settings.source_constraints),
                            "destination_constraints": dict(settings.destination_constraints),
                        },
                        "capability_binding_identities": [
                            item.to_dict() for item in binding_digests
                        ],
                    }
                )
            )
            worker_discovery = await self._discover_in_worker(
                authority=self._discovery_authority(
                    plugin_id=plugin_id,
                    descriptor=worker_descriptor,
                    account_key_sha256=_account_digest(account_key),
                    package_revision=candidate_verified.package_sha256,
                    connection_revision=revision,
                    authority_revision=sha256_hex(
                        canonical_bytes(_plain_json(signed_authority))
                    ),
                ),
                descriptor=worker_descriptor,
                credential={},
                local_execution=local_execution,
            )
            if self._compile_capability_bindings(
                worker_descriptor, worker_discovery.tools
            ) != (
                _ceiling,
                _schemas,
                binding_digests,
            ):
                raise ValueError("plugin_capability_invalid")
            if (
                getattr(self._plugin_registry.snapshot(), "revision", None)
                != expected_registry_revision
            ):
                raise ValueError("plugin_registry_conflict")
            return CandidateConnectionProbe("connected", revision)
        client = self._client_factory(descriptor, None)
        try:
            await self._submit(client.start)
            discovered = await self._submit(
                lambda: self.snapshot_plugin_tools(client, descriptor)
            )
            if settings is None:
                raise ValueError("plugin_capability_scope_invalid")
            _ceiling, _schemas, binding_digests = self._compile_capability_bindings(
                descriptor,
                discovered,
                capability_ceiling=settings.capability_ceiling,
                allowed_tool_schemas=settings.allowed_tool_schemas,
            )
        finally:
            with suppress(BaseException):
                await self._submit(client.close)
        if getattr(self._plugin_registry.snapshot(), "revision", None) != expected_registry_revision:
            raise ValueError("plugin_registry_conflict")
        revision = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.plugin-candidate-connection.v1",
                    "plugin_id": plugin_id,
                    "app_id": app_id,
                    "account_key_sha256": _account_digest(account_key),
                    "package_sha256": candidate_verified.package_sha256,
                    "descriptor": descriptor.to_dict(),
                    "settings": {
                        "scopes": list(settings.scopes),
                        "capability_ceiling": list(settings.capability_ceiling),
                        "allowed_tool_schemas": dict(settings.allowed_tool_schemas),
                        "execution_policy": settings.execution_policy.value,
                        "source_constraints": dict(settings.source_constraints),
                        "destination_constraints": dict(settings.destination_constraints),
                    },
                    "capability_binding_identities": [
                        item.to_dict() for item in binding_digests
                    ],
                }
            )
        )
        return CandidateConnectionProbe("connected", revision)

    async def _probe_candidate_prepared(
        self,
        prepared: OAuthPreparedAuthorization,
        descriptor: McpConnectionDescriptor,
        *,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        expected_registry_revision: str,
    ) -> tuple[types.Tool, ...]:
        descriptor_revision = self._descriptor_revision(descriptor)
        binding = self._probe_binding(prepared, descriptor)
        expectation = _BindingExpectation(
            identity=prepared,
            descriptor_revision=descriptor_revision,
            package_revision=candidate_verified.package_sha256,
            candidate_verified=candidate_verified,
            candidate_binding=candidate_binding,
            registry_revision=expected_registry_revision,
        )
        self._probe_bindings[binding] = expectation
        if self._execution_worker is not None:
            try:
                self._validate_binding_expectation(binding, expectation)
                token = await self._broker._current_prepared_access_token(
                    prepared,
                    binding,
                )
                return (
                    await self._discover_in_worker(
                    authority=self._discovery_authority(
                        plugin_id=prepared.plugin_id,
                        descriptor=descriptor,
                        account_key_sha256=prepared.account_key_sha256,
                        package_revision=candidate_verified.package_sha256,
                        connection_revision=binding.connection_revision,
                        authority_revision=binding.authority_revision,
                    ),
                    descriptor=descriptor,
                    credential={"access_token": token},
                    )
                ).tools
            finally:
                self._probe_bindings.pop(binding, None)
        client: CurrentMcpClient | None = None
        try:
            authorization = self._broker.authorize_prepared_transport(
                prepared,
                binding,
                self._transport_authority,
            )
            client = self._client_factory(descriptor, authorization)

            async def run() -> tuple[types.Tool, ...]:
                assert client is not None
                await client.start()
                return await self.snapshot_plugin_tools(client, descriptor)

            tools = await self._submit(run)
            return tools
        finally:
            if client is not None:
                with suppress(BaseException):
                    await self._submit(client.close)
            self._probe_bindings.pop(binding, None)

    async def _prepare_candidate_connection(
        self,
        prepared: OAuthPreparedAuthorization,
        *,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        expected_registry_revision: str,
    ) -> CandidateConnectionPreparation:
        _current, _record, requirement, descriptor = self._candidate_definition(
            prepared.plugin_id,
            prepared.app_id,
            candidate_verified=candidate_verified,
            candidate_binding=candidate_binding,
            expected_registry_revision=expected_registry_revision,
        )
        settings, descriptor_revision, authorization_flow = (
            self._candidate_prepared_settings(
                prepared,
                candidate_verified,
                candidate_binding,
                expected_registry_revision=expected_registry_revision,
            )
        )
        expected_flow = _OAUTH_FLOW_BY_AUTHENTICATION.get(
            requirement.authentication.kind
        )
        if expected_flow is None or authorization_flow.value != expected_flow:
            raise ValueError("oauth_authorization_flow_mismatch")
        if (
            descriptor_revision != self._descriptor_revision(descriptor)
            or self._context_revision(
                descriptor,
                account_key_sha256=prepared.account_key_sha256,
                settings=settings,
                package_revision=candidate_verified.package_sha256,
            )
            != prepared.authorization_context_revision
        ):
            raise ValueError("plugin_connection_drift")
        tools = await self._probe_candidate_prepared(
            prepared,
            descriptor,
            candidate_verified=candidate_verified,
            candidate_binding=candidate_binding,
            expected_registry_revision=expected_registry_revision,
        )
        activation = self._compile_activation(
            prepared,
            descriptor,
            tools,
            settings,
            descriptor_revision=descriptor_revision,
            package_revision=candidate_verified.package_sha256,
        )
        self._prepared_attempts[prepared.state_sha256] = prepared
        return CandidateConnectionPreparation(
            request_id=prepared.state_sha256,
            plugin_id=prepared.plugin_id,
            app_id=prepared.app_id,
            target_version=candidate_verified.manifest.plugin_version,
            target_manifest_sha256=candidate_verified.manifest_sha256,
            target_package_sha256=candidate_verified.package_sha256,
            candidate_binding_revision=candidate_binding.revision,
            activation=activation,
        )

    @_lifecycle_mutation
    async def complete_candidate(
        self,
        request_id: str,
        callback_uri: str,
        *,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        expected_registry_revision: str,
    ) -> CandidateConnectionPreparation:
        """Exchange and compile one candidate browser callback without publication."""

        state_sha256 = self._broker.callback_state_sha256(callback_uri)
        if request_id != state_sha256:
            raise ValueError("oauth_callback_mismatch")
        attempt = self._broker.authorization_attempt(request_id)
        key = (attempt.plugin_id, attempt.app_id, attempt.account_key_sha256)
        lock = self._callback_locks.setdefault(request_id, asyncio.Lock())
        async with lock:
            async with self._lifecycle_lock(key):
                prepared = self._prepared_attempts.get(request_id)
                if prepared is None:
                    pieces = urlsplit(callback_uri).path.split("/")
                    if len(pieces) != 4 or pieces[1] != "oauth" or pieces[3] != "callback":
                        raise ValueError("oauth_callback_invalid")
                    prepared = await self._broker.complete(
                        profile_id=pieces[2],
                        callback_uri=callback_uri,
                    )
                return await self._prepare_candidate_connection(
                    prepared,
                    candidate_verified=candidate_verified,
                    candidate_binding=candidate_binding,
                    expected_registry_revision=expected_registry_revision,
                )

    def callback_request_id(self, callback_uri: str) -> str:
        return self._broker.callback_state_sha256(callback_uri)

    def request_plugin_id(self, request_id: str) -> str:
        """Resolve a durable authorization attempt without performing network I/O."""

        if (
            type(request_id) is not str
            or len(request_id) != 64
            or any(character not in "0123456789abcdef" for character in request_id)
        ):
            raise ValueError("plugin_connection_state_invalid")
        current = self._grants.current_for_attempt(request_id)
        if current is not None:
            return current.activation.plugin_id
        prepared = self._prepared_attempts.get(request_id)
        if prepared is not None:
            return prepared.plugin_id
        try:
            return self._broker.authorization_attempt(request_id).plugin_id
        except ValueError:
            raise ValueError("plugin_connection_state_invalid") from None

    @_lifecycle_mutation
    def begin_update_recovery(
        self,
        *,
        previous_record: InstalledPluginRecord,
        candidate_binding: PluginAppBinding,
        account_key: str,
        checkpoint_id: str | None = None,
    ) -> PluginUpdateConnectionRecovery:
        if (
            type(previous_record) is not InstalledPluginRecord
            or type(candidate_binding) is not PluginAppBinding
        ):
            raise ValueError("plugin_update_recovery_required")
        plugin_id = previous_record.plugin_id
        account = _account_digest(account_key)
        predecessor_apps = (
            ()
            if previous_record.app_binding is None
            else tuple(
                sorted(
                    previous_record.app_binding.required_app_ids
                    + previous_record.app_binding.optional_app_ids
                )
            )
        )
        candidate_apps = tuple(
            sorted(candidate_binding.required_app_ids + candidate_binding.optional_app_ids)
        )
        all_apps = tuple(sorted(set(predecessor_apps + candidate_apps)))
        if checkpoint_id is None:
            checkpoint_id = "plugin-update-recovery-" + sha256_hex(uuid4().bytes)
        elif (
            type(checkpoint_id) is not str
            or not checkpoint_id.startswith("plugin-update-recovery-")
            or len(checkpoint_id.removeprefix("plugin-update-recovery-")) != 64
            or any(
                character not in "0123456789abcdef"
                for character in checkpoint_id.removeprefix("plugin-update-recovery-")
            )
        ):
            raise ValueError("plugin_update_recovery_required")
        grant_recovery = self._grants.begin_update_recovery(
            checkpoint_id,
            plugin_id=plugin_id,
            account_key_sha256=account,
            app_ids=all_apps,
        )
        lineages = tuple(
            self.authority_store.checkpoint_connection_lineage(
                plugin_id,
                app_id,
                account,
            )
            for app_id in all_apps
        )
        session_documents: list[dict[str, object]] = []
        for app_id in predecessor_apps:
            active = self._sessions.get((plugin_id, app_id, account))
            connection_revision = ""
            if active is not None:
                if active.grant is not None:
                    connection_revision = active.grant.activation.connection_revision
                elif active.credentialless_authority is not None:
                    connection_revision = (
                        active.credentialless_authority.connection_revision
                    )
                else:
                    raise ValueError("plugin_update_recovery_required")
            session_documents.append(
                {
                    "app_id": app_id,
                    "had_live_session": active is not None,
                    "connection_revision": connection_revision,
                }
            )
        material = {
            "checkpoint_id": checkpoint_id,
            "plugin_id": plugin_id,
            "account_key_sha256": account,
            "predecessor_app_ids": list(predecessor_apps),
            "candidate_app_ids": list(candidate_apps),
            "grant_recovery": grant_recovery,
            "authority_lineages": list(lineages),
            "live_sessions": session_documents,
        }
        material["revision"] = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.plugin-connection-update-recovery.v1",
                    "material": material,
                }
            )
        )
        return PluginUpdateConnectionRecovery.from_dict(material)

    @_lifecycle_mutation
    def discard_update_recovery_checkpoint(
        self,
        checkpoint_id: str,
        *,
        plugin_id: str,
        account_key: str,
    ) -> None:
        """Discard an exact checkpoint that was never sealed into the update journal."""

        if (
            checkpoint_id in self._update_session_stashes
            or checkpoint_id in self._restored_update_checkpoints
        ):
            raise ValueError("plugin_update_recovery_required")
        self._grants.discard_update_recovery_checkpoint(
            checkpoint_id,
            plugin_id=plugin_id,
            account_key_sha256=_account_digest(account_key),
        )

    @_lifecycle_mutation
    def begin_update_cutover(self, recovery: object) -> None:
        checked = (
            recovery
            if type(recovery) is PluginUpdateConnectionRecovery
            else PluginUpdateConnectionRecovery.from_dict(recovery)
        )
        if (
            checked.checkpoint_id in self._update_session_stashes
            or checked.checkpoint_id in self._restored_update_checkpoints
        ):
            return
        observed: list[tuple[tuple[str, str, str], _ActiveConnection | None]] = []
        for expected in checked.live_sessions:
            app_id = str(expected["app_id"])
            key = (checked.plugin_id, app_id, checked.account_key_sha256)
            active = self._sessions.get(key)
            if active is None:
                revision = ""
            elif active.grant is not None:
                revision = active.grant.activation.connection_revision
            elif active.credentialless_authority is not None:
                revision = active.credentialless_authority.connection_revision
            else:
                raise ValueError("plugin_update_recovery_required")
            if (
                (active is not None) is not expected["had_live_session"]
                or revision != expected["connection_revision"]
            ):
                raise ValueError("plugin_update_recovery_required")
            observed.append((key, active))
        stash: dict[tuple[str, str, str], _ActiveConnection] = {}
        for key, active in observed:
            if active is not None:
                stash[key] = self._sessions.pop(key)
        self._update_session_stashes[checked.checkpoint_id] = stash

    @_lifecycle_mutation
    async def restore_update_recovery(self, recovery: object) -> None:
        checked = (
            recovery
            if type(recovery) is PluginUpdateConnectionRecovery
            else PluginUpdateConnectionRecovery.from_dict(recovery)
        )
        app_ids = tuple(
            sorted(set(checked.predecessor_app_ids + checked.candidate_app_ids))
        )
        already_restored = checked.checkpoint_id in self._restored_update_checkpoints
        cutover_started = checked.checkpoint_id in self._update_session_stashes
        stash = self._update_session_stashes.get(checked.checkpoint_id, {})
        if not already_restored and cutover_started:
            for app_id in app_ids:
                key = (checked.plugin_id, app_id, checked.account_key_sha256)
                active = self._sessions.pop(key, None)
                if active is not None and active is not stash.get(key):
                    active.tools = ()
                    active.capability_binding_identities = ()
                    active.grant = None
                    active.binding = None
                    with suppress(BaseException):
                        await self._submit(active.client.close)
                self._states[key] = "installed_disconnected"
        self._grants.restore_update_recovery(checked.grant_recovery)
        for lineage in checked.authority_lineages:
            self.authority_store.restore_connection_lineage(lineage)
        if not already_restored:
            for key, active in stash.items():
                self._sessions[key] = active
                self._states[key] = "connected"
            self._update_session_stashes.pop(checked.checkpoint_id, None)
            self._restored_update_checkpoints.add(checked.checkpoint_id)

    @_lifecycle_mutation
    async def finalize_update_recovery(self, recovery: object) -> None:
        checked = (
            recovery
            if type(recovery) is PluginUpdateConnectionRecovery
            else PluginUpdateConnectionRecovery.from_dict(recovery)
        )
        stash = self._update_session_stashes.pop(checked.checkpoint_id, {})
        for active in stash.values():
            active.tools = ()
            active.capability_binding_identities = ()
            active.grant = None
            active.binding = None
            with suppress(BaseException):
                await self._submit(active.client.close)
        self._grants.finalize_update_recovery(checked.grant_recovery)
        self._restored_update_checkpoints.discard(checked.checkpoint_id)

    @_lifecycle_mutation
    async def retire_predecessor_for_update(
        self,
        recovery: object,
        *,
        app_id: str,
        candidate_activations: tuple[PluginGrantActivation, ...] = (),
        candidate_connection_revisions: tuple[tuple[str, str], ...] = (),
    ) -> None:
        checked = (
            recovery
            if type(recovery) is PluginUpdateConnectionRecovery
            else PluginUpdateConnectionRecovery.from_dict(recovery)
        )
        if (
            app_id not in checked.predecessor_app_ids
        ):
            raise ValueError("plugin_update_recovery_required")
        candidate_survives = app_id in checked.candidate_app_ids
        if type(candidate_connection_revisions) is not tuple or any(
                type(item) is not tuple
                or len(item) != 2
                or item[0] not in checked.candidate_app_ids
                or type(item[1]) is not str
                or len(item[1]) != 64
                or any(character not in "0123456789abcdef" for character in item[1])
                for item in candidate_connection_revisions
        ):
            raise ValueError("plugin_update_recovery_required")
        if (
            candidate_connection_revisions
            != tuple(sorted(candidate_connection_revisions))
            or len(candidate_connection_revisions)
            != len({item[0] for item in candidate_connection_revisions})
        ):
            raise ValueError("plugin_update_recovery_required")
        revisions = dict(candidate_connection_revisions)
        if any(
            revisions.get(item.app_id) != item.connection_revision
            for item in candidate_activations
        ):
            raise ValueError("plugin_update_recovery_required")
        activation = next(
            (item for item in candidate_activations if item.app_id == app_id),
            None,
        )
        credentialless_revision = None
        if candidate_survives and activation is None and app_id in revisions:
            credentialless_revision = revisions[app_id]
            key = (checked.plugin_id, app_id, checked.account_key_sha256)
            active = self._sessions.get(key)
            if active is not None and (
                active.grant is not None
                or active.credentialless_authority is None
                or active.credentialless_authority.connection_revision
                != credentialless_revision
            ):
                raise ValueError("plugin_update_recovery_required")
            try:
                authority = self.authority_store.connection(*key)
            except ValueError:
                raise ValueError("plugin_update_recovery_required") from None
            if (
                authority.authority_kind
                is not PluginConnectionAuthorityKind.CREDENTIALLESS
                or authority.connection_revision != credentialless_revision
                or active is not None
                and authority != active.credentialless_authority
            ):
                raise ValueError("plugin_update_recovery_required")
        retire_exact = getattr(self._grants, "retire_update_predecessor", None)
        if callable(retire_exact):
            authority_lineages = tuple(
                lineage
                for lineage in checked.authority_lineages
                if lineage["app_id"] == app_id
            )
            if len(authority_lineages) != 1:
                raise ValueError("plugin_update_recovery_required")
            retired_grant = await retire_exact(
                checked.grant_recovery,
                app_id=app_id,
                candidate_survives=candidate_survives,
                candidate_app_ids=checked.candidate_app_ids,
                candidate_activations=candidate_activations,
                credentialless_connection_revision=credentialless_revision,
                predecessor_authority_lineage=authority_lineages[0],
            )
            if retired_grant or candidate_survives:
                return
        elif candidate_survives:
            raise ValueError("plugin_update_recovery_required")
        pending = self._grants.pending_disconnect(
            checked.plugin_id,
            app_id=app_id,
            account_key_sha256=checked.account_key_sha256,
        )
        if pending is None:
            try:
                pending = self._grants.begin_disconnect(
                    checked.plugin_id,
                    app_id=app_id,
                    account_key_sha256=checked.account_key_sha256,
                )
            except ValueError as exc:
                if str(exc) != "plugin_grant_not_found":
                    raise
                try:
                    self.authority_store.invalidate_connection(
                        checked.plugin_id,
                        app_id,
                        checked.account_key_sha256,
                    )
                except ValueError as authority_exc:
                    if str(authority_exc) != "plugin_connection_authority_not_found":
                        raise
                return
        await self._grants.finish_disconnect(pending)

    @_lifecycle_mutation
    def cancel_candidate(
        self,
        request_id: str,
        *,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
    ) -> None:
        """Discard one exact server-held candidate attempt without selected mutation."""

        attempt = self._broker.authorization_attempt(request_id)
        if (
            attempt.state_sha256 != request_id
            or attempt.plugin_id != candidate_verified.manifest.plugin_id
            or attempt.app_id
            not in candidate_binding.required_app_ids + candidate_binding.optional_app_ids
            or attempt.attempt_context.get("schema_version") != 2
            or attempt.attempt_context.get("candidate_identity")
            != self._candidate_identity(candidate_verified, candidate_binding)
        ):
            raise ValueError("plugin_update_recovery_required")
        self._prepared_attempts.pop(request_id, None)
        self._broker.discard_attempt(request_id)

    @_lifecycle_mutation
    def cancel_candidate_exact(
        self,
        request_id: str,
        *,
        candidate_identity: object,
    ) -> None:
        """Discard one attempt from its sealed local identity, without catalog I/O."""

        if type(candidate_identity) is not dict or set(candidate_identity) != {
            "plugin_id",
            "target_version",
            "target_signing_key_id",
            "target_manifest_sha256",
            "target_package_sha256",
            "candidate_binding_revision",
        }:
            raise ValueError("plugin_update_recovery_required")
        identity = dict(candidate_identity)
        if any(
            type(identity[field]) is not str
            or not identity[field]
            or identity[field] != identity[field].strip()
            or len(identity[field]) > 128
            for field in ("plugin_id", "target_version", "target_signing_key_id")
        ) or any(
            type(identity[field]) is not str
            or len(identity[field]) != 64
            or any(character not in "0123456789abcdef" for character in identity[field])
            for field in (
                "target_manifest_sha256",
                "target_package_sha256",
                "candidate_binding_revision",
            )
        ):
            raise ValueError("plugin_update_recovery_required")
        attempt = self._broker.authorization_attempt(request_id)
        if (
            attempt.state_sha256 != request_id
            or attempt.plugin_id != identity["plugin_id"]
            or attempt.attempt_context.get("schema_version") != 2
            or attempt.attempt_context.get("candidate_identity") != identity
        ):
            raise ValueError("plugin_update_recovery_required")
        self._prepared_attempts.pop(request_id, None)
        self._broker.discard_attempt(request_id)

    @_lifecycle_mutation
    def cancel_candidate_attempts_exact(self, candidate_identity: object) -> None:
        """Consume every exact attempt, including a pre-journal crash orphan."""

        request_ids = self._broker.candidate_attempt_state_sha256(candidate_identity)
        for request_id in request_ids:
            self.cancel_candidate_exact(
                request_id,
                candidate_identity=candidate_identity,
            )

    @_lifecycle_mutation
    async def complete_candidate_device(
        self,
        request_id: str,
        *,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        expected_registry_revision: str,
    ) -> CandidateConnectionPreparation:
        """Poll and compile one candidate Device Flow without publication."""

        attempt = self._broker.authorization_attempt(request_id)
        key = (attempt.plugin_id, attempt.app_id, attempt.account_key_sha256)
        async with self._lifecycle_lock(key):
            prepared = self._prepared_attempts.get(request_id)
            if prepared is None:
                prepared = await self._broker.complete_device(request_id)
            return await self._prepare_candidate_connection(
                prepared,
                candidate_verified=candidate_verified,
                candidate_binding=candidate_binding,
                expected_registry_revision=expected_registry_revision,
            )

    @_lifecycle_mutation
    async def activate_candidate(
        self,
        preparation: CandidateConnectionPreparation,
        *,
        candidate_verified: VerifiedPlugin,
        expected_registry_revision: str,
    ) -> ConnectionStatus:
        """Publish one prepared candidate only after its exact version is selected."""

        if (
            type(preparation) is not CandidateConnectionPreparation
            or type(candidate_verified) is not VerifiedPlugin
            or preparation.target_version != candidate_verified.manifest.plugin_version
            or preparation.target_manifest_sha256 != candidate_verified.manifest_sha256
            or preparation.target_package_sha256 != candidate_verified.package_sha256
        ):
            raise ValueError("plugin_update_failed")
        snapshot = self._plugin_registry.snapshot()
        current = next(
            (
                record
                for record in getattr(snapshot, "records", ())
                if getattr(record, "plugin_id", None) == preparation.plugin_id
            ),
            None,
        )
        selected = next(
            (
                version
                for version in getattr(current, "versions", ())
                if version.plugin_version == preparation.target_version
            ),
            None,
        )
        if (
            getattr(snapshot, "revision", None) != expected_registry_revision
            or type(current) is not InstalledPluginRecord
            or current.selected_version != preparation.target_version
            or selected is None
            or selected.state != "selected"
            or selected.manifest_sha256 != preparation.target_manifest_sha256
            or selected.package_sha256 != preparation.target_package_sha256
            or current.app_binding is None
            or current.app_binding.revision != preparation.candidate_binding_revision
        ):
            raise ValueError("plugin_registry_conflict")
        prepared = self._prepared_attempts.get(preparation.request_id)
        if prepared is None:
            raise ValueError("plugin_update_recovery_required")
        descriptor = self._descriptor(preparation.plugin_id, preparation.app_id)
        activation = preparation.activation
        if (
            activation.descriptor_revision != self._descriptor_revision(descriptor)
            or activation.package_revision != candidate_verified.package_sha256
            or activation.capability_revision != descriptor.capability_revision
        ):
            raise ValueError("plugin_connection_drift")
        key = (
            preparation.plugin_id,
            preparation.app_id,
            activation.account_key_sha256,
        )
        async with self._lifecycle_lock(key):
            authority = self._grants.activate(prepared, activation)
            grant = self._grants.current(
                preparation.plugin_id,
                app_id=preparation.app_id,
                account_key_sha256=activation.account_key_sha256,
            )
            if grant is None or grant.authority != authority:
                raise ValueError("plugin_connection_authority_missing")
            active = await self._start_committed(grant, descriptor)
            previous = self._sessions.pop(key, None)
            self._sessions[key] = active
            self._states[key] = "connected"
            self._prepared_attempts.pop(preparation.request_id, None)
            if previous is not None:
                with suppress(BaseException):
                    await self._submit(previous.client.close)
            return self._status_from_grant(grant, status="connected")

    async def _start_locked(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        scopes: tuple[str, ...] | None = None,
        capability_ceiling: tuple[str, ...] | None = None,
        allowed_tool_schemas: Mapping[str, str] | None = None,
        execution_policy: PluginExecutionPolicy | str | None = None,
        source_constraints: Mapping[str, object] | None = None,
        destination_constraints: Mapping[str, object] | None = None,
        expected_registry_revision: str | None = None,
    ) -> AuthorizationStart:
        (
            plugin,
            record,
            requirement,
            package_revision,
            registry_revision,
        ) = self._mutation_definition(
            plugin_id,
            app_id,
            expected_registry_revision=expected_registry_revision,
        )
        descriptor = self._descriptor(plugin_id, app_id)
        descriptor_revision = self._descriptor_revision(descriptor)
        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        authentication_kind = requirement.authentication.kind
        if descriptor.deployment is McpClientDeployment.LOCAL or (
            descriptor.deployment is McpClientDeployment.REMOTE
            and authentication_kind is PluginAuthenticationKind.NONE
        ):
            settings = self._settings(
                plugin_id,
                descriptor=descriptor,
                scopes=scopes,
                capability_ceiling=capability_ceiling,
                allowed_tool_schemas=allowed_tool_schemas,
                execution_policy=execution_policy,
                source_constraints=source_constraints,
                destination_constraints=destination_constraints,
                oauth=False,
            )
        elif authentication_kind in _OAUTH_FLOW_BY_AUTHENTICATION:
            settings = self._settings(
                plugin_id,
                descriptor=descriptor,
                scopes=scopes,
                capability_ceiling=capability_ceiling,
                allowed_tool_schemas=allowed_tool_schemas,
                execution_policy=execution_policy,
                source_constraints=source_constraints,
                destination_constraints=destination_constraints,
                oauth=True,
            )
        else:
            raise ValueError("plugin_secret_required")
        context_revision = self._context_revision(
            descriptor,
            account_key_sha256=account,
            settings=settings,
            package_revision=package_revision,
        )
        active = self._sessions.get(key)
        current = self._grants.current(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account,
        )
        local_exact = (
            descriptor.deployment is McpClientDeployment.LOCAL
            and active is not None
            and active.authorization_context_revision == context_revision
            and active.descriptor_revision == descriptor_revision
            and active.package_revision == package_revision
            and active.capability_revision == descriptor.capability_revision
            and bool(active.capability_binding_identities)
        )
        remote_exact = (
            descriptor.deployment is McpClientDeployment.REMOTE
            and current is not None
            and self._activation_matches_request(
                current,
                context_revision=context_revision,
                descriptor_revision=descriptor_revision,
                package_revision=package_revision,
                settings=settings,
            )
            and self._durable_grant_is_current(current, descriptor)
        )
        if local_exact or remote_exact:
            try:
                if remote_exact and not self._active_grant_is_current(
                    active, current, descriptor
                ):
                    assert current is not None
                    if active is not None:
                        await self._evict_session(
                            key,
                            state="installed_disconnected",
                            suspend_grant=False,
                        )
                    active = await self._start_committed(current, descriptor)
                    self._sessions[key] = active
                else:
                    assert active is not None
                    await self._refresh_active(active, descriptor, current)
            except BaseException as exc:
                await self._evict_failed_session(
                    key,
                    exc,
                    credentialless=current is None,
                )
                raise
            self._require_registry_revision(plugin_id, registry_revision)
            self._states[key] = "connected"
            return AuthorizationStart(
                plugin_id,
                app_id,
                account,
                "connected",
                None,
                None,
                None,
                context_revision,
                settings.scopes,
                settings.execution_policy,
                settings.capability_ceiling,
                settings.allowed_tool_schemas,
                dict(settings.source_constraints),
                dict(settings.destination_constraints),
            )
        if active is not None or current is not None:
            await self._evict_session(
                key,
                state="installed_disconnected",
                suspend_grant=current is not None,
            )
        if descriptor.deployment is McpClientDeployment.LOCAL:
            if self._execution_worker is not None:
                signed_tools = self._signed_local_tools(descriptor)
                ceiling, schemas, binding_digests = self._compile_capability_bindings(
                    descriptor,
                    signed_tools,
                    capability_ceiling=settings.capability_ceiling,
                    allowed_tool_schemas=settings.allowed_tool_schemas,
                )
                effective_settings = replace(
                    settings,
                    capability_ceiling=ceiling,
                    allowed_tool_schemas=schemas,
                )
                proposed = self._credentialless_authority(
                    plugin_id=plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                    descriptor=descriptor,
                    settings=effective_settings,
                    authorization_context_revision=context_revision,
                    package_revision=package_revision,
                    capability_binding_identities=binding_digests,
                )
                (
                    worker_descriptor,
                    local_execution,
                    worker_package_revision,
                    _connection_revision,
                    _authority_revision,
                    _capability_ceiling,
                    _allowed_tool_schemas,
                ) = self._local_worker_material(
                    plugin_id=plugin_id,
                    record=record,
                    descriptor=descriptor,
                    connection_authority=proposed,
                )
                if worker_package_revision != package_revision:
                    raise ValueError("plugin_connection_drift")
                discovered = await self._discover_in_worker(
                    authority=self._discovery_authority(
                        plugin_id=plugin_id,
                        descriptor=worker_descriptor,
                        account_key_sha256=account,
                        package_revision=package_revision,
                        connection_revision=proposed.connection_revision,
                        authority_revision=proposed.revision,
                    ),
                    descriptor=worker_descriptor,
                    credential={},
                    local_execution=local_execution,
                )
                if self._compile_capability_bindings(
                    worker_descriptor,
                    discovered.tools,
                ) != (ceiling, schemas, binding_digests):
                    raise ValueError("plugin_capability_invalid")
                self._require_registry_revision(plugin_id, registry_revision)
                authority = self._publish_credentialless_authority(
                    plugin_id=plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                    descriptor=worker_descriptor,
                    settings=effective_settings,
                    authorization_context_revision=context_revision,
                    package_revision=package_revision,
                    capability_binding_identities=binding_digests,
                )
                if authority != proposed:
                    self.authority_store.invalidate_connection(plugin_id, app_id, account)
                    raise ValueError("plugin_connection_drift")
                try:
                    self._require_registry_revision(plugin_id, registry_revision)
                except ValueError:
                    self.authority_store.invalidate_connection(plugin_id, app_id, account)
                    raise
                self._sessions[key] = _ActiveConnection(
                    _WorkerOnlyLocalClient(),
                    discovered.tools,
                    None,
                    None,
                    worker_descriptor.capability_revision,
                    binding_digests,
                    context_revision,
                    self._descriptor_revision(worker_descriptor),
                    package_revision,
                    authority,
                    discovered.server_id,
                    discovered.server_version,
                )
                self._states[key] = "connected"
                return AuthorizationStart(
                    plugin_id,
                    app_id,
                    account,
                    "connected",
                    None,
                    None,
                    None,
                    context_revision,
                    settings.scopes,
                    settings.execution_policy,
                    ceiling,
                    schemas,
                    dict(settings.source_constraints),
                    dict(settings.destination_constraints),
                )
            if self._local_connector is None:
                package_registry = self._package_registry
                try:
                    if package_registry is None:
                        raise ValueError
                    package_record = package_registry.get(app_id)
                    package = package_registry.reverify_installed(app_id)
                    if (
                        not package_record.enabled
                        or package_record.blocker is not None
                        or package_record.server_version
                        != record.local_package_version
                        or package_record.platform != record.local_package_platform
                        or package_record.manifest_digest
                        != record.local_package_manifest_sha256
                        or package.manifest_sha256
                        != record.local_package_manifest_sha256
                        or package.manifest.server_id != app_id
                        or package.manifest.publisher_id != record.publisher_id
                    ):
                        raise ValueError
                    client: CurrentMcpClient | _WorkerOnlyLocalClient = (
                        _WorkerOnlyLocalClient()
                    )
                    tools = self._signed_local_tools(descriptor)
                except Exception:
                    raise ValueError("plugin_local_connection_unavailable") from None
            else:
                async def connect_local() -> CurrentMcpClient:
                    assert self._local_connector is not None
                    return await self._local_connector(plugin_id, record)

                client = await self._submit(connect_local)
                tools = ()
            try:
                if type(client) is not _WorkerOnlyLocalClient:
                    tools = await self._submit(
                        lambda: self.snapshot_plugin_tools(client, descriptor)
                    )
                _ceiling, _schemas, binding_digests = self._compile_capability_bindings(
                    descriptor,
                    tools,
                    capability_ceiling=settings.capability_ceiling,
                    allowed_tool_schemas=settings.allowed_tool_schemas,
                )
                authority = self._publish_credentialless_authority(
                    plugin_id=plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                    descriptor=descriptor,
                    settings=replace(
                        settings,
                        capability_ceiling=_ceiling,
                        allowed_tool_schemas=_schemas,
                    ),
                    authorization_context_revision=context_revision,
                    package_revision=package_revision,
                    capability_binding_identities=binding_digests,
                )
            except BaseException:
                with suppress(BaseException):
                    await self._submit(client.close)
                self._states[key] = "installed_disconnected"
                raise
            try:
                self._require_registry_revision(plugin_id, registry_revision)
            except ValueError:
                self.authority_store.invalidate_connection(plugin_id, app_id, account)
                with suppress(BaseException):
                    await self._submit(client.close)
                self._states[key] = "installed_disconnected"
                raise
            previous = self._sessions.pop(key, None)
            if previous is not None:
                with suppress(BaseException):
                    await self._submit(previous.client.close)
            self._sessions[key] = _ActiveConnection(
                client,
                tools,
                None,
                None,
                descriptor.capability_revision,
                binding_digests,
                context_revision,
                descriptor_revision,
                package_revision,
                authority,
            )
            self._states[key] = "connected"
            return AuthorizationStart(
                plugin_id,
                app_id,
                account,
                "connected",
                None,
                None,
                None,
                context_revision,
                settings.scopes,
                settings.execution_policy,
                settings.capability_ceiling,
                settings.allowed_tool_schemas,
                dict(settings.source_constraints),
                dict(settings.destination_constraints),
            )
        if authentication_kind is PluginAuthenticationKind.NONE:
            if descriptor.deployment is not McpClientDeployment.REMOTE:
                raise ValueError("plugin_authentication_mismatch")
            return await self._connect_credentialless_remote(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                descriptor=descriptor,
                settings=settings,
                authorization_context_revision=context_revision,
                descriptor_revision=descriptor_revision,
                package_revision=package_revision,
                registry_revision=registry_revision,
            )
        await self._finish_pending_disconnect_locked(key)
        if self._profile_match_provider is None:
            raise ValueError("oauth_profile_catalog_unavailable")
        match = self._profile_match_provider(plugin_id, record)
        if (
            type(match) is not OAuthProfileMatchInput
            or match.plugin_publisher != getattr(plugin, "publisher_id", None)
            or match.app_publisher != record.publisher_id
            or match.provider_id != record.provider_id
            or not _resolved_issuer_matches_declared(
                str(record.authorization_issuer),
                match.authorization_issuer,
            )
            or match.resource_origin != record.remote_origin
        ):
            raise ValueError("oauth_profile_catalog_unavailable")
        broker_start: OAuthAuthorizationStart = await self._broker.begin(
            OAuthConnectionRequest(
                profile_match=match,
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                scopes=settings.scopes,
                authorization_context_revision=context_revision,
                expected_authorization_flow=OAuthAuthorizationFlow(
                    _OAUTH_FLOW_BY_AUTHENTICATION[authentication_kind]
                ),
                attempt_context=self._attempt_context(
                    plugin_id=plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                    settings=settings,
                    authorization_context_revision=context_revision,
                    descriptor_revision=descriptor_revision,
                    package_revision=package_revision,
                    registry_revision=registry_revision,
                    authorization_flow=OAuthAuthorizationFlow(
                        _OAUTH_FLOW_BY_AUTHENTICATION[authentication_kind]
                    ),
                ),
            )
        )
        try:
            self._require_registry_revision(plugin_id, registry_revision)
        except ValueError:
            self._broker.discard_attempt(broker_start.state_sha256)
            raise
        if broker_start.authorization_flow.value != _OAUTH_FLOW_BY_AUTHENTICATION[authentication_kind]:
            raise ValueError("oauth_authorization_flow_mismatch")
        self._states[key] = "authorizing"
        return AuthorizationStart(
            plugin_id,
            app_id,
            account,
            "authorizing",
            broker_start.state_sha256,
            broker_start.authorization_url,
            broker_start.expires_at,
            context_revision,
            settings.scopes,
            settings.execution_policy,
            settings.capability_ceiling,
            settings.allowed_tool_schemas,
            authorization_flow=broker_start.authorization_flow.value,
            user_code=broker_start.user_code,
            verification_uri=broker_start.verification_uri,
            poll_interval_seconds=broker_start.poll_interval_seconds,
        )

    @_lifecycle_mutation
    async def complete_device(self, request_id: str) -> ConnectionStatus:
        """Complete one provider-neutral Device Flow poll and activate on success."""

        attempt = self._broker.authorization_attempt(request_id)
        key = (
            attempt.plugin_id,
            attempt.app_id,
            attempt.account_key_sha256,
        )
        async with self._lifecycle_lock(key):
            try:
                self._validate_attempt_flow(
                    plugin_id=attempt.plugin_id,
                    app_id=attempt.app_id,
                    attempt_context=attempt.attempt_context,
                )
            except ValueError:
                self._broker.discard_attempt(request_id)
                raise
            current = self._grants.current_for_attempt(request_id)
            if current is not None:
                descriptor = self._descriptor(
                    current.activation.plugin_id,
                    current.activation.app_id,
                )
                active = self._sessions.get(key)
                if not self._active_grant_is_current(active, current, descriptor):
                    active = await self._start_committed(current, descriptor)
                    self._sessions[key] = active
                assert active is not None
                await self._refresh_active(active, descriptor, current)
                self._states[key] = "connected"
                return self._status_from_grant(current, status="connected")
            prepared = self._prepared_attempts.get(request_id)
            if prepared is None:
                prepared = await self._broker.complete_device(request_id)
                if prepared.state_sha256 != request_id:
                    raise ValueError("oauth_callback_mismatch")
                settings, _descriptor_revision, _package_revision, _registry_revision, _flow = (
                    self._prepared_settings(prepared)
                )
                try:
                    self._require_registry_revision(
                        prepared.plugin_id,
                        _registry_revision,
                    )
                except ValueError:
                    self._broker.discard_attempt(request_id)
                    raise
                self._prepared_attempts[request_id] = prepared
            return await self._activate_prepared_locked(prepared)

    @_lifecycle_mutation
    async def activate_prepared(
        self,
        prepared: OAuthPreparedAuthorization,
    ) -> ConnectionStatus:
        if type(prepared) is not OAuthPreparedAuthorization:
            raise ValueError("plugin_connection_invalid")
        key = (prepared.plugin_id, prepared.app_id, prepared.account_key_sha256)
        async with self._lifecycle_lock(key):
            return await self._activate_prepared_locked(prepared)

    async def _activate_prepared_locked(
        self,
        prepared: OAuthPreparedAuthorization,
    ) -> ConnectionStatus:
        try:
            self._validate_attempt_flow(
                plugin_id=prepared.plugin_id,
                app_id=prepared.app_id,
                attempt_context=prepared.attempt_context,
            )
        except ValueError:
            self._prepared_attempts.pop(prepared.state_sha256, None)
            self._broker.discard_attempt(prepared.state_sha256)
            raise
        self._installed(prepared.plugin_id, prepared.app_id)
        descriptor = self._descriptor(prepared.plugin_id, prepared.app_id)
        if descriptor.deployment is not McpClientDeployment.REMOTE:
            raise ValueError("plugin_connection_invalid")
        try:
            (
                settings,
                expected_descriptor_revision,
                expected_package_revision,
                expected_registry_revision,
                _authorization_flow,
            ) = self._prepared_settings(prepared)
            self._require_registry_revision(
                prepared.plugin_id,
                expected_registry_revision,
            )
        except ValueError:
            self._prepared_attempts.pop(prepared.state_sha256, None)
            self._broker.discard_attempt(prepared.state_sha256)
            raise
        key = (prepared.plugin_id, prepared.app_id, prepared.account_key_sha256)
        existing = self._grants.current_for_attempt(prepared.state_sha256)
        if existing is not None:
            active = self._sessions.get(key)
            if not self._active_grant_is_current(active, existing, descriptor):
                active = await self._start_committed(existing, descriptor)
                self._sessions[key] = active
            assert active is not None
            await self._refresh_active(active, descriptor, existing)
            self._states[key] = "connected"
            return self._status_from_grant(existing, status="connected")
        try:
            descriptor_revision = self._descriptor_revision(descriptor)
            package_revision = self._package_revision(prepared.plugin_id)
            if (
                descriptor_revision != expected_descriptor_revision
                or package_revision != expected_package_revision
                or self._context_revision(
                    descriptor,
                    account_key_sha256=prepared.account_key_sha256,
                    settings=settings,
                    package_revision=package_revision,
                )
                != prepared.authorization_context_revision
            ):
                raise ValueError("plugin_connection_drift")
            tools = await self._probe_prepared(
                prepared,
                descriptor,
                descriptor_revision=descriptor_revision,
                package_revision=package_revision,
            )
            activation = self._compile_activation(
                prepared,
                descriptor,
                tools,
                settings,
                descriptor_revision=descriptor_revision,
                package_revision=package_revision,
            )
            binding = self._probe_binding(prepared, descriptor)
            expectation = _BindingExpectation(
                prepared,
                descriptor_revision,
                package_revision,
            )
            self._probe_bindings[binding] = expectation
            try:
                self._validate_binding_expectation(binding, expectation)
                self._broker.authorize_prepared_transport(
                    prepared,
                    binding,
                    self._transport_authority,
                )
            finally:
                self._probe_bindings.pop(binding, None)
            await self._finish_pending_disconnect_locked(key)
            authority = self._grants.activate(prepared, activation)
            grant = self._grants.current(
                prepared.plugin_id,
                app_id=prepared.app_id,
                account_key_sha256=prepared.account_key_sha256,
            )
            if grant is None or grant.authority != authority:
                raise ValueError("plugin_connection_authority_missing")
            active = await self._start_committed(grant, descriptor)
            previous = self._sessions.pop(key, None)
            if previous is not None:
                with suppress(BaseException):
                    await self._submit(previous.client.close)
            self._sessions[key] = active
            self._states[key] = "connected"
            self._prepared_attempts.pop(prepared.state_sha256, None)
            return self._status_from_grant(grant, status="connected")
        except BaseException as exc:
            if _terminal_prepared_failure(exc):
                self._prepared_attempts.pop(prepared.state_sha256, None)
                self._broker.discard_attempt(prepared.state_sha256)
            await self._evict_failed_session(
                key,
                exc,
                credentialless=False,
            )
            if self._grants.current(
                prepared.plugin_id,
                app_id=prepared.app_id,
                account_key_sha256=prepared.account_key_sha256,
            ) is None:
                self._disable_plugin(prepared.plugin_id)
            raise

    async def _complete_locked(
        self,
        *,
        state_sha256: str,
        profile_id: str,
        callback_uri: str,
        attempt: OAuthAuthorizationAttempt | None,
    ) -> ConnectionStatus:
        current = self._grants.current_for_attempt(state_sha256)
        if current is not None:
            descriptor = self._descriptor(
                current.activation.plugin_id,
                current.activation.app_id,
            )
            key = (
                current.activation.plugin_id,
                current.activation.app_id,
                current.activation.account_key_sha256,
            )
            active = self._sessions.get(key)
            if not self._active_grant_is_current(active, current, descriptor):
                active = await self._start_committed(current, descriptor)
                self._sessions[key] = active
            assert active is not None
            await self._refresh_active(active, descriptor, current)
            self._states[key] = "connected"
            return self._status_from_grant(current, status="connected")
        prepared = self._prepared_attempts.get(state_sha256)
        if prepared is None:
            if attempt is None:
                attempt = self._broker.authorization_attempt(state_sha256)
            try:
                self._validate_attempt_flow(
                    plugin_id=attempt.plugin_id,
                    app_id=attempt.app_id,
                    attempt_context=attempt.attempt_context,
                )
            except ValueError:
                self._broker.discard_attempt(state_sha256)
                raise
            try:
                prepared = await self._broker.complete(
                    profile_id=profile_id,
                    callback_uri=callback_uri,
                )
            except BaseException as exc:
                key = (
                    attempt.plugin_id,
                    attempt.app_id,
                    attempt.account_key_sha256,
                )
                await self._evict_session(
                    key,
                    state=self._failure_state(exc),
                    suspend_grant=True,
                )
                self._disable_plugin(attempt.plugin_id)
                raise
            if prepared.state_sha256 != state_sha256:
                raise ValueError("oauth_callback_mismatch")
            self._prepared_attempts[state_sha256] = prepared
        return await self._activate_prepared_locked(prepared)

    @_lifecycle_mutation
    async def complete(self, request_id: str, callback_uri: str) -> ConnectionStatus:
        state_sha256 = self._broker.callback_state_sha256(callback_uri)
        if request_id != state_sha256:
            raise ValueError("oauth_callback_mismatch")
        try:
            pieces = urlsplit(callback_uri).path.split("/")
            if len(pieces) != 4 or pieces[1] != "oauth" or pieces[3] != "callback":
                raise ValueError
            profile_id = pieces[2]
        except (TypeError, ValueError):
            raise ValueError("oauth_callback_invalid") from None
        lock = self._callback_locks.setdefault(state_sha256, asyncio.Lock())
        async with lock:
            current = self._grants.current_for_attempt(state_sha256)
            if current is not None:
                key = (
                    current.activation.plugin_id,
                    current.activation.app_id,
                    current.activation.account_key_sha256,
                )
                attempt = None
            else:
                prepared = self._prepared_attempts.get(state_sha256)
                if prepared is not None:
                    key = (
                        prepared.plugin_id,
                        prepared.app_id,
                        prepared.account_key_sha256,
                    )
                    attempt = None
                else:
                    attempt = self._broker.authorization_attempt(state_sha256)
                    key = (
                        attempt.plugin_id,
                        attempt.app_id,
                        attempt.account_key_sha256,
                    )
            async with self._lifecycle_lock(key):
                return await self._complete_locked(
                    state_sha256=state_sha256,
                    profile_id=profile_id,
                    callback_uri=callback_uri,
                    attempt=attempt,
                )

    @_lifecycle_mutation
    async def complete_callback(self, callback_uri: str) -> ConnectionStatus:
        try:
            pieces = urlsplit(callback_uri).path.split("/")
            if len(pieces) != 4 or pieces[1] != "oauth" or pieces[3] != "callback":
                raise ValueError
            profile_id = pieces[2]
        except (TypeError, ValueError):
            raise ValueError("oauth_callback_invalid") from None
        del profile_id
        return await self.complete(
            self._broker.callback_state_sha256(callback_uri),
            callback_uri,
        )

    def _status_from_grant(
        self,
        grant: PluginGrantRecord,
        *,
        status: str,
    ) -> ConnectionStatus:
        activation = grant.activation
        return ConnectionStatus(
            plugin_id=activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
            status=status,
            authorization_context_revision=str(activation.authorization_context_revision),
            token_revision=None,
            provider_scopes=activation.scopes,
            execution_policy=activation.execution_policy,
            authority_revision=activation.authority_revision,
            capability_ceiling=activation.capability_ceiling,
            allowed_tool_schemas=activation.allowed_tool_schemas,
            source_constraints=activation.source_constraints,
            destination_constraints=activation.destination_constraints,
        )

    @staticmethod
    def _failure_state(exc: BaseException) -> str:
        reason = getattr(getattr(exc, "reason", None), "value", None)
        return (
            "reauthorization_required"
            if str(exc) in {"oauth_reauthorization_required", "authorization_required"}
            or reason == "authorization_required"
            else "installed_disconnected"
        )

    async def status(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
    ) -> ConnectionStatus:
        key = (plugin_id, app_id, _account_digest(account_key))
        async with self._lifecycle_lock(key):
            return await self._status_locked(
                plugin_id,
                app_id=app_id,
                account_key=account_key,
            )

    def local_state_without_account(self, plugin_id: str, *, app_id: str) -> str | None:
        """Read only durable risk state that is safe without an account selector."""

        self._installed(plugin_id, app_id, allow_blocked=True)
        projector = getattr(self._grants, "local_state_without_account", None)
        if not callable(projector):
            return None
        state = projector(plugin_id, app_id=app_id)
        if state not in {None, "revocation_pending", "permission_changed"}:
            raise ValueError("plugin_connection_state_invalid")
        return state

    def local_plugin_state_without_account(self, plugin_id: str) -> str | None:
        """Read plugin-wide durable risk even when the current binding has no apps."""

        projector = getattr(self._grants, "local_plugin_state_without_account", None)
        if not callable(projector):
            return None
        state = projector(plugin_id)
        if state not in {None, "revocation_pending", "permission_changed"}:
            raise ValueError("plugin_connection_state_invalid")
        return state

    async def _status_locked(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
    ) -> ConnectionStatus:
        record = self._installed(plugin_id, app_id, allow_blocked=True)
        requirement = self._verified_requirement(plugin_id, record)
        descriptor = self._descriptor(plugin_id, app_id)
        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        recovered = tuple(
            prepared
            for prepared in self._prepared_attempts.values()
            if (
                prepared.plugin_id,
                prepared.app_id,
                prepared.account_key_sha256,
            )
            == key
        )
        if len(recovered) > 1:
            raise ValueError("plugin_connection_recovery_invalid")
        pending_disconnect = self._grants.pending_disconnect(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account,
        )
        if pending_disconnect is not None and not recovered:
            return ConnectionStatus(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                status="revocation_pending",
                authorization_context_revision="",
                token_revision=None,
                provider_scopes=(),
                execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
                authority_revision="",
            )
        if (
            descriptor.deployment is McpClientDeployment.REMOTE
            and requirement.authentication.kind is PluginAuthenticationKind.NONE
        ):
            try:
                authority = self.authority_store.connection(plugin_id, app_id, account)
            except ValueError as exc:
                if str(exc) != "plugin_connection_authority_not_found":
                    raise
                return ConnectionStatus(
                    plugin_id=plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                    status="installed_disconnected",
                    authorization_context_revision="",
                    token_revision=None,
                    provider_scopes=(),
                    execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
                    authority_revision="",
                )
            durable = self._credentialless_authority_is_current(
                authority,
                plugin_id=plugin_id,
                descriptor=descriptor,
            )
            if durable is None:
                self._states[key] = "permission_changed"
                raise ValueError("plugin_connection_drift")
            authorization_context_revision, _identities = durable
            return ConnectionStatus(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                status="connected",
                authorization_context_revision=authorization_context_revision,
                token_revision=None,
                provider_scopes=(),
                execution_policy=authority.execution_policy,
                authority_revision=authority.revision,
                capability_ceiling=authority.capability_ceiling,
                allowed_tool_schemas=authority.allowed_tool_schemas,
                source_constraints=authority.source_constraints,
                destination_constraints=authority.destination_constraints,
            )
        if recovered:
            prepared = recovered[0]
            current = self._grants.current_for_attempt(prepared.state_sha256)
            if current is not None:
                # The grant commit won, but the process stopped before the
                # in-memory recovery record was retired.  The durable grant is
                # authoritative; discard only the stale prepared view.
                self._prepared_attempts.pop(prepared.state_sha256, None)
                recovered = ()
            elif self._broker.is_transaction_consumed(prepared.state_sha256):
                # A consumed transaction cannot be resumed.  Remove its
                # recovered view so restart/status cannot wedge the connection
                # in ``finalizing`` forever.  The real broker treats a missing
                # consumed transaction as an idempotent discard.
                self._prepared_attempts.pop(prepared.state_sha256, None)
                self._broker.discard_prepared(prepared.state_sha256)
                self._states[key] = "installed_disconnected"
                recovered = ()
        if recovered:
            prepared = recovered[0]
            settings, _descriptor_revision, _package_revision, _registry_revision, _flow = (
                self._prepared_settings(prepared)
            )
            return ConnectionStatus(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                status="finalizing",
                authorization_context_revision=prepared.authorization_context_revision,
                token_revision=None,
                provider_scopes=prepared.scopes,
                execution_policy=settings.execution_policy,
                authority_revision="",
                capability_ceiling=settings.capability_ceiling,
                allowed_tool_schemas=settings.allowed_tool_schemas,
                source_constraints=settings.source_constraints,
                destination_constraints=settings.destination_constraints,
            )
        grant = self._grants.current(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account,
        )
        active = self._sessions.get(key)
        if descriptor.deployment is McpClientDeployment.LOCAL:
            try:
                authority = self.authority_store.connection(plugin_id, app_id, account)
            except ValueError as exc:
                if str(exc) != "plugin_connection_authority_not_found":
                    raise
                authority = None
            durable = (
                None
                if authority is None
                else self._credentialless_authority_is_current(
                    authority,
                    plugin_id=plugin_id,
                    descriptor=descriptor,
                )
            )
            if authority is not None and durable is not None:
                context_revision, _identities = durable
                return ConnectionStatus(
                    plugin_id=plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                    status="connected",
                    authorization_context_revision=context_revision,
                    token_revision=None,
                    provider_scopes=(),
                    execution_policy=authority.execution_policy,
                    authority_revision=authority.revision,
                    capability_ceiling=authority.capability_ceiling,
                    allowed_tool_schemas=authority.allowed_tool_schemas,
                    source_constraints=authority.source_constraints,
                    destination_constraints=authority.destination_constraints,
                )
            return ConnectionStatus(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                status="installed_disconnected",
                authorization_context_revision="",
                token_revision=None,
                provider_scopes=(),
                execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
                authority_revision="",
            )
        if self._active_grant_is_current(active, grant, descriptor):
            assert grant is not None
            return self._status_from_grant(grant, status="connected")
        if self._durable_grant_is_current(grant, descriptor):
            assert grant is not None
            return self._status_from_grant(grant, status="connected")
        state = self._states.get(key, "installed_disconnected")
        if state == "connected":
            state = "installed_disconnected"
            self._states[key] = state
        if grant is not None:
            return self._status_from_grant(grant, status=state)
        return ConnectionStatus(
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256=account,
            status=state,
            authorization_context_revision="",
            token_revision=None,
            provider_scopes=(),
            execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
            authority_revision="",
        )

    @_lifecycle_mutation
    async def ensure_client(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
    ) -> CurrentMcpClient:
        key = (plugin_id, app_id, _account_digest(account_key))
        async with self._lifecycle_lock(key):
            return await self._ensure_client_locked(
                plugin_id,
                app_id=app_id,
                account_key=account_key,
            )

    async def _prepare_authority_context_locked(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        account: str,
        requirement: AppRequirement,
        descriptor: McpConnectionDescriptor,
    ) -> _PreparedAuthorityContext:
        key = (plugin_id, app_id, account)
        grant = self._grants.current(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account,
        )
        active = self._sessions.get(key)
        credentialless = (
            descriptor.deployment is McpClientDeployment.LOCAL
            or requirement.authentication.kind is PluginAuthenticationKind.NONE
        )
        try:
            if credentialless:
                try:
                    durable = self.authority_store.connection(
                        plugin_id,
                        app_id,
                        account,
                    )
                except ValueError:
                    raise ValueError("plugin_connection_not_live") from None
                if (
                    self._credentialless_authority_is_current(
                        durable,
                        plugin_id=plugin_id,
                        descriptor=descriptor,
                    )
                    is None
                ):
                    raise ValueError("plugin_connection_not_live")
                started_now = False
                if active is None:
                    await self._start_locked(
                        plugin_id,
                        app_id=app_id,
                        account_key=account_key,
                        **self._held_consent(durable),
                    )
                    active = self._sessions.get(key)
                    started_now = True
                elif active.credentialless_authority != durable:
                    raise ValueError("plugin_connection_drift")
                assert active is not None
                if not started_now:
                    await self._refresh_active(active, descriptor, None)
                durable = active.credentialless_authority
                if durable is None:
                    raise ValueError("plugin_connection_not_live")
                package_revision = active.package_revision
            else:
                if not self._durable_grant_is_current(grant, descriptor):
                    raise ValueError("plugin_connection_not_live")
                assert grant is not None
                if not self._active_grant_is_current(active, grant, descriptor):
                    active = await self._start_committed(grant, descriptor)
                    self._sessions[key] = active
                else:
                    assert active is not None
                    await self._refresh_active(active, descriptor, grant)
                durable = grant.authority
                package_revision = str(grant.activation.package_revision)
            if (
                active is None
                or type(active.server_id) is not str
                or not active.server_id
                or type(active.server_version) is not str
                or not active.server_version
                or active.package_revision != package_revision
            ):
                raise ValueError("plugin_connection_not_live")
            self._states[key] = "connected"
            return _PreparedAuthorityContext(
                authority=durable,
                package_revision=package_revision,
                server_id=active.server_id,
                server_version=active.server_version,
            )
        except _ProviderDiscoveryError:
            # A typed provider contract failure is deliberately left at the
            # action boundary.  The backend owns the one signed update lookup
            # and may retry discovery after applying it; evicting the live
            # session here would turn that bounded retry into a misleading
            # reconnect/empty-tools failure.
            raise
        except BaseException as exc:
            await self._evict_failed_session(
                key,
                exc,
                credentialless=credentialless,
            )
            raise

    @_lifecycle_mutation
    async def prepare_authority_context(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
    ) -> _PreparedAuthorityContext:
        """Revalidate one live identity in the worker without returning a client."""

        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        async with self._lifecycle_lock(key):
            record = self._installed(plugin_id, app_id)
            requirement = self._verified_requirement(plugin_id, record)
            descriptor = self._descriptor(plugin_id, app_id)
            return await self._prepare_authority_context_locked(
                plugin_id,
                app_id=app_id,
                account_key=account_key,
                account=account,
                requirement=requirement,
                descriptor=descriptor,
            )

    @_lifecycle_mutation
    async def prepare_execution_worker(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        capability_kind: str,
        capability_id: str,
    ) -> PluginExecutionWorkerPreparation:
        """Resolve one current authority without executing the provider operation."""

        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        async with self._lifecycle_lock(key):
            record = self._installed(plugin_id, app_id)
            requirement = self._verified_requirement(plugin_id, record)
            descriptor = self._descriptor(plugin_id, app_id)
            prepared_context = await self._prepare_authority_context_locked(
                plugin_id,
                app_id=app_id,
                account_key=account_key,
                account=account,
                requirement=requirement,
                descriptor=descriptor,
            )
            local_execution: _LocalMcpWorkerExecution | None = None
            oauth_binding: BrokerTransportBinding | None = None
            if descriptor.deployment is McpClientDeployment.LOCAL:
                try:
                    connection_authority = self.authority_store.connection(
                        plugin_id,
                        app_id,
                        account,
                    )
                except ValueError:
                    raise ValueError("plugin_connection_not_live") from None
                (
                    descriptor,
                    local_execution,
                    package_revision,
                    connection_revision,
                    authority_revision,
                    capability_ceiling,
                    allowed_tool_schemas,
                ) = self._local_worker_material(
                    plugin_id=plugin_id,
                    record=record,
                    descriptor=descriptor,
                    connection_authority=connection_authority,
                )
                credential: dict[str, object] = {}
            else:
                grant = self._grants.current(
                    plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                )
                credential = {}
                if requirement.authentication.kind is PluginAuthenticationKind.NONE:
                    try:
                        connection_authority = self.authority_store.connection(
                            plugin_id,
                            app_id,
                            account,
                        )
                    except ValueError:
                        raise ValueError("plugin_connection_not_live") from None
                    durable = self._credentialless_authority_is_current(
                        connection_authority,
                        plugin_id=plugin_id,
                        descriptor=descriptor,
                    )
                    if durable is None:
                        raise ValueError("plugin_connection_not_live")
                    connection_revision = connection_authority.connection_revision
                    authority_revision = connection_authority.revision
                    package_revision = self._package_revision(plugin_id)
                    capability_ceiling = connection_authority.capability_ceiling
                    allowed_tool_schemas = connection_authority.allowed_tool_schemas
                else:
                    if not self._durable_grant_is_current(grant, descriptor):
                        raise ValueError("plugin_connection_not_live")
                    assert grant is not None
                    activation = grant.activation
                    oauth_binding = self._grant_binding(grant, descriptor)
                    connection_revision = activation.connection_revision
                    authority_revision = activation.authority_revision
                    package_revision = activation.package_revision
                    capability_ceiling = activation.capability_ceiling
                    allowed_tool_schemas = activation.allowed_tool_schemas

            if descriptor.deployment is McpClientDeployment.REMOTE and local_execution is not None:
                raise ValueError("plugin_connection_not_live")

            signed = descriptor.signed_capability_authority
            if not isinstance(signed, Mapping):
                raise ValueError("plugin_capability_invalid")
            declared_key = f"{capability_kind}:{capability_id}"
            if capability_kind == "tool":
                policy = signed.get("tools")
                allowed = (
                    declared_key in capability_ceiling
                    and capability_id in allowed_tool_schemas
                    and isinstance(policy, Mapping)
                    and capability_id in policy
                )
            elif capability_kind == "resource":
                policy = signed.get("resources")
                allowed = isinstance(policy, Mapping) and capability_id in policy
            elif capability_kind == "resource_template":
                policy = signed.get("resource_templates")
                allowed = isinstance(policy, Mapping) and capability_id in policy
            elif capability_kind == "prompt":
                policy = signed.get("prompts")
                allowed = isinstance(policy, Mapping) and capability_id in policy
            else:
                raise ValueError("plugin_capability_invalid")
            if not allowed or (
                declared_key not in descriptor.declared_capabilities
                and not (
                    capability_kind == "resource_template"
                    and f"resource:{capability_id}" in descriptor.declared_capabilities
                )
            ):
                raise ValueError("plugin_capability_invalid")
            if str(package_revision) != prepared_context.package_revision:
                raise ValueError("plugin_connection_not_live")
            execution_authority = PluginExecutionAuthority(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account,
                package_revision=package_revision,
                connection_revision=connection_revision,
                authority_revision=authority_revision,
                capability_revision=descriptor.capability_revision,
                capability_kind=capability_kind,
                capability_id=capability_id,
                server_id=prepared_context.server_id,
                server_version=prepared_context.server_version,
            )
            if oauth_binding is not None:
                issue = self._broker._issue_execution_worker_access_token
                issued = issue(
                    oauth_binding,
                    self._transport_authority,
                    execution_worker=self._execution_worker,
                    worker_authority=execution_authority,
                    timeout_seconds=max(
                        1.0,
                        descriptor.limits.max_connection_duration_ms / 1_000,
                    ),
                )
                token = await issued if inspect.isawaitable(issued) else issued
                credential = {"access_token": token}
            return PluginExecutionWorkerPreparation(
                authority=execution_authority,
                descriptor=descriptor,
                credential=credential,
                local_execution=local_execution,
            )

    async def _ensure_client_locked(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
    ) -> CurrentMcpClient:
        record = self._installed(plugin_id, app_id)
        requirement = self._verified_requirement(plugin_id, record)
        descriptor = self._descriptor(plugin_id, app_id)
        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        active = self._sessions.get(key)
        grant = self._grants.current(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account,
        )
        if descriptor.deployment is McpClientDeployment.LOCAL:
            del record
            if active is None:
                if self._states.get(key) == "installed_disconnected":
                    raise ValueError("plugin_connection_not_live")
                await self._start_locked(
                    plugin_id,
                    app_id=app_id,
                    account_key=account_key,
                    **self._durable_credentialless_consent(
                        plugin_id, descriptor=descriptor, account=account
                    ),
                )
                active = self._sessions.get(key)
            if active is None or not active.tools:
                raise ValueError("plugin_connection_not_live")
            try:
                await self._refresh_active(active, descriptor, None)
            except BaseException as exc:
                await self._evict_failed_session(
                    key,
                    exc,
                    credentialless=True,
                )
                raise
            if type(active.client) is _WorkerOnlyLocalClient:
                raise ValueError("plugin_local_connection_unavailable")
            return active.client
        if requirement.authentication.kind is PluginAuthenticationKind.NONE:
            if active is None:
                await self._start_locked(
                    plugin_id,
                    app_id=app_id,
                    account_key=account_key,
                    **self._durable_credentialless_consent(
                        plugin_id, descriptor=descriptor, account=account
                    ),
                )
                active = self._sessions.get(key)
            if active is None or active.credentialless_authority is None:
                raise ValueError("plugin_connection_not_live")
            try:
                await self._refresh_active(active, descriptor, None)
                stored = self.authority_store.connection(plugin_id, app_id, account)
            except BaseException as exc:
                await self._evict_failed_session(
                    key,
                    exc,
                    credentialless=True,
                )
                raise
            if stored != active.credentialless_authority:
                await self._evict_session(
                    key,
                    state="permission_changed",
                    suspend_grant=False,
                )
                raise ValueError("plugin_connection_drift")
            return active.client
        if grant is None:
            raise ValueError("plugin_authorization_required")
        if self._active_grant_is_current(active, grant, descriptor):
            assert active is not None
            try:
                await self._refresh_active(active, descriptor, grant)
            except BaseException as exc:
                await self._evict_failed_session(
                    key,
                    exc,
                    credentialless=False,
                )
                raise
            return active.client
        try:
            if active is not None:
                await self._evict_session(
                    key,
                    state="installed_disconnected",
                    suspend_grant=False,
                )
            active = await self._start_committed(grant, descriptor)
        except BaseException as exc:
            await self._evict_failed_session(
                key,
                exc,
                credentialless=False,
            )
            raise
        self._sessions[key] = active
        self._states[key] = "connected"
        return active.client

    @_lifecycle_mutation
    async def revoke(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        expected_registry_revision: str | None = None,
    ) -> None:
        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        async with self._lifecycle_lock(key):
            _plugin, _record, requirement, _package_revision, _registry_revision = (
                self._mutation_definition(
                    plugin_id,
                    app_id,
                    expected_registry_revision=expected_registry_revision,
                )
            )
            descriptor = self._descriptor(plugin_id, app_id)
            if descriptor.deployment is McpClientDeployment.LOCAL or (
                descriptor.deployment is McpClientDeployment.REMOTE
                and requirement.authentication.kind is PluginAuthenticationKind.NONE
            ):
                self._states[key] = "installed_disconnected"
                active = self._sessions.pop(key, None)
                # Local and auth-none remote connections share the same
                # durable credentialless authority boundary.  Revocation must
                # remove that authority for both deployments or a restart can
                # resurrect a locally disconnected provider.
                self.authority_store.invalidate_connection(plugin_id, app_id, account)
                if active is not None:
                    active.tools = ()
                    active.capability_binding_identities = ()
                    active.grant = None
                    active.binding = None
                    await self._submit(active.client.close)
                return
            # A connection can be cancelled before a grant exists. Consume the
            # durable attempt first so a late callback or restart cannot revive
            # it, then remove only this connection's recovered in-memory view.
            self._broker.discard_connection_attempts(
                plugin_id, app_id=app_id, account_key_sha256=account,
            )
            for state, prepared in tuple(self._prepared_attempts.items()):
                if (prepared.plugin_id, prepared.app_id, prepared.account_key_sha256) == key:
                    self._prepared_attempts.pop(state)
            try:
                pending = self._grants.begin_disconnect(
                    plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                )
            except ValueError as exc:
                if str(exc) != "plugin_grant_not_found":
                    raise
                pending = None
            self._states[key] = "installed_disconnected"
            active = self._sessions.pop(key, None)
            if active is not None:
                active.tools = ()
                active.capability_binding_identities = ()
                active.grant = None
                active.binding = None
                await self._submit(active.client.close)
            if pending is not None:
                # A disconnect lets go: a provider that refuses the revocation
                # -- usually because the token already expired -- must not leave
                # the plugin installed with its credential still held.
                await self._grants.finish_disconnect(
                    pending,
                    tolerate_provider_refusal=True,
                )

    @_lifecycle_mutation
    async def retire_predecessor(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        previous_record: InstalledPluginRecord,
        expected_registry_revision: str,
    ) -> None:
        """Retire authority removed by an exact completed candidate promotion."""

        snapshot = self._plugin_registry.snapshot()
        current = next(
            (record for record in snapshot.records if record.plugin_id == plugin_id),
            None,
        )
        previous_binding = getattr(previous_record, "app_binding", None)
        current_binding = getattr(current, "app_binding", None)
        previous_apps = (
            ()
            if previous_binding is None
            else previous_binding.required_app_ids + previous_binding.optional_app_ids
        )
        current_apps = (
            ()
            if current_binding is None
            else current_binding.required_app_ids + current_binding.optional_app_ids
        )
        if (
            snapshot.revision != expected_registry_revision
            or type(previous_record) is not InstalledPluginRecord
            or type(current) is not InstalledPluginRecord
            or previous_record.plugin_id != plugin_id
            or previous_record.publisher_id != current.publisher_id
            or previous_record.selected_version == current.selected_version
            or app_id not in previous_apps
            or app_id in current_apps
        ):
            raise ValueError("plugin_registry_conflict")
        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        async with self._lifecycle_lock(key):
            try:
                pending = self._grants.begin_disconnect(
                    plugin_id,
                    app_id=app_id,
                    account_key_sha256=account,
                )
            except ValueError as exc:
                if str(exc) != "plugin_grant_not_found":
                    raise
                pending = None
            active = self._sessions.pop(key, None)
            self._states[key] = "installed_disconnected"
            if active is not None:
                active.tools = ()
                active.capability_binding_identities = ()
                active.grant = None
                active.binding = None
                await self._submit(active.client.close)
            if pending is not None:
                await self._grants.finish_disconnect(pending)
            with suppress(ValueError):
                self.authority_store.invalidate_connection(plugin_id, app_id, account)

    @_lifecycle_mutation
    async def invalidate_execution(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        state: str = "installed_disconnected",
    ) -> None:
        self._installed(plugin_id, app_id)
        account = _account_digest(account_key)
        key = (plugin_id, app_id, account)
        async with self._lifecycle_lock(key):
            await self._evict_session(
                key,
                state=state,
                suspend_grant=True,
            )

    @_lifecycle_mutation
    async def close_all(self) -> None:
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        for active in sessions:
            with suppress(BaseException):
                await self._submit(active.client.close)
        self._probe_bindings.clear()
        self._launch_bindings.clear()
        self._lifecycle_locks.clear()


_APP_STATE_BY_CONNECTION_STATUS = {
    "connected": PluginLifecycleState.CONNECTED,
    "authorizing": PluginLifecycleState.CONNECTING,
    "finalizing": PluginLifecycleState.CONNECTING,
    "connecting": PluginLifecycleState.CONNECTING,
    "installed_disconnected": PluginLifecycleState.SETUP_REQUIRED,
    "setup_required": PluginLifecycleState.SETUP_REQUIRED,
    "reauthorization_required": PluginLifecycleState.PERMISSION_CHANGED,
    "permission_changed": PluginLifecycleState.PERMISSION_CHANGED,
    "revocation_pending": PluginLifecycleState.REVOCATION_PENDING,
    "offline": PluginLifecycleState.OFFLINE,
    "blocked": PluginLifecycleState.BLOCKED,
    "incompatible": PluginLifecycleState.INCOMPATIBLE,
}
_ERROR_BY_APP_STATE = {
    PluginLifecycleState.SETUP_REQUIRED: "plugin_setup_required",
    PluginLifecycleState.OFFLINE: "plugin_offline",
    PluginLifecycleState.PERMISSION_CHANGED: "plugin_permission_changed",
    PluginLifecycleState.REVOCATION_PENDING: "plugin_revocation_pending",
    PluginLifecycleState.BLOCKED: "plugin_blocked",
    PluginLifecycleState.INCOMPATIBLE: "plugin_runtime_unsupported",
}
_REQUIRED_STATE_PRECEDENCE = (
    PluginLifecycleState.REVOCATION_PENDING,
    PluginLifecycleState.PERMISSION_CHANGED,
    PluginLifecycleState.BLOCKED,
    PluginLifecycleState.INCOMPATIBLE,
    PluginLifecycleState.OFFLINE,
    PluginLifecycleState.CONNECTING,
    PluginLifecycleState.SETUP_REQUIRED,
    PluginLifecycleState.CONNECTED,
)
_OAUTH_FLOW_BY_AUTHENTICATION = {
    PluginAuthenticationKind.OAUTH_AUTHORIZATION_CODE_PKCE: "authorization_code_pkce",
    PluginAuthenticationKind.OAUTH_DEVICE_CODE: "device_code",
    PluginAuthenticationKind.OAUTH_CLIENT_METADATA_DOCUMENT: "authorization_code_pkce",
}


class PluginConnectionCoordinator:
    """Aggregate exact installed app bindings behind one plugin lifecycle."""

    def __init__(self, *, plugin_registry: _PluginRegistry, connection_service: object) -> None:
        required = (
            getattr(plugin_registry, "verified_installed_snapshot", None),
            getattr(connection_service, "status", None),
            getattr(connection_service, "start", None),
            getattr(connection_service, "revoke", None),
        )
        if any(not callable(item) for item in required):
            raise ValueError("plugin_connection_coordinator_invalid")
        self._plugin_registry = plugin_registry
        self._connections = connection_service

    def _installed_definition(
        self,
        plugin_id: str,
    ) -> tuple[
        InstalledPluginRecord,
        PluginManifest,
        tuple[AppRequirement, ...],
        tuple[AppRequirement, ...],
        str,
        bool,
    ]:
        try:
            snapshot = self._plugin_registry.verified_installed_snapshot(plugin_id)
            record = getattr(snapshot, "record")
            verified = getattr(snapshot, "verified")
            manifest = getattr(verified, "manifest")
            registry_revision = getattr(snapshot, "revision")
            legacy_authority = bool(getattr(verified, "legacy_authority", False))
            binding = record.app_binding
            if (
                type(record) is not InstalledPluginRecord
                or type(manifest) is not PluginManifest
                or type(binding) is not PluginAppBinding
                or record.plugin_id != plugin_id
                or record.publisher_id != manifest.publisher_id
                or record.kind != manifest.kind.value
                or record.selected_version != manifest.plugin_version
                or record.status != "installed"
                or type(registry_revision) is not str
            ):
                raise ValueError
            required_ids = tuple(app.app_id for app in manifest.required_apps)
            optional_ids = tuple(app.app_id for app in manifest.optional_apps)
            if (
                set(binding.required_app_ids) != set(required_ids)
                or not set(binding.optional_app_ids).issubset(optional_ids)
            ):
                raise ValueError
            optional = tuple(
                app for app in manifest.optional_apps if app.app_id in binding.optional_app_ids
            )
        except (AttributeError, TypeError, ValueError):
            raise ValueError("plugin_not_installed") from None
        return (
            record,
            manifest,
            manifest.required_apps,
            optional,
            registry_revision,
            legacy_authority,
        )

    @staticmethod
    def _app_state(app: AppRequirement, required: bool, status: object) -> PluginAppState:
        raw_state = getattr(status, "status", None)
        state = _APP_STATE_BY_CONNECTION_STATUS.get(raw_state)
        if state is None:
            raise ValueError("plugin_connection_state_invalid")
        authority_revision = getattr(status, "authority_revision", "")
        if state is not PluginLifecycleState.CONNECTED:
            authority_revision = ""
        error_code = getattr(status, "error_code", None) or _ERROR_BY_APP_STATE.get(state)
        return PluginAppState(
            app_id=app.app_id,
            required=required,
            state=state,
            authority_revision=authority_revision,
            error_code=error_code,
        )

    @staticmethod
    def _state_from_error(app: AppRequirement, required: bool, exc: ValueError) -> PluginAppState:
        reason = str(exc)
        by_error = {
            "plugin_offline": PluginLifecycleState.OFFLINE,
            "plugin_transport_unavailable": PluginLifecycleState.OFFLINE,
            "plugin_permission_changed": PluginLifecycleState.PERMISSION_CHANGED,
            "plugin_connection_drift": PluginLifecycleState.PERMISSION_CHANGED,
            "plugin_revocation_pending": PluginLifecycleState.REVOCATION_PENDING,
            "plugin_runtime_unsupported": PluginLifecycleState.INCOMPATIBLE,
            "plugin_blocked": PluginLifecycleState.BLOCKED,
        }
        state = by_error.get(reason)
        if state is None:
            raise exc
        return PluginAppState(app.app_id, required, state, "", _ERROR_BY_APP_STATE[state])

    async def status(
        self,
        plugin_id: str,
        account_key: str | None,
    ) -> PluginLifecycleSnapshot:
        for _attempt in range(3):
            record, _manifest, required, optional, registry_revision, legacy_authority = (
                self._installed_definition(plugin_id)
            )
            apps: list[PluginAppState] = []
            for is_required, requirements in ((True, required), (False, optional)):
                for app in requirements:
                    if account_key is None:
                        projector = getattr(
                            self._connections,
                            "local_state_without_account",
                            None,
                        )
                        local_state = (
                            None
                            if not callable(projector)
                            else projector(plugin_id, app_id=app.app_id)
                        )
                        projected = (
                            PluginLifecycleState.SETUP_REQUIRED
                            if local_state is None
                            else _APP_STATE_BY_CONNECTION_STATUS.get(local_state)
                        )
                        if projected is None:
                            raise ValueError("plugin_connection_state_invalid")
                        apps.append(
                            PluginAppState(
                                app.app_id,
                                is_required,
                                projected,
                                "",
                                _ERROR_BY_APP_STATE[projected],
                            )
                        )
                        continue
                    try:
                        connection_status = await self._connections.status(
                            plugin_id,
                            app_id=app.app_id,
                            account_key=account_key,
                        )
                    except ValueError as exc:
                        apps.append(self._state_from_error(app, is_required, exc))
                    else:
                        apps.append(self._app_state(app, is_required, connection_status))
            if self._installed_definition(plugin_id)[4] != registry_revision:
                continue
            required_states = {app.state for app in apps if app.required}
            state = next(
                candidate
                for candidate in _REQUIRED_STATE_PRECEDENCE
                if candidate in required_states
                or (candidate is PluginLifecycleState.CONNECTED and not required_states)
            )
            error_code = _ERROR_BY_APP_STATE.get(state)
            next_required = next(
                (
                    app.app_id
                    for app in apps
                    if app.required and app.state is not PluginLifecycleState.CONNECTED
                ),
                None,
            )
            snapshot_options = {
                "plugin_id": plugin_id,
                "registry_revision": registry_revision,
                "package_version": record.selected_version,
                "apps": tuple(apps),
                "next_required_app_id": next_required,
                "enabled": record.enabled,
            }
            if record.blocker is not None:
                return PluginLifecycleSnapshot._from_registry_blocker(
                    legacy_authority=legacy_authority,
                    **snapshot_options,
                )
            if legacy_authority:
                return PluginLifecycleSnapshot._from_legacy_authority(
                    **snapshot_options,
                )
            return PluginLifecycleSnapshot(
                state=state,
                error_code=error_code,
                **snapshot_options,
            )
        raise ValueError("plugin_registry_conflict")

    @_lifecycle_mutation
    async def start(
        self,
        plugin_id: str,
        app_id: str,
        account_key: str,
        redirect_uri: str | None,
        *,
        scopes: tuple[str, ...] | None = None,
        capability_ceiling: tuple[str, ...] | None = None,
        allowed_tool_schemas: Mapping[str, str] | None = None,
        execution_policy: PluginExecutionPolicy | str | None = None,
        source_constraints: Mapping[str, object] | None = None,
        destination_constraints: Mapping[str, object] | None = None,
    ) -> ConnectionStart:
        _record, _manifest, required, optional, revision, _legacy_authority = (
            self._installed_definition(plugin_id)
        )
        requirements = {app.app_id: app for app in (*required, *optional)}
        requirement = requirements.get(app_id)
        if requirement is None:
            raise ValueError("plugin_app_not_installed")
        if redirect_uri is not None:
            raise ValueError("plugin_redirect_uri_override_forbidden")
        authentication = requirement.authentication
        if authentication.kind not in {
            PluginAuthenticationKind.NONE,
            *_OAUTH_FLOW_BY_AUTHENTICATION,
        }:
            raise ValueError("plugin_secret_required")
        started = await self._connections.start(
            plugin_id,
            app_id=app_id,
            account_key=account_key,
            scopes=scopes,
            capability_ceiling=capability_ceiling,
            allowed_tool_schemas=allowed_tool_schemas,
            execution_policy=execution_policy,
            source_constraints=source_constraints,
            destination_constraints=destination_constraints,
            expected_registry_revision=revision,
        )
        if authentication.kind is PluginAuthenticationKind.NONE:
            if (
                started.status != "connected"
                or started.authorization_url is not None
                or started.request_id is not None
                or started.authorization_flow is not None
                or tuple(started.provider_scopes) != tuple(scopes or ())
                or tuple(started.capability_ceiling) != tuple(capability_ceiling or ())
            ):
                raise ValueError("plugin_authentication_mismatch")
            status = "connected"
        else:
            expected_flow = _OAUTH_FLOW_BY_AUTHENTICATION[authentication.kind]
            if started.status == "connected":
                connected = await self._connections.status(
                    plugin_id, app_id=app_id, account_key=account_key
                )
                if (
                    any(
                        value is not None
                        for value in (
                            started.request_id,
                            started.authorization_url,
                            started.expires_at,
                            started.authorization_flow,
                            started.user_code,
                            started.verification_uri,
                            started.poll_interval_seconds,
                        )
                    )
                    or connected.status != "connected"
                    or not connected.authority_revision
                    or connected.authorization_context_revision
                    != started.authorization_context_revision
                    or tuple(sorted(connected.provider_scopes))
                    != tuple(sorted(started.provider_scopes))
                    or tuple(sorted(started.provider_scopes))
                    != tuple(sorted(scopes or ()))
                    or tuple(sorted(connected.capability_ceiling))
                    != tuple(sorted(capability_ceiling or ()))
                ):
                    raise ValueError("plugin_authentication_mismatch")
                return ConnectionStart(
                    plugin_id=plugin_id,
                    app_id=app_id,
                    status="connected",
                    request_id=None,
                    authorization_url=None,
                    expires_at=None,
                    authorization_flow=None,
                    user_code=None,
                    verification_uri=None,
                    poll_interval_seconds=None,
                )
            if (
                started.status not in {"authorizing", "finalizing"}
                or started.authorization_flow != expected_flow
                # OAuth scopes are unordered. The connection service
                # canonicalizes them; signed manifests preserve author order.
                or tuple(sorted(started.provider_scopes))
                != tuple(sorted(scopes or ()))
                or tuple(sorted(started.capability_ceiling))
                != tuple(sorted(capability_ceiling or ()))
            ):
                raise ValueError("plugin_authentication_mismatch")
            status = "connecting"
        return ConnectionStart(
            plugin_id=plugin_id,
            app_id=app_id,
            status=status,
            request_id=started.request_id,
            authorization_url=started.authorization_url,
            expires_at=started.expires_at,
            authorization_flow=started.authorization_flow,
            user_code=started.user_code,
            verification_uri=started.verification_uri,
            poll_interval_seconds=started.poll_interval_seconds,
        )

    @_lifecycle_mutation
    async def disconnect(self, plugin_id: str, account_key: str) -> PluginLifecycleSnapshot:
        _record, _manifest, required, optional, revision, _legacy_authority = (
            self._installed_definition(plugin_id)
        )
        for app in (*required, *optional):
            await self._connections.revoke(
                plugin_id,
                app_id=app.app_id,
                account_key=account_key,
                expected_registry_revision=revision,
            )
        return await self.status(plugin_id, account_key)


__all__ = [
    "AuthorizationStart",
    "CandidateConnectionProbe",
    "ConnectionStatus",
    "PluginConnectionCoordinator",
    "PluginConnectionService",
    "PluginExecutionWorkerPreparation",
    "descriptor_for_record",
    "locally_sealed_capability_authority",
    "signed_capability_authority_for_candidate",
    "signed_capability_authority_from_package",
]
