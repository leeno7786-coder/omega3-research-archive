"""Closed, signed-package contracts for Omega MCP servers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Any
from types import MappingProxyType

from core.paths import OMEGA_ROOT
from mcp_platform.canonical import canonical_bytes, sha256_hex, strict_json_value
from mcp_platform.extensions import MCP_APPS_EXTENSION_IDENTIFIER, validate_mcp_apps_metadata

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_SCHEMA_ROOT = OMEGA_ROOT / "schema" / "mcp"


class NetworkClass(StrEnum):
    LOCAL_ONLY = "local_only"
    EXTERNAL = "external"


class RiskClass(StrEnum):
    READ_ONLY = "read_only"
    ADDITIVE_WRITE = "additive_write"
    MUTATING_WRITE = "mutating_write"
    DESTRUCTIVE = "destructive"
    SEND_PUBLISH = "send_publish"
    CROSS_PROVIDER_TRANSFER = "cross_provider_transfer"
    #: A capability discovered from a publisher-verified or personal
    #: connection before any review assigned it a signed risk declaration.
    #: Never produced by Omega's signed manifest pipeline; only
    #: ``mcp_platform.plugin_discovery`` issues this classification, and MCP
    #: annotations reported by the publisher can never upgrade it.
    EXTERNAL_UNREVIEWED = "external_unreviewed"


_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "server_id",
        "publisher_id",
        "server_version",
        "minimum_omega_version",
        "supported_mcp_protocol_versions",
        "network_class",
        "platform",
        "entrypoint",
        "artifact_sha256",
        "tool_catalog_sha256",
        "resource_catalog_sha256",
        "capability_tags",
        "risk_declarations",
        "oauth_providers",
        "allowed_network_destinations",
        "signing_key_id",
        "signature",
    }
)
_MAX_SIGNED_APPS_METADATA_BYTES = 65_536
_ENTRYPOINT_FIELDS = frozenset({"artifact", "arguments"})
_TOOL_CATALOG_FIELDS = frozenset({"catalog_version", "server_id", "server_version", "tools"})
_TOOL_FIELDS = frozenset(
    {
        "name",
        "title",
        "input_schema",
        "output_schema",
        "input_schema_sha256",
        "output_schema_sha256",
        "capability_tags",
        "provider_extension_tags",
        "risk_class",
        "effect",
        "requires_revision",
        "max_result_bytes",
        "allowed_mime_types",
    }
)
_RESOURCE_CATALOG_FIELDS = frozenset(
    {"catalog_version", "server_id", "server_version", "resources", "resource_templates"}
)
_RESOURCE_FIELDS = frozenset({"uri", "name", "mime_type", "max_bytes", "capability_tags"})
_RESOURCE_TEMPLATE_FIELDS = frozenset(
    {"uri_template", "name", "mime_type", "max_bytes", "capability_tags"}
)
_HEX = frozenset("0123456789abcdef")
_CAMEL_ACRONYM_BOUNDARY_RE = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_WORD_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SECRET_ARGUMENT_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{3,}", re.IGNORECASE),
    re.compile(r"ya29\.[A-Za-z0-9._-]{8,}"),
    re.compile(r"1//[A-Za-z0-9_-]{12,}"),
    re.compile("GOCS" + r"PX-[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_SECRET_ARGUMENT_DIRECT_WORDS = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "passphrase",
        "passphrases",
        "password",
        "passwords",
    }
)
_SECRET_ARGUMENT_TOKEN_WORDS = frozenset({"secret", "secrets", "token", "tokens"})
_SECRET_ARGUMENT_KEY_WORDS = frozenset({"key", "keys"})
_SECRET_ARGUMENT_SCOPE_WORDS = frozenset(
    {
        "access",
        "api",
        "auth",
        "authorization",
        "bearer",
        "client",
        "id",
        "oauth",
        "private",
        "provider",
        "refresh",
        "session",
        "signing",
        "vault",
    }
)
_SECRET_ARGUMENT_COMPACT_MATERIAL_WORDS = frozenset(
    {
        "credential",
        "credentials",
        "key",
        "keys",
        "passphrase",
        "passphrases",
        "password",
        "passwords",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    if any(type(key) is not str for key in value):
        raise ValueError(f"{label} field names must be strings")
    return value


def _exact_fields(value: dict[str, Any], expected: frozenset[str], *, label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{label} has missing fields: {', '.join(sorted(missing))}")


def _stable_string(value: Any, *, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty stable string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{field} must be a list")
    parsed = tuple(_stable_string(item, field=f"{field} item") for item in value)
    if len(parsed) != len(set(parsed)):
        raise ValueError(f"{field} cannot contain duplicates")
    return parsed


def _semantic_argument_words(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CAMEL_ACRONYM_BOUNDARY_RE.sub(" ", normalized)
    normalized = _CAMEL_WORD_BOUNDARY_RE.sub(" ", normalized)
    separated = "".join(
        character.casefold() if character.isascii() and character.isalnum() else " "
        for character in normalized
    )
    return tuple(separated.split())


def _compact_credential_label(words: tuple[str, ...]) -> bool:
    compact = "".join(words)
    for material in _SECRET_ARGUMENT_COMPACT_MATERIAL_WORDS:
        if not compact.endswith(material):
            continue
        prefix = compact[: -len(material)]
        if any(scope in prefix for scope in _SECRET_ARGUMENT_SCOPE_WORDS):
            return True
    return False


def _credential_argument_label(value: str) -> bool:
    label = re.split(r"[:=]", value, maxsplit=1)[0].lstrip("-/")
    words = _semantic_argument_words(label)
    if not words:
        return False
    word_set = set(words)
    if _compact_credential_label(words):
        return True
    if word_set.intersection(_SECRET_ARGUMENT_DIRECT_WORDS):
        return True
    scoped = bool(word_set.intersection(_SECRET_ARGUMENT_SCOPE_WORDS))
    if word_set.intersection(_SECRET_ARGUMENT_TOKEN_WORDS):
        return len(words) == 1 or scoped
    return bool(word_set.intersection(_SECRET_ARGUMENT_KEY_WORDS)) and scoped


def _entrypoint_arguments(value: Any) -> tuple[str, ...]:
    arguments = _string_list(value, field="entrypoint.arguments")
    if any(
        _credential_argument_label(argument)
        or any(
            pattern.search(unicodedata.normalize("NFKC", argument)) is not None
            for pattern in _SECRET_ARGUMENT_VALUE_PATTERNS
        )
        for argument in arguments
    ):
        raise ValueError("entrypoint_arguments_secret_bearing")
    return arguments


def _digest(value: Any, *, field: str) -> str:
    digest = _stable_string(value, field=field)
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _package_path(value: Any, *, field: str) -> str:
    artifact = _stable_string(value, field=field)
    path = PureWindowsPath(artifact)
    if path.is_absolute() or path.drive or path.root or ".." in path.parts:
        raise ValueError(f"{field} must be a relative package path")
    return artifact


def _mcp_uri(value: Any, *, field: str) -> str:
    uri = _stable_string(value, field=field)
    if not uri.startswith("omega://"):
        raise ValueError(f"{field} must use the omega URI scheme")
    return uri


def _risk_class(value: Any, *, field: str) -> RiskClass:
    try:
        return RiskClass(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a declared risk class") from exc


def _freeze_json(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class McpPackageManifest:
    manifest_version: int
    server_id: str
    publisher_id: str
    server_version: str
    minimum_omega_version: str
    supported_mcp_protocol_versions: tuple[str, ...]
    network_class: NetworkClass
    platform: str
    artifact: str
    arguments: tuple[str, ...]
    artifact_sha256: str
    tool_catalog_sha256: str
    resource_catalog_sha256: str
    capability_tags: tuple[str, ...]
    risk_declarations: tuple[RiskClass, ...]
    oauth_providers: tuple[str, ...]
    allowed_network_destinations: tuple[str, ...]
    signing_key_id: str
    signature: str
    mcp_apps_metadata: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> McpPackageManifest:
        data = _object(raw, label="MCP package manifest")
        expected_fields = _MANIFEST_FIELDS | (
            {"mcp_apps_metadata"} if "mcp_apps_metadata" in data else set()
        )
        _exact_fields(data, frozenset(expected_fields), label="MCP package manifest")
        if type(data["manifest_version"]) is not int or data["manifest_version"] != 1:
            raise ValueError("manifest_version must be 1")
        protocols = _string_list(
            data["supported_mcp_protocol_versions"],
            field="supported_mcp_protocol_versions",
        )
        if protocols != (MCP_PROTOCOL_VERSION,):
            raise ValueError(
                f"supported_mcp_protocol_versions must be [{MCP_PROTOCOL_VERSION}] MCP protocol"
            )
        try:
            network_class = NetworkClass(data["network_class"])
        except ValueError as exc:
            raise ValueError("network_class must be a declared network class") from exc
        entrypoint = _object(data["entrypoint"], label="entrypoint")
        _exact_fields(entrypoint, _ENTRYPOINT_FIELDS, label="entrypoint")
        risk_declarations = tuple(
            _risk_class(item, field="risk_declarations item")
            for item in _string_list(data["risk_declarations"], field="risk_declarations")
        )
        if not risk_declarations:
            raise ValueError("risk_declarations must not be empty")
        oauth_providers = _string_list(data["oauth_providers"], field="oauth_providers")
        destinations = _string_list(
            data["allowed_network_destinations"],
            field="allowed_network_destinations",
        )
        if network_class is NetworkClass.LOCAL_ONLY and (oauth_providers or destinations):
            raise ValueError("local_only packages cannot declare OAuth or network destinations")
        apps_metadata: Mapping[str, Any] | None = None
        if "mcp_apps_metadata" in data:
            candidate = data["mcp_apps_metadata"]
            if not isinstance(candidate, Mapping) or set(candidate) != {
                "tools",
                "resources",
                "resource_templates",
            }:
                raise ValueError("MCP Apps metadata authority is invalid")
            if any(not isinstance(candidate[group], Mapping) for group in candidate):
                raise ValueError("MCP Apps metadata authority is invalid")
            validated = validate_mcp_apps_metadata(
                candidate,
                signed_metadata=candidate,
                declared_extensions={MCP_APPS_EXTENSION_IDENTIFIER: "signed"},
                max_bytes=_MAX_SIGNED_APPS_METADATA_BYTES,
            )
            apps_metadata = _freeze_json(validated)
        return cls(
            manifest_version=1,
            server_id=_stable_string(data["server_id"], field="server_id"),
            publisher_id=_stable_string(data["publisher_id"], field="publisher_id"),
            server_version=_stable_string(data["server_version"], field="server_version"),
            minimum_omega_version=_stable_string(
                data["minimum_omega_version"], field="minimum_omega_version"
            ),
            supported_mcp_protocol_versions=protocols,
            network_class=network_class,
            platform=_stable_string(data["platform"], field="platform"),
            artifact=_package_path(entrypoint["artifact"], field="entrypoint.artifact"),
            arguments=_entrypoint_arguments(entrypoint["arguments"]),
            artifact_sha256=_digest(data["artifact_sha256"], field="artifact_sha256"),
            tool_catalog_sha256=_digest(data["tool_catalog_sha256"], field="tool_catalog_sha256"),
            resource_catalog_sha256=_digest(
                data["resource_catalog_sha256"], field="resource_catalog_sha256"
            ),
            capability_tags=_string_list(data["capability_tags"], field="capability_tags"),
            risk_declarations=risk_declarations,
            oauth_providers=oauth_providers,
            allowed_network_destinations=destinations,
            signing_key_id=_stable_string(data["signing_key_id"], field="signing_key_id"),
            signature=_stable_string(data["signature"], field="signature"),
            mcp_apps_metadata=apps_metadata,
        )

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "manifest_version": self.manifest_version,
            "server_id": self.server_id,
            "publisher_id": self.publisher_id,
            "server_version": self.server_version,
            "minimum_omega_version": self.minimum_omega_version,
            "supported_mcp_protocol_versions": list(self.supported_mcp_protocol_versions),
            "network_class": self.network_class.value,
            "platform": self.platform,
            "entrypoint": {"artifact": self.artifact, "arguments": list(self.arguments)},
            "artifact_sha256": self.artifact_sha256,
            "tool_catalog_sha256": self.tool_catalog_sha256,
            "resource_catalog_sha256": self.resource_catalog_sha256,
            "capability_tags": list(self.capability_tags),
            "risk_declarations": [risk.value for risk in self.risk_declarations],
            "oauth_providers": list(self.oauth_providers),
            "allowed_network_destinations": list(self.allowed_network_destinations),
            "signing_key_id": self.signing_key_id,
            "signature": self.signature,
        }
        if self.mcp_apps_metadata is not None:
            document["mcp_apps_metadata"] = _thaw_json(self.mcp_apps_metadata)
        return document

    def unsigned_dict(self) -> dict[str, object]:
        """Return the exact manifest representation covered by its signature."""
        document = self.to_dict()
        del document["signature"]
        return document

    def signing_bytes(self) -> bytes:
        """Return RFC 8785 bytes for signing, with the signature omitted."""
        return canonical_bytes(self.unsigned_dict())


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    title: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    input_schema_sha256: str
    output_schema_sha256: str
    capability_tags: tuple[str, ...]
    provider_extension_tags: tuple[str, ...]
    risk_class: RiskClass
    effect: str
    requires_revision: bool
    max_result_bytes: int
    allowed_mime_types: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> ToolDefinition:
        data = _object(raw, label="tool")
        _exact_fields(data, _TOOL_FIELDS, label="tool")
        input_schema = _object(data["input_schema"], label="tool input_schema")
        output_schema = _object(data["output_schema"], label="tool output_schema")
        input_digest = _digest(data["input_schema_sha256"], field="input_schema_sha256")
        output_digest = _digest(data["output_schema_sha256"], field="output_schema_sha256")
        if input_digest != sha256_hex(canonical_bytes(input_schema)):
            raise ValueError("input_schema_sha256 does not match canonical input_schema")
        if output_digest != sha256_hex(canonical_bytes(output_schema)):
            raise ValueError("output_schema_sha256 does not match canonical output_schema")
        if type(data["requires_revision"]) is not bool:
            raise ValueError("requires_revision must be boolean")
        return cls(
            name=_stable_string(data["name"], field="tool name"),
            title=_stable_string(data["title"], field="tool title"),
            input_schema=_freeze_json(strict_json_value(input_schema)),
            output_schema=_freeze_json(strict_json_value(output_schema)),
            input_schema_sha256=input_digest,
            output_schema_sha256=output_digest,
            capability_tags=_string_list(data["capability_tags"], field="capability_tags"),
            provider_extension_tags=_string_list(
                data["provider_extension_tags"], field="provider_extension_tags"
            ),
            risk_class=_risk_class(data["risk_class"], field="risk_class"),
            effect=_stable_string(data["effect"], field="effect"),
            requires_revision=data["requires_revision"],
            max_result_bytes=_positive_int(data["max_result_bytes"], field="max_result_bytes"),
            allowed_mime_types=_string_list(data["allowed_mime_types"], field="allowed_mime_types"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "title": self.title,
            "input_schema": _thaw_json(self.input_schema),
            "output_schema": _thaw_json(self.output_schema),
            "input_schema_sha256": self.input_schema_sha256,
            "output_schema_sha256": self.output_schema_sha256,
            "capability_tags": list(self.capability_tags),
            "provider_extension_tags": list(self.provider_extension_tags),
            "risk_class": self.risk_class.value,
            "effect": self.effect,
            "requires_revision": self.requires_revision,
            "max_result_bytes": self.max_result_bytes,
            "allowed_mime_types": list(self.allowed_mime_types),
        }


@dataclass(frozen=True)
class ToolCatalog:
    catalog_version: int
    server_id: str
    server_version: str
    tools: tuple[ToolDefinition, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> ToolCatalog:
        data = _object(raw, label="tool catalog")
        _exact_fields(data, _TOOL_CATALOG_FIELDS, label="tool catalog")
        if type(data["catalog_version"]) is not int or data["catalog_version"] != 1:
            raise ValueError("catalog_version must be 1")
        if type(data["tools"]) is not list:
            raise ValueError("tools must be a list")
        tools = tuple(ToolDefinition.from_dict(item) for item in data["tools"])
        names = tuple(tool.name for tool in tools)
        if len(names) != len(set(names)):
            raise ValueError("tools cannot contain duplicate names")
        return cls(
            catalog_version=1,
            server_id=_stable_string(data["server_id"], field="server_id"),
            server_version=_stable_string(data["server_version"], field="server_version"),
            tools=tools,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog_version": self.catalog_version,
            "server_id": self.server_id,
            "server_version": self.server_version,
            "tools": [tool.to_dict() for tool in self.tools],
        }


@dataclass(frozen=True)
class ResourceDefinition:
    uri: str
    name: str
    mime_type: str
    max_bytes: int
    capability_tags: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> ResourceDefinition:
        data = _object(raw, label="resource")
        _exact_fields(data, _RESOURCE_FIELDS, label="resource")
        return cls(
            uri=_mcp_uri(data["uri"], field="resource uri"),
            name=_stable_string(data["name"], field="resource name"),
            mime_type=_stable_string(data["mime_type"], field="resource mime_type"),
            max_bytes=_positive_int(data["max_bytes"], field="resource max_bytes"),
            capability_tags=_string_list(data["capability_tags"], field="capability_tags"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "name": self.name,
            "mime_type": self.mime_type,
            "max_bytes": self.max_bytes,
            "capability_tags": list(self.capability_tags),
        }


@dataclass(frozen=True)
class ResourceTemplateDefinition:
    uri_template: str
    name: str
    mime_type: str
    max_bytes: int
    capability_tags: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> ResourceTemplateDefinition:
        data = _object(raw, label="resource template")
        _exact_fields(data, _RESOURCE_TEMPLATE_FIELDS, label="resource template")
        return cls(
            uri_template=_mcp_uri(data["uri_template"], field="resource uri_template"),
            name=_stable_string(data["name"], field="resource template name"),
            mime_type=_stable_string(data["mime_type"], field="resource template mime_type"),
            max_bytes=_positive_int(data["max_bytes"], field="resource template max_bytes"),
            capability_tags=_string_list(data["capability_tags"], field="capability_tags"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "uri_template": self.uri_template,
            "name": self.name,
            "mime_type": self.mime_type,
            "max_bytes": self.max_bytes,
            "capability_tags": list(self.capability_tags),
        }


@dataclass(frozen=True)
class ResourceCatalog:
    catalog_version: int
    server_id: str
    server_version: str
    resources: tuple[ResourceDefinition, ...]
    resource_templates: tuple[ResourceTemplateDefinition, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> ResourceCatalog:
        data = _object(raw, label="resource catalog")
        _exact_fields(data, _RESOURCE_CATALOG_FIELDS, label="resource catalog")
        if type(data["catalog_version"]) is not int or data["catalog_version"] != 1:
            raise ValueError("catalog_version must be 1")
        if type(data["resources"]) is not list:
            raise ValueError("resources must be a list")
        if type(data["resource_templates"]) is not list:
            raise ValueError("resource_templates must be a list")
        resources = tuple(ResourceDefinition.from_dict(item) for item in data["resources"])
        templates = tuple(
            ResourceTemplateDefinition.from_dict(item) for item in data["resource_templates"]
        )
        uris = tuple(resource.uri for resource in resources)
        templates_uris = tuple(template.uri_template for template in templates)
        if len(uris) != len(set(uris)):
            raise ValueError("resources cannot contain duplicate URIs")
        if len(templates_uris) != len(set(templates_uris)):
            raise ValueError("resource_templates cannot contain duplicate URI templates")
        return cls(
            catalog_version=1,
            server_id=_stable_string(data["server_id"], field="server_id"),
            server_version=_stable_string(data["server_version"], field="server_version"),
            resources=resources,
            resource_templates=templates,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog_version": self.catalog_version,
            "server_id": self.server_id,
            "server_version": self.server_version,
            "resources": [resource.to_dict() for resource in self.resources],
            "resource_templates": [template.to_dict() for template in self.resource_templates],
        }
