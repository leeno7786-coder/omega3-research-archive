"""Immutable plugin staging, publication, and content-free registry state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, overload
from uuid import uuid4

from core import paths as core_paths
from mcp_platform.canonical import canonical_bytes
from plugin_platform.filesystem import (
    PinnedDirectory,
    ensure_contained,
    fsync_directory,
    reject_reparse,
)
from plugin_platform.package_verifier import VerifiedPlugin
from plugin_platform.records import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginAppBinding,
    PluginRegistryError,
    PluginRegistrySnapshot,
    _blocker,
    _digest,
    _exact_object,
    _identity,
    _parse_semver,
    _semver,
)

_REGISTRY_FIELDS = frozenset({"schema_version", "plugins"})
_STAGE_RE = re.compile(r"\.installing-[0-9a-f]{32}\Z")
_STAGE_NAME_UNSET = object()
_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_REGISTRY_RECORDS = 1024


class _RegistryRecoveryRequired(RuntimeError):
    pass


def _version_record_from_verified(
    verified: VerifiedPlugin,
    *,
    state: str,
) -> InstalledPluginVersionRecord:
    if state not in {"selected", "candidate"}:
        raise ValueError("invalid version state")
    return InstalledPluginVersionRecord(
        plugin_version=_semver(verified.manifest.plugin_version),
        signing_key_id=_identity(verified.manifest.signing_key_id),
        manifest_sha256=_digest(verified.manifest_sha256),
        package_sha256=_digest(verified.package_sha256),
        state=state,
    )


@dataclass(frozen=True)
class StagedPluginInstall:
    """One verified unpublished stage and its immutable candidate identity."""

    stage_path: Path
    candidate: InstalledPluginVersionRecord
    verified: VerifiedPlugin


@dataclass(frozen=True)
class PluginMutationResult:
    """One revision-safe mutation result."""

    record: InstalledPluginRecord
    revision: str


@dataclass(frozen=True)
class VerifiedInstalledPluginSnapshot:
    """One coherent lock-held installed record, package, and registry revision."""

    record: InstalledPluginRecord
    verified: VerifiedPlugin
    revision: str

    def __post_init__(self) -> None:
        try:
            if type(self.record) is not InstalledPluginRecord:
                raise ValueError
            record = InstalledPluginRecord.from_dict(
                self.record.to_dict(),
                registry_key=self.record.plugin_id,
            )
            if type(self.verified) is not VerifiedPlugin:
                raise ValueError
            revision = _digest(self.revision)
            version = _version_for(record, record.selected_version)
            manifest = self.verified.manifest
            if (
                version is None
                or manifest.plugin_id != record.plugin_id
                or manifest.publisher_id != record.publisher_id
                or manifest.plugin_version != record.selected_version
                or manifest.kind.value != record.kind
                or self.verified.manifest_sha256 != version.manifest_sha256
                or self.verified.package_sha256 != version.package_sha256
            ):
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ValueError("invalid verified installed plugin snapshot") from None
        object.__setattr__(self, "record", record)
        object.__setattr__(self, "revision", revision)


@dataclass(frozen=True)
class PluginPublicationObservation:
    """Exact lock-held observation of one expected immutable publication."""

    published: bool
    record: InstalledPluginRecord | None
    revision: str

    def __post_init__(self) -> None:
        try:
            if type(self.published) is not bool:
                raise ValueError("invalid publication observation")
            revision = _digest(self.revision)
            if (self.record is None) is self.published:
                raise ValueError("invalid publication observation")
            record = self.record
            if record is not None:
                if type(record) is not InstalledPluginRecord:
                    raise ValueError("invalid publication observation")
                record = InstalledPluginRecord.from_dict(
                    record.to_dict(),
                    registry_key=record.plugin_id,
                )
        except Exception:
            raise ValueError("invalid publication observation") from None
        object.__setattr__(self, "record", record)
        object.__setattr__(self, "revision", revision)


Verifier = Callable[[Path], VerifiedPlugin]


@dataclass
class _RegistryStorage:
    state: PinnedDirectory
    packages: PinnedDirectory


@dataclass
class _FinalMaterializationState:
    cleanup_required: bool = False
    cleanup_identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class _PublicationIdentity:
    plugin_id: str
    publisher_id: str
    plugin_version: str
    signing_key_id: str
    manifest_sha256: str
    package_sha256: str


def _publication_identity(
    *,
    plugin_id: str,
    publisher_id: str,
    plugin_version: str,
    signing_key_id: str,
    manifest_sha256: str,
    package_sha256: str,
) -> _PublicationIdentity:
    try:
        return _PublicationIdentity(
            plugin_id=_identity(plugin_id),
            publisher_id=_identity(publisher_id),
            plugin_version=_semver(plugin_version),
            signing_key_id=_identity(signing_key_id),
            manifest_sha256=_digest(manifest_sha256),
            package_sha256=_digest(package_sha256),
        )
    except ValueError:
        raise PluginRegistryError("plugin_registry_reconciliation_failed") from None


_MUTATION_LOCKS_GUARD = threading.Lock()
_MUTATION_LOCKS: dict[str, threading.RLock] = {}


def _mutation_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(os.fspath(path))
    with _MUTATION_LOCKS_GUARD:
        return _MUTATION_LOCKS.setdefault(key, threading.RLock())


class PluginRegistry:
    """Stage authenticated payloads and publish immutable plugin versions."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        verifier: Verifier | None = None,
    ) -> None:
        configured_root = core_paths.PLUGIN_STATE_ROOT if state_root is None else Path(state_root)
        self.state_root = _absolute(configured_root)
        self.packages_root = self.state_root / "packages"
        self.registry_path = self.state_root / "registry.json"
        self._verifier = verifier

    def snapshot(self) -> PluginRegistrySnapshot:
        """Read the exact canonical registry while holding its sidecar lock."""
        self._prepare_roots()
        with self._locked() as storage:
            records, _payload = self._load_unlocked(storage)
            return _snapshot_for(records)

    def get(self, plugin_id: str) -> InstalledPluginRecord:
        identity = _identity(plugin_id)
        snapshot = self.snapshot()
        for record in snapshot.records:
            if record.plugin_id == identity:
                return record
        raise PluginRegistryError("plugin_not_installed")

    @overload
    def stage(self, verified: VerifiedPlugin) -> StagedPluginInstall: ...

    @overload
    def stage(
        self,
        verified: VerifiedPlugin,
        *,
        stage_name: str,
    ) -> StagedPluginInstall: ...

    def stage(
        self,
        verified: VerifiedPlugin,
        *,
        stage_name: object = _STAGE_NAME_UNSET,
    ) -> StagedPluginInstall:
        """Write only authenticated immutable inputs and reverify the completed stage."""
        if stage_name is _STAGE_NAME_UNSET:
            canonical_stage_name = f".installing-{uuid4().hex}"
        elif type(stage_name) is str and _STAGE_RE.fullmatch(stage_name) is not None:
            canonical_stage_name = stage_name
        else:
            raise PluginRegistryError("plugin_stage_invalid")
        if self._verifier is None:
            raise PluginRegistryError("plugin_verifier_required")
        manifest_payload = _validated_verified_payloads(verified)
        candidate = _version_record_from_verified(verified, state="candidate")
        self._prepare_roots()
        stage_path = self.packages_root / canonical_stage_name
        created = False
        stage_identity: tuple[int, int] | None = None
        try:
            with self._locked() as storage:
                try:
                    child_names = os.listdir(storage.packages.path)
                    storage.packages.validate_binding()
                except (OSError, RuntimeError, TypeError, ValueError):
                    raise PluginRegistryError("plugin_stage_invalid") from None
                if any(
                    child_name.casefold() == canonical_stage_name.casefold()
                    for child_name in child_names
                ):
                    raise PluginRegistryError("plugin_stage_invalid")
                try:
                    stage = storage.packages.create_directory(canonical_stage_name)
                    stage_identity = stage.identity
                    created = True
                except (OSError, RuntimeError, TypeError, ValueError):
                    raise PluginRegistryError("plugin_stage_invalid") from None
                try:
                    with stage:
                        stage.write_bytes("plugin.json", manifest_payload)
                        for logical_path in sorted(verified.asset_bytes):
                            stage.write_bytes(
                                verified.asset_paths[logical_path],
                                verified.asset_bytes[logical_path],
                            )
                        stage.fsync()
                        storage.packages.fsync()
                    staged_verified = self._verify_stage_path(stage_path)
                    if not _same_verified_identity(verified, staged_verified):
                        raise PluginRegistryError("plugin_stage_invalid")
                except PluginRegistryError:
                    raise
                except (OSError, RuntimeError, TypeError, ValueError):
                    raise PluginRegistryError("plugin_stage_write_failed") from None
            return StagedPluginInstall(
                stage_path=stage_path,
                candidate=candidate,
                verified=staged_verified,
            )
        except PluginRegistryError:
            if created:
                try:
                    _remove_partial_stage(stage_path, stage_identity)
                except (OSError, RuntimeError, ValueError):
                    raise PluginRegistryError("plugin_registry_recovery_required") from None
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            if created:
                try:
                    _remove_partial_stage(stage_path, stage_identity)
                except (OSError, RuntimeError, ValueError):
                    raise PluginRegistryError("plugin_registry_recovery_required") from None
            raise PluginRegistryError("plugin_stage_write_failed") from None

    def publish(
        self,
        staged: StagedPluginInstall,
        expected_revision: str,
        *,
        app_binding: PluginAppBinding,
        preserve_existing_binding: bool = False,
    ) -> PluginMutationResult:
        """Materialize authenticated bytes under the full registry mutation lock."""
        revision = _digest_revision(expected_revision)
        if type(app_binding) is not PluginAppBinding:
            raise PluginRegistryError("plugin_app_binding_invalid")
        try:
            app_binding = PluginAppBinding.from_dict(app_binding.to_dict())
        except ValueError:
            raise PluginRegistryError("plugin_app_binding_invalid") from None
        if type(preserve_existing_binding) is not bool:
            raise PluginRegistryError("plugin_app_binding_invalid")
        self._prepare_roots()
        with self._locked() as storage:
            records, previous_payload = self._load_unlocked(storage)
            current_snapshot = _snapshot_for(records)
            if revision != current_snapshot.revision:
                raise PluginRegistryError("plugin_registry_conflict")
            staged_verified = self._verified_staged(staged)
            manifest = staged_verified.manifest
            _validate_binding_for_manifest(app_binding, manifest)
            plugin_id = _identity(manifest.plugin_id)
            publisher_id = _identity(manifest.publisher_id)
            candidate = _version_record_from_verified(
                staged_verified, state="candidate"
            )
            if candidate != staged.candidate:
                raise PluginRegistryError("plugin_stage_invalid")

            alias = _casefold_record(records, plugin_id)
            if alias is not None and alias.plugin_id != plugin_id:
                raise PluginRegistryError("plugin_identity_collision")
            current = records.get(plugin_id)
            target = self.packages_root / plugin_id / candidate.plugin_version
            _contained_direct_child(self.packages_root, target.parent)
            existing = _version_for(current, candidate.plugin_version)
            if existing is not None:
                if not _same_version_bytes(existing, candidate):
                    raise PluginRegistryError("plugin_identity_collision")
                self._verify_published_target(target, candidate)
                if current is None:
                    raise PluginRegistryError("plugin_registry_reconciliation_failed")
                effective_binding = (
                    current.app_binding if preserve_existing_binding else app_binding
                )
                if effective_binding is None:
                    raise PluginRegistryError("plugin_app_binding_invalid")
                if current.app_binding != effective_binding:
                    updated = replace(current, app_binding=effective_binding)
                    next_records = dict(records)
                    next_records[plugin_id] = updated
                    try:
                        _write_registry(
                            storage.state,
                            canonical_bytes(_document_for(next_records)),
                            previous_payload,
                        )
                    except PluginRegistryError:
                        raise
                    except (OSError, RuntimeError, TypeError, ValueError):
                        raise PluginRegistryError("plugin_registry_write_failed") from None
                    self._discard_if_unchanged(staged, storage.packages)
                    return PluginMutationResult(
                        record=updated,
                        revision=_revision_for(next_records),
                    )
                self._discard_if_unchanged(staged, storage.packages)
                return PluginMutationResult(
                    record=current,
                    revision=current_snapshot.revision,
                )

            if current is not None:
                if current.publisher_id != publisher_id or current.kind != manifest.kind.value:
                    raise PluginRegistryError("plugin_identity_collision")
                if preserve_existing_binding and current.app_binding is None:
                    raise PluginRegistryError("plugin_app_binding_invalid")
                candidate_semver = _parse_semver(candidate.plugin_version)
                if any(
                    _parse_semver(item.plugin_version) == candidate_semver
                    for item in current.versions
                ):
                    raise PluginRegistryError("plugin_identity_collision")
                highest = max(_parse_semver(item.plugin_version) for item in current.versions)
                if candidate_semver < highest:
                    raise PluginRegistryError("plugin_downgrade_rejected")
                updated = replace(
                    current,
                    versions=(*current.versions, candidate),
                    app_binding=(
                        current.app_binding
                        if preserve_existing_binding
                        else app_binding
                    ),
                )
            else:
                if preserve_existing_binding:
                    raise PluginRegistryError("plugin_candidate_not_promotable")
                selected = replace(candidate, state="selected")
                updated = InstalledPluginRecord(
                    plugin_id=plugin_id,
                    publisher_id=publisher_id,
                    kind=manifest.kind.value,
                    selected_version=selected.plugin_version,
                    enabled=False,
                    status="installed",
                    blocker=None,
                    versions=(selected,),
                    app_binding=app_binding,
                )

            if storage.packages.exists(f"{plugin_id}/{candidate.plugin_version}"):
                raise PluginRegistryError("plugin_identity_collision")
            materialization = _FinalMaterializationState()
            try:
                _materialize_final(
                    storage.packages,
                    plugin_id,
                    staged_verified,
                    materialization,
                )
                self._verify_published_target(target, candidate)
                next_records = dict(records)
                next_records[plugin_id] = updated
                _write_registry(
                    storage.state,
                    canonical_bytes(_document_for(next_records)),
                    previous_payload,
                )
            except _RegistryRecoveryRequired:
                if materialization.cleanup_required:
                    try:
                        _remove_final(target, materialization.cleanup_identity)
                    except (OSError, RuntimeError, ValueError):
                        pass
                raise PluginRegistryError("plugin_registry_recovery_required") from None
            except PluginRegistryError:
                if materialization.cleanup_required:
                    try:
                        _remove_final(target, materialization.cleanup_identity)
                    except (OSError, RuntimeError, ValueError):
                        raise PluginRegistryError("plugin_registry_recovery_required") from None
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                if materialization.cleanup_required:
                    try:
                        _remove_final(target, materialization.cleanup_identity)
                    except (OSError, RuntimeError, ValueError):
                        raise PluginRegistryError("plugin_registry_recovery_required") from None
                raise PluginRegistryError("plugin_registry_write_failed") from None
            self._discard_if_unchanged(staged, storage.packages)
            return PluginMutationResult(
                record=updated,
                revision=_revision_for(next_records),
            )

    def preflight_publish(
        self,
        staged: StagedPluginInstall,
        expected_revision: str,
        *,
        app_binding: PluginAppBinding,
        preserve_existing_binding: bool = False,
    ) -> None:
        """Reject an unwriteable exact publication before dependency commit authority."""
        revision = _digest_revision(expected_revision)
        if type(app_binding) is not PluginAppBinding:
            raise PluginRegistryError("plugin_app_binding_invalid")
        try:
            binding = PluginAppBinding.from_dict(app_binding.to_dict())
        except ValueError:
            raise PluginRegistryError("plugin_app_binding_invalid") from None
        if type(preserve_existing_binding) is not bool:
            raise PluginRegistryError("plugin_app_binding_invalid")
        self._prepare_roots()
        with self._locked() as storage:
            records, _payload = self._load_unlocked(storage)
            if _revision_for(records) != revision:
                raise PluginRegistryError("plugin_registry_conflict")
            verified = self._verified_staged(staged)
            manifest = verified.manifest
            _validate_binding_for_manifest(binding, manifest)
            plugin_id = _identity(manifest.plugin_id)
            candidate = _version_record_from_verified(verified, state="candidate")
            current = records.get(plugin_id)
            next_records = dict(records)
            if current is None:
                if preserve_existing_binding:
                    raise PluginRegistryError("plugin_candidate_not_promotable")
                next_records[plugin_id] = InstalledPluginRecord(
                    plugin_id=plugin_id,
                    publisher_id=_identity(manifest.publisher_id),
                    kind=manifest.kind.value,
                    selected_version=candidate.plugin_version,
                    enabled=False,
                    status="installed",
                    blocker=None,
                    versions=(replace(candidate, state="selected"),),
                    app_binding=binding,
                )
            else:
                existing = _version_for(current, candidate.plugin_version)
                if preserve_existing_binding and current.app_binding is None:
                    raise PluginRegistryError("plugin_app_binding_invalid")
                if existing is not None:
                    if not _same_version_bytes(existing, candidate):
                        raise PluginRegistryError("plugin_identity_collision")
                    next_records[plugin_id] = replace(
                        current,
                        app_binding=(
                            current.app_binding
                            if preserve_existing_binding
                            else binding
                        ),
                    )
                else:
                    highest = max(_parse_semver(item.plugin_version) for item in current.versions)
                    if _parse_semver(candidate.plugin_version) < highest:
                        raise PluginRegistryError("plugin_downgrade_rejected")
                    next_records[plugin_id] = replace(
                        current,
                        versions=(*current.versions, candidate),
                        app_binding=(
                            current.app_binding
                            if preserve_existing_binding
                            else binding
                        ),
                    )
            _registry_payload_for(next_records)

    def repair_same_version(
        self,
        staged: StagedPluginInstall,
        expected_revision: str,
        *,
        expected_signing_key_id: str,
        expected_manifest_sha256: str,
        expected_package_sha256: str,
        app_binding: PluginAppBinding,
        installed_verifier: Verifier | None = None,
    ) -> PluginMutationResult:
        """Replace one exact, disabled signed release that reused its version.

        Ordinary publication remains immutable and rejects different bytes for
        the same semantic version.  This recovery seam exists only for a
        caller that names the exact installed key, manifest, and package digests,
        supplies a newly verified stage, and first disables the plugin.  A
        deterministic sibling backup makes interruption fail closed and lets
        a retry finish without accepting arbitrary filesystem bytes.
        """

        revision = _digest_revision(expected_revision)
        expected_signing_key = _identity(expected_signing_key_id)
        expected_manifest = _digest(expected_manifest_sha256)
        expected_package = _digest(expected_package_sha256)
        repair_verifier = self._verifier if installed_verifier is None else installed_verifier
        if not callable(repair_verifier):
            raise PluginRegistryError("plugin_verifier_required")
        if type(app_binding) is not PluginAppBinding:
            raise PluginRegistryError("plugin_app_binding_invalid")
        try:
            binding = PluginAppBinding.from_dict(app_binding.to_dict())
        except ValueError:
            raise PluginRegistryError("plugin_app_binding_invalid") from None
        self._prepare_roots()
        with self._locked() as storage:
            records, previous_payload = self._load_unlocked(storage)
            if _revision_for(records) != revision:
                raise PluginRegistryError("plugin_registry_conflict")
            verified = self._verified_staged(staged)
            manifest = verified.manifest
            _validate_binding_for_manifest(binding, manifest)
            plugin_id = _identity(manifest.plugin_id)
            candidate = _version_record_from_verified(
                verified,
                state="candidate",
            )
            if candidate != staged.candidate:
                raise PluginRegistryError("plugin_stage_invalid")
            current = records.get(plugin_id)
            if current is None:
                raise PluginRegistryError("plugin_not_installed")
            if current.publisher_id != manifest.publisher_id or current.kind != manifest.kind.value:
                raise PluginRegistryError("plugin_identity_collision")
            if current.enabled:
                raise PluginRegistryError("plugin_repair_requires_disabled")
            installed = _version_for(current, candidate.plugin_version)
            if installed is None or installed.state != "selected":
                raise PluginRegistryError("plugin_repair_identity_mismatch")

            target = self.packages_root / plugin_id / candidate.plugin_version
            backup_name = f".repair-{candidate.plugin_version}-{expected_package}"
            backup = target.parent / backup_name
            already_repaired = _same_version_bytes(installed, candidate)
            if not already_repaired and (
                installed.manifest_sha256 != expected_manifest
                or installed.package_sha256 != expected_package
                or installed.signing_key_id != expected_signing_key
            ):
                raise PluginRegistryError("plugin_repair_identity_mismatch")

            try:
                target_is_candidate = False
                target_is_installed = False
                if storage.packages.exists(f"{plugin_id}/{candidate.plugin_version}"):
                    try:
                        self._verify_published_target(target, candidate)
                        target_is_candidate = True
                    except PluginRegistryError:
                        if already_repaired:
                            raise
                        self._verify_repair_target(target, installed, repair_verifier)
                        target_is_installed = True

                with storage.packages.ensure_directory(plugin_id) as plugin_root:
                    backup_exists = plugin_root.exists(backup_name)
                    if backup_exists:
                        self._verify_repair_target(backup, installed, repair_verifier)

                    if already_repaired:
                        if not target_is_candidate:
                            raise PluginRegistryError("plugin_registry_reconciliation_failed")
                    else:
                        if target_is_installed:
                            if backup_exists:
                                raise PluginRegistryError("plugin_identity_collision")
                            plugin_root.rename_tree(candidate.plugin_version, backup_name)
                            backup_exists = True
                            target_is_installed = False
                        elif not target_is_candidate and not backup_exists:
                            raise PluginRegistryError("plugin_registry_reconciliation_failed")

                    if not target_is_candidate:
                        materialization = _FinalMaterializationState()
                        _materialize_final(
                            storage.packages,
                            plugin_id,
                            verified,
                            materialization,
                        )
                        self._verify_published_target(target, candidate)
                        target_is_candidate = True

                    if backup_exists:
                        with plugin_root.pin_tree(backup_name) as pinned:
                            pinned.verify(
                                lambda path: self._verify_repair_target(
                                    path,
                                    installed,
                                    repair_verifier,
                                )
                            )
                            pinned.remove()
                        plugin_root.fsync()

                replacement = replace(candidate, state=installed.state)
                updated = replace(
                    current,
                    versions=tuple(
                        replacement if item.plugin_version == candidate.plugin_version else item
                        for item in current.versions
                    ),
                    app_binding=binding,
                )
                next_records = dict(records)
                next_records[plugin_id] = updated
                if updated != current:
                    _write_registry(
                        storage.state,
                        canonical_bytes(_document_for(next_records)),
                        previous_payload,
                    )
                self._discard_if_unchanged(staged, storage.packages)
                return PluginMutationResult(
                    record=updated,
                    revision=_revision_for(next_records),
                )
            except PluginRegistryError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_registry_recovery_required") from None

    def discard(self, staged: StagedPluginInstall) -> None:
        """Delete only a validated unpublished direct child stage, idempotently."""
        self._prepare_roots()
        with self._locked() as storage:
            self._load_unlocked(storage)
            self._discard_validated(staged, storage.packages)

    def reverify_installed(self, plugin_id: str) -> VerifiedPlugin:
        """Return the selected installed signed package after full public re-verification."""
        return self.verified_installed_snapshot(plugin_id).verified

    def reverify_candidate(
        self,
        plugin_id: str,
        expected_candidate: InstalledPluginVersionRecord,
    ) -> VerifiedPlugin:
        """Reverify one exact locally published candidate without catalog resolution."""

        identity = _identity(plugin_id)
        try:
            expected = InstalledPluginVersionRecord.from_dict(
                expected_candidate.to_dict()
            )
        except (AttributeError, TypeError, ValueError):
            raise PluginRegistryError("plugin_candidate_not_promotable") from None
        if expected.state != "candidate":
            raise PluginRegistryError("plugin_candidate_not_promotable")
        self._prepare_roots()
        with self._locked() as storage:
            records, _payload = self._load_unlocked(storage)
            record = records.get(identity)
            if record is None:
                raise PluginRegistryError("plugin_not_installed")
            candidate = _version_for(record, expected.plugin_version)
            if candidate != expected or record.selected_version == expected.plugin_version:
                raise PluginRegistryError("plugin_candidate_not_promotable")
            target = self.packages_root / identity / expected.plugin_version
            try:
                verified = self._verify_published_target(target, expected)
            except PluginRegistryError:
                raise PluginRegistryError("plugin_registry_reconciliation_failed") from None
            if (
                verified.manifest.plugin_id != record.plugin_id
                or verified.manifest.publisher_id != record.publisher_id
            ):
                raise PluginRegistryError("plugin_registry_reconciliation_failed")
            return verified

    def verified_installed_snapshot(
        self,
        plugin_id: str,
    ) -> VerifiedInstalledPluginSnapshot:
        """Return a coherent installed record, reverified package, and revision."""
        identity = _identity(plugin_id)
        self._prepare_roots()
        with self._locked() as storage:
            records, _payload = self._load_unlocked(storage)
            record = records.get(identity)
            if record is None:
                raise PluginRegistryError("plugin_not_installed")
            version = _version_for(record, record.selected_version)
            if version is None:
                raise PluginRegistryError("plugin_registry_reconciliation_failed")
            target = self.packages_root / identity / record.selected_version
            try:
                verified = self._verify_published_target(target, version)
            except PluginRegistryError:
                raise PluginRegistryError("plugin_registry_reconciliation_failed") from None
            return VerifiedInstalledPluginSnapshot(
                record=record,
                verified=verified,
                revision=_revision_for(records),
            )

    def bind_apps(
        self,
        plugin_id: str,
        *,
        expected_revision: str,
        app_binding: PluginAppBinding,
    ) -> PluginMutationResult:
        """CAS-publish a reverified binding without changing installed package identity."""
        if type(app_binding) is not PluginAppBinding:
            raise PluginRegistryError("plugin_app_binding_invalid")
        try:
            binding = PluginAppBinding.from_dict(app_binding.to_dict())
        except ValueError:
            raise PluginRegistryError("plugin_app_binding_invalid") from None
        verified = self.reverify_installed(plugin_id)
        _validate_binding_for_manifest(binding, verified.manifest)
        return self._mutate(
            plugin_id,
            expected_revision,
            lambda record: (
                record if record.app_binding == binding else replace(record, app_binding=binding)
            ),
        )

    def preflight_bind_apps(
        self,
        plugin_id: str,
        *,
        expected_revision: str,
        app_binding: PluginAppBinding,
    ) -> None:
        """Reject an unwriteable legacy binding before dependency commit authority."""
        identity = _identity(plugin_id)
        revision = _digest_revision(expected_revision)
        if type(app_binding) is not PluginAppBinding:
            raise PluginRegistryError("plugin_app_binding_invalid")
        try:
            binding = PluginAppBinding.from_dict(app_binding.to_dict())
        except ValueError:
            raise PluginRegistryError("plugin_app_binding_invalid") from None
        verified = self.reverify_installed(identity)
        _validate_binding_for_manifest(binding, verified.manifest)
        self._prepare_roots()
        with self._locked() as storage:
            records, _payload = self._load_unlocked(storage)
            if _revision_for(records) != revision:
                raise PluginRegistryError("plugin_registry_conflict")
            record = records.get(identity)
            if record is None:
                raise PluginRegistryError("plugin_not_installed")
            next_records = dict(records)
            next_records[identity] = replace(record, app_binding=binding)
            _registry_payload_for(next_records)

    def observe_publication(
        self,
        *,
        plugin_id: str,
        publisher_id: str,
        plugin_version: str,
        signing_key_id: str,
        manifest_sha256: str,
        package_sha256: str,
    ) -> PluginPublicationObservation:
        """Reconcile one expected registry record with its reverified package tree."""
        expected = _publication_identity(
            plugin_id=plugin_id,
            publisher_id=publisher_id,
            plugin_version=plugin_version,
            signing_key_id=signing_key_id,
            manifest_sha256=manifest_sha256,
            package_sha256=package_sha256,
        )
        self._prepare_roots()
        with self._locked() as storage:
            records, _payload = self._load_unlocked(storage)
            snapshot = _snapshot_for(records)
            record = records.get(expected.plugin_id)
            if record is not None and record.publisher_id != expected.publisher_id:
                raise PluginRegistryError("plugin_registry_reconciliation_failed")
            version = _version_for(record, expected.plugin_version)
            relative_target = f"{expected.plugin_id}/{expected.plugin_version}"
            target_exists = storage.packages.exists(relative_target)
            if version is None and not target_exists:
                return PluginPublicationObservation(
                    published=False,
                    record=None,
                    revision=snapshot.revision,
                )
            if version is None or not target_exists:
                raise PluginRegistryError("plugin_registry_reconciliation_failed")
            if (
                version.signing_key_id != expected.signing_key_id
                or version.manifest_sha256 != expected.manifest_sha256
                or version.package_sha256 != expected.package_sha256
            ):
                raise PluginRegistryError("plugin_registry_reconciliation_failed")
            target = self.packages_root / expected.plugin_id / expected.plugin_version
            try:
                verified = self._verify_published_target(target, version)
            except PluginRegistryError:
                raise PluginRegistryError("plugin_registry_reconciliation_failed") from None
            if record.kind != verified.manifest.kind.value:
                raise PluginRegistryError("plugin_registry_reconciliation_failed")
            return PluginPublicationObservation(
                published=True,
                record=record,
                revision=snapshot.revision,
            )

    def recover_stage(
        self,
        stage_name: str,
        *,
        plugin_id: str,
        publisher_id: str,
        plugin_version: str,
        signing_key_id: str,
        manifest_sha256: str,
        package_sha256: str,
    ) -> StagedPluginInstall | None:
        """Recover one exact authenticated stage for ordinary idempotent discard."""
        expected = _publication_identity(
            plugin_id=plugin_id,
            publisher_id=publisher_id,
            plugin_version=plugin_version,
            signing_key_id=signing_key_id,
            manifest_sha256=manifest_sha256,
            package_sha256=package_sha256,
        )
        if type(stage_name) is not str or _STAGE_RE.fullmatch(stage_name) is None:
            raise PluginRegistryError("plugin_registry_reconciliation_failed")
        self._prepare_roots()
        with self._locked() as storage:
            self._load_unlocked(storage)
            if not storage.packages.exists(stage_name):
                return None
            stage_path = self.packages_root / stage_name
            try:
                verified = self._verify_stage_path(stage_path)
                candidate = _version_record_from_verified(
                    verified,
                    state="candidate",
                )
            except (PluginRegistryError, TypeError, ValueError):
                raise PluginRegistryError("plugin_registry_reconciliation_failed") from None
            if (
                verified.manifest.plugin_id != expected.plugin_id
                or verified.manifest.publisher_id != expected.publisher_id
                or candidate.plugin_version != expected.plugin_version
                or candidate.signing_key_id != expected.signing_key_id
                or candidate.manifest_sha256 != expected.manifest_sha256
                or candidate.package_sha256 != expected.package_sha256
            ):
                raise PluginRegistryError("plugin_registry_reconciliation_failed")
            return StagedPluginInstall(
                stage_path=stage_path,
                candidate=candidate,
                verified=verified,
            )

    def set_enabled(
        self,
        plugin_id: str,
        enabled: bool,
        *,
        expected_revision: str,
    ) -> PluginMutationResult:
        if type(enabled) is not bool:
            raise PluginRegistryError("invalid_enabled_state")

        def mutation(record: InstalledPluginRecord) -> InstalledPluginRecord:
            if enabled and record.blocker is not None:
                raise PluginRegistryError("plugin_enable_blocked")
            return replace(record, enabled=enabled)

        return self._mutate(plugin_id, expected_revision, mutation)

    def promote_candidate(
        self,
        plugin_id: str,
        plugin_version: str,
        *,
        expected_revision: str,
        app_binding: PluginAppBinding | None = None,
    ) -> PluginMutationResult:
        """Select one verified forward candidate with a revision-safe mutation."""

        identity = _identity(plugin_id)
        target_version = _semver(plugin_version)
        revision = _digest(expected_revision)
        binding = None
        if app_binding is not None:
            try:
                binding = PluginAppBinding.from_dict(app_binding.to_dict())
            except (AttributeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_app_binding_invalid") from None
        self._prepare_roots()
        with self._locked() as storage:
            records, previous_payload = self._load_unlocked(storage)
            current_snapshot = _snapshot_for(records)
            if revision != current_snapshot.revision:
                raise PluginRegistryError("plugin_registry_conflict")
            current = records.get(identity)
            if current is None:
                raise PluginRegistryError("plugin_not_installed")
            target = _version_for(current, target_version)
            if (
                target is None
                or target.state != "candidate"
                or _parse_semver(target_version) <= _parse_semver(current.selected_version)
            ):
                raise PluginRegistryError("plugin_candidate_not_promotable")
            package_path = self.packages_root / identity / target_version
            verified = self._verify_published_target(package_path, target)
            if binding is not None:
                _validate_binding_for_manifest(binding, verified.manifest)
            updated = replace(
                current,
                selected_version=target_version,
                app_binding=current.app_binding if binding is None else binding,
                versions=tuple(
                    replace(
                        item,
                        state="selected" if item.plugin_version == target_version else "candidate",
                    )
                    for item in current.versions
                ),
            )
            next_records = dict(records)
            next_records[identity] = updated
            try:
                _write_registry(
                    storage.state,
                    canonical_bytes(_document_for(next_records)),
                    previous_payload,
                )
            except _RegistryRecoveryRequired:
                raise PluginRegistryError("plugin_registry_recovery_required") from None
            except PluginRegistryError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_registry_write_failed") from None
            return PluginMutationResult(
                record=updated,
                revision=_revision_for(next_records),
            )

    def discard_candidate(
        self,
        plugin_id: str,
        plugin_version: str,
        *,
        expected_revision: str,
        expected_candidate: InstalledPluginVersionRecord | None = None,
    ) -> PluginMutationResult:
        """Remove one exact unpublished version without touching selected authority."""

        identity = _identity(plugin_id)
        target_version = _semver(plugin_version)
        revision = _digest(expected_revision)
        expected = None
        if expected_candidate is not None:
            try:
                expected = InstalledPluginVersionRecord.from_dict(
                    expected_candidate.to_dict()
                )
            except (AttributeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_candidate_not_promotable") from None
            if expected.plugin_version != target_version or expected.state != "candidate":
                raise PluginRegistryError("plugin_candidate_not_promotable")
        self._prepare_roots()
        with self._locked() as storage:
            records, previous_payload = self._load_unlocked(storage)
            current_snapshot = _snapshot_for(records)
            if revision != current_snapshot.revision:
                raise PluginRegistryError("plugin_registry_conflict")
            current = records.get(identity)
            if current is None:
                raise PluginRegistryError("plugin_not_installed")
            target = _version_for(current, target_version)
            if target is not None and (target.state != "candidate" or (
                expected is not None and target != expected
            )):
                raise PluginRegistryError("plugin_candidate_not_promotable")
            package_path = self.packages_root / identity / target_version
            target_exists = storage.packages.exists(f"{identity}/{target_version}")
            if target is None:
                if expected is None or current.selected_version == target_version:
                    raise PluginRegistryError("plugin_candidate_not_promotable")
                if not target_exists:
                    return PluginMutationResult(current, current_snapshot.revision)
                try:
                    with PinnedDirectory.open(package_path.parent) as plugin_root:
                        with plugin_root.pin_tree(target_version) as pinned:
                            pinned.remove()
                        plugin_root.fsync()
                except (OSError, RuntimeError, TypeError, ValueError):
                    raise PluginRegistryError("plugin_registry_recovery_required") from None
                return PluginMutationResult(current, current_snapshot.revision)
            if expected is None:
                self._verify_published_target(package_path, target)
            remaining = tuple(
                item for item in current.versions if item.plugin_version != target_version
            )
            if not remaining:
                raise PluginRegistryError("plugin_candidate_not_promotable")
            updated = replace(current, versions=remaining)
            next_records = dict(records)
            next_records[identity] = updated
            try:
                _write_registry(
                    storage.state,
                    canonical_bytes(_document_for(next_records)),
                    previous_payload,
                )
                if target_exists:
                    with PinnedDirectory.open(package_path.parent) as plugin_root:
                        with plugin_root.pin_tree(target_version) as pinned:
                            pinned.remove()
                        plugin_root.fsync()
            except _RegistryRecoveryRequired:
                raise PluginRegistryError("plugin_registry_recovery_required") from None
            except PluginRegistryError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_registry_recovery_required") from None
            return PluginMutationResult(updated, _revision_for(next_records))

    def rollback_update_candidate(
        self,
        *,
        expected_base: InstalledPluginRecord,
        expected_candidate: InstalledPluginVersionRecord,
        candidate_binding: PluginAppBinding,
        expected_revision: str,
    ) -> PluginMutationResult:
        """Restore one sealed predecessor after an exact candidate cutover."""

        try:
            base = InstalledPluginRecord.from_dict(
                expected_base.to_dict(),
                registry_key=expected_base.plugin_id,
            )
            candidate = InstalledPluginVersionRecord.from_dict(
                expected_candidate.to_dict()
            )
            binding = PluginAppBinding.from_dict(candidate_binding.to_dict())
        except (AttributeError, TypeError, ValueError):
            raise PluginRegistryError("plugin_registry_recovery_required") from None
        if (
            candidate.state != "candidate"
            or candidate.plugin_version in {
                item.plugin_version for item in base.versions
            }
        ):
            raise PluginRegistryError("plugin_registry_recovery_required")
        revision = _digest(expected_revision)
        expected_promoted = replace(
            base,
            selected_version=candidate.plugin_version,
            app_binding=binding,
            versions=(
                *tuple(replace(item, state="candidate") for item in base.versions),
                replace(candidate, state="selected"),
            ),
        )
        self._prepare_roots()
        with self._locked() as storage:
            records, previous_payload = self._load_unlocked(storage)
            snapshot = _snapshot_for(records)
            if revision != snapshot.revision:
                raise PluginRegistryError("plugin_registry_conflict")
            current = records.get(base.plugin_id)
            if current != base and current != expected_promoted:
                raise PluginRegistryError("plugin_registry_recovery_required")
            next_records = dict(records)
            next_records[base.plugin_id] = base
            target_path = self.packages_root / base.plugin_id / candidate.plugin_version
            target_exists = storage.packages.exists(
                f"{base.plugin_id}/{candidate.plugin_version}"
            )
            try:
                if current != base:
                    _write_registry(
                        storage.state,
                        canonical_bytes(_document_for(next_records)),
                        previous_payload,
                    )
                if target_exists:
                    with PinnedDirectory.open(target_path.parent) as plugin_root:
                        with plugin_root.pin_tree(candidate.plugin_version) as pinned:
                            pinned.remove()
                        plugin_root.fsync()
            except _RegistryRecoveryRequired:
                raise PluginRegistryError("plugin_registry_recovery_required") from None
            except PluginRegistryError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_registry_recovery_required") from None
            return PluginMutationResult(base, _revision_for(next_records))

    def uninstall(
        self,
        plugin_id: str,
        *,
        expected_revision: str,
    ) -> PluginMutationResult:
        """Remove one installed plugin and every authenticated version tree.

        Uninstall is a revision-CAS mutation just like enable/disable.  The
        package root is pinned under the registry lock before any bytes are
        removed, and every retained version is reverified against the signed
        registry record.  A missing or altered tree fails closed rather than
        deleting an ambiguous path.
        """
        identity = _identity(plugin_id)
        revision = _digest_revision(expected_revision)
        self._prepare_roots()
        with self._locked() as storage:
            records, previous_payload = self._load_unlocked(storage)
            current_snapshot = _snapshot_for(records)
            if revision != current_snapshot.revision:
                raise PluginRegistryError("plugin_registry_conflict")
            current = records.get(identity)
            if current is None:
                raise PluginRegistryError("plugin_not_installed")
            if not storage.packages.exists(identity):
                raise PluginRegistryError("plugin_registry_reconciliation_failed")
            package_root = self.packages_root / identity
            try:
                # Verify while the version tree can still be opened normally;
                # Windows delete pins intentionally deny a second open of the
                # same descendant.  The package-root pin immediately below
                # then binds the exact tree that is removed.
                for version in current.versions:
                    target = package_root / version.plugin_version
                    self._verify_published_target(target, version)
            except PluginRegistryError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_uninstall_failed") from None

            next_records = dict(records)
            del next_records[identity]
            try:
                _write_registry(
                    storage.state,
                    canonical_bytes(_document_for(next_records)),
                    previous_payload,
                )
            except _RegistryRecoveryRequired:
                raise PluginRegistryError("plugin_registry_recovery_required") from None
            except PluginRegistryError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_registry_write_failed") from None
            try:
                # Publish the registry removal before deleting bytes so a
                # failed cleanup can only leave an orphaned tree, never an
                # installed record that points at missing package content.
                with storage.packages.pin_tree(identity) as pinned:
                    pinned.remove()
                storage.packages.fsync()
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_uninstall_recovery_required") from None
            return PluginMutationResult(
                record=current,
                revision=_revision_for(next_records),
            )

    def block(
        self,
        plugin_id: str,
        blocker: str,
        *,
        expected_revision: str,
    ) -> PluginMutationResult:
        reason = _blocker(blocker)
        return self._mutate(
            plugin_id,
            expected_revision,
            lambda record: replace(record, blocker=reason),
        )

    def clear_block(
        self,
        plugin_id: str,
        *,
        expected_revision: str,
    ) -> PluginMutationResult:
        return self._mutate(
            plugin_id,
            expected_revision,
            lambda record: replace(record, blocker=None),
        )

    def _mutate(
        self,
        plugin_id: str,
        expected_revision: str,
        mutation: Callable[[InstalledPluginRecord], InstalledPluginRecord],
    ) -> PluginMutationResult:
        identity = _identity(plugin_id)
        revision = _digest_revision(expected_revision)
        self._prepare_roots()
        with self._locked() as storage:
            records, previous_payload = self._load_unlocked(storage)
            current_snapshot = _snapshot_for(records)
            if revision != current_snapshot.revision:
                raise PluginRegistryError("plugin_registry_conflict")
            try:
                current = records[identity]
            except KeyError:
                raise PluginRegistryError("plugin_not_installed") from None
            updated = mutation(current)
            next_records = dict(records)
            next_records[identity] = updated
            try:
                _write_registry(
                    storage.state,
                    canonical_bytes(_document_for(next_records)),
                    previous_payload,
                )
            except _RegistryRecoveryRequired:
                raise PluginRegistryError("plugin_registry_recovery_required") from None
            except PluginRegistryError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_registry_write_failed") from None
            return PluginMutationResult(record=updated, revision=_revision_for(next_records))

    def _verified_staged(self, staged: StagedPluginInstall) -> VerifiedPlugin:
        if type(staged) is not StagedPluginInstall:
            raise PluginRegistryError("plugin_stage_invalid")
        _validated_stage_path(self.packages_root, staged.stage_path)
        _validated_verified_payloads(staged.verified)
        candidate = _version_record_from_verified(staged.verified, state="candidate")
        if candidate != staged.candidate:
            raise PluginRegistryError("plugin_stage_invalid")
        return staged.verified

    def _verify_stage_path(self, stage_path: Path) -> VerifiedPlugin:
        if self._verifier is None:
            raise PluginRegistryError("plugin_verifier_required")
        try:
            reject_reparse(stage_path)
            verified = self._verifier(stage_path)
            _validated_verified_payloads(verified)
            return verified
        except PluginRegistryError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise PluginRegistryError("plugin_stage_invalid") from None

    def _verify_published_target(
        self,
        target: Path,
        candidate: InstalledPluginVersionRecord,
    ) -> VerifiedPlugin:
        try:
            _validate_plain_directory(target)
            verified = self._verifier(target) if self._verifier is not None else None
            if verified is None:
                raise ValueError("verifier missing")
            observed = _version_record_from_verified(verified, state="candidate")
            if not _same_version_bytes(observed, candidate):
                raise ValueError("signed identity mismatch")
            return verified
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise PluginRegistryError("plugin_identity_collision") from None

    @staticmethod
    def _verify_repair_target(
        target: Path,
        candidate: InstalledPluginVersionRecord,
        verifier: Verifier,
    ) -> VerifiedPlugin:
        try:
            _validate_plain_directory(target)
            verified = verifier(target)
            _validated_verified_payloads(verified)
            observed = _version_record_from_verified(
                verified,
                state="candidate",
            )
            if not _same_version_bytes(observed, candidate):
                raise ValueError("signed identity mismatch")
            return verified
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise PluginRegistryError("plugin_identity_collision") from None

    def _discard_validated(
        self,
        staged: StagedPluginInstall,
        packages: PinnedDirectory,
    ) -> None:
        if type(staged) is not StagedPluginInstall:
            raise PluginRegistryError("plugin_stage_invalid")
        stage_path = _validated_stage_path(self.packages_root, staged.stage_path)
        if not packages.exists(stage_path.name):
            return
        try:
            with packages.pin_tree(stage_path.name) as pinned:
                verified = pinned.verify(self._verify_stage_path)
                if type(verified) is not VerifiedPlugin:
                    raise PluginRegistryError("plugin_stage_invalid")
                if not _same_verified_identity(verified, staged.verified):
                    raise PluginRegistryError("plugin_stage_invalid")
                pinned.remove()
            packages.fsync()
        except PluginRegistryError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise PluginRegistryError("plugin_stage_invalid") from None

    def _discard_if_unchanged(
        self,
        staged: StagedPluginInstall,
        packages: PinnedDirectory,
    ) -> None:
        try:
            self._discard_validated(staged, packages)
        except PluginRegistryError:
            return

    def _prepare_roots(self) -> None:
        try:
            self.state_root.mkdir(parents=True, exist_ok=True)
            _validate_plain_directory(self.state_root)
            with PinnedDirectory.open(self.state_root) as state:
                with state.ensure_directory("packages") as packages:
                    packages.validate_binding()
                state.validate_binding()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise PluginRegistryError("plugin_registry_invalid") from None

    @contextmanager
    def _locked(self):
        process_lock = _mutation_lock(self.state_root)
        with process_lock:
            state: PinnedDirectory | None = None
            packages: PinnedDirectory | None = None
            try:
                state = PinnedDirectory.open(self.state_root)
                packages = state.ensure_directory("packages")
                with state.lock_file("registry.json.lock"):
                    state.validate_binding()
                    packages.validate_binding()
                    yield _RegistryStorage(state=state, packages=packages)
                    packages.validate_binding()
                    state.validate_binding()
            except (_RegistryRecoveryRequired, PluginRegistryError):
                raise
            except (OSError, RuntimeError, TypeError, ValueError):
                raise PluginRegistryError("plugin_registry_invalid") from None
            finally:
                if packages is not None:
                    packages.close()
                if state is not None:
                    state.close()

    def _load_unlocked(
        self,
        storage: _RegistryStorage,
    ) -> tuple[dict[str, InstalledPluginRecord], bytes | None]:
        if not storage.state.exists("registry.json"):
            return {}, None
        try:
            payload = storage.state.read_bytes("registry.json", limit=_MAX_REGISTRY_BYTES)
            if len(payload) > _MAX_REGISTRY_BYTES:
                raise ValueError("registry too large")
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_json_constant,
            )
            if canonical_bytes(document) != payload:
                raise ValueError("registry is not canonical")
            data = _exact_object(document, _REGISTRY_FIELDS)
            if type(data["schema_version"]) is not int or data["schema_version"] not in {1, 2}:
                raise ValueError("invalid registry version")
            if type(data["plugins"]) is not dict:
                raise ValueError("invalid plugins object")
            if len(data["plugins"]) > _MAX_REGISTRY_RECORDS:
                raise PluginRegistryError("plugin_registry_limit_exceeded")
            records: dict[str, InstalledPluginRecord] = {}
            aliases: set[str] = set()
            for key, raw in data["plugins"].items():
                identity = _identity(key)
                alias = identity.casefold()
                if alias in aliases:
                    raise ValueError("plugin identity aliases")
                aliases.add(alias)
                records[identity] = InstalledPluginRecord.from_dict(
                    raw,
                    registry_key=key,
                    legacy=data["schema_version"] == 1,
                )
            return records, payload
        except PluginRegistryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PluginRegistryError("plugin_registry_unreadable") from None


def _validated_verified_payloads(verified: VerifiedPlugin) -> bytes:
    try:
        if type(verified) is not VerifiedPlugin:
            raise ValueError("invalid verified plugin")
        manifest = verified.manifest
        _identity(manifest.plugin_id)
        _identity(manifest.publisher_id)
        _identity(manifest.signing_key_id)
        _semver(manifest.plugin_version)
        declared = {asset.path: asset for asset in manifest.assets}
        if set(verified.asset_paths) != set(declared) or set(verified.asset_bytes) != set(declared):
            raise ValueError("asset key mismatch")
        for logical_path, asset in declared.items():
            expected_path = Path(*logical_path.split("/"))
            if (
                not isinstance(verified.asset_paths[logical_path], Path)
                or verified.asset_paths[logical_path] != expected_path
                or expected_path.is_absolute()
            ):
                raise ValueError("asset path mismatch")
            payload = verified.asset_bytes[logical_path]
            if type(payload) is not bytes or hashlib.sha256(payload).hexdigest() != asset.sha256:
                raise ValueError("asset payload mismatch")
        manifest_payload = canonical_bytes(manifest.to_dict())
        if hashlib.sha256(manifest_payload).hexdigest() != verified.manifest_sha256:
            raise ValueError("manifest digest mismatch")
        package_payload = canonical_bytes(
            {
                "manifest_sha256": verified.manifest_sha256,
                "assets": {
                    asset.path: asset.sha256
                    for asset in sorted(manifest.assets, key=lambda item: item.path)
                },
            }
        )
        if hashlib.sha256(package_payload).hexdigest() != verified.package_sha256:
            raise ValueError("package digest mismatch")
        return manifest_payload
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        raise PluginRegistryError("verified_plugin_payload_mismatch") from None


def _materialize_final(
    packages: PinnedDirectory,
    plugin_id: str,
    verified: VerifiedPlugin,
    state: _FinalMaterializationState,
) -> None:
    """Create a final version tree only from the retained authenticated payloads."""
    manifest_payload = _validated_verified_payloads(verified)
    version = verified.manifest.plugin_version
    with packages.ensure_directory(plugin_id) as plugin:
        fsync_directory(packages.path)
        state.cleanup_required = True
        with plugin.create_directory(version) as final:
            state.cleanup_identity = final.identity
            final.write_bytes("plugin.json", manifest_payload)
            for logical_path in sorted(verified.asset_bytes):
                final.write_bytes(
                    verified.asset_paths[logical_path],
                    verified.asset_bytes[logical_path],
                )
            final.fsync()
        plugin.fsync()
        fsync_directory(plugin.path)
    packages.validate_binding()


def _remove_final(
    target: Path,
    expected_identity: tuple[int, int] | None,
) -> None:
    if not _lexists(target):
        return
    if expected_identity is None:
        raise ValueError("package_tree_invalid")
    with PinnedDirectory.open(target.parent) as parent:
        with parent.pin_tree(target.name) as pinned:
            if pinned.identity != expected_identity:
                raise ValueError("package_tree_invalid")
            pinned.remove()
        parent.fsync()


def _same_verified_identity(left: VerifiedPlugin, right: VerifiedPlugin) -> bool:
    return (
        left.manifest.plugin_id,
        left.manifest.publisher_id,
        left.manifest.plugin_version,
        left.manifest.kind,
        left.manifest.signing_key_id,
        left.manifest_sha256,
        left.package_sha256,
        dict(left.asset_paths),
        dict(left.asset_bytes),
    ) == (
        right.manifest.plugin_id,
        right.manifest.publisher_id,
        right.manifest.plugin_version,
        right.manifest.kind,
        right.manifest.signing_key_id,
        right.manifest_sha256,
        right.package_sha256,
        dict(right.asset_paths),
        dict(right.asset_bytes),
    )


def _validated_stage_path(packages_root: Path, stage_path: Path) -> Path:
    try:
        candidate = _absolute(Path(stage_path))
        if candidate.parent != packages_root or _STAGE_RE.fullmatch(candidate.name) is None:
            raise ValueError("invalid stage path")
        ensure_contained(packages_root, candidate)
        return candidate
    except (OSError, RuntimeError, TypeError, ValueError):
        raise PluginRegistryError("plugin_stage_invalid") from None


def _remove_partial_stage(
    stage_path: Path,
    expected_identity: tuple[int, int] | None,
) -> None:
    if not _lexists(stage_path):
        return
    if expected_identity is None:
        raise ValueError("package_tree_invalid")
    with PinnedDirectory.open(stage_path.parent) as parent:
        with parent.pin_tree(stage_path.name) as pinned:
            if pinned.identity != expected_identity:
                raise ValueError("package_tree_invalid")
            pinned.remove()
        parent.fsync()


def _document_for(records: Mapping[str, InstalledPluginRecord]) -> dict[str, object]:
    if len(records) > _MAX_REGISTRY_RECORDS:
        raise PluginRegistryError("plugin_registry_limit_exceeded")
    return {
        "schema_version": 2,
        "plugins": {key: records[key].to_dict() for key in sorted(records)},
    }


def _registry_payload_for(records: Mapping[str, InstalledPluginRecord]) -> bytes:
    payload = canonical_bytes(_document_for(records))
    if len(payload) > _MAX_REGISTRY_BYTES:
        raise PluginRegistryError("plugin_registry_limit_exceeded")
    return payload


def _revision_for(records: Mapping[str, InstalledPluginRecord]) -> str:
    return hashlib.sha256(canonical_bytes(_document_for(records))).hexdigest()


def _snapshot_for(records: Mapping[str, InstalledPluginRecord]) -> PluginRegistrySnapshot:
    return PluginRegistrySnapshot(
        records=tuple(records[key] for key in sorted(records)),
        revision=_revision_for(records),
    )


def _casefold_record(
    records: Mapping[str, InstalledPluginRecord], plugin_id: str
) -> InstalledPluginRecord | None:
    alias = plugin_id.casefold()
    return next((record for key, record in records.items() if key.casefold() == alias), None)


def _version_for(
    current: InstalledPluginRecord | None,
    plugin_version: str,
) -> InstalledPluginVersionRecord | None:
    if current is None:
        return None
    for version in current.versions:
        if version.plugin_version == plugin_version:
            return version
    return None


def _same_version_bytes(
    left: InstalledPluginVersionRecord,
    right: InstalledPluginVersionRecord,
) -> bool:
    return (
        left.plugin_version,
        left.signing_key_id,
        left.manifest_sha256,
        left.package_sha256,
    ) == (
        right.plugin_version,
        right.signing_key_id,
        right.manifest_sha256,
        right.package_sha256,
    )


def _validate_binding_for_manifest(binding: PluginAppBinding, manifest: Any) -> None:
    try:
        required = tuple(sorted(item.app_id for item in manifest.required_apps))
        optional = {item.app_id for item in manifest.optional_apps}
        if binding.required_app_ids != required or not set(binding.optional_app_ids) <= optional:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise PluginRegistryError("plugin_app_binding_invalid") from None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON field")
        document[key] = value
    return document


def _reject_non_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _digest_revision(value: Any) -> str:
    try:
        return _digest(value)
    except ValueError:
        raise PluginRegistryError("invalid_registry_revision") from None


def _absolute(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError("invalid path") from None


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _validate_plain_directory(path: Path) -> None:
    reject_reparse(path)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("invalid directory")


def _contained_direct_child(root: Path, candidate: Path) -> None:
    if candidate.parent != root:
        raise PluginRegistryError("plugin_identity_collision")
    try:
        ensure_contained(root, candidate)
    except ValueError:
        raise PluginRegistryError("plugin_identity_collision") from None


def _after_registry_temp_fsync(_temporary: Path) -> None:
    return


def _replace_registry_file(
    directory: PinnedDirectory,
    temporary_name: str,
    registry_name: str,
) -> None:
    directory.replace(temporary_name, registry_name)


def _write_registry(
    directory: PinnedDirectory,
    payload: bytes,
    previous_payload: bytes | None,
) -> None:
    if len(payload) > _MAX_REGISTRY_BYTES:
        raise PluginRegistryError("plugin_registry_limit_exceeded")
    registry_name = "registry.json"
    temporary_name = f".{registry_name}.{uuid4().hex}.part"
    temporary = directory.path / temporary_name
    replaced = False
    active: BaseException | None = None
    try:
        directory.write_bytes(temporary_name, payload)
        _after_registry_temp_fsync(temporary)
        _replace_registry_file(directory, temporary_name, registry_name)
        replaced = True
        directory.fsync()
    except BaseException as error:
        active = error
        if replaced:
            try:
                _restore_registry_payload(directory, previous_payload)
            except BaseException:
                if not isinstance(error, Exception):
                    raise error
                raise _RegistryRecoveryRequired from None
        raise
    finally:
        try:
            directory.unlink(temporary_name, missing_ok=True)
        except BaseException:
            if active is not None and not isinstance(active, Exception):
                raise active
            raise


def _restore_registry_payload(
    directory: PinnedDirectory,
    previous_payload: bytes | None,
) -> None:
    if previous_payload is None:
        directory.unlink("registry.json", missing_ok=True)
        return
    temporary_name = f".registry.json.{uuid4().hex}.restore"
    try:
        directory.write_bytes(temporary_name, previous_payload)
        directory.replace(temporary_name, "registry.json")
    finally:
        directory.unlink(temporary_name, missing_ok=True)
