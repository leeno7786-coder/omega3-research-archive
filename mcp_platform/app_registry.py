"""Durable content-free identities for installed dormant MCP apps."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import paths as core_paths
from mcp_platform.canonical import canonical_bytes
from mcp_platform.app_sources import (
    TrustedRemoteAppSource,
    VerifiedLocalAppSource,
    _validated_source_state,
)
from mcp_platform.registry import InstalledPackageRecord
from mcp_platform.state_store import McpStateStore
from plugin_platform.contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    AppRequirement,
    CapabilityRequirement,
    McpTransport,
    PluginAuthentication,
    parse_app_id,
)

_DOCUMENT_FIELDS = frozenset({"schema_version", "apps"})
_RECORD_FIELDS = frozenset(
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
        "local_package_version",
        "local_package_platform",
        "local_package_manifest_sha256",
        "installed",
        "trusted",
        "enabled",
    }
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PACKAGE_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_RECORD_AUTHORITY = object()
_DEFAULT_MAX_REGISTRY_DOCUMENT_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_APP_RECORDS = 4_096


class McpAppRegistryError(RuntimeError):
    """A stable dormant-app registry failure."""


class McpAppRegistryUnreadable(McpAppRegistryError):
    """The app registry exists but is not its exact canonical schema."""


class McpAppNotInstalled(McpAppRegistryError):
    """A requested app identity is absent."""


class McpAppIdentityCollision(McpAppRegistryError):
    """Different trusted identity claimed an installed app ID."""


class McpAppRegistryConflict(McpAppRegistryError):
    """The caller's observed app-registry revision is stale."""


def _canonical_requirement(
    *,
    app_id: object,
    publisher_id: object,
    deployment: object,
    provider_id: object,
    protocol_version: object,
    transports: object,
    remote_origin: object,
    authorization_issuer: object,
    authentication: object,
    capabilities: object,
) -> AppRequirement:
    if type(protocol_version) is not str or protocol_version != PLUGIN_PROTOCOL_VERSION:
        raise ValueError("mcp_app_identity_invalid")
    if type(transports) is not tuple or any(type(item) is not McpTransport for item in transports):
        raise ValueError("mcp_app_identity_invalid")
    if type(capabilities) is not tuple or any(
        type(item) is not CapabilityRequirement for item in capabilities
    ):
        raise ValueError("mcp_app_identity_invalid")
    if type(authentication) is not PluginAuthentication:
        raise ValueError("mcp_app_identity_invalid")
    try:
        document = {
            "app_id": app_id,
            "publisher_id": publisher_id,
            "deployment": deployment.value if type(deployment) is AppDeployment else deployment,
            "provider_id": provider_id,
            "protocol_version": protocol_version,
            "transports": sorted(item.value for item in transports),
            "remote_origin": remote_origin,
            "authorization_issuer": authorization_issuer,
            "authentication": authentication.to_dict(),
            "capabilities": sorted(
                (item.to_dict() for item in capabilities),
                key=lambda item: (
                    item["kind"],
                    item["name"],
                    item["schema_sha256"] or "",
                    item["version"] or "",
                ),
            ),
        }
        return AppRequirement.from_dict(document)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("mcp_app_identity_invalid") from None


def _package_identity(value: object) -> str:
    if type(value) is not str or _PACKAGE_IDENTITY_RE.fullmatch(value) is None:
        raise ValueError("mcp_app_identity_invalid")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("mcp_app_identity_invalid")
    return value


