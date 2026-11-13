"""Durable connection scope and exact plugin-plan authority."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from core.paths import MCP_PLUGIN_AUTHORITY_STATE_PATH
from mcp_platform.authority import McpOperationAuthorityEvaluator, _AuthorityIssuer
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.operation_contracts import (
    McpAuthorityDecision,
    McpAuthoritySnapshot,
    McpConnectionSnapshot,
    McpOperationProposal,
    McpToolAuthority,
)
#: Re-exported so authority-loading callers -- including
#: ``mcp_platform.plugin_discovery``, which classifies every capability it
#: enumerates from a publisher-verified or personal connection as
#: ``RiskClass.EXTERNAL_UNREVIEWED`` -- can name this module, not
#: ``package_contracts``, as the source of the closed risk vocabulary
#: ``_snapshot`` below evaluates. ``_snapshot`` already denies
#: ``allow_send_publish``/``allow_destructive``/``allow_cross_provider`` to
#: any risk other than its own named class, so ``EXTERNAL_UNREVIEWED`` gets
#: none of the three by construction, and never more autonomy than an
#: explicit later standing grant names.
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_argument_constraints import evaluate_argument_constraints
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.plugin_planning import PluginOperationPlan, PluginOperationStep
from mcp_platform.state_store import McpStateStore

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RECORDS = 512
_MAX_BYTES = 1_048_576
_SCHEMA_VERSION = 4
_PROCESS_EPOCH = os.urandom(32).hex()
_ZERO_REVISION = "0" * 64
_RECOVERY_ATTEMPTS = 3
_MAX_PLAN_STEPS = 4
BindingResolver = Callable[
    [PluginOperationPlan, str],
    tuple[PluginToolBinding, ...],
]


def _text(value: object, *, maximum: int = 1024) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("plugin_authority_invalid")
    return value


def _tool_capability_id(capability: object) -> str | None:
    if (
        type(capability) is not str
        or not capability
        or capability.strip() != capability
        or ":" in capability
    ):
        return None
    return f"tool:{capability}"


def _digest(value: object) -> str:
    value = _text(value, maximum=64)
    if _DIGEST.fullmatch(value) is None:
        raise ValueError("plugin_authority_invalid")
    return value


def _tuple(value: object, *, allow_empty: bool = True) -> tuple[str, ...]:
    if type(value) not in {tuple, list}:
        raise ValueError("plugin_authority_invalid")
    result = tuple(_text(item) for item in value)
    if (not allow_empty and not result) or len(result) != len(set(result)):
        raise ValueError("plugin_authority_invalid")
    return result


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain(item) for item in value]
    return value


def _mapping(value: object, *, digests: bool = False) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        value = _plain(value)
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError("plugin_authority_invalid")
    document = json.loads(canonical_bytes(value).decode("utf-8"))
    if digests:
        document = {_text(key): _digest(item) for key, item in document.items()}
    if len(canonical_bytes(document)) > 65_536:
        raise ValueError("plugin_authority_limit_exceeded")
    return MappingProxyType(document)


def _plan_document(plan: PluginOperationPlan) -> dict[str, object]:
    if type(plan) is not PluginOperationPlan:
        raise ValueError("plugin_authority_invalid")
    return {
        "plan_id": plan.plan_id,
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "authority_digest": plan.authority_digest,
        "steps": [
            {
                "step_id": step.step_id,
                "candidate_id": step.candidate_id,
                "arguments": _plain(step.arguments),
                "consumes_step_ids": list(step.consumes_step_ids),
            }
            for step in plan.steps
        ],
    }


def plugin_plan_digest(plan: PluginOperationPlan) -> str:
    return sha256_hex(canonical_bytes(_plan_document(plan)))


class PluginExecutionPolicy(StrEnum):
    DIRECT_TURN = "direct_turn"
    CONFIRM_EACH = "confirm_each"


class PluginConnectionAuthorityKind(StrEnum):
    OAUTH = "oauth"
    CREDENTIALLESS = "credentialless"


_CONNECTION_CAPABILITY_KINDS = frozenset(
    {"tool", "resource", "resource_template", "prompt"}
)


@dataclass(frozen=True, slots=True)
class PluginCapabilityBindingIdentity:
    """Content-free identity for one exact signed MCP capability binding."""

    capability_kind: str
    binding_sha256: str

    def __post_init__(self) -> None:
        try:
            kind = _text(self.capability_kind)
            binding = _digest(self.binding_sha256)
            if kind not in _CONNECTION_CAPABILITY_KINDS:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_authority_invalid") from None
        object.__setattr__(self, "capability_kind", kind)
        object.__setattr__(self, "binding_sha256", binding)

    def to_dict(self) -> dict[str, str]:
        return {
            "capability_kind": self.capability_kind,
            "binding_sha256": self.binding_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginCapabilityBindingIdentity:
        if type(value) is not dict or set(value) != {
            "capability_kind",
            "binding_sha256",
        }:
            raise ValueError("plugin_authority_invalid")
        try:
            return cls(**value)
        except (TypeError, ValueError):
            raise ValueError("plugin_authority_invalid") from None


def _connection_document(
    *,
    plugin_id: str,
    app_id: str,
    account_key_hash: str,
    connection_revision: str,
    provider_scopes: tuple[str, ...],
    capability_ceiling: tuple[str, ...],
    allowed_tool_schemas: Mapping[str, str],
    source_constraints: Mapping[str, object],
    destination_constraints: Mapping[str, object],
    execution_policy: PluginExecutionPolicy,
    authority_kind: PluginConnectionAuthorityKind,
) -> dict[str, object]:
    if type(execution_policy) is not PluginExecutionPolicy:
        raise ValueError("plugin_authority_invalid")
    if type(authority_kind) is not PluginConnectionAuthorityKind:
        raise ValueError("plugin_authority_invalid")
    scopes = _tuple(provider_scopes)
    if (authority_kind is PluginConnectionAuthorityKind.OAUTH and not scopes) or (
        authority_kind is PluginConnectionAuthorityKind.CREDENTIALLESS and scopes
    ):
        raise ValueError("plugin_authority_invalid")
    return {
        "plugin_id": _text(plugin_id),
        "app_id": _text(app_id),
        "account_key_hash": _digest(account_key_hash),
        "connection_revision": _digest(connection_revision),
        "provider_scopes": list(scopes),
        "capability_ceiling": list(_tuple(capability_ceiling)),
        "allowed_tool_schemas": dict(_mapping(allowed_tool_schemas, digests=True)),
        "source_constraints": _plain(_mapping(source_constraints)),
        "destination_constraints": _plain(_mapping(destination_constraints)),
        "execution_policy": execution_policy.value,
        "authority_kind": authority_kind.value,
    }


def plugin_connection_authority_revision(
    *,
    plugin_id: str,
    app_id: str,
    account_key_hash: str,
    connection_revision: str,
    provider_scopes: tuple[str, ...],
    capability_ceiling: tuple[str, ...],
    allowed_tool_schemas: Mapping[str, str],
    source_constraints: Mapping[str, object],
    destination_constraints: Mapping[str, object],
    execution_policy: PluginExecutionPolicy,
    authority_kind: PluginConnectionAuthorityKind,
) -> str:
    return sha256_hex(
        canonical_bytes(
            _connection_document(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_hash=account_key_hash,
                connection_revision=connection_revision,
                provider_scopes=provider_scopes,
                capability_ceiling=capability_ceiling,
                allowed_tool_schemas=allowed_tool_schemas,
                source_constraints=source_constraints,
                destination_constraints=destination_constraints,
                execution_policy=execution_policy,
                authority_kind=authority_kind,
            )
        )
    )


def plugin_connection_binding_revision(
    authority: PluginConnectionAuthority,
    *,
    capability_revision: str,
    capability_binding_revision: str,
    capability_binding_identities: tuple[PluginCapabilityBindingIdentity, ...],
) -> str:
    """Compile one non-empty signed authority/capability binding revision.

    Connection status can use this content-free digest to prove that the
    published authority, signed capability revision, and exact signed surface
    identities are all present.  It deliberately contains no provider identity
    or bearer material. Tools retain their live schema ceiling; non-tool
    surfaces are bound by their explicit kind, capability, and signed digest.
    """

    if type(authority) is not PluginConnectionAuthority:
        raise ValueError("plugin_authority_invalid")
    capability = _digest(capability_revision)
    expected_bindings = _digest(capability_binding_revision)
    if (
        type(capability_binding_identities) is not tuple
        or not capability_binding_identities
        or any(
            type(item) is not PluginCapabilityBindingIdentity
            for item in capability_binding_identities
        )
    ):
        raise ValueError("plugin_authority_invalid")
    identities = tuple(capability_binding_identities)
    ordering = tuple(
        (item.capability_kind, item.binding_sha256)
        for item in identities
    )
    if ordering != tuple(sorted(ordering)) or len(set(ordering)) != len(ordering):
        raise ValueError("plugin_authority_invalid")
    actual_bindings = sha256_hex(
        canonical_bytes(
            {
                "capability_binding_identities": [
                    item.to_dict() for item in identities
                ]
            }
        )
    )
    ceiling = tuple(
        item.removeprefix("tool:")
        for item in authority.capability_ceiling
        if item.startswith("tool:")
    )
    tool_count = sum(item.capability_kind == "tool" for item in identities)
    if (
        actual_bindings != expected_bindings
        or len(ceiling) != len(authority.capability_ceiling)
        or set(ceiling) != set(authority.allowed_tool_schemas)
        or (tool_count == 1) != bool(ceiling)
    ):
        raise ValueError("plugin_authority_invalid")
    return sha256_hex(
        canonical_bytes(
            {
                "authority_revision": authority.revision,
                "capability_revision": capability,
                "capability_binding_revision": expected_bindings,
            }
        )
    )


@dataclass(frozen=True, slots=True)
class PluginConnectionAuthority:
    plugin_id: str
    app_id: str
    account_key_hash: str
    connection_revision: str
    provider_scopes: tuple[str, ...]
    capability_ceiling: tuple[str, ...]
    allowed_tool_schemas: Mapping[str, str]
    source_constraints: Mapping[str, object]
    destination_constraints: Mapping[str, object]
    execution_policy: PluginExecutionPolicy
    authority_kind: PluginConnectionAuthorityKind
    revision: str

    def __post_init__(self) -> None:
        try:
            policy = PluginExecutionPolicy(self.execution_policy)
            authority_kind = PluginConnectionAuthorityKind(self.authority_kind)
        except (TypeError, ValueError):
            raise ValueError("plugin_authority_invalid") from None
        document = _connection_document(
            plugin_id=self.plugin_id,
            app_id=self.app_id,
            account_key_hash=self.account_key_hash,
            connection_revision=self.connection_revision,
            provider_scopes=self.provider_scopes,
            capability_ceiling=self.capability_ceiling,
            allowed_tool_schemas=self.allowed_tool_schemas,
            source_constraints=self.source_constraints,
            destination_constraints=self.destination_constraints,
            execution_policy=policy,
            authority_kind=authority_kind,
        )
        expected = sha256_hex(canonical_bytes(document))
        if _digest(self.revision) != expected:
            raise ValueError("plugin_authority_invalid")
        object.__setattr__(self, "plugin_id", document["plugin_id"])
        object.__setattr__(self, "app_id", document["app_id"])
        object.__setattr__(self, "account_key_hash", document["account_key_hash"])
        object.__setattr__(self, "connection_revision", document["connection_revision"])
        object.__setattr__(self, "provider_scopes", tuple(document["provider_scopes"]))
        object.__setattr__(self, "capability_ceiling", tuple(document["capability_ceiling"]))
        object.__setattr__(
            self, "allowed_tool_schemas", _mapping(document["allowed_tool_schemas"], digests=True)
        )
        object.__setattr__(self, "source_constraints", _mapping(document["source_constraints"]))
        object.__setattr__(
            self, "destination_constraints", _mapping(document["destination_constraints"])
        )
        object.__setattr__(self, "execution_policy", policy)
        object.__setattr__(self, "authority_kind", authority_kind)
        object.__setattr__(self, "revision", expected)

    def to_dict(self) -> dict[str, object]:
        return {
            **_connection_document(
                plugin_id=self.plugin_id,
                app_id=self.app_id,
                account_key_hash=self.account_key_hash,
                connection_revision=self.connection_revision,
                provider_scopes=self.provider_scopes,
                capability_ceiling=self.capability_ceiling,
                allowed_tool_schemas=self.allowed_tool_schemas,
                source_constraints=self.source_constraints,
                destination_constraints=self.destination_constraints,
                execution_policy=self.execution_policy,
                authority_kind=self.authority_kind,
            ),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginConnectionAuthority:
        if type(value) is not dict or set(value) != {
            "plugin_id",
            "app_id",
            "account_key_hash",
            "connection_revision",
            "provider_scopes",
            "capability_ceiling",
            "allowed_tool_schemas",
            "source_constraints",
            "destination_constraints",
            "execution_policy",
            "authority_kind",
            "revision",
        }:
            raise ValueError("plugin_authority_invalid")
        try:
            return cls(**value)
        except (TypeError, ValueError):
            raise ValueError("plugin_authority_invalid") from None


def _authority_to_dict(authority: McpAuthoritySnapshot) -> dict[str, object]:
    return {
        "authority_kind": authority.authority_kind,
        "authority_id": authority.authority_id,
        "authority_revision": authority.authority_revision,
        "episode_id": authority.episode_id,
        "job_id": authority.job_id,
        "job_revision": authority.job_revision,
        "connection_id": authority.connection_id,
        "connection_revision": authority.connection_revision,
        "server_id": authority.server_id,
        "server_version": authority.server_version,
        "artifact_sha256": authority.artifact_sha256,
        "tool_name": authority.tool_name,
        "tool_schema_sha256": authority.tool_schema_sha256,
        "capability_tags": list(authority.capability_tags),
        "source_constraints": _plain(authority.source_constraints),
        "destination_constraints": _plain(authority.destination_constraints),
        "risk_classes": list(authority.risk_classes),
        "direct_execution": authority.direct_execution,
        "standing_execution": authority.standing_execution,
        "allow_send_publish": authority.allow_send_publish,
        "allow_destructive": authority.allow_destructive,
        "allow_cross_provider": authority.allow_cross_provider,
        "plan_digest": authority.plan_digest,
        "account_key_hash": authority.account_key_hash,
        "candidate_id": authority.candidate_id,
        "plan_step_id": authority.plan_step_id,
        "arguments_sha256": authority.arguments_sha256,
        "connection_authority_revision": authority.connection_authority_revision,
        "effect": authority.effect,
        "allowed_tools": [dict(entry) for entry in authority.allowed_tools],
        "argument_constraints": _plain(authority.argument_constraints),
    }


def _authority_from_dict(value: object) -> McpAuthoritySnapshot:
    if type(value) is not dict:
        raise ValueError("plugin_authority_invalid")
    try:
        return McpAuthoritySnapshot(**value)
    except (TypeError, ValueError):
        raise ValueError("plugin_authority_invalid") from None


class PluginAuthorityStore:
    def __init__(
        self,
        *,
        path: Path | None = None,
        max_document_bytes: int = _MAX_BYTES,
        max_records: int = _MAX_RECORDS,
    ) -> None:
        selected = Path(path or MCP_PLUGIN_AUTHORITY_STATE_PATH)
        if type(max_records) is not int or max_records <= 0 or max_records > _MAX_RECORDS:
            raise ValueError("plugin_authority_limit_invalid")
        self._max_records = max_records
        self.path = selected
        self._state = McpStateStore(
            state_root=selected.parent,
            document_path=selected,
            invalid_path_reason="plugin_authority_store_unavailable",
            max_document_bytes=max_document_bytes,
        )

    @staticmethod
    def _empty() -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "connections": {},
            "authorities": {},
            "executions": {},
        }

    @staticmethod
    def _execution_parent_key(
        kind: str,
        connection_key: str,
        authority_key: str = "",
    ) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "kind": kind,
                    "connection_key": connection_key,
                    "authority_key": authority_key,
                }
            )
        )

    @staticmethod
    def _execution_revision(record: Mapping[str, object]) -> str:
        return sha256_hex(
            canonical_bytes({key: value for key, value in record.items() if key != "revision"})
        )

    @staticmethod
    def _baseline(record: Mapping[str, object] | None) -> dict[str, object]:
        if record is None:
            return {
                "exists": False,
                "lineage_key": "",
                "active_episode_id": "",
                "used_execution_keys": [],
                "current_execution_revision": "",
            }
        return {
            "exists": True,
            "lineage_key": record["lineage_key"],
            "active_episode_id": record["active_episode_id"],
            "used_execution_keys": list(record["used_execution_keys"]),
            "current_execution_revision": record["current_execution_revision"],
        }

    @staticmethod
    def _validate_baseline(value: object) -> None:
        if type(value) is not dict or set(value) != {
            "exists",
            "lineage_key",
            "active_episode_id",
            "used_execution_keys",
            "current_execution_revision",
        }:
            raise ValueError("plugin_authority_store_unavailable")
        if type(value["exists"]) is not bool:
            raise ValueError("plugin_authority_store_unavailable")
        for field in ("lineage_key", "active_episode_id", "current_execution_revision"):
            if type(value[field]) is not str:
                raise ValueError("plugin_authority_store_unavailable")
        used = value["used_execution_keys"]
        if type(used) is not list or len(used) > _MAX_PLAN_STEPS or len(used) != len(set(used)):
            raise ValueError("plugin_authority_store_unavailable")
        for item in used:
            _digest(item)
        if not value["exists"] and any((value["lineage_key"], value["active_episode_id"], used)):
            raise ValueError("plugin_authority_store_unavailable")
        if value["current_execution_revision"]:
            _digest(value["current_execution_revision"])

    @classmethod
    def _validate_recovery(cls, value: object, *, committed: bool) -> None:
        expected = {
            "reservation_id",
            "execution_key",
            "process_epoch",
            "next_execution_revision",
            "base_revision",
            "baseline",
        }
        if committed:
            expected.add("reservation_revision")
        if type(value) is not dict or set(value) != expected:
            raise ValueError("plugin_authority_store_unavailable")
        for field in ("reservation_id", "execution_key", "process_epoch", "base_revision"):
            _digest(value[field])
        if type(value["next_execution_revision"]) is not str:
            raise ValueError("plugin_authority_store_unavailable")
        if value["next_execution_revision"]:
            _digest(value["next_execution_revision"])
        if committed:
            _digest(value["reservation_revision"])
        cls._validate_baseline(value["baseline"])

    @classmethod
    def _validate_execution(cls, parent_key: str, record: object) -> None:
        if type(record) is not dict or set(record) != {
            "parent_key",
            "kind",
            "connection_key",
            "authority_key",
            "lineage_key",
            "active_episode_id",
            "used_execution_keys",
            "current_execution_revision",
            "reservation",
            "last_commit",
            "revision",
        }:
            raise ValueError("plugin_authority_store_unavailable")
        if parent_key != record["parent_key"] or record["kind"] not in {
            "direct_turn",
            "standing_grant",
        }:
            raise ValueError("plugin_authority_store_unavailable")
        for field in ("parent_key", "connection_key", "revision"):
            _digest(record[field])
        used = record["used_execution_keys"]
        if type(used) is not list or len(used) > _MAX_PLAN_STEPS or len(used) != len(set(used)):
            raise ValueError("plugin_authority_store_unavailable")
        for item in used:
            _digest(item)
        if record["kind"] == "direct_turn":
            _digest(record["lineage_key"])
            _text(record["active_episode_id"])
            if record["authority_key"] or record["current_execution_revision"]:
                raise ValueError("plugin_authority_store_unavailable")
        else:
            _digest(record["authority_key"])
            _digest(record["current_execution_revision"])
            if record["lineage_key"] or record["active_episode_id"] or used:
                raise ValueError("plugin_authority_store_unavailable")
        if record["reservation"] is not None and record["last_commit"] is not None:
            raise ValueError("plugin_authority_store_unavailable")
        if record["reservation"] is not None:
            cls._validate_recovery(record["reservation"], committed=False)
        if record["last_commit"] is not None:
            cls._validate_recovery(record["last_commit"], committed=True)
        if record["revision"] != cls._execution_revision(record):
            raise ValueError("plugin_authority_store_unavailable")

    @classmethod
    def _migrate_schema_two(cls, document: dict[str, object]) -> dict[str, object]:
        if document.get("schema_version") != 2 or set(document) != {
            "schema_version",
            "connections",
            "authorities",
            "executions",
        }:
            raise ValueError("plugin_authority_store_unavailable")
        connections = document["connections"]
        authorities = document["authorities"]
        executions = document["executions"]
        if (
            type(connections) is not dict
            or type(authorities) is not dict
            or type(executions) is not dict
        ):
            raise ValueError("plugin_authority_store_unavailable")
        migrated: dict[str, object] = {
            "schema_version": 3,
            "connections": connections,
            "authorities": authorities,
            "executions": {},
        }
        for old_key, old_value in executions.items():
            if (
                type(old_key) is not str
                or _DIGEST.fullmatch(old_key) is None
                or type(old_value) is not dict
                or set(old_value)
                != {
                    "execution_key",
                    "kind",
                    "connection_key",
                    "status",
                    "process_epoch",
                    "reservation_id",
                    "revision",
                }
                or old_value["execution_key"] != old_key
                or old_value["kind"] not in {"direct_turn", "standing_grant"}
                or old_value["status"] not in {"reserved", "consumed"}
            ):
                raise ValueError("plugin_authority_store_unavailable")
            connection_key = old_value["connection_key"]
            if (
                type(connection_key) is not str
                or _DIGEST.fullmatch(connection_key) is None
                or connection_key not in connections
            ):
                raise ValueError("plugin_authority_store_unavailable")
            for field in ("process_epoch", "reservation_id", "revision"):
                _digest(old_value[field])
            # A reservation that was not delivered before the schema upgrade
            # has no safe consumer.  Restart recovery retires it instead of
            # making a stale handle look usable.
            if old_value["status"] == "reserved":
                continue
            if old_value["kind"] == "direct_turn":
                parent_key = cls._execution_parent_key("direct_turn", connection_key)
                baseline = {
                    "exists": False,
                    "lineage_key": "",
                    "active_episode_id": "",
                    "used_execution_keys": [],
                    "current_execution_revision": "",
                }
                record: dict[str, object] = {
                    "parent_key": parent_key,
                    "kind": "direct_turn",
                    "connection_key": connection_key,
                    "authority_key": "",
                    "lineage_key": old_key,
                    "active_episode_id": f"migrated-{old_key}",
                    "used_execution_keys": [old_key],
                    "current_execution_revision": "",
                    "reservation": None,
                    "last_commit": {
                        "reservation_id": old_value["reservation_id"],
                        "execution_key": old_key,
                        "process_epoch": old_value["process_epoch"],
                        "next_execution_revision": "",
                        "base_revision": _ZERO_REVISION,
                        "baseline": baseline,
                        "reservation_revision": old_value["revision"],
                    },
                    "revision": "",
                }
                record["revision"] = cls._execution_revision(record)
                migrated["executions"][parent_key] = record
                continue
            matching = [
                (authority_key, raw)
                for authority_key, raw in authorities.items()
                if type(raw) is dict and raw.get("connection_key") == connection_key
            ]
            if not matching:
                raise ValueError("plugin_authority_store_unavailable")
            for authority_key, raw in matching:
                snapshot = raw.get("snapshot")
                if type(snapshot) is not dict:
                    raise ValueError("plugin_authority_store_unavailable")
                prior_revision = snapshot.get("authority_revision")
                _digest(prior_revision)
                next_revision = sha256_hex(
                    canonical_bytes(
                        {
                            "migration": "schema-2",
                            "authority_revision": prior_revision,
                            "execution_key": old_key,
                        }
                    )
                )
                snapshot = {**snapshot, "authority_revision": next_revision}
                _authority_from_dict(snapshot)
                raw["snapshot"] = snapshot
                parent_key = cls._execution_parent_key(
                    "standing_grant", connection_key, authority_key
                )
                record = {
                    "parent_key": parent_key,
                    "kind": "standing_grant",
                    "connection_key": connection_key,
                    "authority_key": authority_key,
                    "lineage_key": "",
                    "active_episode_id": "",
                    "used_execution_keys": [],
                    "current_execution_revision": next_revision,
                    "reservation": None,
                    "last_commit": None,
                    "revision": "",
                }
                record["revision"] = cls._execution_revision(record)
                migrated["executions"][parent_key] = record
        return migrated

    @staticmethod
    def _migrate_schema_three(document: dict[str, object]) -> dict[str, object]:
        if (
            type(document) is not dict
            or set(document) != {"schema_version", "connections", "authorities", "executions"}
            or document.get("schema_version") != 3
            or type(document.get("connections")) is not dict
            or type(document.get("authorities")) is not dict
            or type(document.get("executions")) is not dict
        ):
            raise ValueError("plugin_authority_store_unavailable")
        return {
            "schema_version": _SCHEMA_VERSION,
            "connections": {},
            "authorities": {},
            "executions": {},
        }

    def _read_locked(self) -> dict[str, object]:
        raw = self._state.read_bytes()
        if raw is None:
            return self._empty()
        try:
            document = json.loads(raw.decode("utf-8"))
            if type(document) is not dict:
                raise ValueError
            if document.get("schema_version") == 2:
                document = self._migrate_schema_two(document)
            if document.get("schema_version") == 3:
                document = self._migrate_schema_three(document)
                self._write_locked(document)
            if (
                set(document) != {"schema_version", "connections", "authorities", "executions"}
                or document["schema_version"] != _SCHEMA_VERSION
                or type(document["connections"]) is not dict
                or type(document["authorities"]) is not dict
                or type(document["executions"]) is not dict
            ):
                raise ValueError
            for value in document["connections"].values():
                PluginConnectionAuthority.from_dict(value)
            for value in document["authorities"].values():
                if type(value) is not dict or set(value) != {"connection_key", "snapshot"}:
                    raise ValueError
                _digest(value["connection_key"])
                _authority_from_dict(value["snapshot"])
            live_reservations = 0
            for parent_key, value in document["executions"].items():
                self._validate_execution(parent_key, value)
                if value["connection_key"] not in document["connections"]:
                    raise ValueError
                if value["kind"] == "standing_grant":
                    raw_authority = document["authorities"].get(value["authority_key"])
                    if (
                        raw_authority is None
                        or raw_authority["connection_key"] != value["connection_key"]
                        or raw_authority["snapshot"].get("authority_revision")
                        != value["current_execution_revision"]
                    ):
                        raise ValueError
                elif value["authority_key"]:
                    raise ValueError
                live_reservations += int(value["reservation"] is not None)
            if len(document["connections"]) + len(
                document["authorities"]
            ) + live_reservations > self._max_records or len(document["executions"]) > len(
                document["connections"]
            ) + len(document["authorities"]):
                raise ValueError
            return document
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ValueError("plugin_authority_store_unavailable") from None

    def _write_locked(self, document: dict[str, object]) -> None:
        payload = canonical_bytes(document)
        if len(payload) > self._state.max_document_bytes:
            raise ValueError("plugin_authority_store_limit_exceeded")
        self._state.write_bytes(payload)

    def reconcile(self) -> None:
        """Recover every abandoned reservation at an owner-fenced boundary."""

        with self._state.locked():
            document = self._read_locked()
            recovered = False
            for parent_key, record in tuple(document["executions"].items()):
                reservation = record["reservation"]
                if reservation is not None:
                    self._restore_execution(document, parent_key, record)
                    recovered = True
            if recovered:
                self._write_locked(document)

    @staticmethod
    def _connection_key(plugin_id: str, app_id: str, account_key_hash: str) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "plugin_id": _text(plugin_id),
                    "app_id": _text(app_id),
                    "account_key_hash": _digest(account_key_hash),
                }
            )
        )

    @staticmethod
    def _authority_key(kind: str, authority_id: str) -> str:
        return sha256_hex(
            canonical_bytes({"kind": _text(kind), "authority_id": _text(authority_id)})
        )

    def connection(
        self, plugin_id: str, app_id: str, account_key_hash: str
    ) -> PluginConnectionAuthority:
        key = self._connection_key(plugin_id, app_id, account_key_hash)
        with self._state.locked():
            value = self._read_locked()["connections"].get(key)
        if value is None:
            raise ValueError("plugin_connection_authority_not_found")
        return PluginConnectionAuthority.from_dict(value)

    @classmethod
    def _connection_lineage_checkpoint(cls, value: object) -> dict[str, object]:
        if type(value) is not dict or set(value) != {
            "plugin_id",
            "app_id",
            "account_key_hash",
            "connection_key",
            "connection",
            "authorities",
            "executions",
            "revision",
        }:
            raise ValueError("plugin_authority_recovery_invalid")
        try:
            plugin_id = _text(value["plugin_id"])
            app_id = _text(value["app_id"])
            account = _digest(value["account_key_hash"])
            connection_key = _digest(value["connection_key"])
            if connection_key != cls._connection_key(plugin_id, app_id, account):
                raise ValueError
            raw_connection = value["connection"]
            connection = (
                None
                if raw_connection is None
                else PluginConnectionAuthority.from_dict(raw_connection).to_dict()
            )
            if connection is not None and (
                connection["plugin_id"] != plugin_id
                or connection["app_id"] != app_id
                or connection["account_key_hash"] != account
            ):
                raise ValueError
            raw_authorities = value["authorities"]
            raw_executions = value["executions"]
            if type(raw_authorities) is not dict or type(raw_executions) is not dict:
                raise ValueError
            authorities: dict[str, object] = {}
            for key, raw in raw_authorities.items():
                authority_key = _digest(key)
                if (
                    type(raw) is not dict
                    or set(raw) != {"connection_key", "snapshot"}
                    or raw["connection_key"] != connection_key
                ):
                    raise ValueError
                _authority_from_dict(raw["snapshot"])
                authorities[authority_key] = raw
            executions: dict[str, object] = {}
            for key, raw in raw_executions.items():
                parent_key = _digest(key)
                cls._validate_execution(parent_key, raw)
                if raw["connection_key"] != connection_key or (
                    raw["kind"] == "standing_grant"
                    and raw["authority_key"] not in authorities
                ):
                    raise ValueError
                executions[parent_key] = raw
            if connection is None and (authorities or executions):
                raise ValueError
            material = {
                "plugin_id": plugin_id,
                "app_id": app_id,
                "account_key_hash": account,
                "connection_key": connection_key,
                "connection": connection,
                "authorities": authorities,
                "executions": executions,
            }
            revision = sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.plugin-connection-lineage.v1",
                        "material": material,
                    }
                )
            )
            if value["revision"] != revision:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("plugin_authority_recovery_invalid") from None
        return {**material, "revision": revision}

    def checkpoint_connection_lineage(
        self,
        plugin_id: str,
        app_id: str,
        account_key_hash: str,
    ) -> dict[str, object]:
        plugin = _text(plugin_id)
        app = _text(app_id)
        account = _digest(account_key_hash)
        key = self._connection_key(plugin, app, account)
        with self._state.locked():
            document = self._read_locked()
            connection = document["connections"].get(key)
            authorities = {
                authority_key: raw
                for authority_key, raw in document["authorities"].items()
                if raw["connection_key"] == key
            }
            executions = {
                execution_key: raw
                for execution_key, raw in document["executions"].items()
                if raw["connection_key"] == key
            }
        material = {
            "plugin_id": plugin,
            "app_id": app,
            "account_key_hash": account,
            "connection_key": key,
            "connection": connection,
            "authorities": authorities,
            "executions": executions,
        }
        material["revision"] = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.plugin-connection-lineage.v1",
                    "material": material,
                }
            )
        )
        return self._connection_lineage_checkpoint(material)

    def restore_connection_lineage(self, checkpoint: object) -> None:
        checked = self._connection_lineage_checkpoint(checkpoint)
        key = str(checked["connection_key"])
        with self._state.locked():
            document = self._read_locked()
            document["connections"].pop(key, None)
            for authority_key, raw in tuple(document["authorities"].items()):
                if raw["connection_key"] == key:
                    document["authorities"].pop(authority_key)
            for execution_key, raw in tuple(document["executions"].items()):
                if raw["connection_key"] == key:
                    document["executions"].pop(execution_key)
            connection = checked["connection"]
            if connection is not None:
                document["connections"][key] = connection
                document["authorities"].update(checked["authorities"])
                document["executions"].update(checked["executions"])
            if len(document["connections"]) + len(document["authorities"]) > (
                self._max_records
            ):
                raise ValueError("plugin_authority_store_limit_exceeded")
            self._write_locked(document)

    def require_connection_lineage_exact(self, checkpoint: object) -> None:
        """Require one sealed connection and every descendant to remain exact."""

        checked = self._connection_lineage_checkpoint(checkpoint)
        key = str(checked["connection_key"])
        with self._state.locked():
            document = self._read_locked()
            current = {
                "plugin_id": checked["plugin_id"],
                "app_id": checked["app_id"],
                "account_key_hash": checked["account_key_hash"],
                "connection_key": key,
                "connection": document["connections"].get(key),
                "authorities": {
                    authority_key: raw
                    for authority_key, raw in document["authorities"].items()
                    if raw["connection_key"] == key
                },
                "executions": {
                    execution_key: raw
                    for execution_key, raw in document["executions"].items()
                    if raw["connection_key"] == key
                },
            }
        current["revision"] = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.plugin-connection-lineage.v1",
                    "material": current,
                }
            )
        )
        if self._connection_lineage_checkpoint(current) != checked:
            raise ValueError("plugin_authority_revision_conflict")

    def compare_and_invalidate_connection_lineage(self, checkpoint: object) -> int:
        """Delete only an exact sealed connection lineage, idempotently if absent."""

        checked = self._connection_lineage_checkpoint(checkpoint)
        key = str(checked["connection_key"])
        with self._state.locked():
            document = self._read_locked()
            connection = document["connections"].get(key)
            authorities = {
                authority_key: raw
                for authority_key, raw in document["authorities"].items()
                if raw["connection_key"] == key
            }
            executions = {
                execution_key: raw
                for execution_key, raw in document["executions"].items()
                if raw["connection_key"] == key
            }
            if connection is None and not authorities and not executions:
                return 0
            current = {
                "plugin_id": checked["plugin_id"],
                "app_id": checked["app_id"],
                "account_key_hash": checked["account_key_hash"],
                "connection_key": key,
                "connection": connection,
                "authorities": authorities,
                "executions": executions,
            }
            current["revision"] = sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.plugin-connection-lineage.v1",
                        "material": current,
                    }
                )
            )
            if self._connection_lineage_checkpoint(current) != checked:
                raise ValueError("plugin_authority_revision_conflict")
            removed = 1 + len(authorities) + len(executions)
            document["connections"].pop(key)
            for authority_key in authorities:
                document["authorities"].pop(authority_key)
            for execution_key in executions:
                document["executions"].pop(execution_key)
            self._write_locked(document)
            return removed

    def publish_connection(
        self, authority: PluginConnectionAuthority, *, expected_revision: str
    ) -> PluginConnectionAuthority:
        if type(authority) is not PluginConnectionAuthority or type(expected_revision) is not str:
            raise ValueError("plugin_authority_invalid")
        key = self._connection_key(
            authority.plugin_id, authority.app_id, authority.account_key_hash
        )
        with self._state.locked():
            document = self._read_locked()
            connections = document["connections"]
            current = connections.get(key)
            current_revision = "" if current is None else current.get("revision")
            if current_revision != expected_revision:
                raise ValueError("plugin_authority_revision_conflict")
            if (
                current is None
                and len(connections)
                + len(document["authorities"])
                + sum(value["reservation"] is not None for value in document["executions"].values())
                >= self._max_records
            ):
                raise ValueError("plugin_authority_store_limit_exceeded")
            connections[key] = authority.to_dict()
            if current is not None and current_revision != authority.revision:
                for authority_key, raw in tuple(document["authorities"].items()):
                    if raw["connection_key"] == key:
                        document["authorities"].pop(authority_key)
                for execution_key, raw in tuple(document["executions"].items()):
                    if raw["connection_key"] == key:
                        document["executions"].pop(execution_key)
            self._write_locked(document)
        return authority

    def authority(self, kind: str, authority_id: str) -> McpAuthoritySnapshot:
        key = self._authority_key(kind, authority_id)
        with self._state.locked():
            value = self._read_locked()["authorities"].get(key)
        if value is None:
            raise ValueError("plugin_authority_not_found")
        authority = _authority_from_dict(value["snapshot"])
        if authority.authority_kind != kind or authority.authority_id != authority_id:
            raise ValueError("plugin_authority_store_unavailable")
        return authority

    @staticmethod
    def _template_capability_scope_ok(
        authority: McpAuthoritySnapshot, raw_connection: Mapping[str, object]
    ) -> bool:
        """Does a template grant's ``capability_tags`` set exactly match the
        capabilities its ``allowed_tools`` entries name?

        Fix round 1 derived "the capability implied by an allowed_tools
        entry" through a schema-digest proxy (``allowed_tools`` carried no
        capability field), which a schema collision between two distinct
        tools could fool into accepting fewer tags than there are entries.
        Round 2 made each entry fully self-describing -- it now carries its
        own ``capability`` -- so this is exact set identity, not a proxy:
        every declared tag must be unambiguous and present in the
        connection's capability ceiling, and the set of tags must equal
        *exactly* the set of ``"tool:"``-prefixed capabilities named across
        ``allowed_tools`` -- neither a tag with no covering entry, nor an
        entry with no covering tag. ``McpAuthoritySnapshot`` already
        guarantees ``capability_tags`` has no duplicates.
        """

        tags = authority.capability_tags
        ceiling = raw_connection["capability_ceiling"]
        if not tags or any(
            not tag.startswith("tool:")
            or tag not in ceiling
            or tag.removeprefix("tool:") in ceiling
            for tag in tags
        ):
            return False
        declared_tags = {f"tool:{entry['capability']}" for entry in authority.allowed_tools}
        return set(tags) == declared_tags

    def publish_standing_grant(
        self, authority: McpAuthoritySnapshot, *, expected_revision: str
    ) -> McpAuthoritySnapshot:
        if (
            type(authority) is not McpAuthoritySnapshot
            or authority.authority_kind != "standing_grant"
            or not (authority.plan_digest or authority.argument_constraints)
            or not authority.account_key_hash
            or type(expected_revision) is not str
        ):
            raise ValueError("plugin_authority_invalid")
        key = self._authority_key(authority.authority_kind, authority.authority_id)
        with self._state.locked():
            document = self._read_locked()
            authorities = document["authorities"]
            current = authorities.get(key)
            current_revision = (
                "" if current is None else current["snapshot"].get("authority_revision")
            )
            if current_revision != expected_revision:
                raise ValueError("plugin_authority_revision_conflict")
            if (
                current is None
                and len(document["connections"])
                + len(authorities)
                + sum(value["reservation"] is not None for value in document["executions"].values())
                >= self._max_records
            ):
                raise ValueError("plugin_authority_store_limit_exceeded")
            matching_connections = [
                connection_key
                for connection_key, raw_connection in document["connections"].items()
                if raw_connection["account_key_hash"] == authority.account_key_hash
                and raw_connection["connection_revision"] == authority.connection_revision
                and raw_connection["revision"] == authority.connection_authority_revision
                and (
                    self._template_capability_scope_ok(authority, raw_connection)
                    if authority.argument_constraints
                    else (
                        len(authority.capability_tags) == 1
                        and authority.capability_tags[0].startswith("tool:")
                        and authority.capability_tags[0] in raw_connection["capability_ceiling"]
                        and authority.capability_tags[0].removeprefix("tool:")
                        not in raw_connection["capability_ceiling"]
                        and raw_connection["allowed_tool_schemas"].get(
                            authority.capability_tags[0].removeprefix("tool:")
                        )
                        == authority.tool_schema_sha256
                    )
                )
            ]
            if len(matching_connections) != 1:
                raise ValueError("plugin_authority_connection_ambiguous")
            authorities[key] = {
                "connection_key": matching_connections[0],
                "snapshot": _authority_to_dict(authority),
            }
            if current is not None and current_revision != authority.authority_revision:
                for execution_key, raw in tuple(document["executions"].items()):
                    if raw["authority_key"] == key:
                        document["executions"].pop(execution_key)
            self._write_locked(document)
        return authority

    def reserve_execution(
        self,
        *,
        kind: str,
        execution_key: str,
        plugin_id: str,
        app_id: str,
        account_key_hash: str,
        process_epoch: str,
        reservation_id: str,
        lineage_key: str = "",
        active_episode_id: str = "",
        authority_id: str = "",
        expected_execution_revision: str = "",
        next_execution_revision: str = "",
    ) -> str | None:
        if kind not in {"direct_turn", "standing_grant"}:
            raise ValueError("plugin_authority_invalid")
        key = _digest(execution_key)
        epoch = _digest(process_epoch)
        reservation = _digest(reservation_id)
        connection_key = self._connection_key(plugin_id, app_id, account_key_hash)
        parent_key = ""
        reserved_revision = ""
        try:
            with self._state.locked():
                document = self._read_locked()
                if connection_key not in document["connections"]:
                    return None
                current: dict[str, object] | None
                authority_key = ""
                if kind == "direct_turn":
                    lineage = _digest(lineage_key)
                    episode_id = _text(active_episode_id)
                    if authority_id or expected_execution_revision or next_execution_revision:
                        raise ValueError("plugin_authority_invalid")
                    parent_key = self._execution_parent_key(kind, connection_key)
                else:
                    if lineage_key or active_episode_id:
                        raise ValueError("plugin_authority_invalid")
                    authority_key = self._authority_key(kind, _text(authority_id))
                    expected = _digest(expected_execution_revision)
                    next_revision = _digest(next_execution_revision)
                    if expected == next_revision:
                        raise ValueError("plugin_authority_invalid")
                    raw_authority = document["authorities"].get(authority_key)
                    if (
                        raw_authority is None
                        or raw_authority["connection_key"] != connection_key
                        or raw_authority["snapshot"].get("authority_revision") != expected
                    ):
                        return None
                    parent_key = self._execution_parent_key(kind, connection_key, authority_key)
                current = document["executions"].get(parent_key)
                if current is not None:
                    self._validate_execution(parent_key, current)
                    active_reservation = current["reservation"]
                    if active_reservation is not None:
                        if active_reservation["process_epoch"] == epoch:
                            return None
                        self._restore_execution(document, parent_key, current)
                        current = document["executions"].get(parent_key)
                if kind == "direct_turn":
                    if current is not None and current["lineage_key"] == lineage:
                        if (
                            current["active_episode_id"] != episode_id
                            or key in current["used_execution_keys"]
                        ):
                            return None
                        used = list(current["used_execution_keys"])
                    elif current is not None and current["active_episode_id"] == episode_id:
                        return None
                    else:
                        used = []
                    core = {
                        "parent_key": parent_key,
                        "kind": kind,
                        "connection_key": connection_key,
                        "authority_key": "",
                        "lineage_key": lineage,
                        "active_episode_id": episode_id,
                        "used_execution_keys": used,
                        "current_execution_revision": "",
                    }
                    next_revision = ""
                else:
                    if current is not None and current["current_execution_revision"] != expected:
                        return None
                    core = {
                        "parent_key": parent_key,
                        "kind": kind,
                        "connection_key": connection_key,
                        "authority_key": authority_key,
                        "lineage_key": "",
                        "active_episode_id": "",
                        "used_execution_keys": [],
                        "current_execution_revision": expected,
                    }
                live = sum(
                    value["reservation"] is not None for value in document["executions"].values()
                )
                if (
                    len(document["connections"]) + len(document["authorities"]) + live
                    >= self._max_records
                ):
                    raise ValueError("plugin_authority_store_limit_exceeded")
                baseline = self._baseline(current)
                if kind == "standing_grant" and current is None:
                    baseline["current_execution_revision"] = expected
                recovery = {
                    "reservation_id": reservation,
                    "execution_key": key,
                    "process_epoch": epoch,
                    "next_execution_revision": next_revision,
                    "base_revision": (_ZERO_REVISION if current is None else current["revision"]),
                    "baseline": baseline,
                }
                record = {
                    **core,
                    "reservation": recovery,
                    "last_commit": None,
                    "revision": "",
                }
                record["revision"] = self._execution_revision(record)
                reserved_revision = record["revision"]
                document["executions"][parent_key] = record
                self._write_locked(document)
                return reserved_revision
        except BaseException as exc:
            if parent_key:
                try:
                    self._reconcile_rollback(
                        execution_key=key,
                        reservation_id=reservation,
                        expected_revision=reserved_revision or None,
                    )
                except BaseException:
                    exc.add_note("plugin authority reserve reconciliation failed")
            raise

    def commit_execution(
        self,
        execution_key: str,
        *,
        expected_revision: str,
    ) -> None:
        key = _digest(execution_key)
        expected = _digest(expected_revision)
        reservation_id = ""
        try:
            with self._state.locked():
                document = self._read_locked()
                matches = [
                    (parent_key, record)
                    for parent_key, record in document["executions"].items()
                    if record["revision"] == expected
                    and record["reservation"] is not None
                    and record["reservation"]["execution_key"] == key
                ]
                if len(matches) != 1:
                    raise ValueError("plugin_authority_execution_conflict")
                parent_key, record = matches[0]
                recovery = dict(record["reservation"])
                reservation_id = recovery["reservation_id"]
                if record["kind"] == "direct_turn":
                    used = [*record["used_execution_keys"], key]
                    if len(used) > _MAX_PLAN_STEPS or len(used) != len(set(used)):
                        raise ValueError("plugin_authority_execution_conflict")
                    record["used_execution_keys"] = used
                else:
                    next_revision = recovery["next_execution_revision"]
                    current_revision = record["current_execution_revision"]
                    authority_record = document["authorities"].get(record["authority_key"])
                    if (
                        authority_record is None
                        or authority_record["snapshot"].get("authority_revision")
                        != current_revision
                    ):
                        raise ValueError("plugin_authority_execution_conflict")
                    record["current_execution_revision"] = next_revision
                    authority_record["snapshot"]["authority_revision"] = next_revision
                recovery["reservation_revision"] = expected
                record["reservation"] = None
                record["last_commit"] = recovery
                record["revision"] = self._execution_revision(record)
                self._write_locked(document)
        except BaseException as exc:
            if reservation_id:
                try:
                    self._reconcile_rollback(
                        execution_key=key,
                        reservation_id=reservation_id,
                        expected_revision=expected,
                    )
                except BaseException:
                    exc.add_note("plugin authority commit reconciliation failed")
            raise

    def rollback_execution(
        self,
        execution_key: str,
        *,
        expected_revision: str,
        reservation_id: str = "",
    ) -> None:
        key = _digest(execution_key)
        expected = _digest(expected_revision) if expected_revision else None
        reservation = _digest(reservation_id) if reservation_id else ""
        try:
            self._rollback_once(
                execution_key=key,
                reservation_id=reservation,
                expected_revision=expected,
            )
        except BaseException as exc:
            try:
                self._reconcile_rollback(
                    execution_key=key,
                    reservation_id=reservation,
                    expected_revision=expected,
                )
            except BaseException:
                exc.add_note("plugin authority rollback reconciliation failed")
            raise

    def reconcile_execution(
        self,
        execution_key: str,
        *,
        expected_revision: str,
        reservation_id: str,
    ) -> None:
        """Retire only the exact reservation/commit proof after an ambiguous failure."""
        self._reconcile_rollback(
            execution_key=_digest(execution_key),
            reservation_id=_digest(reservation_id),
            expected_revision=(_digest(expected_revision) if expected_revision else None),
        )

    @classmethod
    def _restore_execution(
        cls,
        document: dict[str, object],
        parent_key: str,
        record: Mapping[str, object],
    ) -> None:
        recovery = record["reservation"] or record["last_commit"]
        if type(recovery) is not dict:
            raise ValueError("plugin_authority_execution_conflict")
        baseline = recovery["baseline"]
        if record["kind"] == "standing_grant":
            authority_record = document["authorities"].get(record["authority_key"])
            if authority_record is None:
                raise ValueError("plugin_authority_execution_conflict")
            prior_revision = baseline["current_execution_revision"]
            if prior_revision:
                authority_record["snapshot"]["authority_revision"] = prior_revision
        if not baseline["exists"]:
            document["executions"].pop(parent_key, None)
            return
        restored = {
            "parent_key": record["parent_key"],
            "kind": record["kind"],
            "connection_key": record["connection_key"],
            "authority_key": record["authority_key"],
            "lineage_key": baseline["lineage_key"],
            "active_episode_id": baseline["active_episode_id"],
            "used_execution_keys": list(baseline["used_execution_keys"]),
            "current_execution_revision": baseline["current_execution_revision"],
            "reservation": None,
            "last_commit": None,
            "revision": "",
        }
        restored["revision"] = cls._execution_revision(restored)
        document["executions"][parent_key] = restored

    def _rollback_once(
        self,
        *,
        execution_key: str,
        reservation_id: str,
        expected_revision: str | None,
    ) -> None:
        with self._state.locked():
            document = self._read_locked()
            matches: list[tuple[str, dict[str, object]]] = []
            for parent_key, record in document["executions"].items():
                recovery = record["reservation"] or record["last_commit"]
                if type(recovery) is not dict or recovery["execution_key"] != execution_key:
                    continue
                if reservation_id and recovery["reservation_id"] != reservation_id:
                    continue
                if expected_revision is not None and not (
                    record["reservation"] is not None
                    and record["revision"] == expected_revision
                    or record["last_commit"] is not None
                    and recovery["reservation_revision"] == expected_revision
                ):
                    continue
                matches.append((parent_key, record))
            if not matches:
                return
            if len(matches) != 1:
                raise ValueError("plugin_authority_execution_conflict")
            parent_key, record = matches[0]
            self._restore_execution(document, parent_key, record)
            self._write_locked(document)

    def _reconcile_rollback(
        self,
        *,
        execution_key: str,
        reservation_id: str,
        expected_revision: str | None,
    ) -> None:
        failure: BaseException | None = None
        for _attempt in range(_RECOVERY_ATTEMPTS):
            try:
                self._rollback_once(
                    execution_key=execution_key,
                    reservation_id=reservation_id,
                    expected_revision=expected_revision,
                )
                return
            except BaseException as exc:
                failure = exc
        assert failure is not None
        raise failure

    def invalidate_connection(self, plugin_id: str, app_id: str, account_key_hash: str) -> int:
        key = self._connection_key(plugin_id, app_id, account_key_hash)
        with self._state.locked():
            document = self._read_locked()
            connection = document["connections"].pop(key, None)
            removed = int(connection is not None)
            if connection is not None:
                for authority_key, raw in tuple(document["authorities"].items()):
                    if raw["connection_key"] == key:
                        document["authorities"].pop(authority_key)
                        removed += 1
                for execution_key, raw in tuple(document["executions"].items()):
                    if raw["connection_key"] == key:
                        document["executions"].pop(execution_key)
                        removed += 1
            if removed:
                self._write_locked(document)
            return removed

    def revoke_standing_grant(self, authority_id: str) -> None:
        """Remove one standing-grant authority and any execution rows bound to it.

        Unlike ``invalidate_connection``, this touches neither the connection
        nor any other authority published against it -- the missing
        single-authority-revocation primitive a job's own "any failure after
        publish revokes the grant" and "revokes the grant" invariants need.
        Idempotent: revoking an already-absent authority is a no-op.
        """

        key = self._authority_key("standing_grant", _text(authority_id))
        with self._state.locked():
            document = self._read_locked()
            if document["authorities"].pop(key, None) is None:
                return
            for execution_key, raw in tuple(document["executions"].items()):
                if raw["authority_key"] == key:
                    document["executions"].pop(execution_key)
            self._write_locked(document)


class PluginAuthorityService:
    def __init__(
        self,
        *,
        store: PluginAuthorityStore,
        binding_resolver: BindingResolver,
        random_bytes: Callable[[int], bytes] = os.urandom,
        authority_evaluator: McpOperationAuthorityEvaluator | None = None,
        active_episode_id: Callable[[], str] | None = None,
    ) -> None:
        if not callable(random_bytes) or not callable(binding_resolver):
            raise ValueError("plugin_authority_randomness_invalid")
        self.store = store
        self._binding_resolver = binding_resolver
        self._random_bytes = random_bytes
        self._authority_evaluator = authority_evaluator or McpOperationAuthorityEvaluator()
        self._authority_issuer = _AuthorityIssuer()
        self._active_episode_id = active_episode_id or self._current_episode_id

    @staticmethod
    def _current_episode_id() -> str:
        from memory.episode_sram import current_episode_id

        return str(current_episode_id() or "")

    def _binding_for_step(
        self,
        plan: PluginOperationPlan,
        account_key_hash: str,
        candidate_id: str,
        step_id: str,
    ) -> tuple[PluginOperationStep, PluginToolBinding] | None:
        try:
            bindings = self._binding_resolver(plan, account_key_hash)
        except (LookupError, OSError, RuntimeError, TypeError, ValueError):
            return None
        if (
            type(bindings) is not tuple
            or len(bindings) != len(plan.steps)
            or any(type(binding) is not PluginToolBinding for binding in bindings)
            or tuple(binding.candidate_id for binding in bindings)
            != tuple(step.candidate_id for step in plan.steps)
            or any(binding.account_key_hash != account_key_hash for binding in bindings)
        ):
            return None
        selected = tuple(
            (step, binding)
            for step, binding in zip(plan.steps, bindings, strict=True)
            if step.step_id == step_id and step.candidate_id == candidate_id
        )
        return selected[0] if len(selected) == 1 else None

    def _random_digest(self) -> str:
        value = self._random_bytes(32)
        if type(value) is not bytes or len(value) != 32:
            raise ValueError("plugin_authority_randomness_invalid")
        return value.hex()

    def _mutation_scope(self) -> object:
        guard = getattr(self, "_lifecycle_mutation_guard", None)
        return nullcontext() if not callable(guard) else guard()

    def _issue_once(
        self,
        authority: McpAuthoritySnapshot,
        *,
        execution_key: str,
        connection: PluginConnectionAuthority,
        lineage_key: str = "",
        active_episode_id: str = "",
        standing_authority_id: str = "",
        expected_execution_revision: str = "",
        next_execution_revision: str = "",
    ) -> McpAuthoritySnapshot | None:
        reservation_id = self._random_digest()
        try:
            reservation_revision = self.store.reserve_execution(
                kind=authority.authority_kind,
                execution_key=execution_key,
                plugin_id=connection.plugin_id,
                app_id=connection.app_id,
                account_key_hash=connection.account_key_hash,
                process_epoch=_PROCESS_EPOCH,
                reservation_id=reservation_id,
                lineage_key=lineage_key,
                active_episode_id=active_episode_id,
                authority_id=standing_authority_id,
                expected_execution_revision=expected_execution_revision,
                next_execution_revision=next_execution_revision,
            )
        except BaseException as exc:
            try:
                self.store.rollback_execution(
                    execution_key,
                    expected_revision="",
                    reservation_id=reservation_id,
                )
            except BaseException:
                try:
                    self.store.reconcile_execution(
                        execution_key,
                        expected_revision="",
                        reservation_id=reservation_id,
                    )
                except BaseException:
                    exc.add_note("plugin authority reserve cleanup failed")
            raise
        if reservation_revision is None:
            return None
        issued: McpAuthoritySnapshot | None = None
        try:
            issued = self._authority_issuer.issue(authority)
            self.store.commit_execution(
                execution_key,
                expected_revision=reservation_revision,
            )
        except BaseException as exc:
            if issued is not None:
                self._authority_issuer.revoke(issued)
            try:
                self.store.rollback_execution(
                    execution_key,
                    expected_revision=reservation_revision,
                    reservation_id=reservation_id,
                )
            except BaseException:
                try:
                    self.store.reconcile_execution(
                        execution_key,
                        expected_revision=reservation_revision,
                        reservation_id=reservation_id,
                    )
                except BaseException:
                    exc.add_note("plugin authority reservation cleanup failed")
            raise
        return issued

    def evaluate_operation(
        self,
        proposal: McpOperationProposal,
        *,
        connection: McpConnectionSnapshot,
        authority: McpAuthoritySnapshot,
        tool: McpToolAuthority,
    ) -> McpAuthorityDecision:
        return self._authority_evaluator.evaluate(
            proposal,
            connection=connection,
            authority=authority,
            tool=tool,
        )

    @staticmethod
    def _binding_matches(
        connection: PluginConnectionAuthority,
        snapshot: McpConnectionSnapshot,
        binding: PluginToolBinding,
    ) -> bool:
        capability_id = _tool_capability_id(binding.capability)
        return (
            capability_id is not None
            and connection.account_key_hash == snapshot.account_ref
            and connection.connection_revision == snapshot.connection_revision
            and snapshot.authority_revision == connection.revision
            and binding.plugin_id == connection.plugin_id
            and binding.app_id == connection.app_id
            and binding.account_key_hash == connection.account_key_hash
            and binding.connection_revision == connection.connection_revision
            and capability_id in connection.capability_ceiling
            and binding.capability not in connection.capability_ceiling
            and connection.allowed_tool_schemas.get(binding.capability)
            == binding.input_schema_sha256
            and capability_id in snapshot.capability_ceiling
            and binding.capability not in snapshot.capability_ceiling
            and snapshot.allowed_tool_schemas.get(binding.tool_name) == binding.input_schema_sha256
            and _plain(connection.source_constraints) == _plain(snapshot.source_constraints)
            and _plain(connection.destination_constraints)
            == _plain(snapshot.destination_constraints)
        )

    def _connection_is_current(self, connection: PluginConnectionAuthority) -> bool:
        try:
            current = self.store.connection(
                connection.plugin_id,
                connection.app_id,
                connection.account_key_hash,
            )
        except ValueError:
            return False
        return current == connection

    @staticmethod
    def _snapshot(
        plan: PluginOperationPlan,
        *,
        authority_id: str,
        authority_kind: str,
        authority_revision: str,
        connection: PluginConnectionAuthority,
        snapshot: McpConnectionSnapshot,
        tool: McpToolAuthority,
        candidate_id: str,
        plan_step_id: str,
        arguments: Mapping[str, object],
        job_id: str = "",
        job_revision: str = "",
    ) -> McpAuthoritySnapshot:
        risk = tool.risk_class
        return McpAuthoritySnapshot(
            authority_kind=authority_kind,
            authority_id=authority_id,
            authority_revision=authority_revision,
            episode_id=plan.episode_id,
            job_id=job_id,
            job_revision=job_revision,
            connection_id=snapshot.connection_id,
            connection_revision=snapshot.connection_revision,
            server_id=snapshot.server_id,
            server_version=snapshot.server_version,
            artifact_sha256=snapshot.artifact_sha256,
            tool_name=tool.tool_name,
            tool_schema_sha256=tool.input_schema_sha256,
            capability_tags=tool.capability_tags,
            source_constraints=_plain(connection.source_constraints),
            destination_constraints=_plain(connection.destination_constraints),
            risk_classes=(risk.value,),
            direct_execution=authority_kind == "direct_turn",
            standing_execution=authority_kind == "standing_grant",
            allow_send_publish=risk is RiskClass.SEND_PUBLISH,
            allow_destructive=risk is RiskClass.DESTRUCTIVE,
            allow_cross_provider=risk is RiskClass.CROSS_PROVIDER_TRANSFER,
            plan_digest=plugin_plan_digest(plan),
            account_key_hash=connection.account_key_hash,
            candidate_id=candidate_id,
            plan_step_id=plan_step_id,
            arguments_sha256=sha256_hex(canonical_bytes(_plain(arguments))),
            connection_authority_revision=connection.revision,
            effect=tool.effect,
        )

    @staticmethod
    def _tool_from_binding(binding: PluginToolBinding) -> McpToolAuthority:
        if type(binding) is not PluginToolBinding:
            raise ValueError("plugin_authority_invalid")
        pointer = binding.input_schema.get("x-omega-provider-object-id-pointer")
        required = binding.input_schema.get("required")
        capability_id = _tool_capability_id(binding.capability)
        if capability_id is None:
            raise ValueError("plugin_authority_invalid")
        return McpToolAuthority(
            tool_name=binding.tool_name,
            input_schema_sha256=binding.input_schema_sha256,
            capability_tags=(capability_id,),
            risk_class=binding.risk_class,
            effect=binding.effect,
            requires_revision=binding.requires_revision,
            provider_object_id_pointer=pointer if type(pointer) is str else None,
            revision_map_required=(
                isinstance(required, (list, tuple)) and "expected_provider_revisions" in required
            ),
        )

    def authorize_direct_turn(
        self,
        plan: PluginOperationPlan,
        *,
        connection: PluginConnectionAuthority,
        connection_snapshot: McpConnectionSnapshot,
        candidate_id: str,
        step_id: str,
        active_contributor: Mapping[str, object],
    ) -> McpAuthoritySnapshot | None:
        with self._mutation_scope():
            return self._authorize_direct_turn_unfenced(
                plan,
                connection=connection,
                connection_snapshot=connection_snapshot,
                candidate_id=candidate_id,
                step_id=step_id,
                active_contributor=active_contributor,
            )

    def _authorize_direct_turn_unfenced(
        self,
        plan: PluginOperationPlan,
        *,
        connection: PluginConnectionAuthority,
        connection_snapshot: McpConnectionSnapshot,
        candidate_id: str,
        step_id: str,
        active_contributor: Mapping[str, object],
    ) -> McpAuthoritySnapshot | None:
        digest = plugin_plan_digest(plan)
        resolved = self._binding_for_step(
            plan,
            connection.account_key_hash,
            candidate_id,
            step_id,
        )
        if resolved is None:
            return None
        exact_step, binding = resolved
        try:
            tool = self._tool_from_binding(binding)
        except ValueError:
            return None
        entailed = (
            type(active_contributor) is dict
            and set(active_contributor)
            == {
                "source",
                "type",
                "episode_id",
                "objective_sha256",
                "entailed_plan_digest",
            }
            and active_contributor["source"] == "operator"
            and active_contributor["type"] == "command"
            and active_contributor["episode_id"] == plan.episode_id
            and active_contributor["objective_sha256"] == plan.objective_sha256
            and active_contributor["entailed_plan_digest"] == digest
        )
        if (
            connection.execution_policy is not PluginExecutionPolicy.DIRECT_TURN
            or binding.candidate_id != candidate_id
            or not entailed
            or not self._connection_is_current(connection)
            or not self._binding_matches(connection, connection_snapshot, binding)
        ):
            return None
        try:
            active_episode_id = _text(self._active_episode_id())
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if active_episode_id != plan.episode_id:
            return None
        material = {
            "kind": "direct_turn",
            "plan_digest": digest,
            "candidate_id": candidate_id,
            "step_id": exact_step.step_id,
            "arguments_sha256": sha256_hex(canonical_bytes(_plain(exact_step.arguments))),
            "connection_revision": connection.revision,
            "tool": tool.tool_name,
            "schema": tool.input_schema_sha256,
            "account": connection.account_key_hash,
        }
        entitlement = sha256_hex(canonical_bytes(material))
        authority_id = sha256_hex(canonical_bytes({**material, "nonce": self._random_digest()}))
        revision = sha256_hex(canonical_bytes({**material, "authority_id": authority_id}))
        authority = self._snapshot(
            plan,
            authority_id=authority_id,
            authority_kind="direct_turn",
            authority_revision=revision,
            connection=connection,
            snapshot=connection_snapshot,
            tool=tool,
            candidate_id=candidate_id,
            plan_step_id=exact_step.step_id,
            arguments=exact_step.arguments,
        )
        return self._issue_once(
            authority,
            execution_key=entitlement,
            connection=connection,
            lineage_key=sha256_hex(
                canonical_bytes(
                    {
                        "kind": "direct_lineage",
                        "episode_id": plan.episode_id,
                        "plan_digest": digest,
                        "connection_revision": connection.revision,
                        "account": connection.account_key_hash,
                    }
                )
            ),
            active_episode_id=active_episode_id,
        )

    def authorize_standing_grant(
        self,
        plan: PluginOperationPlan,
        *,
        authority_id: str,
        job_id: str,
        job_revision: str,
        execution_revision: str,
        candidate_id: str,
        step_id: str,
        connection: PluginConnectionAuthority,
    ) -> McpAuthoritySnapshot | None:
        with self._mutation_scope():
            return self._authorize_standing_grant_unfenced(
                plan,
                authority_id=authority_id,
                job_id=job_id,
                job_revision=job_revision,
                execution_revision=execution_revision,
                candidate_id=candidate_id,
                step_id=step_id,
                connection=connection,
            )

    def _authorize_standing_grant_unfenced(
        self,
        plan: PluginOperationPlan,
        *,
        authority_id: str,
        job_id: str,
        job_revision: str,
        execution_revision: str,
        candidate_id: str,
        step_id: str,
        connection: PluginConnectionAuthority,
    ) -> McpAuthoritySnapshot | None:
        try:
            execution = _digest(execution_revision)
        except ValueError:
            return None
        try:
            authority = self.store.authority("standing_grant", authority_id)
        except ValueError:
            return None
        resolved = self._binding_for_step(
            plan,
            connection.account_key_hash,
            candidate_id,
            step_id,
        )
        if resolved is None:
            return None
        exact_step, binding = resolved
        try:
            tool = self._tool_from_binding(binding)
        except ValueError:
            return None
        is_template = bool(authority.argument_constraints)
        if is_template:
            # A template grant plans fresh steps every run, so it cannot bind
            # to one exact plan/candidate/step/arguments digest the way an
            # exact-hash grant does. It binds instead to: the tool name plus
            # its sealed schema digest, risk class, effect, and capability,
            # each drawn from that tool's own ``allowed_tools`` entry (fix
            # round 2 -- every entry is now fully self-describing, so this
            # is per-tool identity, not the grant-level ``effect`` scalar or
            # the ``capability_tags``-membership check fix round 1 used);
            # the closed argument-constraint grammar; the connection
            # identity/revisions; job identity; and the single-use
            # execution-revision chain below -- every other exact-hash check
            # (connection revisions, risk-class ceiling, binding/capability-
            # ceiling identity, connection currency) stays exactly as it is.
            entry = next(
                (t for t in authority.allowed_tools if t["tool_name"] == tool.tool_name), None
            )
            if (
                execution != authority.authority_revision
                or authority.job_id != job_id
                or authority.job_revision != job_revision
                or authority.account_key_hash != connection.account_key_hash
                or authority.connection_revision != connection.connection_revision
                or authority.connection_authority_revision != connection.revision
                or entry is None
                or entry["tool_schema_sha256"] != tool.input_schema_sha256
                or entry["risk_class"] != tool.risk_class.value
                or entry["effect"] != binding.effect
                or entry["capability"] != binding.capability
                or tool.risk_class.value not in authority.risk_classes
                or not evaluate_argument_constraints(
                    authority.argument_constraints.get(tool.tool_name, {}),
                    _plain(exact_step.arguments),
                )
                or not (
                    binding.plugin_id == connection.plugin_id
                    and binding.app_id == connection.app_id
                    and binding.account_key_hash == connection.account_key_hash
                    and binding.connection_revision == connection.connection_revision
                    and _tool_capability_id(binding.capability) in connection.capability_ceiling
                    and binding.capability not in connection.capability_ceiling
                    and connection.allowed_tool_schemas.get(binding.capability)
                    == binding.input_schema_sha256
                )
                or not self._connection_is_current(connection)
            ):
                return None
        elif (
            execution != authority.authority_revision
            or authority.job_id != job_id
            or authority.job_revision != job_revision
            or authority.plan_digest != plugin_plan_digest(plan)
            or authority.account_key_hash != connection.account_key_hash
            or authority.connection_revision != connection.connection_revision
            or authority.connection_authority_revision != connection.revision
            or authority.candidate_id != binding.candidate_id
            or authority.plan_step_id != exact_step.step_id
            or authority.arguments_sha256
            != sha256_hex(canonical_bytes(_plain(exact_step.arguments)))
            or authority.tool_name != tool.tool_name
            or authority.tool_schema_sha256 != tool.input_schema_sha256
            or tuple(authority.capability_tags) != tuple(tool.capability_tags)
            or tool.risk_class.value not in authority.risk_classes
            or authority.effect != binding.effect
            or not (
                binding.plugin_id == connection.plugin_id
                and binding.app_id == connection.app_id
                and binding.account_key_hash == connection.account_key_hash
                and binding.connection_revision == connection.connection_revision
                and _tool_capability_id(binding.capability) in connection.capability_ceiling
                and binding.capability not in connection.capability_ceiling
                and connection.allowed_tool_schemas.get(binding.capability)
                == binding.input_schema_sha256
            )
            or not self._connection_is_current(connection)
        ):
            return None
        material = {
            "kind": "standing_execution",
            "grant_id": authority.authority_id,
            "grant_revision": authority.authority_revision,
            "job_id": job_id,
            "job_revision": job_revision,
            "execution_revision": execution,
            "plan_digest": plugin_plan_digest(plan),
            "candidate_id": candidate_id,
            "step_id": exact_step.step_id,
            "arguments_sha256": sha256_hex(canonical_bytes(_plain(exact_step.arguments))),
            "connection_revision": connection.revision,
            "tool": tool.tool_name,
            "schema": tool.input_schema_sha256,
            "account": connection.account_key_hash,
        }
        next_execution_revision = self._random_digest()
        material["next_execution_revision"] = next_execution_revision
        derived_id = sha256_hex(canonical_bytes(material))
        return self._issue_once(
            replace(
                authority,
                authority_id=derived_id,
                authority_revision=next_execution_revision,
            ),
            execution_key=derived_id,
            connection=connection,
            standing_authority_id=authority.authority_id,
            expected_execution_revision=execution,
            next_execution_revision=next_execution_revision,
        )


__all__ = [
    "PluginAuthorityService",
    "PluginAuthorityStore",
    "PluginCapabilityBindingIdentity",
    "PluginConnectionAuthority",
    "PluginConnectionAuthorityKind",
    "PluginExecutionPolicy",
    "plugin_connection_authority_revision",
    "plugin_connection_binding_revision",
    "plugin_plan_digest",
]
