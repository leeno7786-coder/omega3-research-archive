"""Immutable, closed contracts for signed declarative plugin manifests."""

from __future__ import annotations

import posixpath
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version

from mcp_platform.canonical import canonical_bytes

PLUGIN_PROTOCOL_VERSION = "2026-07-28"
_TASKS_EXTENSION_IDENTIFIER = "io.modelcontextprotocol/tasks"


class PluginKind(StrEnum):
    SKILL_ONLY = "skill_only"
    MCP_BACKED = "mcp_backed"
    COMPOSITE = "composite"
    APP_ONLY = "app_only"


class AssetKind(StrEnum):
    SKILL = "skill"
    WORKFLOW = "workflow"
    ICON = "icon"
    CAPABILITY_AUTHORITY = "capability_authority"


class CapabilityKind(StrEnum):
    TOOL = "tool"
    PROMPT = "prompt"
    RESOURCE = "resource"
    EXTENSION = "extension"


class McpTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class AppDeployment(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class PluginAuthenticationKind(StrEnum):
    NONE = "none"
    OAUTH_AUTHORIZATION_CODE_PKCE = "oauth_authorization_code_pkce"
    OAUTH_DEVICE_CODE = "oauth_device_code"
    OAUTH_CLIENT_METADATA_DOCUMENT = "oauth_client_metadata_document"
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    BASIC = "basic"
    DECLARED_SECRET_FIELDS = "declared_secret_fields"


_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "plugin_id",
        "publisher_id",
        "plugin_version",
        "minimum_omega_version",
        "kind",
        "title",
        "description",
        "assets",
        "entry_points",
        "starter_requests",
        "required_apps",
        "optional_apps",
        "schedule_template_paths",
        "proactive_event_rules",
        "enterprise_roles",
        "signing_key_id",
        "signature",
    }
)
_CAPABILITY_FIELDS = frozenset({"kind", "name", "schema_sha256", "version"})
_APP_FIELDS = frozenset(
    {
        "app_id",
        "publisher_id",
        "deployment",
        "provider_id",
        "protocol_version",
        "transports",
        "remote_origin",
        "authorization_issuer",
        "authentication",
        "capabilities",
    }
)
_AUTHENTICATION_FIELDS = frozenset({"kind", "scopes", "secret_fields"})
_SECRET_FIELD_FIELDS = frozenset({"field_id", "label", "secret", "required"})
_ASSET_FIELDS = frozenset({"path", "kind", "media_type", "sha256"})
_ENTRY_POINT_FIELDS = frozenset({"entry_point_id", "title", "skill_path", "required_app_ids"})
_ROLE_FIELDS = frozenset({"role_id", "entry_point_ids"})
_PROACTIVE_RULE_FIELDS = frozenset({"event_class", "presentation", "acknowledgement_required"})
_HEX = frozenset("0123456789abcdef")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_ASSET_MEDIA_TYPES = {
    AssetKind.SKILL: frozenset({"text/markdown"}),
    AssetKind.WORKFLOW: frozenset({"application/json"}),
    AssetKind.ICON: frozenset({"image/png"}),
    AssetKind.CAPABILITY_AUTHORITY: frozenset({"application/json"}),
}
_REMOTE_ORIGIN_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~-]+)+/?\Z")
_RESERVED_PATH_SEGMENTS = frozenset({".", ".."})
_PROACTIVE_EVENT_CLASSES = frozenset({"task_progress", "task_terminal"})
_MAX_SECRET_FIELD_LABEL_CHARS = 160
_CANONICAL_HOST_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z"
)
#: A trailing slash is part of the issuer, not noise. RFC 8414 compares an
#: issuer by EXACT string, and vendors publish one: Netlify's is
#: "https://netlify-mcp.netlify.app/" and Miro's "https://mcp.miro.com/".
#: Refusing that shape meant Omega could not even describe those providers,
#: let alone match their metadata -- the same mistake as the endpoint path.
_CANONICAL_ISSUER_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~-]+)*/?\Z")


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
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must be a canonical stable string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must be a stable string without control characters")
    return value


