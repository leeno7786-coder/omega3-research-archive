"""Immutable planner catalog compiled only from durable signed plugin authority."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from mcp_platform.app_registry import InstalledMcpAppRecord, McpAppRegistrySnapshot
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client_contracts import (
    McpClientLimits,
    McpConnectionDescriptor,
)
from mcp_platform.package_contracts import RiskClass
from plugin_platform.contracts import (
    AppRequirement,
    AssetKind,
    CapabilityKind,
    PluginManifest,
)
from plugin_platform.package_verifier import VerifiedPlugin
from plugin_platform.records import (
    InstalledPluginRecord,
    PluginAppBinding,
    PluginRegistrySnapshot,
)

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


class PluginCapabilityKind(StrEnum):
    TOOL = "tool"
    RESOURCE = "resource"
    RESOURCE_TEMPLATE = "resource_template"
    PROMPT = "prompt"
    SKILL = "skill"


@dataclass(frozen=True, slots=True)
class PluginCatalogAppAuthority:
    app_id: str
    account_key_hash: str
    connection_revision: str
    authority_revision: str
    capability_revision: str
    #: The tools the operator left selected (``tool:<name>``); only these are served.
    #: ``None`` when no connection authority exists to narrow the signed tools. The
    #: ceiling is already bound by ``authority_revision``, so it is not re-hashed.
    capability_ceiling: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        try:
            if self.capability_ceiling is not None and (
                type(self.capability_ceiling) is not tuple
                or any(type(item) is not str for item in self.capability_ceiling)
            ):
                raise ValueError
            object.__setattr__(self, "app_id", _stable_text(self.app_id))
            object.__setattr__(self, "account_key_hash", _digest(self.account_key_hash))
            object.__setattr__(
                self,
                "connection_revision",
                _digest(self.connection_revision),
            )
            object.__setattr__(self, "authority_revision", _digest(self.authority_revision))
            object.__setattr__(
                self,
                "capability_revision",
                _digest(self.capability_revision),
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_catalog_authority_invalid") from None

    def to_dict(self) -> dict[str, str]:
        return {
            "app_id": self.app_id,
            "account_key_hash": self.account_key_hash,
            "connection_revision": self.connection_revision,
            "authority_revision": self.authority_revision,
            "capability_revision": self.capability_revision,
        }


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("plugin_catalog_invalid")
    return value


def _stable_text(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("plugin_catalog_invalid")
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("plugin_catalog_invalid")
        return MappingProxyType({key: _freeze_json(value[key]) for key in sorted(value)})
    if type(value) in {tuple, list}:
        return tuple(_freeze_json(item) for item in value)
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise ValueError("plugin_catalog_invalid")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PluginToolBinding:
    candidate_id: str
    tool_name: str
    plugin_id: str
    plugin_revision: str
    app_id: str
    account_key_hash: str
    connection_revision: str
    capability: str
    title: str
    description: str
    input_schema: Mapping[str, object]
    input_schema_sha256: str
    output_schema: Mapping[str, object]
    output_schema_sha256: str
    risk_class: RiskClass
    effect: str
    requires_revision: bool
    max_result_bytes: int
    allowed_mime_types: tuple[str, ...]
    package_revision: str | None = None
    authority_revision: str | None = None
    capability_revision: str | None = None
    binding_revision: str | None = None

    @property
    def capability_kind(self) -> str:
        return PluginCapabilityKind.TOOL.value

    def __post_init__(self) -> None:
        try:
            candidate_id = _digest(self.candidate_id)
            plugin_revision = _digest(self.plugin_revision)
            account_key_hash = _digest(self.account_key_hash)
            connection_revision = _digest(self.connection_revision)
            package_revision = _digest(
                self.plugin_revision
                if self.package_revision is None
                else self.package_revision
            )
            authority_revision = _digest(
                connection_revision
                if self.authority_revision is None
                else self.authority_revision
            )
            capability_revision = _digest(
                self.input_schema_sha256
                if self.capability_revision is None
                else self.capability_revision
            )
            binding_revision = _digest(
                self.plugin_revision
                if self.binding_revision is None
                else self.binding_revision
            )
            tool_name = _stable_text(self.tool_name)
            plugin_id = _stable_text(self.plugin_id)
            app_id = _stable_text(self.app_id)
            capability = _stable_text(self.capability)
            title = _stable_text(self.title)
            description = _stable_text(self.description)
            input_schema = _freeze_json(self.input_schema)
            output_schema = _freeze_json(self.output_schema)
            if not isinstance(input_schema, Mapping) or not isinstance(output_schema, Mapping):
                raise ValueError
            input_digest = _digest(self.input_schema_sha256)
            output_digest = _digest(self.output_schema_sha256)
            if input_digest != sha256_hex(canonical_bytes(_thaw_json(input_schema))):
                raise ValueError
            if output_digest != sha256_hex(canonical_bytes(_thaw_json(output_schema))):
                raise ValueError
            if type(self.risk_class) is not RiskClass:
                raise ValueError
            effect = _stable_text(self.effect)
            if type(self.requires_revision) is not bool:
                raise ValueError
            if type(self.max_result_bytes) is not int or self.max_result_bytes <= 0:
                raise ValueError
            if (
                type(self.allowed_mime_types) is not tuple
                or not self.allowed_mime_types
                or len(self.allowed_mime_types) != len(set(self.allowed_mime_types))
            ):
                raise ValueError
            mime_types = tuple(_stable_text(item) for item in self.allowed_mime_types)
        except (TypeError, ValueError):
            raise ValueError("plugin_tool_binding_invalid") from None
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "plugin_revision", plugin_revision)
        object.__setattr__(self, "app_id", app_id)
        object.__setattr__(self, "account_key_hash", account_key_hash)
        object.__setattr__(self, "connection_revision", connection_revision)
        object.__setattr__(self, "package_revision", package_revision)
        object.__setattr__(self, "authority_revision", authority_revision)
        object.__setattr__(self, "capability_revision", capability_revision)
        object.__setattr__(self, "binding_revision", binding_revision)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "input_schema_sha256", input_digest)
        object.__setattr__(self, "output_schema", output_schema)
        object.__setattr__(self, "output_schema_sha256", output_digest)
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "allowed_mime_types", mime_types)

    def to_registry_entry(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "plugin_id": self.plugin_id,
            "plugin_revision": self.plugin_revision,
            "app_id": self.app_id,
            "connection_revision": self.connection_revision,
            "package_revision": self.package_revision,
            "authority_revision": self.authority_revision,
            "capability_revision": self.capability_revision,
            "binding_revision": self.binding_revision,
            "capability_kind": self.capability_kind,
            "capability": self.capability,
            "title": self.title,
            "description": self.description,
            "input_schema": _thaw_json(self.input_schema),
            "input_schema_sha256": self.input_schema_sha256,
            "output_schema": _thaw_json(self.output_schema),
            "output_schema_sha256": self.output_schema_sha256,
            "risk_class": self.risk_class.value,
            "effect": self.effect,
            "requires_revision": self.requires_revision,
            "max_result_bytes": self.max_result_bytes,
            "allowed_mime_types": list(self.allowed_mime_types),
            "origin": "signed_plugin_runtime",
        }


@dataclass(frozen=True, slots=True)
class _PluginSurfaceBinding:
    candidate_id: str
    capability: str
    plugin_id: str
    plugin_revision: str
    package_revision: str
    binding_revision: str
    app_id: str
    account_key_hash: str
    connection_revision: str
    authority_revision: str
    capability_revision: str
    title: str
    description: str
    capability_kind: str = field(init=False)

    def _validate_common(self, kind: PluginCapabilityKind) -> None:
        try:
            for name in (
                "candidate_id",
                "plugin_revision",
                "package_revision",
                "binding_revision",
                "account_key_hash",
                "connection_revision",
                "authority_revision",
                "capability_revision",
            ):
                object.__setattr__(self, name, _digest(getattr(self, name)))
            for name in (
                "capability",
                "plugin_id",
                "app_id",
                "title",
                "description",
            ):
                object.__setattr__(self, name, _stable_text(getattr(self, name)))
            object.__setattr__(self, "capability_kind", kind.value)
        except (TypeError, ValueError):
            raise ValueError("plugin_catalog_surface_invalid") from None


@dataclass(frozen=True, slots=True)
class PluginResourceBinding(_PluginSurfaceBinding):
    uri: str
    mime_type: str
    max_result_bytes: int

    def __post_init__(self) -> None:
        self._validate_common(PluginCapabilityKind.RESOURCE)
        try:
            object.__setattr__(self, "uri", _stable_text(self.uri))
            object.__setattr__(self, "mime_type", _stable_text(self.mime_type))
            if type(self.max_result_bytes) is not int or self.max_result_bytes <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_catalog_surface_invalid") from None


@dataclass(frozen=True, slots=True)
class PluginResourceTemplateBinding(_PluginSurfaceBinding):
    uri_template: str
    mime_type: str
    max_result_bytes: int

    def __post_init__(self) -> None:
        self._validate_common(PluginCapabilityKind.RESOURCE_TEMPLATE)
        try:
            object.__setattr__(self, "uri_template", _stable_text(self.uri_template))
            object.__setattr__(self, "mime_type", _stable_text(self.mime_type))
            if type(self.max_result_bytes) is not int or self.max_result_bytes <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_catalog_surface_invalid") from None


@dataclass(frozen=True, slots=True)
class PluginPromptBinding(_PluginSurfaceBinding):
    argument_schema: Mapping[str, object]
    argument_schema_sha256: str
    max_result_bytes: int

    def __post_init__(self) -> None:
        self._validate_common(PluginCapabilityKind.PROMPT)
        try:
            schema = _freeze_json(self.argument_schema)
            if not isinstance(schema, Mapping):
                raise ValueError
            digest = _digest(self.argument_schema_sha256)
            if digest != sha256_hex(canonical_bytes(_thaw_json(schema))):
                raise ValueError
            if type(self.max_result_bytes) is not int or self.max_result_bytes <= 0:
                raise ValueError
            object.__setattr__(self, "argument_schema", schema)
            object.__setattr__(self, "argument_schema_sha256", digest)
        except (TypeError, ValueError):
            raise ValueError("plugin_catalog_surface_invalid") from None


@dataclass(frozen=True, slots=True)
class PluginSkillBinding:
    candidate_id: str
    capability: str
    plugin_id: str
    plugin_revision: str
    package_revision: str
    binding_revision: str
    account_key_hash: str
    title: str
    description: str
    asset_path: str
    media_type: str
    asset_sha256: str
    required_app_ids: tuple[str, ...]
    app_authorities: tuple[PluginCatalogAppAuthority, ...]
    capability_kind: str = field(init=False, default=PluginCapabilityKind.SKILL.value)

    def __post_init__(self) -> None:
        try:
            for name in (
                "candidate_id",
                "plugin_revision",
                "package_revision",
                "binding_revision",
                "account_key_hash",
                "asset_sha256",
            ):
                object.__setattr__(self, name, _digest(getattr(self, name)))
            for name in (
                "capability",
                "plugin_id",
                "title",
                "description",
                "asset_path",
                "media_type",
            ):
                object.__setattr__(self, name, _stable_text(getattr(self, name)))
            if (
                type(self.required_app_ids) is not tuple
                or type(self.app_authorities) is not tuple
                or any(type(item) is not PluginCatalogAppAuthority for item in self.app_authorities)
            ):
                raise ValueError
            required = tuple(_stable_text(item) for item in self.required_app_ids)
            if required != tuple(sorted(set(required))):
                raise ValueError
            if tuple(item.app_id for item in self.app_authorities) != required:
                raise ValueError
            object.__setattr__(self, "required_app_ids", required)
        except (TypeError, ValueError):
            raise ValueError("plugin_catalog_skill_invalid") from None


@dataclass(frozen=True, slots=True)
class PluginRuntimeCatalogSnapshot:
    plugin_registry_revision: str
    app_registry_revision: str
    account_key_hash: str
    tools: tuple[PluginToolBinding, ...]
    resources: tuple[PluginResourceBinding, ...]
    resource_templates: tuple[PluginResourceTemplateBinding, ...]
    prompts: tuple[PluginPromptBinding, ...]
    skills: tuple[PluginSkillBinding, ...]
    revision: str

    def __post_init__(self) -> None:
        try:
            for name in (
                "plugin_registry_revision",
                "app_registry_revision",
                "account_key_hash",
                "revision",
            ):
                object.__setattr__(self, name, _digest(getattr(self, name)))
            groups = (
                ("tools", PluginToolBinding),
                ("resources", PluginResourceBinding),
                ("resource_templates", PluginResourceTemplateBinding),
                ("prompts", PluginPromptBinding),
                ("skills", PluginSkillBinding),
            )
            candidates: set[str] = set()
            for name, expected_type in groups:
                values = getattr(self, name)
                if (
                    type(values) is not tuple
                    or any(type(item) is not expected_type for item in values)
                    or tuple(item.candidate_id for item in values)
                    != tuple(sorted(item.candidate_id for item in values))
                    or candidates.intersection(item.candidate_id for item in values)
                ):
                    raise ValueError
                candidates.update(item.candidate_id for item in values)
        except (TypeError, ValueError):
            raise ValueError("plugin_runtime_catalog_invalid") from None

    @property
    def entries(self) -> tuple[object, ...]:
        return tuple(
            sorted(
                (
                    *self.tools,
                    *self.resources,
                    *self.resource_templates,
                    *self.prompts,
                    *self.skills,
                ),
                key=lambda item: (item.capability_kind, item.candidate_id),
            )
        )


def _validated_descriptor(value: object) -> McpConnectionDescriptor:
    if type(value) is not McpConnectionDescriptor:
        raise ValueError("plugin_catalog_invalid")
    try:
        return McpConnectionDescriptor(
            app_id=value.app_id,
            publisher_id=value.publisher_id,
            deployment=value.deployment,
            provider_id=value.provider_id,
            protocol_version=value.protocol_version,
            transport=value.transport,
            verified_launch_sha256=value.verified_launch_sha256,
            remote_origin=value.remote_origin,
            authorization_issuer=value.authorization_issuer,
            authentication=value.authentication,
            declared_capabilities=dict(value.declared_capabilities),
            extension_versions=dict(value.extension_versions),
            capability_revision=value.capability_revision,
            limits=McpClientLimits(**value.limits.to_dict()),
            signed_capability_authority=value.signed_capability_authority,
            runtime_compatibility=value.runtime_compatibility,
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("plugin_catalog_invalid") from None


def _validated_app(record: object) -> InstalledMcpAppRecord | None:
    if type(record) is not InstalledMcpAppRecord:
        raise ValueError("plugin_catalog_invalid")
    if record.installed is not True or record.trusted is not True:
        return None
    try:
        return InstalledMcpAppRecord.from_dict(record.to_dict(), registry_key=record.app_id)
    except (AttributeError, TypeError, ValueError):
        return None


def _descriptor_matches_app(
    descriptor: McpConnectionDescriptor,
    app: InstalledMcpAppRecord,
) -> bool:
    if (
        descriptor.app_id != app.app_id
        or descriptor.publisher_id != app.publisher_id
        or descriptor.provider_id != app.provider_id
        or descriptor.deployment.value != app.deployment.value
        or descriptor.transport.value not in {transport.value for transport in app.transports}
        or descriptor.authentication != app.authentication
    ):
        return False
    expected_capabilities = {
        f"{capability.kind.value}:{capability.name}": capability.schema_sha256
        for capability in app.capabilities
        if capability.kind is not CapabilityKind.EXTENSION
    }
    if dict(descriptor.declared_capabilities) != expected_capabilities:
        return False
    try:
        requirement = AppRequirement.from_dict(
            {
                "app_id": app.app_id,
                "publisher_id": app.publisher_id,
                "deployment": app.deployment.value,
                "provider_id": app.provider_id,
                "protocol_version": app.protocol_version,
                "transports": [transport.value for transport in app.transports],
                "remote_origin": app.remote_origin,
                "authorization_issuer": app.authorization_issuer,
                "authentication": app.authentication.to_dict(),
                "capabilities": [capability.to_dict() for capability in app.capabilities],
            }
        )
        revision = project_declared_capabilities(requirement).snapshot.capability_revision
    except (AttributeError, TypeError, ValueError):
        return False
    return descriptor.capability_revision == revision


def _validated_plugin_snapshot(
    snapshot: PluginRegistrySnapshot,
) -> tuple[tuple[InstalledPluginRecord, ...], str]:
    records: list[InstalledPluginRecord] = []
    by_id: dict[str, InstalledPluginRecord] = {}
    for raw_record in snapshot.records:
        if type(raw_record) is not InstalledPluginRecord:
            raise ValueError("plugin_catalog_invalid")
        try:
            record = InstalledPluginRecord.from_dict(
                raw_record.to_dict(),
                registry_key=raw_record.plugin_id,
            )
        except (AttributeError, TypeError, ValueError):
            raise ValueError("plugin_catalog_invalid") from None
        if record.plugin_id in by_id:
            raise ValueError("plugin_catalog_invalid")
        by_id[record.plugin_id] = record
        records.append(record)
    if [record.plugin_id for record in records] != sorted(by_id):
        raise ValueError("plugin_catalog_invalid")
    revision = sha256_hex(
        canonical_bytes(
            {
                "schema_version": 2,
                "plugins": {
                    plugin_id: by_id[plugin_id].to_dict() for plugin_id in sorted(by_id)
                },
            }
        )
    )
    if snapshot.revision != revision:
        raise ValueError("plugin_catalog_invalid")
    return tuple(records), revision


def _candidate_id(
    *,
    plugin: InstalledPluginRecord,
    plugin_revision: str,
    app_revision: str,
    binding_revision: str,
    app_id: str,
    account_key_hash: str,
    connection_revision: str,
    capability: str,
    capability_revision: str,
    input_schema_sha256: str,
    output_schema_sha256: str,
    capability_kind: str = PluginCapabilityKind.TOOL.value,
    package_revision: str | None = None,
    authority_revision: str | None = None,
) -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                "plugin_id": plugin.plugin_id,
                "plugin_revision": plugin_revision,
                "package_revision": package_revision,
                "app_id": app_id,
                "app_revision": app_revision,
                "binding_revision": binding_revision,
                "account_key_hash": account_key_hash,
                "connection_revision": connection_revision,
                "authority_revision": authority_revision,
                "capability_kind": capability_kind,
                "capability": capability,
                "capability_revision": capability_revision,
                "input_schema_sha256": input_schema_sha256,
                "output_schema_sha256": output_schema_sha256,
            }
        )
    ).hexdigest()


def compile_plugin_tool_bindings(
    *,
    plugin_snapshot: PluginRegistrySnapshot,
    app_snapshot: McpAppRegistrySnapshot,
    descriptor_for_app: Callable[[str, str], McpConnectionDescriptor],
    account_key_hash: str,
    connection_revision_provider: (
        Callable[[str, str, str, McpConnectionDescriptor], str | None] | None
    ) = None,
    app_authority_provider: (
        Callable[
            [str, str, str, McpConnectionDescriptor],
            PluginCatalogAppAuthority | None,
        ]
        | None
    ) = None,
) -> tuple[PluginToolBinding, ...]:
    """Compile the only planner-visible plugin catalog from exact signed authority."""

    if (
        type(plugin_snapshot) is not PluginRegistrySnapshot
        or type(plugin_snapshot.records) is not tuple
        or type(app_snapshot) is not McpAppRegistrySnapshot
        or type(app_snapshot.records) is not tuple
        or not callable(descriptor_for_app)
    ):
        raise ValueError("plugin_catalog_invalid")
    plugin_records, plugin_revision = _validated_plugin_snapshot(plugin_snapshot)
    app_revision = _digest(app_snapshot.revision)
    account_hash = _digest(account_key_hash)
    apps: dict[str, InstalledMcpAppRecord] = {}
    for raw_app in app_snapshot.records:
        app = _validated_app(raw_app)
        if app is not None:
            if app.app_id in apps:
                raise ValueError("plugin_catalog_invalid")
            apps[app.app_id] = app

    pending: list[dict[str, Any]] = []
    tool_names: set[str] = set()
    missing_authority = False
    for raw_plugin in plugin_records:
        if (
            raw_plugin.status != "installed"
            or raw_plugin.enabled is not True
            or raw_plugin.blocker is not None
            or raw_plugin.app_binding is None
        ):
            continue
        plugin = raw_plugin
        selected_version = next(
            (
                version
                for version in plugin.versions
                if version.plugin_version == plugin.selected_version
                and version.state == "selected"
            ),
            None,
        )
        if selected_version is None:
            raise ValueError("plugin_catalog_invalid")
        package_revision = _digest(selected_version.package_sha256)
        try:
            binding = PluginAppBinding.from_dict(raw_plugin.app_binding.to_dict())
        except (AttributeError, TypeError, ValueError):
            raise ValueError("plugin_catalog_invalid") from None
        app_ids = binding.required_app_ids + binding.optional_app_ids
        for app_id in app_ids:
            app = apps.get(app_id)
            if app is None:
                continue
            try:
                descriptor = _validated_descriptor(
                    descriptor_for_app(plugin.plugin_id, app_id)
                )
            except (LookupError, OSError, RuntimeError, TypeError, ValueError):
                continue
            if not _descriptor_matches_app(descriptor, app):
                continue
            authority = descriptor.signed_capability_authority
            if authority is None:
                missing_authority = True
                continue
            policies = authority["tools"]
            tool_capabilities = {
                capability.name: capability
                for capability in app.capabilities
                if capability.kind is CapabilityKind.TOOL
            }
            if not set(policies).issubset(tool_capabilities):
                continue
            app_authority: PluginCatalogAppAuthority | None = None
            if app_authority_provider is not None:
                app_authority = app_authority_provider(
                    plugin.plugin_id,
                    app_id,
                    account_hash,
                    descriptor,
                )
                if app_authority is None:
                    continue
                if (
                    type(app_authority) is not PluginCatalogAppAuthority
                    or app_authority.app_id != app_id
                    or app_authority.account_key_hash != account_hash
                    or app_authority.capability_revision
                    != descriptor.capability_revision
                ):
                    raise ValueError("plugin_catalog_invalid")
                connection_revision = app_authority.connection_revision
                authority_revision = app_authority.authority_revision
            elif connection_revision_provider is None:
                connection_revision = sha256_hex(canonical_bytes(descriptor.to_dict()))
                authority_revision = connection_revision
            else:
                connection_revision = connection_revision_provider(
                    plugin.plugin_id,
                    app_id,
                    account_hash,
                    descriptor,
                )
                if connection_revision is None:
                    continue
                try:
                    connection_revision = _digest(connection_revision)
                except (TypeError, ValueError):
                    raise ValueError("plugin_catalog_invalid") from None
                authority_revision = connection_revision
            ceiling = None if app_authority is None else app_authority.capability_ceiling
            for capability in sorted(policies):
                if ceiling is not None and f"tool:{capability}" not in ceiling:
                    # Withheld on the consent form: never offered to the planner.
                    continue
                policy = policies[capability]
                input_schema = policy["input_schema"]
                output_schema = policy["output_schema"]
                input_digest = sha256_hex(canonical_bytes(_thaw_json(input_schema)))
                output_digest = sha256_hex(canonical_bytes(_thaw_json(output_schema)))
                if (
                    input_digest != policy["input_schema_sha256"]
                    or output_digest != policy["output_schema_sha256"]
                    or tool_capabilities[capability].schema_sha256 != input_digest
                ):
                    continue
                try:
                    risk_class = RiskClass(policy["risk_class"])
                except (TypeError, ValueError):
                    continue
                candidate = _candidate_id(
                    plugin=plugin,
                    plugin_revision=plugin_revision,
                    app_revision=app_revision,
                    binding_revision=binding.revision,
                    app_id=app_id,
                    account_key_hash=account_hash,
                    connection_revision=connection_revision,
                    capability=capability,
                    capability_revision=descriptor.capability_revision,
                    input_schema_sha256=input_digest,
                    output_schema_sha256=output_digest,
                    package_revision=package_revision,
                    authority_revision=authority_revision,
                )
                from mcp_platform.plugin_dispatch import canonical_plugin_tool_name

                tool_name = canonical_plugin_tool_name(plugin.plugin_id, capability)
                if tool_name in tool_names:
                    raise ValueError("plugin_tool_name_collision")

                document = {
                    "candidate_id": candidate,
                    "tool_name": tool_name,
                    "plugin_id": plugin.plugin_id,
                    "plugin_revision": plugin_revision,
                    "app_id": app_id,
                    "account_key_hash": account_hash,
                    "connection_revision": connection_revision,
                    "capability": capability,
                    "title": policy["title"],
                    "description": policy["title"],
                    "input_schema": input_schema,
                    "input_schema_sha256": input_digest,
                    "output_schema": output_schema,
                    "output_schema_sha256": output_digest,
                    "risk_class": risk_class,
                    "effect": policy["effect"],
                    "requires_revision": policy["requires_revision"],
                    "max_result_bytes": policy["max_result_bytes"],
                    "allowed_mime_types": policy["allowed_mime_types"],
                    "package_revision": package_revision,
                    "authority_revision": authority_revision,
                    "capability_revision": descriptor.capability_revision,
                    "binding_revision": binding.revision,
                }
                tool_names.add(tool_name)
                pending.append(document)

    bindings = tuple(PluginToolBinding(**document) for document in pending)
    if not bindings and missing_authority:
        raise ValueError("plugin_catalog_authority_missing")
    return tuple(sorted(bindings, key=lambda item: item.candidate_id))


def _surface_candidate_id(
    *,
    kind: PluginCapabilityKind,
    plugin_id: str,
    plugin_revision: str,
    package_revision: str,
    binding_revision: str,
    app_authority: PluginCatalogAppAuthority | None,
    capability: str,
    policy: Mapping[str, object],
) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "capability_kind": kind.value,
                "plugin_id": plugin_id,
                "plugin_revision": plugin_revision,
                "package_revision": package_revision,
                "binding_revision": binding_revision,
                "app_authority": (
                    None if app_authority is None else app_authority.to_dict()
                ),
                "capability": capability,
                "policy": _thaw_json(policy),
            }
        )
    )


def compile_plugin_runtime_catalog(
    *,
    plugin_snapshot: PluginRegistrySnapshot,
    app_snapshot: McpAppRegistrySnapshot,
    descriptor_for_app: Callable[[str, str], McpConnectionDescriptor],
    account_key_hash: str,
    connection_revision_provider: (
        Callable[[str, str, str, McpConnectionDescriptor], str | None] | None
    ) = None,
    app_authority_provider: (
        Callable[
            [str, str, str, McpConnectionDescriptor],
            PluginCatalogAppAuthority | None,
        ]
        | None
    ) = None,
    verified_plugin_provider: Callable[[str], VerifiedPlugin] | None = None,
) -> PluginRuntimeCatalogSnapshot:
    """Compile every installed capability surface without provider work."""

    if (
        type(plugin_snapshot) is not PluginRegistrySnapshot
        or type(app_snapshot) is not McpAppRegistrySnapshot
        or not callable(descriptor_for_app)
        or app_authority_provider is not None
        and not callable(app_authority_provider)
        or verified_plugin_provider is not None
        and not callable(verified_plugin_provider)
    ):
        raise ValueError("plugin_catalog_invalid")
    plugin_records, plugin_revision = _validated_plugin_snapshot(plugin_snapshot)
    app_revision = _digest(app_snapshot.revision)
    account_hash = _digest(account_key_hash)
    apps: dict[str, InstalledMcpAppRecord] = {}
    for raw_app in app_snapshot.records:
        app = _validated_app(raw_app)
        if app is not None:
            if app.app_id in apps:
                raise ValueError("plugin_catalog_invalid")
            apps[app.app_id] = app

    tools = compile_plugin_tool_bindings(
        plugin_snapshot=plugin_snapshot,
        app_snapshot=app_snapshot,
        descriptor_for_app=descriptor_for_app,
        account_key_hash=account_hash,
        connection_revision_provider=connection_revision_provider,
        app_authority_provider=app_authority_provider,
    )
    resources: list[PluginResourceBinding] = []
    templates: list[PluginResourceTemplateBinding] = []
    prompts: list[PluginPromptBinding] = []
    skills: list[PluginSkillBinding] = []

    for plugin in plugin_records:
        if (
            plugin.status != "installed"
            or plugin.enabled is not True
            or plugin.blocker is not None
        ):
            continue
        binding = (
            None
            if plugin.app_binding is None
            else PluginAppBinding.from_dict(plugin.app_binding.to_dict())
        )
        binding_revision = (
            sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.plugin-runtime.no-app-binding.v1",
                        "plugin_id": plugin.plugin_id,
                    }
                )
            )
            if binding is None
            else binding.revision
        )
        selected = next(
            (
                version
                for version in plugin.versions
                if version.plugin_version == plugin.selected_version
                and version.state == "selected"
            ),
            None,
        )
        if selected is None:
            raise ValueError("plugin_catalog_invalid")
        package_revision = _digest(selected.package_sha256)
        app_authorities: dict[str, PluginCatalogAppAuthority] = {}

        app_ids = () if binding is None else binding.required_app_ids + binding.optional_app_ids
        for app_id in app_ids:
            app = apps.get(app_id)
            if app is None:
                continue
            try:
                descriptor = _validated_descriptor(
                    descriptor_for_app(plugin.plugin_id, app_id)
                )
            except (LookupError, OSError, RuntimeError, TypeError, ValueError):
                continue
            if not _descriptor_matches_app(descriptor, app):
                continue
            if app_authority_provider is not None:
                current = app_authority_provider(
                    plugin.plugin_id,
                    app_id,
                    account_hash,
                    descriptor,
                )
                if current is None:
                    continue
                if (
                    type(current) is not PluginCatalogAppAuthority
                    or current.app_id != app_id
                    or current.account_key_hash != account_hash
                    or current.capability_revision != descriptor.capability_revision
                ):
                    raise ValueError("plugin_catalog_invalid")
            else:
                connection = (
                    sha256_hex(canonical_bytes(descriptor.to_dict()))
                    if connection_revision_provider is None
                    else connection_revision_provider(
                        plugin.plugin_id,
                        app_id,
                        account_hash,
                        descriptor,
                    )
                )
                if connection is None:
                    continue
                current = PluginCatalogAppAuthority(
                    app_id=app_id,
                    account_key_hash=account_hash,
                    connection_revision=_digest(connection),
                    authority_revision=_digest(connection),
                    capability_revision=descriptor.capability_revision,
                )
            app_authorities[app_id] = current
            authority = descriptor.signed_capability_authority
            if authority is None:
                continue
            prompt_capabilities = {
                item.name: item
                for item in app.capabilities
                if item.kind is CapabilityKind.PROMPT
            }
            resource_capabilities = {
                item.name: item
                for item in app.capabilities
                if item.kind is CapabilityKind.RESOURCE
            }
            signed_prompts = authority.get("prompts", {})
            signed_resources = authority["resources"]
            signed_templates = authority["resource_templates"]
            if (
                not isinstance(signed_prompts, Mapping)
                or not isinstance(signed_resources, Mapping)
                or not isinstance(signed_templates, Mapping)
                or not set(signed_prompts).issubset(prompt_capabilities)
                or not (
                    set(signed_resources) | set(signed_templates)
                ).issubset(resource_capabilities)
                or set(signed_resources) & set(signed_templates)
            ):
                continue
            common = {
                "plugin_id": plugin.plugin_id,
                "plugin_revision": plugin_revision,
                "package_revision": package_revision,
                "binding_revision": binding_revision,
                "app_id": app_id,
                "account_key_hash": account_hash,
                "connection_revision": current.connection_revision,
                "authority_revision": current.authority_revision,
                "capability_revision": current.capability_revision,
            }
            for capability in sorted(signed_resources):
                policy = signed_resources[capability]
                if not isinstance(policy, Mapping):
                    raise ValueError("plugin_catalog_invalid")
                candidate = _surface_candidate_id(
                    kind=PluginCapabilityKind.RESOURCE,
                    plugin_id=plugin.plugin_id,
                    plugin_revision=plugin_revision,
                    package_revision=package_revision,
                    binding_revision=binding_revision,
                    app_authority=current,
                    capability=capability,
                    policy=policy,
                )
                resources.append(
                    PluginResourceBinding(
                        candidate_id=candidate,
                        capability=capability,
                        title=capability,
                        description=capability,
                        uri=policy["uri"],
                        mime_type=policy["mime_type"],
                        max_result_bytes=policy["max_bytes"],
                        **common,
                    )
                )
            for capability in sorted(signed_templates):
                policy = signed_templates[capability]
                if not isinstance(policy, Mapping):
                    raise ValueError("plugin_catalog_invalid")
                candidate = _surface_candidate_id(
                    kind=PluginCapabilityKind.RESOURCE_TEMPLATE,
                    plugin_id=plugin.plugin_id,
                    plugin_revision=plugin_revision,
                    package_revision=package_revision,
                    binding_revision=binding_revision,
                    app_authority=current,
                    capability=capability,
                    policy=policy,
                )
                templates.append(
                    PluginResourceTemplateBinding(
                        candidate_id=candidate,
                        capability=capability,
                        title=capability,
                        description=capability,
                        uri_template=policy["uri_template"],
                        mime_type=policy["mime_type"],
                        max_result_bytes=policy["max_bytes"],
                        **common,
                    )
                )
            for capability in sorted(signed_prompts):
                policy = signed_prompts[capability]
                if not isinstance(policy, Mapping):
                    raise ValueError("plugin_catalog_invalid")
                argument_schema = {"arguments": policy["arguments"]}
                if (
                    prompt_capabilities[capability].schema_sha256
                    != policy["schema_sha256"]
                ):
                    continue
                candidate = _surface_candidate_id(
                    kind=PluginCapabilityKind.PROMPT,
                    plugin_id=plugin.plugin_id,
                    plugin_revision=plugin_revision,
                    package_revision=package_revision,
                    binding_revision=binding_revision,
                    app_authority=current,
                    capability=capability,
                    policy=policy,
                )
                prompts.append(
                    PluginPromptBinding(
                        candidate_id=candidate,
                        capability=capability,
                        title=policy["title"],
                        description=policy["title"],
                        argument_schema=argument_schema,
                        argument_schema_sha256=policy["schema_sha256"],
                        max_result_bytes=policy["max_result_bytes"],
                        **common,
                    )
                )

        if verified_plugin_provider is None:
            continue
        try:
            verified = verified_plugin_provider(plugin.plugin_id)
        except (LookupError, OSError, RuntimeError, TypeError, ValueError):
            continue
        try:
            manifest = PluginManifest.from_dict(verified.manifest.to_dict())
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            type(verified) is not VerifiedPlugin
            or manifest != verified.manifest
            or verified.manifest.plugin_id != plugin.plugin_id
            or verified.manifest.plugin_version != plugin.selected_version
            or verified.manifest_sha256 != selected.manifest_sha256
            or verified.package_sha256 != package_revision
        ):
            continue
        if getattr(verified, "legacy_authority", False):
            # Spec principle 8 forbids a compatibility fork: a package the
            # verifier flagged as the retired flat authority shape compiles
            # zero bindings, including skill entry points whose app
            # connection is otherwise live, until the v2 package replaces it.
            continue
        manifest_required = tuple(
            item.app_id for item in verified.manifest.required_apps
        )
        manifest_optional = tuple(
            item.app_id for item in verified.manifest.optional_apps
        )
        if (
            binding is None
            and (manifest_required or manifest_optional)
            or binding is not None
            and (
                set(binding.required_app_ids) != set(manifest_required)
                or not set(binding.optional_app_ids).issubset(manifest_optional)
            )
        ):
            continue
        skill_assets = {
            asset.path: asset
            for asset in verified.manifest.assets
            if asset.kind is AssetKind.SKILL
        }
        for entry_point in sorted(
            verified.manifest.entry_points,
            key=lambda item: item.entry_point_id,
        ):
            asset = skill_assets.get(entry_point.skill_path)
            payload = verified.asset_bytes.get(entry_point.skill_path)
            required_app_ids = tuple(sorted(entry_point.required_app_ids))
            required_authorities = tuple(
                app_authorities[app_id]
                for app_id in required_app_ids
                if app_id in app_authorities
            )
            if (
                asset is None
                or type(payload) is not bytes
                or sha256_hex(payload) != asset.sha256
                or len(required_authorities) != len(required_app_ids)
            ):
                continue
            policy = {
                "asset_path": asset.path,
                "asset_sha256": asset.sha256,
                "media_type": asset.media_type,
                "required_app_ids": list(required_app_ids),
                "app_authorities": [item.to_dict() for item in required_authorities],
            }
            skills.append(
                PluginSkillBinding(
                    candidate_id=_surface_candidate_id(
                        kind=PluginCapabilityKind.SKILL,
                        plugin_id=plugin.plugin_id,
                        plugin_revision=plugin_revision,
                        package_revision=package_revision,
                        binding_revision=binding_revision,
                        app_authority=None,
                        capability=entry_point.entry_point_id,
                        policy=policy,
                    ),
                    capability=entry_point.entry_point_id,
                    plugin_id=plugin.plugin_id,
                    plugin_revision=plugin_revision,
                    package_revision=package_revision,
                    binding_revision=binding_revision,
                    account_key_hash=account_hash,
                    title=entry_point.title,
                    description=verified.manifest.description,
                    asset_path=asset.path,
                    media_type=asset.media_type,
                    asset_sha256=asset.sha256,
                    required_app_ids=required_app_ids,
                    app_authorities=required_authorities,
                )
            )

    sorted_groups = tuple(
        tuple(sorted(group, key=lambda item: item.candidate_id))
        for group in (resources, templates, prompts, skills)
    )
    resources_tuple, templates_tuple, prompts_tuple, skills_tuple = sorted_groups
    revision = sha256_hex(
        canonical_bytes(
            {
                "plugin_registry_revision": plugin_revision,
                "app_registry_revision": app_revision,
                "account_key_hash": account_hash,
                "entries": [
                    [item.capability_kind, item.candidate_id]
                    for item in sorted(
                        (
                            *tools,
                            *resources_tuple,
                            *templates_tuple,
                            *prompts_tuple,
                            *skills_tuple,
                        ),
                        key=lambda item: (item.capability_kind, item.candidate_id),
                    )
                ],
            }
        )
    )
    return PluginRuntimeCatalogSnapshot(
        plugin_registry_revision=plugin_revision,
        app_registry_revision=app_revision,
        account_key_hash=account_hash,
        tools=tools,
        resources=resources_tuple,
        resource_templates=templates_tuple,
        prompts=prompts_tuple,
        skills=skills_tuple,
        revision=revision,
    )


def plugin_tool_registry_entries(*, service: Any | None = None) -> dict[str, dict[str, Any]]:
    from mcp_platform.plugin_dispatch import plugin_tool_bindings

    return {
        binding.tool_name: binding.to_registry_entry()
        for binding in plugin_tool_bindings(service=service)
    }


__all__ = [
    "PluginCapabilityKind",
    "PluginCatalogAppAuthority",
    "PluginPromptBinding",
    "PluginResourceBinding",
    "PluginResourceTemplateBinding",
    "PluginRuntimeCatalogSnapshot",
    "PluginSkillBinding",
    "PluginToolBinding",
    "compile_plugin_runtime_catalog",
    "compile_plugin_tool_bindings",
    "plugin_tool_registry_entries",
]
