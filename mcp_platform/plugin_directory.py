"""One product boundary for signed plugin discovery and chat activation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import re
from typing import Protocol
from urllib.parse import urlsplit

from plugin_platform.catalog_sources import CatalogSourceDescriptor, CatalogSourcePolicy
from plugin_platform.contracts import AppDeployment


_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_TRUST_TIER_VALUES = frozenset({"omega_verified", "publisher_verified", "personal"})
_SEMVER_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


class PluginDirectoryError(RuntimeError):
    """Stable directory and chat-handoff failure."""


def _identity(value: object) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise ValueError("plugin_directory_entry_invalid")
    return value


def _text(value: object, *, limit: int) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or value != value.strip()
        or len(value) > limit
    ):
        raise ValueError("plugin_directory_entry_invalid")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("plugin_directory_entry_invalid")
    return value


def _identities(values: object) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError("plugin_directory_entry_invalid")
    result = tuple(_identity(item) for item in values)
    if len(result) != len(set(result)):
        raise ValueError("plugin_directory_entry_invalid")
    return result


def _texts(values: object, *, limit: int) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError("plugin_directory_entry_invalid")
    result = tuple(_text(item, limit=limit) for item in values)
    if len(result) != len(set(result)):
        raise ValueError("plugin_directory_entry_invalid")
    return result


def _optional_identity(value: object) -> str | None:
    if value is None:
        return None
    return _identity(value)


def _optional_text(value: object, *, limit: int) -> str | None:
    if value is None:
        return None
    return _text(value, limit=limit)


def _optional_https_url(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or value != value.strip() or len(value) > 2_048:
        raise ValueError("plugin_directory_entry_invalid")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("plugin_directory_entry_invalid")
    return value


def _introduction_options(verified: object) -> tuple[str, ...]:
    """Project user-facing options only from verified signed package metadata.

    Options come from the one signed capability authority the package verifier
    already parsed, read one signed app dependency at a time.  Two apps of one
    plugin may legitimately declare the same capability name, so options are
    collected per app and only exact duplicates are collapsed.
    """

    manifest = getattr(verified, "manifest", None)
    if getattr(verified, "legacy_authority", False):
        # Spec principle 8 forbids a compatibility fork: a package the
        # verifier flagged as the retired flat authority shape has no
        # admitted capability data.  That is not the same case as a package
        # that never shipped an authority asset, so it must not fall back
        # to the manifest's generic starter requests.
        raise PluginDirectoryError("plugin_update_required")
    authority = getattr(verified, "capability_authority", None)
    if authority is None:
        starters = getattr(manifest, "starter_requests", None)
        try:
            return _texts(tuple(starters), limit=500)
        except (TypeError, ValueError):
            raise PluginDirectoryError("plugin_interaction_unavailable") from None
    try:
        app_ids = tuple(
            app.app_id for app in (*manifest.required_apps, *manifest.optional_apps)
        )
    except (AttributeError, TypeError):
        raise PluginDirectoryError("plugin_interaction_unavailable") from None
    options: list[str] = []
    for app_id in app_ids:
        try:
            tools = authority[app_id]["tools"]
        except (AttributeError, KeyError, TypeError):
            raise PluginDirectoryError("plugin_interaction_unavailable") from None
        if type(tools) is not dict:
            raise PluginDirectoryError("plugin_interaction_unavailable")
        app_options: list[str] = []
        for name in sorted(tools):
            policy = tools[name]
            if type(name) is not str or type(policy) is not dict:
                raise PluginDirectoryError("plugin_interaction_unavailable")
            try:
                title = _text(policy.get("title"), limit=500)
            except ValueError:
                raise PluginDirectoryError("plugin_interaction_unavailable") from None
            app_options.append(title if title.endswith((".", "?", "!")) else f"{title}.")
        if len(app_options) != len(set(app_options)):
            raise PluginDirectoryError("plugin_interaction_unavailable")
        options.extend(item for item in app_options if item not in options)
    if not options:
        raise PluginDirectoryError("plugin_interaction_unavailable")
    return tuple(options)


@dataclass(frozen=True, slots=True)
class CatalogPluginEntry:
    """Signed, provider-neutral plugin metadata exposed by one source."""

    plugin_id: str
    publisher_id: str
    plugin_version: str
    title: str
    description: str
    signing_key_id: str
    manifest_sha256: str
    package_sha256: str
    required_app_ids: tuple[str, ...]
    optional_app_ids: tuple[str, ...]
    starter_requests: tuple[str, ...]
    entry_point_ids: tuple[str, ...]
    categories: tuple[str, ...] = ()
    #: No default: a construction site must always state its own tier rather
    #: than silently inheriting the highest one. See
    #: ``tests/mcp_platform/test_plugin_source_of_truth.py``'s
    #: ``test_no_code_path_assigns_omega_verified_from_publisher_metadata``.
    trust_tier: str = field(kw_only=True)
    provider_publisher_id: str | None = None
    publisher_display_name: str | None = None
    provider_id: str | None = None
    remote_origin: str | None = None
    authoritative_source_url: str | None = None
    provenance: str = "signed_plugin_package"

    def __post_init__(self) -> None:
        try:
            plugin_id = _identity(self.plugin_id)
            publisher_id = _identity(self.publisher_id)
            signing_key_id = _identity(self.signing_key_id)
            if type(self.plugin_version) is not str or _SEMVER_RE.fullmatch(self.plugin_version) is None:
                raise ValueError
            title = _text(self.title, limit=120)
            description = _text(self.description, limit=500)
            manifest = _digest(self.manifest_sha256)
            package = _digest(self.package_sha256)
            required = _identities(self.required_app_ids)
            optional = _identities(self.optional_app_ids)
            if set(required) & set(optional):
                raise ValueError
            starters = _texts(self.starter_requests, limit=500)
            entry_points = _identities(self.entry_point_ids)
            categories = _identities(self.categories)
            if not entry_points:
                raise ValueError
            if type(self.trust_tier) is not str or self.trust_tier not in _TRUST_TIER_VALUES:
                raise ValueError
            provider_publisher_id = _optional_identity(self.provider_publisher_id)
            publisher_display_name = _optional_text(self.publisher_display_name, limit=120)
            provider_id = _optional_identity(self.provider_id)
            remote_origin = _optional_https_url(self.remote_origin)
            authoritative_source_url = _optional_https_url(self.authoritative_source_url)
            provenance = _identity(self.provenance)
            authoritative_values = (
                provider_publisher_id,
                publisher_display_name,
                provider_id,
                remote_origin,
                authoritative_source_url,
            )
            if provenance == "authoritative_remote_mcp":
                if any(item is None for item in authoritative_values):
                    raise ValueError
            elif provenance != "signed_plugin_package" or any(
                item is not None for item in authoritative_values
            ):
                raise ValueError
        except ValueError:
            raise ValueError("plugin_directory_entry_invalid") from None
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "publisher_id", publisher_id)
        object.__setattr__(self, "signing_key_id", signing_key_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "manifest_sha256", manifest)
        object.__setattr__(self, "package_sha256", package)
        object.__setattr__(self, "required_app_ids", required)
        object.__setattr__(self, "optional_app_ids", optional)
        object.__setattr__(self, "starter_requests", starters)
        object.__setattr__(self, "entry_point_ids", entry_points)
        object.__setattr__(self, "categories", categories)
        object.__setattr__(self, "provider_publisher_id", provider_publisher_id)
        object.__setattr__(self, "publisher_display_name", publisher_display_name)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "remote_origin", remote_origin)
        object.__setattr__(self, "authoritative_source_url", authoritative_source_url)
        object.__setattr__(self, "provenance", provenance)


@dataclass(frozen=True, slots=True)
class CatalogSourceSnapshot:
    source: CatalogSourceDescriptor
    revision: str
    plugins: tuple[CatalogPluginEntry, ...]

    def __post_init__(self) -> None:
        if type(self.source) is not CatalogSourceDescriptor:
            raise ValueError("catalog_source_snapshot_invalid")
        try:
            revision = _digest(self.revision)
        except ValueError:
            raise ValueError("catalog_source_snapshot_invalid") from None
        if type(self.plugins) is not tuple or any(
            type(item) is not CatalogPluginEntry for item in self.plugins
        ):
            raise ValueError("catalog_source_snapshot_invalid")
        ids = tuple(item.plugin_id for item in self.plugins)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("catalog_source_snapshot_invalid")
        object.__setattr__(self, "revision", revision)


class CatalogSourceAdapter(Protocol):
    descriptor: CatalogSourceDescriptor

    def snapshot(self) -> CatalogSourceSnapshot: ...

    def review(self, plugin_id: str) -> object: ...

    def install(self, plugin_id: str, *, confirmation: object) -> object: ...


@dataclass(frozen=True, slots=True)
class PluginDirectoryEntry:
    plugin_id: str
    publisher_id: str
    plugin_version: str
    title: str
    description: str
    signing_key_id: str
    manifest_sha256: str
    package_sha256: str
    required_app_ids: tuple[str, ...]
    optional_app_ids: tuple[str, ...]
    starter_requests: tuple[str, ...]
    entry_point_ids: tuple[str, ...]
    categories: tuple[str, ...]
    source_id: str
    source_kind: str
    source_state: str
    trust_state: str
    compatibility_state: str
    update_state: str
    installed: bool
    enabled: bool
    connection_state: str
    blocker: str | None
    package_publisher_id: str
    publisher_display_name: str
    provider_id: str | None
    remote_origin: str | None
    authoritative_source_url: str | None
    provenance: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "publisher_id": self.publisher_id,
            "publisher_display_name": self.publisher_display_name,
            "package_publisher_id": self.package_publisher_id,
            "provider_id": self.provider_id,
            "remote_origin": self.remote_origin,
            "authoritative_source_url": self.authoritative_source_url,
            "provenance": self.provenance,
            "plugin_version": self.plugin_version,
            "title": self.title,
            "description": self.description,
            "signing_key_id": self.signing_key_id,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "required_app_ids": list(self.required_app_ids),
            "optional_app_ids": list(self.optional_app_ids),
            "starter_requests": list(self.starter_requests),
            "entry_point_ids": list(self.entry_point_ids),
            "categories": list(self.categories),
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "source_state": self.source_state,
            "trust_state": self.trust_state,
            "compatibility_state": self.compatibility_state,
            "update_state": self.update_state,
            "installed": self.installed,
            "enabled": self.enabled,
            "connection_state": self.connection_state,
            "blocker": self.blocker,
        }


@dataclass(frozen=True, slots=True)
class PluginDirectorySnapshot:
    status: str
    plugins: tuple[PluginDirectoryEntry, ...]
    source_errors: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "plugins": [item.to_dict() for item in self.plugins],
            "source_errors": [
                {"source_id": source_id, "reason": reason}
                for source_id, reason in self.source_errors
            ],
        }


@dataclass(frozen=True, slots=True)
class TryInChatHandoff:
    plugin_id: str
    title: str
    intro_text: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "title": self.title,
            "intro_text": self.intro_text,
        }


class PluginDirectoryService:
    """Merge signed sources with durable install state and chat handoffs."""

    def __init__(
        self,
        *,
        sources: Sequence[CatalogSourceAdapter],
        plugin_registry: object,
        source_policy: CatalogSourcePolicy | None = None,
    ) -> None:
        source_tuple = tuple(sources)
        if any(not isinstance(item.descriptor, CatalogSourceDescriptor) for item in source_tuple):
            raise ValueError("catalog_source_invalid")
        policy = source_policy or CatalogSourcePolicy(developer_mode=False)
        if type(policy) is not CatalogSourcePolicy:
            raise ValueError("catalog_source_policy_invalid")
        for item in source_tuple:
            policy.admit(item.descriptor)
        source_ids = tuple(item.descriptor.source_id for item in source_tuple)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("catalog_source_duplicate")
        self.sources = source_tuple
        self.source_policy = policy
        self.plugin_registry = plugin_registry

    @staticmethod
    def _connection_state(record: object | None) -> str:
        if record is None:
            return "not_installed"
        blocker = getattr(record, "blocker", None)
        if blocker:
            return "blocked"
        return "connected" if getattr(record, "enabled", False) is True else "connection_required"

    def _entry(
        self,
        plugin: CatalogPluginEntry,
        *,
        source: CatalogSourceDescriptor,
        source_state: str,
        record: object | None,
    ) -> PluginDirectoryEntry:
        installed = record is not None
        selected = str(getattr(record, "selected_version", "") or "")
        update_state = "available" if installed and selected != plugin.plugin_version else "current"
        return PluginDirectoryEntry(
            plugin_id=plugin.plugin_id,
            publisher_id=plugin.provider_publisher_id or plugin.publisher_id,
            plugin_version=plugin.plugin_version,
            title=plugin.title,
            description=plugin.description,
            signing_key_id=plugin.signing_key_id,
            manifest_sha256=plugin.manifest_sha256,
            package_sha256=plugin.package_sha256,
            required_app_ids=plugin.required_app_ids,
            optional_app_ids=plugin.optional_app_ids,
            starter_requests=plugin.starter_requests,
            entry_point_ids=plugin.entry_point_ids,
            categories=plugin.categories,
            source_id=source.source_id,
            source_kind=source.kind.value,
            source_state=source_state,
            trust_state=plugin.trust_tier,
            compatibility_state="compatible",
            update_state=update_state,
            installed=installed,
            enabled=bool(getattr(record, "enabled", False)) if installed else False,
            connection_state=self._connection_state(record),
            blocker=(str(getattr(record, "blocker")) if getattr(record, "blocker", None) else None),
            package_publisher_id=plugin.publisher_id,
            publisher_display_name=plugin.publisher_display_name or plugin.publisher_id,
            provider_id=plugin.provider_id,
            remote_origin=plugin.remote_origin,
            authoritative_source_url=plugin.authoritative_source_url,
            provenance=plugin.provenance,
        )

    @staticmethod
    def _installed_plugin(record: object, verified: object) -> CatalogPluginEntry:
        manifest = verified.manifest
        return CatalogPluginEntry(
            plugin_id=manifest.plugin_id,
            publisher_id=manifest.publisher_id,
            plugin_version=manifest.plugin_version,
            title=manifest.title,
            description=manifest.description,
            signing_key_id=manifest.signing_key_id,
            manifest_sha256=verified.manifest_sha256,
            package_sha256=verified.package_sha256,
            required_app_ids=tuple(item.app_id for item in manifest.required_apps),
            optional_app_ids=tuple(item.app_id for item in manifest.optional_apps),
            starter_requests=tuple(manifest.starter_requests),
            entry_point_ids=tuple(item.entry_point_id for item in manifest.entry_points),
            categories=(),
            trust_tier=verified.trust_tier.value,
        )

    @staticmethod
    def installed_plugin_summary(verified: object) -> dict[str, object]:
        """Validation-only projection of one installed plugin's sealed
        title, summary, and trust tier from its already fully re-verified
        package (``verified`` is a ``plugin_platform.package_verifier.
        VerifiedPlugin`` -- see ``PluginRegistry.reverify_installed``).

        This makes no new trust decision: it only restates, in a stable
        shape, exactly what ``VerifiedPlugin``/``PluginManifest`` already
        validated when the package was verified (the same fields
        ``_installed_plugin`` above already trusts to build a fallback
        catalog entry for an installed plugin absent from every catalog
        source). Unlike a catalog listing, this never depends on the
        plugin still being present in any admitted catalog source or page
        -- it is the installed package's own identity, so it is what the
        HUD's installed strip and chat composer sync use to name an
        installed plugin regardless of catalog pagination or a Personal
        plugin with no catalog listing at all.
        """
        manifest = verified.manifest
        title = manifest.title
        summary = manifest.description
        trust_tier = verified.trust_tier.value
        if type(title) is not str or not title:
            raise ValueError("plugin_directory_entry_invalid")
        if type(summary) is not str:
            raise ValueError("plugin_directory_entry_invalid")
        if type(trust_tier) is not str or not trust_tier:
            raise ValueError("plugin_directory_entry_invalid")
        return {"title": title, "summary": summary, "trust_tier": trust_tier}

    @staticmethod
    def installed_plugin_authority(verified: object) -> dict[str, object]:
        """Validation-only projection of one installed plugin's declared
        capabilities, its per-app requested provider scopes and
        authentication kind, and its network/filesystem boundaries -- all
        read directly from the already re-verified sealed manifest (the same
        ``VerifiedPlugin`` ``installed_plugin_summary`` above restates).

        This makes no new trust decision and triggers no package download:
        it only restates, in a stable shape, exactly what ``VerifiedPlugin``/
        ``PluginManifest`` already validated when the installed package was
        last (re)verified. Used by the HUD's plugin details route to show
        what an already-installed plugin's currently active authority looks
        like -- never a claim about an unreviewed catalog candidate, which
        this method never sees.
        """
        manifest = verified.manifest
        requirements = tuple(manifest.required_apps) + tuple(manifest.optional_apps)
        if not requirements:
            raise ValueError("plugin_directory_entry_invalid")
        capability_tokens: list[str] = []
        seen_capabilities: set[str] = set()
        apps: list[dict[str, object]] = []
        network_origins: set[str] = set()
        filesystem_access = False
        for requirement in requirements:
            app_id = requirement.app_id
            authentication = requirement.authentication
            if type(app_id) is not str or not app_id:
                raise ValueError("plugin_directory_entry_invalid")
            authentication_kind = authentication.kind.value
            scopes = tuple(authentication.scopes)
            if any(type(scope) is not str for scope in scopes):
                raise ValueError("plugin_directory_entry_invalid")
            remote_origin = requirement.remote_origin
            if remote_origin is not None and type(remote_origin) is not str:
                raise ValueError("plugin_directory_entry_invalid")
            apps.append(
                {
                    "app_id": app_id,
                    "authentication_kind": authentication_kind,
                    "requested_scopes": list(scopes),
                    "remote_origin": remote_origin,
                }
            )
            for capability in requirement.capabilities:
                name = capability.name
                if type(name) is not str or not name:
                    raise ValueError("plugin_directory_entry_invalid")
                token = f"{capability.kind.value}:{name}"
                if token not in seen_capabilities:
                    seen_capabilities.add(token)
                    capability_tokens.append(token)
            if requirement.deployment is AppDeployment.REMOTE and remote_origin:
                network_origins.add(remote_origin)
            if requirement.deployment is AppDeployment.LOCAL:
                filesystem_access = True
        return {
            "capabilities": capability_tokens,
            "apps": apps,
            "network_origins": sorted(network_origins),
            "filesystem_access": filesystem_access,
        }

    def list_directory(self) -> PluginDirectorySnapshot:
        registry_snapshot = self.plugin_registry.snapshot()
        installed = {item.plugin_id: item for item in registry_snapshot.records}
        merged: dict[str, PluginDirectoryEntry] = {}
        errors: list[tuple[str, str]] = []
        failed_sources: list[CatalogSourceDescriptor] = []
        for source in self.sources:
            try:
                snapshot = source.snapshot()
                if snapshot.source != source.descriptor:
                    raise ValueError("catalog_source_snapshot_invalid")
                for plugin in snapshot.plugins:
                    if plugin.plugin_id in merged:
                        raise PluginDirectoryError("plugin_directory_identity_ambiguous")
                    merged[plugin.plugin_id] = self._entry(
                        plugin,
                        source=source.descriptor,
                        source_state="ready",
                        record=installed.get(plugin.plugin_id),
                    )
            except PluginDirectoryError:
                raise
            except Exception:
                errors.append((source.descriptor.source_id, "catalog_source_unavailable"))
                failed_sources.append(source.descriptor)
        fallback_source = (
            failed_sources[0]
            if failed_sources
            else CatalogSourceDescriptor(
                source_id="installed.local",
                title="Installed plugins",
                kind="installed",
            )
        )
        for plugin_id, record in installed.items():
            if plugin_id in merged:
                continue
            try:
                verified = self.plugin_registry.reverify_installed(plugin_id)
                plugin = self._installed_plugin(record, verified)
            except Exception:
                continue
            merged[plugin_id] = self._entry(
                plugin,
                source=fallback_source,
                source_state="unavailable" if failed_sources else "installed",
                record=record,
            )
        return PluginDirectorySnapshot(
            status="degraded" if errors else "ready",
            plugins=tuple(merged[key] for key in sorted(merged)),
            source_errors=tuple(sorted(errors)),
        )

    def _source_for(self, plugin_id: str) -> CatalogSourceAdapter:
        identity = _identity(plugin_id)
        matches: list[CatalogSourceAdapter] = []
        for source in self.sources:
            try:
                if any(item.plugin_id == identity for item in source.snapshot().plugins):
                    matches.append(source)
            except Exception:
                continue
        if len(matches) != 1:
            raise PluginDirectoryError("plugin_directory_entry_unavailable")
        return matches[0]

    def review(self, plugin_id: str) -> object:
        return self._source_for(plugin_id).review(plugin_id)

    def install(self, plugin_id: str, *, confirmation: object) -> object:
        return self._source_for(plugin_id).install(plugin_id, confirmation=confirmation)

    def _current_record(self, plugin_id: str) -> tuple[object, object]:
        identity = _identity(plugin_id)
        snapshot = self.plugin_registry.snapshot()
        record = next((item for item in snapshot.records if item.plugin_id == identity), None)
        if record is None:
            raise PluginDirectoryError("plugin_not_installed")
        return snapshot, record

    def chat_selector(self, plugin_id: str) -> dict[str, str]:
        _snapshot, record = self._current_record(plugin_id)
        if getattr(record, "enabled", False) is not True:
            raise PluginDirectoryError("plugin_connection_required")
        if getattr(record, "blocker", None):
            raise PluginDirectoryError("plugin_interaction_unavailable")
        return {"plugin_id": record.plugin_id}

    def try_in_chat_handoff(self, plugin_id: str) -> TryInChatHandoff:
        _snapshot, record = self._current_record(plugin_id)
        self.chat_selector(plugin_id)
        directory_entry = next(
            (
                item
                for item in self.list_directory().plugins
                if item.plugin_id == record.plugin_id
            ),
            None,
        )
        if directory_entry is None:
            raise PluginDirectoryError("plugin_directory_entry_unavailable")
        verified = self.plugin_registry.reverify_installed(record.plugin_id)
        starter_requests = _introduction_options(verified)
        if not starter_requests:
            raise PluginDirectoryError("plugin_interaction_unavailable")
        options = "\n".join(f"- {text}" for text in starter_requests)
        return TryInChatHandoff(
            plugin_id=record.plugin_id,
            title=directory_entry.title,
            intro_text=(
                f"{directory_entry.title} is connected. I can help you:\n\n"
                f"{options}\n\n"
                "Choose an option or describe what you want to do."
            ),
        )


__all__ = [
    "CatalogPluginEntry",
    "CatalogSourceAdapter",
    "CatalogSourceSnapshot",
    "PluginDirectoryEntry",
    "PluginDirectoryError",
    "PluginDirectoryService",
    "PluginDirectorySnapshot",
    "TryInChatHandoff",
]
