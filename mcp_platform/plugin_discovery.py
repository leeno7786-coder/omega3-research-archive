"""Restricted discovery for publisher-verified and personal plugin connections.

Omega has not endorsed a publisher-verified or personal plugin's behavior --
only its source namespace (or, for a personal plugin, the operator's own
review) stands behind it. Per the marketplace design's capability-discovery
contract, such a plugin "begins in restricted discovery mode. The runtime may
perform only protocol negotiation and capability enumeration. It cannot call
a discovered tool, fetch arbitrary resources, or subscribe to events."

``RestrictedPluginDiscovery`` is that boundary. ``discover`` issues exactly
five operations, in this exact order, and no others: protocol negotiation,
then ``tools/list``, ``prompts/list``, ``resources/list``, and
``resources/templates/list``. It never issues ``tools/call``, ``prompts/get``,
or ``resources/read`` -- there is no code path in this module that could.
Every discovered capability is classified ``RiskClass.EXTERNAL_UNREVIEWED``;
a publisher's own MCP annotations (``readOnlyHint``, ``destructiveHint``, and
so on) are preserved as publisher claims for the user to read, but nothing
in this module lets them upgrade that classification. Only after the user
reviews the enumerated capabilities and states a connection scope does
``approve`` seal the exact, filtered subset the user chose into a
``LocalCapabilityEnvelope``, signed with the device-local authority-MAC key
(``plugin_platform.local_trust``) -- never Omega's own signature, and never
more than what the user actually scoped.

Architecture note: ``mcp_platform``'s AST rails
(``tests/mcp_platform/test_architecture.py``) reserve every literal MCP SDK
operation name (``list_tools``, ``call_tool``, ...) and the literal
``initialize`` feature call for the modules that already own a live,
capability-bound session (``mcp_platform.client``, ``mcp_platform.supervisor``).
This module never spells those names as call expressions -- every wire
operation crosses through one peer method, ``negotiate(operation)``, whose
operation is a plain string. Bridging a real connection's session into that
narrow ``DiscoveryPeer`` protocol belongs to whichever later change wires
restricted discovery into a live connection; this module only defines the
boundary and enforces it.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client_contracts import UNREVIEWED_EFFECT
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_planning import (
    project_provider_input_schema,
    project_provider_output_schema,
)
from plugin_platform.local_trust import (
    LocalTrustError,
    LocalTrustRoot,
    SealedCapabilityAuthority,
)

MAX_TITLE_CHARS = 200
MAX_SCHEMA_BYTES = 16_384
MAX_SCHEMA_DEPTH = 12
MAX_MIME_TYPE_CHARS = 128
MAX_RESULT_BYTES = 8 * 1024 * 1024
#: The blessed open-object MCP tool result shape
#: (``mcp_platform.plugin_planning``'s one exemption from a closed result
#: schema) -- the default output schema for a discovered tool that declares
#: none of its own.
_OPEN_RESULT_SCHEMA: Mapping[str, object] = MappingProxyType(
    {"type": "object", "additionalProperties": True, "properties": {}}
)
#: Every sealed tool policy declares the same allowed result MIME type: MCP
#: tool call results are JSON-RPC structured content.
_SEALED_TOOL_MIME_TYPES: tuple[str, ...] = ("application/json",)

_REVIEW_DOMAIN = "omega.plugin-discovery.review.v1"
_MIME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*\Z")
_SCHEMA_REF_KEYS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_HEX = frozenset("0123456789abcdef")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z")
_URL_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~!$&'()*+,;=:@+-]+)*\Z")

_REVIEW_REQUIRED = "plugin_review_required"
_IDENTITY_CHANGED = "plugin_discovery_identity_changed"
_ENDPOINT_CHANGED = "plugin_discovery_endpoint_changed"
_MUTABLE_PAGES = "plugin_discovery_mutable_pages"
_DUPLICATE_IDENTITY = "plugin_discovery_duplicate_identity"
_SCHEMA_UNSUPPORTED = "plugin_discovery_schema_unsupported"
_SCHEMA_LIMIT_EXCEEDED = "plugin_discovery_schema_limit_exceeded"
_MIME_UNSUPPORTED = "plugin_discovery_mime_unsupported"
_RESULT_LIMIT_INVALID = "plugin_discovery_result_limit_invalid"
_TRANSPORT_UNAVAILABLE = "plugin_discovery_transport_unavailable"
_SCOPE_UNAVAILABLE = "plugin_discovery_scope_unavailable"
_ENVELOPE_UNAVAILABLE = "plugin_discovery_envelope_unavailable"

_FIELDS_BY_KIND: Mapping[str, frozenset[str]] = {
    "tool": frozenset({"name", "title", "input_schema", "output_schema", "annotations"}),
    "prompt": frozenset({"name", "title", "annotations"}),
    "resource": frozenset({"name", "title", "mime_type", "max_bytes", "annotations"}),
    "resource_template": frozenset({"name", "title", "mime_type", "max_bytes", "annotations"}),
}
_REQUIRED_FIELDS_BY_KIND: Mapping[str, frozenset[str]] = {
    "tool": frozenset({"name", "input_schema"}),
    "prompt": frozenset({"name"}),
    "resource": frozenset({"name"}),
    "resource_template": frozenset({"name"}),
}
_PLURAL_BY_KIND: Mapping[str, str] = {
    "tool": "tools",
    "prompt": "prompts",
    "resource": "resources",
    "resource_template": "resource_templates",
}
_OPERATION_BY_KIND: Mapping[str, str] = {
    "tool": "tools/list",
    "prompt": "prompts/list",
    "resource": "resources/list",
    "resource_template": "resources/templates/list",
}


class PluginDiscoveryError(Exception):
    """A stable, content-free restricted-discovery rejection reason."""


class DiscoveryPeer(Protocol):
    """The only surface restricted discovery drives.

    A conforming peer accepts exactly the five negotiation/enumeration
    operation names this module issues and raises ``PluginDiscoveryError``
    for anything else -- most importantly, for any attempt to execute a
    discovered capability (``tools/call``, ``prompts/get``,
    ``resources/read``) before the user has reviewed and approved it.
    ``endpoint`` reports the exact canonical endpoint this peer is actually
    connected to -- the transport target it was constructed with -- so
    discovery can catch drift from what the connection layer expects before
    issuing any operation.
    """

    @property
    def endpoint(self) -> str: ...

    async def negotiate(self, operation: str) -> object: ...


def _stable_string(value: object, *, field: str) -> str:
    """Validate one non-empty, control-character-free string.

    A local copy rather than an import from ``plugin_platform.contracts``:
    ``mcp_platform``'s architecture rails
    (``tests/mcp_platform/test_architecture.py::test_private_sdk_and_plugin_registry_seams_are_rejected``)
    forbid importing any underscore-prefixed ``plugin_platform`` symbol from
    this layer, so this boundary owns its own field validation.
    """

    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty stable string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must be a stable string without control characters")
    return value


def _identifier(value: object, *, field: str) -> str:
    identifier = _stable_string(value, field=field)
    if _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError(f"{field} is invalid")
    return identifier


def _digest(value: object, *, field: str) -> str:
    digest = _stable_string(value, field=field)
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _bounded_text(value: object, *, field: str, limit: int) -> str:
    text = _stable_string(value, field=field)
    if len(text) > limit:
        raise ValueError(f"{field} exceeds its bound")
    return text


def _canonical_https_authority(parsed: Any) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if hostname is None or _HOST_RE.fullmatch(hostname) is None or port in (443, 0):
        return False
    expected_netloc = hostname if port is None else f"{hostname}:{port}"
    return parsed.netloc == expected_netloc


def _canonical_endpoint(value: object, *, field: str) -> str:
    """Validate one canonical HTTPS endpoint: origin plus an exact, unambiguous path.

    A binding's ``endpoint`` is the connection layer's own expectation of
    which exact remote target discovery must run against; accepting a
    non-canonical spelling (default port, userinfo, query, a path that
    normalizes to something else) would let two different-looking strings
    silently name the same or a different endpoint, defeating the drift
    check this exists for.
    """

    endpoint = _stable_string(value, field=field)
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not _canonical_https_authority(parsed)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _URL_PATH_RE.fullmatch(parsed.path) is None
        or (parsed.path and posixpath.normpath(parsed.path) != parsed.path)
        or parsed.geturl() != endpoint
    ):
        raise ValueError(f"{field} must be a canonical HTTPS endpoint")
    return endpoint


def _schema_depth(value: object, depth: int = 0) -> int:
    if depth > MAX_SCHEMA_DEPTH:
        return depth
    if type(value) is dict:
        if not value:
            return depth
        return max(_schema_depth(item, depth + 1) for item in value.values())
    if type(value) is list:
        if not value:
            return depth
        return max(_schema_depth(item, depth + 1) for item in value)
    return depth


def _contains_ref(value: object) -> bool:
    if type(value) is dict:
        if _SCHEMA_REF_KEYS.intersection(value):
            return True
        return any(_contains_ref(item) for item in value.values())
    if type(value) is list:
        return any(_contains_ref(item) for item in value)
    return False


def _validate_schema(schema: object) -> None:
    """Accept only a small, bounded, self-contained JSON Schema document.

    A discovered tool schema is untrusted publisher input pinned into a
    local seal; it is never resolved against a remote or shared definition
    (no ``$ref`` family keyword), never unbounded in size or nesting, and
    always a schema the vocabulary itself recognizes as well-formed.
    """

    if type(schema) is not dict:
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED)
    try:
        encoded = canonical_bytes(schema)
    except (TypeError, ValueError):
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED) from None
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise PluginDiscoveryError(_SCHEMA_LIMIT_EXCEEDED)
    if _schema_depth(schema) > MAX_SCHEMA_DEPTH:
        raise PluginDiscoveryError(_SCHEMA_LIMIT_EXCEEDED)
    if _contains_ref(schema):
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED) from None


def _validate_mime_type(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_MIME_TYPE_CHARS
        or _MIME_RE.fullmatch(value) is None
    ):
        raise PluginDiscoveryError(_MIME_UNSUPPORTED)


def _validate_max_bytes(value: object) -> None:
    if type(value) is not int or type(value) is bool or not 0 < value <= MAX_RESULT_BYTES:
        raise PluginDiscoveryError(_RESULT_LIMIT_INVALID)


@dataclass(frozen=True, slots=True)
class CapabilityDiscoveryBinding:
    """What the connection layer already knows before discovery begins.

    ``expected_server_id``/``expected_server_version`` are ``None`` on a
    first discovery (nothing to compare against yet) and are the previously
    reviewed identity on every later re-discovery; a mismatch is drift, not
    an ordinary update, and fails closed.
    """

    plugin_id: str
    app_id: str
    publisher_id: str
    account_key_hash: str
    endpoint: str
    protocol_version: str
    expected_server_id: str | None = None
    expected_server_version: str | None = None

    def __post_init__(self) -> None:
        try:
            plugin_id = _identifier(self.plugin_id, field="plugin_id")
            app_id = _identifier(self.app_id, field="app_id")
            publisher_id = _identifier(self.publisher_id, field="publisher_id")
            account_key_hash = _digest(self.account_key_hash, field="account_key_hash")
            endpoint = _canonical_endpoint(self.endpoint, field="endpoint")
            protocol_version = _stable_string(self.protocol_version, field="protocol_version")
            expected_server_id = (
                None
                if self.expected_server_id is None
                else _identifier(self.expected_server_id, field="expected_server_id")
            )
            expected_server_version = (
                None
                if self.expected_server_version is None
                else _stable_string(
                    self.expected_server_version, field="expected_server_version"
                )
            )
        except (TypeError, ValueError):
            raise PluginDiscoveryError(_REVIEW_REQUIRED) from None
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "app_id", app_id)
        object.__setattr__(self, "publisher_id", publisher_id)
        object.__setattr__(self, "account_key_hash", account_key_hash)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "protocol_version", protocol_version)
        object.__setattr__(self, "expected_server_id", expected_server_id)
        object.__setattr__(self, "expected_server_version", expected_server_version)


@dataclass(frozen=True, slots=True)
class DiscoveredCapability:
    """One enumerated, bounded, never-yet-executed capability.

    ``input_schema``/``output_schema`` carry the exact literal JSON Schema a
    discovered tool declared (``None`` for every other kind) -- the bounded
    payload a sealed authority signs so a compiled runtime tool binding has
    something to execute against, not only a digest. ``compatible`` is
    ``True`` unless this is a tool whose literal schema cannot satisfy the
    planner's own closed-schema projection (``mcp_platform.plugin_planning``):
    such a capability is still shown in the review, but can never enter an
    approved connection scope -- it is incompatible and unsealed, never
    sealed with a widened or looser schema.
    """

    kind: str
    name: str
    title: str
    risk_class: str
    schema_sha256: str
    publisher_annotations: Mapping[str, object]
    input_schema: Mapping[str, object] | None = None
    output_schema: Mapping[str, object] | None = None
    compatible: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "title": self.title,
            "risk_class": self.risk_class,
            "schema_sha256": self.schema_sha256,
            "publisher_annotations": dict(self.publisher_annotations),
            "input_schema": None if self.input_schema is None else dict(self.input_schema),
            "output_schema": None if self.output_schema is None else dict(self.output_schema),
            "compatible": self.compatible,
        }


@dataclass(frozen=True, slots=True)
class CapabilityDiscoveryReview:
    """The complete, bounded result of one restricted-discovery pass."""

    binding: CapabilityDiscoveryBinding
    server_id: str
    server_version: str
    tools: tuple[DiscoveredCapability, ...]
    prompts: tuple[DiscoveredCapability, ...]
    resources: tuple[DiscoveredCapability, ...]
    resource_templates: tuple[DiscoveredCapability, ...]
    review_sha256: str


_ENVELOPE_SEAL: Any = object()
_ENVELOPE_FIELDS = (
    "plugin_id",
    "app_id",
    "publisher_id",
    "account_key_hash",
    "endpoint",
    "protocol_version",
    "server_id",
    "server_version",
    "scopes",
    "capability_authority",
    "capability_schema_sha256",
    "mac",
    "mac_key_sha256",
)


@dataclass(frozen=True, slots=True, init=False)
class LocalCapabilityEnvelope:
    """One device-local sealed authority for exactly the user's approved scope.

    Bound to endpoint, publisher, account partition, protocol, server
    identity and version, and the exact scoped capability-schema digest --
    the local seal is installation authority, never an Omega Verified badge.

    Constructible only through :meth:`_sealed`, called only by
    ``RestrictedPluginDiscovery.approve`` -- the ordinary constructor always
    refuses, so a caller can never fabricate an envelope with a
    ``capability_authority`` that disagrees with its own ``mac``. That seal
    proves *provenance* (this object came from this module's own code), not
    content integrity by itself: a consumer of a sealed envelope --
    ``mcp_platform.plugin_connections.locally_sealed_capability_authority``
    -- still re-verifies the MAC against a live ``LocalTrustRoot``
    (:meth:`sealed_authority`) before trusting its content.
    """

    plugin_id: str
    app_id: str
    publisher_id: str
    account_key_hash: str
    endpoint: str
    protocol_version: str
    server_id: str
    server_version: str
    scopes: tuple[str, ...]
    capability_authority: Mapping[str, Mapping[str, object]]
    capability_schema_sha256: str
    mac: str
    mac_key_sha256: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise PluginDiscoveryError(_ENVELOPE_UNAVAILABLE)

    @classmethod
    def _sealed(
        cls,
        *,
        plugin_id: str,
        app_id: str,
        publisher_id: str,
        account_key_hash: str,
        endpoint: str,
        protocol_version: str,
        server_id: str,
        server_version: str,
        scopes: tuple[str, ...],
        capability_authority: Mapping[str, Mapping[str, object]],
        capability_schema_sha256: str,
        mac: str,
        mac_key_sha256: str,
        seal: object,
    ) -> LocalCapabilityEnvelope:
        if seal is not _ENVELOPE_SEAL:
            raise PluginDiscoveryError(_ENVELOPE_UNAVAILABLE)
        values = {
            "plugin_id": plugin_id,
            "app_id": app_id,
            "publisher_id": publisher_id,
            "account_key_hash": account_key_hash,
            "endpoint": endpoint,
            "protocol_version": protocol_version,
            "server_id": server_id,
            "server_version": server_version,
            "scopes": scopes,
            "capability_authority": capability_authority,
            "capability_schema_sha256": capability_schema_sha256,
            "mac": mac,
            "mac_key_sha256": mac_key_sha256,
        }
        envelope = object.__new__(cls)
        for field_name in _ENVELOPE_FIELDS:
            object.__setattr__(envelope, field_name, values[field_name])
        return envelope

    def sealed_authority(self) -> SealedCapabilityAuthority:
        """Reconstruct the public seal record a ``LocalTrustRoot`` can re-verify."""

        return SealedCapabilityAuthority(
            document_sha256=self.capability_schema_sha256,
            mac=self.mac,
            mac_key_sha256=self.mac_key_sha256,
        )


def _capability_tag(item: DiscoveredCapability) -> str:
    return f"{item.kind}:{item.name}"


def _validate_capability(
    kind: str, raw: object, seen: set[str]
) -> DiscoveredCapability:
    if type(raw) is not dict or any(type(key) is not str for key in raw):
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED)
    allowed = _FIELDS_BY_KIND[kind]
    required = _REQUIRED_FIELDS_BY_KIND[kind]
    fields = set(raw)
    if not fields.issubset(allowed) or not required.issubset(fields):
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED)
    try:
        name = _identifier(raw["name"], field=f"{kind} name")
    except (TypeError, ValueError):
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED) from None
    if name in seen:
        raise PluginDiscoveryError(_DUPLICATE_IDENTITY)
    seen.add(name)
    try:
        title = _bounded_text(raw.get("title", name), field=f"{kind} title", limit=MAX_TITLE_CHARS)
    except ValueError:
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED) from None

    schema_source: dict[str, object] = {}
    input_schema: dict[str, object] | None = None
    output_schema: dict[str, object] | None = None
    compatible = True
    if kind == "tool":
        input_schema = raw["input_schema"]
        _validate_schema(input_schema)
        schema_source["input_schema"] = input_schema
        if "output_schema" in raw:
            output_schema = raw["output_schema"]
            _validate_schema(output_schema)
        else:
            output_schema = dict(_OPEN_RESULT_SCHEMA)
        schema_source["output_schema"] = output_schema
        compatible = _closed_planning_compatible(input_schema, output_schema)
    if kind in ("resource", "resource_template"):
        if "mime_type" in raw:
            _validate_mime_type(raw["mime_type"])
            schema_source["mime_type"] = raw["mime_type"]
        if "max_bytes" in raw:
            _validate_max_bytes(raw["max_bytes"])
            schema_source["max_bytes"] = raw["max_bytes"]

    annotations = raw.get("annotations", {})
    if type(annotations) is not dict or any(type(key) is not str for key in annotations):
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED)
    try:
        frozen_annotations = canonical_bytes(annotations)
    except (TypeError, ValueError):
        raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED) from None

    schema_sha256 = sha256_hex(canonical_bytes(schema_source))
    return DiscoveredCapability(
        kind=kind,
        name=name,
        title=title,
        risk_class=RiskClass.EXTERNAL_UNREVIEWED.value,
        schema_sha256=schema_sha256,
        publisher_annotations=MappingProxyType(json.loads(frozen_annotations.decode("utf-8"))),
        input_schema=None if input_schema is None else MappingProxyType(input_schema),
        output_schema=None if output_schema is None else MappingProxyType(output_schema),
        compatible=compatible,
    )


def _closed_planning_compatible(input_schema: object, output_schema: object) -> bool:
    """Whether a discovered tool's literal schemas can become a plan step.

    Reuses the planner's own closed-schema projection
    (``mcp_platform.plugin_planning``) so a capability that could never
    compile into a callable ``PluginToolBinding`` is marked incompatible at
    discovery time, before any seal is ever produced for it.
    """

    try:
        project_provider_input_schema(input_schema)
        project_provider_output_schema(output_schema)
    except ValueError:
        return False
    return True


def _server_identity(identity: object) -> tuple[str, str, bool]:
    if type(identity) is not dict or any(type(key) is not str for key in identity):
        raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE)
    try:
        server_id = _identifier(identity.get("server_id"), field="server_id")
        server_version = _stable_string(identity.get("server_version"), field="server_version")
    except (TypeError, ValueError):
        raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE) from None
    capabilities = identity.get("capabilities", {})
    if type(capabilities) is not dict:
        raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE)
    mutable = False
    for key in ("tools", "prompts", "resources"):
        group = capabilities.get(key)
        if group is None:
            continue
        if type(group) is not dict:
            raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE)
        if bool(group.get("listChanged")):
            mutable = True
    return server_id, server_version, mutable


def _validate_scope(
    connection_scope: object, review: CapabilityDiscoveryReview
) -> tuple[str, ...]:
    if type(connection_scope) not in (list, tuple, set, frozenset):
        raise PluginDiscoveryError(_SCOPE_UNAVAILABLE)
    try:
        tags = tuple(
            _stable_string(item, field="connection_scope item") for item in connection_scope
        )
    except (TypeError, ValueError):
        raise PluginDiscoveryError(_SCOPE_UNAVAILABLE) from None
    if not tags or len(tags) != len(set(tags)):
        raise PluginDiscoveryError(_SCOPE_UNAVAILABLE)
    # An incompatible tool (its literal schema cannot satisfy the planner's
    # own closed-schema projection) is never an available scope tag -- it is
    # shown in the review but can never be sealed with a looser schema.
    available = {
        _capability_tag(item)
        for item in (
            *review.tools,
            *review.prompts,
            *review.resources,
            *review.resource_templates,
        )
        if item.compatible
    }
    if not set(tags).issubset(available):
        raise PluginDiscoveryError(_SCOPE_UNAVAILABLE)
    return tuple(sorted(tags))


def _sealed_tool_policy(item: DiscoveredCapability) -> dict[str, object]:
    """The exact tool-policy shape the signed-authority compile path expects.

    Mirrors ``mcp_platform.client_contracts._signed_capability_authority``'s
    required tool-policy field set exactly, so a locally sealed capability
    compiles a ``PluginToolBinding`` and a connection descriptor through the
    same code the marketplace Ed25519 path already uses -- no second
    compiler. ``item.compatible`` is always ``True`` here: an incompatible
    tool's tag can never enter ``scope`` (see :func:`_validate_scope`).
    """

    input_schema = {} if item.input_schema is None else dict(item.input_schema)
    output_schema = dict(_OPEN_RESULT_SCHEMA) if item.output_schema is None else dict(item.output_schema)
    return {
        "title": item.title,
        "input_schema": input_schema,
        "input_schema_sha256": sha256_hex(canonical_bytes(input_schema)),
        "output_schema": output_schema,
        "output_schema_sha256": sha256_hex(canonical_bytes(output_schema)),
        "risk_class": item.risk_class,
        "effect": UNREVIEWED_EFFECT,
        "requires_revision": False,
        "max_result_bytes": MAX_RESULT_BYTES,
        "allowed_mime_types": list(_SEALED_TOOL_MIME_TYPES),
    }


def _filtered_authority(
    review: CapabilityDiscoveryReview, scope: tuple[str, ...]
) -> dict[str, dict[str, object]]:
    groups: dict[str, dict[str, object]] = {
        "tools": {},
        "resources": {},
        "resource_templates": {},
        "prompts": {},
    }
    scope_set = set(scope)
    for item in (
        *review.tools,
        *review.prompts,
        *review.resources,
        *review.resource_templates,
    ):
        if _capability_tag(item) not in scope_set:
            continue
        if item.kind == "tool":
            groups["tools"][item.name] = _sealed_tool_policy(item)
            continue
        groups[_PLURAL_BY_KIND[item.kind]][item.name] = {
            "title": item.title,
            "risk_class": item.risk_class,
            "schema_sha256": item.schema_sha256,
            "publisher_annotations": dict(item.publisher_annotations),
        }
    return groups


class RestrictedPluginDiscovery:
    """Enumerate one MCP peer's capabilities without ever calling any of them."""

    def __init__(self, peer: DiscoveryPeer, *, trust_root: LocalTrustRoot | None = None) -> None:
        self._peer = peer
        self._trust_root = trust_root

    async def _negotiate(self, operation: str) -> object:
        try:
            return await self._peer.negotiate(operation)
        except PluginDiscoveryError:
            raise
        except Exception:
            raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE) from None

    async def _enumerate(self, kind: str) -> tuple[DiscoveredCapability, ...]:
        raw = await self._negotiate(_OPERATION_BY_KIND[kind])
        if type(raw) is not list:
            raise PluginDiscoveryError(_SCHEMA_UNSUPPORTED)
        seen: set[str] = set()
        return tuple(_validate_capability(kind, item, seen) for item in raw)

    async def discover(self, binding: CapabilityDiscoveryBinding) -> CapabilityDiscoveryReview:
        """Negotiate and enumerate -- and nothing else -- in the fixed order.

        ``initialize`` (protocol negotiation), then ``tools/list``,
        ``prompts/list``, ``resources/list``, ``resources/templates/list``.
        """

        if type(binding) is not CapabilityDiscoveryBinding:
            raise PluginDiscoveryError(_REVIEW_REQUIRED)
        self._require_matching_endpoint(binding.endpoint)
        identity = await self._negotiate("initialize")
        server_id, server_version, mutable = _server_identity(identity)
        if mutable:
            raise PluginDiscoveryError(_MUTABLE_PAGES)
        if (
            binding.expected_server_id is not None
            and binding.expected_server_id != server_id
        ) or (
            binding.expected_server_version is not None
            and binding.expected_server_version != server_version
        ):
            raise PluginDiscoveryError(_IDENTITY_CHANGED)

        tools = await self._enumerate("tool")
        prompts = await self._enumerate("prompt")
        resources = await self._enumerate("resource")
        resource_templates = await self._enumerate("resource_template")

        review_document = {
            "domain": _REVIEW_DOMAIN,
            "plugin_id": binding.plugin_id,
            "app_id": binding.app_id,
            "server_id": server_id,
            "server_version": server_version,
            "tools": [item.to_dict() for item in tools],
            "prompts": [item.to_dict() for item in prompts],
            "resources": [item.to_dict() for item in resources],
            "resource_templates": [item.to_dict() for item in resource_templates],
        }
        return CapabilityDiscoveryReview(
            binding=binding,
            server_id=server_id,
            server_version=server_version,
            tools=tools,
            prompts=prompts,
            resources=resources,
            resource_templates=resource_templates,
            review_sha256=sha256_hex(canonical_bytes(review_document)),
        )

    def approve(
        self, review: CapabilityDiscoveryReview, connection_scope: object
    ) -> LocalCapabilityEnvelope:
        """Seal exactly the user's chosen subset of one prior review.

        Nothing outside ``connection_scope`` ever reaches the sealed
        envelope, and nothing not already present in ``review`` can be named
        by ``connection_scope`` -- there is no way to approve a capability
        this device never actually enumerated.
        """

        if type(review) is not CapabilityDiscoveryReview or self._trust_root is None:
            raise PluginDiscoveryError(_REVIEW_REQUIRED)
        self._require_matching_endpoint(review.binding.endpoint)
        scope = _validate_scope(connection_scope, review)
        capability_authority = _filtered_authority(review, scope)
        try:
            sealed = self._trust_root.seal_authority(capability_authority)
        except LocalTrustError:
            raise PluginDiscoveryError(_ENVELOPE_UNAVAILABLE) from None
        return LocalCapabilityEnvelope._sealed(
            plugin_id=review.binding.plugin_id,
            app_id=review.binding.app_id,
            publisher_id=review.binding.publisher_id,
            account_key_hash=review.binding.account_key_hash,
            endpoint=review.binding.endpoint,
            protocol_version=review.binding.protocol_version,
            server_id=review.server_id,
            server_version=review.server_version,
            scopes=scope,
            capability_authority={
                group: MappingProxyType(dict(entries))
                for group, entries in capability_authority.items()
            },
            capability_schema_sha256=sealed.document_sha256,
            mac=sealed.mac,
            mac_key_sha256=sealed.mac_key_sha256,
            seal=_ENVELOPE_SEAL,
        )

    def _require_matching_endpoint(self, expected_endpoint: str) -> None:
        """Fail closed before any operation if the live peer targets a different endpoint.

        ``binding.endpoint``/``review.binding.endpoint`` is what the
        connection layer -- and, for ``approve``, the original review --
        expects; the peer's own ``endpoint`` is the transport target it was
        actually constructed with. A review taken against one endpoint can
        never be approved through a discovery instance bound to another.
        """

        try:
            peer_endpoint = self._peer.endpoint
        except PluginDiscoveryError:
            raise
        except Exception:
            raise PluginDiscoveryError(_TRANSPORT_UNAVAILABLE) from None
        if type(peer_endpoint) is not str or peer_endpoint != expected_endpoint:
            raise PluginDiscoveryError(_ENDPOINT_CHANGED)


__all__ = [
    "CapabilityDiscoveryBinding",
    "CapabilityDiscoveryReview",
    "DiscoveredCapability",
    "DiscoveryPeer",
    "LocalCapabilityEnvelope",
    "PluginDiscoveryError",
    "RestrictedPluginDiscovery",
]
