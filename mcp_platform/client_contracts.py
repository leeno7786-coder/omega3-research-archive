"""Immutable, transport-neutral contracts for the current MCP client."""

from __future__ import annotations

import base64
import math
import posixpath
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.extensions import (
    MCP_APPS_EXTENSION_IDENTIFIER,
    validate_mcp_apps_metadata,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, RiskClass
from plugin_platform.contracts import PluginAuthentication

_HEX = frozenset("0123456789abcdef")
_REMOTE_ORIGIN_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~-]+)+/?\Z")
_RESERVED_PATH_SEGMENTS = frozenset({".", ".."})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]*\Z")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z")
#: A trailing slash is part of the issuer, not noise. RFC 8414 compares an
#: issuer by EXACT string, and vendors publish one: Netlify's is
#: "https://netlify-mcp.netlify.app/" and Miro's "https://mcp.miro.com/".
#: Refusing that shape meant Omega could not even describe those providers,
#: let alone match their metadata -- the same mistake as the endpoint path.
_ISSUER_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~-]+)*/?\Z")
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_MAX_SIGNED_APPS_METADATA_BYTES = 65_536
TASKS_EXTENSION_IDENTIFIER = "io.modelcontextprotocol/tasks"
#: The one effect string a locally sealed, external_unreviewed tool policy
#: (``mcp_platform.plugin_discovery``) carries -- kept in sync with
#: ``_EFFECT_SUMMARIES`` in ``mcp_platform.plugin_permissions``.
UNREVIEWED_EFFECT = "unreviewed"


class McpClientDeployment(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class McpTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class DiscoverySource(StrEnum):
    CATALOG_DECLARED = "catalog_declared"
    LIVE_OBSERVED = "live_observed"
    LIVE_AUTHENTICATED = "live_authenticated"


class McpOperationKind(StrEnum):
    TOOLS_LIST = "tools/list"
    TOOL_CALL = "tools/call"
    PROMPTS_LIST = "prompts/list"
    PROMPT_GET = "prompts/get"
    RESOURCES_LIST = "resources/list"
    RESOURCE_TEMPLATES_LIST = "resources/templates/list"
    RESOURCE_READ = "resources/read"
    SUBSCRIPTIONS_LISTEN = "subscriptions/listen"
    TASK_GET = "tasks/get"
    TASK_UPDATE = "tasks/update"
    TASK_CANCEL = "tasks/cancel"


class McpOperationStatus(StrEnum):
    COMPLETED = "completed"
    INPUT_REQUIRED = "input_required"
    TASK_STARTED = "task_started"
    TASK_RECOVERY_REQUIRED = "task_recovery_required"
    TASK_UPDATED = "task_updated"
    PROGRESS = "progress"
    NOT_MODIFIED = "not_modified"
    FAILED = "failed"


class McpClientErrorReason(StrEnum):
    PROTOCOL_MISMATCH = "protocol_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    CAPABILITY_DRIFT = "capability_drift"
    AUTHORIZATION_REQUIRED = "authorization_required"
    AUTHORIZATION_INVALID = "authorization_invalid"
    AUTHORIZATION_STORE_UNAVAILABLE = "authorization_store_unavailable"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    REQUEST_TIMEOUT = "request_timeout"
    DOCUMENT_LIMIT_EXCEEDED = "document_limit_exceeded"
    PAGINATION_LIMIT_EXCEEDED = "pagination_limit_exceeded"
    RESULT_LIMIT_EXCEEDED = "result_limit_exceeded"
    INPUT_LIMIT_EXCEEDED = "input_limit_exceeded"
    TASK_INVALID = "task_invalid"
    PROGRESS_LIMIT_EXCEEDED = "progress_limit_exceeded"
    NOTIFICATION_LIMIT_EXCEEDED = "notification_limit_exceeded"
    REDIRECT_LIMIT_EXCEEDED = "redirect_limit_exceeded"
    RESULT_INVALID = "result_invalid"
    CLEANUP_REQUIRED = "cleanup_required"
    #: A discovery-only client (``CurrentMcpClient.for_restricted_discovery``)
    #: refuses every operation but ``discover_restricted`` with this cause --
    #: the connection has not been reviewed and approved, so it structurally
    #: cannot call a tool, fetch a prompt or resource, or subscribe.
    REVIEW_REQUIRED = "plugin_review_required"


def _plain_string(value: Any, *, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or unicodedata.normalize("NFC", value) != value
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in value
        )
    ):
        raise ValueError(f"{field} must be an NFC plain string")
    return value


def _optional_plain_string(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _plain_string(value, field=field)


def _identifier(value: Any, *, field: str) -> str:
    identifier = _plain_string(value, field=field)
    if _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError(f"{field} is invalid")
    return identifier


def _optional_identifier(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field=field)


def _revision(value: Any, *, field: str) -> str:
    revision = _plain_string(value, field=field)
    if len(revision) != 64 or any(character not in _HEX for character in revision):
        raise ValueError(f"{field} must be a lowercase SHA-256 value")
    return revision


def _semver(value: Any, *, field: str) -> str:
    version = _plain_string(value, field=field)
    if _SEMVER_RE.fullmatch(version) is None:
        raise ValueError(f"{field} is not exact SemVer")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"{field} is not exact SemVer") from exc
    return version


