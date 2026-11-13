"""Exact-version, transport-neutral lifecycle for the public MCP SDK v2 client."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import re
import unicodedata
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Any, Protocol
from urllib.parse import unquote

import jsonschema
from mcp import Client, MCPError, types
from mcp.client.caching import CacheConfig
from mcp.client.extension import ClientExtension
from mcp.client.subscriptions import SubscriptionLost
from mcp.server import Server
from pydantic import ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT201909, DRAFT202012

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import (
    ObservedCapabilities,
    canonical_resource_uri,
    reconcile_live_capabilities,
)
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
    RestrictedDiscoveryReceipt,
)
from mcp_platform.extensions import build_cache_config, normalize_subscription_event
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.tasks import (
    TASKS_EXTENSION_IDENTIFIER,
    CancelTaskParams,
    CancelTaskRequest,
    CancelTaskResult,
    CreateTaskResult,
    GetTaskParams,
    GetTaskRequest,
    GetTaskResult,
    TasksClientExtension,
    TaskInputRequest,
    UpdateTaskParams,
    UpdateTaskRequest,
    UpdateTaskResult,
    is_terminal_task_status,
    normalized_task,
    parse_task_input_request,
    parse_task_input_response,
    validate_create_task_result,
    validate_task_request,
    validate_task_result,
)

_CLIENT_NAME = "omega-mcp-host"
_CLIENT_VERSION = "3.0.0"
_SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
_OWNER_TASK_PREFIX = "omega-current-mcp-client:"
_IDENTITY_FIELDS = (
    "appId",
    "publisherId",
    "deployment",
    "providerId",
    "protocolVersion",
    "transport",
    "remoteOrigin",
    "authorizationIssuer",
)
_LIST_OPERATIONS = {
    McpOperationKind.TOOLS_LIST: "tools/list",
    McpOperationKind.PROMPTS_LIST: "prompts/list",
    McpOperationKind.RESOURCES_LIST: "resources/list",
    McpOperationKind.RESOURCE_TEMPLATES_LIST: "resources/templates/list",
}
_SUPPORTED_SCHEMA_VALIDATORS = frozenset(
    {
        jsonschema.Draft201909Validator,
        jsonschema.Draft202012Validator,
    }
)
_SUPPORTED_SCHEMA_IDS = frozenset(
    validator.META_SCHEMA["$id"] for validator in _SUPPORTED_SCHEMA_VALIDATORS
)
_URI_TEMPLATE_OPERATOR = frozenset("+#./;?&")
_URI_TEMPLATE_VARNAME = re.compile(
    r"(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})+"
    r"(?:\.(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})+)*\Z"
)
_URI_ATOMIC_VALUE = re.compile(r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})*\Z")
_URI_SIMPLE_VALUE = re.compile(r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2}|,)*\Z")
_URI_RESERVED_VALUE = re.compile(r"(?:[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=-]|%[0-9A-Fa-f]{2})*\Z")
_SCHEMA_SINGLE_SUBSCHEMAS = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_ARRAY_SUBSCHEMAS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_MAPPING_SUBSCHEMAS = frozenset(
    {
        "$defs",
        "definitions",
        "dependentSchemas",
        "patternProperties",
        "properties",
    }
)
_ELICITATION_SAME_INSTANCE_SINGLE = frozenset({"else", "if", "not", "then"})
_ELICITATION_SAME_INSTANCE_ARRAY = frozenset({"allOf", "anyOf", "oneOf"})


class _SdkClientBuilder(Protocol):
    def __call__(
        self,
        *,
        mode: str,
        client_info: types.Implementation,
        extensions: tuple[ClientExtension, ...],
        cache: CacheConfig | None,
    ) -> Any: ...


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class SdkClientFactory:
    """Sealed process-local capability for one descriptor-bound SDK client."""

    _builder: _SdkClientBuilder
    deployment: McpClientDeployment
    transport: McpTransport
    discovery_source: DiscoverySource
    _builder_identity: object
    _authorization_context_revision: str
    _proof: object

    @classmethod
    def for_in_process_server(cls, server: Server) -> SdkClientFactory:
        if type(server) is not Server:
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        return _issue_sdk_client_factory(
            _SDK_CLIENT_FACTORY_ISSUER,
            deployment=McpClientDeployment.LOCAL,
            transport=McpTransport.STDIO,
            builder=_InProcessSdkClientBuilder(server),
            builder_identity=server,
        )

    def __repr__(self) -> str:
        return (
            "SdkClientFactory("
            f"deployment={self.deployment.value!r}, transport={self.transport.value!r})"
        )


@dataclass(frozen=True, slots=True, eq=False)
class _InProcessSdkClientBuilder:
    server: Server

    def __call__(
        self,
        *,
        mode: str,
        client_info: types.Implementation,
        extensions: tuple[ClientExtension, ...],
        cache: CacheConfig | None,
    ) -> Client:
        return Client(
            self.server,
            mode=mode,
            client_info=client_info,
            extensions=extensions,
            cache=cache,
        )


@dataclass(frozen=True, slots=True)
class _IssuedFactoryProof:
    factory_ref: weakref.ReferenceType[SdkClientFactory]
    builder: _SdkClientBuilder
    builder_identity: object
    deployment: McpClientDeployment
    transport: McpTransport
    discovery_source: DiscoverySource
    descriptor_binding: str | None
    authorization_context_revision: str
    proof: object
    fingerprint: tuple[int, int, int, int, str, str, str, str | None, str]


_SDK_CLIENT_FACTORY_ISSUER = object()
_ISSUED_SDK_CLIENT_FACTORIES: weakref.WeakKeyDictionary[
    SdkClientFactory,
    _IssuedFactoryProof,
] = weakref.WeakKeyDictionary()


def _factory_fingerprint(
    factory: SdkClientFactory,
    *,
    builder: _SdkClientBuilder,
    builder_identity: object,
    deployment: McpClientDeployment,
    transport: McpTransport,
    discovery_source: DiscoverySource,
    descriptor_binding: str | None,
    authorization_context_revision: str,
    proof: object,
) -> tuple[int, int, int, int, str, str, str, str | None, str]:
    return (
        id(factory),
        id(builder),
        id(builder_identity),
        id(proof),
        deployment.value,
        transport.value,
        discovery_source.value,
        descriptor_binding,
        authorization_context_revision,
    )


def _descriptor_binding(descriptor: McpConnectionDescriptor) -> str:
    if type(descriptor) is not McpConnectionDescriptor:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    try:
        return sha256_hex(canonical_bytes(descriptor.to_dict()))
    except (TypeError, ValueError, UnicodeError):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)


def _snapshot_descriptor(descriptor: McpConnectionDescriptor) -> McpConnectionDescriptor:
    if type(descriptor) is not McpConnectionDescriptor:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    try:
        limits = McpClientLimits(**descriptor.limits.to_dict())
        return McpConnectionDescriptor(
            app_id=descriptor.app_id,
            publisher_id=descriptor.publisher_id,
            deployment=descriptor.deployment,
            provider_id=descriptor.provider_id,
            protocol_version=descriptor.protocol_version,
            transport=descriptor.transport,
            verified_launch_sha256=descriptor.verified_launch_sha256,
            remote_origin=descriptor.remote_origin,
            authorization_issuer=descriptor.authorization_issuer,
            authentication=descriptor.authentication,
            declared_capabilities=dict(descriptor.declared_capabilities),
            extension_versions=dict(descriptor.extension_versions),
            capability_revision=descriptor.capability_revision,
            limits=limits,
            signed_capability_authority=descriptor.signed_capability_authority,
            runtime_compatibility=descriptor.runtime_compatibility,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)


def _issue_sdk_client_factory(
    issuer: object,
    *,
    deployment: McpClientDeployment,
    transport: McpTransport,
    builder: _SdkClientBuilder,
    builder_identity: object,
    discovery_source: DiscoverySource = DiscoverySource.LIVE_OBSERVED,
    descriptor: McpConnectionDescriptor | None = None,
    authorization_context_revision: str | None = None,
) -> SdkClientFactory:
    if (
        issuer is not _SDK_CLIENT_FACTORY_ISSUER
        or type(deployment) is not McpClientDeployment
        or type(transport) is not McpTransport
        or type(discovery_source) is not DiscoverySource
        or builder_identity is None
        or not callable(builder)
        or deployment is McpClientDeployment.REMOTE
        and (
            type(descriptor) is not McpConnectionDescriptor
            or descriptor.deployment is not deployment
            or descriptor.transport is not transport
        )
        or deployment is McpClientDeployment.LOCAL
        and descriptor is not None
        or authorization_context_revision is not None
        and (
            type(authorization_context_revision) is not str
            or re.fullmatch(r"[0-9a-f]{64}", authorization_context_revision) is None
        )
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    descriptor_binding = None if descriptor is None else _descriptor_binding(descriptor)
    factory = object.__new__(SdkClientFactory)
    proof = object()
    sealed_authorization_revision = (
        authorization_context_revision
        if authorization_context_revision is not None
        else sha256_hex(
            canonical_bytes(
                {
                    "authorization": "none",
                    "factory_proof": id(proof),
                }
            )
        )
    )
    object.__setattr__(factory, "_builder", builder)
    object.__setattr__(factory, "deployment", deployment)
    object.__setattr__(factory, "transport", transport)
    object.__setattr__(factory, "discovery_source", discovery_source)
    object.__setattr__(factory, "_builder_identity", builder_identity)
    object.__setattr__(
        factory,
        "_authorization_context_revision",
        sealed_authorization_revision,
    )
    object.__setattr__(factory, "_proof", proof)
    record = _IssuedFactoryProof(
        factory_ref=weakref.ref(factory),
        builder=builder,
        builder_identity=builder_identity,
        deployment=deployment,
        transport=transport,
        discovery_source=discovery_source,
        descriptor_binding=descriptor_binding,
        authorization_context_revision=sealed_authorization_revision,
        proof=proof,
        fingerprint=_factory_fingerprint(
            factory=factory,
            builder=builder,
            builder_identity=builder_identity,
            deployment=deployment,
            transport=transport,
            discovery_source=discovery_source,
            descriptor_binding=descriptor_binding,
            authorization_context_revision=sealed_authorization_revision,
            proof=proof,
        ),
    )
    _ISSUED_SDK_CLIENT_FACTORIES[factory] = record
    return factory


class _LifecycleState(Enum):
    NEW = auto()
    STARTING = auto()
    LIVE = auto()
    CLOSING = auto()
    CLOSED = auto()


@dataclass(frozen=True, slots=True)
class _StartOutcome:
    connected: Any | None
    receipt: McpDiscoveryReceipt | None
    error: BaseException | None


@dataclass(frozen=True, slots=True)
class McpProviderRecovery:
    """Typed result of the single lifecycle-owned drift recovery lookup."""

    status: str
    approval_diff: tuple[str, ...] = ()
    retry_candidate: tuple[str, str] | None = None
    generation: int = 0

    def __post_init__(self) -> None:
        candidate = self.retry_candidate
        if (
            self.status not in {"applied", "approval_required", "reconnect_required"}
            or type(self.approval_diff) is not tuple
            or any(type(item) is not str or not item for item in self.approval_diff)
            or self.status != "approval_required" and self.approval_diff
            or candidate is not None
            and (
                type(candidate) is not tuple
                or len(candidate) != 2
                or any(type(item) is not str or not item or item.strip() != item for item in candidate)
            )
            or candidate is not None
            and self.status != "approval_required"
            or candidate is not None
            and self.approval_diff != ("update_pending",)
            or type(self.generation) is not int
            or self.generation < 0
        ):
            raise ValueError("provider recovery result is invalid")

    @property
    def applied(self) -> bool:
        return self.status == "applied"


@dataclass(frozen=True, slots=True)
class McpProviderDriftStatus:
    """Bounded, typed status for the last provider-boundary failure.

    Provider text and exception details are deliberately absent.  The phase is
    an Omega-owned label and ``reason`` is the closed client error vocabulary,
    so callers can decide whether to perform the one lifecycle-owned update
    lookup or ask the customer to reconnect without treating a failed
    discovery as an empty tool list.
    """

    phase: str
    reason: McpClientErrorReason
    plugin_id: str | None = None
    update_lookup_attempted: bool = False
    retry_attempted: bool = False
    reconnect_required: bool = False
    approval_required: bool = False
    approval_diff: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.phase) is not str
            or not self.phase
            or self.phase.strip() != self.phase
            or type(self.reason) is not McpClientErrorReason
            or (
                self.plugin_id is not None
                and (
                    type(self.plugin_id) is not str
                    or not self.plugin_id
                    or self.plugin_id.strip() != self.plugin_id
                )
            )
            or any(
                type(value) is not bool
                for value in (
                    self.update_lookup_attempted,
                    self.retry_attempted,
                    self.reconnect_required,
                )
            )
            or self.retry_attempted
            and not self.update_lookup_attempted
            or type(self.approval_required) is not bool
            or type(self.approval_diff) is not tuple
            or any(type(item) is not str or not item for item in self.approval_diff)
            or bool(self.approval_diff) and not self.approval_required
            or self.approval_required and self.reconnect_required
        ):
            raise ValueError("provider drift status is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "reason": self.reason.value,
            "plugin_id": self.plugin_id,
            "update_lookup_attempted": self.update_lookup_attempted,
            "retry_attempted": self.retry_attempted,
            "reconnect_required": self.reconnect_required,
            "approval_required": self.approval_required,
            "approval_diff": list(self.approval_diff),
        }


InputCallback = Callable[[McpInputRequest], Awaitable[Mapping[str, object]]]
ProgressCallback = Callable[[McpOperationResult], Awaitable[None]]


@dataclass(slots=True)
class _OperationContext:
    request: McpOperationRequest
    receipt: McpDiscoveryReceipt
    input_callback: InputCallback | None
    progress_callback: ProgressCallback | None
    input_request_ids: set[str]
    progress_events: int = 0
    input_rounds: int = 0
    input_requests: int = 0
    input_fields: int = 0
    callback_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _OperationResponse:
    status: McpOperationStatus
    data: Mapping[str, object]
    task_handle: McpTaskHandle | None = None


@dataclass(frozen=True, slots=True)
class _TaskInputAuthority:
    request: TaskInputRequest
    generation: int
    fingerprint: str
    validator: jsonschema.protocols.Validator


@dataclass(slots=True)
class _LiveTask:
    task_id: str
    app_id: str
    capability_id: str
    capability_revision: str
    connection_revision: str
    authorization_context_revision: str
    output_schema: Mapping[str, Any] | None
    signed_policy: Mapping[str, Any] | None
    started_at: float
    deadline_at: float
    polls: int = 0
    updates: int = 0
    terminal: bool = False
    next_poll_at: float = 0
    input_generation: int = 0
    outstanding_input_requests: dict[str, _TaskInputAuthority] = field(default_factory=dict)
    input_reservations: dict[tuple[str, int, str], object] = field(default_factory=dict)
    used_input_request_ids: set[str] = field(default_factory=set)

    def public_handle(self) -> McpTaskHandle:
        return McpTaskHandle(
            task_id=self.task_id,
            app_id=self.app_id,
            capability_id=self.capability_id,
            capability_revision=self.capability_revision,
            connection_revision=self.connection_revision,
            authorization_context_revision=self.authorization_context_revision,
        )

    def registry_key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.task_id,
            self.app_id,
            self.capability_id,
            self.capability_revision,
            self.connection_revision,
            self.authorization_context_revision,
        )


@dataclass(frozen=True, slots=True)
class _UriTemplateVariable:
    name: str
    prefix: int | None
    explode: bool


@dataclass(frozen=True, slots=True)
class _UriTemplateExpression:
    operator: str
    variables: tuple[_UriTemplateVariable, ...]


@dataclass(slots=True)
class _CanonicalBudget:
    limit: int
    reason: McpClientErrorReason
    size: int = 0

    def _add(self, size: int) -> None:
        self.size += size
        if self.size > self.limit:
            _fail(self.reason)

    def _ensure(self, minimum_size: int) -> None:
        if self.size + minimum_size > self.limit:
            _fail(self.reason)

    def _string(self, value: str) -> None:
        self._add(1)
        for character in value:
            codepoint = ord(character)
            if character in {'"', "\\"} or character in "\b\t\n\f\r":
                self._add(2)
            elif codepoint < 0x20:
                self._add(6)
            else:
                try:
                    self._add(len(character.encode("utf-8")))
                except UnicodeEncodeError:
                    _fail(McpClientErrorReason.RESULT_INVALID)
        self._add(1)

    def _mapping(self, value: Mapping[Any, Any]) -> None:
        entry_count = len(value)
        self._ensure(2 + max(0, entry_count - 1) + 4 * entry_count)
        keys = list(value)
        if len(keys) != entry_count or any(type(key) is not str for key in keys):
            _fail(McpClientErrorReason.RESULT_INVALID)
        self._add(1)
        for index, key in enumerate(sorted(keys)):
            if index:
                self._add(1)
            self._string(key)
            self._add(1)
            self.walk(value[key])
        self._add(1)

    def _sequence(self, value: list[Any] | tuple[Any, ...]) -> None:
        item_count = len(value)
        self._ensure(2 + max(0, item_count - 1) + item_count)
        self._add(1)
        for index, item in enumerate(value):
            if index:
                self._add(1)
            self.walk(item)
        self._add(1)

    def walk(self, value: Any) -> None:
        value_type = type(value)
        if value is None:
            self._add(4)
        elif value_type is bool:
            self._add(4 if value else 5)
        elif value_type is str:
            self._string(value)
        elif value_type in {int, float}:
            try:
                self._add(len(canonical_bytes(value)))
            except (TypeError, ValueError):
                _fail(McpClientErrorReason.RESULT_INVALID)
        elif value_type in {list, tuple}:
            self._sequence(value)
        elif isinstance(value, Mapping):
            self._mapping(value)
        else:
            _fail(McpClientErrorReason.RESULT_INVALID)


_ACTIVE_OPERATION: contextvars.ContextVar[_OperationContext | None] = contextvars.ContextVar(
    "omega_mcp_active_operation",
    default=None,
)


PageResult = (
    types.ListToolsResult
    | types.ListPromptsResult
    | types.ListResourcesResult
    | types.ListResourceTemplatesResult
)
PageCall = Callable[..., Awaitable[PageResult]]


def _fail(reason: McpClientErrorReason) -> None:
    raise McpClientError(reason)


def _unwrap_exception_group(error: BaseException) -> BaseException:
    """Follow a chain of single-member ``ExceptionGroup``s to their real leaf.

    Structured concurrency (``anyio``/``asyncio`` task groups, used
    throughout the SDK session/transport layers this module drives) wraps
    whatever a task raises in an ``ExceptionGroup`` on exit, even when there
    was only ever one real failure. ``discover_restricted`` uses this so a
    stable ``McpClientError``/``TimeoutError`` raised deep inside its own
    one-shot connect-enumerate-disconnect flow is still recognized as such,
    rather than reported as a generic transport failure.
    """
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _client_info() -> types.Implementation:
    return types.Implementation(name=_CLIENT_NAME, version=_CLIENT_VERSION)


def _json_value(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int, float}:
        if type(value) is float and not math.isfinite(value):
            _fail(McpClientErrorReason.RESULT_INVALID)
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            _fail(McpClientErrorReason.RESULT_INVALID)
        return {key: _json_value(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_json_value(item) for item in value]
    _fail(McpClientErrorReason.RESULT_INVALID)


def _bounded_document(value: Any, limit: int, reason: McpClientErrorReason) -> Any:
    try:
        _CanonicalBudget(limit=limit, reason=reason).walk(value)
    except McpClientError:
        raise
    except Exception:
        _fail(McpClientErrorReason.RESULT_INVALID)
    document = _json_value(value)
    try:
        payload = canonical_bytes(document)
    except (TypeError, ValueError):
        _fail(McpClientErrorReason.RESULT_INVALID)
    if len(payload) > limit:
        _fail(reason)
    return document


def _bounded_text(
    value: Any,
    limit: int,
    limit_reason: McpClientErrorReason = McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
) -> str:
    if type(value) is not str or unicodedata.normalize("NFC", value) != value:
        _fail(McpClientErrorReason.RESULT_INVALID)
    size = 0
    try:
        for character in value:
            size += len(character.encode("utf-8"))
            if size > limit:
                _fail(limit_reason)
    except UnicodeEncodeError:
        _fail(McpClientErrorReason.RESULT_INVALID)
    return value


def _optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, limit)


def _text_mime(value: Any) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or not (value.lower().startswith("text/") or value.lower() == "application/json")
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    return value


def _schema_validator(schema: Any) -> jsonschema.protocols.Validator:
    if not isinstance(schema, Mapping):
        _fail(McpClientErrorReason.RESULT_INVALID)
    schema_document = dict(schema)
    declared_schema = schema_document.get("$schema")
    if declared_schema is not None and (
        type(declared_schema) is not str or declared_schema not in _SUPPORTED_SCHEMA_IDS
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    effective_schema = (
        declared_schema
        if type(declared_schema) is str
        else jsonschema.Draft202012Validator.META_SCHEMA["$id"]
    )
    _validate_schema_authority(schema_document, effective_schema)
    validator_type = jsonschema.validators.validator_for(
        schema_document,
        default=jsonschema.Draft202012Validator,
    )
    if validator_type not in _SUPPORTED_SCHEMA_VALIDATORS:
        _fail(McpClientErrorReason.RESULT_INVALID)
    validator_type.check_schema(schema_document)
    return validator_type(schema_document)


def _validate_schema_authority(
    schema: Any,
    effective_schema: str,
    *,
    is_root: bool = True,
) -> None:
    if not isinstance(schema, Mapping):
        _fail(McpClientErrorReason.RESULT_INVALID)
    root_schema = schema
    specification = (
        DRAFT201909
        if effective_schema == jsonschema.Draft201909Validator.META_SCHEMA["$id"]
        else DRAFT202012
    )
    reference_resolver = Registry().resolver_with_root(
        Resource.from_contents(
            root_schema,
            default_specification=specification,
        )
    )
    if effective_schema == jsonschema.Draft201909Validator.META_SCHEMA["$id"]:
        reference_keywords = ("$ref", "$recursiveRef")
        unsupported_reference_keyword = "$dynamicRef"
    else:
        reference_keywords = ("$ref", "$dynamicRef")
        unsupported_reference_keyword = "$recursiveRef"
    pending: list[tuple[Any, bool]] = [(schema, is_root)]
    seen_schema_nodes: set[int] = set()
    while pending:
        current, current_is_root = pending.pop()
        if type(current) is bool:
            continue
        if not isinstance(current, Mapping):
            _fail(McpClientErrorReason.RESULT_INVALID)
        current_identity = id(current)
        if current_identity in seen_schema_nodes:
            continue
        seen_schema_nodes.add(current_identity)

        declared_schema = current.get("$schema")
        if declared_schema is not None and (
            type(declared_schema) is not str
            or declared_schema not in _SUPPORTED_SCHEMA_IDS
            or declared_schema != effective_schema
        ):
            _fail(McpClientErrorReason.RESULT_INVALID)
        if "$id" in current and not current_is_root:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if unsupported_reference_keyword in current:
            _fail(McpClientErrorReason.RESULT_INVALID)
        for reference_keyword in reference_keywords:
            reference = current.get(reference_keyword)
            if reference is None:
                continue
            if type(reference) is not str or not reference.startswith("#"):
                _fail(McpClientErrorReason.RESULT_INVALID)
            try:
                target = reference_resolver.lookup(reference).contents
            except Unresolvable:
                _fail(McpClientErrorReason.RESULT_INVALID)
            if type(target) is not bool and not isinstance(target, Mapping):
                _fail(McpClientErrorReason.RESULT_INVALID)
            pending.append((target, target is root_schema))

        for keyword in _SCHEMA_SINGLE_SUBSCHEMAS:
            subschema = current.get(keyword)
            if subschema is None:
                continue
            if keyword == "items" and type(subschema) is list:
                pending.extend((item, False) for item in subschema)
            else:
                pending.append((subschema, False))
        for keyword in _SCHEMA_ARRAY_SUBSCHEMAS:
            subschemas = current.get(keyword)
            if subschemas is None:
                continue
            if type(subschemas) is not list:
                _fail(McpClientErrorReason.RESULT_INVALID)
            pending.extend((subschema, False) for subschema in subschemas)
        for keyword in _SCHEMA_MAPPING_SUBSCHEMAS:
            subschemas = current.get(keyword)
            if subschemas is None:
                continue
            if not isinstance(subschemas, Mapping):
                _fail(McpClientErrorReason.RESULT_INVALID)
            pending.extend((subschema, False) for subschema in subschemas.values())
        dependencies = current.get("dependencies")
        if isinstance(dependencies, Mapping):
            pending.extend(
                (dependency, False)
                for dependency in dependencies.values()
                if isinstance(dependency, Mapping) or type(dependency) is bool
            )


def _resolve_schema_pointer(root_schema: Mapping[str, Any], reference: str) -> Any:
    if reference == "#":
        return root_schema
    if not reference.startswith("#/"):
        _fail(McpClientErrorReason.RESULT_INVALID)
    try:
        pointer = unquote(reference[1:], errors="strict")
    except UnicodeDecodeError:
        _fail(McpClientErrorReason.RESULT_INVALID)
    target: Any = root_schema
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(target, Mapping) and token in target:
            target = target[token]
        elif type(target) is list and token.isascii() and token.isdigit():
            index = int(token)
            if index >= len(target):
                _fail(McpClientErrorReason.RESULT_INVALID)
            target = target[index]
        else:
            _fail(McpClientErrorReason.RESULT_INVALID)
    return target


def _validate_elicitation_required_closure(
    schema: Mapping[str, Any],
    exposed_fields: frozenset[str],
    *,
    root_schema: Mapping[str, Any] | None = None,
    seen_references: frozenset[str] = frozenset(),
) -> None:
    if root_schema is None:
        root_schema = schema
    required = schema.get("required", [])
    if type(required) is not list or not set(required) <= exposed_fields:
        _fail(McpClientErrorReason.RESULT_INVALID)
    dynamic_reference = schema.get("$dynamicRef")
    if dynamic_reference is not None:
        _fail(McpClientErrorReason.RESULT_INVALID)
    recursive_reference = schema.get("$recursiveRef")
    if recursive_reference is not None:
        _fail(McpClientErrorReason.RESULT_INVALID)
    reference = schema.get("$ref")
    if reference is not None:
        if type(reference) is not str:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if reference not in seen_references:
            target = _resolve_schema_pointer(root_schema, reference)
            if type(target) is not bool and not isinstance(target, Mapping):
                _fail(McpClientErrorReason.RESULT_INVALID)
            if isinstance(target, Mapping):
                _validate_elicitation_required_closure(
                    target,
                    exposed_fields,
                    root_schema=root_schema,
                    seen_references=seen_references | {reference},
                )
    for keyword in _ELICITATION_SAME_INSTANCE_SINGLE:
        subschema = schema.get(keyword)
        if isinstance(subschema, Mapping):
            _validate_elicitation_required_closure(
                subschema,
                exposed_fields,
                root_schema=root_schema,
                seen_references=seen_references,
            )
    for keyword in _ELICITATION_SAME_INSTANCE_ARRAY:
        subschemas = schema.get(keyword)
        if type(subschemas) is list:
            for subschema in subschemas:
                if isinstance(subschema, Mapping):
                    _validate_elicitation_required_closure(
                        subschema,
                        exposed_fields,
                        root_schema=root_schema,
                        seen_references=seen_references,
                    )
    dependent_schemas = schema.get("dependentSchemas")
    if isinstance(dependent_schemas, Mapping):
        for subschema in dependent_schemas.values():
            if isinstance(subschema, Mapping):
                _validate_elicitation_required_closure(
                    subschema,
                    exposed_fields,
                    root_schema=root_schema,
                    seen_references=seen_references,
                )
    dependent_required = schema.get("dependentRequired")
    if dependent_required is not None:
        if not isinstance(dependent_required, Mapping):
            _fail(McpClientErrorReason.RESULT_INVALID)
        for dependencies in dependent_required.values():
            if type(dependencies) is not list or not set(dependencies) <= exposed_fields:
                _fail(McpClientErrorReason.RESULT_INVALID)
    dependencies = schema.get("dependencies")
    if isinstance(dependencies, Mapping):
        for dependency in dependencies.values():
            if isinstance(dependency, Mapping):
                _validate_elicitation_required_closure(
                    dependency,
                    exposed_fields,
                    root_schema=root_schema,
                    seen_references=seen_references,
                )


def _valid_percent_encoding(value: str) -> bool:
    position = 0
    while position < len(value):
        if value[position] != "%":
            position += 1
            continue
        if position + 2 >= len(value) or not all(
            character in "0123456789abcdefABCDEF"
            for character in value[position + 1 : position + 3]
        ):
            return False
        position += 3
    return True


def _parse_uri_template(
    template: str,
    *,
    max_variables: int,
) -> tuple[tuple[str, _UriTemplateExpression | None], ...]:
    if (
        type(template) is not str
        or not template
        or "\\" in template
        or any(character.isspace() or ord(character) < 0x20 for character in template)
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    pieces: list[tuple[str, _UriTemplateExpression | None]] = []
    names: set[str] = set()
    position = 0
    previous_unprefixed_expression = False
    while position < len(template):
        opening = template.find("{", position)
        closing_before = template.find("}", position)
        if closing_before >= 0 and (opening < 0 or closing_before < opening):
            _fail(McpClientErrorReason.RESULT_INVALID)
        if opening < 0:
            literal = template[position:]
            if "}" in literal or not _valid_percent_encoding(literal):
                _fail(McpClientErrorReason.RESULT_INVALID)
            pieces.append((literal, None))
            break
        literal = template[position:opening]
        if not _valid_percent_encoding(literal):
            _fail(McpClientErrorReason.RESULT_INVALID)
        if literal:
            pieces.append((literal, None))
        closing = template.find("}", opening + 1)
        if closing < 0 or "{" in template[opening + 1 : closing]:
            _fail(McpClientErrorReason.RESULT_INVALID)
        body = template[opening + 1 : closing]
        if not body:
            _fail(McpClientErrorReason.RESULT_INVALID)
        operator = body[0] if body[0] in _URI_TEMPLATE_OPERATOR else ""
        if previous_unprefixed_expression and not literal and operator in {"", "+"}:
            _fail(McpClientErrorReason.RESULT_INVALID)
        variable_text = body[1:] if operator else body
        variables: list[_UriTemplateVariable] = []
        for raw_variable in variable_text.split(","):
            if not raw_variable:
                _fail(McpClientErrorReason.RESULT_INVALID)
            explode = raw_variable.endswith("*")
            value = raw_variable[:-1] if explode else raw_variable
            prefix: int | None = None
            if ":" in value:
                name, separator, prefix_text = value.partition(":")
                if (
                    not separator
                    or explode
                    or not prefix_text.isascii()
                    or not prefix_text.isdigit()
                    or not 1 <= len(prefix_text) <= 4
                    or int(prefix_text) <= 0
                ):
                    _fail(McpClientErrorReason.RESULT_INVALID)
                value = name
                prefix = int(prefix_text)
            if _URI_TEMPLATE_VARNAME.fullmatch(value) is None or value in names:
                _fail(McpClientErrorReason.RESULT_INVALID)
            names.add(value)
            if len(names) > max_variables:
                _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
            variables.append(_UriTemplateVariable(name=value, prefix=prefix, explode=explode))
        pieces.append(("", _UriTemplateExpression(operator, tuple(variables))))
        previous_unprefixed_expression = operator in {"", "+"}
        position = closing + 1
    if not pieces:
        _fail(McpClientErrorReason.RESULT_INVALID)
    return tuple(pieces)


def _template_value_is_valid(value: str, *, reserved: bool) -> bool:
    pattern = _URI_RESERVED_VALUE if reserved else _URI_SIMPLE_VALUE
    return pattern.fullmatch(value) is not None and _valid_percent_encoding(value)


def _prefix_value_matches(
    variable: _UriTemplateVariable,
    value: str,
    *,
    reserved: bool,
) -> bool:
    if variable.prefix is None:
        return _template_value_is_valid(value, reserved=reserved)
    pattern = _URI_RESERVED_VALUE if reserved else _URI_ATOMIC_VALUE
    if pattern.fullmatch(value) is None or not _valid_percent_encoding(value):
        return False
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeDecodeError:
        return False
    return len(decoded) <= variable.prefix


def _simple_expression_matches(
    variables: tuple[_UriTemplateVariable, ...],
    value: str,
    *,
    reserved: bool,
    max_components: int,
) -> bool:
    if not _template_value_is_valid(value, reserved=reserved):
        if reserved or not any(variable.explode for variable in variables):
            return False
        components = value.split(",")
        if len(components) > max_components:
            return False
        if not all(
            (separator := component.partition("="))[1]
            and _URI_ATOMIC_VALUE.fullmatch(separator[0]) is not None
            and _URI_ATOMIC_VALUE.fullmatch(separator[2]) is not None
            for component in components
        ):
            return False
    if any(variable.prefix is None for variable in variables):
        return True

    components = value.split(",")
    if len(components) > max_components:
        return False
    reachable = {0}
    for variable in variables:
        next_reachable = set(reachable)
        for start in reachable:
            last_end = len(components) if reserved else min(start + 1, len(components))
            for end in range(start + 1, last_end + 1):
                candidate = ",".join(components[start:end])
                if _prefix_value_matches(variable, candidate, reserved=reserved):
                    next_reachable.add(end)
        reachable = next_reachable
    return len(components) in reachable


def _exploded_component_is_valid(value: str) -> bool:
    if _URI_ATOMIC_VALUE.fullmatch(value) is not None and _valid_percent_encoding(value):
        return True
    key, separator, item = value.partition("=")
    return bool(
        separator
        and key
        and _URI_ATOMIC_VALUE.fullmatch(key) is not None
        and _URI_ATOMIC_VALUE.fullmatch(item) is not None
        and _valid_percent_encoding(key)
        and _valid_percent_encoding(item)
    )


def _positional_expression_matches(
    variables: tuple[_UriTemplateVariable, ...],
    components: list[str],
) -> bool:
    reachable = {0}
    for variable in variables:
        next_reachable = set(reachable)
        for start in reachable:
            if variable.explode:
                for end in range(start + 1, len(components) + 1):
                    if not _exploded_component_is_valid(components[end - 1]):
                        break
                    next_reachable.add(end)
            elif start < len(components) and _prefix_value_matches(
                variable,
                components[start],
                reserved=False,
            ):
                next_reachable.add(start + 1)
        reachable = next_reachable
    return len(components) in reachable


def _label_expression_matches(
    variables: tuple[_UriTemplateVariable, ...],
    value: str,
    *,
    max_components: int,
) -> bool:
    if not _template_value_is_valid(value, reserved=False):
        if not any(variable.explode for variable in variables):
            return False
        exploded_components = value.split(".")
        if len(exploded_components) > max_components or not all(
            _exploded_component_is_valid(component) for component in exploded_components
        ):
            return False
    if any(variable.prefix is None for variable in variables):
        return True

    components = value.split(".")
    if len(components) > max_components:
        return False
    reachable = {0}
    for variable in variables:
        next_reachable = set(reachable)
        for start in reachable:
            for end in range(start + 1, len(components) + 1):
                candidate = ".".join(components[start:end])
                if _prefix_value_matches(variable, candidate, reserved=False):
                    next_reachable.add(end)
        reachable = next_reachable
    return len(components) in reachable


def _named_expression_matches(
    expression: _UriTemplateExpression,
    expansion: str,
    *,
    max_components: int,
) -> bool:
    operator = expression.operator
    separator_character = ";" if operator == ";" else "&"
    if not expansion.startswith(operator):
        return False
    raw_components = expansion[1:].split(separator_character)
    if len(raw_components) > max_components or any(not item for item in raw_components):
        return False
    components: list[tuple[str, bool, str]] = []
    for component in raw_components:
        key, separator, value = component.partition("=")
        has_value = bool(separator)
        if (
            not key
            or _URI_ATOMIC_VALUE.fullmatch(key) is None
            or not _valid_percent_encoding(key)
            or operator in {"?", "&"}
            and not has_value
            or _URI_SIMPLE_VALUE.fullmatch(value) is None
            or not _valid_percent_encoding(value)
        ):
            return False
        components.append((key, has_value, value))

    reachable = {0}
    for variable in expression.variables:
        next_reachable = set(reachable)
        for start in reachable:
            if start >= len(components):
                continue
            if not variable.explode:
                key, _has_value, value = components[start]
                if key == variable.name and _prefix_value_matches(
                    variable,
                    value,
                    reserved=False,
                ):
                    next_reachable.add(start + 1)
                continue
            list_valid = True
            map_valid = True
            map_keys: set[str] = set()
            for end in range(start + 1, len(components) + 1):
                key, _has_value, value = components[end - 1]
                atomic_value = _URI_ATOMIC_VALUE.fullmatch(
                    value
                ) is not None and _valid_percent_encoding(value)
                list_valid = list_valid and key == variable.name and atomic_value
                map_valid = map_valid and key not in map_keys and atomic_value
                map_keys.add(key)
                if list_valid or map_valid:
                    next_reachable.add(end)
                else:
                    break
        reachable = next_reachable
    return len(components) in reachable


def _expression_matches(
    expression: _UriTemplateExpression,
    expansion: str,
    *,
    max_components: int,
) -> bool:
    operator = expression.operator
    variables = expression.variables
    if not expansion:
        return True
    if operator in {"", "+", "#"}:
        value = expansion
        if operator == "#":
            if not value.startswith("#"):
                return False
            value = value[1:]
        return _simple_expression_matches(
            variables,
            value,
            reserved=operator in {"+", "#"},
            max_components=max_components,
        )
    if operator == ".":
        if not expansion.startswith("."):
            return False
        return _label_expression_matches(
            variables,
            expansion[1:],
            max_components=max_components,
        )
    if operator == "/":
        if not expansion.startswith(operator):
            return False
        components = expansion[1:].split(operator)
        return len(components) <= max_components and _positional_expression_matches(
            variables,
            components,
        )
    if operator in {";", "?", "&"}:
        return _named_expression_matches(
            expression,
            expansion,
            max_components=max_components,
        )
    return False


def _legacy_matches_uri_template(
    template: str,
    uri: str,
    limits: McpClientLimits,
) -> bool:
    if (
        type(uri) is not str
        or not uri
        or "\\" in uri
        or not _valid_percent_encoding(uri)
        or any(character.isspace() or ord(character) < 0x20 for character in uri)
    ):
        return False
    pieces = _parse_uri_template(template, max_variables=limits.max_list_items)
    pattern: list[str] = [r"\A"]
    expressions: list[_UriTemplateExpression] = []
    for literal, expression in pieces:
        if expression is None:
            pattern.append(re.escape(literal))
        else:
            capture = {
                "/": r"(?:/[^?#]*)?",
                ".": r"(?:\.[^/?#]*)?",
                ";": r"(?:;[^/?#]*)?",
                "?": r"(?:\?[^#]*)?",
                "&": r"(?:&[^#]*)?",
                "#": r"(?:\#.*)?",
            }.get(expression.operator, r".*?")
            pattern.append(f"(?P<expression_{len(expressions)}>{capture})")
            expressions.append(expression)
    pattern.append(r"\Z")
    matched = re.fullmatch("".join(pattern), uri)
    return matched is not None and all(
        _expression_matches(
            expression,
            matched.group(f"expression_{index}"),
            max_components=limits.max_list_items,
        )
        for index, expression in enumerate(expressions)
    )


from mcp_platform.uri_templates import matches_uri_template as _matches_uri_template


def _result(
    request: McpOperationRequest,
    *,
    status: McpOperationStatus,
    data: Mapping[str, object] | None = None,
    task_handle: McpTaskHandle | None = None,
    error: McpClientError | None = None,
) -> McpOperationResult:
    return McpOperationResult(
        request_id=request.request_id,
        app_id=request.app_id,
        capability_id=request.capability_id,
        capability_revision=request.capability_revision,
        status=status,
        data={} if data is None else data,
        input_request=None,
        task_handle=task_handle,
        error=error,
    )


def _failed(request: McpOperationRequest, reason: McpClientErrorReason) -> McpOperationResult:
    return _result(
        request,
        status=McpOperationStatus.FAILED,
        error=McpClientError(reason),
    )


def _operation_metadata(
    descriptor: McpConnectionDescriptor,
    receipt: McpDiscoveryReceipt,
    metadata: object,
) -> None:
    _normalize_metadata(
        descriptor,
        metadata,
        expected_server=(receipt.server_id, receipt.server_version),  # type: ignore[arg-type]
    )
    if isinstance(metadata, Mapping):
        revision = metadata.get("capabilityRevision")
        if revision is not None and revision != receipt.capability_revision:
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)


def _schema_digest(schema: Any) -> str:
    try:
        return sha256_hex(canonical_bytes(_json_value(schema)))
    except (TypeError, ValueError):
        _fail(McpClientErrorReason.RESULT_INVALID)


def _prompt_schema(arguments: list[types.PromptArgument] | None) -> dict[str, object]:
    parsed: list[dict[str, object]] = []
    names: set[str] = set()
    if arguments is not None and type(arguments) is not list:
        _fail(McpClientErrorReason.RESULT_INVALID)
    for argument in arguments or []:
        if (
            type(argument) is not types.PromptArgument
            or type(argument.name) is not str
            or not argument.name
            or argument.name in names
            or argument.required is not None
            and type(argument.required) is not bool
        ):
            _fail(McpClientErrorReason.RESULT_INVALID)
        names.add(argument.name)
        parsed.append({"name": argument.name, "required": argument.required is True})
    return {"arguments": sorted(parsed, key=lambda item: str(item["name"]))}


def _expected_identity(descriptor: McpConnectionDescriptor) -> dict[str, str | None]:
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


def _sdk_mode_for_descriptor(descriptor: McpConnectionDescriptor) -> str:
    compatibility = descriptor.runtime_compatibility
    if compatibility is None:
        return MCP_PROTOCOL_VERSION
    return str(compatibility["handshake_mode"])


def _identity_equal(actual: object, expected: str | None) -> bool:
    if expected is None:
        return actual is None
    return type(actual) is str and actual == expected


def _normalize_metadata(
    descriptor: McpConnectionDescriptor,
    metadata: object,
    *,
    expected_server: tuple[str, str] | None,
) -> tuple[dict[str, object], tuple[str, str]]:
    compatibility = descriptor.runtime_compatibility
    # A package declaring the legacy handshake says where identity comes from:
    # `serverInfo` in the initialize result, already checked against
    # `missing_metadata`. A result's `_meta` is a 2026-07-28 mechanism and is
    # not consulted for identity on such a connection -- Stripe negotiates
    # 2025-03-26 and puts a Ruby object's inspect string in every `_meta`
    # serverInfo, which is not identity and must not be able to veto one that
    # was already established.
    #
    # REMOTE only. For a LOCAL deployment Omega launches the server, so holding
    # its `_meta` to the strict reading is Omega's business -- the same boundary
    # `_advertised_surfaces` draws. An absent `_meta` resolves this way for
    # either, exactly as it always has.
    if compatibility is not None and (
        metadata is None or descriptor.deployment is McpClientDeployment.REMOTE
    ):
        defaults = compatibility["missing_metadata"]
        server_identity = (
            str(defaults["server_id"]),
            str(defaults["server_version"]),
        )
        if expected_server is not None and expected_server != server_identity:
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        normalized = {
            field: value for field, value in _expected_identity(descriptor).items()
        }
        normalized["serverId"], normalized["serverVersion"] = server_identity
        return normalized, server_identity
    if not isinstance(metadata, Mapping) or any(type(key) is not str for key in metadata):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    expected = _expected_identity(descriptor)
    remote = descriptor.deployment is McpClientDeployment.REMOTE
    for identity_field in _IDENTITY_FIELDS:
        if identity_field not in metadata:
            if remote or expected[identity_field] is None:
                continue
            _fail(
                McpClientErrorReason.PROTOCOL_MISMATCH
                if identity_field == "protocolVersion"
                else McpClientErrorReason.IDENTITY_MISMATCH
            )
        if not _identity_equal(metadata[identity_field], expected[identity_field]):
            _fail(
                McpClientErrorReason.PROTOCOL_MISMATCH
                if identity_field == "protocolVersion"
                else McpClientErrorReason.IDENTITY_MISMATCH
            )

    server_info = metadata.get(_SERVER_INFO_META_KEY)
    if not isinstance(server_info, Mapping) or any(type(key) is not str for key in server_info):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    server_id = server_info.get("name")
    server_version = server_info.get("version")
    if (
        type(server_id) is not str
        or not server_id
        or type(server_version) is not str
        or not server_version
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    server_identity = (server_id, server_version)
    if expected_server is not None and server_identity != expected_server:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    for metadata_field, value in (("serverId", server_id), ("serverVersion", server_version)):
        if metadata_field in metadata and metadata[metadata_field] != value:
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)

    normalized: dict[str, object] = {
        identity_field: (
            expected[identity_field] if identity_field not in metadata else metadata[identity_field]
        )
        for identity_field in _IDENTITY_FIELDS
    }
    normalized["serverId"] = server_id
    normalized["serverVersion"] = server_version
    return normalized, server_identity


def _source(
    descriptor: McpConnectionDescriptor,
    discovery_source: DiscoverySource,
) -> DiscoverySource:
    if descriptor.deployment is McpClientDeployment.REMOTE:
        if discovery_source is not DiscoverySource.LIVE_AUTHENTICATED:
            _fail(McpClientErrorReason.AUTHORIZATION_REQUIRED)
        return discovery_source
    if discovery_source is not DiscoverySource.LIVE_OBSERVED:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return discovery_source


def _sdk_client_builder_record(
    descriptor: McpConnectionDescriptor,
    sdk_client_factory: object,
) -> _IssuedFactoryProof:
    """Validate one SDK client factory against the descriptor it must serve.

    The shared core both :func:`_sdk_client_builder` (the ordinary,
    credentialed operational path -- always requires
    ``DiscoverySource.LIVE_AUTHENTICATED`` for a REMOTE deployment, via
    :func:`_source`) and :func:`_discovery_sdk_client_builder` (a
    discovery-only connection, which is unauthenticated by construction and
    so never reaches -- nor needs -- that requirement) build on. Every
    factory/descriptor identity and fingerprint check below applies equally
    to both; only what each caller does with the resolved
    ``record.discovery_source`` differs.
    """
    if type(sdk_client_factory) is not SdkClientFactory:
        if descriptor.deployment is McpClientDeployment.REMOTE:
            _fail(McpClientErrorReason.AUTHORIZATION_REQUIRED)
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    record = _ISSUED_SDK_CLIENT_FACTORIES.get(sdk_client_factory)
    if record is None or record.factory_ref() is not sdk_client_factory:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    if (
        sdk_client_factory._builder is not record.builder
        or sdk_client_factory._builder_identity is not record.builder_identity
        or sdk_client_factory._authorization_context_revision
        != record.authorization_context_revision
        or sdk_client_factory.deployment is not record.deployment
        or sdk_client_factory.transport is not record.transport
        or sdk_client_factory.discovery_source is not record.discovery_source
        or sdk_client_factory._proof is not record.proof
        or _factory_fingerprint(
            factory=sdk_client_factory,
            builder=sdk_client_factory._builder,
            builder_identity=sdk_client_factory._builder_identity,
            deployment=sdk_client_factory.deployment,
            transport=sdk_client_factory.transport,
            discovery_source=sdk_client_factory.discovery_source,
            descriptor_binding=record.descriptor_binding,
            authorization_context_revision=sdk_client_factory._authorization_context_revision,
            proof=sdk_client_factory._proof,
        )
        != record.fingerprint
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    if (
        type(sdk_client_factory._builder) is _InProcessSdkClientBuilder
        and sdk_client_factory._builder.server is not record.builder_identity
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    if (
        sdk_client_factory.deployment is not descriptor.deployment
        or sdk_client_factory.transport is not descriptor.transport
        or descriptor.deployment is McpClientDeployment.REMOTE
        and record.descriptor_binding != _descriptor_binding(descriptor)
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return record


def _sdk_client_builder(
    descriptor: McpConnectionDescriptor,
    sdk_client_factory: object,
) -> tuple[_SdkClientBuilder, DiscoverySource, str]:
    record = _sdk_client_builder_record(descriptor, sdk_client_factory)
    source = _source(descriptor, record.discovery_source)
    return record.builder, source, record.authorization_context_revision


def _discovery_sdk_client_builder(
    descriptor: McpConnectionDescriptor,
    sdk_client_factory: object,
) -> tuple[_SdkClientBuilder, str]:
    """Resolve one discovery-only client's SDK builder.

    Reuses :func:`_sdk_client_builder_record`'s own factory/descriptor
    identity and fingerprint validation unchanged, but never requires
    ``DiscoverySource.LIVE_AUTHENTICATED`` the way the ordinary, credentialed
    operational path (:func:`_sdk_client_builder`, via :func:`_source`)
    does -- a discovery-only connection has no credential to present (no
    review has happened yet to grant it one), so the only legitimate source
    here is ``DiscoverySource.LIVE_OBSERVED``, exactly what
    ``StreamableHttpClientConfig.unauthenticated()`` genuinely produces for
    a real REMOTE connection. ``start()``'s own ``_sdk_client_builder`` call
    -- and therefore ``_source``'s REMOTE-must-be-authenticated
    requirement -- is entirely untouched by this addition.
    """
    record = _sdk_client_builder_record(descriptor, sdk_client_factory)
    if record.discovery_source is not DiscoverySource.LIVE_OBSERVED:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return record.builder, record.authorization_context_revision


_LOGGER = logging.getLogger("omega.mcp.client")


def _public_client_error(
    descriptor: McpConnectionDescriptor,
    error: BaseException,
) -> BaseException:
    del descriptor
    if isinstance(error, Exception) and type(error) is not McpClientError:
        # The public reason is deliberately content-free, which also means the
        # real cause vanishes: every unexpected failure reaches the operator as
        # `transport_unavailable` with nothing to chase. The TYPE is Omega's own
        # or the standard library's -- never provider-authored text -- and it is
        # the difference between a timeout, a protocol error and a bug here.
        _LOGGER.warning(
            "mcp connect failed and was reported as transport_unavailable: %s",
            type(error).__name__,
        )
        return McpClientError(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
    return error


def _advertised_surfaces(
    capabilities: types.ServerCapabilities,
    *,
    allow_unrequested_standard: bool = False,
) -> tuple[bool, bool, bool]:
    if type(capabilities) is not types.ServerCapabilities:
        _fail(McpClientErrorReason.RESULT_INVALID)
    # A surface the client never requested is one the server will never send
    # to it. For a LOCAL deployment Omega launches the server and an unasked-for
    # surface is a packaging error worth refusing; for a REMOTE one it is just
    # what the vendor's framework emits -- Wix advertises `logging`, Miro adds
    # `experimental: {}` -- and refusing it made both unconnectable after a
    # successful sign-in, handshake and token exchange (live 2026-09-07).
    if not allow_unrequested_standard and any(
        claim is not None
        for claim in (
            capabilities.experimental,
            capabilities.logging,
            capabilities.completions,
            capabilities.tasks,
        )
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    return (
        capabilities.tools is not None,
        capabilities.prompts is not None,
        capabilities.resources is not None,
    )


async def _collect_pages(
    call: PageCall,
    *,
    descriptor: McpConnectionDescriptor,
    result_type: type[PageResult],
    expected_server: tuple[str, str] | None,
) -> tuple[tuple[PageResult, ...], tuple[str, str], dict[str, object]]:
    pages: list[PageResult] = []
    cursors: set[str] = set()
    params: types.PaginatedRequestParams | None = None
    first_metadata: dict[str, object] | None = None
    server_identity = expected_server
    while True:
        result = await call(params=params)
        if type(result) is not result_type:
            _fail(McpClientErrorReason.RESULT_INVALID)
        normalized, observed_server = _normalize_metadata(
            descriptor,
            result.meta,
            expected_server=server_identity,
        )
        if server_identity is None:
            server_identity = observed_server
        if first_metadata is None:
            first_metadata = normalized
        pages.append(result.model_copy(update={"meta": normalized}))

        cursor = result.next_cursor
        if cursor is None:
            break
        if (
            type(cursor) is not str
            or not cursor
            or cursor in cursors
            or len(cursors) >= descriptor.limits.max_cursor_history
            or len(pages) >= descriptor.limits.max_list_pages
        ):
            _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
        cursors.add(cursor)
        params = types.PaginatedRequestParams(cursor=cursor)

    if (
        len(pages) > descriptor.limits.max_list_pages
        or server_identity is None
        or first_metadata is None
    ):
        _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
    return tuple(pages), server_identity, first_metadata


async def _discover_receipt(
    descriptor: McpConnectionDescriptor,
    connected: Any,
    discovery_source: DiscoverySource,
) -> McpDiscoveryReceipt:
    source = _source(descriptor, discovery_source)
    compatibility = descriptor.runtime_compatibility
    expected_wire_version = (
        MCP_PROTOCOL_VERSION
        if compatibility is None
        else compatibility["wire_protocol_version"]
    )
    if (
        type(connected.protocol_version) is not str
        or connected.protocol_version != expected_wire_version
    ):
        _fail(McpClientErrorReason.PROTOCOL_MISMATCH)
    session = connected.session
    discovery_metadata: dict[str, object] | None = None
    server_identity: tuple[str, str] | None = None
    if compatibility is not None:
        defaults = compatibility["missing_metadata"]
        connected_server_info = getattr(connected, "server_info", None)
        server_identity = (
            getattr(connected_server_info, "name", defaults["server_id"]),
            getattr(connected_server_info, "version", defaults["server_version"]),
        )
        if server_identity != (defaults["server_id"], defaults["server_version"]):
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    extensions: Mapping[str, object] = {}
    advertised_surfaces = (True, True, True)
    connected_capabilities = getattr(connected, "server_capabilities", None)
    if type(connected_capabilities) is types.ServerCapabilities:
        # Same allowance the `discover` branch below already makes. A provider
        # speaking the legacy handshake answers `discover` with METHOD_NOT_FOUND
        # and never reaches that line, so holding it to a stricter reading here
        # applied the narrow rule to exactly the servers Omega does not control.
        advertised_surfaces = _advertised_surfaces(
            connected_capabilities,
            allow_unrequested_standard=(
                descriptor.deployment is McpClientDeployment.REMOTE
            ),
        )
    # `server/discover` belongs to 2026-07-28. A connection that negotiated a
    # handshake-era version already took its identity from `serverInfo` in the
    # initialize result, checked above against the package's declared
    # `missing_metadata`, so an answer here cannot outrank it. Stripe negotiates
    # 2025-03-26, answers anyway, and returns a Ruby object's inspect string
    # where the server-info block belongs -- which Omega rightly refuses as
    # identity, and wrongly let veto the whole connection.
    speaks_current_wire = connected.protocol_version == MCP_PROTOCOL_VERSION
    discovered = None
    if speaks_current_wire:
        try:
            discovered = await session.send_request(
                types.DiscoverRequest(),
                types.DiscoverResult,
            )
        except MCPError as error:
            if error.code != types.METHOD_NOT_FOUND:
                raise
            discovered = None
    if discovered is not None:
        if type(discovered) is not types.DiscoverResult:
            _fail(McpClientErrorReason.RESULT_INVALID)
        supported_versions = discovered.supported_versions
        negotiated_protocol = connected.protocol_version
        if (
            type(supported_versions) is not list
            or not supported_versions
            or len(supported_versions) > 16
            or len(supported_versions) != len(set(supported_versions))
            or any(type(item) is not str or not item for item in supported_versions)
            or negotiated_protocol not in supported_versions
        ):
            _fail(McpClientErrorReason.PROTOCOL_MISMATCH)
        discovery_metadata, server_identity = _normalize_metadata(
            descriptor,
            discovered.meta,
            expected_server=None,
        )
        advertised_surfaces = _advertised_surfaces(
            discovered.capabilities,
            allow_unrequested_standard=(descriptor.deployment is McpClientDeployment.REMOTE),
        )
        discovered_extensions = discovered.capabilities.extensions
        if discovered_extensions is not None:
            extensions = discovered_extensions

    tool_pages: tuple[PageResult, ...] = ()
    prompt_pages: tuple[PageResult, ...] = ()
    resource_pages: tuple[PageResult, ...] = ()
    resource_template_pages: tuple[PageResult, ...] = ()
    first_metadata: dict[str, object] | None = None
    tools_advertised, prompts_advertised, resources_advertised = advertised_surfaces
    if descriptor.deployment is McpClientDeployment.REMOTE:
        declared = tuple(descriptor.declared_capabilities)
        tools_advertised = tools_advertised and any(
            item.startswith("tool:") for item in declared
        )
        prompts_advertised = prompts_advertised and any(
            item.startswith("prompt:") for item in declared
        )
        resources_advertised = resources_advertised and any(
            item.startswith("resource:") for item in declared
        )
    if tools_advertised:
        tool_pages, server_identity, first_metadata = await _collect_pages(
            session.list_tools,
            descriptor=descriptor,
            result_type=types.ListToolsResult,
            expected_server=server_identity,
        )
    if prompts_advertised:
        prompt_pages, server_identity, prompt_metadata = await _collect_pages(
            session.list_prompts,
            descriptor=descriptor,
            result_type=types.ListPromptsResult,
            expected_server=server_identity,
        )
        if first_metadata is None:
            first_metadata = prompt_metadata
    if resources_advertised:
        resource_pages, server_identity, resource_metadata = await _collect_pages(
            session.list_resources,
            descriptor=descriptor,
            result_type=types.ListResourcesResult,
            expected_server=server_identity,
        )
        if first_metadata is None:
            first_metadata = resource_metadata
        resource_template_pages, _, template_metadata = await _collect_pages(
            session.list_resource_templates,
            descriptor=descriptor,
            result_type=types.ListResourceTemplatesResult,
            expected_server=server_identity,
        )
        if first_metadata is None:
            first_metadata = template_metadata
    observation_metadata = discovery_metadata if discovery_metadata is not None else first_metadata
    if observation_metadata is None and compatibility is not None:
        # A legacy-handshake connection that advertised no group observed no
        # `_meta` anywhere, and does not need to: its identity came from
        # `serverInfo` in the initialize result and is already checked against
        # the package's `missing_metadata`. Deriving it here is the same thing
        # every absent `_meta` on such a connection has always resolved to.
        observation_metadata, server_identity = _normalize_metadata(
            descriptor,
            None,
            expected_server=server_identity,
        )
    if observation_metadata is None:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    # Unadvertised/unrequested groups are known absent, not failed pagination.
    # Wait for validated discovery/list metadata before representing them. An
    # advertised list still has to complete, and reconciliation still rejects
    # any capability required by the descriptor but absent from the server.
    absent_options = {"ttlMs": 0, "cacheScope": "private", "_meta": observation_metadata}
    if not tools_advertised:
        tool_pages = (types.ListToolsResult(tools=[], **absent_options),)
    if not prompts_advertised:
        prompt_pages = (types.ListPromptsResult(prompts=[], **absent_options),)
    if not resources_advertised:
        resource_pages = (types.ListResourcesResult(resources=[], **absent_options),)
        resource_template_pages = (
            types.ListResourceTemplatesResult(resourceTemplates=[], **absent_options),
        )
    observed = ObservedCapabilities(
        limits=descriptor.limits,
        source=source,
        metadata=observation_metadata,
        tool_pages=tool_pages,
        prompt_pages=prompt_pages,
        resource_pages=resource_pages,
        resource_template_pages=resource_template_pages,
        extensions=extensions,
    )
    receipt = reconcile_live_capabilities(descriptor, observed).receipt
    authority = descriptor.signed_capability_authority
    if authority is not None:
        _validate_signed_discovery(receipt, authority)
        receipt = replace(receipt, signed_capability_authority=authority)
    return receipt


def _validate_signed_discovery(
    receipt: McpDiscoveryReceipt,
    authority: Mapping[str, Any],
) -> None:
    signed_tools = authority["tools"]
    signed_resources = authority["resources"]
    signed_templates = authority["resource_templates"]
    if (
        not isinstance(signed_tools, Mapping)
        or not isinstance(signed_resources, Mapping)
        or not isinstance(signed_templates, Mapping)
        or set(receipt.tools) != set(signed_tools)
        or set(receipt.resources) != set(signed_resources)
        or set(receipt.resource_templates) != set(signed_templates)
    ):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    for name, observed in receipt.tools.items():
        signed = signed_tools[name]
        observed_output = (
            None
            if not isinstance(observed, Mapping)
            else observed.get("output_schema_sha256")
        )
        if (
            not isinstance(observed, Mapping)
            or not isinstance(signed, Mapping)
            or observed.get("input_schema_sha256") != signed.get("input_schema_sha256")
            or observed_output is not None
            and observed_output != signed.get("output_schema_sha256")
        ):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    for observed_group, signed_group, endpoint_field in (
        (receipt.resources, signed_resources, "uri"),
        (receipt.resource_templates, signed_templates, "uri_template"),
    ):
        for name, observed in observed_group.items():
            signed = signed_group[name]
            if (
                not isinstance(observed, Mapping)
                or not isinstance(signed, Mapping)
                or observed.get(endpoint_field) != signed.get(endpoint_field)
                or observed.get("mime_type") != signed.get("mime_type")
            ):
                _fail(McpClientErrorReason.CAPABILITY_DRIFT)


async def _operation_pages(
    call: Callable[[str | None], Awaitable[Any]],
    *,
    descriptor: McpConnectionDescriptor,
    receipt: McpDiscoveryReceipt,
    result_type: type[Any],
    item_field: str,
    normalize: Callable[[Any], dict[str, object]],
) -> list[dict[str, object]]:
    pages = 0
    item_count = 0
    cursors: set[str] = set()
    cursor: str | None = None
    items: list[dict[str, object]] = []
    while True:
        pages += 1
        if pages > descriptor.limits.max_list_pages:
            _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
        page = await call(cursor)
        if type(page) is not result_type or page.result_type != "complete":
            _fail(McpClientErrorReason.RESULT_INVALID)
        _operation_metadata(descriptor, receipt, page.meta)
        raw_items = getattr(page, item_field, None)
        if type(raw_items) is not list:
            _fail(McpClientErrorReason.RESULT_INVALID)
        item_count += len(raw_items)
        if item_count > descriptor.limits.max_list_items:
            _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        items.extend(normalize(item) for item in raw_items)
        next_cursor = page.next_cursor
        if next_cursor is None:
            break
        if (
            type(next_cursor) is not str
            or not next_cursor
            or next_cursor in cursors
            or len(cursors) >= descriptor.limits.max_cursor_history
            or pages >= descriptor.limits.max_list_pages
        ):
            _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
        cursors.add(next_cursor)
        cursor = next_cursor
    return items


def _normalize_tool(item: Any, limits: McpClientLimits) -> dict[str, object]:
    if (
        type(item) is not types.Tool
        or type(item.name) is not str
        or not item.name
        or type(item.input_schema) is not dict
        or item.output_schema is not None
        and type(item.output_schema) is not dict
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    input_schema = _bounded_document(
        item.input_schema,
        limits.max_result_document_bytes,
        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
    )
    _schema_validator(input_schema)
    output_schema = (
        None
        if item.output_schema is None
        else _bounded_document(
            item.output_schema,
            limits.max_result_document_bytes,
            McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
        )
    )
    if output_schema is not None:
        _schema_validator(output_schema)
    normalized = {
        "name": item.name,
        "description": _optional_text(item.description, limits.max_result_document_bytes),
        "input_schema": input_schema,
        "output_schema": output_schema,
    }
    apps_ui = _normalize_apps_ui(item, limits)
    if apps_ui is not None:
        normalized["ui"] = apps_ui
    return normalized


def _normalize_prompt(item: Any, limits: McpClientLimits) -> dict[str, object]:
    if type(item) is not types.Prompt or type(item.name) is not str or not item.name:
        _fail(McpClientErrorReason.RESULT_INVALID)
    schema = _prompt_schema(item.arguments)
    return {
        "name": item.name,
        "description": _optional_text(item.description, limits.max_result_document_bytes),
        "arguments": schema["arguments"],
    }


def _normalize_resource(item: Any, limits: McpClientLimits) -> dict[str, object]:
    if (
        type(item) is not types.Resource
        or type(item.name) is not str
        or not item.name
        or type(item.uri) is not str
        or not item.uri
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    normalized = {
        "name": item.name,
        "uri": item.uri,
        "description": _optional_text(item.description, limits.max_result_document_bytes),
        "mime_type": _text_mime(item.mime_type),
    }
    apps_ui = _normalize_apps_ui(item, limits)
    if apps_ui is not None:
        normalized["ui"] = apps_ui
    return normalized


def _normalize_resource_template(item: Any, limits: McpClientLimits) -> dict[str, object]:
    if (
        type(item) is not types.ResourceTemplate
        or type(item.name) is not str
        or not item.name
        or type(item.uri_template) is not str
        or not item.uri_template
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    _parse_uri_template(item.uri_template, max_variables=limits.max_list_items)
    normalized = {
        "name": item.name,
        "uri_template": item.uri_template,
        "description": _optional_text(item.description, limits.max_result_document_bytes),
        "mime_type": _text_mime(item.mime_type),
    }
    apps_ui = _normalize_apps_ui(item, limits)
    if apps_ui is not None:
        normalized["ui"] = apps_ui
    return normalized


def _normalize_apps_ui(
    item: Any,
    limits: McpClientLimits,
) -> Mapping[str, Any] | None:
    meta = getattr(item, "meta", None)
    if meta is None:
        return None
    if not isinstance(meta, Mapping):
        _fail(McpClientErrorReason.RESULT_INVALID)
    ui = meta.get("ui")
    if ui is None:
        return None
    if not isinstance(ui, Mapping):
        _fail(McpClientErrorReason.RESULT_INVALID)
    normalized = _bounded_document(
        ui,
        min(limits.max_result_document_bytes, 65_536),
        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
    )
    if not isinstance(normalized, Mapping):
        _fail(McpClientErrorReason.RESULT_INVALID)
    return normalized


def _matches_apps_ui(item: Mapping[str, Any], authority: Mapping[str, Any]) -> bool:
    expected = authority.get("ui")
    actual = item.get("ui")
    if (expected is None) != (actual is None):
        return False
    if expected is None:
        return True
    try:
        return canonical_bytes(actual) == canonical_bytes(expected)
    except (TypeError, UnicodeError, ValueError):
        return False


def _unique_by_name(items: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    for item in items:
        name = item["name"]
        if type(name) is not str or name in indexed:
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)
        indexed[name] = item
    return indexed


# --- restricted discovery -----------------------------------------------
#
# A discovery-only client (``CurrentMcpClient.for_restricted_discovery``)
# never reconciles against a declared capability set -- there is none yet,
# by construction, before the user has ever reviewed anything. The helpers
# below are a deliberately separate, parallel path from the reconciling
# helpers above: they reuse the same low-level bounding/schema-validation
# primitives (``_bounded_document``, ``_schema_validator``, ``_text_mime``,
# ``_bounded_text``, ``_parse_uri_template``, ``_prompt_schema``) but never
# call ``reconcile_live_capabilities``/``_validate_declared_authority`` --
# that equality check exists only for the ordinary reconciling ``start()``
# path and is never touched here. Unlike ``_normalize_tool`` and its
# siblings (which the reconciling path uses, and which never carry a title
# or the publisher's own MCP annotations -- authorized apps get those from
# their signed manifest instead), these normalizers *do* capture the
# server's own title and annotation claims verbatim, since a discovery
# receipt is the user's very first, unreviewed look at what a server says
# about itself.


def _discovery_server_identity(meta: object) -> tuple[str, str]:
    """Extract a server's own identity from one ``server/discover`` response.

    Reads the same ``_SERVER_INFO_META_KEY`` stamp the reconciling path
    reads, but only that -- no cross-check against a descriptor's own
    app_id/publisher_id/deployment/etc. (``_normalize_metadata``'s job for
    the reconciling path), since an arbitrary, not-yet-reviewed third-party
    server has no way to know or echo back Omega's internal identifiers.
    """
    if not isinstance(meta, Mapping) or any(type(key) is not str for key in meta):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    server_info = meta.get(_SERVER_INFO_META_KEY)
    if not isinstance(server_info, Mapping) or any(type(key) is not str for key in server_info):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    server_id = server_info.get("name")
    server_version = server_info.get("version")
    if (
        type(server_id) is not str
        or not server_id
        or type(server_version) is not str
        or not server_version
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return server_id, server_version


def _discovery_title(title: Any, name: str, limits: McpClientLimits) -> str:
    if title is None:
        return name
    if type(title) is not str:
        _fail(McpClientErrorReason.RESULT_INVALID)
    return _bounded_text(title, limits.max_result_document_bytes)


def _discovery_annotations(value: Any, limits: McpClientLimits) -> dict[str, object]:
    """Lower one publisher-claimed annotations object into a wire-shaped dict.

    Never influences risk classification (that boundary lives entirely in
    ``mcp_platform.plugin_discovery``) -- this only bounds and screens the
    claim so it can be carried in the receipt at all.
    """
    if value is None:
        return {}
    if not hasattr(value, "model_dump"):
        _fail(McpClientErrorReason.RESULT_INVALID)
    try:
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    except Exception:
        _fail(McpClientErrorReason.RESULT_INVALID)
    bounded = _bounded_document(
        dumped,
        limits.max_result_document_bytes,
        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
    )
    if type(bounded) is not dict:
        _fail(McpClientErrorReason.RESULT_INVALID)
    return bounded


def _discovery_tool(item: Any, limits: McpClientLimits) -> dict[str, object]:
    if (
        type(item) is not types.Tool
        or type(item.name) is not str
        or not item.name
        or type(item.input_schema) is not dict
        or item.output_schema is not None
        and type(item.output_schema) is not dict
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    input_schema = _bounded_document(
        item.input_schema,
        limits.max_result_document_bytes,
        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
    )
    _schema_validator(input_schema)
    normalized: dict[str, object] = {
        "name": item.name,
        "title": _discovery_title(item.title, item.name, limits),
        "input_schema": input_schema,
        "annotations": _discovery_annotations(item.annotations, limits),
    }
    if item.output_schema is not None:
        output_schema = _bounded_document(
            item.output_schema,
            limits.max_result_document_bytes,
            McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
        )
        _schema_validator(output_schema)
        normalized["output_schema"] = output_schema
    return normalized


def _discovery_prompt(item: Any, limits: McpClientLimits) -> dict[str, object]:
    if type(item) is not types.Prompt or type(item.name) is not str or not item.name:
        _fail(McpClientErrorReason.RESULT_INVALID)
    schema = _prompt_schema(item.arguments)
    return {
        "name": item.name,
        "title": _discovery_title(item.title, item.name, limits),
        # The MCP prompt type carries no annotations of its own -- only
        # tools and resources do -- so this is always the empty claim, never
        # fabricated from anything.
        "annotations": {},
        "arguments": schema["arguments"],
    }


def _discovery_resource(item: Any, limits: McpClientLimits) -> dict[str, object]:
    if (
        type(item) is not types.Resource
        or type(item.name) is not str
        or not item.name
        or type(item.uri) is not str
        or not item.uri
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    normalized: dict[str, object] = {
        "name": item.name,
        "title": _discovery_title(item.title, item.name, limits),
        "uri": item.uri,
        "annotations": _discovery_annotations(item.annotations, limits),
    }
    mime_type = _text_mime(item.mime_type)
    if mime_type is not None:
        normalized["mime_type"] = mime_type
    size = item.size
    if type(size) is int and type(size) is not bool and size > 0:
        normalized["max_bytes"] = size
    return normalized


def _discovery_resource_template(item: Any, limits: McpClientLimits) -> dict[str, object]:
    if (
        type(item) is not types.ResourceTemplate
        or type(item.name) is not str
        or not item.name
        or type(item.uri_template) is not str
        or not item.uri_template
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    _parse_uri_template(item.uri_template, max_variables=limits.max_list_items)
    normalized: dict[str, object] = {
        "name": item.name,
        "title": _discovery_title(item.title, item.name, limits),
        "uri_template": item.uri_template,
        "annotations": _discovery_annotations(item.annotations, limits),
    }
    mime_type = _text_mime(item.mime_type)
    if mime_type is not None:
        normalized["mime_type"] = mime_type
    return normalized


async def _discovery_pages(
    call: Callable[[str | None], Awaitable[Any]],
    *,
    limits: McpClientLimits,
    result_type: type[Any],
    item_field: str,
    normalize: Callable[[Any, McpClientLimits], dict[str, object]],
) -> list[dict[str, object]]:
    """Page one list operation with exactly the reconciling path's own
    pagination/count bounds (``max_list_pages``/``max_list_items``/
    ``max_cursor_history``) -- but with no ``McpDiscoveryReceipt`` to
    reconcile against, since discovery has not fixed a capability set yet.
    """
    pages = 0
    item_count = 0
    cursors: set[str] = set()
    cursor: str | None = None
    items: list[dict[str, object]] = []
    while True:
        pages += 1
        if pages > limits.max_list_pages:
            _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
        page = await call(cursor)
        if type(page) is not result_type or page.result_type != "complete":
            _fail(McpClientErrorReason.RESULT_INVALID)
        raw_items = getattr(page, item_field, None)
        if type(raw_items) is not list:
            _fail(McpClientErrorReason.RESULT_INVALID)
        item_count += len(raw_items)
        if item_count > limits.max_list_items:
            _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        items.extend(normalize(item, limits) for item in raw_items)
        next_cursor = page.next_cursor
        if next_cursor is None:
            break
        if (
            type(next_cursor) is not str
            or not next_cursor
            or next_cursor in cursors
            or len(cursors) >= limits.max_cursor_history
            or pages >= limits.max_list_pages
        ):
            _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
        cursors.add(next_cursor)
        cursor = next_cursor
    return items


def _validate_tool_authority(
    receipt: McpDiscoveryReceipt,
    items: list[dict[str, object]],
) -> None:
    indexed = _unique_by_name(items)
    if set(indexed) != set(receipt.tools):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    for name, item in indexed.items():
        authority = receipt.tools[name]
        if not isinstance(authority, Mapping):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)
        if _schema_digest(item["input_schema"]) != authority.get("input_schema_sha256"):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)
        expected_output = authority.get("output_schema_sha256")
        actual_output = item["output_schema"]
        if (expected_output is None) != (actual_output is None) or (
            actual_output is not None and _schema_digest(actual_output) != expected_output
        ):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)
        if not _matches_apps_ui(item, authority):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)


def _validate_prompt_authority(
    receipt: McpDiscoveryReceipt,
    items: list[dict[str, object]],
) -> None:
    indexed = _unique_by_name(items)
    if set(indexed) != set(receipt.prompts):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    for name, item in indexed.items():
        authority = receipt.prompts[name]
        if not isinstance(authority, Mapping) or _schema_digest(
            {"arguments": item["arguments"]}
        ) != authority.get("schema_sha256"):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)


def _validate_resource_authority(
    authority: Mapping[str, Any],
    items: list[dict[str, object]],
    *,
    endpoint_field: str,
) -> None:
    indexed = _unique_by_name(items)
    if set(indexed) != set(authority):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    for name, item in indexed.items():
        expected = authority[name]
        if (
            not isinstance(expected, Mapping)
            or item[endpoint_field] != expected.get(endpoint_field)
            or item["mime_type"] != expected.get("mime_type")
            or not _matches_apps_ui(item, expected)
        ):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)


def _normalize_content(content: Any, limits: McpClientLimits) -> dict[str, object]:
    if type(content) is types.TextContent:
        return {
            "type": "text",
            "text": _bounded_text(content.text, limits.max_result_document_bytes),
        }
    if type(content) is types.EmbeddedResource:
        resource = content.resource
        if type(resource) is not types.TextResourceContents:
            _fail(McpClientErrorReason.RESULT_INVALID)
        text = _bounded_text(resource.text, limits.max_embedded_resource_bytes)
        return {
            "type": "resource",
            "resource": {
                "uri": resource.uri,
                "mime_type": _text_mime(resource.mime_type),
                "text": text,
            },
        }
    _fail(McpClientErrorReason.RESULT_INVALID)


def _normalize_tool_result(result: Any, limits: McpClientLimits) -> dict[str, object]:
    if type(result) is not types.CallToolResult or type(result.content) is not list:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if type(result.is_error) is not bool:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if len(result.content) > limits.max_list_items:
        _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
    structured_content = (
        None
        if result.structured_content is None
        else _bounded_document(
            result.structured_content,
            limits.max_structured_result_bytes,
            McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
        )
    )
    data = {
        "content": [_normalize_content(item, limits) for item in result.content],
        "structured_content": structured_content,
        "is_error": result.is_error,
    }
    return _bounded_document(
        data,
        limits.max_result_document_bytes,
        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
    )


def _task_terminal_tool_result(result: GetTaskResult) -> types.CallToolResult:
    if result.status != "completed":
        return types.CallToolResult(content=[], structuredContent=None, isError=True)
    document = result.result
    if not isinstance(document, dict) or not set(document) <= {
        "_meta",
        "content",
        "structuredContent",
        "isError",
        "resultType",
    }:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if "content" not in document or document.get("resultType") != "complete":
        _fail(McpClientErrorReason.RESULT_INVALID)
    try:
        parsed = types.CallToolResult.model_validate(document)
    except ValidationError:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if parsed.result_type != "complete":
        _fail(McpClientErrorReason.RESULT_INVALID)
    return parsed


def _enforce_signed_tool_result(
    normalized: dict[str, object],
    policy: Mapping[str, Any],
) -> dict[str, object]:
    allowed_mime_types = policy["allowed_mime_types"]
    # A list is what a signed policy actually carries: it is JSON inside the
    # verifier's mappingproxy, and `_installed_package_authority` builds it with
    # `list(...)` too. Demanding a tuple refused every policy Omega constructs,
    # on its type, before any content was examined. What matters is that this is
    # a real collection of mime-type strings -- and a bare string is still
    # refused, since iterating one yields characters.
    if type(allowed_mime_types) not in {tuple, list} or any(
        type(item) is not str for item in allowed_mime_types
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    observed_mime_types: set[str] = set()
    if normalized["structured_content"] is not None:
        observed_mime_types.add("application/json")
    content = normalized["content"]
    if not isinstance(content, list):
        _fail(McpClientErrorReason.RESULT_INVALID)
    for item in content:
        if not isinstance(item, Mapping):
            _fail(McpClientErrorReason.RESULT_INVALID)
        if item.get("type") == "text":
            observed_mime_types.add("text/plain")
        elif item.get("type") == "resource":
            resource = item.get("resource")
            if not isinstance(resource, Mapping) or type(resource.get("mime_type")) is not str:
                _fail(McpClientErrorReason.RESULT_INVALID)
            observed_mime_types.add(resource["mime_type"])
        else:
            _fail(McpClientErrorReason.RESULT_INVALID)
    if not observed_mime_types <= set(allowed_mime_types):
        _fail(McpClientErrorReason.RESULT_INVALID)
    return _bounded_document(
        normalized,
        policy["max_result_bytes"],
        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
    )


def _normalize_prompt_result(result: Any, limits: McpClientLimits) -> dict[str, object]:
    if type(result) is not types.GetPromptResult or type(result.messages) is not list:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if len(result.messages) > limits.max_list_items:
        _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
    messages: list[dict[str, object]] = []
    for message in result.messages:
        if type(message) is not types.PromptMessage or message.role not in {"user", "assistant"}:
            _fail(McpClientErrorReason.RESULT_INVALID)
        messages.append(
            {
                "role": message.role,
                "content": _normalize_content(message.content, limits),
            }
        )
    data = {
        "description": _optional_text(result.description, limits.max_result_document_bytes),
        "messages": messages,
    }
    return _bounded_document(
        data,
        limits.max_result_document_bytes,
        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
    )


def _normalize_read_result(
    result: Any,
    limits: McpClientLimits,
    *,
    expected_uri: str,
    expected_mime_type: str | None = None,
    max_bytes: int | None = None,
) -> dict[str, object]:
    if type(result) is not types.ReadResourceResult or type(result.contents) is not list:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if len(result.contents) > limits.max_list_items:
        _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
    contents: list[dict[str, object]] = []
    signed_payload_bytes = 0
    for content in result.contents:
        if type(content) is not types.TextResourceContents or content.uri != expected_uri:
            _fail(McpClientErrorReason.RESULT_INVALID)
        mime_type = _text_mime(content.mime_type)
        if expected_mime_type is not None and mime_type != expected_mime_type:
            _fail(McpClientErrorReason.RESULT_INVALID)
        text = _bounded_text(
            content.text,
            limits.max_resource_text_bytes if max_bytes is None else max_bytes,
        )
        if max_bytes is not None:
            signed_payload_bytes += len(text.encode("utf-8"))
            if signed_payload_bytes > max_bytes:
                _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        contents.append(
            {
                "uri": content.uri,
                "mime_type": mime_type,
                "text": text,
            }
        )
    return _bounded_document(
        {"contents": contents},
        limits.max_result_document_bytes,
        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
    )


class CurrentMcpClient:
    """Own one exact-version SDK client context and its live discovery receipt."""

    def __init__(
        self,
        descriptor: McpConnectionDescriptor,
        sdk_client_factory: SdkClientFactory,
        *,
        discovery_only: bool = False,
    ) -> None:
        if type(descriptor) is not McpConnectionDescriptor or type(discovery_only) is not bool:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if discovery_only and (
            descriptor.deployment is not McpClientDeployment.REMOTE
            or descriptor.transport is not McpTransport.STREAMABLE_HTTP
            or descriptor.declared_capabilities
            or descriptor.signed_capability_authority is not None
        ):
            # A discovery-only client can only ever exist for a first-contact
            # connection with nothing declared yet -- never for a descriptor
            # that already carries a declared capability set or a signed
            # authority, which would mean this is not actually first contact.
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        self._discovery_only = discovery_only
        self._descriptor = descriptor
        self._sdk_client_factory = sdk_client_factory
        self._connection_revision = _descriptor_binding(descriptor)
        factory_record = (
            _ISSUED_SDK_CLIENT_FACTORIES.get(sdk_client_factory)
            if type(sdk_client_factory) is SdkClientFactory
            else None
        )
        self._authorization_context_revision = (
            factory_record.authorization_context_revision
            if factory_record is not None and factory_record.factory_ref() is sdk_client_factory
            else "0" * 64
        )
        self._state = _LifecycleState.NEW
        self._owner_task: asyncio.Task[BaseException | None] | None = None
        self._close_requested: asyncio.Event | None = None
        self._connected: Any | None = None
        self._receipt: McpDiscoveryReceipt | None = None
        self._active_operations: dict[
            asyncio.Task[Any],
            Callable[[], None] | None,
        ] = {}
        self._cache: CacheConfig | None = None
        self._tasks: dict[tuple[str, str, str, str, str, str], _LiveTask] = {}
        self._task_tombstones: dict[tuple[str, str, str, str, str, str], None] = {}
        self._pending_task_creations = 0
        self._cleanup_recovery_required = False
        self._drift_status: McpProviderDriftStatus | None = None

    @classmethod
    def for_restricted_discovery(
        cls,
        descriptor: McpConnectionDescriptor,
        sdk_client_factory: SdkClientFactory,
    ) -> CurrentMcpClient:
        """Construct a client that can only ever call ``discover_restricted``.

        The one production path a not-yet-reviewed publisher-verified or
        personal MCP connection may use: ``discover_restricted`` negotiates
        the protocol and enumerates capabilities and nothing else. Every
        other public operation this class exposes (``list_tools``,
        ``call_tool``, ``get_prompt``, ``read_resource``, ``listen``, the
        task methods, ...) fails closed with
        ``McpClientErrorReason.REVIEW_REQUIRED`` on a client constructed this
        way -- there is no code path back to an ordinary, reconciling
        ``start()`` session from here.
        """
        return cls(descriptor, sdk_client_factory, discovery_only=True)

    @property
    def connection_revision(self) -> str:
        return self._connection_revision

    @property
    def authorization_context_revision(self) -> str:
        return self._authorization_context_revision

    @property
    def drift_status(self) -> McpProviderDriftStatus | None:
        """Return the last typed provider mismatch, if one occurred."""
        return self._drift_status

    def _record_drift(
        self,
        reason: McpClientErrorReason,
        *,
        phase: str,
    ) -> None:
        self._drift_status = McpProviderDriftStatus(
            phase=phase,
            reason=reason,
            reconnect_required=reason
            in {
                McpClientErrorReason.AUTHORIZATION_REQUIRED,
                McpClientErrorReason.AUTHORIZATION_INVALID,
            },
        )

    @property
    def receipt(self) -> McpDiscoveryReceipt:
        if self._state is not _LifecycleState.LIVE or self._receipt is None:
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        return self._receipt

    async def start(self) -> McpDiscoveryReceipt:
        if self._discovery_only:
            _fail(McpClientErrorReason.REVIEW_REQUIRED)
        if self._state is not _LifecycleState.NEW:
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        loop = asyncio.get_running_loop()
        connection_deadline = (
            loop.time() + self._descriptor.limits.max_connection_duration_ms / 1_000
        )
        self._descriptor = _snapshot_descriptor(self._descriptor)
        self._connection_revision = _descriptor_binding(self._descriptor)
        extensions: tuple[ClientExtension, ...] = ()
        if TASKS_EXTENSION_IDENTIFIER in self._descriptor.extension_versions:
            extensions = (TasksClientExtension(),)
        cache = build_cache_config(
            app_id=self._descriptor.app_id,
            connection_revision=self._connection_revision,
            authorization_context_revision=self._authorization_context_revision,
            max_entries=self._descriptor.limits.max_cache_entries,
            max_ttl_seconds=self._descriptor.limits.max_cache_ttl_seconds,
        )
        self._cache = cache
        sdk_client_builder, discovery_source, authorization_context_revision = _sdk_client_builder(
            self._descriptor,
            self._sdk_client_factory,
        )
        if authorization_context_revision != self._authorization_context_revision:
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        self._state = _LifecycleState.STARTING
        ready: asyncio.Future[_StartOutcome] = loop.create_future()
        close_requested = asyncio.Event()
        owner = asyncio.create_task(
            self._run_owner(
                sdk_client_builder,
                discovery_source,
                extensions,
                cache,
                ready,
                close_requested,
            ),
            name=f"{_OWNER_TASK_PREFIX}{id(self):x}",
        )
        self._owner_task = owner
        self._close_requested = close_requested
        active_error: BaseException | None = None
        outcome: _StartOutcome | None = None
        try:
            done, _pending = await asyncio.wait(
                {ready},
                timeout=max(0.0, connection_deadline - loop.time()),
            )
            if done:
                outcome = ready.result()
        except BaseException as error:
            active_error = error
        if active_error is not None or outcome is None:
            owner.cancel()
            shutdown_deadline = (
                loop.time() + self._descriptor.limits.max_shutdown_duration_ms / 1_000
            )
            joined, _owner_result, cleanup_cancellation = await self._join_owner(
                owner,
                deadline=shutdown_deadline,
            )
            if cleanup_cancellation is not None and active_error is None:
                active_error = cleanup_cancellation
            if not joined:
                self._cleanup_recovery_required = True
                owner.add_done_callback(self._consume_owned_task)
            self._retire()
            if active_error is not None:
                if isinstance(active_error, McpClientError):
                    self._record_drift(active_error.reason, phase="initialize")
                raise active_error
            if not joined:
                _fail(McpClientErrorReason.CLEANUP_REQUIRED)
            _fail(McpClientErrorReason.REQUEST_TIMEOUT)
        assert outcome is not None
        if outcome.error is not None:
            shutdown_deadline = (
                loop.time() + self._descriptor.limits.max_shutdown_duration_ms / 1_000
            )
            joined, _owner_result, cleanup_cancellation = await self._join_owner(
                owner,
                deadline=shutdown_deadline,
            )
            if not joined:
                self._cleanup_recovery_required = True
                owner.add_done_callback(self._consume_owned_task)
            self._retire()
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            if not joined:
                _fail(McpClientErrorReason.CLEANUP_REQUIRED)
            public_error = _public_client_error(self._descriptor, outcome.error)
            if isinstance(public_error, McpClientError):
                self._record_drift(public_error.reason, phase="discovery")
            raise public_error
        if (
            self._state is not _LifecycleState.STARTING
            or outcome.connected is None
            or outcome.receipt is None
        ):
            close_requested.set()
            shutdown_deadline = (
                loop.time() + self._descriptor.limits.max_shutdown_duration_ms / 1_000
            )
            joined, _owner_result, cleanup_cancellation = await self._join_owner(
                owner,
                deadline=shutdown_deadline,
            )
            if not joined:
                self._cleanup_recovery_required = True
                owner.add_done_callback(self._consume_owned_task)
            self._retire()
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            if not joined:
                _fail(McpClientErrorReason.CLEANUP_REQUIRED)
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        self._connected = outcome.connected
        self._receipt = outcome.receipt
        self._state = _LifecycleState.LIVE
        return outcome.receipt

    async def discover_restricted(self) -> RestrictedDiscoveryReceipt:
        """Negotiate and enumerate -- and nothing else -- then close.

        Only callable on a client built by :meth:`for_restricted_discovery`,
        and only once. Issues ``initialize`` (the SDK's own session
        handshake -- never spelled as a literal call anywhere in this
        module), then ``tools/list``, ``prompts/list``, ``resources/list``,
        and ``resources/templates/list`` in that exact order and no others,
        then closes the session before returning. This is a deliberately
        self-contained, one-shot connect-enumerate-disconnect operation --
        unlike :meth:`start`, it never leaves a session open for later
        operations (there are none it could ever perform), so it does not
        share ``start()``'s long-lived owner-task/ready-future machinery.
        Never goes through ``reconcile_live_capabilities``/
        ``_validate_declared_authority`` -- there is no declared capability
        set yet to reconcile against.
        """
        if not self._discovery_only:
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        if self._state is not _LifecycleState.NEW:
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        self._state = _LifecycleState.STARTING
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._descriptor.limits.max_connection_duration_ms / 1_000
        try:
            sdk_client_builder, authorization_context_revision = (
                _discovery_sdk_client_builder(self._descriptor, self._sdk_client_factory)
            )
            if authorization_context_revision != self._authorization_context_revision:
                _fail(McpClientErrorReason.IDENTITY_MISMATCH)
            context = sdk_client_builder(
                mode=_sdk_mode_for_descriptor(self._descriptor),
                client_info=_client_info(),
                extensions=(),
                cache=None,
            )
            async with asyncio.timeout_at(deadline):
                async with context as connected:
                    receipt = await self._collect_restricted_discovery(connected)
        except McpClientError:
            self._state = _LifecycleState.CLOSED
            raise
        except asyncio.CancelledError:
            self._state = _LifecycleState.CLOSED
            raise
        except TimeoutError:
            self._state = _LifecycleState.CLOSED
            _fail(McpClientErrorReason.REQUEST_TIMEOUT)
        except Exception as error:
            self._state = _LifecycleState.CLOSED
            # A failure raised deep inside ``_collect_restricted_discovery``
            # propagates out through the SDK session's own internal task
            # group (``async with context as connected:``'s exit), which
            # wraps it in an ``ExceptionGroup`` before it ever reaches the
            # clean `except McpClientError`/`except TimeoutError` clauses
            # above -- unwrap to find the real, single leaf cause rather
            # than reporting every such failure as content-free
            # TRANSPORT_UNAVAILABLE.
            leaf = _unwrap_exception_group(error)
            if isinstance(leaf, McpClientError):
                raise leaf
            if isinstance(leaf, TimeoutError):
                _fail(McpClientErrorReason.REQUEST_TIMEOUT)
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        self._state = _LifecycleState.CLOSED
        return receipt

    async def _collect_restricted_discovery(self, connected: Any) -> RestrictedDiscoveryReceipt:
        descriptor = self._descriptor
        if (
            type(connected.protocol_version) is not str
            or connected.protocol_version != MCP_PROTOCOL_VERSION
        ):
            _fail(McpClientErrorReason.PROTOCOL_MISMATCH)
        # Negotiation ends here -- everything below is enumeration. Identity
        # comes from the standard ``server/discover`` request/response pair
        # (``types.DiscoverRequest``/``DiscoverResult``, defined by the SDK
        # itself, not an Omega extension) rather than the ``server_info``/
        # ``server_capabilities`` convenience properties the reconciling
        # ``_discover_receipt`` path prefers when available: those properties
        # populate only from ``initialize()``/``discover()``/``adopt()``
        # explicitly being called on the session, none of which the implicit
        # connection handshake performs on its own, and ``initialize`` may
        # never be spelled as a literal call anywhere in this file. This also
        # never validates the response's identity fields against the
        # descriptor's own app_id/publisher_id/etc. the way
        # ``_normalize_metadata`` does for the reconciling path -- an
        # arbitrary, not-yet-reviewed third-party server has no way to know
        # Omega's internal identifiers to echo back.
        session = connected.session
        try:
            discovered = await session.send_request(
                types.DiscoverRequest(),
                types.DiscoverResult,
            )
        except MCPError as error:
            if error.code != types.METHOD_NOT_FOUND:
                raise
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        if type(discovered) is not types.DiscoverResult:
            _fail(McpClientErrorReason.RESULT_INVALID)
        supported_versions = discovered.supported_versions
        if (
            type(supported_versions) is not list
            or not supported_versions
            or len(supported_versions) > 16
            or len(supported_versions) != len(set(supported_versions))
            or any(type(item) is not str or not item for item in supported_versions)
            or connected.protocol_version not in supported_versions
        ):
            _fail(McpClientErrorReason.PROTOCOL_MISMATCH)
        server_id, server_version = _discovery_server_identity(discovered.meta)
        capabilities = discovered.capabilities
        if type(capabilities) is not types.ServerCapabilities:
            _fail(McpClientErrorReason.RESULT_INVALID)
        tools_list_changed = (
            bool(capabilities.tools.list_changed) if capabilities.tools is not None else False
        )
        prompts_list_changed = (
            bool(capabilities.prompts.list_changed) if capabilities.prompts is not None else False
        )
        resources_list_changed = (
            bool(capabilities.resources.list_changed)
            if capabilities.resources is not None
            else False
        )
        limits = descriptor.limits
        tools = await _discovery_pages(
            lambda cursor: connected.list_tools(cursor=cursor, cache_mode="use"),
            limits=limits,
            result_type=types.ListToolsResult,
            item_field="tools",
            normalize=_discovery_tool,
        )
        prompts = await _discovery_pages(
            lambda cursor: connected.list_prompts(cursor=cursor, cache_mode="use"),
            limits=limits,
            result_type=types.ListPromptsResult,
            item_field="prompts",
            normalize=_discovery_prompt,
        )
        resources = await _discovery_pages(
            lambda cursor: connected.list_resources(cursor=cursor, cache_mode="use"),
            limits=limits,
            result_type=types.ListResourcesResult,
            item_field="resources",
            normalize=_discovery_resource,
        )
        resource_templates = await _discovery_pages(
            lambda cursor: connected.list_resource_templates(cursor=cursor, cache_mode="use"),
            limits=limits,
            result_type=types.ListResourceTemplatesResult,
            item_field="resource_templates",
            normalize=_discovery_resource_template,
        )
        return RestrictedDiscoveryReceipt(
            server_id=server_id,
            server_version=server_version,
            tools_list_changed=tools_list_changed,
            prompts_list_changed=prompts_list_changed,
            resources_list_changed=resources_list_changed,
            tools=tuple(tools),
            prompts=tuple(prompts),
            resources=tuple(resources),
            resource_templates=tuple(resource_templates),
        )

    def _operation_receipt(
        self,
        request: McpOperationRequest,
        operation: McpOperationKind,
    ) -> McpDiscoveryReceipt:
        if type(request) is not McpOperationRequest:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if self._state is not _LifecycleState.LIVE or self._receipt is None:
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        receipt = self._receipt
        if request.operation is not operation:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if request.app_id != self._descriptor.app_id or request.app_id != receipt.app_id:
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        if (
            request.capability_revision != self._descriptor.capability_revision
            or request.capability_revision != receipt.capability_revision
        ):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)
        task_operation = operation in {
            McpOperationKind.TASK_GET,
            McpOperationKind.TASK_UPDATE,
            McpOperationKind.TASK_CANCEL,
        }
        if request.input_request_id is not None or (
            task_operation != (request.task_id is not None)
        ):
            _fail(McpClientErrorReason.RESULT_INVALID)
        if (
            operation is McpOperationKind.SUBSCRIPTIONS_LISTEN
            and request.capability_id != "subscriptions/listen"
        ):
            _fail(McpClientErrorReason.RESULT_INVALID)
        expected_surface = _LIST_OPERATIONS.get(operation)
        if expected_surface is not None and request.capability_id != expected_surface:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if operation is McpOperationKind.TOOL_CALL:
            name = self._capability_name(request.capability_id, "tool:")
            if name not in receipt.tools:
                _fail(McpClientErrorReason.CAPABILITY_DRIFT)
        elif operation is McpOperationKind.PROMPT_GET:
            name = self._capability_name(request.capability_id, "prompt:")
            if name not in receipt.prompts:
                _fail(McpClientErrorReason.CAPABILITY_DRIFT)
        elif operation is McpOperationKind.RESOURCE_READ:
            name = self._capability_name(request.capability_id, "resource:")
            if name not in receipt.resources and name not in receipt.resource_templates:
                _fail(McpClientErrorReason.CAPABILITY_DRIFT)
        return receipt

    @staticmethod
    def _capability_name(capability_id: str, prefix: str) -> str:
        if not capability_id.startswith(prefix) or len(capability_id) == len(prefix):
            _fail(McpClientErrorReason.RESULT_INVALID)
        return capability_id[len(prefix) :]

    def _ensure_operation_revision(self, context: _OperationContext) -> None:
        if (
            self._state is not _LifecycleState.LIVE
            or self._receipt is not context.receipt
            or context.request.capability_revision != self._descriptor.capability_revision
            or context.receipt.capability_revision != self._descriptor.capability_revision
        ):
            _fail(McpClientErrorReason.CAPABILITY_DRIFT)

    async def _run_operation(
        self,
        request: McpOperationRequest,
        operation: McpOperationKind,
        run: Callable[
            [_OperationContext],
            Awaitable[Mapping[str, object] | _OperationResponse],
        ],
        *,
        input_callback: InputCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        ownership_released: Callable[[], None] | None = None,
    ) -> McpOperationResult:
        if self._discovery_only:
            # Every operation method funnels through here -- tools/prompts/
            # resources list and call/get/read, subscriptions, tasks -- so
            # this one guard, checked before anything else (including the
            # try/task machinery below, which converts most other failures
            # into a returned FAILED result rather than raising) is what
            # makes a discovery-only client structurally incapable of
            # reaching any of them: this raises, it is never swallowed.
            _fail(McpClientErrorReason.REVIEW_REQUIRED)
        context: _OperationContext | None = None
        operation_task: asyncio.Task[Any] | None = None
        try:
            receipt = self._operation_receipt(request, operation)
            context = _OperationContext(
                request=request,
                receipt=receipt,
                input_callback=input_callback,
                progress_callback=progress_callback,
                input_request_ids=set(),
            )
            loop = asyncio.get_running_loop()
            now = loop.time()
            deadline = min(
                request.deadline_monotonic,
                now + self._descriptor.limits.max_operation_duration_ms / 1_000,
            )
            if deadline <= now:
                _fail(McpClientErrorReason.REQUEST_TIMEOUT)

            async def execute() -> Mapping[str, object] | _OperationResponse:
                response = await run(context)
                self._ensure_operation_revision(context)
                return response

            token = _ACTIVE_OPERATION.set(context)
            try:
                operation_task = asyncio.create_task(
                    execute(),
                    name=f"omega-mcp-operation:{id(context):x}",
                )
            finally:
                _ACTIVE_OPERATION.reset(token)
            self._active_operations[operation_task] = ownership_released
            operation_task.add_done_callback(
                lambda _completed: self._release_operation_ownership(operation_task)
            )
            active_error: BaseException | None = None
            timed_out = False
            try:
                done, _pending = await asyncio.wait(
                    {operation_task},
                    timeout=max(0.0, deadline - loop.time()),
                )
                timed_out = operation_task not in done
            except BaseException as error:
                active_error = error
            if timed_out or active_error is not None:
                operation_task.cancel()
            joined_result: object | BaseException | None
            if operation_task.done():
                try:
                    joined_result = operation_task.result()
                except BaseException as error:
                    joined_result = error
                joined = True
            else:
                shutdown_deadline = (
                    loop.time() + self._descriptor.limits.max_shutdown_duration_ms / 1_000
                )
                joined, joined_result, cleanup_cancellation = await self._join_owned_task(
                    operation_task,
                    deadline=shutdown_deadline,
                )
                if cleanup_cancellation is not None and active_error is None:
                    active_error = cleanup_cancellation
            if not joined:
                self._cleanup_recovery_required = True
            if active_error is not None:
                raise active_error
            if not joined:
                _fail(McpClientErrorReason.CLEANUP_REQUIRED)
            if timed_out:
                _fail(McpClientErrorReason.REQUEST_TIMEOUT)
            if isinstance(joined_result, BaseException):
                raise joined_result
            response = joined_result
            if type(response) is _OperationResponse:
                return _result(
                    request,
                    status=response.status,
                    data=response.data,
                    task_handle=response.task_handle,
                )
            return _result(request, status=McpOperationStatus.COMPLETED, data=response)
        except BaseException as error:
            if context is not None and context.callback_error is not None:
                raise context.callback_error
            if isinstance(error, asyncio.CancelledError) or not isinstance(error, Exception):
                raise error
            if type(error) is McpClientError:
                self._record_drift(error.reason, phase=operation.value)
                return _failed(request, error.reason)
            if isinstance(error, TimeoutError):
                self._record_drift(
                    McpClientErrorReason.REQUEST_TIMEOUT,
                    phase=operation.value,
                )
                return _failed(request, McpClientErrorReason.REQUEST_TIMEOUT)
            if isinstance(error, MCPError | SubscriptionLost):
                self._record_drift(
                    McpClientErrorReason.TRANSPORT_UNAVAILABLE,
                    phase=operation.value,
                )
                return _failed(request, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
            if isinstance(
                error,
                (
                    RuntimeError,
                    ValidationError,
                    jsonschema.SchemaError,
                    jsonschema.ValidationError,
                    TypeError,
                    ValueError,
                ),
            ):
                self._record_drift(
                    McpClientErrorReason.RESULT_INVALID,
                    phase=operation.value,
                )
                return _failed(request, McpClientErrorReason.RESULT_INVALID)
            if isinstance(error, OSError):
                self._record_drift(
                    McpClientErrorReason.TRANSPORT_UNAVAILABLE,
                    phase=operation.value,
                )
                return _failed(request, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
            self._record_drift(
                McpClientErrorReason.TRANSPORT_UNAVAILABLE,
                phase=operation.value,
            )
            return _failed(request, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        finally:
            if operation_task is None and ownership_released is not None:
                ownership_released()

    async def _list_tools_data(self, context: _OperationContext) -> list[dict[str, object]]:
        connected = self._connected
        if connected is None:
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        items = await _operation_pages(
            lambda cursor: connected.list_tools(cursor=cursor, cache_mode="use"),
            descriptor=self._descriptor,
            receipt=context.receipt,
            result_type=types.ListToolsResult,
            item_field="tools",
            normalize=lambda item: _normalize_tool(item, self._descriptor.limits),
        )
        _validate_tool_authority(context.receipt, items)
        return items

    async def _run_input_rounds(
        self,
        context: _OperationContext,
        first: Any,
        retry: Callable[[dict[str, Any] | None, str | None], Awaitable[Any]],
    ) -> Any:
        current = first
        state_only_delay = 0.05
        while type(current) is types.InputRequiredResult:
            _operation_metadata(self._descriptor, context.receipt, current.meta)
            self._ensure_operation_revision(context)
            context.input_rounds += 1
            if context.input_rounds > self._descriptor.limits.max_input_rounds:
                _fail(McpClientErrorReason.INPUT_LIMIT_EXCEEDED)

            request_state = current.request_state
            if request_state is None:
                request_state_bytes = b""
            elif type(request_state) is str:
                _bounded_text(
                    request_state,
                    self._descriptor.limits.max_result_document_bytes,
                    McpClientErrorReason.INPUT_LIMIT_EXCEEDED,
                )
                try:
                    request_state_bytes = request_state.encode("utf-8")
                except UnicodeEncodeError:
                    _fail(McpClientErrorReason.RESULT_INVALID)
            else:
                _fail(McpClientErrorReason.RESULT_INVALID)

            raw_requests = current.input_requests
            if raw_requests is None:
                raw_requests = {}
            if type(raw_requests) is not dict:
                _fail(McpClientErrorReason.RESULT_INVALID)
            max_total = (
                self._descriptor.limits.max_input_rounds * self._descriptor.limits.max_input_fields
            )
            prospective_requests = context.input_requests + len(raw_requests)
            if prospective_requests > max_total:
                _fail(McpClientErrorReason.INPUT_LIMIT_EXCEEDED)

            pending_ids: set[str] = set()
            prepared: list[
                tuple[
                    str,
                    jsonschema.protocols.Validator,
                    McpInputRequest,
                    frozenset[str],
                ]
            ] = []
            prospective_fields = context.input_fields
            for request_id, embedded in raw_requests.items():
                if (
                    type(request_id) is not str
                    or not request_id
                    or request_id in context.input_request_ids
                    or request_id in pending_ids
                    or type(embedded) is not types.ElicitRequest
                    or type(embedded.params) is not types.ElicitRequestFormParams
                    or embedded.params.task is not None
                ):
                    _fail(McpClientErrorReason.RESULT_INVALID)
                _bounded_text(
                    request_id,
                    self._descriptor.limits.max_result_document_bytes,
                    McpClientErrorReason.INPUT_LIMIT_EXCEEDED,
                )
                schema = _bounded_document(
                    embedded.params.requested_schema,
                    self._descriptor.limits.max_result_document_bytes,
                    McpClientErrorReason.INPUT_LIMIT_EXCEEDED,
                )
                validator = _schema_validator(schema)
                if type(schema) is not dict or schema.get("type") != "object":
                    _fail(McpClientErrorReason.RESULT_INVALID)
                properties = schema.get("properties")
                if (
                    type(properties) is not dict
                    or not properties
                    or len(properties) > self._descriptor.limits.max_input_fields
                    or any(type(field) is not str or not field for field in properties)
                ):
                    _fail(McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
                field_ids = frozenset(properties)
                _validate_elicitation_required_closure(schema, field_ids)
                prospective_fields += len(properties)
                if prospective_fields > max_total:
                    _fail(McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
                message = _bounded_text(
                    embedded.params.message,
                    self._descriptor.limits.max_result_document_bytes,
                    McpClientErrorReason.INPUT_LIMIT_EXCEEDED,
                )
                pending_ids.add(request_id)
                prepared.append(
                    (
                        request_id,
                        validator,
                        McpInputRequest(
                            request_id=request_id,
                            capability_id=context.request.capability_id,
                            field_ids=tuple(properties),
                            field_schemas=properties,
                            request_state=request_state_bytes,
                            round_number=context.input_rounds,
                            message=message,
                        ),
                        field_ids,
                    )
                )

            context.input_requests = prospective_requests
            context.input_fields = prospective_fields
            context.input_request_ids.update(pending_ids)
            responses: dict[str, Any] | None = None
            if prepared:
                if context.input_callback is None:
                    _fail(McpClientErrorReason.INPUT_LIMIT_EXCEEDED)
                responses = {}
                for request_id, validator, input_request, field_ids in prepared:
                    try:
                        response = await context.input_callback(input_request)
                    except asyncio.CancelledError:
                        raise
                    except BaseException as error:
                        context.callback_error = error
                        raise
                    self._ensure_operation_revision(context)
                    if not isinstance(response, Mapping) or any(
                        type(key) is not str for key in response
                    ):
                        _fail(McpClientErrorReason.RESULT_INVALID)
                    response_document = _bounded_document(
                        response,
                        self._descriptor.limits.max_tool_argument_bytes,
                        McpClientErrorReason.INPUT_LIMIT_EXCEEDED,
                    )
                    if (
                        not isinstance(response_document, dict)
                        or not set(response_document) <= field_ids
                    ):
                        _fail(McpClientErrorReason.RESULT_INVALID)
                    validator.validate(response_document)
                    responses[request_id] = types.ElicitResult(
                        action="accept",
                        content=response_document,
                    )
                state_only_delay = 0.05
            else:
                await asyncio.sleep(state_only_delay)
                state_only_delay = min(state_only_delay * 2, 0.25)
            self._ensure_operation_revision(context)
            current = await retry(responses, request_state)
        return current

    async def _list_prompts_data(self, context: _OperationContext) -> list[dict[str, object]]:
        connected = self._connected
        if connected is None:
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        items = await _operation_pages(
            lambda cursor: connected.list_prompts(cursor=cursor, cache_mode="use"),
            descriptor=self._descriptor,
            receipt=context.receipt,
            result_type=types.ListPromptsResult,
            item_field="prompts",
            normalize=lambda item: _normalize_prompt(item, self._descriptor.limits),
        )
        _validate_prompt_authority(context.receipt, items)
        return items

    async def _list_resources_data(self, context: _OperationContext) -> list[dict[str, object]]:
        connected = self._connected
        if connected is None:
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        items = await _operation_pages(
            lambda cursor: connected.list_resources(cursor=cursor, cache_mode="use"),
            descriptor=self._descriptor,
            receipt=context.receipt,
            result_type=types.ListResourcesResult,
            item_field="resources",
            normalize=lambda item: _normalize_resource(item, self._descriptor.limits),
        )
        _validate_resource_authority(
            context.receipt.resources,
            items,
            endpoint_field="uri",
        )
        return items

    async def _list_templates_data(self, context: _OperationContext) -> list[dict[str, object]]:
        connected = self._connected
        if connected is None:
            _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        items = await _operation_pages(
            lambda cursor: connected.list_resource_templates(
                cursor=cursor,
                cache_mode="use",
            ),
            descriptor=self._descriptor,
            receipt=context.receipt,
            result_type=types.ListResourceTemplatesResult,
            item_field="resource_templates",
            normalize=lambda item: _normalize_resource_template(item, self._descriptor.limits),
        )
        _validate_resource_authority(
            context.receipt.resource_templates,
            items,
            endpoint_field="uri_template",
        )
        return items

    async def list_tools(self, request: McpOperationRequest) -> McpOperationResult:
        async def run(context: _OperationContext) -> Mapping[str, object]:
            if request.arguments:
                _fail(McpClientErrorReason.RESULT_INVALID)
            items = await self._list_tools_data(context)
            return _bounded_document(
                {"tools": items},
                self._descriptor.limits.max_result_document_bytes,
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
            )

        return await self._run_operation(request, McpOperationKind.TOOLS_LIST, run)

    async def call_tool(
        self,
        request: McpOperationRequest,
        *,
        input_callback: InputCallback | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> McpOperationResult:
        task_creation_reserved = False

        def release_task_creation() -> None:
            nonlocal task_creation_reserved
            if task_creation_reserved:
                task_creation_reserved = False
                self._pending_task_creations -= 1

        async def run(
            context: _OperationContext,
        ) -> Mapping[str, object] | _OperationResponse:
            items = await self._list_tools_data(context)
            name = self._capability_name(request.capability_id, "tool:")
            item = _unique_by_name(items)[name]
            arguments = _bounded_document(
                request.arguments,
                self._descriptor.limits.max_tool_argument_bytes,
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
            )
            schema = item["input_schema"]
            _schema_validator(schema).validate(arguments)
            connected = self._connected
            if connected is None:
                _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)

            async def retry(
                input_responses: dict[str, Any] | None,
                request_state: str | None,
            ) -> Any:
                return await connected.session.call_tool(
                    name,
                    arguments,
                    progress_callback=(
                        self._handle_progress if progress_callback is not None else None
                    ),
                    input_responses=input_responses,
                    request_state=request_state,
                    allow_input_required=True,
                    allow_claimed=True,
                )

            result = await self._run_input_rounds(context, await retry(None, None), retry)
            signed_authority = context.receipt.signed_capability_authority
            signed_policy = (
                None if signed_authority is None else signed_authority["tools"].get(name)
            )
            output_schema = (
                item["output_schema"]
                if not isinstance(signed_policy, Mapping)
                else _json_value(signed_policy["output_schema"])
            )
            if type(result) is CreateTaskResult:
                self._ensure_operation_revision(context)
                started_at = asyncio.get_running_loop().time()
                maximum_duration = self._descriptor.limits.max_task_duration_ms
                task = _LiveTask(
                    task_id=result.task_id,
                    app_id=request.app_id,
                    capability_id=request.capability_id,
                    capability_revision=request.capability_revision,
                    connection_revision=self._connection_revision,
                    authorization_context_revision=self._authorization_context_revision,
                    output_schema=(output_schema if isinstance(output_schema, Mapping) else None),
                    signed_policy=(signed_policy if isinstance(signed_policy, Mapping) else None),
                    started_at=started_at,
                    deadline_at=started_at + maximum_duration / 1_000,
                )
                task_key = task.registry_key()
                registered = (
                    len(self._tasks) < self._descriptor.limits.max_active_tasks
                    and task_key not in self._tasks
                    and task_key not in self._task_tombstones
                )
                if registered:
                    release_task_creation()
                    self._tasks[task_key] = task
                try:
                    _operation_metadata(self._descriptor, context.receipt, result.meta)
                    validate_create_task_result(result)
                    if not registered:
                        _fail(McpClientErrorReason.TASK_INVALID)
                    server_duration = result.ttl_ms
                    duration_ms = (
                        maximum_duration
                        if server_duration is None
                        else min(maximum_duration, server_duration)
                    )
                    if duration_ms <= 0:
                        _fail(McpClientErrorReason.TASK_INVALID)
                    task.deadline_at = started_at + duration_ms / 1_000
                    task.output_schema = (
                        None
                        if output_schema is None
                        else _bounded_document(
                            output_schema,
                            self._descriptor.limits.max_result_document_bytes,
                            McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
                        )
                    )
                    task.signed_policy = signed_policy
                except BaseException as active_error:
                    return await self._recover_created_task(
                        context,
                        task,
                        active_error,
                        registered=registered,
                    )
                return _OperationResponse(
                    status=McpOperationStatus.TASK_STARTED,
                    data={},
                    task_handle=task.public_handle(),
                )
            _operation_metadata(self._descriptor, context.receipt, result.meta)
            normalized = _normalize_tool_result(result, self._descriptor.limits)
            # A declared output schema is an authority requirement, not a
            # hint.  A successful text-only response cannot satisfy it: fail
            # closed instead of allowing a missing structured result to bypass
            # validation.  Tools without a declared schema remain text-only.
            if output_schema is not None and not normalized["is_error"]:
                structured = normalized["structured_content"]
                if structured is None:
                    _fail(McpClientErrorReason.RESULT_INVALID)
                _schema_validator(output_schema).validate(structured)
            if isinstance(signed_policy, Mapping):
                normalized = _enforce_signed_tool_result(normalized, signed_policy)
            return normalized

        if (
            len(self._tasks) + self._pending_task_creations
            >= self._descriptor.limits.max_active_tasks
        ):

            async def reject_at_capacity(
                _context: _OperationContext,
            ) -> Mapping[str, object]:
                _fail(McpClientErrorReason.TASK_INVALID)

            return await self._run_operation(
                request,
                McpOperationKind.TOOL_CALL,
                reject_at_capacity,
                input_callback=input_callback,
                progress_callback=progress_callback,
            )
        self._pending_task_creations += 1
        task_creation_reserved = True
        return await self._run_operation(
            request,
            McpOperationKind.TOOL_CALL,
            run,
            input_callback=input_callback,
            progress_callback=progress_callback,
            ownership_released=release_task_creation,
        )

    async def invoke_plugin_tool(self, request: McpOperationRequest) -> McpOperationResult:
        """Run one signed plugin tool call through the one operation path.

        Plugin execution code outside this module never performs direct SDK
        operations; it submits plugin-scoped requests here so every wire call
        stays owned by the current client.
        """
        return await self.call_tool(request)

    async def snapshot_plugin_tools(self, request: McpOperationRequest) -> McpOperationResult:
        """List one plugin app's signed tools through the one operation path."""
        return await self.list_tools(request)

    async def list_prompts(self, request: McpOperationRequest) -> McpOperationResult:
        async def run(context: _OperationContext) -> Mapping[str, object]:
            if request.arguments:
                _fail(McpClientErrorReason.RESULT_INVALID)
            items = await self._list_prompts_data(context)
            return _bounded_document(
                {"prompts": items},
                self._descriptor.limits.max_result_document_bytes,
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
            )

        return await self._run_operation(request, McpOperationKind.PROMPTS_LIST, run)

    async def get_prompt(
        self,
        request: McpOperationRequest,
        *,
        input_callback: InputCallback | None = None,
    ) -> McpOperationResult:
        async def run(context: _OperationContext) -> Mapping[str, object]:
            items = await self._list_prompts_data(context)
            name = self._capability_name(request.capability_id, "prompt:")
            prompt = _unique_by_name(items)[name]
            arguments = _bounded_document(
                request.arguments,
                self._descriptor.limits.max_tool_argument_bytes,
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
            )
            if not isinstance(arguments, dict) or any(
                type(value) is not str for value in arguments.values()
            ):
                _fail(McpClientErrorReason.RESULT_INVALID)
            definitions = {
                argument["name"]: argument
                for argument in prompt["arguments"]  # type: ignore[union-attr]
            }
            if not set(arguments) <= set(definitions) or any(
                definition["required"] and field not in arguments
                for field, definition in definitions.items()
            ):
                _fail(McpClientErrorReason.RESULT_INVALID)
            connected = self._connected
            if connected is None:
                _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)

            async def retry(
                input_responses: dict[str, Any] | None,
                request_state: str | None,
            ) -> Any:
                return await connected.session.get_prompt(
                    name,
                    arguments,
                    input_responses=input_responses,
                    request_state=request_state,
                    allow_input_required=True,
                )

            result = await self._run_input_rounds(context, await retry(None, None), retry)
            _operation_metadata(self._descriptor, context.receipt, result.meta)
            return _normalize_prompt_result(result, self._descriptor.limits)

        return await self._run_operation(
            request,
            McpOperationKind.PROMPT_GET,
            run,
            input_callback=input_callback,
        )

    async def list_resources(self, request: McpOperationRequest) -> McpOperationResult:
        async def run(context: _OperationContext) -> Mapping[str, object]:
            if request.arguments:
                _fail(McpClientErrorReason.RESULT_INVALID)
            items = await self._list_resources_data(context)
            return _bounded_document(
                {"resources": items},
                self._descriptor.limits.max_result_document_bytes,
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
            )

        return await self._run_operation(request, McpOperationKind.RESOURCES_LIST, run)

    async def list_resource_templates(
        self,
        request: McpOperationRequest,
    ) -> McpOperationResult:
        async def run(context: _OperationContext) -> Mapping[str, object]:
            if request.arguments:
                _fail(McpClientErrorReason.RESULT_INVALID)
            items = await self._list_templates_data(context)
            return _bounded_document(
                {"resource_templates": items},
                self._descriptor.limits.max_result_document_bytes,
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
            )

        return await self._run_operation(
            request,
            McpOperationKind.RESOURCE_TEMPLATES_LIST,
            run,
        )

    async def read_resource(
        self,
        request: McpOperationRequest,
        *,
        input_callback: InputCallback | None = None,
    ) -> McpOperationResult:
        async def run(context: _OperationContext) -> Mapping[str, object]:
            name = self._capability_name(request.capability_id, "resource:")
            observed_authority = context.receipt.resources.get(name)
            is_template = False
            if (
                isinstance(observed_authority, Mapping)
                and type(observed_authority.get("uri")) is str
            ):
                if request.arguments:
                    _fail(McpClientErrorReason.RESULT_INVALID)
                uri = observed_authority["uri"]
            else:
                observed_authority = context.receipt.resource_templates.get(name)
                if (
                    not isinstance(observed_authority, Mapping)
                    or type(observed_authority.get("uri_template")) is not str
                ):
                    _fail(McpClientErrorReason.RESULT_INVALID)
                is_template = True
                arguments = _json_value(request.arguments)
                if (
                    not isinstance(arguments, dict)
                    or set(arguments) != {"uri"}
                    or type(arguments["uri"]) is not str
                    or not _matches_uri_template(
                        observed_authority["uri_template"],
                        arguments["uri"],
                        self._descriptor.limits,
                    )
                ):
                    _fail(McpClientErrorReason.RESULT_INVALID)
                uri = _bounded_text(
                    arguments["uri"],
                    self._descriptor.limits.max_result_document_bytes,
                )
            _text_mime(observed_authority.get("mime_type"))
            signed_authority = context.receipt.signed_capability_authority
            signed_policy: Mapping[str, Any] | None = None
            if signed_authority is not None:
                group = (
                    signed_authority["resource_templates"]
                    if is_template
                    else signed_authority["resources"]
                )
                candidate = group.get(name)
                if not isinstance(candidate, Mapping):
                    _fail(McpClientErrorReason.CAPABILITY_DRIFT)
                signed_policy = candidate
            connected = self._connected
            if connected is None:
                _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)

            async def retry(
                input_responses: dict[str, Any] | None,
                request_state: str | None,
            ) -> Any:
                return await connected.session.read_resource(
                    uri,
                    input_responses=input_responses,
                    request_state=request_state,
                    allow_input_required=True,
                )

            result = await self._run_input_rounds(context, await retry(None, None), retry)
            _operation_metadata(self._descriptor, context.receipt, result.meta)
            return _normalize_read_result(
                result,
                self._descriptor.limits,
                expected_uri=uri,
                expected_mime_type=(None if signed_policy is None else signed_policy["mime_type"]),
                max_bytes=None if signed_policy is None else signed_policy["max_bytes"],
            )

        return await self._run_operation(
            request,
            McpOperationKind.RESOURCE_READ,
            run,
            input_callback=input_callback,
        )

    async def listen(
        self,
        request: McpOperationRequest,
        *,
        event_callback: ProgressCallback,
    ) -> McpOperationResult:
        async def run(context: _OperationContext) -> Mapping[str, object]:
            arguments = _json_value(request.arguments)
            if not isinstance(arguments, dict):
                _fail(McpClientErrorReason.RESULT_INVALID)
            allowed = {
                "tools_list_changed",
                "prompts_list_changed",
                "resources_list_changed",
                "resource_subscriptions",
                "max_reconnects",
            }
            if not set(arguments) <= allowed:
                _fail(McpClientErrorReason.RESULT_INVALID)
            tools_changed = arguments.get("tools_list_changed", False)
            prompts_changed = arguments.get("prompts_list_changed", False)
            resources_changed = arguments.get("resources_list_changed", False)
            if any(
                type(value) is not bool
                for value in (tools_changed, prompts_changed, resources_changed)
            ):
                _fail(McpClientErrorReason.RESULT_INVALID)
            raw_subscriptions = arguments.get("resource_subscriptions", [])
            if (
                type(raw_subscriptions) is not list
                or any(type(uri) is not str or not uri for uri in raw_subscriptions)
                or len(raw_subscriptions) > self._descriptor.limits.max_list_items
            ):
                _fail(McpClientErrorReason.RESULT_INVALID)
            resource_subscriptions = tuple(
                _bounded_text(uri, self._descriptor.limits.max_result_document_bytes)
                for uri in raw_subscriptions
            )
            if len(resource_subscriptions) != len(set(resource_subscriptions)):
                _fail(McpClientErrorReason.RESULT_INVALID)
            signed_authority = context.receipt.signed_capability_authority
            if resource_subscriptions and signed_authority is None:
                _fail(McpClientErrorReason.RESULT_INVALID)
            resource_authority = {} if signed_authority is None else signed_authority["resources"]
            template_authority = (
                {} if signed_authority is None else signed_authority["resource_templates"]
            )
            exact_resources = {
                authority.get("uri")
                for authority in resource_authority.values()
                if isinstance(authority, Mapping) and type(authority.get("uri")) is str
            }
            resource_templates = tuple(
                authority["uri_template"]
                for authority in template_authority.values()
                if isinstance(authority, Mapping) and type(authority.get("uri_template")) is str
            )
            for uri in resource_subscriptions:
                if canonical_resource_uri(uri) != uri or (
                    uri not in exact_resources
                    and not any(
                        _matches_uri_template(template, uri, self._descriptor.limits)
                        for template in resource_templates
                    )
                ):
                    _fail(McpClientErrorReason.RESULT_INVALID)
            if not (
                tools_changed or prompts_changed or resources_changed or resource_subscriptions
            ):
                _fail(McpClientErrorReason.RESULT_INVALID)
            max_reconnects = arguments.get("max_reconnects", 0)
            if (
                type(max_reconnects) is not int
                or max_reconnects < 0
                or max_reconnects > self._descriptor.limits.max_list_pages
            ):
                _fail(McpClientErrorReason.RESULT_INVALID)
            connected = self._connected
            if connected is None:
                _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
            events = 0
            reconnects = 0

            async def deliver(data: Mapping[str, object]) -> None:
                event = _result(
                    request,
                    status=McpOperationStatus.PROGRESS,
                    data=data,
                )
                try:
                    await event_callback(event)
                except BaseException as error:
                    context.callback_error = error
                    raise
                self._ensure_operation_revision(context)

            while True:
                try:
                    async with connected.listen(
                        tools_list_changed=tools_changed,
                        prompts_list_changed=prompts_changed,
                        resources_list_changed=resources_changed,
                        resource_subscriptions=resource_subscriptions,
                    ) as subscription:
                        async for raw_event in subscription:
                            self._ensure_operation_revision(context)
                            events += 1
                            if events > self._descriptor.limits.max_notification_events:
                                _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
                            event_data = normalize_subscription_event(raw_event)
                            if (
                                event_data.get("kind") == "resource_updated"
                                and event_data.get("uri") not in resource_subscriptions
                            ):
                                _fail(McpClientErrorReason.RESULT_INVALID)
                            await deliver(event_data)
                    if context.callback_error is not None:
                        raise context.callback_error
                    break
                except SubscriptionLost:
                    if context.callback_error is not None:
                        raise context.callback_error
                    if reconnects >= max_reconnects:
                        raise
                    reconnects += 1
                    cache = self._cache
                    if cache is None:
                        _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
                    await cache.store.clear()
                    await deliver({"kind": "subscription_gap", "reconnect": reconnects})
            return {"events": events, "reconnects": reconnects}

        return await self._run_operation(
            request,
            McpOperationKind.SUBSCRIPTIONS_LISTEN,
            run,
            progress_callback=event_callback,
        )

    async def _cancel_untrusted_created_task(
        self,
        context: _OperationContext,
        task: _LiveTask,
    ) -> bool:
        connected = self._connected
        if connected is None:
            return False
        timeout_seconds = self._descriptor.limits.max_shutdown_duration_ms / 1_000
        raw_request = CancelTaskRequest(params=CancelTaskParams(task_id=task.task_id))
        try:
            validate_task_request(raw_request)
            async with asyncio.timeout(timeout_seconds):
                result = await connected.session.send_request(
                    raw_request,
                    result_type=CancelTaskResult,
                    request_read_timeout_seconds=timeout_seconds,
                )
            validate_task_result(raw_request.method, result)
            _operation_metadata(self._descriptor, context.receipt, result.meta)
        except BaseException:
            return False
        return True

    async def _recover_created_task(
        self,
        context: _OperationContext,
        task: _LiveTask,
        active_error: BaseException,
        *,
        registered: bool,
    ) -> _OperationResponse:
        cleanup = asyncio.create_task(
            self._cancel_untrusted_created_task(context, task),
            name=f"omega-created-task-cleanup:{task.task_id}",
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._descriptor.limits.max_shutdown_duration_ms / 1_000
        caller_cancellation: BaseException | None = None
        cleaned = False
        while loop.time() < deadline and not cleanup.done():
            try:
                done, _pending = await asyncio.wait(
                    {cleanup},
                    timeout=max(0.0, deadline - loop.time()),
                )
            except BaseException as error:
                if caller_cancellation is None:
                    caller_cancellation = error
                continue
            if done:
                break
        if cleanup.done():
            try:
                cleaned = cleanup.result()
            except BaseException as error:
                if caller_cancellation is None:
                    caller_cancellation = error
        else:
            cleanup.cancel()
            self._cleanup_recovery_required = True
            if not registered and self._tasks.get(task.registry_key()) is None:
                while not cleanup.done():
                    try:
                        await asyncio.wait({cleanup})
                    except BaseException as error:
                        if caller_cancellation is None:
                            caller_cancellation = error
                try:
                    cleaned = cleanup.result()
                except BaseException as error:
                    if caller_cancellation is None:
                        caller_cancellation = error
            else:
                cleanup.add_done_callback(self._consume_owned_task)
        if cleaned:
            task_key = task.registry_key()
            if registered and self._tasks.get(task_key) is task:
                self._tasks.pop(task_key, None)
            elif not registered:
                existing = self._tasks.get(task_key)
                if existing is not None:
                    self._evict_task(existing)
            if caller_cancellation is not None:
                raise caller_cancellation
            raise active_error
        if caller_cancellation is not None:
            raise caller_cancellation
        if isinstance(active_error, asyncio.CancelledError) or not isinstance(
            active_error,
            Exception,
        ):
            raise active_error
        if not registered:
            existing = self._tasks.get(task.registry_key())
            if existing is None:
                _fail(McpClientErrorReason.CLEANUP_REQUIRED)
            return _OperationResponse(
                status=McpOperationStatus.TASK_RECOVERY_REQUIRED,
                data={},
                task_handle=existing.public_handle(),
            )
        return _OperationResponse(
            status=McpOperationStatus.TASK_RECOVERY_REQUIRED,
            data={},
            task_handle=task.public_handle(),
        )

    def _bound_task(self, request: McpOperationRequest) -> _LiveTask:
        task_id = request.task_id
        if task_id is None:
            _fail(McpClientErrorReason.TASK_INVALID)
        task = self._tasks.get(
            (
                task_id,
                request.app_id,
                request.capability_id,
                request.capability_revision,
                self._connection_revision,
                self._authorization_context_revision,
            )
        )
        if task is None or task.terminal:
            _fail(McpClientErrorReason.TASK_INVALID)
        if (
            task.task_id != task_id
            or task.app_id != request.app_id
            or task.capability_id != request.capability_id
            or task.capability_revision != request.capability_revision
            or task.connection_revision != self._connection_revision
            or task.authorization_context_revision != self._authorization_context_revision
        ):
            _fail(McpClientErrorReason.TASK_INVALID)
        if asyncio.get_running_loop().time() >= task.deadline_at:
            self._evict_task(task)
            _fail(McpClientErrorReason.TASK_INVALID)
        return task

    def _evict_task(self, task: _LiveTask) -> None:
        task.terminal = True
        task.outstanding_input_requests.clear()
        task.input_reservations.clear()
        key = task.registry_key()
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        self._task_tombstones.pop(key, None)
        if len(self._task_tombstones) >= self._descriptor.limits.max_task_tombstones:
            self._task_tombstones.pop(next(iter(self._task_tombstones)))
        self._task_tombstones[key] = None

    async def get_task(self, request: McpOperationRequest) -> McpOperationResult:
        async def run(context: _OperationContext) -> Mapping[str, object] | _OperationResponse:
            if request.arguments:
                _fail(McpClientErrorReason.RESULT_INVALID)
            task = self._bound_task(request)
            now = asyncio.get_running_loop().time()
            if now < task.next_poll_at:
                _fail(McpClientErrorReason.TASK_INVALID)
            task.polls += 1
            if task.polls > self._descriptor.limits.max_task_polls:
                _fail(McpClientErrorReason.TASK_INVALID)
            raw_request = GetTaskRequest(params=GetTaskParams(task_id=task.task_id))
            validate_task_request(raw_request)
            connected = self._connected
            if connected is None:
                _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
            result = await connected.session.send_request(
                raw_request,
                result_type=GetTaskResult,
            )
            if asyncio.get_running_loop().time() >= task.deadline_at:
                self._evict_task(task)
                _fail(McpClientErrorReason.TASK_INVALID)
            validate_task_result(raw_request.method, result)
            _operation_metadata(self._descriptor, context.receipt, result.meta)
            if result.task_id != task.task_id:
                _fail(McpClientErrorReason.TASK_INVALID)
            poll_interval_ms = result.poll_interval_ms or 0
            if poll_interval_ms > self._descriptor.limits.max_task_duration_ms:
                self._evict_task(task)
                _fail(McpClientErrorReason.TASK_INVALID)
            task.next_poll_at = now + poll_interval_ms / 1_000
            if is_terminal_task_status(result.status):
                terminal_result = _task_terminal_tool_result(result)
                try:
                    normalized = _normalize_tool_result(
                        terminal_result,
                        self._descriptor.limits,
                    )
                    if task.output_schema is not None and not normalized["is_error"]:
                        _schema_validator(task.output_schema).validate(
                            normalized["structured_content"]
                        )
                    if task.signed_policy is not None:
                        normalized = _enforce_signed_tool_result(
                            normalized,
                            task.signed_policy,
                        )
                finally:
                    self._evict_task(task)
                return _OperationResponse(
                    status=McpOperationStatus.COMPLETED,
                    data=normalized,
                )
            if result.status == "input_required":
                input_requests = result.input_requests
                if (
                    not isinstance(input_requests, dict)
                    or not input_requests
                    or len(input_requests) > self._descriptor.limits.max_input_fields
                ):
                    _fail(McpClientErrorReason.TASK_INVALID)
                normalized_requests: dict[str, object] = {}
                next_outstanding: dict[str, _TaskInputAuthority] = {}
                next_generation = task.input_generation + 1
                for request_id, embedded in input_requests.items():
                    if (
                        type(request_id) is not str
                        or not request_id
                        or len(request_id) > 256
                        or unicodedata.normalize("NFC", request_id) != request_id
                        or any(ord(character) < 0x20 for character in request_id)
                    ):
                        _fail(McpClientErrorReason.TASK_INVALID)
                    if request_id in task.used_input_request_ids:
                        _fail(McpClientErrorReason.TASK_INVALID)
                    try:
                        parsed_request = parse_task_input_request(embedded)
                    except (TypeError, ValueError, ValidationError):
                        _fail(McpClientErrorReason.TASK_INVALID)
                    request_document = _bounded_document(
                        parsed_request.model_dump(by_alias=True, exclude_none=True),
                        self._descriptor.limits.max_result_document_bytes,
                        McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
                    )
                    schema = parsed_request.params.requested_schema
                    validator = _schema_validator(schema)
                    if type(schema) is not dict or schema.get("type") != "object":
                        _fail(McpClientErrorReason.TASK_INVALID)
                    properties = schema.get("properties")
                    if (
                        type(properties) is not dict
                        or not properties
                        or len(properties) > self._descriptor.limits.max_input_fields
                        or any(type(field) is not str or not field for field in properties)
                    ):
                        _fail(McpClientErrorReason.TASK_INVALID)
                    _validate_elicitation_required_closure(schema, frozenset(properties))
                    fingerprint = sha256_hex(canonical_bytes(request_document))
                    existing = task.outstanding_input_requests.get(request_id)
                    next_outstanding[request_id] = (
                        existing
                        if existing is not None and existing.fingerprint == fingerprint
                        else _TaskInputAuthority(
                            request=parsed_request,
                            generation=next_generation,
                            fingerprint=fingerprint,
                            validator=validator,
                        )
                    )
                    normalized_requests[request_id] = request_document
                task.input_generation = next_generation
                task.outstanding_input_requests = next_outstanding
                task_state = normalized_task(result)
                task_state["input_requests"] = normalized_requests
            else:
                task.outstanding_input_requests.clear()
                task_state = normalized_task(result)
            task_document = _bounded_document(
                {"task": task_state},
                self._descriptor.limits.max_result_document_bytes,
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
            )
            return _OperationResponse(
                status=McpOperationStatus.COMPLETED,
                data=task_document,
            )

        return await self._run_operation(request, McpOperationKind.TASK_GET, run)

    async def update_task(self, request: McpOperationRequest) -> McpOperationResult:
        async def run(context: _OperationContext) -> _OperationResponse:
            arguments = _bounded_document(
                request.arguments,
                self._descriptor.limits.max_tool_argument_bytes,
                McpClientErrorReason.RESULT_LIMIT_EXCEEDED,
            )
            if not isinstance(arguments, dict) or set(arguments) != {"inputResponses"}:
                _fail(McpClientErrorReason.RESULT_INVALID)
            responses = arguments["inputResponses"]
            if not isinstance(responses, dict) or not responses:
                _fail(McpClientErrorReason.RESULT_INVALID)
            task = self._bound_task(request)
            outstanding = task.outstanding_input_requests
            if not set(responses) <= set(outstanding):
                _fail(McpClientErrorReason.TASK_INVALID)
            normalized_responses: dict[str, object] = {}
            captured_authorities: dict[str, _TaskInputAuthority] = {}
            try:
                for request_id, response in responses.items():
                    authority = outstanding[request_id]
                    parsed_response = parse_task_input_response(response)
                    response_document = _bounded_document(
                        parsed_response.model_dump(by_alias=True, exclude_none=True),
                        self._descriptor.limits.max_tool_argument_bytes,
                        McpClientErrorReason.INPUT_LIMIT_EXCEEDED,
                    )
                    if parsed_response.action == "accept":
                        authority.validator.validate(parsed_response.content)
                    normalized_responses[request_id] = response_document
                    captured_authorities[request_id] = authority
            except (jsonschema.SchemaError, jsonschema.ValidationError, TypeError, ValueError):
                _fail(McpClientErrorReason.TASK_INVALID)
            next_update = task.updates + 1
            if next_update > self._descriptor.limits.max_task_updates:
                _fail(McpClientErrorReason.TASK_INVALID)
            raw_request = UpdateTaskRequest(
                params=UpdateTaskParams(
                    task_id=task.task_id,
                    input_responses=normalized_responses,
                )
            )
            validate_task_request(raw_request)
            connected = self._connected
            if connected is None:
                _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
            reservation_keys = tuple(
                (request_id, authority.generation, authority.fingerprint)
                for request_id, authority in captured_authorities.items()
            )
            if any(key in task.input_reservations for key in reservation_keys):
                _fail(McpClientErrorReason.TASK_INVALID)
            reservation_owner = object()
            for key in reservation_keys:
                task.input_reservations[key] = reservation_owner
            task.updates = next_update
            try:
                result = await connected.session.send_request(
                    raw_request,
                    result_type=UpdateTaskResult,
                )
                if asyncio.get_running_loop().time() >= task.deadline_at:
                    self._evict_task(task)
                    _fail(McpClientErrorReason.TASK_INVALID)
                validate_task_result(raw_request.method, result)
                _operation_metadata(self._descriptor, context.receipt, result.meta)
                for request_id, captured in captured_authorities.items():
                    current = task.outstanding_input_requests.get(request_id)
                    if (
                        current is not None
                        and current.generation == captured.generation
                        and current.fingerprint == captured.fingerprint
                    ):
                        task.used_input_request_ids.add(request_id)
                        task.outstanding_input_requests.pop(request_id)
                return _OperationResponse(
                    status=McpOperationStatus.TASK_UPDATED,
                    data={},
                    task_handle=task.public_handle(),
                )
            finally:
                for key in reservation_keys:
                    if task.input_reservations.get(key) is reservation_owner:
                        task.input_reservations.pop(key)

        return await self._run_operation(request, McpOperationKind.TASK_UPDATE, run)

    async def cancel_task(self, request: McpOperationRequest) -> McpOperationResult:
        async def run(context: _OperationContext) -> _OperationResponse:
            if request.arguments:
                _fail(McpClientErrorReason.RESULT_INVALID)
            task = self._bound_task(request)
            raw_request = CancelTaskRequest(params=CancelTaskParams(task_id=task.task_id))
            validate_task_request(raw_request)
            connected = self._connected
            if connected is None:
                _fail(McpClientErrorReason.TRANSPORT_UNAVAILABLE)
            result = await connected.session.send_request(
                raw_request,
                result_type=CancelTaskResult,
            )
            validate_task_result(raw_request.method, result)
            _operation_metadata(self._descriptor, context.receipt, result.meta)
            handle = task.public_handle()
            self._evict_task(task)
            return _OperationResponse(
                status=McpOperationStatus.TASK_UPDATED,
                data={},
                task_handle=handle,
            )

        return await self._run_operation(request, McpOperationKind.TASK_CANCEL, run)

    async def _handle_progress(
        self,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        context = _ACTIVE_OPERATION.get()
        if context is None or context.progress_callback is None:
            return
        self._ensure_operation_revision(context)
        if (
            type(progress) not in {int, float}
            or not math.isfinite(progress)
            or progress < 0
            or total is not None
            and (type(total) not in {int, float} or not math.isfinite(total) or total < progress)
        ):
            _fail(McpClientErrorReason.RESULT_INVALID)
        context.progress_events += 1
        if context.progress_events > self._descriptor.limits.max_progress_events:
            _fail(McpClientErrorReason.PROGRESS_LIMIT_EXCEEDED)
        event = _result(
            context.request,
            status=McpOperationStatus.PROGRESS,
            data={
                "progress": float(progress),
                "total": None if total is None else float(total),
                "message": _optional_text(
                    message,
                    self._descriptor.limits.max_result_document_bytes,
                ),
            },
        )
        try:
            await context.progress_callback(event)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            context.callback_error = error
            raise
        self._ensure_operation_revision(context)

    async def _handle_elicitation(self, _sdk_context: Any, _params: Any) -> Any:
        return types.ErrorData(code=types.INVALID_REQUEST, message="input unavailable")

    async def _run_owner(
        self,
        sdk_client_builder: _SdkClientBuilder,
        discovery_source: DiscoverySource,
        extensions: tuple[ClientExtension, ...],
        cache: CacheConfig,
        ready: asyncio.Future[_StartOutcome],
        close_requested: asyncio.Event,
    ) -> BaseException | None:
        active_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        entered = False
        try:
            context = sdk_client_builder(
                mode=_sdk_mode_for_descriptor(self._descriptor),
                client_info=_client_info(),
                extensions=extensions,
                cache=cache,
            )
            context.elicitation_callback = self._handle_elicitation
            try:
                async with context as connected:
                    entered = True
                    try:
                        receipt = await _discover_receipt(
                            self._descriptor,
                            connected,
                            discovery_source,
                        )
                        if not ready.done():
                            ready.set_result(
                                _StartOutcome(
                                    connected=connected,
                                    receipt=receipt,
                                    error=None,
                                )
                            )
                        await close_requested.wait()
                    except BaseException as error:
                        active_error = error
                        raise
            except BaseException as error:
                if not entered:
                    active_error = error
                elif active_error is None or error is not active_error:
                    cleanup_error = error
        except BaseException as error:
            active_error = error
        finally:
            outcome_error = active_error if active_error is not None else cleanup_error
            if not ready.done():
                ready.set_result(
                    _StartOutcome(
                        connected=None,
                        receipt=None,
                        error=outcome_error,
                    )
                )
        return active_error if active_error is not None else cleanup_error

    async def close(self) -> None:
        if self._state is _LifecycleState.CLOSED:
            return
        if self._state is _LifecycleState.NEW:
            self._state = _LifecycleState.CLOSED
            return
        loop = asyncio.get_running_loop()
        shutdown_deadline = loop.time() + self._descriptor.limits.max_shutdown_duration_ms / 1_000
        owner = self._owner_task
        close_requested = self._close_requested
        if owner is None or close_requested is None:
            self._retire()
            _fail(McpClientErrorReason.CLEANUP_REQUIRED)
        if asyncio.current_task() in self._active_operations:
            _fail(McpClientErrorReason.CLEANUP_REQUIRED)
        self._state = _LifecycleState.CLOSING
        self._receipt = None
        close_requested.set()
        active_error: BaseException | None = None
        operations_joined, operation_cancellation = await self._cancel_and_join_operations(
            deadline=shutdown_deadline,
        )
        if operation_cancellation is not None:
            active_error = operation_cancellation
        if not operations_joined:
            self._cleanup_recovery_required = True
        owner_joined, owner_error, owner_cancellation = await self._join_owner(
            owner,
            deadline=shutdown_deadline,
        )
        if owner_cancellation is not None and active_error is None:
            active_error = owner_cancellation
        if not owner_joined:
            self._cleanup_recovery_required = True
            owner.cancel()
            owner.add_done_callback(self._consume_owned_task)
        if operations_joined:
            self._retire()
        if active_error is not None:
            raise active_error
        if not operations_joined or not owner_joined:
            _fail(McpClientErrorReason.CLEANUP_REQUIRED)
        if owner_error is not None:
            raise _public_client_error(self._descriptor, owner_error)

    async def _cancel_and_join_operations(
        self,
        *,
        deadline: float,
    ) -> tuple[bool, BaseException | None]:
        current = asyncio.current_task()
        operations = tuple(
            operation for operation in self._active_operations if operation is not current
        )
        for operation in operations:
            if not operation.done():
                operation.cancel()
        all_joined = True
        caller_cancellation: BaseException | None = None
        for operation in operations:
            joined, _result, cancellation = await self._join_owned_task(
                operation,
                deadline=deadline,
            )
            if cancellation is not None and caller_cancellation is None:
                caller_cancellation = cancellation
            if joined:
                self._release_operation_ownership(operation)
            else:
                all_joined = False
        return all_joined, caller_cancellation

    @staticmethod
    async def _join_owner(
        owner: asyncio.Task[BaseException | None],
        *,
        deadline: float,
    ) -> tuple[bool, BaseException | None, BaseException | None]:
        joined, result, caller_cancellation = await CurrentMcpClient._join_owned_task(
            owner,
            deadline=deadline,
        )
        return joined, result if isinstance(result, BaseException) else None, caller_cancellation

    @staticmethod
    async def _join_owned_task(
        owned: asyncio.Task[Any],
        *,
        deadline: float,
    ) -> tuple[bool, object | BaseException | None, BaseException | None]:
        loop = asyncio.get_running_loop()
        caller_cancellation: BaseException | None = None
        while loop.time() < deadline and not owned.done():
            try:
                done, _pending = await asyncio.wait(
                    {owned},
                    timeout=max(0.0, deadline - loop.time()),
                )
            except BaseException as error:
                if caller_cancellation is None:
                    caller_cancellation = error
                continue
            if done:
                break
        if not owned.done():
            return False, None, caller_cancellation
        try:
            result: object | BaseException | None = owned.result()
        except BaseException as error:
            result = error
        return True, result, caller_cancellation

    @staticmethod
    def _consume_owned_task(owned: asyncio.Future[object]) -> None:
        try:
            owned.exception()
        except BaseException:
            pass

    def _release_operation_ownership(self, operation: asyncio.Task[Any]) -> None:
        if operation not in self._active_operations:
            return
        ownership_released = self._active_operations.pop(operation)
        self._consume_owned_task(operation)
        if ownership_released is not None:
            ownership_released()

    def _retire(self) -> None:
        self._receipt = None
        self._connected = None
        self._owner_task = None
        self._close_requested = None
        self._active_operations.clear()
        self._cache = None
        self._tasks.clear()
        self._task_tombstones.clear()
        self._pending_task_creations = 0
        self._state = _LifecycleState.CLOSED


__all__ = [
    "CurrentMcpClient",
    "McpProviderDriftStatus",
    "McpProviderRecovery",
    "SdkClientFactory",
]
