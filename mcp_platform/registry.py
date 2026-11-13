"""Atomic immutable-package publication and content-free MCP registry state."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

from packaging.version import InvalidVersion, Version

from core import paths as core_paths
from mcp_platform.canonical import canonical_bytes
from mcp_platform.package_contracts import NetworkClass
from mcp_platform.package_verifier import VerifiedPackage
from mcp_platform.state_store import (
    McpStateStore,
    reject_reparse,
    reject_reparse_chain,
)

_REGISTRY_FIELDS = frozenset({"schema_version", "packages"})
_RECORD_FIELDS = frozenset(
    {
        "server_id",
        "server_version",
        "platform",
        "publisher_id",
        "signing_key_id",
        "manifest_digest",
        "artifact_sha256",
        "tool_catalog_sha256",
        "resource_catalog_sha256",
        "network_class",
        "enabled",
        "status",
        "blocker",
        "crash_count",
    }
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_BLOCKER_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_PACKAGE_FILES = ("manifest.json", "tool_catalog.json", "resource_catalog.json")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class McpRegistryError(RuntimeError):
    """A stable registry or immutable-install failure."""


class McpRegistryUnreadable(McpRegistryError):
    """The registry exists but cannot be parsed as its exact schema."""


class McpPackageNotInstalled(McpRegistryError):
    """A requested server identity has no installed registry record."""


class PackageIdentityCollision(McpRegistryError):
    """Different signed bytes claimed an already-published package identity."""


class McpRegistryConflict(McpRegistryError):
    """The caller's observed registry revision is no longer current."""


@dataclass(frozen=True)
class InstalledPackageRecord:
    """Content-free durable state for one installed package."""

    server_id: str
    server_version: str
    platform: str
    publisher_id: str
    signing_key_id: str
    manifest_digest: str
    artifact_sha256: str
    tool_catalog_sha256: str
    resource_catalog_sha256: str
    network_class: str
    enabled: bool
    status: str
    blocker: str | None
    crash_count: int

    @classmethod
    def from_dict(cls, raw: Any, *, registry_key: str) -> InstalledPackageRecord:
        data = _exact_object(raw, _RECORD_FIELDS)
        server_id = _identity(data["server_id"])
        if registry_key != server_id:
            raise ValueError("registry key mismatch")
        server_version = _identity(data["server_version"])
        platform = _identity(data["platform"])
        publisher_id = _identity(data["publisher_id"])
        signing_key_id = _identity(data["signing_key_id"])
        digests = {
            field: _digest(data[field])
            for field in (
                "manifest_digest",
                "artifact_sha256",
                "tool_catalog_sha256",
                "resource_catalog_sha256",
            )
        }
        try:
            network_class = NetworkClass(data["network_class"]).value
        except (TypeError, ValueError):
            raise ValueError("invalid network class") from None
        if type(data["enabled"]) is not bool:
            raise ValueError("invalid enabled state")
        if data["status"] != "installed":
            raise ValueError("invalid package status")
        blocker = data["blocker"]
        if blocker is not None:
            blocker = _blocker(blocker)
        crash_count = data["crash_count"]
        if type(crash_count) is not int or crash_count < 0:
            raise ValueError("invalid crash count")
        return cls(
            server_id=server_id,
            server_version=server_version,
            platform=platform,
            publisher_id=publisher_id,
            signing_key_id=signing_key_id,
            manifest_digest=digests["manifest_digest"],
            artifact_sha256=digests["artifact_sha256"],
            tool_catalog_sha256=digests["tool_catalog_sha256"],
            resource_catalog_sha256=digests["resource_catalog_sha256"],
            network_class=network_class,
            enabled=data["enabled"],
            status="installed",
            blocker=blocker,
            crash_count=crash_count,
        )

    @classmethod
    def from_verified(cls, package: VerifiedPackage) -> InstalledPackageRecord:
        manifest = package.manifest
        return cls(
            server_id=_identity(manifest.server_id),
            server_version=_identity(manifest.server_version),
            platform=_identity(manifest.platform),
            publisher_id=_identity(manifest.publisher_id),
            signing_key_id=_identity(manifest.signing_key_id),
            manifest_digest=_digest(package.manifest_sha256),
            artifact_sha256=_digest(manifest.artifact_sha256),
            tool_catalog_sha256=_digest(manifest.tool_catalog_sha256),
            resource_catalog_sha256=_digest(manifest.resource_catalog_sha256),
            network_class=manifest.network_class.value,
            enabled=False,
            status="installed",
            blocker=None,
            crash_count=0,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "server_id": self.server_id,
            "server_version": self.server_version,
            "platform": self.platform,
            "publisher_id": self.publisher_id,
            "signing_key_id": self.signing_key_id,
            "manifest_digest": self.manifest_digest,
            "artifact_sha256": self.artifact_sha256,
            "tool_catalog_sha256": self.tool_catalog_sha256,
            "resource_catalog_sha256": self.resource_catalog_sha256,
            "network_class": self.network_class,
            "enabled": self.enabled,
            "status": self.status,
            "blocker": self.blocker,
            "crash_count": self.crash_count,
        }


