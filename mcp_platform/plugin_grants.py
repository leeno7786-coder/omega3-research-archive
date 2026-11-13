"""Provider-neutral durable OAuth grants for signed plugin MCP connections."""

from __future__ import annotations

import json
import inspect
import re
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_broker import (
    OAuthBroker,
    OAuthConnectionCommit,
    OAuthPreparedAuthorization,
)
from mcp_platform.oauth_vault import (
    OAuthCommittedGrant,
    OAuthGrantReceiptClaimOutcome,
)
from mcp_platform.plugin_authority import (
    PluginAuthorityStore,
    PluginCapabilityBindingIdentity,
    PluginConnectionAuthority,
    PluginConnectionAuthorityKind,
    PluginExecutionPolicy,
    plugin_connection_authority_revision,
    plugin_connection_binding_revision,
)
from mcp_platform.state_store import McpStateStore


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PROFILE_REVISION = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_DOCUMENT_BYTES = 1_048_576
_MAX_RECORDS = 512
_SCHEMA_VERSION = 6


def _lifecycle_mutation(method: Callable[..., object]) -> Callable[..., object]:
    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def guarded_async(
            self: object,
            *args: object,
            **kwargs: object,
        ) -> object:
            async_guard = getattr(self, "_lifecycle_mutation_async_guard", None)
            if callable(async_guard):
                async with async_guard():
                    return await method(self, *args, **kwargs)
            guard = getattr(self, "_lifecycle_mutation_guard", None)
            scope = nullcontext() if not callable(guard) else guard()
            with scope:
                return await method(self, *args, **kwargs)

        return guarded_async

    @wraps(method)
    def guarded(self: object, *args: object, **kwargs: object) -> object:
        guard = getattr(self, "_lifecycle_mutation_guard", None)
        scope = nullcontext() if not callable(guard) else guard()
        with scope:
            return method(self, *args, **kwargs)

    return guarded


class _PermissionStore(Protocol):
    def invalidate_plugin(self, plugin_id: str) -> int: ...


def _text(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("plugin_grant_invalid")
    return value


def _digest(value: object) -> str:
    value = _text(value)
    if _DIGEST.fullmatch(value) is None:
        raise ValueError("plugin_grant_invalid")
    return value


def _profile_revision(value: object) -> str:
    value = _text(value)
    if _PROFILE_REVISION.fullmatch(value) is None:
        raise ValueError("plugin_grant_invalid")
    return value


def _scopes(value: object) -> tuple[str, ...]:
    if type(value) not in {tuple, list}:
        raise ValueError("plugin_grant_invalid")
    result = tuple(sorted(_text(item) for item in value))
    if not result or len(result) != len(set(result)) or len(result) > 128:
        raise ValueError("plugin_grant_invalid")
    return result


def _strings(value: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) not in {tuple, list}:
        raise ValueError("plugin_grant_invalid")
    result = tuple(_text(item) for item in value)
    if (not allow_empty and not result) or len(result) != len(set(result)):
        raise ValueError("plugin_grant_invalid")
    return result


def _plain_mapping(value: object, *, digest_values: bool = False) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError("plugin_grant_invalid")
    try:
        document = json.loads(canonical_bytes(dict(value)).decode("utf-8"))
    except (TypeError, ValueError):
        raise ValueError("plugin_grant_invalid") from None
    if digest_values:
        document = {_text(key): _digest(item) for key, item in document.items()}
    return document


@dataclass(frozen=True, slots=True)
class PluginGrantActivation:
    plugin_id: str
    app_id: str
    account_key_sha256: str
    profile_id: str
    profile_revision: str
    token_partition_key: str
    connection_revision: str
    capability_revision: str
    scopes: tuple[str, ...]
    capability_ceiling: tuple[str, ...]
    allowed_tool_schemas: Mapping[str, str]
    source_constraints: Mapping[str, object]
    destination_constraints: Mapping[str, object]
    capability_binding_identities: tuple[PluginCapabilityBindingIdentity, ...]
    execution_policy: PluginExecutionPolicy
    authorization_context_revision: str | None = None
    descriptor_revision: str | None = None
    package_revision: str | None = None
    attempt_state_sha256: str | None = None
    capability_binding_revision: str = field(init=False)
    authority_revision: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            policy = PluginExecutionPolicy(self.execution_policy)
            plugin_id = _text(self.plugin_id)
            app_id = _text(self.app_id)
            account = _digest(self.account_key_sha256)
            profile_id = _text(self.profile_id)
            profile_revision = _profile_revision(self.profile_revision)
            partition = _digest(self.token_partition_key)
            connection = _digest(self.connection_revision)
            capability = _digest(self.capability_revision)
            authorization_context = _digest(
                connection
                if self.authorization_context_revision is None
                else self.authorization_context_revision
            )
            descriptor_revision = _digest(
                capability if self.descriptor_revision is None else self.descriptor_revision
            )
            package_revision = _digest(
                capability if self.package_revision is None else self.package_revision
            )
            attempt_state = _digest(
                connection if self.attempt_state_sha256 is None else self.attempt_state_sha256
            )
            scopes = _scopes(self.scopes)
            ceiling = _strings(self.capability_ceiling, allow_empty=True)
            schemas = _plain_mapping(self.allowed_tool_schemas, digest_values=True)
            source = _plain_mapping(self.source_constraints)
            destination = _plain_mapping(self.destination_constraints)
            if (
                type(self.capability_binding_identities) is not tuple
                or not self.capability_binding_identities
                or any(
                    type(item) is not PluginCapabilityBindingIdentity
                    for item in self.capability_binding_identities
                )
            ):
                raise ValueError
            binding_identities = tuple(self.capability_binding_identities)
            ordering = tuple(
                (item.capability_kind, item.binding_sha256)
                for item in binding_identities
            )
            if ordering != tuple(sorted(ordering)) or len(ordering) != len(set(ordering)):
                raise ValueError
            capability_binding_revision = sha256_hex(
                canonical_bytes(
                    {
                        "capability_binding_identities": [
                            item.to_dict() for item in binding_identities
                        ]
                    }
                )
            )
            authority_revision = plugin_connection_authority_revision(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_hash=account,
                connection_revision=connection,
                provider_scopes=scopes,
                capability_ceiling=ceiling,
                allowed_tool_schemas=schemas,
                source_constraints=source,
                destination_constraints=destination,
                execution_policy=policy,
                authority_kind=PluginConnectionAuthorityKind.OAUTH,
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_grant_invalid") from None
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "app_id", app_id)
        object.__setattr__(self, "account_key_sha256", account)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "profile_revision", profile_revision)
        object.__setattr__(self, "token_partition_key", partition)
        object.__setattr__(self, "connection_revision", connection)
        object.__setattr__(self, "capability_revision", capability)
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "capability_ceiling", ceiling)
        object.__setattr__(self, "allowed_tool_schemas", MappingProxyType(schemas))
        object.__setattr__(self, "source_constraints", MappingProxyType(source))
        object.__setattr__(self, "destination_constraints", MappingProxyType(destination))
        object.__setattr__(self, "capability_binding_identities", binding_identities)
        object.__setattr__(self, "execution_policy", policy)
        object.__setattr__(self, "authorization_context_revision", authorization_context)
        object.__setattr__(self, "descriptor_revision", descriptor_revision)
        object.__setattr__(self, "package_revision", package_revision)
        object.__setattr__(self, "attempt_state_sha256", attempt_state)
        object.__setattr__(
            self,
            "capability_binding_revision",
            capability_binding_revision,
        )
        object.__setattr__(self, "authority_revision", authority_revision)
        plugin_connection_binding_revision(
            self.authority(),
            capability_revision=capability,
            capability_binding_revision=capability_binding_revision,
            capability_binding_identities=binding_identities,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "app_id": self.app_id,
            "account_key_sha256": self.account_key_sha256,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "token_partition_key": self.token_partition_key,
            "connection_revision": self.connection_revision,
            "capability_revision": self.capability_revision,
            "scopes": list(self.scopes),
            "capability_ceiling": list(self.capability_ceiling),
            "allowed_tool_schemas": dict(self.allowed_tool_schemas),
            "source_constraints": dict(self.source_constraints),
            "destination_constraints": dict(self.destination_constraints),
            "capability_binding_identities": [
                item.to_dict() for item in self.capability_binding_identities
            ],
            "execution_policy": self.execution_policy.value,
            "authorization_context_revision": self.authorization_context_revision,
            "descriptor_revision": self.descriptor_revision,
            "package_revision": self.package_revision,
            "attempt_state_sha256": self.attempt_state_sha256,
            "capability_binding_revision": self.capability_binding_revision,
            "authority_revision": self.authority_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginGrantActivation:
        if type(value) is not dict:
            raise ValueError("plugin_grant_invalid")
        document = dict(value)
        capability_binding_revision = document.pop("capability_binding_revision", None)
        authority_revision = document.pop("authority_revision", None)
        try:
            document["capability_binding_identities"] = tuple(
                PluginCapabilityBindingIdentity.from_dict(item)
                for item in document["capability_binding_identities"]
            )
            activation = cls(**document)
        except (KeyError, TypeError, ValueError):
            raise ValueError("plugin_grant_invalid") from None
        if (
            capability_binding_revision != activation.capability_binding_revision
            or authority_revision != activation.authority_revision
        ):
            raise ValueError("plugin_grant_invalid")
        return activation

    def authority(self) -> PluginConnectionAuthority:
        return PluginConnectionAuthority(
            plugin_id=self.plugin_id,
            app_id=self.app_id,
            account_key_hash=self.account_key_sha256,
            connection_revision=self.connection_revision,
            provider_scopes=self.scopes,
            capability_ceiling=self.capability_ceiling,
            allowed_tool_schemas=self.allowed_tool_schemas,
            source_constraints=self.source_constraints,
            destination_constraints=self.destination_constraints,
            execution_policy=self.execution_policy,
            authority_kind=PluginConnectionAuthorityKind.OAUTH,
            revision=self.authority_revision,
        )


def _legacy_oauth_authority_revision(activation: PluginGrantActivation) -> str:
    """Recompute the exact authority revision used before auth kinds were signed."""

    if type(activation) is not PluginGrantActivation:
        raise ValueError("plugin_grant_invalid")
    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": activation.plugin_id,
                "app_id": activation.app_id,
                "account_key_hash": activation.account_key_sha256,
                "connection_revision": activation.connection_revision,
                "provider_scopes": list(activation.scopes),
                "capability_ceiling": list(activation.capability_ceiling),
                "allowed_tool_schemas": dict(activation.allowed_tool_schemas),
                "source_constraints": dict(activation.source_constraints),
                "destination_constraints": dict(activation.destination_constraints),
                "execution_policy": activation.execution_policy.value,
            }
        )
    )


def _schema_two_activation(value: object) -> tuple[PluginGrantActivation, bool]:
    """Decode a current activation or one exact pre-auth-kind OAuth activation."""

    try:
        return PluginGrantActivation.from_dict(value), False
    except ValueError:
        pass
    if type(value) is not dict:
        raise ValueError("plugin_grant_invalid")
    document = dict(value)
    tool_binding_revision = document.pop("tool_binding_revision", None)
    tool_binding_digests = document.pop("tool_binding_digests", None)
    authority_revision = document.pop("authority_revision", None)
    try:
        if (
            type(tool_binding_digests) is not list
            or not tool_binding_digests
            or len(tool_binding_digests) != len(set(tool_binding_digests))
        ):
            raise ValueError
        digests = tuple(_digest(item) for item in tool_binding_digests)
        expected_tools = sha256_hex(
            canonical_bytes({"tool_binding_digests": list(digests)})
        )
        if tool_binding_revision != expected_tools:
            raise ValueError
        document["capability_binding_identities"] = (
            PluginCapabilityBindingIdentity(
                capability_kind="tool",
                binding_sha256=expected_tools,
            ),
        )
        activation = PluginGrantActivation(**document)
    except (TypeError, ValueError):
        raise ValueError("plugin_grant_invalid") from None
    if authority_revision == activation.authority_revision:
        return activation, False
    if authority_revision != _legacy_oauth_authority_revision(activation):
        raise ValueError("plugin_grant_invalid")
    return activation, True