def _identifier(value: Any, *, field: str) -> str:
    identifier = _stable_string(value, field=field)
    if _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError(f"{field} is invalid")
    return identifier


def parse_app_id(value: Any) -> str:
    """Validate one public app identity without fabricating an app contract."""
    return _identifier(value, field="plugin app_id")


def _capability_label(value: Any, *, field: str) -> str:
    """A human-readable capability name: bounded, plain, no control characters."""
    label = _stable_string(value, field=field)
    if len(label) > 120:
        raise ValueError(f"{field} is invalid")
    return label


def _extension_name(value: Any, *, field: str) -> str:
    name = _stable_string(value, field=field)
    segments = name.split("/")
    if len(segments) != 2:
        raise ValueError(f"{field} is invalid")
    _identifier(segments[0], field=field)
    _identifier(segments[1], field=field)
    return name


def _semver(value: Any, *, field: str) -> str:
    version = _stable_string(value, field=field)
    if _SEMVER_RE.fullmatch(version) is None:
        raise ValueError(f"{field} is not exact SemVer")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"{field} is not exact SemVer") from exc
    return version


def _digest(value: Any, *, field: str) -> str:
    digest = _stable_string(value, field=field)
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _string_list(value: Any, *, field: str, identifiers: bool = False) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{field} must be a list")
    parser = _identifier if identifiers else _stable_string
    parsed = tuple(parser(item, field=f"{field} item") for item in value)
    if len(parsed) != len(set(parsed)):
        raise ValueError(f"{field} cannot contain duplicates")
    return parsed


def _enum(value: Any, enum: type[StrEnum], *, field: str, message: str) -> StrEnum:
    try:
        return enum(value)
    except ValueError as exc:
        raise ValueError(message) from exc


def _package_path(value: Any, *, field: str) -> str:
    path = _stable_string(value, field=field)
    windows_path = PureWindowsPath(path)
    if (
        "\\" in path
        or ":" in path
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or windows_path.is_reserved()
        or ".." in windows_path.parts
        or "." in windows_path.parts
    ):
        raise ValueError(f"{field} is invalid")
    return path


def _canonical_https_authority(parsed: Any) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if (
        hostname is None
        or _CANONICAL_HOST_RE.fullmatch(hostname) is None
        or port == 443
        or port == 0
    ):
        return False
    expected_netloc = hostname if port is None else f"{hostname}:{port}"
    return parsed.netloc == expected_netloc


def _https_origin(value: Any, *, field: str) -> str:
    origin = _stable_string(value, field=field)
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not _canonical_https_authority(parsed)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != origin
    ):
        raise ValueError(f"{field} must be a canonical HTTPS origin")
    path = parsed.path
    if path and (
        _REMOTE_ORIGIN_PATH_RE.fullmatch(path) is None
        or _RESERVED_PATH_SEGMENTS & set(path.strip("/").split("/"))
    ):
        raise ValueError(f"{field} must be a canonical HTTPS origin")
    return origin