@dataclass(frozen=True)
class PackageRegistrySnapshot:
    """Immutable content-free registry records and their canonical revision."""

    records: tuple[InstalledPackageRecord, ...]
    revision: str


@dataclass(frozen=True)
class PackageEnableResult:
    """Immutable result of one revision-safe package enable mutation."""

    record: InstalledPackageRecord
    revision: str


Verifier = Callable[[Path], VerifiedPackage]
_DEFAULT_MAX_REGISTRY_DOCUMENT_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_PACKAGE_RECORDS = 4_096
_MAX_PACKAGE_ID_HINT_BYTES = 1_048_576


def _bounded_package_server_id_hint(package_root: Path) -> str | None:
    """Read only enough untrusted manifest data to preflight a full registry."""
    manifest_path = package_root / "manifest.json"
    try:
        _reject_reparse(manifest_path)
        with manifest_path.open("rb") as source:
            if os.fstat(source.fileno()).st_size > _MAX_PACKAGE_ID_HINT_BYTES:
                return None
            payload = source.read(_MAX_PACKAGE_ID_HINT_BYTES + 1)
        if len(payload) > _MAX_PACKAGE_ID_HINT_BYTES:
            return None
        document = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if type(document) is not dict:
        return None
    server_id = document.get("server_id")
    if type(server_id) is not str or _IDENTITY_RE.fullmatch(server_id) is None:
        return None
    return server_id