def _positive_int(value: Any, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _capability_label(value: Any, *, field: str) -> str:
    """A human-readable capability name, bounded the way the manifest bounds it.

    ``plugin_platform.contracts._capability_label`` is the manifest-side twin;
    the two are held to the same shape because a package declares a resource
    with one and the signed authority names the same resource with the other.
    """
    label = _plain_string(value, field=field)
    if not 1 <= len(label) <= 120:
        raise ValueError(f"{field} must be 1..120 characters")
    return label


def _server_id(value: Any) -> str:
    """A server's self-reported name, which is a LABEL and not an identifier.

    MCP's ``serverInfo.name`` is human-readable: Slack answers "Slack MCP",
    with a space, and a signed, installed, authorized plugin failed at connect
    with result_invalid because this alone demanded an identifier. The signed
    side never agreed with that -- a package's own
    ``runtime_compatibility.missing_metadata.server_id`` has always been a
    bounded plain string, and ``_normalize_metadata`` copies that exact value
    into the receipt, so the two rules contradicted each other for any provider
    whose name is not identifier-shaped. Same bound as the signed side; the
    equality check it feeds is unchanged.
    """
    server_id = _plain_string(value, field="server_id")
    if not 1 <= len(server_id) <= 120:
        raise ValueError("server_id is invalid")
    return server_id


def _canonical_https_authority(parsed: Any) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if hostname is None or _HOST_RE.fullmatch(hostname) is None or port in {0, 443}:
        return False
    expected = hostname if port is None else f"{hostname}:{port}"
    return parsed.netloc == expected


def _https_origin(value: Any) -> str:
    try:
        origin = _plain_string(value, field="remote_origin")
        parsed = urlsplit(origin)
    except (TypeError, ValueError):
        raise ValueError("remote_origin must be a canonical HTTPS origin") from None
    if (
        parsed.scheme != "https"
        or not _canonical_https_authority(parsed)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != origin
    ):
        raise ValueError("remote_origin must be a canonical HTTPS origin")
    path = parsed.path
    if path and (
        _REMOTE_ORIGIN_PATH_RE.fullmatch(path) is None
        or _RESERVED_PATH_SEGMENTS & set(path.strip("/").split("/"))
    ):
        raise ValueError("remote_origin must be a canonical HTTPS origin")
    return origin


def _https_issuer(value: Any) -> str:
    try:
        issuer = _plain_string(value, field="authorization_issuer")
        parsed = urlsplit(issuer)
    except (TypeError, ValueError):
        raise ValueError("authorization_issuer must be a canonical HTTPS issuer") from None
    if (
        parsed.scheme != "https"
        or not _canonical_https_authority(parsed)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _ISSUER_PATH_RE.fullmatch(parsed.path) is None
        # A ".." segment matches the path grammar's character class and always
        # did. It must not: `_metadata_url` inserts the well-known prefix ahead
        # of this path, and an HTTP client resolves dot segments, so a traversal
        # here would address a document outside the well-known space.
        or posixpath.normpath(parsed.path or "/") != (parsed.path.rstrip("/") or "/")
        or parsed.geturl() != issuer
    ):
        raise ValueError("authorization_issuer must be a canonical HTTPS issuer")
    return issuer


def _freeze_json(value: Any, *, field: str) -> Any:
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{field} must be finite JSON data")
        return value
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError(f"{field} strings must be NFC")
        if any(unicodedata.category(character) == "Cs" for character in value):
            raise ValueError(f"{field} strings must be JSON-compatible")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError(f"{field} keys must be strings")
        try:
            for key in value:
                _plain_string(key, field=f"{field} key")
        except ValueError:
            raise ValueError(f"{field} keys must be NFC plain strings") from None
        return MappingProxyType(
            {key: _freeze_json(value[key], field=f"{field}.{key}") for key in sorted(value)}
        )
    if type(value) in {list, tuple}:
        return tuple(_freeze_json(item, field=field) for item in value)
    raise ValueError(f"{field} must be JSON-compatible")


def _canonical_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    frozen = _freeze_json(value, field=field)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return frozen


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _capability_mapping(value: Any) -> Mapping[str, str | None]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError("declared_capabilities must be a mapping")
    parsed: dict[str, str | None] = {}
    for key in sorted(value):
        capability_id = _identifier(key, field="declared capability identity")
        digest = value[key]
        parsed[capability_id] = (
            None if digest is None else _revision(digest, field="declared capability digest")
        )
    return MappingProxyType(parsed)


def _version_mapping(value: Any) -> Mapping[str, str | None]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError("extension_versions must be a mapping")
    parsed: dict[str, str | None] = {}
    for raw_key in sorted(value):
        key = _identifier(raw_key, field="extension identity")
        version = value[raw_key]
        if key == TASKS_EXTENSION_IDENTIFIER:
            if version is not None:
                raise ValueError("Tasks extension version must be null")
            parsed[key] = None
        else:
            parsed[key] = _semver(version, field="extension version")
    return MappingProxyType(parsed)


def _runtime_compatibility(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, Mapping) or set(value) != {
            "wire_protocol_version",
            "handshake_mode",
            "missing_metadata",
        }:
            raise ValueError
        metadata = value["missing_metadata"]
        if not isinstance(metadata, Mapping) or set(metadata) != {
            "server_id",
            "server_version",
        }:
            raise ValueError
        wire_version = _plain_string(
            value["wire_protocol_version"], field="runtime wire protocol version"
        )
        handshake_mode = _plain_string(
            value["handshake_mode"], field="runtime handshake mode"
        )
        server_id = _plain_string(metadata["server_id"], field="runtime server id")
        server_version = _plain_string(
            metadata["server_version"], field="runtime server version"
        )
        # Any handshake-era version, not just the newest: this names the wire
        # version the server actually speaks, and ``_discover_receipt`` compares
        # the live negotiated version against it exactly. Pinning it to the
        # newest one made a legacy server on any other version unsupportable.
        if (
            wire_version not in HANDSHAKE_PROTOCOL_VERSIONS
            or handshake_mode != "legacy"
            or not 1 <= len(server_id) <= 120
            or not 1 <= len(server_version) <= 120
        ):
            raise ValueError
        return _canonical_mapping(
            {
                "wire_protocol_version": wire_version,
                "handshake_mode": handshake_mode,
                "missing_metadata": {
                    "server_id": server_id,
                    "server_version": server_version,
                },
            },
            field="runtime_compatibility",
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("runtime_compatibility_invalid") from None


def _signed_capability_authority(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    try:
        required_fields = {
            "tools",
            "resources",
            "resource_templates",
        }
        optional_fields = {"prompts", "mcp_apps_metadata"}
        if (
            not isinstance(value, Mapping)
            or not required_fields.issubset(value)
            or not set(value).issubset(required_fields | optional_fields)
        ):
            raise ValueError
        tools = value["tools"]
        resources = value["resources"]
        templates = value["resource_templates"]
        prompts = value.get("prompts", {})
        if not all(
            isinstance(group, Mapping)
            for group in (tools, resources, templates, prompts)
        ):
            raise ValueError

        parsed_tools: dict[str, object] = {}
        for raw_name in sorted(tools):
            name = _identifier(raw_name, field="signed tool name")
            policy = tools[raw_name]
            if not isinstance(policy, Mapping) or set(policy) != {
                "title",
                "input_schema",
                "input_schema_sha256",
                "output_schema",
                "output_schema_sha256",
                "risk_class",
                "effect",
                "requires_revision",
                "max_result_bytes",
                "allowed_mime_types",
            }:
                raise ValueError
            title = _plain_string(policy["title"], field="signed tool title")
            if not title:
                raise ValueError
            input_schema = _canonical_mapping(
                policy["input_schema"],
                field="signed tool input_schema",
            )
            input_digest = _revision(
                policy["input_schema_sha256"],
                field="signed tool input_schema_sha256",
            )
            if input_digest != sha256_hex(canonical_bytes(_thaw_json(input_schema))):
                raise ValueError
            output_schema = _canonical_mapping(
                policy["output_schema"],
                field="signed tool output_schema",
            )
            output_digest = _revision(
                policy["output_schema_sha256"],
                field="signed tool output_schema_sha256",
            )
            if output_digest != sha256_hex(canonical_bytes(_thaw_json(output_schema))):
                raise ValueError
            mime_types = policy["allowed_mime_types"]
            if type(mime_types) not in {list, tuple}:
                raise ValueError
            parsed_mime_types = tuple(
                _plain_string(item, field="signed tool allowed MIME type") for item in mime_types
            )
            if len(parsed_mime_types) != len(set(parsed_mime_types)):
                raise ValueError
            try:
                risk_class = RiskClass(policy["risk_class"])
            except (TypeError, ValueError):
                raise ValueError from None
            effect = _plain_string(policy["effect"], field="signed tool effect")
            expected_effect = {
                RiskClass.READ_ONLY: "read",
                RiskClass.ADDITIVE_WRITE: "create",
                RiskClass.MUTATING_WRITE: "update",
                RiskClass.DESTRUCTIVE: "delete",
                RiskClass.SEND_PUBLISH: "send",
                RiskClass.CROSS_PROVIDER_TRANSFER: "transfer",
                RiskClass.EXTERNAL_UNREVIEWED: UNREVIEWED_EFFECT,
            }[risk_class]
            if effect != expected_effect:
                raise ValueError
            if type(policy["requires_revision"]) is not bool:
                raise ValueError
            parsed_tools[name] = {
                "title": title,
                "input_schema": input_schema,
                "input_schema_sha256": input_digest,
                "output_schema": output_schema,
                "output_schema_sha256": output_digest,
                "risk_class": risk_class.value,
                "effect": effect,
                "requires_revision": policy["requires_revision"],
                "max_result_bytes": _positive_int(
                    policy["max_result_bytes"],
                    field="signed tool max_result_bytes",
                ),
                "allowed_mime_types": parsed_mime_types,
            }

        def parse_resource_group(
            group: Mapping[Any, Any],
            *,
            uri_field: str,
        ) -> dict[str, object]:
            parsed: dict[str, object] = {}
            for raw_name in sorted(group):
                # A LABEL, like the manifest's own resource capability. Slack
                # names one "Send Message Input Form" and Stripe "Balance
                # summary"; demanding an identifier here contradicted the rule
                # the package is written against.
                name = _capability_label(raw_name, field="signed resource name")
                policy = group[raw_name]
                if not isinstance(policy, Mapping) or set(policy) != {
                    uri_field,
                    "mime_type",
                    "max_bytes",
                }:
                    raise ValueError
                # ``mimeType`` is optional in MCP, and the receipt copies
                # whatever the provider sent -- None included. A policy that
                # could not say None could not describe Wix's widgets at all.
                declared_mime = policy["mime_type"]
                parsed[name] = {
                    uri_field: _plain_string(policy[uri_field], field=f"signed {uri_field}"),
                    "mime_type": (
                        None
                        if declared_mime is None
                        else _plain_string(
                            declared_mime,
                            field="signed resource MIME type",
                        )
                    ),
                    "max_bytes": _positive_int(
                        policy["max_bytes"],
                        field="signed resource max_bytes",
                    ),
                }
            return parsed

        parsed_prompts: dict[str, object] = {}
        for raw_name in sorted(prompts):
            name = _identifier(raw_name, field="signed prompt name")
            policy = prompts[raw_name]
            if not isinstance(policy, Mapping) or set(policy) != {
                "title",
                "arguments",
                "schema_sha256",
                "max_result_bytes",
            }:
                raise ValueError
            title = _plain_string(policy["title"], field="signed prompt title")
            raw_arguments = policy["arguments"]
            if type(raw_arguments) not in {list, tuple}:
                raise ValueError
            arguments: list[dict[str, object]] = []
            argument_names: set[str] = set()
            for raw_argument in raw_arguments:
                if not isinstance(raw_argument, Mapping) or set(raw_argument) != {
                    "name",
                    "required",
                }:
                    raise ValueError
                argument_name = _identifier(
                    raw_argument["name"],
                    field="signed prompt argument name",
                )
                if (
                    argument_name in argument_names
                    or type(raw_argument["required"]) is not bool
                ):
                    raise ValueError
                argument_names.add(argument_name)
                arguments.append(
                    {
                        "name": argument_name,
                        "required": raw_argument["required"],
                    }
                )
            arguments.sort(key=lambda item: str(item["name"]))
            schema = {"arguments": arguments}
            schema_digest = _revision(
                policy["schema_sha256"],
                field="signed prompt schema_sha256",
            )
            if schema_digest != sha256_hex(canonical_bytes(schema)):
                raise ValueError
            parsed_prompts[name] = {
                "title": title,
                "arguments": arguments,
                "schema_sha256": schema_digest,
                "max_result_bytes": _positive_int(
                    policy["max_result_bytes"],
                    field="signed prompt max_result_bytes",
                ),
            }

        parsed = {
            "tools": parsed_tools,
            "resources": parse_resource_group(resources, uri_field="uri"),
            "resource_templates": parse_resource_group(
                templates,
                uri_field="uri_template",
            ),
            "prompts": parsed_prompts,
        }
        if "mcp_apps_metadata" in value:
            apps = value["mcp_apps_metadata"]
            if not isinstance(apps, Mapping) or set(apps) != required_fields:
                raise ValueError
            if not all(isinstance(apps[group], Mapping) for group in required_fields):
                raise ValueError
            if any(set(apps[group]) - set(parsed[group]) for group in required_fields):
                raise ValueError
            plain_apps = _thaw_json(apps)
            parsed["mcp_apps_metadata"] = validate_mcp_apps_metadata(
                plain_apps,
                signed_metadata=plain_apps,
                declared_extensions={MCP_APPS_EXTENSION_IDENTIFIER: "signed"},
                max_bytes=_MAX_SIGNED_APPS_METADATA_BYTES,
            )
        return _canonical_mapping(parsed, field="signed_capability_authority")
    except (KeyError, TypeError, ValueError):
        raise ValueError("signed_capability_authority_invalid") from None


@dataclass(frozen=True, slots=True)
class McpClientLimits:
    max_discovery_document_bytes: int = 1_048_576
    max_result_document_bytes: int = 1_048_576
    max_list_pages: int = 64
    max_list_items: int = 4_096
    max_cursor_history: int = 128
    max_tool_argument_bytes: int = 262_144
    max_structured_result_bytes: int = 1_048_576
    max_resource_text_bytes: int = 1_048_576
    max_embedded_resource_bytes: int = 4_194_304
    max_input_rounds: int = 8
    max_input_fields: int = 64
    max_task_polls: int = 256
    max_task_updates: int = 256
    max_task_duration_ms: int = 900_000
    max_active_tasks: int = 128
    max_task_tombstones: int = 512
    max_progress_events: int = 1_024
    max_notification_events: int = 1_024
    max_cache_entries: int = 512
    max_cache_ttl_seconds: int = 300
    max_http_redirects: int = 3
    max_connection_duration_ms: int = 120_000
    max_operation_duration_ms: int = 60_000
    max_stderr_bytes: int = 65_536
    max_shutdown_duration_ms: int = 10_000

    def __post_init__(self) -> None:
        for field in fields(self):
            _positive_int(getattr(self, field.name), field=field.name)

    def to_dict(self) -> dict[str, int]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class McpConnectionDescriptor:
    app_id: str
    publisher_id: str
    deployment: McpClientDeployment
    provider_id: str | None
    protocol_version: str
    transport: McpTransport
    verified_launch_sha256: str | None
    remote_origin: str | None
    authorization_issuer: str | None
    authentication: PluginAuthentication
    declared_capabilities: Mapping[str, str | None]
    extension_versions: Mapping[str, str | None]
    capability_revision: str
    limits: McpClientLimits
    signed_capability_authority: Mapping[str, Any] | None = None
    runtime_compatibility: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if type(self.deployment) is not McpClientDeployment:
            raise ValueError("descriptor deployment is invalid")
        if type(self.transport) is not McpTransport:
            raise ValueError("descriptor transport is invalid")
        if type(self.protocol_version) is not str or self.protocol_version != MCP_PROTOCOL_VERSION:
            raise ValueError("descriptor protocol is invalid")
        if type(self.limits) is not McpClientLimits:
            raise ValueError("descriptor limits are invalid")
        if type(self.authentication) is not PluginAuthentication:
            raise ValueError("descriptor authentication is invalid")
        object.__setattr__(
            self,
            "authentication",
            PluginAuthentication.from_dict(self.authentication.to_dict()),
        )
        object.__setattr__(self, "app_id", _identifier(self.app_id, field="app_id"))
        object.__setattr__(
            self,
            "publisher_id",
            _identifier(self.publisher_id, field="publisher_id"),
        )
        object.__setattr__(
            self,
            "provider_id",
            _optional_identifier(self.provider_id, field="provider_id"),
        )
        object.__setattr__(
            self,
            "capability_revision",
            _revision(self.capability_revision, field="capability_revision"),
        )
        object.__setattr__(
            self,
            "declared_capabilities",
            _capability_mapping(self.declared_capabilities),
        )
        object.__setattr__(
            self,
            "extension_versions",
            _version_mapping(self.extension_versions),
        )
        object.__setattr__(
            self,
            "signed_capability_authority",
            _signed_capability_authority(self.signed_capability_authority),
        )
        object.__setattr__(
            self,
            "runtime_compatibility",
            _runtime_compatibility(self.runtime_compatibility),
        )
        if (
            self.signed_capability_authority is not None
            and self.signed_capability_authority.get("mcp_apps_metadata") is not None
            and MCP_APPS_EXTENSION_IDENTIFIER not in self.extension_versions
        ):
            raise ValueError("signed_capability_authority_invalid")
        if self.deployment is McpClientDeployment.LOCAL:
            if self.transport is not McpTransport.STDIO:
                raise ValueError("local descriptor transport is invalid")
            if self.verified_launch_sha256 is None:
                raise ValueError("local descriptor launch identity is invalid")
            object.__setattr__(
                self,
                "verified_launch_sha256",
                _revision(self.verified_launch_sha256, field="verified_launch_sha256"),
            )
            if self.remote_origin is not None or self.authorization_issuer is not None:
                raise ValueError("local descriptor remote identity is invalid")
        else:
            if self.transport is not McpTransport.STREAMABLE_HTTP:
                raise ValueError("remote descriptor transport is invalid")
            if self.verified_launch_sha256 is not None:
                raise ValueError("remote descriptor launch identity is invalid")
            object.__setattr__(self, "remote_origin", _https_origin(self.remote_origin))
            object.__setattr__(
                self,
                "authorization_issuer",
                _https_issuer(self.authorization_issuer),
            )
        if self.runtime_compatibility is not None and self.deployment is not McpClientDeployment.REMOTE:
            raise ValueError("runtime_compatibility_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "publisher_id": self.publisher_id,
            "deployment": self.deployment.value,
            "provider_id": self.provider_id,
            "protocol_version": self.protocol_version,
            "transport": self.transport.value,
            "verified_launch_sha256": self.verified_launch_sha256,
            "remote_origin": self.remote_origin,
            "authorization_issuer": self.authorization_issuer,
            "authentication": self.authentication.to_dict(),
            "declared_capabilities": dict(self.declared_capabilities),
            "extension_versions": dict(self.extension_versions),
            "capability_revision": self.capability_revision,
            "limits": self.limits.to_dict(),
            "signed_capability_authority": _thaw_json(self.signed_capability_authority),
            "runtime_compatibility": _thaw_json(self.runtime_compatibility),
        }


@dataclass(frozen=True, slots=True)
class McpDiscoveryReceipt:
    app_id: str
    publisher_id: str
    deployment: McpClientDeployment
    transport: McpTransport
    protocol_version: str
    source: DiscoverySource
    server_id: str | None
    server_version: str | None
    tools: Mapping[str, Any]
    prompts: Mapping[str, Any]
    resources: Mapping[str, Any]
    resource_templates: Mapping[str, Any]
    extensions: Mapping[str, Any]
    cache_metadata: Mapping[str, Any]
    capability_revision: str
    signed_capability_authority: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if type(self.deployment) is not McpClientDeployment:
            raise ValueError("discovery deployment is invalid")
        if type(self.transport) is not McpTransport:
            raise ValueError("discovery transport is invalid")
        if type(self.source) is not DiscoverySource:
            raise ValueError("discovery source is invalid")
        if type(self.protocol_version) is not str or self.protocol_version != MCP_PROTOCOL_VERSION:
            raise ValueError("discovery protocol is invalid")
        if (
            self.deployment is McpClientDeployment.LOCAL
            and self.transport is not McpTransport.STDIO
        ) or (
            self.deployment is McpClientDeployment.REMOTE
            and self.transport is not McpTransport.STREAMABLE_HTTP
        ):
            raise ValueError("discovery transport identity is invalid")
        object.__setattr__(self, "app_id", _identifier(self.app_id, field="app_id"))
        object.__setattr__(
            self,
            "publisher_id",
            _identifier(self.publisher_id, field="publisher_id"),
        )
        if self.source is DiscoverySource.CATALOG_DECLARED:
            if self.server_id is not None or self.server_version is not None:
                raise ValueError("catalog discovery server identity is invalid")
        elif self.server_id is None or self.server_version is None:
            raise ValueError("live discovery server identity is invalid")
        else:
            object.__setattr__(self, "server_id", _server_id(self.server_id))
            object.__setattr__(
                self,
                "server_version",
                _plain_string(self.server_version, field="server_version"),
            )
        for field in (
            "tools",
            "prompts",
            "resources",
            "resource_templates",
            "extensions",
            "cache_metadata",
        ):
            object.__setattr__(
                self,
                field,
                _canonical_mapping(getattr(self, field), field=field),
            )
        object.__setattr__(
            self,
            "capability_revision",
            _revision(self.capability_revision, field="capability_revision"),
        )
        object.__setattr__(
            self,
            "signed_capability_authority",
            _signed_capability_authority(self.signed_capability_authority),
        )
        if (
            self.signed_capability_authority is not None
            and self.signed_capability_authority.get("mcp_apps_metadata") is not None
            and MCP_APPS_EXTENSION_IDENTIFIER not in self.extensions
        ):
            raise ValueError("signed_capability_authority_invalid")
    def to_dict(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "publisher_id": self.publisher_id,
            "deployment": self.deployment.value,
            "transport": self.transport.value,
            "protocol_version": self.protocol_version,
            "source": self.source.value,
            "server_id": self.server_id,
            "server_version": self.server_version,
            "tools": _thaw_json(self.tools),
            "prompts": _thaw_json(self.prompts),
            "resources": _thaw_json(self.resources),
            "resource_templates": _thaw_json(self.resource_templates),
            "extensions": _thaw_json(self.extensions),
            "cache_metadata": _thaw_json(self.cache_metadata),
            "capability_revision": self.capability_revision,
            "signed_capability_authority": _thaw_json(self.signed_capability_authority),
        }


@dataclass(frozen=True, slots=True)
class RestrictedDiscoveryReceipt:
    """One immutable, complete record of a restricted-discovery pass.

    Produced only by ``CurrentMcpClient.discover_restricted`` -- the sole
    entry point a discovery-only client (``CurrentMcpClient.for_restricted_discovery``)
    exposes. Carries every capability the server actually enumerated during
    ``initialize`` plus ``tools/list``/``prompts/list``/``resources/list``/
    ``resources/templates/list`` -- literal input/output schemas for tools,
    URI/MIME for resources and templates, prompt arguments, and the
    publisher's own MCP annotations preserved as unreviewed claims -- and
    nothing else: no result of ``tools/call``, ``prompts/get``,
    ``resources/read``, or a subscription ever reaches this type, because a
    discovery-only client structurally cannot issue those operations.
    """

    server_id: str
    server_version: str
    tools_list_changed: bool
    prompts_list_changed: bool
    resources_list_changed: bool
    tools: tuple[Mapping[str, Any], ...]
    prompts: tuple[Mapping[str, Any], ...]
    resources: tuple[Mapping[str, Any], ...]
    resource_templates: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", _server_id(self.server_id))
        object.__setattr__(
            self,
            "server_version",
            _plain_string(self.server_version, field="server_version"),
        )
        for flag_field in (
            "tools_list_changed",
            "prompts_list_changed",
            "resources_list_changed",
        ):
            if type(getattr(self, flag_field)) is not bool:
                raise ValueError(f"{flag_field} must be a boolean")
        for group_field in ("tools", "prompts", "resources", "resource_templates"):
            value = getattr(self, group_field)
            if type(value) is not tuple or any(
                not isinstance(item, Mapping)
                or any(type(key) is not str for key in item)
                for item in value
            ):
                raise ValueError(f"{group_field} must be a tuple of string-keyed mappings")
            object.__setattr__(
                self,
                group_field,
                tuple(MappingProxyType(dict(item)) for item in value),
            )


@dataclass(frozen=True, slots=True)
class McpOperationRequest:
    request_id: str
    app_id: str
    operation: McpOperationKind
    capability_id: str
    arguments: Mapping[str, Any]
    capability_revision: str
    deadline_monotonic: float
    cancellation_id: str
    task_id: str | None
    input_request_id: str | None
    request_state: bytes | None = dataclass_field(repr=False)

    def __post_init__(self) -> None:
        if type(self.operation) is not McpOperationKind:
            raise ValueError("operation is invalid")
        if (
            type(self.deadline_monotonic) not in {int, float}
            or not math.isfinite(self.deadline_monotonic)
            or self.deadline_monotonic <= 0
        ):
            raise ValueError("deadline_monotonic must be a positive finite number")
        if self.request_state is not None and type(self.request_state) is not bytes:
            raise ValueError("request_state must be bytes")
        if (
            self.task_id is not None
            and (self.input_request_id is not None or self.request_state is not None)
        ) or ((self.input_request_id is None) != (self.request_state is None)):
            raise ValueError("operation continuation identity is invalid")
        object.__setattr__(self, "request_id", _identifier(self.request_id, field="request_id"))
        object.__setattr__(self, "app_id", _identifier(self.app_id, field="app_id"))
        object.__setattr__(
            self,
            "capability_id",
            _identifier(self.capability_id, field="capability_id"),
        )
        object.__setattr__(
            self,
            "arguments",
            _canonical_mapping(self.arguments, field="arguments"),
        )
        object.__setattr__(
            self,
            "capability_revision",
            _revision(self.capability_revision, field="capability_revision"),
        )
        object.__setattr__(
            self,
            "cancellation_id",
            _identifier(self.cancellation_id, field="cancellation_id"),
        )
        object.__setattr__(
            self,
            "task_id",
            _optional_identifier(self.task_id, field="task_id"),
        )
        object.__setattr__(
            self,
            "input_request_id",
            _optional_identifier(self.input_request_id, field="input_request_id"),
        )
        if self.request_state is not None:
            object.__setattr__(self, "request_state", bytes(self.request_state))

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "app_id": self.app_id,
            "operation": self.operation.value,
            "capability_id": self.capability_id,
            "arguments": _thaw_json(self.arguments),
            "capability_revision": self.capability_revision,
            "deadline_monotonic": self.deadline_monotonic,
            "cancellation_id": self.cancellation_id,
            "task_id": self.task_id,
            "input_request_id": self.input_request_id,
            "request_state_base64": (
                None
                if self.request_state is None
                else base64.b64encode(self.request_state).decode("ascii")
            ),
        }


@dataclass(frozen=True, slots=True)
class McpInputRequest:
    request_id: str
    capability_id: str
    field_ids: tuple[str, ...]
    field_schemas: Mapping[str, Any]
    request_state: bytes = dataclass_field(repr=False)
    round_number: int
    message: str = ""

    def __post_init__(self) -> None:
        if type(self.field_ids) is not tuple:
            raise ValueError("input field_ids must be a tuple")
        if type(self.request_state) is not bytes:
            raise ValueError("input request_state must be bytes")
        object.__setattr__(self, "request_id", _identifier(self.request_id, field="request_id"))
        object.__setattr__(
            self,
            "capability_id",
            _identifier(self.capability_id, field="capability_id"),
        )
        parsed_ids = tuple(_identifier(item, field="input field_id") for item in self.field_ids)
        if len(parsed_ids) != len(set(parsed_ids)):
            raise ValueError("input field_ids are duplicated")
        parsed_ids = tuple(sorted(parsed_ids))
        schemas = _canonical_mapping(self.field_schemas, field="field_schemas")
        if tuple(schemas) != parsed_ids:
            raise ValueError("input field schemas do not match field_ids")
        object.__setattr__(self, "field_ids", parsed_ids)
        object.__setattr__(self, "field_schemas", schemas)
        object.__setattr__(self, "request_state", bytes(self.request_state))
        _positive_int(self.round_number, field="round_number")
        if self.message:
            object.__setattr__(self, "message", _plain_string(self.message, field="message"))
        elif type(self.message) is not str:
            raise ValueError("message must be an NFC plain string")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "capability_id": self.capability_id,
            "field_ids": list(self.field_ids),
            "field_schemas": _thaw_json(self.field_schemas),
            "request_state_base64": base64.b64encode(self.request_state).decode("ascii"),
            "round_number": self.round_number,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class McpTaskHandle:
    task_id: str
    app_id: str
    capability_id: str
    capability_revision: str
    connection_revision: str
    authorization_context_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _identifier(self.task_id, field="task_id"))
        object.__setattr__(self, "app_id", _identifier(self.app_id, field="app_id"))
        object.__setattr__(
            self,
            "capability_id",
            _identifier(self.capability_id, field="capability_id"),
        )
        for field in (
            "capability_revision",
            "connection_revision",
            "authorization_context_revision",
        ):
            object.__setattr__(
                self,
                field,
                _revision(getattr(self, field), field=field),
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "app_id": self.app_id,
            "capability_id": self.capability_id,
            "capability_revision": self.capability_revision,
            "connection_revision": self.connection_revision,
            "authorization_context_revision": self.authorization_context_revision,
        }


class McpClientError(Exception):
    """A stable failure carrying no raw cause or external content."""

    __slots__ = ("_frozen", "_reason")

    def __init__(self, reason: McpClientErrorReason) -> None:
        if type(reason) is not McpClientErrorReason:
            raise ValueError("mcp client error reason is invalid")
        object.__setattr__(self, "_reason", reason)
        super().__init__(reason.value)
        object.__setattr__(self, "_frozen", True)

    #: The interpreter's own exception machinery. `contextlib.__exit__` assigns
    #: ``value.__traceback__`` while unwinding, and anyio's cancel scopes do the
    #: same, so freezing these turned every failure that crossed a generator
    #: context manager into ``AttributeError: McpClientError is immutable`` --
    #: destroying the reason it was carrying. Live 2026-09-07: Miro and Stripe
    #: reported `AttributeError` at the HUD instead of why they refused.
    #:
    #: Nothing is given up by allowing them. ``raise X from Y`` and implicit
    #: chaining set ``__cause__``/``__context__`` through C slots that never
    #: reach ``__setattr__``, so the freeze was not preventing a raw cause from
    #: being attached -- only breaking the unwind. What it does protect, and
    #: still protects, is this error's own ``_reason``.
    _EXCEPTION_MACHINERY = frozenset(
        {
            "__traceback__",
            "__context__",
            "__cause__",
            "__suppress_context__",
            "__notes__",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name not in McpClientError._EXCEPTION_MACHINERY and getattr(
            self, "_frozen", False
        ):
            raise AttributeError("McpClientError is immutable")
        super().__setattr__(name, value)

    def __getattribute__(self, name: str) -> object:
        if name == "__dict__":
            raise AttributeError("McpClientError has no instance dictionary")
        return super().__getattribute__(name)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError("McpClientError is immutable")
        super().__delattr__(name)

    @property
    def reason(self) -> McpClientErrorReason:
        return self._reason

    def __repr__(self) -> str:
        return f"McpClientError(reason={self.reason.value!r})"

    def to_dict(self) -> dict[str, str]:
        return {"reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class McpOperationResult:
    request_id: str
    app_id: str
    capability_id: str
    capability_revision: str
    status: McpOperationStatus
    data: Mapping[str, Any]
    input_request: McpInputRequest | None
    task_handle: McpTaskHandle | None
    error: McpClientError | None

    def __post_init__(self) -> None:
        if type(self.status) is not McpOperationStatus:
            raise ValueError("operation result status is invalid")
        object.__setattr__(self, "request_id", _identifier(self.request_id, field="request_id"))
        object.__setattr__(self, "app_id", _identifier(self.app_id, field="app_id"))
        object.__setattr__(
            self,
            "capability_id",
            _identifier(self.capability_id, field="capability_id"),
        )
        object.__setattr__(
            self,
            "capability_revision",
            _revision(self.capability_revision, field="capability_revision"),
        )
        object.__setattr__(self, "data", _canonical_mapping(self.data, field="result data"))
        if self.input_request is not None and type(self.input_request) is not McpInputRequest:
            raise ValueError("operation result status payload is invalid")
        if self.task_handle is not None and type(self.task_handle) is not McpTaskHandle:
            raise ValueError("operation result status payload is invalid")
        if self.error is not None and type(self.error) is not McpClientError:
            raise ValueError("operation result status payload is invalid")
        if self.task_handle is not None and (
            self.task_handle.app_id != self.app_id
            or self.task_handle.capability_id != self.capability_id
            or self.task_handle.capability_revision != self.capability_revision
        ):
            raise ValueError("operation result authority identity is invalid")
        if (
            self.input_request is not None
            and self.input_request.capability_id != self.capability_id
        ):
            raise ValueError("operation result authority identity is invalid")
        input_valid = self.input_request is not None
        task_valid = self.task_handle is not None
        error_valid = self.error is not None
        data_present = bool(self.data)
        valid = {
            McpOperationStatus.COMPLETED: not input_valid and not task_valid and not error_valid,
            McpOperationStatus.INPUT_REQUIRED: (
                not data_present and input_valid and not task_valid and not error_valid
            ),
            McpOperationStatus.TASK_STARTED: (
                not data_present and not input_valid and task_valid and not error_valid
            ),
            McpOperationStatus.TASK_RECOVERY_REQUIRED: (
                not data_present and not input_valid and task_valid and not error_valid
            ),
            McpOperationStatus.TASK_UPDATED: (
                not data_present and not input_valid and task_valid and not error_valid
            ),
            McpOperationStatus.PROGRESS: (
                data_present and not input_valid and not task_valid and not error_valid
            ),
            McpOperationStatus.NOT_MODIFIED: (
                not data_present and not input_valid and not task_valid and not error_valid
            ),
            McpOperationStatus.FAILED: (
                not data_present and not input_valid and not task_valid and error_valid
            ),
        }[self.status]
        if not valid:
            raise ValueError("operation result status payload is invalid")

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "request_id": self.request_id,
            "app_id": self.app_id,
            "capability_id": self.capability_id,
            "capability_revision": self.capability_revision,
            "status": self.status.value,
        }
        if self.status in {McpOperationStatus.COMPLETED, McpOperationStatus.PROGRESS}:
            document["data"] = _thaw_json(self.data)
        elif self.status is McpOperationStatus.INPUT_REQUIRED:
            assert self.input_request is not None
            document["input_request"] = self.input_request.to_dict()
        elif self.status in {
            McpOperationStatus.TASK_STARTED,
            McpOperationStatus.TASK_RECOVERY_REQUIRED,
            McpOperationStatus.TASK_UPDATED,
        }:
            assert self.task_handle is not None
            document["task_handle"] = self.task_handle.to_dict()
        elif self.status is McpOperationStatus.FAILED:
            assert self.error is not None
            document["error"] = self.error.to_dict()
        return document