@dataclass(frozen=True, slots=True)
class InstalledMcpAppRecord:
    """Exact trusted app declaration plus a local package binding when needed."""

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
    local_package_version: str | None
    local_package_platform: str | None
    local_package_manifest_sha256: str | None
    installed: bool
    trusted: bool
    enabled: bool
    _authority: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.installed) is not bool
            or type(self.trusted) is not bool
            or type(self.enabled) is not bool
            or not self.installed
            or not self.trusted
            or self.enabled
        ):
            raise ValueError("mcp_app_state_invalid")
        requirement = _canonical_requirement(
            app_id=self.app_id,
            publisher_id=self.publisher_id,
            deployment=self.deployment,
            provider_id=self.provider_id,
            protocol_version=self.protocol_version,
            transports=self.transports,
            remote_origin=self.remote_origin,
            authorization_issuer=self.authorization_issuer,
            authentication=self.authentication,
            capabilities=self.capabilities,
        )
        if requirement.deployment is AppDeployment.LOCAL:
            if (
                requirement.transports != (McpTransport.STDIO,)
                or requirement.remote_origin is not None
                or requirement.authorization_issuer is not None
                or self.local_package_version is None
                or self.local_package_platform is None
                or self.local_package_manifest_sha256 is None
            ):
                raise ValueError("mcp_app_identity_invalid")
            local_version = _package_identity(self.local_package_version)
            local_platform = _package_identity(self.local_package_platform)
            local_manifest = _digest(self.local_package_manifest_sha256)
        else:
            if (
                requirement.transports != (McpTransport.STREAMABLE_HTTP,)
                or requirement.remote_origin is None
                or requirement.authorization_issuer is None
                or self.local_package_version is not None
                or self.local_package_platform is not None
                or self.local_package_manifest_sha256 is not None
            ):
                raise ValueError("mcp_app_identity_invalid")
            local_version = None
            local_platform = None
            local_manifest = None
        object.__setattr__(self, "app_id", requirement.app_id)
        object.__setattr__(self, "publisher_id", requirement.publisher_id)
        object.__setattr__(self, "deployment", requirement.deployment)
        object.__setattr__(self, "provider_id", requirement.provider_id)
        object.__setattr__(self, "protocol_version", requirement.protocol_version)
        object.__setattr__(self, "transports", requirement.transports)
        object.__setattr__(self, "remote_origin", requirement.remote_origin)
        object.__setattr__(
            self,
            "authorization_issuer",
            requirement.authorization_issuer,
        )
        object.__setattr__(self, "authentication", requirement.authentication)
        object.__setattr__(self, "capabilities", requirement.capabilities)
        object.__setattr__(self, "local_package_version", local_version)
        object.__setattr__(self, "local_package_platform", local_platform)
        object.__setattr__(self, "local_package_manifest_sha256", local_manifest)

    @classmethod
    def from_requirement(
        cls,
        requirement: AppRequirement,
        *,
        package: InstalledPackageRecord | None = None,
    ) -> InstalledMcpAppRecord:
        del requirement, package
        raise ValueError("mcp_app_identity_invalid")

    @classmethod
    def from_source(
        cls,
        source: VerifiedLocalAppSource | TrustedRemoteAppSource,
    ) -> InstalledMcpAppRecord:
        try:
            canonical, verified_package = _validated_source_state(source)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("mcp_app_identity_invalid") from None
        if canonical.deployment is AppDeployment.LOCAL:
            if verified_package is None:
                raise ValueError("mcp_app_identity_invalid")
            package = InstalledPackageRecord.from_verified(verified_package)
            local_version = package.server_version
            local_platform = package.platform
            local_manifest = package.manifest_digest
        elif canonical.deployment is AppDeployment.REMOTE and verified_package is None:
            local_version = None
            local_platform = None
            local_manifest = None
        else:
            raise ValueError("mcp_app_identity_invalid")
        record = cls(
            app_id=canonical.app_id,
            publisher_id=canonical.publisher_id,
            deployment=canonical.deployment,
            provider_id=canonical.provider_id,
            protocol_version=canonical.protocol_version,
            transports=canonical.transports,
            remote_origin=canonical.remote_origin,
            authorization_issuer=canonical.authorization_issuer,
            authentication=canonical.authentication,
            capabilities=canonical.capabilities,
            local_package_version=local_version,
            local_package_platform=local_platform,
            local_package_manifest_sha256=local_manifest,
            installed=True,
            trusted=True,
            enabled=False,
        )
        return _seal_record(record)

    @classmethod
    def from_dict(
        cls,
        raw: Any,
        *,
        registry_key: str,
    ) -> InstalledMcpAppRecord:
        data = _exact_object(raw, _RECORD_FIELDS)
        if data["app_id"] != registry_key:
            raise ValueError("mcp_app_identity_invalid")
        if type(data["transports"]) is not list or type(data["capabilities"]) is not list:
            raise ValueError("mcp_app_identity_invalid")
        try:
            requirement = AppRequirement.from_dict(
                {
                    "app_id": data["app_id"],
                    "publisher_id": data["publisher_id"],
                    "deployment": data["deployment"],
                    "provider_id": data["provider_id"],
                    "protocol_version": data["protocol_version"],
                    "transports": data["transports"],
                    "remote_origin": data["remote_origin"],
                    "authorization_issuer": data["authorization_issuer"],
                    "authentication": data["authentication"],
                    "capabilities": data["capabilities"],
                }
            )
        except (TypeError, ValueError):
            raise ValueError("mcp_app_identity_invalid") from None
        return cls(
            app_id=requirement.app_id,
            publisher_id=requirement.publisher_id,
            deployment=requirement.deployment,
            provider_id=requirement.provider_id,
            protocol_version=requirement.protocol_version,
            transports=requirement.transports,
            remote_origin=requirement.remote_origin,
            authorization_issuer=requirement.authorization_issuer,
            authentication=requirement.authentication,
            capabilities=requirement.capabilities,
            local_package_version=data["local_package_version"],
            local_package_platform=data["local_package_platform"],
            local_package_manifest_sha256=data["local_package_manifest_sha256"],
            installed=data["installed"],
            trusted=data["trusted"],
            enabled=data["enabled"],
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
            "local_package_version": self.local_package_version,
            "local_package_platform": self.local_package_platform,
            "local_package_manifest_sha256": self.local_package_manifest_sha256,
            "installed": self.installed,
            "trusted": self.trusted,
            "enabled": self.enabled,
        }