class PackageRegistry:
    """Publish verified package trees and atomically mutate their safe state."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        verifier: Verifier | None = None,
        max_records: int = _DEFAULT_MAX_PACKAGE_RECORDS,
        max_document_bytes: int = _DEFAULT_MAX_REGISTRY_DOCUMENT_BYTES,
    ) -> None:
        if type(max_records) is not int or max_records <= 0:
            raise ValueError("max_records must be a positive integer")
        if type(max_document_bytes) is not int or max_document_bytes <= 0:
            raise ValueError("max_document_bytes must be a positive integer")
        self.state_root = Path(state_root) if state_root is not None else core_paths.MCP_STATE_ROOT
        self.packages_root = self.state_root / "packages"
        self.registry_path = self.state_root / "registry.json"
        self._verifier = verifier
        self._max_records = max_records
        self._max_document_bytes = max_document_bytes
        self._store = McpStateStore(
            state_root=self.state_root,
            document_path=self.registry_path,
            additional_roots=((self.packages_root, True),),
            invalid_path_reason="package_file_escape",
            max_document_bytes=max_document_bytes,
        )

    def load(self) -> dict[str, InstalledPackageRecord]:
        """Load exact registry state; missing is empty and corruption is never empty."""
        self._validate_durable_roots()
        with self._store.locked():
            self._validate_durable_roots()
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, InstalledPackageRecord]:
        """Parse registry state while the caller holds the sidecar lock."""
        payload = self._store.read_bytes()
        if payload is None:
            return {}
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
            document = _exact_object(raw, _REGISTRY_FIELDS)
            if (
                type(document["schema_version"]) is not int
                or document["schema_version"] != 1
                or type(document["packages"]) is not dict
                or len(document["packages"]) > self._max_records
            ):
                raise ValueError("invalid registry root")
            records = {
                _identity(key): InstalledPackageRecord.from_dict(value, registry_key=key)
                for key, value in document["packages"].items()
            }
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise McpRegistryUnreadable("mcp_registry_unreadable") from None
        return records

    def list_records(self) -> tuple[InstalledPackageRecord, ...]:
        return self.snapshot().records

    def snapshot(self) -> PackageRegistrySnapshot:
        """Return one immutable snapshot and RFC 8785 registry revision."""
        self._validate_durable_roots()
        with self._store.locked():
            self._validate_durable_roots()
            records = self._load_unlocked()
            return _snapshot_for(records)

    def get(self, server_id: str) -> InstalledPackageRecord:
        identity = _identity(server_id)
        try:
            return self.load()[identity]
        except KeyError:
            raise McpPackageNotInstalled("package_not_installed") from None

    def reverify_installed(self, server_id: str) -> VerifiedPackage:
        """Reverify and return the exact bytes bound to one installed registry record."""
        if self._verifier is None:
            raise McpRegistryError("package_verifier_required")
        identity = _identity(server_id)
        self._validate_durable_roots()
        with self._store.locked():
            self._validate_durable_roots()
            records = self._load_unlocked()
            try:
                record = records[identity]
            except KeyError:
                raise McpPackageNotInstalled("package_not_installed") from None
            target = self.package_path(record)
            try:
                _reject_reparse(target)
                verified = self._verifier(target)
                _declared_source_files(target, verified)
                observed = InstalledPackageRecord.from_verified(verified)
            except Exception:
                raise PackageIdentityCollision("package_identity_collision") from None
            if not _same_identity(observed, record) or not _same_signed_bytes(observed, record):
                raise PackageIdentityCollision("package_identity_collision")
            return verified

    def package_path(self, record: InstalledPackageRecord) -> Path:
        """Return the immutable directory for a validated installed record."""
        return (
            self.packages_root
            / _identity(record.server_id)
            / _identity(record.server_version)
            / _identity(record.platform)
        )

    def install(self, package_dir: Path) -> InstalledPackageRecord:
        """Verify, stage, reverify, and atomically publish one package identity."""
        if self._verifier is None:
            raise McpRegistryError("package_verifier_required")
        observed_records = self.load()
        source_input = Path(package_dir)
        _reject_reparse(source_input)
        source_root = source_input.resolve(strict=True)
        if not source_root.is_dir():
            raise ValueError("invalid_package_identity")
        if (
            len(observed_records) >= self._max_records
            and _bounded_package_server_id_hint(source_root) not in observed_records
        ):
            raise McpRegistryError("package_registry_limit_exceeded")
        verified = self._verifier(source_root)
        candidate = InstalledPackageRecord.from_verified(verified)
        source_files = _declared_source_files(source_root, verified)
        self._validate_durable_roots()
        with self._store.locked():
            self._validate_durable_roots()
            records = self._load_unlocked()
            current = records.get(candidate.server_id)
            try:
                candidate_version = Version(candidate.server_version)
                current_version = Version(current.server_version) if current is not None else None
            except InvalidVersion:
                raise McpRegistryError("package_version_invalid") from None
            if current_version is not None and candidate_version < current_version:
                raise McpRegistryError("package_downgrade_rejected")
            if current is None and len(records) >= self._max_records:
                raise McpRegistryError("package_registry_limit_exceeded")
            projected = dict(records)
            projected[candidate.server_id] = candidate
            registry_payload = self._payload_for_write(projected)
            self._prepare_packages_root()
            target = self.package_path(candidate)
            _ensure_contained(self.packages_root, target)

            if current is not None and _same_identity(current, candidate):
                if not _same_signed_bytes(current, candidate):
                    raise PackageIdentityCollision("package_identity_collision")
                if target.exists():
                    installed = self._verified_existing(target, candidate)
                    if not _same_signed_bytes(installed, candidate):
                        raise PackageIdentityCollision("package_identity_collision")
                    return current

            staging = self.packages_root / f".installing-{uuid4().hex}"
            staging.mkdir(exist_ok=False)
            try:
                for relative_path, source in source_files.items():
                    destination = staging / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    _fsync_file(destination)
                staged = self._verifier(staging)
                staged_record = InstalledPackageRecord.from_verified(staged)
                _declared_source_files(staging, staged)
                if candidate != staged_record:
                    raise PackageIdentityCollision("package_identity_collision")

                target.parent.mkdir(parents=True, exist_ok=True)
                _ensure_contained(self.packages_root, target)
                if target.exists():
                    installed = self._verified_existing(target, candidate)
                    if not _same_signed_bytes(installed, candidate):
                        raise PackageIdentityCollision("package_identity_collision")
                else:
                    os.replace(staging, target)
                    staging = None
                    _fsync_directory(target.parent)

                records[candidate.server_id] = candidate
                self._store.write_bytes(registry_payload)
                return candidate
            finally:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)

    def set_enabled(
        self,
        server_id: str,
        enabled: bool,
        *,
        expected_revision: str,
    ) -> PackageEnableResult:
        if type(enabled) is not bool:
            raise ValueError("invalid_enabled_state")
        if type(expected_revision) is not str or not _DIGEST_RE.fullmatch(expected_revision):
            raise ValueError("invalid_registry_revision")
        identity = _identity(server_id)
        self._validate_durable_roots()
        with self._store.locked():
            self._validate_durable_roots()
            records = self._load_unlocked()
            current_snapshot = _snapshot_for(records)
            if expected_revision != current_snapshot.revision:
                raise McpRegistryConflict("mcp_registry_conflict")
            try:
                current = records[identity]
            except KeyError:
                raise McpPackageNotInstalled("package_not_installed") from None
            updated = replace(current, enabled=enabled)
            records[identity] = updated
            self._write(records)
            return PackageEnableResult(
                record=updated,
                revision=_revision_for(records),
            )

    def block(
        self,
        server_id: str,
        blocker: str,
        *,
        increment_crash: bool = False,
    ) -> InstalledPackageRecord:
        reason = _blocker(blocker)
        if type(increment_crash) is not bool:
            raise ValueError("invalid_crash_state")

        def _blocked(record: InstalledPackageRecord) -> InstalledPackageRecord:
            count = record.crash_count + 1 if increment_crash else record.crash_count
            return replace(record, blocker=reason, crash_count=count)

        return self._mutate(server_id, _blocked)

    def clear_block(self, server_id: str) -> InstalledPackageRecord:
        return self._mutate(
            server_id,
            lambda record: replace(record, blocker=None, crash_count=0),
        )

    def record_crash(
        self,
        server_id: str,
        *,
        permanent_block_at: int,
        permanent_blocker: str,
    ) -> InstalledPackageRecord:
        """Atomically persist one crash and apply a threshold blocker."""
        if type(permanent_block_at) is not int or permanent_block_at < 1:
            raise ValueError("invalid_crash_threshold")
        reason = _blocker(permanent_blocker)

        def _crashed(record: InstalledPackageRecord) -> InstalledPackageRecord:
            crash_count = record.crash_count + 1
            blocker = reason if crash_count >= permanent_block_at else record.blocker
            return replace(record, blocker=blocker, crash_count=crash_count)

        return self._mutate(
            server_id,
            _crashed,
        )

    def _mutate(
        self,
        server_id: str,
        mutation: Callable[[InstalledPackageRecord], InstalledPackageRecord],
    ) -> InstalledPackageRecord:
        identity = _identity(server_id)
        self._validate_durable_roots()
        with self._store.locked():
            self._validate_durable_roots()
            records = self._load_unlocked()
            try:
                current = records[identity]
            except KeyError:
                raise McpPackageNotInstalled("package_not_installed") from None
            updated = mutation(current)
            records[identity] = updated
            self._write(records)
            return updated

    def _verified_existing(
        self,
        target: Path,
        candidate: InstalledPackageRecord,
    ) -> InstalledPackageRecord:
        try:
            _reject_reparse(target)
            verified = self._verifier(target)
            _declared_source_files(target, verified)
            installed = InstalledPackageRecord.from_verified(verified)
        except Exception as exc:
            if isinstance(exc, PackageIdentityCollision):
                raise
            raise PackageIdentityCollision("package_identity_collision") from None
        if not _same_identity(installed, candidate):
            raise PackageIdentityCollision("package_identity_collision")
        return installed

    def _prepare_packages_root(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        _reject_reparse(self.state_root)
        self.packages_root.mkdir(parents=True, exist_ok=True)
        _reject_reparse(self.packages_root)
        _ensure_contained(self.state_root, self.packages_root)

    def _validate_durable_roots(self) -> None:
        self._store.validate()

    def _write(self, records: dict[str, InstalledPackageRecord]) -> None:
        self._store.write_bytes(self._payload_for_write(records))

    def _payload_for_write(self, records: dict[str, InstalledPackageRecord]) -> bytes:
        if len(records) > self._max_records:
            raise McpRegistryError("package_registry_limit_exceeded")
        payload = canonical_bytes(_document_for(records))
        if len(payload) > self._max_document_bytes:
            raise McpRegistryError("package_registry_limit_exceeded")
        return payload

    @property
    def max_records(self) -> int:
        return self._max_records

    @property
    def max_document_bytes(self) -> int:
        return self._max_document_bytes


def _document_for(
    records: dict[str, InstalledPackageRecord],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "packages": {key: records[key].to_dict() for key in sorted(records)},
    }


def _revision_for(records: dict[str, InstalledPackageRecord]) -> str:
    return hashlib.sha256(canonical_bytes(_document_for(records))).hexdigest()


def _snapshot_for(
    records: dict[str, InstalledPackageRecord],
) -> PackageRegistrySnapshot:
    return PackageRegistrySnapshot(
        records=tuple(records[key] for key in sorted(records)),
        revision=_revision_for(records),
    )


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


def _identity(value: Any) -> str:
    if (
        type(value) is not str
        or not _IDENTITY_RE.fullmatch(value)
        or value.endswith(".")
        or value.partition(".")[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("invalid_package_identity")
    path = PureWindowsPath(value)
    if path.is_absolute() or path.drive or path.root or value in {".", ".."}:
        raise ValueError("invalid_package_identity")
    return value


def _digest(value: Any) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        raise ValueError("invalid digest")
    return value


def _blocker(value: Any) -> str:
    if type(value) is not str or not _BLOCKER_RE.fullmatch(value):
        raise ValueError("invalid_blocker_state")
    return value


def _declared_source_files(
    source_root: Path,
    verified: VerifiedPackage,
) -> dict[Path, Path]:
    artifact_relative = Path(verified.manifest.artifact)
    declared = {
        Path("manifest.json"): source_root / "manifest.json",
        Path("tool_catalog.json"): verified.tool_catalog_path,
        Path("resource_catalog.json"): verified.resource_catalog_path,
        artifact_relative: verified.artifact_path,
    }
    if len(declared) != len(_PACKAGE_FILES) + 1:
        raise ValueError("invalid_package_identity")
    for relative, candidate in declared.items():
        windows_path = PureWindowsPath(str(relative))
        if windows_path.is_absolute() or windows_path.drive or windows_path.root:
            raise ValueError("package_file_escape")
        resolved = Path(candidate).resolve(strict=True)
        try:
            resolved.relative_to(source_root)
        except ValueError:
            raise ValueError("package_file_escape") from None
        _reject_reparse_chain(source_root, Path(candidate))
        if not resolved.is_file():
            raise ValueError("package_file_escape")
        declared[relative] = resolved
    _validate_package_tree(source_root, frozenset(declared))
    return declared


def _validate_package_tree(root: Path, expected_files: frozenset[Path]) -> None:
    actual_files: set[Path] = set()

    def _walk(directory: Path, relative_directory: Path) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise ValueError("package_file_escape") from None
        for entry in entries:
            path = Path(entry.path)
            _reject_reparse(path)
            relative = relative_directory / entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    _walk(path, relative)
                elif entry.is_file(follow_symlinks=False):
                    actual_files.add(relative)
                else:
                    raise ValueError("package_file_escape")
            except OSError:
                raise ValueError("package_file_escape") from None

    _reject_reparse(root)
    _walk(root, Path())
    if actual_files != set(expected_files):
        raise ValueError("package_file_set_mismatch")


def _same_identity(left: InstalledPackageRecord, right: InstalledPackageRecord) -> bool:
    return (
        left.server_id,
        left.server_version,
        left.platform,
    ) == (
        right.server_id,
        right.server_version,
        right.platform,
    )


def _same_signed_bytes(left: InstalledPackageRecord, right: InstalledPackageRecord) -> bool:
    return (
        left.publisher_id,
        left.signing_key_id,
        left.manifest_digest,
        left.artifact_sha256,
        left.tool_catalog_sha256,
        left.resource_catalog_sha256,
        left.network_class,
    ) == (
        right.publisher_id,
        right.signing_key_id,
        right.manifest_digest,
        right.artifact_sha256,
        right.tool_catalog_sha256,
        right.resource_catalog_sha256,
        right.network_class,
    )


def _reject_reparse(path: Path) -> None:
    reject_reparse(path, reason="package_file_escape")


def _reject_reparse_chain(root: Path, candidate: Path) -> None:
    reject_reparse_chain(root, candidate, reason="package_file_escape")


def _ensure_contained(root: Path, candidate: Path) -> None:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        raise ValueError("invalid_package_identity") from None


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