def _https_issuer(value: Any, *, field: str) -> str:
    issuer = _stable_string(value, field=field)
    parsed = urlsplit(issuer)
    if (
        parsed.scheme != "https"
        or not _canonical_https_authority(parsed)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _CANONICAL_ISSUER_PATH_RE.fullmatch(parsed.path) is None
        # A ".." segment matches the path grammar's character class and always
        # did. It must not: `_metadata_url` inserts the well-known prefix ahead
        # of this path, and an HTTP client resolves dot segments, so a traversal
        # here would address a document outside the well-known space.
        or posixpath.normpath(parsed.path or "/") != (parsed.path.rstrip("/") or "/")
        or parsed.geturl() != issuer
    ):
        raise ValueError(f"{field} must be a canonical HTTPS authorization issuer")
    return issuer


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    kind: CapabilityKind
    name: str
    schema_sha256: str | None
    version: str | None

    def __post_init__(self) -> None:
        if type(self.kind) is not CapabilityKind:
            raise ValueError("plugin capability kind is invalid")
        if self.kind is CapabilityKind.EXTENSION:
            name = _extension_name(self.name, field="plugin capability name")
        elif self.kind is CapabilityKind.RESOURCE:
            # MCP calls a resource's `name` "a human-readable name", and real
            # servers mean it: Slack's is "Send Message Input Form". A tool name
            # is a programmatic identifier because the planner calls it; a
            # resource name is a label, and demanding identifier shape made any
            # provider whose resources got enumerated unusable. Uniqueness is
            # still enforced where it matters -- `_ensure_unique` fails closed
            # on a duplicate rather than merging two resources under one key.
            name = _capability_label(self.name, field="plugin capability name")
            # The Tasks extension identity stays reserved to the extension
            # kind. Loosening resource names to labels is what lets a provider
            # call one "Send Message Input Form"; it must not also let one
            # claim a name the protocol has already spoken for.
            if name == _TASKS_EXTENSION_IDENTIFIER:
                raise ValueError("plugin capability name is invalid")
        else:
            name = _identifier(self.name, field="plugin capability name")
        schema_sha256 = self.schema_sha256
        if schema_sha256 is not None:
            schema_sha256 = _digest(
                schema_sha256,
                field="plugin capability schema_sha256",
            )
        if self.kind is CapabilityKind.EXTENSION:
            if name == _TASKS_EXTENSION_IDENTIFIER:
                if self.version is not None:
                    raise ValueError("plugin Tasks extension version must be null")
                version = None
            else:
                version = _semver(self.version, field="plugin extension version")
        elif self.version is not None:
            raise ValueError("plugin non-extension capability version is invalid")
        else:
            version = None
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "schema_sha256", schema_sha256)
        object.__setattr__(self, "version", version)

    @classmethod
    def from_dict(cls, raw: Any) -> CapabilityRequirement:
        data = _object(raw, label="plugin capability")
        _exact_fields(data, _CAPABILITY_FIELDS, label="plugin capability")
        kind = _enum(
            data["kind"],
            CapabilityKind,
            field="plugin capability kind",
            message="plugin capability kind is invalid",
        )
        schema_sha256 = data["schema_sha256"]
        if schema_sha256 is not None:
            schema_sha256 = _digest(schema_sha256, field="plugin capability schema_sha256")
        return cls(
            kind=kind,
            name=data["name"],
            schema_sha256=schema_sha256,
            version=data["version"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "schema_sha256": self.schema_sha256,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class PluginSecretField:
    field_id: str
    label: str
    secret: bool
    required: bool

    @classmethod
    def from_dict(cls, raw: Any) -> PluginSecretField:
        data = _object(raw, label="plugin authentication secret field")
        _exact_fields(
            data,
            _SECRET_FIELD_FIELDS,
            label="plugin authentication secret field",
        )
        label = _stable_string(
            data["label"],
            field="plugin authentication secret field label",
        )
        if len(label) > _MAX_SECRET_FIELD_LABEL_CHARS:
            raise ValueError("plugin authentication secret field label is invalid")
        if type(data["secret"]) is not bool or type(data["required"]) is not bool:
            raise ValueError("plugin authentication secret field flags are invalid")
        return cls(
            field_id=_identifier(
                data["field_id"],
                field="plugin authentication secret field_id",
            ),
            label=label,
            secret=data["secret"],
            required=data["required"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "label": self.label,
            "secret": self.secret,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class PluginAuthentication:
    kind: PluginAuthenticationKind
    scopes: tuple[str, ...]
    secret_fields: tuple[PluginSecretField, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> PluginAuthentication:
        data = _object(raw, label="plugin authentication")
        _exact_fields(data, _AUTHENTICATION_FIELDS, label="plugin authentication")
        kind = _enum(
            data["kind"],
            PluginAuthenticationKind,
            field="plugin authentication kind",
            message="plugin authentication kind is invalid",
        )
        scopes = _string_list(data["scopes"], field="plugin authentication scopes")
        secret_fields = _parse_list(
            data["secret_fields"],
            PluginSecretField.from_dict,
            field="plugin authentication secret_fields",
        )
        _no_duplicate(
            tuple(item.field_id for item in secret_fields),
            "plugin authentication secret field identity is duplicated",
        )
        oauth_kinds = {
            PluginAuthenticationKind.OAUTH_AUTHORIZATION_CODE_PKCE,
            PluginAuthenticationKind.OAUTH_DEVICE_CODE,
            PluginAuthenticationKind.OAUTH_CLIENT_METADATA_DOCUMENT,
        }
        if kind in oauth_kinds:
            if not scopes or secret_fields:
                raise ValueError("plugin OAuth authentication scopes or fields are invalid")
        elif scopes:
            raise ValueError("plugin authentication scopes are invalid")
        if kind is PluginAuthenticationKind.NONE and secret_fields:
            raise ValueError("plugin none authentication fields are invalid")
        expected_fields: dict[
            PluginAuthenticationKind,
            tuple[tuple[str, bool, bool], ...],
        ] = {
            PluginAuthenticationKind.API_KEY: (("api_key", True, True),),
            PluginAuthenticationKind.BEARER_TOKEN: (("token", True, True),),
            PluginAuthenticationKind.BASIC: (
                ("username", False, True),
                ("password", True, True),
            ),
        }
        expected = expected_fields.get(kind)
        if expected is not None:
            actual = tuple(
                (item.field_id, item.secret, item.required) for item in secret_fields
            )
            if actual != expected:
                raise ValueError("plugin authentication secret fields are invalid")
        if kind is PluginAuthenticationKind.DECLARED_SECRET_FIELDS and not secret_fields:
            raise ValueError("plugin declared authentication secret fields are invalid")
        if kind in {PluginAuthenticationKind.NONE, *oauth_kinds} and secret_fields:
            raise ValueError("plugin authentication secret fields are invalid")
        return cls(kind=kind, scopes=scopes, secret_fields=secret_fields)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "scopes": list(self.scopes),
            "secret_fields": [item.to_dict() for item in self.secret_fields],
        }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class AppRequirement:
    app_id: str
    publisher_id: str
    deployment: AppDeployment
    provider_id: str | None
    protocol_version: str
    transports: tuple[McpTransport, ...]
    remote_origin: str | None
    authorization_issuer: str | None
    authentication: PluginAuthentication
    capabilities: tuple[CapabilityRequirement, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> AppRequirement:
        data = _object(raw, label="plugin app")
        _exact_fields(data, _APP_FIELDS, label="plugin app")
        deployment = _enum(
            data["deployment"],
            AppDeployment,
            field="plugin app deployment",
            message="plugin app deployment is invalid",
        )
        if data["protocol_version"] != PLUGIN_PROTOCOL_VERSION:
            raise ValueError("plugin app protocol is invalid")
        if type(data["transports"]) is not list:
            raise ValueError("plugin app transports must be a list")
        transports = tuple(
            _enum(
                item,
                McpTransport,
                field="plugin app transport",
                message="plugin app transport is invalid",
            )
            for item in data["transports"]
        )
        if not transports or len(transports) != len(set(transports)):
            raise ValueError("plugin app transports are invalid")
        if deployment is AppDeployment.LOCAL:
            if data["remote_origin"] is not None or data["authorization_issuer"] is not None:
                raise ValueError("local plugin app remote identity is invalid")
            remote_origin = None
            authorization_issuer = None
        else:
            remote_origin = _https_origin(data["remote_origin"], field="plugin app remote_origin")
            authorization_issuer = _https_issuer(
                data["authorization_issuer"], field="plugin app authorization_issuer"
            )
            if McpTransport.STREAMABLE_HTTP not in transports:
                raise ValueError("remote plugin app transport is invalid")
        provider_id = data["provider_id"]
        if provider_id is not None:
            provider_id = _identifier(provider_id, field="plugin app provider_id")
        if type(data["capabilities"]) is not list:
            raise ValueError("plugin app capabilities must be a list")
        capabilities = tuple(CapabilityRequirement.from_dict(item) for item in data["capabilities"])
        identities = tuple((item.kind, item.name) for item in capabilities)
        if len(identities) != len(set(identities)):
            raise ValueError("plugin capability identity is duplicated")
        return cls(
            app_id=_identifier(data["app_id"], field="plugin app_id"),
            publisher_id=_identifier(data["publisher_id"], field="plugin app publisher_id"),
            deployment=deployment,
            provider_id=provider_id,
            protocol_version=PLUGIN_PROTOCOL_VERSION,
            transports=transports,
            remote_origin=remote_origin,
            authorization_issuer=authorization_issuer,
            authentication=PluginAuthentication.from_dict(data["authentication"]),
            capabilities=capabilities,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "publisher_id": self.publisher_id,
            "deployment": self.deployment.value,
            "provider_id": self.provider_id,
            "protocol_version": self.protocol_version,
            "transports": [item.value for item in self.transports],
            "remote_origin": self.remote_origin,
            "authorization_issuer": self.authorization_issuer,
            "authentication": self.authentication.to_dict(),
            "capabilities": [item.to_dict() for item in self.capabilities],
        }


@dataclass(frozen=True)
class PluginAsset:
    path: str
    kind: AssetKind
    media_type: str
    sha256: str

    @classmethod
    def from_dict(cls, raw: Any) -> PluginAsset:
        data = _object(raw, label="plugin asset")
        _exact_fields(data, _ASSET_FIELDS, label="plugin asset")
        kind = _enum(
            data["kind"],
            AssetKind,
            field="plugin asset kind",
            message="plugin asset kind is invalid",
        )
        media_type = _stable_string(data["media_type"], field="plugin asset media type")
        if media_type not in _ASSET_MEDIA_TYPES[kind]:
            raise ValueError("plugin asset media type is invalid")
        return cls(
            path=_package_path(data["path"], field="plugin asset path"),
            kind=kind,
            media_type=media_type,
            sha256=_digest(data["sha256"], field="plugin asset sha256"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "media_type": self.media_type,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class PluginEntryPoint:
    entry_point_id: str
    title: str
    skill_path: str
    required_app_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> PluginEntryPoint:
        data = _object(raw, label="plugin entry point")
        _exact_fields(data, _ENTRY_POINT_FIELDS, label="plugin entry point")
        return cls(
            entry_point_id=_identifier(data["entry_point_id"], field="plugin entry point_id"),
            title=_stable_string(data["title"], field="plugin entry point title"),
            skill_path=_package_path(data["skill_path"], field="plugin entry point skill_path"),
            required_app_ids=_string_list(
                data["required_app_ids"],
                field="plugin entry point required_app_ids",
                identifiers=True,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_point_id": self.entry_point_id,
            "title": self.title,
            "skill_path": self.skill_path,
            "required_app_ids": list(self.required_app_ids),
        }


@dataclass(frozen=True)
class PluginRoleMetadata:
    role_id: str
    entry_point_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> PluginRoleMetadata:
        data = _object(raw, label="plugin enterprise role")
        _exact_fields(data, _ROLE_FIELDS, label="plugin enterprise role")
        return cls(
            role_id=_identifier(data["role_id"], field="plugin enterprise role_id"),
            entry_point_ids=_string_list(
                data["entry_point_ids"], field="plugin enterprise entry_point_ids", identifiers=True
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {"role_id": self.role_id, "entry_point_ids": list(self.entry_point_ids)}


@dataclass(frozen=True)
class ProactiveEventRule:
    event_class: str
    presentation: str
    acknowledgement_required: bool

    @classmethod
    def from_dict(cls, raw: Any) -> ProactiveEventRule:
        data = _object(raw, label="plugin proactive event rule")
        _exact_fields(data, _PROACTIVE_RULE_FIELDS, label="plugin proactive event rule")
        event_class = _identifier(data["event_class"], field="plugin proactive event class")
        if event_class not in _PROACTIVE_EVENT_CLASSES:
            raise ValueError("plugin proactive event class is invalid")
        if data["presentation"] != "proactive_panel":
            raise ValueError("plugin proactive presentation is invalid")
        if type(data["acknowledgement_required"]) is not bool:
            raise ValueError("plugin proactive acknowledgement is invalid")
        return cls(
            event_class=event_class,
            presentation="proactive_panel",
            acknowledgement_required=data["acknowledgement_required"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_class": self.event_class,
            "presentation": self.presentation,
            "acknowledgement_required": self.acknowledgement_required,
        }


@dataclass(frozen=True)
class PluginManifest:
    manifest_version: int
    plugin_id: str
    publisher_id: str
    plugin_version: str
    minimum_omega_version: str
    kind: PluginKind
    title: str
    description: str
    assets: tuple[PluginAsset, ...]
    entry_points: tuple[PluginEntryPoint, ...]
    starter_requests: tuple[str, ...]
    required_apps: tuple[AppRequirement, ...]
    optional_apps: tuple[AppRequirement, ...]
    schedule_template_paths: tuple[str, ...]
    proactive_event_rules: tuple[ProactiveEventRule, ...]
    enterprise_roles: tuple[PluginRoleMetadata, ...]
    signing_key_id: str
    signature: str

    @classmethod
    def from_dict(cls, raw: Any) -> PluginManifest:
        data = _object(raw, label="plugin manifest")
        _exact_fields(data, _MANIFEST_FIELDS, label="plugin manifest")
        if type(data["manifest_version"]) is not int or data["manifest_version"] != 2:
            raise ValueError("plugin manifest_version is invalid")
        kind = _enum(
            data["kind"], PluginKind, field="plugin kind", message="plugin kind is invalid"
        )
        if kind is PluginKind.APP_ONLY:
            raise ValueError("plugin app_only manifests are invalid")
        assets = _parse_list(data["assets"], PluginAsset.from_dict, field="plugin assets")
        entry_points = _parse_list(
            data["entry_points"], PluginEntryPoint.from_dict, field="plugin entry_points"
        )
        required_apps = _parse_list(
            data["required_apps"], AppRequirement.from_dict, field="plugin required_apps"
        )
        optional_apps = _parse_list(
            data["optional_apps"], AppRequirement.from_dict, field="plugin optional_apps"
        )
        rules = _parse_list(
            data["proactive_event_rules"],
            ProactiveEventRule.from_dict,
            field="plugin proactive_event_rules",
        )
        roles = _parse_list(
            data["enterprise_roles"], PluginRoleMetadata.from_dict, field="plugin enterprise_roles"
        )
        _no_duplicate(tuple(item.path for item in assets), "plugin asset identity is duplicated")
        _no_duplicate(
            tuple(item.entry_point_id for item in entry_points),
            "plugin entry point identity is duplicated",
        )
        _no_duplicate(
            tuple(item.app_id for item in required_apps), "plugin app identity is duplicated"
        )
        _no_duplicate(
            tuple(item.app_id for item in optional_apps), "plugin app identity is duplicated"
        )
        _no_duplicate(
            tuple(item.event_class for item in rules),
            "plugin proactive event identity is duplicated",
        )
        _no_duplicate(
            tuple(item.role_id for item in roles), "plugin enterprise role identity is duplicated"
        )
        schedule_template_paths = _string_list(
            data["schedule_template_paths"], field="plugin schedule_template_paths"
        )
        schedule_template_paths = tuple(
            _package_path(path, field="plugin schedule template path")
            for path in schedule_template_paths
        )
        manifest = cls(
            manifest_version=2,
            plugin_id=_identifier(data["plugin_id"], field="plugin_id"),
            publisher_id=_identifier(data["publisher_id"], field="publisher_id"),
            plugin_version=_semver(data["plugin_version"], field="plugin_version"),
            minimum_omega_version=_semver(
                data["minimum_omega_version"], field="minimum_omega_version"
            ),
            kind=kind,
            title=_stable_string(data["title"], field="plugin title"),
            description=_stable_string(data["description"], field="plugin description"),
            assets=assets,
            entry_points=entry_points,
            starter_requests=_string_list(
                data["starter_requests"], field="plugin starter_requests"
            ),
            required_apps=required_apps,
            optional_apps=optional_apps,
            schedule_template_paths=schedule_template_paths,
            proactive_event_rules=rules,
            enterprise_roles=roles,
            signing_key_id=_identifier(data["signing_key_id"], field="plugin signing_key_id"),
            signature=_stable_string(data["signature"], field="plugin signature"),
        )
        manifest._validate_cross_fields()
        return manifest

    def _validate_cross_fields(self) -> None:
        asset_by_path = {asset.path: asset for asset in self.assets}
        required_ids = {app.app_id for app in self.required_apps}
        optional_ids = {app.app_id for app in self.optional_apps}
        if required_ids & optional_ids:
            raise ValueError("plugin app dependency is duplicated")
        for entry in self.entry_points:
            asset = asset_by_path.get(entry.skill_path)
            if asset is None or asset.kind is not AssetKind.SKILL:
                raise ValueError("plugin entry point skill is invalid")
            if not set(entry.required_app_ids).issubset(required_ids | optional_ids):
                raise ValueError("plugin entry point app is invalid")
        for path in self.schedule_template_paths:
            asset = asset_by_path.get(path)
            if asset is None or asset.kind is not AssetKind.WORKFLOW:
                raise ValueError("plugin schedule template is invalid")
        entry_point_ids = {item.entry_point_id for item in self.entry_points}
        for role in self.enterprise_roles:
            if not set(role.entry_point_ids).issubset(entry_point_ids):
                raise ValueError("plugin enterprise role is invalid")
        if self.kind is PluginKind.SKILL_ONLY and (self.required_apps or self.optional_apps):
            raise ValueError("plugin skill_only app dependencies are invalid")
        if self.kind is PluginKind.MCP_BACKED and len(self.required_apps) != 1:
            raise ValueError("plugin mcp_backed required app count is invalid")
        if self.kind is PluginKind.COMPOSITE and len(self.required_apps) < 2:
            raise ValueError("plugin composite required app count is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "plugin_id": self.plugin_id,
            "publisher_id": self.publisher_id,
            "plugin_version": self.plugin_version,
            "minimum_omega_version": self.minimum_omega_version,
            "kind": self.kind.value,
            "title": self.title,
            "description": self.description,
            "assets": [item.to_dict() for item in self.assets],
            "entry_points": [item.to_dict() for item in self.entry_points],
            "starter_requests": list(self.starter_requests),
            "required_apps": [item.to_dict() for item in self.required_apps],
            "optional_apps": [item.to_dict() for item in self.optional_apps],
            "schedule_template_paths": list(self.schedule_template_paths),
            "proactive_event_rules": [item.to_dict() for item in self.proactive_event_rules],
            "enterprise_roles": [item.to_dict() for item in self.enterprise_roles],
            "signing_key_id": self.signing_key_id,
            "signature": self.signature,
        }

    def unsigned_dict(self) -> dict[str, object]:
        document = self.to_dict()
        del document["signature"]
        return document

    def signing_bytes(self) -> bytes:
        return canonical_bytes(self.unsigned_dict())


def _parse_list(value: Any, parser: Any, *, field: str) -> tuple[Any, ...]:
    if type(value) is not list:
        raise ValueError(f"{field} must be a list")
    return tuple(parser(item) for item in value)


def _no_duplicate(values: tuple[Any, ...], message: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(message)