def _seal_record(record: InstalledMcpAppRecord) -> InstalledMcpAppRecord:
    object.__setattr__(record, "_authority", _RECORD_AUTHORITY)
    return record


def _record_from_durable(raw: Any, *, registry_key: str) -> InstalledMcpAppRecord:
    return _seal_record(InstalledMcpAppRecord.from_dict(raw, registry_key=registry_key))


def _validated_record(record: object) -> InstalledMcpAppRecord:
    try:
        if (
            type(record) is not InstalledMcpAppRecord
            or object.__getattribute__(record, "_authority") is not _RECORD_AUTHORITY
        ):
            raise ValueError
        return _record_from_durable(record.to_dict(), registry_key=record.app_id)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("mcp_app_identity_invalid") from None


@dataclass(frozen=True, slots=True)
class McpAppRegistrySnapshot:
    """Immutable sorted app records and their canonical registry revision."""

    records: tuple[InstalledMcpAppRecord, ...]
    revision: str

    def __post_init__(self) -> None:
        try:
            if type(self.records) is not tuple:
                raise ValueError
            records = tuple(_validated_record(record) for record in self.records)
            if tuple(record.app_id for record in records) != tuple(
                sorted(record.app_id for record in records)
            ):
                raise ValueError
            by_id = {record.app_id: record for record in records}
            if len(by_id) != len(records):
                raise ValueError
            if type(self.revision) is not str or self.revision != _revision_for(by_id):
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ValueError("mcp_app_snapshot_invalid") from None
        object.__setattr__(self, "records", records)


