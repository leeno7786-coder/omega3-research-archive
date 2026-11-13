"""Pure deterministic resolution of immutable MCP app capability snapshots."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AppDeployment,
    AppRequirement,
    CapabilityRequirement,
    CapabilityKind,
    McpTransport,
    PluginAuthentication,
    PluginManifest,
    parse_app_id,
)

_CAPABILITY_FIELDS = (
    (CapabilityKind.TOOL, "tools"),
    (CapabilityKind.PROMPT, "prompts"),
    (CapabilityKind.RESOURCE, "resources"),
    (CapabilityKind.EXTENSION, "extensions"),
)
_TASKS_EXTENSION_IDENTIFIER = "io.modelcontextprotocol/tasks"
_BLOCKER_REASONS = frozenset(
    {
        "app_absent",
        "app_ambiguous",
        "app_id_mismatch",
        "app_uninstalled",
        "app_untrusted",
        "publisher_mismatch",
        "deployment_mismatch",
        "provider_mismatch",
        "protocol_mismatch",
        "transport_mismatch",
        "origin_mismatch",
        "issuer_mismatch",
        "authentication_mismatch",
        "capability_missing",
        "schema_mismatch",
        "version_mismatch",
    }
)


def _task1_app_id(value: Any) -> str:
    return parse_app_id(value)


def _task1_named_capability(
    name: Any,
    schema_sha256: Any,
    version: Any,
    kind: CapabilityKind | None = None,
) -> CapabilityRequirement:
    # A stated kind wins. Inferring it from the name's shape is a guess that
    # only ever worked because tools and extensions happen to look different --
    # it cannot tell a resource from a tool at all, which is why a resource
    # could not be named the way MCP says resources are named. Callers that
    # know the kind now say so; the guess remains for those that do not.
    if kind is None:
        kind = (
            CapabilityKind.EXTENSION
            if type(name) is str and "/" in name
            else CapabilityKind.TOOL
        )
    return CapabilityRequirement.from_dict(
        {
            "kind": kind.value,
            "name": name,
            "schema_sha256": schema_sha256,
            "version": version,
        }
    )


def _plain_description(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value) > 1_024
        or unicodedata.normalize("NFC", value) != value
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in value
        )
    ):
        raise ValueError("app capability description is invalid")
    return value


@dataclass(frozen=True)
class NamedCapability:
    name: str
    schema_sha256: str | None
    version: str | None
    description: str | None
    #: What this capability IS. Optional so every existing construction keeps
    #: working; stated wherever the caller knows, which is everywhere that
    #: matters. Deliberately excluded from the capability revision -- see
    #: `_revision_document`, which hashes name, schema digest and version only.
    kind: CapabilityKind | None = None

    def __post_init__(self) -> None:
        validated = _task1_named_capability(
            self.name,
            self.schema_sha256,
            self.version,
            self.kind,
        )
        object.__setattr__(self, "name", validated.name)
        object.__setattr__(self, "schema_sha256", validated.schema_sha256)
        object.__setattr__(self, "version", validated.version)
        if validated.name == _TASKS_EXTENSION_IDENTIFIER:
            if self.description is not None:
                raise ValueError("Tasks extension description must be null")
            description = None
        else:
            description = _plain_description(self.description)
        object.__setattr__(self, "description", description)


@dataclass(frozen=True)
class AppCapabilitySnapshot:
    app_id: str
    publisher_id: str
    deployment: AppDeployment
    provider_id: str | None
    protocol_version: str
    transports: tuple[McpTransport, ...]
    remote_origin: str | None
    authorization_issuer: str | None
    authentication: PluginAuthentication
    tools: tuple[NamedCapability, ...]
    prompts: tuple[NamedCapability, ...]
    resources: tuple[NamedCapability, ...]
    extensions: tuple[NamedCapability, ...]
    capability_revision: str
    installed: bool
    trusted: bool
    enabled: bool

    def __post_init__(self) -> None:
        if type(self.transports) is not tuple:
            raise ValueError("app capability transports must be a tuple")
        for _, field in _CAPABILITY_FIELDS:
            capabilities = getattr(self, field)
            if type(capabilities) is not tuple:
                raise ValueError(f"app capability {field} must be a tuple")
            if any(type(item) is not NamedCapability for item in capabilities):
                raise ValueError(f"app capability {field} entries are invalid")
        if (
            type(self.capability_revision) is not str
            or len(self.capability_revision) != 64
            or any(character not in "0123456789abcdef" for character in self.capability_revision)
        ):
            raise ValueError("app capability revision is invalid")
        if any(type(value) is not bool for value in (self.installed, self.trusted, self.enabled)):
            raise ValueError("app capability state is invalid")

        validated = AppRequirement.from_dict(
            {
                "app_id": self.app_id,
                "publisher_id": self.publisher_id,
                "deployment": self.deployment.value
                if type(self.deployment) is AppDeployment
                else self.deployment,
                "provider_id": self.provider_id,
                "protocol_version": self.protocol_version,
                "transports": [
                    item.value if type(item) is McpTransport else item for item in self.transports
                ],
                "remote_origin": self.remote_origin,
                "authorization_issuer": self.authorization_issuer,
                "authentication": self.authentication.to_dict()
                if type(self.authentication) is PluginAuthentication
                else self.authentication,
                "capabilities": [
                    {
                        "kind": kind.value,
                        "name": capability.name,
                        "schema_sha256": capability.schema_sha256,
                        "version": capability.version,
                    }
                    for kind, field in _CAPABILITY_FIELDS
                    for capability in getattr(self, field)
                ],
            }
        )
        object.__setattr__(self, "app_id", validated.app_id)
        object.__setattr__(self, "publisher_id", validated.publisher_id)
        object.__setattr__(self, "deployment", validated.deployment)
        object.__setattr__(self, "provider_id", validated.provider_id)
        object.__setattr__(self, "protocol_version", validated.protocol_version)
        object.__setattr__(
            self,
            "transports",
            tuple(sorted(validated.transports, key=lambda item: item.value)),
        )
        object.__setattr__(self, "remote_origin", validated.remote_origin)
        object.__setattr__(self, "authorization_issuer", validated.authorization_issuer)
        object.__setattr__(self, "authentication", validated.authentication)
        for _, field in _CAPABILITY_FIELDS:
            object.__setattr__(
                self,
                field,
                tuple(
                    sorted(
                        getattr(self, field),
                        key=lambda item: (
                            item.name,
                            item.schema_sha256 or "",
                            item.version or "",
                            item.description or "",
                        ),
                    )
                ),
            )


@dataclass(frozen=True)
class ResolvedAppDependency:
    requirement: AppRequirement
    app: AppCapabilitySnapshot

    def __post_init__(self) -> None:
        if (
            type(self.requirement) is not AppRequirement
            or type(self.app) is not AppCapabilitySnapshot
        ):
            raise ValueError("resolved app dependency is invalid")
        requirement = _canonical_requirement(self.requirement)
        if _app_blockers(requirement, self.app):
            raise ValueError("resolved app dependency is invalid")
        object.__setattr__(self, "requirement", requirement)


@dataclass(frozen=True)
class DependencyBlocker:
    app_id: str
    capability_kind: CapabilityKind | None
    capability_name: str | None
    reason: str

    def __post_init__(self) -> None:
        if type(self.reason) is not str or self.reason not in _BLOCKER_REASONS:
            raise ValueError("dependency blocker is invalid")
        capability_reason = self.reason in {
            "schema_mismatch",
            "version_mismatch",
            "capability_missing",
        }
        capability_kind_present = self.capability_kind is not None
        capability_name_present = self.capability_name is not None
        if capability_kind_present is not capability_name_present:
            raise ValueError("dependency blocker capability is invalid")
        capability_identity_present = capability_kind_present
        if capability_reason is not capability_identity_present:
            raise ValueError("dependency blocker is invalid")
        try:
            app_id = _task1_app_id(self.app_id)
        except ValueError as exc:
            raise ValueError("dependency blocker is invalid") from exc
        object.__setattr__(self, "app_id", app_id)
        if self.capability_kind is not None and type(self.capability_kind) is not CapabilityKind:
            raise ValueError("dependency blocker capability is invalid")
        if self.capability_kind is not None:
            version = (
                None
                if self.capability_name == _TASKS_EXTENSION_IDENTIFIER
                else "0.0.0"
                if self.capability_kind is CapabilityKind.EXTENSION
                else None
            )
            try:
                capability = CapabilityRequirement.from_dict(
                    {
                        "kind": self.capability_kind.value,
                        "name": self.capability_name,
                        "schema_sha256": None,
                        "version": version,
                    }
                )
            except ValueError as exc:
                raise ValueError("dependency blocker capability is invalid") from exc
            object.__setattr__(self, "capability_name", capability.name)


@dataclass(frozen=True)
class DependencyResolution:
    required: tuple[ResolvedAppDependency, ...]
    optional: tuple[ResolvedAppDependency, ...]
    blockers: tuple[DependencyBlocker, ...]
    optional_blockers: tuple[DependencyBlocker, ...]
    ready: bool

    def __post_init__(self) -> None:
        for field, item_type in (
            ("required", ResolvedAppDependency),
            ("optional", ResolvedAppDependency),
            ("blockers", DependencyBlocker),
            ("optional_blockers", DependencyBlocker),
        ):
            value = getattr(self, field)
            if type(value) is not tuple or any(type(item) is not item_type for item in value):
                raise ValueError("dependency resolution is invalid")
        try:
            required = tuple(
                sorted(
                    (
                        ResolvedAppDependency(requirement=item.requirement, app=item.app)
                        for item in self.required
                    ),
                    key=lambda item: item.requirement.app_id,
                )
            )
            optional = tuple(
                sorted(
                    (
                        ResolvedAppDependency(requirement=item.requirement, app=item.app)
                        for item in self.optional
                    ),
                    key=lambda item: item.requirement.app_id,
                )
            )
            blockers = tuple(
                sorted(
                    (
                        DependencyBlocker(
                            app_id=item.app_id,
                            capability_kind=item.capability_kind,
                            capability_name=item.capability_name,
                            reason=item.reason,
                        )
                        for item in self.blockers
                    ),
                    key=_blocker_sort_key,
                )
            )
            optional_blockers = tuple(
                sorted(
                    (
                        DependencyBlocker(
                            app_id=item.app_id,
                            capability_kind=item.capability_kind,
                            capability_name=item.capability_name,
                            reason=item.reason,
                        )
                        for item in self.optional_blockers
                    ),
                    key=_blocker_sort_key,
                )
            )
        except ValueError as exc:
            raise ValueError("dependency resolution is invalid") from exc

        required_ids = tuple(item.requirement.app_id for item in required)
        optional_ids = tuple(item.requirement.app_id for item in optional)
        required_blocker_ids = {item.app_id for item in blockers}
        optional_blocker_ids = {item.app_id for item in optional_blockers}
        required_lane_ids = set(required_ids) | required_blocker_ids
        optional_lane_ids = set(optional_ids) | optional_blocker_ids
        if (
            len(required_ids) != len(set(required_ids))
            or len(optional_ids) != len(set(optional_ids))
            or len(blockers) != len(set(blockers))
            or len(optional_blockers) != len(set(optional_blockers))
            or set(required_ids) & required_blocker_ids
            or set(optional_ids) & optional_blocker_ids
            or required_lane_ids & optional_lane_ids
        ):
            raise ValueError("dependency resolution is invalid")
        if type(self.ready) is not bool or self.ready is bool(blockers):
            raise ValueError("dependency resolution readiness is invalid")
        object.__setattr__(self, "required", required)
        object.__setattr__(self, "optional", optional)
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "optional_blockers", optional_blockers)


def _available_snapshots(
    available_apps: tuple[AppCapabilitySnapshot, ...]
    | list[AppCapabilitySnapshot]
    | Mapping[object, AppCapabilitySnapshot],
) -> tuple[AppCapabilitySnapshot, ...]:
    if type(available_apps) is dict:
        snapshots = tuple(available_apps.values())
    elif type(available_apps) in {tuple, list}:
        snapshots = tuple(available_apps)
    else:
        raise ValueError("available app snapshots are invalid")
    if any(type(item) is not AppCapabilitySnapshot for item in snapshots):
        raise ValueError("available app snapshot is invalid")
    return snapshots


def _blocker(
    requirement: AppRequirement,
    reason: str,
    capability_kind: CapabilityKind | None = None,
    capability_name: str | None = None,
) -> DependencyBlocker:
    return DependencyBlocker(
        app_id=requirement.app_id,
        capability_kind=capability_kind,
        capability_name=capability_name,
        reason=reason,
    )


def _capability_indexes(
    app: AppCapabilitySnapshot,
) -> dict[CapabilityKind, dict[str, NamedCapability]]:
    return {
        kind: {item.name: item for item in getattr(app, field)}
        for kind, field in _CAPABILITY_FIELDS
    }


def _canonical_requirement(requirement: AppRequirement) -> AppRequirement:
    document = requirement.to_dict()
    document["transports"] = sorted(document["transports"])
    document["capabilities"] = sorted(
        document["capabilities"],
        key=lambda item: (
            item["kind"],
            item["name"],
            item["schema_sha256"] or "",
            item["version"] or "",
        ),
    )
    return AppRequirement.from_dict(document)


def _app_blockers(
    requirement: AppRequirement,
    app: AppCapabilitySnapshot,
) -> tuple[DependencyBlocker, ...]:
    blockers: list[DependencyBlocker] = []
    identity_checks = (
        (app.app_id != requirement.app_id, "app_id_mismatch"),
        (not app.installed, "app_uninstalled"),
        (not app.trusted, "app_untrusted"),
        (app.publisher_id != requirement.publisher_id, "publisher_mismatch"),
        (app.deployment is not requirement.deployment, "deployment_mismatch"),
        (app.provider_id != requirement.provider_id, "provider_mismatch"),
        (app.protocol_version != requirement.protocol_version, "protocol_mismatch"),
        (not set(app.transports).intersection(requirement.transports), "transport_mismatch"),
        (app.remote_origin != requirement.remote_origin, "origin_mismatch"),
        (
            app.authorization_issuer != requirement.authorization_issuer,
            "issuer_mismatch",
        ),
        (
            app.authentication != requirement.authentication,
            "authentication_mismatch",
        ),
    )
    blockers.extend(_blocker(requirement, reason) for failed, reason in identity_checks if failed)

    indexes = _capability_indexes(app)
    for capability in requirement.capabilities:
        match = indexes[capability.kind].get(capability.name)
        if match is None:
            blockers.append(
                _blocker(
                    requirement,
                    "capability_missing",
                    capability.kind,
                    capability.name,
                )
            )
            continue
        if capability.schema_sha256 is not None and match.schema_sha256 != capability.schema_sha256:
            blockers.append(
                _blocker(
                    requirement,
                    "schema_mismatch",
                    capability.kind,
                    capability.name,
                )
            )
        if capability.version is not None and match.version != capability.version:
            blockers.append(
                _blocker(
                    requirement,
                    "version_mismatch",
                    capability.kind,
                    capability.name,
                )
            )
    return tuple(sorted(blockers, key=_blocker_sort_key))


def _blocker_sort_key(blocker: DependencyBlocker) -> tuple[str, str, str, str]:
    return (
        blocker.app_id,
        blocker.capability_kind.value if blocker.capability_kind is not None else "",
        blocker.capability_name or "",
        blocker.reason,
    )


def _resolve_group(
    requirements: tuple[AppRequirement, ...],
    apps_by_id: dict[str, tuple[AppCapabilitySnapshot, ...]],
) -> tuple[tuple[ResolvedAppDependency, ...], tuple[DependencyBlocker, ...]]:
    resolved: list[ResolvedAppDependency] = []
    blockers: list[DependencyBlocker] = []
    for original_requirement in sorted(requirements, key=lambda item: item.app_id):
        requirement = _canonical_requirement(original_requirement)
        candidates = apps_by_id.get(requirement.app_id, ())
        if not candidates:
            blockers.append(_blocker(requirement, "app_absent"))
            continue
        if len(candidates) != 1:
            blockers.append(_blocker(requirement, "app_ambiguous"))
            continue
        app = candidates[0]
        app_blockers = _app_blockers(requirement, app)
        if app_blockers:
            blockers.extend(app_blockers)
        else:
            resolved.append(ResolvedAppDependency(requirement=requirement, app=app))
    return tuple(resolved), tuple(sorted(blockers, key=_blocker_sort_key))


def resolve_dependencies(
    manifest: PluginManifest,
    available_apps: tuple[AppCapabilitySnapshot, ...]
    | list[AppCapabilitySnapshot]
    | Mapping[object, AppCapabilitySnapshot],
) -> DependencyResolution:
    """Resolve every declared dependency without consulting mutable external state."""

    if type(manifest) is not PluginManifest:
        raise ValueError("plugin manifest is invalid")
    snapshots = _available_snapshots(available_apps)
    grouped: dict[str, list[AppCapabilitySnapshot]] = {}
    for app in snapshots:
        grouped.setdefault(app.app_id, []).append(app)
    apps_by_id = {
        app_id: tuple(sorted(apps, key=lambda item: item.capability_revision))
        for app_id, apps in grouped.items()
    }
    required, blockers = _resolve_group(manifest.required_apps, apps_by_id)
    optional, optional_blockers = _resolve_group(manifest.optional_apps, apps_by_id)
    return DependencyResolution(
        required=required,
        optional=optional,
        blockers=blockers,
        optional_blockers=optional_blockers,
        ready=not blockers,
    )
