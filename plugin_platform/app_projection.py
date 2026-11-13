"""Pure deterministic projection of a reviewed MCP app into a dormant plugin shell."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from mcp_platform.canonical import canonical_bytes, sha256_hex

from .contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    AppRequirement,
    CapabilityKind,
    McpTransport,
    PluginAuthentication,
    PluginKind,
)
from .dependencies import AppCapabilitySnapshot, NamedCapability

MAX_APP_ONLY_DISPLAY_NAME_CHARS = 160
MAX_APP_ONLY_DESCRIPTION_CHARS = 1_024
_STARTER_PREFIX = "Help me get started with "
MAX_APP_ONLY_STARTER_REQUEST_CHARS = len(_STARTER_PREFIX) + MAX_APP_ONLY_DISPLAY_NAME_CHARS + 1
_CAPABILITY_FIELDS = (
    (CapabilityKind.TOOL, "tools"),
    (CapabilityKind.PROMPT, "prompts"),
    (CapabilityKind.RESOURCE, "resources"),
    (CapabilityKind.EXTENSION, "extensions"),
)
_HEX = frozenset("0123456789abcdef")


def _screened_text(value: Any, *, maximum: int, message: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value) > maximum
        or unicodedata.normalize("NFC", value) != value
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in value
        )
    ):
        raise ValueError(message)
    return value


def _digest(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValueError("app-only projection is invalid")
    return value


def _capability_sort_key(item: NamedCapability) -> tuple[str, str, str, str]:
    return (
        item.name,
        item.schema_sha256 or "",
        item.version or "",
        item.description,
    )


def _canonical_capabilities(value: Any) -> tuple[NamedCapability, ...]:
    if type(value) is not tuple or any(type(item) is not NamedCapability for item in value):
        raise ValueError("app-only projection is invalid")
    capabilities = tuple(
        NamedCapability(
            name=item.name,
            schema_sha256=item.schema_sha256,
            version=item.version,
            description=item.description,
            kind=item.kind,
        )
        for item in value
    )
    return tuple(sorted(capabilities, key=_capability_sort_key))


def _canonical_snapshot(app: Any) -> AppCapabilitySnapshot:
    if type(app) is not AppCapabilitySnapshot:
        raise ValueError("app-only projection app is invalid")
    try:
        capability_groups = {
            field: _canonical_capabilities(getattr(app, field)) for _, field in _CAPABILITY_FIELDS
        }
        canonical = AppCapabilitySnapshot(
            app_id=app.app_id,
            publisher_id=app.publisher_id,
            deployment=app.deployment,
            provider_id=app.provider_id,
            protocol_version=app.protocol_version,
            transports=app.transports,
            remote_origin=app.remote_origin,
            authorization_issuer=app.authorization_issuer,
            authentication=app.authentication,
            tools=capability_groups["tools"],
            prompts=capability_groups["prompts"],
            resources=capability_groups["resources"],
            extensions=capability_groups["extensions"],
            capability_revision=app.capability_revision,
            installed=app.installed,
            trusted=app.trusted,
            enabled=app.enabled,
        )
        return canonical
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("app-only projection app is invalid") from exc


def _required_app(
    *,
    app_id: str,
    publisher_id: str,
    deployment: AppDeployment,
    provider_id: str | None,
    protocol_version: str,
    transports: tuple[McpTransport, ...],
    remote_origin: str | None,
    authorization_issuer: str | None,
    authentication: PluginAuthentication,
    capability_groups: dict[str, tuple[NamedCapability, ...]],
) -> AppRequirement:
    return AppRequirement.from_dict(
        {
            "app_id": app_id,
            "publisher_id": publisher_id,
            "deployment": deployment.value,
            "provider_id": provider_id,
            "protocol_version": protocol_version,
            "transports": sorted(item.value for item in transports),
            "remote_origin": remote_origin,
            "authorization_issuer": authorization_issuer,
            "authentication": authentication.to_dict(),
            "capabilities": [
                {
                    "kind": kind.value,
                    "name": capability.name,
                    "schema_sha256": capability.schema_sha256,
                    "version": capability.version,
                }
                for kind, field in _CAPABILITY_FIELDS
                for capability in capability_groups[field]
            ],
        }
    )


def _canonical_required_app(value: Any) -> AppRequirement:
    if type(value) is not AppRequirement:
        raise ValueError("app-only projection is invalid")
    try:
        canonical = AppRequirement.from_dict(value.to_dict())
        return canonical
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("app-only projection is invalid") from exc


def _plugin_id(app: AppRequirement) -> str:
    identity_digest = sha256_hex(
        canonical_bytes(
            {
                "app_id": app.app_id,
                "publisher_id": app.publisher_id,
                "deployment": app.deployment.value,
                "provider_id": app.provider_id,
                "remote_origin": app.remote_origin,
                "authorization_issuer": app.authorization_issuer,
            }
        )
    )
    return f"omega.generated.mcp.{identity_digest[:24]}"


def _revision(
    *,
    plugin_id: str,
    capability_revision: str,
    display_name: str,
    description: str,
) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": plugin_id,
                "capability_revision": capability_revision,
                "display_name": display_name,
                "description": description,
            }
        )
    )


@dataclass(frozen=True)
class AppOnlyPluginProjection:
    """Closed content-free shell derived from one reviewed MCP app snapshot."""

    plugin_id: str
    revision: str
    kind: PluginKind
    display_name: str
    description: str
    required_apps: tuple[AppRequirement, ...]
    capability_revision: str
    tools: tuple[NamedCapability, ...]
    prompts: tuple[NamedCapability, ...]
    resources: tuple[NamedCapability, ...]
    extensions: tuple[NamedCapability, ...]
    starter_request: str
    generated: bool
    enabled: bool
    skill_assets: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            if type(self.required_apps) is not tuple or len(self.required_apps) != 1:
                raise ValueError("app-only projection is invalid")
            required_app = _canonical_required_app(self.required_apps[0])
            capability_groups = {
                field: _canonical_capabilities(getattr(self, field))
                for _, field in _CAPABILITY_FIELDS
            }
            expected_app = _required_app(
                app_id=required_app.app_id,
                publisher_id=required_app.publisher_id,
                deployment=required_app.deployment,
                provider_id=required_app.provider_id,
                protocol_version=required_app.protocol_version,
                transports=required_app.transports,
                remote_origin=required_app.remote_origin,
                authorization_issuer=required_app.authorization_issuer,
                authentication=required_app.authentication,
                capability_groups=capability_groups,
            )
            display_name = _screened_text(
                self.display_name,
                maximum=MAX_APP_ONLY_DISPLAY_NAME_CHARS,
                message="app-only projection is invalid",
            )
            description = _screened_text(
                self.description,
                maximum=MAX_APP_ONLY_DESCRIPTION_CHARS,
                message="app-only projection is invalid",
            )
            capability_revision = _digest(self.capability_revision)
            expected_plugin_id = _plugin_id(expected_app)
            expected_revision = _revision(
                plugin_id=expected_plugin_id,
                capability_revision=capability_revision,
                display_name=display_name,
                description=description,
            )
            expected_starter = f"{_STARTER_PREFIX}{display_name}."
            if (
                type(self.plugin_id) is not str
                or self.plugin_id != expected_plugin_id
                or type(self.revision) is not str
                or self.revision != expected_revision
                or type(self.kind) is not PluginKind
                or self.kind is not PluginKind.APP_ONLY
                or required_app != expected_app
                or type(self.starter_request) is not str
                or self.starter_request != expected_starter
                or len(self.starter_request) > MAX_APP_ONLY_STARTER_REQUEST_CHARS
                or self.generated is not True
                or self.enabled is not False
                or type(self.skill_assets) is not tuple
                or self.skill_assets != ()
                or not any(capability_groups.values())
            ):
                raise ValueError("app-only projection is invalid")
        except (AttributeError, TypeError, ValueError) as exc:
            if type(exc) is ValueError and str(exc) == "app-only projection is invalid":
                raise
            raise ValueError("app-only projection is invalid") from exc

        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "required_apps", (expected_app,))
        object.__setattr__(self, "capability_revision", capability_revision)
        for _, field in _CAPABILITY_FIELDS:
            object.__setattr__(self, field, capability_groups[field])

    @property
    def capabilities(self) -> tuple[NamedCapability, ...]:
        """Return all screened capability summaries in stable kind/name order."""

        return self.tools + self.prompts + self.resources + self.extensions


def derive_app_only_plugin(
    app: AppCapabilitySnapshot,
    display_name: str,
    description: str,
) -> AppOnlyPluginProjection:
    """Derive one immutable dormant plugin shell without consulting mutable state."""

    canonical_app = _canonical_snapshot(app)
    if (
        not canonical_app.installed
        or not canonical_app.trusted
        or canonical_app.protocol_version != PLUGIN_PROTOCOL_VERSION
    ):
        raise ValueError("app-only projection app is ineligible")
    display_name = _screened_text(
        display_name,
        maximum=MAX_APP_ONLY_DISPLAY_NAME_CHARS,
        message="app-only projection display name is invalid",
    )
    description = _screened_text(
        description,
        maximum=MAX_APP_ONLY_DESCRIPTION_CHARS,
        message="app-only projection description is invalid",
    )
    capability_groups = {field: getattr(canonical_app, field) for _, field in _CAPABILITY_FIELDS}
    if not any(capability_groups.values()):
        raise ValueError("app-only projection guidance is insufficient")
    required_app = _required_app(
        app_id=canonical_app.app_id,
        publisher_id=canonical_app.publisher_id,
        deployment=canonical_app.deployment,
        provider_id=canonical_app.provider_id,
        protocol_version=canonical_app.protocol_version,
        transports=canonical_app.transports,
        remote_origin=canonical_app.remote_origin,
        authorization_issuer=canonical_app.authorization_issuer,
        authentication=canonical_app.authentication,
        capability_groups=capability_groups,
    )
    plugin_id = _plugin_id(required_app)
    revision = _revision(
        plugin_id=plugin_id,
        capability_revision=canonical_app.capability_revision,
        display_name=display_name,
        description=description,
    )
    return AppOnlyPluginProjection(
        plugin_id=plugin_id,
        revision=revision,
        kind=PluginKind.APP_ONLY,
        display_name=display_name,
        description=description,
        required_apps=(required_app,),
        capability_revision=canonical_app.capability_revision,
        tools=canonical_app.tools,
        prompts=canonical_app.prompts,
        resources=canonical_app.resources,
        extensions=canonical_app.extensions,
        starter_request=f"{_STARTER_PREFIX}{display_name}.",
        generated=True,
        enabled=False,
        skill_assets=(),
    )