class McpAppRegistry:
    """Revision-checked durable mutations for trusted dormant app identities."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        max_records: int = _DEFAULT_MAX_APP_RECORDS,
        max_document_bytes: int = _DEFAULT_MAX_REGISTRY_DOCUMENT_BYTES,
    ) -> None:
        if type(max_records) is not int or max_records <= 0:
            raise ValueError("max_records must be a positive integer")
        if type(max_document_bytes) is not int or max_document_bytes <= 0:
            raise ValueError("max_document_bytes must be a positive integer")
        self.state_root = Path(state_root) if state_root is not None else core_paths.MCP_STATE_ROOT
        self.registry_path = self.state_root / "apps_registry.json"
        self._max_records = max_records
        self._max_document_bytes = max_document_bytes
        self._store = McpStateStore(
            state_root=self.state_root,
            document_path=self.registry_path,
            invalid_path_reason="mcp_state_path_invalid",
            max_document_bytes=max_document_bytes,
        )

    def snapshot(self) -> McpAppRegistrySnapshot:
        with self._store.locked():
            return _snapshot_for(self._load_unlocked())

    def upgrade_pre_authentication_exact(
        self, *, expected_payload_sha256: str, proofs: tuple[object, ...]
    ) -> McpAppRegistrySnapshot:
        """One-shot offline upgrade; ordinary readers never accept the old schema.

        Every old record must be covered by signed no-permission-change evidence.
        This method neither reads nor changes connection authority or credentials.
        """
        from mcp_platform.pre_auth_recovery import validated_upgrade

        reason = "plugin_legacy_recovery_invalid"
        with self._store.locked():
            payload = self._store.read_bytes()
            if (
                payload is None or type(proofs) is not tuple or not proofs
                or hashlib.sha256(payload).hexdigest() != expected_payload_sha256
            ):
                raise ValueError(reason)
            try:
                raw = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
                if (
                    type(raw) is not dict or set(raw) != {"schema_version", "apps"}
                    or type(raw["schema_version"]) is not int or raw["schema_version"] != 1
                    or type(raw["apps"]) is not dict or canonical_bytes(raw) != payload
                ):
                    raise ValueError(reason)
                replacements = {}
                for proof in proofs:
                    old_record_bytes, replacement = validated_upgrade(proof)
                    record = _validated_record(replacement)
                    if (
                        record.app_id in replacements
                        or canonical_bytes(raw["apps"].get(record.app_id)) != old_record_bytes
                    ):
                        raise ValueError(reason)
                    replacements[record.app_id] = record
                if set(replacements) != set(raw["apps"]):
                    raise ValueError(reason)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(reason) from exc
            self._write(replacements)
            return _snapshot_for(replacements)

    def list_records(self) -> tuple[InstalledMcpAppRecord, ...]:
        return self.snapshot().records

    def get(self, app_id: str) -> InstalledMcpAppRecord:
        identity = _app_id(app_id)
        with self._store.locked():
            records = self._load_unlocked()
            try:
                return records[identity]
            except KeyError:
                raise McpAppNotInstalled("mcp_app_not_installed") from None

    def install(
        self,
        record: InstalledMcpAppRecord,
        *,
        expected_revision: str,
    ) -> McpAppRegistrySnapshot:
        expected = _expected_revision(expected_revision)
        validated = _validated_record(record)
        return self.apply_exact(
            install_records=(validated,),
            remove_records=(),
            expected_revision=expected,
        )

    def remove(
        self,
        app_id: str,
        *,
        expected_revision: str,
    ) -> McpAppRegistrySnapshot:
        expected = _expected_revision(expected_revision)
        identity = _app_id(app_id)
        with self._store.locked():
            records = self._load_unlocked()
            current = _snapshot_for(records)
            if expected != current.revision:
                raise McpAppRegistryConflict("mcp_app_registry_conflict")
            if identity not in records:
                raise McpAppNotInstalled("mcp_app_not_installed")
            del records[identity]
            self._write(records)
            return _snapshot_for(records)

    def replace_exact(
        self,
        *,
        expected_record: InstalledMcpAppRecord,
        replacement_record: InstalledMcpAppRecord,
        expected_revision: str,
    ) -> McpAppRegistrySnapshot:
        """Atomically replace one exact trusted app descriptor through CAS."""

        expected = _expected_revision(expected_revision)
        installed = _validated_record(expected_record)
        replacement = _validated_record(replacement_record)
        if installed.app_id != replacement.app_id:
            raise ValueError("mcp_app_batch_invalid")
        with self._store.locked():
            records = self._load_unlocked()
            current = _snapshot_for(records)
            if expected != current.revision:
                raise McpAppRegistryConflict("mcp_app_registry_conflict")
            observed = records.get(installed.app_id)
            if observed is None:
                raise McpAppNotInstalled("mcp_app_not_installed")
            if observed != installed:
                raise McpAppIdentityCollision("mcp_app_identity_collision")
            if replacement != observed:
                records[installed.app_id] = replacement
                self._write(records)
            return _snapshot_for(records)

    def apply_exact(
        self,
        *,
        install_records: tuple[InstalledMcpAppRecord, ...],
        remove_records: tuple[InstalledMcpAppRecord, ...],
        expected_revision: str,
    ) -> McpAppRegistrySnapshot:
        """Apply exact sealed additions/removals through one revision-CAS write."""
        expected = _expected_revision(expected_revision)
        installs = _validated_batch(install_records)
        removals = _validated_batch(remove_records)
        if set(installs) & set(removals):
            raise ValueError("mcp_app_batch_invalid")
        with self._store.locked():
            records = self._load_unlocked()
            current = _snapshot_for(records)
            if expected != current.revision:
                raise McpAppRegistryConflict("mcp_app_registry_conflict")
            for identity, record in removals.items():
                installed = records.get(identity)
                if installed is None:
                    raise McpAppNotInstalled("mcp_app_not_installed")
                if installed != record:
                    raise McpAppIdentityCollision("mcp_app_identity_collision")
            for identity, record in installs.items():
                installed = records.get(identity)
                if installed is not None and installed != record:
                    raise McpAppIdentityCollision("mcp_app_identity_collision")
            projected = (set(records) - set(removals)) | set(installs)
            if len(projected) > self._max_records:
                raise McpAppRegistryError("mcp_app_registry_limit_exceeded")
            changed = False
            for identity in removals:
                del records[identity]
                changed = True
            for identity, record in installs.items():
                if identity not in records:
                    records[identity] = record
                    changed = True
            if changed:
                self._write(records)
            return _snapshot_for(records)

    def replace_many_exact(
        self,
        *,
        installed_records: tuple[InstalledMcpAppRecord, ...],
        replacement_records: tuple[InstalledMcpAppRecord, ...],
        expected_revision: str,
    ) -> McpAppRegistrySnapshot:
        """Replace an exact sealed record set through one revision-CAS write."""

        expected = _expected_revision(expected_revision)
        installed = _validated_batch(installed_records)
        replacements = _validated_batch(replacement_records)
        if set(installed) != set(replacements):
            raise ValueError("mcp_app_batch_invalid")
        with self._store.locked():
            records = self._load_unlocked()
            current = _snapshot_for(records)
            if expected != current.revision:
                raise McpAppRegistryConflict("mcp_app_registry_conflict")
            for identity, record in installed.items():
                observed = records.get(identity)
                if observed is None:
                    raise McpAppNotInstalled("mcp_app_not_installed")
                if observed != record:
                    raise McpAppIdentityCollision("mcp_app_identity_collision")
            changed = False
            for identity, replacement in replacements.items():
                if records[identity] != replacement:
                    records[identity] = replacement
                    changed = True
            if changed:
                self._write(records)
            return _snapshot_for(records)

    def _load_unlocked(self) -> dict[str, InstalledMcpAppRecord]:
        payload = self._store.read_bytes()
        if payload is None:
            return {}
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
            document = _exact_object(raw, _DOCUMENT_FIELDS)
            if (
                type(document["schema_version"]) is not int
                or document["schema_version"] != 1
                or type(document["apps"]) is not dict
                or len(document["apps"]) > self._max_records
            ):
                raise ValueError("mcp app registry root invalid")
            records = {
                _app_id(key): _record_from_durable(value, registry_key=key)
                for key, value in document["apps"].items()
            }
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise McpAppRegistryUnreadable("mcp_app_registry_unreadable") from None
        return records

    def _write(self, records: dict[str, InstalledMcpAppRecord]) -> None:
        if len(records) > self._max_records:
            raise McpAppRegistryError("mcp_app_registry_limit_exceeded")
        payload = canonical_bytes(_document_for(records))
        if len(payload) > self._max_document_bytes:
            raise McpAppRegistryError("mcp_app_registry_limit_exceeded")
        self._store.write_bytes(payload)

    @property
    def max_records(self) -> int:
        return self._max_records

    @property
    def max_document_bytes(self) -> int:
        return self._max_document_bytes


def _document_for(records: dict[str, InstalledMcpAppRecord]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "apps": {key: records[key].to_dict() for key in sorted(records)},
    }


def _revision_for(records: dict[str, InstalledMcpAppRecord]) -> str:
    return hashlib.sha256(canonical_bytes(_document_for(records))).hexdigest()


def _snapshot_for(
    records: dict[str, InstalledMcpAppRecord],
) -> McpAppRegistrySnapshot:
    return McpAppRegistrySnapshot(
        records=tuple(records[key] for key in sorted(records)),
        revision=_revision_for(records),
    )


def _expected_revision(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("invalid_app_registry_revision")
    return value


def _validated_batch(
    value: object,
) -> dict[str, InstalledMcpAppRecord]:
    try:
        if type(value) is not tuple:
            raise ValueError
        records = tuple(_validated_record(record) for record in value)
        by_id = {record.app_id: record for record in records}
        if len(by_id) != len(records):
            raise ValueError
        return {identity: by_id[identity] for identity in sorted(by_id)}
    except (AttributeError, TypeError, ValueError):
        raise ValueError("mcp_app_batch_invalid") from None


def _app_id(value: object) -> str:
    try:
        return parse_app_id(value)
    except (TypeError, ValueError):
        raise ValueError("mcp_app_identity_invalid") from None


def _exact_object(raw: Any, fields: frozenset[str]) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != fields or any(type(key) is not str for key in raw):
        raise ValueError("invalid exact object")
    return raw


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON field")
        document[key] = value
    return document