def _schema_five_receipt(
    value: object,
    activation: PluginGrantActivation | None = None,
) -> OAuthCommittedGrant:
    """Migrate one exact tool-only receipt into the generic current schema."""

    if type(value) is not dict or "tool_binding_revision" not in value:
        raise ValueError("plugin_grant_invalid")
    document = dict(value)
    tool_revision = _digest(document.pop("tool_binding_revision"))
    identity = PluginCapabilityBindingIdentity(
        capability_kind="tool",
        binding_sha256=tool_revision,
    )
    capability_revision = sha256_hex(
        canonical_bytes(
            {"capability_binding_identities": [identity.to_dict()]}
        )
    )
    if activation is not None:
        if activation.capability_binding_identities != (identity,):
            raise ValueError("plugin_grant_invalid")
        capability_revision = activation.capability_binding_revision
    document["capability_binding_revision"] = capability_revision
    return _receipt_from_dict(document)


def _schema_migration_receipt(
    value: object,
    activation: PluginGrantActivation | None = None,
) -> OAuthCommittedGrant:
    try:
        return _receipt_from_dict(value)
    except ValueError:
        return _schema_five_receipt(value, activation)


@dataclass(frozen=True, slots=True)
class PluginGrantRecord:
    receipt: OAuthCommittedGrant
    activation: PluginGrantActivation
    authority: PluginConnectionAuthority
    connection_generation: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt": _receipt_to_dict(self.receipt),
            "activation": self.activation.to_dict(),
            "authority": self.authority.to_dict(),
            "connection_generation": self.connection_generation,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginGrantRecord:
        if type(value) is not dict or set(value) != {
            "receipt",
            "activation",
            "authority",
            "connection_generation",
        }:
            raise ValueError("plugin_grant_invalid")
        try:
            receipt = _receipt_from_dict(value["receipt"])
            activation = PluginGrantActivation.from_dict(value["activation"])
            authority = PluginConnectionAuthority.from_dict(value["authority"])
            generation = value["connection_generation"]
            if (
                type(generation) is not int
                or generation < 0
                or generation != receipt.connection_generation
                or authority != activation.authority()
                or not PluginGrantService._receipt_matches(
                    receipt,
                    activation,
                    generation,
                )
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("plugin_grant_invalid") from None
        return cls(receipt, activation, authority, generation)


@dataclass(frozen=True, slots=True)
class PluginGrantDisconnect:
    provider_token_deleted: bool
    remaining_plugin_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginGrantRevocation:
    plugin_id: str
    app_id: str
    account_key_sha256: str
    token_partition_key: str | None
    connection_generation: int | None
    completed: PluginGrantDisconnect | None = None


def _receipt_to_dict(receipt: OAuthCommittedGrant) -> dict[str, object]:
    return {
        "receipt_id": receipt.receipt_id,
        "plugin_id": receipt.plugin_id,
        "app_id": receipt.app_id,
        "account_key_sha256": receipt.account_key_sha256,
        "profile_id": receipt.profile_id,
        "profile_revision": receipt.profile_revision,
        "token_partition_key": receipt.token_partition_key,
        "connection_revision": receipt.connection_revision,
        "capability_revision": receipt.capability_revision,
        "authority_revision": receipt.authority_revision,
        "capability_binding_revision": receipt.capability_binding_revision,
        "scopes": list(receipt.scopes),
        "execution_policy": receipt.execution_policy,
        "status": receipt.status,
        "claimed_authority_revision": receipt.claimed_authority_revision,
        "connection_generation": receipt.connection_generation,
    }


def _receipt_from_dict(value: object) -> OAuthCommittedGrant:
    if type(value) is not dict:
        raise ValueError("plugin_grant_invalid")
    try:
        receipt = OAuthCommittedGrant(**{**value, "scopes": tuple(value["scopes"])})
        _digest(receipt.receipt_id)
        _digest(receipt.account_key_sha256)
        _profile_revision(receipt.profile_revision)
        for digest in (
            receipt.token_partition_key,
            receipt.connection_revision,
            receipt.capability_revision,
            receipt.authority_revision,
            receipt.capability_binding_revision,
        ):
            _digest(digest)
        _text(receipt.plugin_id)
        _text(receipt.app_id)
        _text(receipt.profile_id)
        _scopes(receipt.scopes)
        PluginExecutionPolicy(receipt.execution_policy)
        if type(receipt.connection_generation) is not int or receipt.connection_generation < 0:
            raise ValueError
        if receipt.status != "active":
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("plugin_grant_invalid") from None
    return receipt


def _schema_four_receipt_binding_revision(receipt: OAuthCommittedGrant) -> str:
    if type(receipt) is not OAuthCommittedGrant:
        raise ValueError("plugin_grant_invalid")
    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": receipt.plugin_id,
                "app_id": receipt.app_id,
                "account_key_sha256": receipt.account_key_sha256,
                "profile_id": receipt.profile_id,
                "profile_revision": receipt.profile_revision,
                "token_partition_key": receipt.token_partition_key,
                "connection_revision": receipt.connection_revision,
                "capability_revision": receipt.capability_revision,
                "authority_revision": receipt.authority_revision,
                "tool_binding_revision": receipt.capability_binding_revision,
                "scopes": list(receipt.scopes),
                "execution_policy": receipt.execution_policy,
                "connection_generation": receipt.connection_generation,
            }
        )
    )


def _receipt_binding_revision(receipt: OAuthCommittedGrant) -> str:
    if type(receipt) is not OAuthCommittedGrant:
        raise ValueError("plugin_grant_invalid")
    return sha256_hex(
        canonical_bytes(
            {
                "receipt_id": receipt.receipt_id,
                "plugin_id": receipt.plugin_id,
                "app_id": receipt.app_id,
                "account_key_sha256": receipt.account_key_sha256,
                "profile_id": receipt.profile_id,
                "profile_revision": receipt.profile_revision,
                "token_partition_key": receipt.token_partition_key,
                "connection_revision": receipt.connection_revision,
                "capability_revision": receipt.capability_revision,
                "authority_revision": receipt.authority_revision,
                "capability_binding_revision": receipt.capability_binding_revision,
                "scopes": list(receipt.scopes),
                "execution_policy": receipt.execution_policy,
                "connection_generation": receipt.connection_generation,
            }
        )
    )


def _activation_receipt_binding_revision(
    activation: PluginGrantActivation,
    generation: int,
    authority_revision: str,
    receipt_id: str,
) -> str:
    if (
        type(activation) is not PluginGrantActivation
        or type(generation) is not int
        or generation < 0
    ):
        raise ValueError("plugin_grant_invalid")
    return sha256_hex(
        canonical_bytes(
            {
                "receipt_id": _digest(receipt_id),
                "plugin_id": activation.plugin_id,
                "app_id": activation.app_id,
                "account_key_sha256": activation.account_key_sha256,
                "profile_id": activation.profile_id,
                "profile_revision": activation.profile_revision,
                "token_partition_key": activation.token_partition_key,
                "connection_revision": activation.connection_revision,
                "capability_revision": activation.capability_revision,
                "authority_revision": _digest(authority_revision),
                "capability_binding_revision": activation.capability_binding_revision,
                "scopes": list(activation.scopes),
                "execution_policy": activation.execution_policy.value,
                "connection_generation": generation,
            }
        )
    )


def _activation_receipt(
    activation: PluginGrantActivation,
    generation: int,
    authority_revision: str,
    receipt_id: str,
) -> OAuthCommittedGrant:
    return OAuthCommittedGrant(
        receipt_id=_digest(receipt_id),
        plugin_id=activation.plugin_id,
        app_id=activation.app_id,
        account_key_sha256=activation.account_key_sha256,
        profile_id=activation.profile_id,
        profile_revision=activation.profile_revision,
        token_partition_key=activation.token_partition_key,
        connection_revision=activation.connection_revision,
        capability_revision=activation.capability_revision,
        authority_revision=_digest(authority_revision),
        capability_binding_revision=activation.capability_binding_revision,
        scopes=activation.scopes,
        execution_policy=activation.execution_policy.value,
        connection_generation=generation,
    )


def _authorization_receipt_id(connection_revision: str, state_sha256: str) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "connection_revision": _digest(connection_revision),
                "state_sha256": _digest(state_sha256),
            }
        )
    )


class PluginGrantService:
    """Commit, publish, reconcile, and revoke provider-neutral plugin grants."""

    def __init__(
        self,
        *,
        broker: OAuthBroker,
        authority_store: PluginAuthorityStore,
        permission_store: _PermissionStore,
        journal_path: Path,
    ) -> None:
        if (
            not all(
                callable(getattr(broker, name, None))
                for name in (
                    "commit",
                    "unclaimed_grant_receipts",
                    "committed_grant_receipts",
                    "grant_receipt_state",
                    "claim_grant_receipt_exact",
                    "claim_grant_receipt",
                    "maximum_connection_generation",
                    "revoke",
                )
            )
            or type(authority_store) is not PluginAuthorityStore
        ):
            raise ValueError("plugin_grant_configuration_invalid")
        if not callable(getattr(permission_store, "invalidate_plugin", None)):
            raise ValueError("plugin_grant_configuration_invalid")
        path = Path(journal_path)
        self.broker = broker
        self.authority_store = authority_store
        self.permission_store = permission_store
        self.journal_path = path
        self._state = McpStateStore(
            state_root=path.parent,
            document_path=path,
            invalid_path_reason="plugin_grant_store_unavailable",
            max_document_bytes=_MAX_DOCUMENT_BYTES,
        )

    @staticmethod
    def _empty() -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "staged": {},
            "active": {},
            "generations": {},
            "disconnected": {},
            "retired_legacy": {},
        }

    def _upgrade_v1(self, document: dict[str, object]) -> dict[str, object]:
        upgraded = self._empty()
        active_identities: set[str] = set()
        for key, value in document["active"].items():
            activation, legacy_authority = _schema_two_activation(value["activation"])
            if legacy_authority:
                raise ValueError("plugin_grant_store_unavailable")
            identity = self._key(
                activation.plugin_id,
                activation.app_id,
                activation.account_key_sha256,
            )
            if key != identity:
                raise ValueError("plugin_grant_store_unavailable")
            receipt = _schema_migration_receipt(value["receipt"], activation)
            broker_floor = self.broker.maximum_connection_generation(
                activation.plugin_id,
                app_id=activation.app_id,
                account_key_sha256=activation.account_key_sha256,
            )
            generation_floor = max(
                receipt.connection_generation,
                receipt.connection_generation if broker_floor is None else broker_floor,
            )
            upgraded["active"][key] = {
                "generation": receipt.connection_generation,
                "receipt": _receipt_to_dict(receipt),
                "activation": activation.to_dict(),
                "eligible": receipt.connection_generation == generation_floor,
                "revocation_pending": False,
            }
            upgraded["generations"][key] = generation_floor
            active_identities.add(key)
        for state, value in document["staged"].items():
            activation, legacy_authority = _schema_two_activation(value)
            if legacy_authority:
                raise ValueError("plugin_grant_store_unavailable")
            identity = self._key(
                activation.plugin_id,
                activation.app_id,
                activation.account_key_sha256,
            )
            broker_floor = self.broker.maximum_connection_generation(
                activation.plugin_id,
                app_id=activation.app_id,
                account_key_sha256=activation.account_key_sha256,
            )
            floor = 0 if broker_floor is None else broker_floor
            upgraded["staged"][state] = {
                "generation": 0,
                "activation": activation.to_dict(),
                "retired": identity in active_identities or floor > 0,
            }
            upgraded["generations"][identity] = max(
                upgraded["generations"].get(identity, 0),
                floor,
            )
        return upgraded

    @staticmethod
    def _receipt_matches_authority_revision(
        receipt: OAuthCommittedGrant,
        activation: PluginGrantActivation,
        generation: int,
        authority_revision: str,
    ) -> bool:
        return (
            type(receipt) is OAuthCommittedGrant
            and receipt.plugin_id == activation.plugin_id
            and receipt.app_id == activation.app_id
            and receipt.account_key_sha256 == activation.account_key_sha256
            and receipt.profile_id == activation.profile_id
            and receipt.profile_revision == activation.profile_revision
            and receipt.token_partition_key == activation.token_partition_key
            and receipt.connection_revision == activation.connection_revision
            and receipt.capability_revision == activation.capability_revision
            and receipt.authority_revision == authority_revision
            and receipt.capability_binding_revision == activation.capability_binding_revision
            and receipt.scopes == activation.scopes
            and receipt.execution_policy == activation.execution_policy.value
            and receipt.status == "active"
            and receipt.connection_generation == generation
        )

    def _retire_legacy_activation(
        self,
        document: dict[str, object],
        activation: PluginGrantActivation,
        generation: int,
        receipt_id: str,
    ) -> None:
        identity = self._key(
            activation.plugin_id,
            activation.app_id,
            activation.account_key_sha256,
        )
        revision = _legacy_oauth_authority_revision(activation)
        receipt_binding = _activation_receipt_binding_revision(
            activation,
            generation,
            revision,
            receipt_id,
        )
        receipt = _activation_receipt(
            activation,
            generation,
            revision,
            receipt_id,
        )
        history = document["retired_legacy"].setdefault(identity, [])
        if any(item["generation"] == generation for item in history):
            raise ValueError("plugin_grant_store_unavailable")
        history.append(
            {
                "generation": generation,
                "authority_revision": revision,
                "receipt_binding_revision": receipt_binding,
                "receipt": _receipt_to_dict(receipt),
            }
        )
        history.sort(key=lambda item: item["generation"])
        document["generations"][identity] = max(
            generation,
            document["generations"].get(identity, 0),
        )

    def _upgrade_v2(self, document: dict[str, object]) -> dict[str, object]:
        if (
            set(document)
            != {"schema_version", "staged", "active", "generations", "disconnected"}
            or document.get("schema_version") != 2
            or type(document.get("staged")) is not dict
            or type(document.get("active")) is not dict
            or type(document.get("generations")) is not dict
            or type(document.get("disconnected")) is not dict
            or len(document["staged"]) + len(document["active"])
            + len(document["disconnected"])
            > _MAX_RECORDS
        ):
            raise ValueError("plugin_grant_store_unavailable")
        upgraded = {
            "schema_version": 5,
            "staged": dict(document["staged"]),
            "active": dict(document["active"]),
            "generations": dict(document["generations"]),
            "disconnected": dict(document["disconnected"]),
            "retired_legacy": {},
        }
        for key, generation in upgraded["generations"].items():
            _digest(key)
            if type(generation) is not int or generation < 0:
                raise ValueError("plugin_grant_store_unavailable")
        for state, value in tuple(upgraded["staged"].items()):
            _digest(state)
            if (
                type(value) is not dict
                or set(value) != {"generation", "activation", "retired"}
                or type(value["generation"]) is not int
                or value["generation"] < 0
                or type(value["retired"]) is not bool
            ):
                raise ValueError("plugin_grant_store_unavailable")
            activation, legacy = _schema_two_activation(value["activation"])
            value["activation"] = activation.to_dict()
            if legacy:
                self._retire_legacy_activation(
                    upgraded,
                    activation,
                    value["generation"],
                    _authorization_receipt_id(
                        activation.connection_revision,
                        state,
                    ),
                )
                upgraded["staged"].pop(state)
        for key, value in tuple(upgraded["active"].items()):
            _digest(key)
            if (
                type(value) is not dict
                or set(value)
                != {
                    "generation",
                    "receipt",
                    "activation",
                    "eligible",
                    "revocation_pending",
                }
                or type(value["generation"]) is not int
                or value["generation"] < 0
                or type(value["eligible"]) is not bool
                or type(value["revocation_pending"]) is not bool
                or value["eligible"]
                and value["revocation_pending"]
            ):
                raise ValueError("plugin_grant_store_unavailable")
            activation, legacy = _schema_two_activation(value["activation"])
            receipt = _schema_migration_receipt(value["receipt"], activation)
            value["activation"] = activation.to_dict()
            value["receipt"] = _receipt_to_dict(receipt)
            identity = self._key(
                activation.plugin_id,
                activation.app_id,
                activation.account_key_sha256,
            )
            if key != identity or receipt.connection_generation != value["generation"]:
                raise ValueError("plugin_grant_store_unavailable")
            expected_revision = (
                _legacy_oauth_authority_revision(activation)
                if legacy
                else activation.authority_revision
            )
            if not self._receipt_matches_authority_revision(
                receipt,
                activation,
                value["generation"],
                expected_revision,
            ):
                raise ValueError("plugin_grant_store_unavailable")
            if legacy:
                self._retire_legacy_activation(
                    upgraded,
                    activation,
                    value["generation"],
                    receipt.receipt_id,
                )
                upgraded["active"].pop(key)
        return upgraded

    def _upgrade_v5(self, document: dict[str, object]) -> dict[str, object]:
        """Rewrite every durable tool-only binding into one generic schema."""

        if (
            set(document)
            != {
                "schema_version",
                "staged",
                "active",
                "generations",
                "disconnected",
                "retired_legacy",
            }
            or document.get("schema_version") != 5
            or any(
                type(document.get(name)) is not dict
                for name in (
                    "staged",
                    "active",
                    "generations",
                    "disconnected",
                    "retired_legacy",
                )
            )
        ):
            raise ValueError("plugin_grant_store_unavailable")
        upgraded = json.loads(canonical_bytes(document).decode("utf-8"))
        upgraded["schema_version"] = _SCHEMA_VERSION
        for value in upgraded["staged"].values():
            if type(value) is not dict or "activation" not in value:
                raise ValueError("plugin_grant_store_unavailable")
            activation, legacy_authority = _schema_two_activation(value["activation"])
            if legacy_authority:
                raise ValueError("plugin_grant_store_unavailable")
            value["activation"] = activation.to_dict()
        for value in upgraded["active"].values():
            if (
                type(value) is not dict
                or "activation" not in value
                or "receipt" not in value
            ):
                raise ValueError("plugin_grant_store_unavailable")
            activation, legacy_authority = _schema_two_activation(value["activation"])
            if legacy_authority:
                raise ValueError("plugin_grant_store_unavailable")
            receipt = _schema_migration_receipt(value["receipt"], activation)
            value["activation"] = activation.to_dict()
            value["receipt"] = _receipt_to_dict(receipt)
        for history in upgraded["retired_legacy"].values():
            if type(history) is not list:
                raise ValueError("plugin_grant_store_unavailable")
            for value in history:
                if type(value) is not dict or "receipt" not in value:
                    raise ValueError("plugin_grant_store_unavailable")
                receipt = _schema_migration_receipt(value["receipt"])
                value["receipt"] = _receipt_to_dict(receipt)
                value["receipt_binding_revision"] = _receipt_binding_revision(
                    receipt
                )
        return upgraded

    @staticmethod
    def _upgrade_v3(document: dict[str, object]) -> dict[str, object]:
        if (
            set(document)
            != {
                "schema_version",
                "staged",
                "active",
                "generations",
                "disconnected",
                "retired_legacy",
            }
            or document.get("schema_version") != 3
            or type(document.get("retired_legacy")) is not dict
        ):
            raise ValueError("plugin_grant_store_unavailable")
        upgraded = dict(document)
        upgraded["schema_version"] = 4
        upgraded["retired_legacy"] = {
            key: [value] for key, value in document["retired_legacy"].items()
        }
        return upgraded

    def _upgrade_v4(self, document: dict[str, object]) -> dict[str, object]:
        if (
            set(document)
            != {
                "schema_version",
                "staged",
                "active",
                "generations",
                "disconnected",
                "retired_legacy",
            }
            or document.get("schema_version") != 4
            or type(document.get("staged")) is not dict
            or type(document.get("active")) is not dict
            or type(document.get("generations")) is not dict
            or type(document.get("disconnected")) is not dict
            or type(document.get("retired_legacy")) is not dict
            or len(document["staged"])
            + len(document["active"])
            + len(document["disconnected"])
            + sum(
                len(history)
                for history in document["retired_legacy"].values()
                if type(history) is list
            )
            > _MAX_RECORDS
        ):
            raise ValueError("plugin_grant_store_unavailable")
        receipts = self.broker.committed_grant_receipts()
        if (
            type(receipts) is not tuple
            or len(receipts) > _MAX_RECORDS
            or any(type(receipt) is not OAuthCommittedGrant for receipt in receipts)
        ):
            raise ValueError("plugin_grant_store_unavailable")
        normalized_receipts = tuple(
            _receipt_from_dict(
                _receipt_to_dict(replace(receipt, claimed_authority_revision=None))
            )
            for receipt in receipts
            if receipt.status == "active"
            and receipt.claimed_authority_revision
            in {None, receipt.authority_revision}
        )
        upgraded = {
            "schema_version": 5,
            "staged": dict(document["staged"]),
            "active": dict(document["active"]),
            "generations": dict(document["generations"]),
            "disconnected": dict(document["disconnected"]),
            "retired_legacy": {},
        }
        for key, history in document["retired_legacy"].items():
            _digest(key)
            if (
                type(history) is not list
                or not history
                or any(type(value) is not dict for value in history)
            ):
                raise ValueError("plugin_grant_store_unavailable")
            previous_generation = -1
            for value in history:
                if (
                    set(value)
                    != {
                        "generation",
                        "authority_revision",
                        "receipt_binding_revision",
                    }
                    or type(value["generation"]) is not int
                    or value["generation"] < 0
                    or value["generation"] <= previous_generation
                ):
                    raise ValueError("plugin_grant_store_unavailable")
                authority_revision = _digest(value["authority_revision"])
                receipt_binding_revision = _digest(value["receipt_binding_revision"])
                previous_generation = value["generation"]
                candidates = tuple(
                    receipt
                    for receipt in normalized_receipts
                    if self._key(
                        receipt.plugin_id,
                        receipt.app_id,
                        receipt.account_key_sha256,
                    )
                    == key
                    and receipt.connection_generation == value["generation"]
                    and receipt.authority_revision == authority_revision
                    and _schema_four_receipt_binding_revision(receipt)
                    == receipt_binding_revision
                )
                if len(candidates) == 1:
                    receipt = candidates[0]
                    upgraded["retired_legacy"].setdefault(key, []).append(
                        {
                            "generation": receipt.connection_generation,
                            "authority_revision": receipt.authority_revision,
                            "receipt_binding_revision": _receipt_binding_revision(receipt),
                            "receipt": _receipt_to_dict(receipt),
                        }
                    )
            if document["generations"].get(key, -1) < previous_generation:
                raise ValueError("plugin_grant_store_unavailable")
        return upgraded

    def _read_locked(self) -> dict[str, object]:
        raw = self._state.read_bytes()
        if raw is None:
            return self._empty()
        try:
            document = json.loads(raw.decode("utf-8"))
            if type(document) is not dict:
                raise ValueError
            migrated = False
            if document.get("schema_version") == 1 and set(document) == {
                "schema_version",
                "staged",
                "active",
            }:
                document = self._upgrade_v1(document)
                migrated = True
            if document.get("schema_version") == 2:
                document = self._upgrade_v2(document)
                migrated = True
            if document.get("schema_version") == 3:
                document = self._upgrade_v3(document)
                migrated = True
            if document.get("schema_version") == 4:
                document = self._upgrade_v4(document)
                migrated = True
            if document.get("schema_version") == 5:
                document = self._upgrade_v5(document)
                migrated = True
            if (
                set(document)
                != {
                    "schema_version",
                    "staged",
                    "active",
                    "generations",
                    "disconnected",
                    "retired_legacy",
                }
                or document["schema_version"] != _SCHEMA_VERSION
                or type(document["staged"]) is not dict
                or type(document["active"]) is not dict
                or type(document["generations"]) is not dict
                or type(document["disconnected"]) is not dict
                or type(document["retired_legacy"]) is not dict
                or len(document["staged"]) + len(document["active"])
                + len(document["disconnected"])
                + sum(
                    len(history)
                    for history in document["retired_legacy"].values()
                    if type(history) is list
                )
                > _MAX_RECORDS
            ):
                raise ValueError
            for key, value in document["staged"].items():
                _digest(key)
                if (
                    type(value) is not dict
                    or set(value) != {"generation", "activation", "retired"}
                    or type(value["generation"]) is not int
                    or value["generation"] < 0
                    or type(value["retired"]) is not bool
                ):
                    raise ValueError
                activation = PluginGrantActivation.from_dict(value["activation"])
                identity = self._key(
                    activation.plugin_id,
                    activation.app_id,
                    activation.account_key_sha256,
                )
                if document["generations"].get(identity, -1) < value["generation"]:
                    raise ValueError
            for key, value in document["active"].items():
                _digest(key)
                if (
                    type(value) is not dict
                    or set(value)
                    != {
                        "generation",
                        "receipt",
                        "activation",
                        "eligible",
                        "revocation_pending",
                    }
                    or type(value["generation"]) is not int
                    or value["generation"] < 0
                    or type(value["eligible"]) is not bool
                    or type(value["revocation_pending"]) is not bool
                    or value["eligible"]
                    and value["revocation_pending"]
                ):
                    raise ValueError
                receipt = _receipt_from_dict(value["receipt"])
                activation = PluginGrantActivation.from_dict(value["activation"])
                identity = self._key(
                    activation.plugin_id,
                    activation.app_id,
                    activation.account_key_sha256,
                )
                if (
                    key != identity
                    or not self._receipt_matches(
                        receipt,
                        activation,
                        value["generation"],
                    )
                ):
                    raise ValueError
            for key, generation in document["generations"].items():
                _digest(key)
                if type(generation) is not int or generation < 0:
                    raise ValueError
            for key, value in document["disconnected"].items():
                _digest(key)
                if (
                    type(value) is not dict
                    or set(value)
                    != {
                        "plugin_id",
                        "app_id",
                        "account_key_sha256",
                        "provider_token_deleted",
                        "remaining_plugin_ids",
                    }
                    or type(value["provider_token_deleted"]) is not bool
                    or type(value["remaining_plugin_ids"]) is not list
                ):
                    raise ValueError
                _text(value["plugin_id"])
                _text(value["app_id"])
                _digest(value["account_key_sha256"])
                _strings(value["remaining_plugin_ids"], allow_empty=True)
            for key, history in document["retired_legacy"].items():
                _digest(key)
                if (
                    type(history) is not list
                    or not history
                    or any(type(value) is not dict for value in history)
                ):
                    raise ValueError
                previous_generation = -1
                for value in history:
                    if (
                        set(value)
                        != {
                            "generation",
                            "authority_revision",
                            "receipt_binding_revision",
                            "receipt",
                        }
                        or type(value["generation"]) is not int
                        or value["generation"] < 0
                        or value["generation"] <= previous_generation
                    ):
                        raise ValueError
                    _digest(value["authority_revision"])
                    _digest(value["receipt_binding_revision"])
                    receipt = _receipt_from_dict(value["receipt"])
                    if (
                        receipt.claimed_authority_revision is not None
                        or receipt.connection_generation != value["generation"]
                        or receipt.authority_revision != value["authority_revision"]
                        or _receipt_binding_revision(receipt)
                        != value["receipt_binding_revision"]
                        or self._key(
                            receipt.plugin_id,
                            receipt.app_id,
                            receipt.account_key_sha256,
                        )
                        != key
                    ):
                        raise ValueError
                    previous_generation = value["generation"]
                if document["generations"].get(key, -1) < previous_generation:
                    raise ValueError
            if migrated:
                self._write_locked(document)
            return document
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ValueError("plugin_grant_store_unavailable") from None

    def _write_locked(self, document: dict[str, object]) -> None:
        payload = canonical_bytes(document)
        if len(payload) > _MAX_DOCUMENT_BYTES:
            raise ValueError("plugin_grant_store_limit_exceeded")
        self._state.write_bytes(payload)

    @staticmethod
    def _key(plugin_id: str, app_id: str, account_key_sha256: str) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "plugin_id": _text(plugin_id),
                    "app_id": _text(app_id),
                    "account_key_sha256": _digest(account_key_sha256),
                }
            )
        )

    @staticmethod
    def _prepared_matches(
        prepared: OAuthPreparedAuthorization,
        activation: PluginGrantActivation,
    ) -> bool:
        return (
            type(prepared) is OAuthPreparedAuthorization
            and type(activation) is PluginGrantActivation
            and prepared.plugin_id == activation.plugin_id
            and prepared.app_id == activation.app_id
            and prepared.account_key_sha256 == activation.account_key_sha256
            and prepared.profile_id == activation.profile_id
            and prepared.profile_revision == activation.profile_revision
            and prepared.partition_key == activation.token_partition_key
            and prepared.scopes == activation.scopes
        )

    @staticmethod
    def _receipt_matches(
        receipt: OAuthCommittedGrant,
        activation: PluginGrantActivation,
        generation: int,
    ) -> bool:
        return (
            type(receipt) is OAuthCommittedGrant
            and receipt.plugin_id == activation.plugin_id
            and receipt.app_id == activation.app_id
            and receipt.account_key_sha256 == activation.account_key_sha256
            and receipt.profile_id == activation.profile_id
            and receipt.profile_revision == activation.profile_revision
            and receipt.token_partition_key == activation.token_partition_key
            and receipt.connection_revision == activation.connection_revision
            and receipt.capability_revision == activation.capability_revision
            and receipt.authority_revision == activation.authority_revision
            and receipt.capability_binding_revision == activation.capability_binding_revision
            and receipt.scopes == activation.scopes
            and receipt.execution_policy == activation.execution_policy.value
            and receipt.status == "active"
            and receipt.connection_generation == generation
        )

    def _stage(
        self,
        prepared: OAuthPreparedAuthorization,
        activation: PluginGrantActivation,
    ) -> int:
        if not self._prepared_matches(prepared, activation):
            raise ValueError("plugin_grant_binding_invalid")
        with self._state.locked():
            document = self._read_locked()
            current = document["staged"].get(prepared.state_sha256)
            encoded = activation.to_dict()
            if current is not None:
                if current["activation"] != encoded:
                    raise ValueError("plugin_grant_revision_conflict")
                return current["generation"]
            key = self._key(
                activation.plugin_id,
                activation.app_id,
                activation.account_key_sha256,
            )
            broker_floor = self.broker.maximum_connection_generation(
                activation.plugin_id,
                app_id=activation.app_id,
                account_key_sha256=activation.account_key_sha256,
            )
            if broker_floor is None and key not in document["generations"]:
                try:
                    self.authority_store.connection(
                        activation.plugin_id,
                        activation.app_id,
                        activation.account_key_sha256,
                    )
                except ValueError as exc:
                    if str(exc) != "plugin_connection_authority_not_found":
                        raise
                else:
                    raise ValueError("plugin_grant_recovery_invalid")
            generation = (
                max(
                    document["generations"].get(key, 0),
                    0 if broker_floor is None else broker_floor,
                )
                + 1
            )
            document["generations"][key] = generation
            retired_active = document["active"].get(key)
            should_invalidate = bool(retired_active is not None and retired_active["eligible"])
            if retired_active is not None:
                retired_active["eligible"] = False
            for raw in document["staged"].values():
                staged_activation = PluginGrantActivation.from_dict(raw["activation"])
                if (
                    self._key(
                        staged_activation.plugin_id,
                        staged_activation.app_id,
                        staged_activation.account_key_sha256,
                    )
                    == key
                ):
                    raw["retired"] = True
            document["staged"][prepared.state_sha256] = {
                "generation": generation,
                "activation": encoded,
                "retired": False,
            }
            document["disconnected"].pop(key, None)
            self._write_locked(document)
        if should_invalidate:
            self.authority_store.invalidate_connection(
                activation.plugin_id,
                activation.app_id,
                activation.account_key_sha256,
            )
            self.permission_store.invalidate_plugin(activation.plugin_id)
        return generation

    def _publish(
        self,
        receipt: OAuthCommittedGrant,
        activation: PluginGrantActivation,
        generation: int,
    ) -> PluginConnectionAuthority | None:
        if not self._receipt_matches(receipt, activation, generation):
            raise ValueError("plugin_grant_binding_invalid")
        authority = activation.authority()
        key = self._key(
            activation.plugin_id,
            activation.app_id,
            activation.account_key_sha256,
        )
        published = False
        with self._state.locked():
            document = self._read_locked()
            staged = tuple(
                raw
                for raw in document["staged"].values()
                if raw["generation"] == generation and raw["activation"] == activation.to_dict()
            )
            active = document["active"].get(key)
            current_generation = document["generations"].get(key)
            stale = current_generation != generation or any(item["retired"] for item in staged)
            if stale:
                for state, raw in tuple(document["staged"].items()):
                    if raw["generation"] == generation:
                        document["staged"].pop(state)
                self._write_locked(document)
            else:
                if not staged and not (
                    active is not None
                    and active["generation"] == generation
                    and active["activation"] == activation.to_dict()
                    and active["receipt"] == _receipt_to_dict(receipt)
                ):
                    raise ValueError("plugin_grant_recovery_invalid")
                try:
                    current = self.authority_store.connection(
                        activation.plugin_id,
                        activation.app_id,
                        activation.account_key_sha256,
                    )
                except ValueError as exc:
                    if str(exc) != "plugin_connection_authority_not_found":
                        raise
                    current = None
                if current is None:
                    self.authority_store.publish_connection(authority, expected_revision="")
                elif current != authority:
                    self.authority_store.publish_connection(
                        authority,
                        expected_revision=current.revision,
                    )
                    self.permission_store.invalidate_plugin(activation.plugin_id)
                document["active"][key] = {
                    "generation": generation,
                    "receipt": _receipt_to_dict(receipt),
                    "activation": activation.to_dict(),
                    "eligible": True,
                    "revocation_pending": False,
                }
                for state, raw in tuple(document["staged"].items()):
                    if raw["generation"] == generation:
                        document["staged"].pop(state)
                self._write_locked(document)
                published = True
        self.broker.claim_grant_receipt(receipt.receipt_id, authority.revision)
        return authority if published else None

    @_lifecycle_mutation
    def activate(
        self,
        prepared: OAuthPreparedAuthorization,
        activation: PluginGrantActivation,
    ) -> PluginConnectionAuthority:
        generation = self._stage(prepared, activation)
        receipt = self.broker.commit(
            prepared,
            OAuthConnectionCommit(
                connection_revision=activation.connection_revision,
                capability_revision=activation.capability_revision,
                authority_revision=activation.authority_revision,
                capability_binding_revision=activation.capability_binding_revision,
                execution_policy=activation.execution_policy.value,
                connection_generation=generation,
            ),
        )
        published = self._publish(receipt, activation, generation)
        if published is None:
            raise ValueError("plugin_grant_superseded")
        return published

    @staticmethod
    def _update_recovery(value: object) -> dict[str, object]:
        if type(value) is not dict or set(value) != {
            "checkpoint_id",
            "vault_revision",
            "plugin_id",
            "account_key_sha256",
            "app_ids",
            "predecessor_grants",
            "permission_recovery",
            "revision",
        }:
            raise ValueError("plugin_update_recovery_required")
        try:
            checkpoint_id = _text(value["checkpoint_id"])
            if not checkpoint_id.startswith("plugin-update-recovery-"):
                raise ValueError
            vault_revision = _digest(value["vault_revision"])
            plugin_id = _text(value["plugin_id"])
            account = _digest(value["account_key_sha256"])
            raw_apps = value["app_ids"]
            raw_grants = value["predecessor_grants"]
            permission_recovery = value["permission_recovery"]
            if type(raw_apps) is not list or type(raw_grants) is not list:
                raise ValueError
            if permission_recovery is not None and type(permission_recovery) is not dict:
                raise ValueError
            app_ids = tuple(_text(app_id) for app_id in raw_apps)
            if app_ids != tuple(sorted(app_ids)) or len(app_ids) != len(set(app_ids)):
                raise ValueError
            predecessors = tuple(PluginGrantRecord.from_dict(item) for item in raw_grants)
            if tuple(
                (item.activation.app_id, item.activation.connection_revision)
                for item in predecessors
            ) != tuple(
                sorted(
                    (
                        item.activation.app_id,
                        item.activation.connection_revision,
                    )
                    for item in predecessors
                )
            ) or any(
                item.activation.plugin_id != plugin_id
                or item.activation.account_key_sha256 != account
                or item.activation.app_id not in app_ids
                for item in predecessors
            ):
                raise ValueError
            material = {
                "checkpoint_id": checkpoint_id,
                "vault_revision": vault_revision,
                "plugin_id": plugin_id,
                "account_key_sha256": account,
                "app_ids": list(app_ids),
                "predecessor_grants": [item.to_dict() for item in predecessors],
                "permission_recovery": permission_recovery,
            }
            revision = sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.plugin-grant-update-recovery.v1",
                        "material": material,
                    }
                )
            )
            if value["revision"] != revision:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_update_recovery_required") from None
        return {**material, "revision": revision}

    @_lifecycle_mutation
    def begin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        plugin_id: str,
        account_key_sha256: str,
        app_ids: tuple[str, ...],
    ) -> dict[str, object]:
        plugin = _text(plugin_id)
        account = _digest(account_key_sha256)
        if type(app_ids) is not tuple:
            raise ValueError("plugin_update_recovery_required")
        apps = tuple(sorted(_text(app_id) for app_id in app_ids))
        if len(apps) != len(set(apps)):
            raise ValueError("plugin_update_recovery_required")
        checkpoint = self.broker.begin_plugin_update_recovery(
            checkpoint_id,
            plugin_id=plugin,
            account_key_sha256=account,
            app_ids=apps,
        )
        predecessors = tuple(
            sorted(
                (
                    current
                    for app_id in apps
                    for current in (
                        self.current(
                            plugin,
                            app_id=app_id,
                            account_key_sha256=account,
                        ),
                    )
                    if current is not None
                ),
                key=lambda item: (
                    item.activation.app_id,
                    item.activation.connection_revision,
                ),
            )
        )
        checkpoint_permissions = getattr(
            self.permission_store,
            "checkpoint_plugin",
            None,
        )
        permission_recovery = (
            None
            if not callable(checkpoint_permissions)
            else checkpoint_permissions(plugin)
        )
        material = {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "vault_revision": checkpoint["revision"],
            "plugin_id": plugin,
            "account_key_sha256": account,
            "app_ids": list(apps),
            "predecessor_grants": [item.to_dict() for item in predecessors],
            "permission_recovery": permission_recovery,
        }
        material["revision"] = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.plugin-grant-update-recovery.v1",
                    "material": material,
                }
            )
        )
        return self._update_recovery(material)

    @_lifecycle_mutation
    def restore_update_recovery(self, recovery: object) -> None:
        checked = self._update_recovery(recovery)
        plugin = str(checked["plugin_id"])
        account = str(checked["account_key_sha256"])
        app_ids = tuple(str(item) for item in checked["app_ids"])
        predecessors = tuple(
            PluginGrantRecord.from_dict(item)
            for item in checked["predecessor_grants"]
        )
        self.broker.restore_plugin_update_recovery(
            str(checked["checkpoint_id"]),
            expected_revision=str(checked["vault_revision"]),
        )
        with self._state.locked():
            document = self._read_locked()
            for state, raw in tuple(document["staged"].items()):
                activation = PluginGrantActivation.from_dict(raw["activation"])
                if (
                    activation.plugin_id == plugin
                    and activation.account_key_sha256 == account
                    and activation.app_id in app_ids
                ):
                    document["staged"].pop(state)
            for key, raw in tuple(document["active"].items()):
                receipt = _receipt_from_dict(raw["receipt"])
                if (
                    receipt.plugin_id == plugin
                    and receipt.account_key_sha256 == account
                    and receipt.app_id in app_ids
                ):
                    document["active"].pop(key)
            for predecessor in predecessors:
                activation = predecessor.activation
                key = self._key(plugin, activation.app_id, account)
                document["active"][key] = {
                    "generation": predecessor.connection_generation,
                    "receipt": _receipt_to_dict(predecessor.receipt),
                    "activation": activation.to_dict(),
                    "eligible": True,
                    "revocation_pending": False,
                }
                document["generations"][key] = predecessor.connection_generation
                document["disconnected"].pop(key, None)
            self._write_locked(document)
        for app_id in app_ids:
            try:
                current = self.authority_store.connection(plugin, app_id, account)
            except ValueError as exc:
                if str(exc) != "plugin_connection_authority_not_found":
                    raise
                current = None
            predecessor = next(
                (item for item in predecessors if item.activation.app_id == app_id),
                None,
            )
            if predecessor is None:
                if current is not None:
                    self.authority_store.invalidate_connection(plugin, app_id, account)
                continue
            if current != predecessor.authority:
                self.authority_store.publish_connection(
                    predecessor.authority,
                    expected_revision="" if current is None else current.revision,
                )
        permission_recovery = checked["permission_recovery"]
        if permission_recovery is not None:
            restore_permissions = getattr(
                self.permission_store,
                "restore_plugin_checkpoint",
                None,
            )
            if not callable(restore_permissions):
                raise ValueError("plugin_update_recovery_required")
            restore_permissions(permission_recovery)

    @_lifecycle_mutation
    def discard_update_recovery_checkpoint(
        self,
        checkpoint_id: str,
        *,
        plugin_id: str,
        account_key_sha256: str,
    ) -> None:
        self.broker.discard_plugin_update_recovery(
            checkpoint_id,
            plugin_id=_text(plugin_id),
            account_key_sha256=_digest(account_key_sha256),
        )

    @_lifecycle_mutation
    def finalize_update_recovery(self, recovery: object) -> None:
        checked = self._update_recovery(recovery)
        self.broker.finalize_plugin_update_recovery(
            str(checked["checkpoint_id"]),
            expected_revision=str(checked["vault_revision"]),
        )

    @_lifecycle_mutation
    def reconcile(self) -> tuple[PluginConnectionAuthority, ...]:
        reconcile_authority = getattr(self.authority_store, "reconcile", None)
        if callable(reconcile_authority):
            reconcile_authority()
        with self._state.locked():
            document = self._read_locked()
            recoverable = tuple(
                (value["generation"], PluginGrantActivation.from_dict(value["activation"]))
                for _key, value in sorted(document["staged"].items())
            ) + tuple(
                (value["generation"], PluginGrantActivation.from_dict(value["activation"]))
                for _key, value in sorted(document["active"].items())
            )
            retired_legacy = {
                key: tuple(
                    {**item, "receipt": dict(item["receipt"])} for item in history
                )
                for key, history in document["retired_legacy"].items()
            }
        published: list[PluginConnectionAuthority] = []
        confirmed: dict[str, list[dict[str, object]]] = {}
        failure: BaseException | None = None
        try:
            for key, history in retired_legacy.items():
                for item in history:
                    receipt = _receipt_from_dict(item["receipt"])
                    outcome = self._claim_retired_receipt(receipt)
                    if outcome is OAuthGrantReceiptClaimOutcome.ABSENT:
                        continue
                    if outcome not in {
                        OAuthGrantReceiptClaimOutcome.CLAIMED,
                        OAuthGrantReceiptClaimOutcome.IDEMPOTENTLY_CLAIMED,
                    }:
                        raise ValueError("plugin_grant_recovery_invalid")
                    confirmed.setdefault(key, []).append(item)

            for receipt in self.broker.unclaimed_grant_receipts():
                matches = {
                    (generation, item.authority_revision): item
                    for generation, item in recoverable
                    if self._receipt_matches(receipt, item, generation)
                }
                if not matches:
                    legacy_matches = tuple(
                        (key, item, expected)
                        for key, history in retired_legacy.items()
                        for item in history
                        for expected in (_receipt_from_dict(item["receipt"]),)
                        if expected == receipt
                    )
                    if len(legacy_matches) != 1:
                        raise ValueError("plugin_grant_recovery_invalid")
                    key, item, expected = legacy_matches[0]
                    outcome = self._claim_retired_receipt(expected)
                    if outcome not in {
                        OAuthGrantReceiptClaimOutcome.CLAIMED,
                        OAuthGrantReceiptClaimOutcome.IDEMPOTENTLY_CLAIMED,
                    }:
                        raise ValueError("plugin_grant_recovery_invalid")
                    if item not in confirmed.setdefault(key, []):
                        confirmed[key].append(item)
                    continue
                if len(matches) != 1:
                    raise ValueError("plugin_grant_recovery_invalid")
                activation = next(iter(matches.values()))
                authority = self._publish(
                    receipt,
                    activation,
                    receipt.connection_generation,
                )
                if authority is not None:
                    published.append(authority)
        except BaseException as exc:
            failure = exc
        try:
            self._cleanup_retired_legacy(
                {key: tuple(history) for key, history in confirmed.items()}
            )
        except BaseException:
            if failure is None:
                raise
        if failure is not None:
            raise failure
        return tuple(published)

    def _claim_retired_receipt(
        self,
        receipt: OAuthCommittedGrant,
    ) -> OAuthGrantReceiptClaimOutcome:
        try:
            outcome = self.broker.claim_grant_receipt_exact(
                receipt,
                receipt.authority_revision,
            )
        except ValueError:
            raise ValueError("plugin_grant_recovery_invalid") from None
        if type(outcome) is not OAuthGrantReceiptClaimOutcome:
            raise ValueError("plugin_grant_recovery_invalid")
        return outcome

    def _cleanup_retired_legacy(
        self,
        retired_legacy: Mapping[str, tuple[dict[str, object], ...]],
    ) -> None:
        if not retired_legacy:
            return
        with self._state.locked():
            document = self._read_locked()
            changed = False
            for key, retired in retired_legacy.items():
                history = document["retired_legacy"].get(key)
                if history is None:
                    continue
                remaining = [item for item in history if item not in retired]
                if len(remaining) == len(history):
                    continue
                changed = True
                if remaining:
                    document["retired_legacy"][key] = remaining
                else:
                    document["retired_legacy"].pop(key)
            if changed:
                self._write_locked(document)

    def current(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> PluginGrantRecord | None:
        key = self._key(plugin_id, app_id, account_key_sha256)
        with self._state.locked():
            document = self._read_locked()
            raw = document["active"].get(key)
        if (
            raw is None
            or not raw["eligible"]
            or raw["revocation_pending"]
            or raw["generation"] != document["generations"].get(key)
        ):
            return None
        receipt = _receipt_from_dict(raw["receipt"])
        activation = PluginGrantActivation.from_dict(raw["activation"])
        if not self._receipt_matches(receipt, activation, raw["generation"]):
            raise ValueError("plugin_grant_store_unavailable")
        try:
            authority = self.authority_store.connection(
                plugin_id,
                app_id,
                account_key_sha256,
            )
        except ValueError:
            return None
        if authority != activation.authority():
            return None
        return PluginGrantRecord(
            receipt=receipt,
            activation=activation,
            authority=authority,
            connection_generation=raw["generation"],
        )

    @_lifecycle_mutation
    def rebind_compatible_update(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
        package_revision: str,
    ) -> PluginGrantRecord:
        """Move one exact live grant to a package with unchanged authority.

        Only the package binding changes.  Provider account, profile, token
        partition, scopes, capability bindings, and execution policy are read
        from the existing durable activation and never selected by plugin id.
        """

        plugin = _text(plugin_id)
        app = _text(app_id)
        account = _digest(account_key_sha256)
        package = _digest(package_revision)
        key = self._key(plugin, app, account)
        with self._state.locked():
            document = self._read_locked()
            raw = document["active"].get(key)
            if (
                raw is None
                or not raw["eligible"]
                or raw["revocation_pending"]
                or raw["generation"] != document["generations"].get(key)
            ):
                raise ValueError("plugin_reconnect_required")
            receipt = _receipt_from_dict(raw["receipt"])
            activation = PluginGrantActivation.from_dict(raw["activation"])
            if (
                activation.plugin_id != plugin
                or activation.app_id != app
                or activation.account_key_sha256 != account
                or not self._receipt_matches(receipt, activation, raw["generation"])
            ):
                raise ValueError("plugin_reconnect_required")
            authority = self.authority_store.connection(plugin, app, account)
            if authority != activation.authority() or not self._broker_receipt_is_live(
                PluginGrantRecord(
                    receipt=receipt,
                    activation=activation,
                    authority=authority,
                    connection_generation=raw["generation"],
                )
            ):
                raise ValueError("plugin_reconnect_required")
            rebound = replace(activation, package_revision=package)
            if rebound.authority() != activation.authority():
                raise ValueError("plugin_reconnect_required")
            raw["activation"] = rebound.to_dict()
            self._write_locked(document)
            generation = raw["generation"]
        return PluginGrantRecord(
            receipt=receipt,
            activation=rebound,
            authority=rebound.authority(),
            connection_generation=generation,
        )

    def current_for_attempt(self, state_sha256: str) -> PluginGrantRecord | None:
        state = _digest(state_sha256)
        with self._state.locked():
            document = self._read_locked()
            matches = tuple(
                (
                    PluginGrantActivation.from_dict(raw["activation"]),
                    raw,
                )
                for raw in document["active"].values()
                if raw["eligible"]
                and not raw["revocation_pending"]
                and raw["generation"]
                == document["generations"].get(
                    self._key(
                        raw["receipt"]["plugin_id"],
                        raw["receipt"]["app_id"],
                        raw["receipt"]["account_key_sha256"],
                    )
                )
                and raw["activation"].get("attempt_state_sha256") == state
            )
        if len(matches) != 1:
            return None
        activation, _raw = matches[0]
        return self.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )

    @_lifecycle_mutation
    def suspend(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> None:
        key = self._key(plugin_id, app_id, account_key_sha256)
        with self._state.locked():
            document = self._read_locked()
            raw = document["active"].get(key)
            if raw is None or not raw["eligible"]:
                return
            raw["eligible"] = False
            self._write_locked(document)
        self.authority_store.invalidate_connection(plugin_id, app_id, account_key_sha256)
        self.permission_store.invalidate_plugin(plugin_id)

    def union_scopes(
        self,
        token_partition_key: str,
        requested: tuple[str, ...],
    ) -> tuple[str, ...]:
        partition = _digest(token_partition_key)
        union = set(_scopes(requested))
        with self._state.locked():
            records = tuple(self._read_locked()["active"].values())
        for raw in records:
            receipt = _receipt_from_dict(raw["receipt"])
            if receipt.token_partition_key == partition:
                union.update(receipt.scopes)
        return tuple(sorted(union))

    def local_state_without_account(self, plugin_id: str, *, app_id: str) -> str | None:
        """Return only higher-priority durable risk state without choosing an account."""

        plugin = _text(plugin_id)
        app = _text(app_id)
        with self._state.locked():
            records = tuple(
                raw
                for raw in self._read_locked()["active"].values()
                if raw["receipt"]["plugin_id"] == plugin
                and raw["receipt"]["app_id"] == app
            )
        if any(raw["revocation_pending"] for raw in records):
            return "revocation_pending"
        if any(not raw["eligible"] for raw in records):
            return "permission_changed"
        return None

    def local_plugin_state_without_account(self, plugin_id: str) -> str | None:
        """Project plugin-wide durable risk without inventing an account or app."""

        plugin = _text(plugin_id)
        with self._state.locked():
            records = tuple(
                raw
                for raw in self._read_locked()["active"].values()
                if raw["receipt"]["plugin_id"] == plugin
            )
        if any(raw["revocation_pending"] for raw in records):
            return "revocation_pending"
        if any(not raw["eligible"] for raw in records):
            return "permission_changed"
        return None

    def _durable_grant_record(
        self,
        plugin_id: str,
        app_id: str,
        account_key_sha256: str,
    ) -> PluginGrantRecord | None:
        key = self._key(plugin_id, app_id, account_key_sha256)
        with self._state.locked():
            document = self._read_locked()
            raw = document["active"].get(key)
            if raw is None:
                return None
            if not raw["eligible"] or raw["revocation_pending"]:
                return None
            if raw["generation"] != document["generations"].get(key):
                raise ValueError("plugin_grant_revision_conflict")
            receipt = _receipt_from_dict(raw["receipt"])
            activation = PluginGrantActivation.from_dict(raw["activation"])
            if not self._receipt_matches(receipt, activation, raw["generation"]):
                raise ValueError("plugin_grant_store_unavailable")
        return PluginGrantRecord(
            receipt=receipt,
            activation=activation,
            authority=activation.authority(),
            connection_generation=raw["generation"],
        )

    @staticmethod
    def _receipt_state_is_live(state: object) -> bool:
        if state not in {"absent", "claimed", "unclaimed"}:
            raise ValueError("plugin_update_recovery_required")
        return state != "absent"

    def _broker_receipt_is_live(self, record: PluginGrantRecord) -> bool:
        state = getattr(self.broker, "grant_receipt_state", None)
        if not callable(state):
            raise ValueError("plugin_update_recovery_required")
        try:
            return self._receipt_state_is_live(state(record.receipt))
        except ValueError as exc:
            if str(exc) in {
                "oauth_grant_receipt_conflict",
                "oauth_grant_invalid",
            }:
                raise ValueError("plugin_update_recovery_required") from None
            raise

    def _broker_partition_dependents(self, token_partition_key: str) -> tuple[str, ...]:
        committed = getattr(self.broker, "committed_grant_receipts", None)
        if not callable(committed):
            raise ValueError("plugin_update_recovery_required")
        try:
            records = tuple(committed())
            if any(type(item) is not OAuthCommittedGrant for item in records):
                raise ValueError
            return tuple(
                sorted(
                    {
                        item.plugin_id
                        for item in records
                        if item.token_partition_key == token_partition_key
                        and item.status in {"active", "reauthorization_required"}
                    }
                )
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_update_recovery_required") from None

    def _require_partition_receipts_exact(
        self,
        *,
        token_partition_key: str,
        plugin_id: str,
        expected: tuple[OAuthCommittedGrant, ...],
    ) -> None:
        require = getattr(self.broker, "require_plugin_partition_receipts_exact", None)
        try:
            if callable(require):
                require(
                    token_partition_key,
                    plugin_id=plugin_id,
                    expected_receipts=expected,
                )
                return
            committed = tuple(self.broker.committed_grant_receipts())
            current = tuple(
                sorted(
                    (
                        replace(item, claimed_authority_revision=None)
                        for item in committed
                        if item.plugin_id == plugin_id
                        and item.token_partition_key == token_partition_key
                        and item.status in {"active", "reauthorization_required"}
                    ),
                    key=lambda item: item.receipt_id,
                )
            )
            normalized = tuple(
                sorted(
                    (
                        replace(
                            item,
                            status="active",
                            claimed_authority_revision=None,
                        )
                        for item in expected
                    ),
                    key=lambda item: item.receipt_id,
                )
            )
            current = tuple(replace(item, status="active") for item in current)
            if current != normalized:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_update_recovery_required") from None

    def _verified_partition_survivor(
        self,
        predecessor: PluginGrantRecord,
        candidates: tuple[PluginGrantRecord, ...],
    ) -> PluginGrantRecord | None:
        live = tuple(item for item in candidates if self._broker_receipt_is_live(item))
        if len(live) != len(candidates):
            raise ValueError("plugin_update_recovery_required")
        if len(live) > 1 or live and self._broker_receipt_is_live(predecessor):
            raise ValueError("plugin_update_recovery_required")
        return None if not live else live[0]

    def _require_candidate_receipts_exact(
        self,
        candidates: tuple[PluginGrantRecord, ...],
    ) -> None:
        """Prove every sealed OAuth candidate in its exact vault partition."""

        by_partition: dict[tuple[str, str], list[OAuthCommittedGrant]] = {}
        for candidate in candidates:
            key = (
                candidate.activation.token_partition_key,
                candidate.activation.plugin_id,
            )
            by_partition.setdefault(key, []).append(candidate.receipt)
        for (partition, plugin_id), receipts in sorted(by_partition.items()):
            self._require_partition_receipts_exact(
                token_partition_key=partition,
                plugin_id=plugin_id,
                expected=tuple(sorted(receipts, key=lambda item: item.receipt_id)),
            )

    def _require_completed_partition_retirement_exact(
        self,
        predecessor: PluginGrantRecord,
        completed: PluginGrantDisconnect,
    ) -> None:
        require = getattr(
            self.broker,
            "require_plugin_partition_retirement_exact",
            None,
        )
        try:
            if callable(require):
                require(
                    predecessor.activation.token_partition_key,
                    plugin_id=predecessor.activation.plugin_id,
                    expected_remaining_plugin_ids=completed.remaining_plugin_ids,
                )
                return
            # Legacy deterministic doubles expose receipt exactness without a
            # token-partition state CAS. Production always takes the atomic
            # terminal-state branch above.
            self._require_partition_receipts_exact(
                token_partition_key=predecessor.activation.token_partition_key,
                plugin_id=predecessor.activation.plugin_id,
                expected=(),
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_update_recovery_required") from None

    def _candidate_records_for_update(
        self,
        *,
        plugin_id: str,
        account_key_sha256: str,
        candidate_app_ids: tuple[str, ...],
        candidate_activations: tuple[PluginGrantActivation, ...],
        predecessors: tuple[PluginGrantRecord, ...],
        credentialless_app_id: str | None,
    ) -> tuple[PluginGrantRecord, ...]:
        if (
            type(candidate_app_ids) is not tuple
            or candidate_app_ids != tuple(sorted(candidate_app_ids))
            or len(candidate_app_ids) != len(set(candidate_app_ids))
            or type(candidate_activations) is not tuple
        ):
            raise ValueError("plugin_update_recovery_required")
        expected_by_app: dict[str, PluginGrantActivation] = {}
        for activation in candidate_activations:
            if (
                type(activation) is not PluginGrantActivation
                or activation.plugin_id != plugin_id
                or activation.account_key_sha256 != account_key_sha256
                or activation.app_id not in candidate_app_ids
                or activation.app_id in expected_by_app
            ):
                raise ValueError("plugin_update_recovery_required")
            expected_by_app[activation.app_id] = activation
        predecessor_by_app = {
            item.activation.app_id: item
            for item in predecessors
            if item.activation.app_id in candidate_app_ids
        }
        records: list[PluginGrantRecord] = []
        for app_id in candidate_app_ids:
            if app_id == credentialless_app_id:
                if app_id in expected_by_app:
                    raise ValueError("plugin_update_recovery_required")
                continue
            current = self._durable_grant_record(plugin_id, app_id, account_key_sha256)
            expected = expected_by_app.get(app_id)
            if expected is not None:
                if current is None or current.activation != expected:
                    raise ValueError("plugin_update_recovery_required")
                records.append(current)
                continue
            predecessor = predecessor_by_app.get(app_id)
            if current is None:
                if predecessor is not None:
                    raise ValueError("plugin_update_recovery_required")
                continue
            if predecessor is None or current != predecessor:
                raise ValueError("plugin_update_recovery_required")
            records.append(current)
        return tuple(records)

    def _begin_update_retirement(
        self,
        predecessor: PluginGrantRecord,
        *,
        preserved_authority: PluginConnectionAuthority | None = None,
        predecessor_authority_lineage: object = None,
        preserved_authority_lineage: object = None,
    ) -> PluginGrantRevocation:
        activation = predecessor.activation
        key = self._key(
            activation.plugin_id,
            activation.app_id,
            activation.account_key_sha256,
        )
        with self._state.locked():
            document = self._read_locked()
            raw = document["active"].get(key)
            if raw is None:
                raise ValueError("plugin_update_recovery_required")
            current = PluginGrantRecord(
                receipt=_receipt_from_dict(raw["receipt"]),
                activation=PluginGrantActivation.from_dict(raw["activation"]),
                authority=PluginGrantActivation.from_dict(raw["activation"]).authority(),
                connection_generation=raw["generation"],
            )
            if (
                raw["generation"] != document["generations"].get(key)
                or current != predecessor
            ):
                raise ValueError("plugin_update_recovery_required")
            if not raw["revocation_pending"]:
                if not raw["eligible"]:
                    raise ValueError("plugin_update_recovery_required")
                raw["eligible"] = False
                raw["revocation_pending"] = True
                self._write_locked(document)
        self._retire_update_authority(
            predecessor,
            preserved_authority=preserved_authority,
            predecessor_authority_lineage=predecessor_authority_lineage,
            preserved_authority_lineage=preserved_authority_lineage,
        )
        return PluginGrantRevocation(
            activation.plugin_id,
            activation.app_id,
            activation.account_key_sha256,
            activation.token_partition_key,
            predecessor.connection_generation,
        )

    def _retire_update_authority(
        self,
        predecessor: PluginGrantRecord,
        *,
        preserved_authority: PluginConnectionAuthority | None,
        predecessor_authority_lineage: object,
        preserved_authority_lineage: object,
    ) -> None:
        """Exact, idempotent authority half of a durable predecessor retirement."""

        try:
            lineage = (
                predecessor_authority_lineage
                if preserved_authority is None
                else preserved_authority_lineage
            )
            if lineage is None:
                raise ValueError("plugin_update_recovery_required")
            checked = PluginAuthorityStore._connection_lineage_checkpoint(lineage)
            expected_authority = predecessor.authority
            if preserved_authority is not None:
                expected_authority = preserved_authority
            raw_connection = checked["connection"]
            if (
                raw_connection is None
                or PluginConnectionAuthority.from_dict(raw_connection)
                != expected_authority
                or preserved_authority is not None
                and (checked["authorities"] or checked["executions"])
            ):
                raise ValueError("plugin_update_recovery_required")
            if preserved_authority is None:
                self.authority_store.compare_and_invalidate_connection_lineage(checked)
            else:
                self.authority_store.require_connection_lineage_exact(checked)
        except ValueError as exc:
            if str(exc) == "plugin_update_recovery_required":
                raise
            raise ValueError("plugin_update_recovery_required") from None

    def _require_preserved_update_authority(self, lineage: object) -> None:
        try:
            self.authority_store.require_connection_lineage_exact(lineage)
        except ValueError:
            raise ValueError("plugin_update_recovery_required") from None

    def _completed_update_retirement(
        self,
        predecessor: PluginGrantRecord,
    ) -> PluginGrantDisconnect | None:
        """Return an exact completed tombstone, rejecting a still-live receipt."""

        activation = predecessor.activation
        key = self._key(
            activation.plugin_id,
            activation.app_id,
            activation.account_key_sha256,
        )
        with self._state.locked():
            document = self._read_locked()
            if document["active"].get(key) is not None:
                return None
            tombstone = document["disconnected"].get(key)
            if tombstone is None:
                return None
            if (
                document["generations"].get(key) != predecessor.connection_generation
                or tombstone["plugin_id"] != activation.plugin_id
                or tombstone["app_id"] != activation.app_id
                or tombstone["account_key_sha256"]
                != activation.account_key_sha256
            ):
                raise ValueError("plugin_update_recovery_required")
            completed = PluginGrantDisconnect(
                tombstone["provider_token_deleted"],
                tuple(tombstone["remaining_plugin_ids"]),
            )
        if self._broker_receipt_is_live(predecessor):
            raise ValueError("plugin_update_recovery_required")
        return completed

    @staticmethod
    def _is_compatible_update_rebind(
        predecessor: PluginGrantRecord,
        candidate: PluginGrantRecord,
    ) -> bool:
        """Recognize only the package-only move of one exact live grant."""

        predecessor_activation = predecessor.activation
        candidate_activation = candidate.activation
        if (
            predecessor.receipt != candidate.receipt
            or predecessor.connection_generation != candidate.connection_generation
            or predecessor.authority != candidate.authority
            or candidate_activation.package_revision
            == predecessor_activation.package_revision
        ):
            return False
        if (
            candidate_activation.plugin_id != predecessor_activation.plugin_id
            or candidate_activation.app_id != predecessor_activation.app_id
            or candidate_activation.account_key_sha256
            != predecessor_activation.account_key_sha256
            or candidate_activation.profile_id != predecessor_activation.profile_id
            or candidate_activation.profile_revision
            != predecessor_activation.profile_revision
            or candidate_activation.token_partition_key
            != predecessor_activation.token_partition_key
        ):
            return False
        return (
            replace(
                predecessor_activation,
                package_revision=candidate_activation.package_revision,
            )
            == candidate_activation
        )

    @_lifecycle_mutation
    async def retire_update_predecessor(
        self,
        recovery: object,
        *,
        app_id: str,
        candidate_survives: bool,
        candidate_app_ids: tuple[str, ...] = (),
        candidate_activations: tuple[PluginGrantActivation, ...] = (),
        credentialless_connection_revision: str | None = None,
        predecessor_authority_lineage: object = None,
    ) -> bool:
        """Retire one exact checkpointed grant without targeting its replacement."""

        checked = self._update_recovery(recovery)
        app = _text(app_id)
        if type(candidate_survives) is not bool:
            raise ValueError("plugin_update_recovery_required")
        all_predecessors = tuple(
            PluginGrantRecord.from_dict(raw)
            for raw in checked["predecessor_grants"]
        )
        predecessors = tuple(
            item for item in all_predecessors if item.activation.app_id == app
        )
        if not predecessors:
            return False
        if len(predecessors) != 1:
            raise ValueError("plugin_update_recovery_required")
        predecessor = predecessors[0]
        plugin = str(checked["plugin_id"])
        account = str(checked["account_key_sha256"])
        activation = predecessor.activation
        if (
            activation.plugin_id != plugin
            or activation.app_id != app
            or activation.account_key_sha256 != account
            or candidate_survives != (app in candidate_app_ids)
        ):
            raise ValueError("plugin_update_recovery_required")

        if predecessor_authority_lineage is not None:
            try:
                predecessor_authority_lineage = (
                    PluginAuthorityStore._connection_lineage_checkpoint(
                        predecessor_authority_lineage
                    )
                )
                sealed_connection = predecessor_authority_lineage["connection"]
                if (
                    predecessor_authority_lineage["plugin_id"] != plugin
                    or predecessor_authority_lineage["app_id"] != app
                    or predecessor_authority_lineage["account_key_hash"] != account
                    or sealed_connection is None
                    or PluginConnectionAuthority.from_dict(sealed_connection)
                    != predecessor.authority
                ):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("plugin_update_recovery_required") from None

        preserved_authority = None
        preserved_authority_lineage = None
        if credentialless_connection_revision is not None:
            try:
                revision = _digest(credentialless_connection_revision)
                preserved_authority = self.authority_store.connection(plugin, app, account)
            except ValueError:
                raise ValueError("plugin_update_recovery_required") from None
            if (
                not candidate_survives
                or preserved_authority.authority_kind
                is not PluginConnectionAuthorityKind.CREDENTIALLESS
                or preserved_authority.connection_revision != revision
            ):
                raise ValueError("plugin_update_recovery_required")
            preserved_authority_lineage = (
                self.authority_store.checkpoint_connection_lineage(plugin, app, account)
            )

        candidates = self._candidate_records_for_update(
            plugin_id=plugin,
            account_key_sha256=account,
            candidate_app_ids=candidate_app_ids,
            candidate_activations=candidate_activations,
            predecessors=all_predecessors,
            credentialless_app_id=(app if preserved_authority is not None else None),
        )
        # This is intentionally before pending/local/authority predecessor
        # mutation. A durable candidate activation alone is not proof that its
        # exact vault receipt survived a crash or concurrent replacement.
        self._require_candidate_receipts_exact(candidates)
        candidate = next(
            (item for item in candidates if item.activation.app_id == app),
            None,
        )
        partition_survivors = tuple(
            item
            for item in candidates
            if item.activation.app_id != app
            and item.activation.token_partition_key == activation.token_partition_key
        )

        pending = self.pending_disconnect(
            plugin,
            app_id=app,
            account_key_sha256=account,
        )
        if pending is not None:
            if (
                candidate_survives
                and preserved_authority is None
                or pending.token_partition_key != activation.token_partition_key
            ):
                raise ValueError("plugin_update_recovery_required")
            self._retire_update_authority(
                predecessor,
                preserved_authority=preserved_authority,
                predecessor_authority_lineage=predecessor_authority_lineage,
                preserved_authority_lineage=preserved_authority_lineage,
            )
            exact_survivor = self._verified_partition_survivor(
                predecessor,
                partition_survivors,
            )
            if exact_survivor is not None:
                self._require_partition_receipts_exact(
                    token_partition_key=activation.token_partition_key,
                    plugin_id=plugin,
                    expected=(exact_survivor.receipt,),
                )
                self._complete_disconnect(
                    pending,
                    PluginGrantDisconnect(
                        False,
                        self._broker_partition_dependents(activation.token_partition_key),
                    ),
                )
                self._require_partition_receipts_exact(
                    token_partition_key=activation.token_partition_key,
                    plugin_id=plugin,
                    expected=(exact_survivor.receipt,),
                )
            else:
                await self.finish_disconnect(
                    pending,
                    expected_receipts=(predecessor.receipt,),
                )
            if preserved_authority_lineage is not None:
                self._require_preserved_update_authority(preserved_authority_lineage)
            return True

        current = self._durable_grant_record(plugin, app, account)
        if current is None:
            completed = self._completed_update_retirement(predecessor)
            if completed is None:
                raise ValueError("plugin_update_recovery_required")
            exact_survivor = self._verified_partition_survivor(
                predecessor,
                partition_survivors,
            )
            if exact_survivor is not None:
                self._require_partition_receipts_exact(
                    token_partition_key=activation.token_partition_key,
                    plugin_id=plugin,
                    expected=(exact_survivor.receipt,),
                )
            else:
                self._require_completed_partition_retirement_exact(
                    predecessor,
                    completed,
                )
            self._retire_update_authority(
                predecessor,
                preserved_authority=preserved_authority,
                predecessor_authority_lineage=predecessor_authority_lineage,
                preserved_authority_lineage=preserved_authority_lineage,
            )
            if exact_survivor is not None:
                self._require_partition_receipts_exact(
                    token_partition_key=activation.token_partition_key,
                    plugin_id=plugin,
                    expected=(exact_survivor.receipt,),
                )
            else:
                self._require_completed_partition_retirement_exact(
                    predecessor,
                    completed,
                )
            if preserved_authority_lineage is not None:
                self._require_preserved_update_authority(preserved_authority_lineage)
            return True
        if candidate_survives:
            if preserved_authority is not None:
                if current != predecessor:
                    raise ValueError("plugin_update_recovery_required")
                pending = self._begin_update_retirement(
                    predecessor,
                    preserved_authority=preserved_authority,
                    predecessor_authority_lineage=predecessor_authority_lineage,
                    preserved_authority_lineage=preserved_authority_lineage,
                )
                exact_survivor = self._verified_partition_survivor(
                    predecessor,
                    partition_survivors,
                )
                if exact_survivor is not None:
                    self._require_partition_receipts_exact(
                        token_partition_key=activation.token_partition_key,
                        plugin_id=plugin,
                        expected=(exact_survivor.receipt,),
                    )
                    self._complete_disconnect(
                        pending,
                        PluginGrantDisconnect(
                            False,
                            self._broker_partition_dependents(
                                activation.token_partition_key
                            ),
                        ),
                    )
                    self._require_partition_receipts_exact(
                        token_partition_key=activation.token_partition_key,
                        plugin_id=plugin,
                        expected=(exact_survivor.receipt,),
                    )
                else:
                    await self.finish_disconnect(
                        pending,
                        expected_receipts=(predecessor.receipt,),
                    )
                assert preserved_authority_lineage is not None
                self._require_preserved_update_authority(preserved_authority_lineage)
                return True
            if candidate is None or current != candidate:
                raise ValueError("plugin_update_recovery_required")
            if candidate == predecessor:
                return True
            if self._is_compatible_update_rebind(predecessor, candidate):
                if (
                    not self._broker_receipt_is_live(predecessor)
                    or not self._broker_receipt_is_live(candidate)
                ):
                    raise ValueError("plugin_update_recovery_required")
                self._require_partition_receipts_exact(
                    token_partition_key=activation.token_partition_key,
                    plugin_id=plugin,
                    expected=(candidate.receipt,),
                )
                return True
            if candidate.activation.token_partition_key == activation.token_partition_key:
                if (
                    self._broker_receipt_is_live(predecessor)
                    or not self._broker_receipt_is_live(candidate)
                ):
                    raise ValueError("plugin_update_recovery_required")
                self._require_partition_receipts_exact(
                    token_partition_key=activation.token_partition_key,
                    plugin_id=plugin,
                    expected=(candidate.receipt,),
                )
                return True
            exact_survivor = self._verified_partition_survivor(
                predecessor,
                partition_survivors,
            )
            if exact_survivor is not None:
                self._require_partition_receipts_exact(
                    token_partition_key=activation.token_partition_key,
                    plugin_id=plugin,
                    expected=(exact_survivor.receipt,),
                )
                return True
            try:
                await self.broker.revoke(
                    activation.token_partition_key,
                    plugin_id=plugin,
                    expected_receipts=(predecessor.receipt,),
                )
            except ValueError as exc:
                if str(exc) != "oauth_token_partition_missing":
                    raise
            return True

        pending = self._begin_update_retirement(
            predecessor,
            predecessor_authority_lineage=predecessor_authority_lineage,
        )
        if pending.completed is not None:
            return True
        exact_survivor = self._verified_partition_survivor(
            predecessor,
            partition_survivors,
        )
        if exact_survivor is not None:
            self._require_partition_receipts_exact(
                token_partition_key=activation.token_partition_key,
                plugin_id=plugin,
                expected=(exact_survivor.receipt,),
            )
            self._complete_disconnect(
                pending,
                PluginGrantDisconnect(
                    False,
                    self._broker_partition_dependents(activation.token_partition_key),
                ),
            )
            self._require_partition_receipts_exact(
                token_partition_key=activation.token_partition_key,
                plugin_id=plugin,
                expected=(exact_survivor.receipt,),
            )
        else:
            await self.finish_disconnect(
                pending,
                expected_receipts=(predecessor.receipt,),
            )
        return True

    def pending_disconnect(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> PluginGrantRevocation | None:
        """Return the exact durable disconnect that must finish before reconnecting."""

        plugin = _text(plugin_id)
        app = _text(app_id)
        account = _digest(account_key_sha256)
        key = self._key(plugin, app, account)
        with self._state.locked():
            document = self._read_locked()
            raw = document["active"].get(key)
            if raw is None or not raw["revocation_pending"]:
                return None
            generation = raw["generation"]
            if generation != document["generations"].get(key):
                raise ValueError("plugin_grant_revision_conflict")
            receipt = _receipt_from_dict(raw["receipt"])
            activation = PluginGrantActivation.from_dict(raw["activation"])
            if (
                not self._receipt_matches(receipt, activation, generation)
                or (receipt.plugin_id, receipt.app_id, receipt.account_key_sha256)
                != (plugin, app, account)
            ):
                raise ValueError("plugin_grant_store_unavailable")
            return PluginGrantRevocation(
                plugin,
                app,
                account,
                receipt.token_partition_key,
                generation,
            )

    @_lifecycle_mutation
    def begin_disconnect(
        self,
        plugin_id: str,
        *,
        app_id: str | None = None,
        account_key_sha256: str,
    ) -> PluginGrantRevocation:
        plugin = _text(plugin_id)
        app = None if app_id is None else _text(app_id)
        account = _digest(account_key_sha256)
        with self._state.locked():
            document = self._read_locked()
            matches = [
                (key, raw)
                for key, raw in document["active"].items()
                if raw["receipt"]["plugin_id"] == plugin
                and raw["receipt"]["account_key_sha256"] == account
                and (app is None or raw["receipt"]["app_id"] == app)
            ]
            if not matches:
                tombstones = [
                    value
                    for value in document["disconnected"].values()
                    if value["plugin_id"] == plugin and value["account_key_sha256"] == account
                    and (app is None or value["app_id"] == app)
                ]
                if len(tombstones) == 1:
                    tombstone = tombstones[0]
                    return PluginGrantRevocation(
                        plugin,
                        tombstone["app_id"],
                        account,
                        None,
                        None,
                        PluginGrantDisconnect(
                            tombstone["provider_token_deleted"],
                            tuple(tombstone["remaining_plugin_ids"]),
                        ),
                    )
                raise ValueError("plugin_grant_not_found")
            if len(matches) != 1:
                raise ValueError("plugin_grant_not_found")
            key, raw = matches[0]
            receipt = _receipt_from_dict(raw["receipt"])
            raw["eligible"] = False
            raw["revocation_pending"] = True
            self._write_locked(document)
        self.authority_store.invalidate_connection(plugin, receipt.app_id, account)
        self.permission_store.invalidate_plugin(plugin)
        return PluginGrantRevocation(
            plugin,
            receipt.app_id,
            account,
            receipt.token_partition_key,
            raw["generation"],
        )

    @_lifecycle_mutation
    async def finish_disconnect(
        self,
        pending: PluginGrantRevocation,
        *,
        expected_receipts: tuple[OAuthCommittedGrant, ...] | None = None,
        tolerate_provider_refusal: bool = False,
    ) -> PluginGrantDisconnect:
        """Complete one pending disconnect.

        ``tolerate_provider_refusal`` defaults to False so a predecessor
        RETIREMENT, which shares this path, keeps the retry it needs: the plugin
        is staying and the old token really should be revoked. A DISCONNECT
        passes True -- it is an instruction to let go, and refusing over a
        provider that will not confirm leaves the plugin installed and the
        credential held.
        """
        if type(pending) is not PluginGrantRevocation:
            raise ValueError("plugin_grant_invalid")
        if pending.completed is not None:
            return pending.completed
        if pending.token_partition_key is None or pending.connection_generation is None:
            raise ValueError("plugin_grant_invalid")
        key = self._key(
            pending.plugin_id,
            pending.app_id,
            pending.account_key_sha256,
        )
        with self._state.locked():
            document = self._read_locked()
            raw = document["active"].get(key)
            if (
                raw is None
                or raw["generation"] != pending.connection_generation
                or not raw["revocation_pending"]
            ):
                raise ValueError("plugin_grant_revision_conflict")
            exact_receipt = _receipt_from_dict(raw["receipt"])
            sibling_records: list[PluginGrantRecord] = []
            for sibling_key, sibling_raw in document["active"].items():
                if sibling_key == key:
                    continue
                sibling_receipt = _receipt_from_dict(sibling_raw["receipt"])
                if (
                    sibling_receipt.plugin_id != pending.plugin_id
                    or sibling_receipt.account_key_sha256
                    != pending.account_key_sha256
                    or sibling_receipt.token_partition_key
                    != pending.token_partition_key
                ):
                    continue
                sibling_activation = PluginGrantActivation.from_dict(
                    sibling_raw["activation"]
                )
                if (
                    not sibling_raw["eligible"]
                    or sibling_raw["revocation_pending"]
                    or sibling_raw["generation"]
                    != document["generations"].get(sibling_key)
                    or not self._receipt_matches(
                        sibling_receipt,
                        sibling_activation,
                        sibling_raw["generation"],
                    )
                ):
                    raise ValueError("plugin_update_recovery_required")
                sibling_records.append(
                    PluginGrantRecord(
                        receipt=sibling_receipt,
                        activation=sibling_activation,
                        authority=sibling_activation.authority(),
                        connection_generation=sibling_raw["generation"],
                    )
                )
        caller_supplied_exact_receipts = expected_receipts is not None
        if expected_receipts is None:
            expected_receipts = (exact_receipt,)
        elif expected_receipts != (exact_receipt,):
            raise ValueError("plugin_update_recovery_required")
        if not caller_supplied_exact_receipts and sibling_records:
            predecessor = PluginGrantRecord(
                receipt=exact_receipt,
                activation=PluginGrantActivation.from_dict(raw["activation"]),
                authority=PluginGrantActivation.from_dict(raw["activation"]).authority(),
                connection_generation=raw["generation"],
            )
            survivor = self._verified_partition_survivor(
                predecessor,
                tuple(sibling_records),
            )
            if survivor is None:
                raise ValueError("plugin_update_recovery_required")
            self._require_partition_receipts_exact(
                token_partition_key=pending.token_partition_key,
                plugin_id=pending.plugin_id,
                expected=(survivor.receipt,),
            )
            disconnected = self._complete_disconnect(
                pending,
                PluginGrantDisconnect(
                    False,
                    self._broker_partition_dependents(pending.token_partition_key),
                ),
            )
            self._require_partition_receipts_exact(
                token_partition_key=pending.token_partition_key,
                plugin_id=pending.plugin_id,
                expected=(survivor.receipt,),
            )
            return disconnected
        try:
            if callable(
                getattr(
                    self.broker,
                    "require_plugin_partition_receipts_exact",
                    None,
                )
            ):
                result = await self.broker.revoke(
                    pending.token_partition_key,
                    plugin_id=pending.plugin_id,
                    expected_receipts=expected_receipts,
                    tolerate_provider_refusal=tolerate_provider_refusal,
                )
            else:
                # Legacy in-memory broker doubles have no atomic CAS surface;
                # an exact precheck preserves their deterministic test
                # contract. Production always takes the atomic branch.
                self._require_partition_receipts_exact(
                    token_partition_key=pending.token_partition_key,
                    plugin_id=pending.plugin_id,
                    expected=expected_receipts,
                )
                result = await self.broker.revoke(
                    pending.token_partition_key,
                    plugin_id=pending.plugin_id,
                )
        except ValueError as exc:
            if str(exc) == "oauth_grant_receipt_conflict":
                raise ValueError("plugin_update_recovery_required") from None
            if str(exc) != "oauth_token_partition_missing":
                raise
            result = type(
                "_RevocationResult",
                (),
                {"local_token_deleted": True, "dependents": ()},
            )()
        disconnected = PluginGrantDisconnect(
            provider_token_deleted=result.local_token_deleted,
            remaining_plugin_ids=result.dependents,
        )
        return self._complete_disconnect(pending, disconnected)

    def _complete_disconnect(
        self,
        pending: PluginGrantRevocation,
        disconnected: PluginGrantDisconnect,
    ) -> PluginGrantDisconnect:
        if (
            type(pending) is not PluginGrantRevocation
            or pending.completed is not None
            or pending.token_partition_key is None
            or pending.connection_generation is None
            or type(disconnected) is not PluginGrantDisconnect
        ):
            raise ValueError("plugin_grant_invalid")
        key = self._key(
            pending.plugin_id,
            pending.app_id,
            pending.account_key_sha256,
        )
        with self._state.locked():
            document = self._read_locked()
            current = document["active"].get(key)
            if (
                current is not None
                and current["generation"] == pending.connection_generation
                and current["revocation_pending"]
            ):
                receipt = _receipt_from_dict(current["receipt"])
                if (
                    receipt.plugin_id != pending.plugin_id
                    or receipt.app_id != pending.app_id
                    or receipt.account_key_sha256 != pending.account_key_sha256
                    or receipt.token_partition_key != pending.token_partition_key
                ):
                    raise ValueError("plugin_grant_revision_conflict")
                document["active"].pop(key)
                document["disconnected"][key] = {
                    "plugin_id": receipt.plugin_id,
                    "app_id": receipt.app_id,
                    "account_key_sha256": receipt.account_key_sha256,
                    "provider_token_deleted": disconnected.provider_token_deleted,
                    "remaining_plugin_ids": list(disconnected.remaining_plugin_ids),
                }
                self._write_locked(document)
            elif current is None:
                tombstone = document["disconnected"].get(key)
                if tombstone is None:
                    raise ValueError("plugin_grant_revision_conflict")
                return PluginGrantDisconnect(
                    tombstone["provider_token_deleted"],
                    tuple(tombstone["remaining_plugin_ids"]),
                )
            else:
                raise ValueError("plugin_grant_revision_conflict")
        return disconnected

    @_lifecycle_mutation
    async def disconnect(
        self,
        plugin_id: str,
        *,
        app_id: str | None = None,
        account_key_sha256: str,
    ) -> PluginGrantDisconnect:
        pending = self.begin_disconnect(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account_key_sha256,
        )
        return await self.finish_disconnect(pending)

    @_lifecycle_mutation
    async def revoke(
        self,
        plugin_id: str,
        *,
        app_id: str | None = None,
        account_key_sha256: str,
    ) -> PluginGrantDisconnect:
        return await self.disconnect(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account_key_sha256,
        )


__all__ = [
    "PluginGrantActivation",
    "PluginGrantDisconnect",
    "PluginGrantRecord",
    "PluginGrantRevocation",
    "PluginGrantService",
]
