"""Atomic, confirmation-bound plugin and dependency installation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from uuid import uuid4

from core import paths as core_paths
from mcp_platform.canonical import canonical_bytes

from . import registry as _registry
from .catalog import (
    MarketplaceCatalogEntry,
    VerifiedMarketplaceCatalog,
    _validated_verified_catalog,
)
from .contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    AppRequirement,
    CapabilityRequirement,
    McpTransport,
    PluginKind,
    parse_app_id,
)
from .dependencies import (
    AppCapabilitySnapshot,
    DependencyResolution,
    NamedCapability,
    resolve_dependencies,
)
from .filesystem import PinnedDirectory
from .local_trust import LOCAL_SEAL_NAME, LocalTrustError, LocalTrustRoot
from .marketplace_importer import VerifiedNormalizedPluginCandidate
from .package_verifier import (
    PackageVerificationError,
    VerifiedPlugin,
    verify_locally_sealed_plugin_package,
)
from .registry import (
    InstalledPluginRecord,
    PluginAppBinding,
    PluginMutationResult,
    PluginRegistry,
    StagedPluginInstall,
)

PluginRegistryError = _registry.PluginRegistryError


_SAFE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_TRANSACTION_RE = re.compile(r"[0-9a-f]{32}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_STAGE_RE = re.compile(r"\.installing-[0-9a-f]{32}")
_JOURNAL_FIELDS = frozenset(
    {
        "schema_version",
        "transaction_id",
        "plugin_id",
        "publisher_id",
        "plugin_version",
        "signing_key_id",
        "catalog_sha256",
        "manifest_sha256",
        "package_sha256",
        "review_sha256",
        "prepared_id",
        "stage_name",
        "state",
        "dependency_receipt_id",
        "expected_registry_revision",
        "published_revision",
        "app_binding",
    }
)
_STATES = frozenset(
    {
        "preparing",
        "prepared",
        "plugin_staging",
        "plugin_staged",
        "dependencies_committing",
        "dependencies_committed",
        "plugin_published",
    }
)
_MAX_JOURNAL_BYTES = 32 * 1024
_MAX_LOCAL_SEAL_BYTES = 8 * 1024
_LOCAL_SEAL_STORE_PREFIX = ".local-seal-"
_JOURNAL_BASENAME = core_paths.PLUGIN_INSTALL_JOURNAL_PATH.name
_LOCKS_GUARD = threading.Lock()
_INSTALL_LOCKS: dict[Path, threading.RLock] = {}


class PluginInstallError(RuntimeError):
    """Stable content-free installation failure."""


@dataclass(frozen=True)
class PreparedDependencySet:
    transaction_id: str
    prepared_id: str
    receipt_id: str
    app_snapshots: tuple[AppCapabilitySnapshot, ...]

    def __post_init__(self) -> None:
        try:
            transaction_id = _transaction_id(self.transaction_id)
            prepared_id = _safe_id(self.prepared_id)
            receipt_id = _safe_id(self.receipt_id)
            if type(self.app_snapshots) is not tuple:
                raise ValueError
            snapshots = tuple(_copy_snapshot(item) for item in self.app_snapshots)
            identities = tuple(item.app_id for item in snapshots)
            if len(identities) != len(set(identities)):
                raise ValueError
        except Exception:
            raise ValueError("prepared dependency set is invalid") from None
        object.__setattr__(self, "transaction_id", transaction_id)
        object.__setattr__(self, "prepared_id", prepared_id)
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(
            self, "app_snapshots", tuple(sorted(snapshots, key=lambda item: item.app_id))
        )


@dataclass(frozen=True)
class DependencyCommitReceipt:
    receipt_id: str
    prepared_id: str
    introduced_app_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            receipt_id = _safe_id(self.receipt_id)
            prepared_id = _safe_id(self.prepared_id)
            if type(self.introduced_app_ids) is not tuple:
                raise ValueError
            app_ids = tuple(sorted(_contract_id(value) for value in self.introduced_app_ids))
            if len(app_ids) != len(set(app_ids)):
                raise ValueError
        except Exception:
            raise ValueError("dependency commit receipt is invalid") from None
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "prepared_id", prepared_id)
        object.__setattr__(self, "introduced_app_ids", app_ids)


@dataclass(frozen=True)
class PluginCapabilityReview:
    kind: str
    name: str
    schema_sha256: str | None
    version: str | None

    def __post_init__(self) -> None:
        try:
            validated = CapabilityRequirement.from_dict(self.to_dict())
        except Exception:
            raise ValueError("plugin capability review is invalid") from None
        object.__setattr__(self, "kind", validated.kind.value)
        object.__setattr__(self, "name", validated.name)
        object.__setattr__(self, "schema_sha256", validated.schema_sha256)
        object.__setattr__(self, "version", validated.version)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "schema_sha256": self.schema_sha256,
            "version": self.version,
        }


@dataclass(frozen=True)
class PluginDependencyReview:
    app_id: str
    publisher_id: str
    deployment: str
    provider_id: str | None
    protocol_version: str
    transports: tuple[str, ...]
    required: bool
    installed: bool
    trusted: bool
    capability_revision: str | None
    capabilities: tuple[PluginCapabilityReview, ...]

    def __post_init__(self) -> None:
        try:
            app_id = _contract_id(self.app_id)
            publisher_id = _contract_id(self.publisher_id)
            deployment = AppDeployment(self.deployment).value
            provider_id = None if self.provider_id is None else _contract_id(self.provider_id)
            if self.protocol_version != PLUGIN_PROTOCOL_VERSION:
                raise ValueError
            if type(self.transports) is not tuple:
                raise ValueError
            transports = tuple(sorted(McpTransport(item).value for item in self.transports))
            if not transports or len(transports) != len(set(transports)):
                raise ValueError
            if (
                deployment == AppDeployment.REMOTE.value
                and McpTransport.STREAMABLE_HTTP.value not in transports
            ):
                raise ValueError
            if type(self.required) is not bool or type(self.installed) is not bool:
                raise ValueError
            if type(self.trusted) is not bool:
                raise ValueError
            revision = (
                None if self.capability_revision is None else _digest(self.capability_revision)
            )
            if type(self.capabilities) is not tuple:
                raise ValueError
            capabilities = tuple(_copy_capability_review(item) for item in self.capabilities)
            keys = tuple((item.kind, item.name) for item in capabilities)
            if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
                raise ValueError
            if (revision is None) != (not self.installed):
                raise ValueError
            if not self.installed and self.trusted:
                raise ValueError
        except Exception:
            raise ValueError("plugin dependency review is invalid") from None
        object.__setattr__(self, "app_id", app_id)
        object.__setattr__(self, "publisher_id", publisher_id)
        object.__setattr__(self, "deployment", deployment)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "protocol_version", PLUGIN_PROTOCOL_VERSION)
        object.__setattr__(self, "transports", transports)
        object.__setattr__(self, "capability_revision", revision)
        object.__setattr__(self, "capabilities", capabilities)

    def to_dict(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "publisher_id": self.publisher_id,
            "deployment": self.deployment,
            "provider_id": self.provider_id,
            "protocol_version": self.protocol_version,
            "transports": list(self.transports),
            "required": self.required,
            "installed": self.installed,
            "trusted": self.trusted,
            "capability_revision": self.capability_revision,
            "capabilities": [item.to_dict() for item in self.capabilities],
        }


@dataclass(frozen=True)
class PluginInstallReview:
    plugin_id: str
    publisher_id: str
    plugin_version: str
    kind: str
    catalog_sha256: str
    manifest_sha256: str
    package_sha256: str
    signing_key_id: str
    expected_registry_revision: str
    required_app_ids: tuple[str, ...]
    optional_app_ids: tuple[str, ...]
    dependencies: tuple[PluginDependencyReview, ...]
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            plugin_id = _contract_id(self.plugin_id)
            publisher_id = _contract_id(self.publisher_id)
            plugin_version = _contract_semver(self.plugin_version)
            signing_key_id = _contract_id(self.signing_key_id)
            for value in (
                self.catalog_sha256,
                self.manifest_sha256,
                self.package_sha256,
                self.expected_registry_revision,
            ):
                _digest(value)
            if type(self.kind) is not str or type(self.dependencies) is not tuple:
                raise ValueError
            kind = PluginKind(self.kind).value
            if kind == PluginKind.APP_ONLY.value:
                raise ValueError
            if type(self.required_app_ids) is not tuple or type(self.optional_app_ids) is not tuple:
                raise ValueError
            required = tuple(sorted(_contract_id(value) for value in self.required_app_ids))
            optional = tuple(sorted(_contract_id(value) for value in self.optional_app_ids))
            if len(required) != len(set(required)) or len(optional) != len(set(optional)):
                raise ValueError
            if set(required) & set(optional):
                raise ValueError
            if kind == PluginKind.SKILL_ONLY.value and (required or optional):
                raise ValueError
            if kind == PluginKind.MCP_BACKED.value and len(required) != 1:
                raise ValueError
            if kind == PluginKind.COMPOSITE.value and len(required) < 2:
                raise ValueError
            dependencies = tuple(_copy_dependency_review(item) for item in self.dependencies)
            dependency_ids = tuple(item.app_id for item in dependencies)
            if tuple(item.app_id for item in dependencies) != tuple(
                sorted(item.app_id for item in dependencies)
            ):
                raise ValueError
            if len(dependency_ids) != len(set(dependency_ids)):
                raise ValueError
            if set(dependency_ids) != set(required) | set(optional):
                raise ValueError
            if any(item.required is not (item.app_id in set(required)) for item in dependencies):
                raise ValueError
        except Exception:
            raise ValueError("plugin install review is invalid") from None
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "publisher_id", publisher_id)
        object.__setattr__(self, "plugin_version", plugin_version)
        object.__setattr__(self, "signing_key_id", signing_key_id)
        object.__setattr__(self, "required_app_ids", required)
        object.__setattr__(self, "optional_app_ids", optional)
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "sha256", hashlib.sha256(canonical_bytes(self.to_dict())).hexdigest()
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "publisher_id": self.publisher_id,
            "plugin_version": self.plugin_version,
            "kind": self.kind,
            "catalog_sha256": self.catalog_sha256,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "signing_key_id": self.signing_key_id,
            "expected_registry_revision": self.expected_registry_revision,
            "required_app_ids": list(self.required_app_ids),
            "optional_app_ids": list(self.optional_app_ids),
            "dependencies": [item.to_dict() for item in self.dependencies],
        }


@dataclass(frozen=True)
class PluginInstallResult:
    transaction_id: str
    review_sha256: str
    record: InstalledPluginRecord
    dependency_receipt: DependencyCommitReceipt
    revision: str

    def __post_init__(self) -> None:
        try:
            transaction_id = _transaction_id(self.transaction_id)
            review_sha256 = _digest(self.review_sha256)
            revision = _digest(self.revision)
            record = _copy_record(self.record)
            receipt = _copy_receipt(self.dependency_receipt)
            if record.status != "installed":
                raise ValueError
        except Exception:
            raise ValueError("plugin install result is invalid") from None
        object.__setattr__(self, "transaction_id", transaction_id)
        object.__setattr__(self, "review_sha256", review_sha256)
        object.__setattr__(self, "record", record)
        object.__setattr__(self, "dependency_receipt", receipt)
        object.__setattr__(self, "revision", revision)


@dataclass(frozen=True)
class InstallRecoveryResult:
    transaction_id: str | None
    plugin_id: str | None
    recovered_state: str
    completed: bool
    record: InstalledPluginRecord | None
    revision: str | None

    def __post_init__(self) -> None:
        try:
            if self.recovered_state not in {"none", "aborted", "rolled_back", "published"}:
                raise ValueError
            if type(self.completed) is not bool:
                raise ValueError
            transaction_id = (
                None if self.transaction_id is None else _transaction_id(self.transaction_id)
            )
            plugin_id = None if self.plugin_id is None else _contract_id(self.plugin_id)
            record = None if self.record is None else _copy_record(self.record)
            revision = None if self.revision is None else _digest(self.revision)
            if self.recovered_state == "none":
                if (
                    any(
                        value is not None for value in (transaction_id, plugin_id, record, revision)
                    )
                    or self.completed
                ):
                    raise ValueError
            elif transaction_id is None or plugin_id is None or not self.completed:
                raise ValueError
            elif self.recovered_state == "published":
                if record is None or revision is None or record.plugin_id != plugin_id:
                    raise ValueError
            elif record is not None or revision is not None:
                raise ValueError
        except Exception:
            raise ValueError("install recovery result is invalid") from None
        object.__setattr__(self, "transaction_id", transaction_id)
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "record", record)
        object.__setattr__(self, "revision", revision)


class InstallConfirmationVerifier(Protocol):
    def consume(self, confirmation: object, expected_review_sha256: str) -> None: ...


class DependencyProvisioner(Protocol):
    def prepare(
        self, transaction_id: str, requirements: tuple[AppRequirement, ...]
    ) -> PreparedDependencySet: ...

    def abort(self, transaction_id: str) -> None: ...

    def commit(self, prepared: PreparedDependencySet) -> DependencyCommitReceipt: ...

    def rollback(self, receipt_id: str) -> None: ...


class PluginInstaller:
    """Serialize, journal, and reconcile one plugin installation transaction."""

    def __init__(
        self,
        *,
        registry: PluginRegistry,
        provisioner: DependencyProvisioner,
        confirmation_verifier: InstallConfirmationVerifier,
        transaction_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.registry = registry
        self.provisioner = provisioner
        self.confirmation_verifier = confirmation_verifier
        self.transaction_id_factory = transaction_id_factory or (lambda: uuid4().hex)
        self.journal_path = Path(registry.state_root) / _JOURNAL_BASENAME

    def accept_normalized(
        self,
        candidate: object,
        envelope: object,
        trust_root: LocalTrustRoot,
        confirmation: object,
        *,
        expected_review_sha256: str,
        transaction_id: str | None = None,
    ) -> PluginInstallResult:
        """Seal, materialize, and durably install one publisher-verified remote candidate.

        This is the installer's only door for a marketplace import, and it opens
        only for the marker type the importer mints after re-proving the source
        evidence and any pinned artifact. There is deliberately no overload that
        takes a raw registry record: discovery metadata must become a verified
        candidate before the installer will look at it at all. An exact type test
        is the point -- a look-alike carrying the same attributes has passed none
        of those checks.

        Unlike :meth:`install_from_catalog`, there is no signed marketplace
        catalog entry backing this candidate -- ``expected_review_sha256`` is
        the exact digest the caller's own discovery review already showed the
        user (``mcp_platform.plugin_lifecycle.PluginDiscoveryInstallReview.sha256``),
        confirmed through the same ``confirmation_verifier`` boundary every
        other install uses. The candidate is sealed and materialized into a
        throwaway scratch tree first; only its fully re-verified bytes (never
        the scratch tree itself) reach :meth:`PluginRegistry.stage`, so the
        registry row, install journal, and package directory this produces are
        the ordinary ones. The device-local seal itself cannot live inside the
        manifest-declared asset set :meth:`PluginRegistry.stage`/``publish``
        materialize from (it authenticates the package rather than being part
        of what the package digest covers), so it is durably kept in a small
        sidecar store next to the install journal, keyed by the sealed
        manifest's own digest, written before staging so the registry's own
        re-verification of the freshly staged tree can find it.
        """
        if type(candidate) is not VerifiedNormalizedPluginCandidate:
            raise PluginInstallError("plugin_install_candidate_unverified")
        if type(trust_root) is not LocalTrustRoot:
            raise PluginInstallError("plugin_local_trust_unavailable")
        if type(expected_review_sha256) is not str:
            raise PluginInstallError("plugin_install_review_changed")
        with self._locked_storage() as storage:
            self._recover_locked(storage)
            verified, seal_bytes = self._seal_and_verify_candidate(
                candidate, envelope, trust_root
            )
            expected_revision = _digest(self.registry.snapshot().revision)
            transaction_id = (
                self._new_transaction_id()
                if transaction_id is None
                else _transaction_id(transaction_id)
            )
            journal = _new_sealed_journal(transaction_id, verified, expected_revision)
            prepared: PreparedDependencySet | None = None
            receipt: DependencyCommitReceipt | None = None
            publication_started = False
            seal_stored = False
            try:
                self._write_journal(storage, journal)
                requirements = verified.manifest.required_apps + verified.manifest.optional_apps
                returned_prepared = self.provisioner.prepare(transaction_id, requirements)
                prepared = _copy_prepared(returned_prepared)
                _validate_prepared(prepared, transaction_id, requirements)

                resolution = resolve_dependencies(verified.manifest, prepared.app_snapshots)
                if not resolution.ready:
                    raise PluginInstallError("plugin_dependencies_unresolved")

                journal = _transition(
                    journal,
                    "prepared",
                    prepared_id=prepared.prepared_id,
                    review_sha256=expected_review_sha256,
                )
                self._write_journal(storage, journal)
                self.confirmation_verifier.consume(confirmation, expected_review_sha256)

                _write_local_seal_store_entry(storage, verified.manifest_sha256, seal_bytes)
                seal_stored = True

                journal = _transition(
                    journal,
                    "plugin_staging",
                    stage_name=f".installing-{transaction_id}",
                )
                self._write_journal(storage, journal)
                staged = self.registry.stage(
                    verified,
                    stage_name=_optional_stage(journal["stage_name"]),
                )
                journal = _transition(journal, "plugin_staged")
                self._write_journal(storage, journal)

                journal = _transition(
                    journal,
                    "dependencies_committing",
                    dependency_receipt_id=prepared.receipt_id,
                    app_binding=_binding_for(verified, prepared).to_dict(),
                )
                self.registry.preflight_publish(
                    staged,
                    expected_revision,
                    app_binding=_journal_binding(journal),
                )
                self._write_journal(storage, journal)
                returned_receipt = self.provisioner.commit(prepared)
                receipt = _copy_receipt(returned_receipt)
                _validate_receipt(receipt, prepared, requirements)
                journal = _transition(journal, "dependencies_committed")
                self._write_journal(storage, journal)

                publication_started = True
                published = self.registry.publish(
                    staged,
                    expected_revision,
                    app_binding=_journal_binding(journal),
                )
                published = _copy_mutation(published)
                result = PluginInstallResult(
                    transaction_id=transaction_id,
                    review_sha256=expected_review_sha256,
                    record=published.record,
                    dependency_receipt=receipt,
                    revision=published.revision,
                )
                terminal = _transition(
                    journal,
                    "plugin_published",
                    published_revision=published.revision,
                )
                self._write_journal(storage, terminal)
                journal = terminal
                self._clear_journal(storage, terminal=terminal)
                return result
            except BaseException as active:
                cancellation = not isinstance(active, Exception)
                if publication_started:
                    try:
                        durable = self._read_journal(storage)
                        if durable is None:
                            raise ValueError
                        observation = self._observe(durable)
                    except BaseException as reconciliation_error:
                        if cancellation:
                            raise active
                        if not isinstance(reconciliation_error, Exception):
                            raise
                        raise PluginInstallError("plugin_install_recovery_failed") from None
                    if observation.published:
                        try:
                            if receipt is None or observation.record is None:
                                raise ValueError
                            if durable["state"] == "plugin_published":
                                if durable["published_revision"] != observation.revision:
                                    raise ValueError
                            else:
                                durable = _transition(
                                    durable,
                                    "plugin_published",
                                    published_revision=observation.revision,
                                )
                                self._write_journal(storage, durable)
                            result = PluginInstallResult(
                                transaction_id=transaction_id,
                                review_sha256=_digest(durable["review_sha256"]),
                                record=observation.record,
                                dependency_receipt=receipt,
                                revision=observation.revision,
                            )
                        except BaseException as reconciliation_error:
                            if cancellation:
                                raise active
                            if not isinstance(reconciliation_error, Exception):
                                raise
                            raise PluginInstallError("plugin_install_recovery_failed") from None
                        if cancellation:
                            raise active
                        try:
                            self._clear_journal(storage, terminal=durable)
                        except BaseException as cleanup_error:
                            if not isinstance(cleanup_error, Exception):
                                raise
                            raise PluginInstallError("plugin_install_recovery_failed") from None
                        return result
                    try:
                        self.provisioner.rollback(_safe_id(durable["dependency_receipt_id"]))
                        self._discard_journaled_stage(durable)
                        if seal_stored:
                            _delete_local_seal_store_entry(storage, verified.manifest_sha256)
                        self._clear_journal(storage)
                    except BaseException as cleanup_error:
                        if cancellation:
                            raise active
                        if not isinstance(cleanup_error, Exception):
                            raise
                        raise PluginInstallError("plugin_install_recovery_required") from None
                    if cancellation:
                        raise active
                    raise
                try:
                    if journal["state"] in {
                        "dependencies_committing",
                        "dependencies_committed",
                    }:
                        self.provisioner.rollback(_safe_id(journal["dependency_receipt_id"]))
                    else:
                        self.provisioner.abort(transaction_id)
                    self._discard_journaled_stage(journal)
                    if seal_stored:
                        _delete_local_seal_store_entry(storage, verified.manifest_sha256)
                    self._clear_journal(storage)
                except BaseException as cleanup_error:
                    if cancellation:
                        raise active
                    if not isinstance(cleanup_error, Exception):
                        raise
                    raise PluginInstallError("plugin_install_recovery_required") from None
                if cancellation:
                    raise active
                raise

    def _seal_and_verify_candidate(
        self,
        candidate: VerifiedNormalizedPluginCandidate,
        envelope: object,
        trust_root: LocalTrustRoot,
    ) -> tuple[VerifiedPlugin, bytes]:
        """Materialize one sealed candidate into a throwaway tree and re-verify it.

        The scratch tree exists only long enough to produce a fully re-proved
        ``VerifiedPlugin`` and the exact ``local_seal.json`` bytes this device
        just signed; nothing about the scratch path itself is retained.
        """
        scratch_root = (
            Path(self.registry.state_root) / f".sealing-{uuid4().hex}"
        )
        try:
            trust_root.materialize_sealed_package(candidate, envelope, scratch_root)
            verified = verify_locally_sealed_plugin_package(scratch_root, trust_root)
            seal_bytes = (scratch_root / LOCAL_SEAL_NAME).read_bytes()
        except LocalTrustError as exc:
            _discard_scratch_root(scratch_root)
            raise PluginInstallError(str(exc)) from None
        except PackageVerificationError as exc:
            _discard_scratch_root(scratch_root)
            raise PluginInstallError(str(exc)) from None
        except OSError:
            _discard_scratch_root(scratch_root)
            raise PluginInstallError("plugin_install_recovery_failed") from None
        _discard_scratch_root(scratch_root)
        return verified, seal_bytes

    def review_from_catalog(
        self,
        catalog: VerifiedMarketplaceCatalog,
        entry: MarketplaceCatalogEntry,
        verified: VerifiedPlugin,
        resolution: DependencyResolution,
        *,
        expected_registry_revision: str,
    ) -> PluginInstallReview:
        return _build_review(catalog, entry, verified, resolution, expected_registry_revision)

    def install_from_catalog(
        self,
        catalog: VerifiedMarketplaceCatalog,
        entry: MarketplaceCatalogEntry,
        verified: VerifiedPlugin,
        confirmation: object,
        *,
        preserve_existing_binding: bool = False,
        transaction_id: str | None = None,
    ) -> PluginInstallResult:
        if type(preserve_existing_binding) is not bool:
            raise PluginInstallError("plugin_install_request_invalid")
        with self._locked_storage() as storage:
            self._recover_locked(storage)
            _validate_catalog_package(catalog, entry, verified)
            expected_revision = _digest(self.registry.snapshot().revision)
            transaction_id = (
                self._new_transaction_id()
                if transaction_id is None
                else _transaction_id(transaction_id)
            )
            journal = _new_journal(
                transaction_id,
                catalog,
                verified,
                expected_revision,
            )
            prepared: PreparedDependencySet | None = None
            receipt: DependencyCommitReceipt | None = None
            publication_started = False
            try:
                self._write_journal(storage, journal)
                requirements = verified.manifest.required_apps + verified.manifest.optional_apps
                prepare = self.provisioner.prepare
                if preserve_existing_binding:
                    prepare = getattr(self.provisioner, "prepare_candidate", prepare)
                returned_prepared = prepare(transaction_id, requirements)
                prepared = _copy_prepared(returned_prepared)
                _validate_prepared(prepared, transaction_id, requirements)

                resolution = resolve_dependencies(verified.manifest, prepared.app_snapshots)
                if not resolution.ready:
                    raise PluginInstallError("plugin_dependencies_unresolved")
                review = self.review_from_catalog(
                    catalog,
                    entry,
                    verified,
                    resolution,
                    expected_registry_revision=expected_revision,
                )
                journal = _transition(
                    journal,
                    "prepared",
                    prepared_id=prepared.prepared_id,
                    review_sha256=review.sha256,
                )
                self._write_journal(storage, journal)
                self.confirmation_verifier.consume(confirmation, review.sha256)

                journal = _transition(
                    journal,
                    "plugin_staging",
                    stage_name=f".installing-{transaction_id}",
                )
                self._write_journal(storage, journal)
                staged = self.registry.stage(
                    verified,
                    stage_name=_optional_stage(journal["stage_name"]),
                )
                journal = _transition(journal, "plugin_staged")
                self._write_journal(storage, journal)

                journal = _transition(
                    journal,
                    "dependencies_committing",
                    dependency_receipt_id=prepared.receipt_id,
                    app_binding=_binding_for(verified, prepared).to_dict(),
                )
                self.registry.preflight_publish(
                    staged,
                    expected_revision,
                    app_binding=_journal_binding(journal),
                    **(
                        {"preserve_existing_binding": True}
                        if preserve_existing_binding
                        else {}
                    ),
                )
                self._write_journal(storage, journal)
                returned_receipt = self.provisioner.commit(prepared)
                receipt = _copy_receipt(returned_receipt)
                _validate_receipt(
                    receipt,
                    prepared,
                    requirements,
                )
                journal = _transition(journal, "dependencies_committed")
                self._write_journal(storage, journal)

                publication_started = True
                published = self.registry.publish(
                    staged,
                    expected_revision,
                    app_binding=_journal_binding(journal),
                    **(
                        {"preserve_existing_binding": True}
                        if preserve_existing_binding
                        else {}
                    ),
                )
                published = _copy_mutation(published)
                result = PluginInstallResult(
                    transaction_id=transaction_id,
                    review_sha256=review.sha256,
                    record=published.record,
                    dependency_receipt=receipt,
                    revision=published.revision,
                )
                terminal = _transition(
                    journal,
                    "plugin_published",
                    published_revision=published.revision,
                )
                self._write_journal(storage, terminal)
                journal = terminal
                self._clear_journal(storage, terminal=terminal)
                return result
            except BaseException as active:
                cancellation = not isinstance(active, Exception)
                if publication_started:
                    try:
                        durable = self._read_journal(storage)
                        if durable is None:
                            raise ValueError
                        observation = self._observe(durable)
                    except BaseException as reconciliation_error:
                        if cancellation:
                            raise active
                        if not isinstance(reconciliation_error, Exception):
                            raise
                        raise PluginInstallError("plugin_install_recovery_failed") from None
                    if observation.published:
                        try:
                            if receipt is None or observation.record is None:
                                raise ValueError
                            if durable["state"] == "plugin_published":
                                if durable["published_revision"] != observation.revision:
                                    raise ValueError
                            else:
                                durable = _transition(
                                    durable,
                                    "plugin_published",
                                    published_revision=observation.revision,
                                )
                                self._write_journal(storage, durable)
                            result = PluginInstallResult(
                                transaction_id=transaction_id,
                                review_sha256=_digest(durable["review_sha256"]),
                                record=observation.record,
                                dependency_receipt=receipt,
                                revision=observation.revision,
                            )
                        except BaseException as reconciliation_error:
                            if cancellation:
                                raise active
                            if not isinstance(reconciliation_error, Exception):
                                raise
                            raise PluginInstallError("plugin_install_recovery_failed") from None
                        if cancellation:
                            raise active
                        try:
                            self._clear_journal(storage, terminal=durable)
                        except BaseException as cleanup_error:
                            if not isinstance(cleanup_error, Exception):
                                raise
                            raise PluginInstallError("plugin_install_recovery_failed") from None
                        return result
                    try:
                        self.provisioner.rollback(_safe_id(durable["dependency_receipt_id"]))
                        self._discard_journaled_stage(durable)
                        self._clear_journal(storage)
                    except BaseException as cleanup_error:
                        if cancellation:
                            raise active
                        if not isinstance(cleanup_error, Exception):
                            raise
                        raise PluginInstallError("plugin_install_recovery_required") from None
                    if cancellation:
                        raise active
                    raise
                try:
                    if journal["state"] in {
                        "dependencies_committing",
                        "dependencies_committed",
                    }:
                        self.provisioner.rollback(_safe_id(journal["dependency_receipt_id"]))
                    else:
                        self.provisioner.abort(transaction_id)
                    self._discard_journaled_stage(journal)
                    self._clear_journal(storage)
                except BaseException as cleanup_error:
                    if cancellation:
                        raise active
                    if not isinstance(cleanup_error, Exception):
                        raise
                    raise PluginInstallError("plugin_install_recovery_required") from None
                if cancellation:
                    raise active
                raise

    def recover(self) -> InstallRecoveryResult:
        with self._locked_storage() as storage:
            return self._recover_locked(storage)

    def reconcile_installed_binding(self, plugin_id: str) -> PluginMutationResult:
        """Close a legacy record only after re-verifying its selected signed package."""
        with self._locked_storage() as storage:
            self._recover_locked(storage)
            expected_revision = _digest(self.registry.snapshot().revision)
            record = self.registry.get(plugin_id)
            if record.app_binding is not None:
                return PluginMutationResult(record, expected_revision)
            verified = self.registry.reverify_installed(plugin_id)
            transaction_id = self._new_transaction_id()
            prepared: PreparedDependencySet | None = None
            commit_started = False
            journal: dict[str, object] | None = None
            try:
                requirements = verified.manifest.required_apps + verified.manifest.optional_apps
                prepared = _copy_prepared(self.provisioner.prepare(transaction_id, requirements))
                _validate_prepared(prepared, transaction_id, requirements)
                if not resolve_dependencies(verified.manifest, prepared.app_snapshots).ready:
                    raise PluginInstallError("plugin_dependencies_unresolved")
                binding = _binding_for(verified, prepared)
                self.registry.preflight_bind_apps(
                    plugin_id,
                    expected_revision=expected_revision,
                    app_binding=binding,
                )
                journal = _new_migration_journal(
                    transaction_id,
                    verified,
                    expected_revision,
                    prepared,
                    binding,
                )
                self._write_journal(storage, journal)
                commit_started = True
                receipt = _copy_receipt(self.provisioner.commit(prepared))
                _validate_receipt(receipt, prepared, requirements)
                journal = _transition(journal, "dependencies_committed")
                self._write_journal(storage, journal)
                mutation = self.registry.bind_apps(
                    plugin_id,
                    expected_revision=expected_revision,
                    app_binding=binding,
                )
                journal = _transition(
                    journal,
                    "plugin_published",
                    published_revision=mutation.revision,
                )
                self._write_journal(storage, journal)
                self._clear_journal(storage, terminal=journal)
                return mutation
            except BaseException as active:
                cancellation = not isinstance(active, Exception)
                try:
                    durable = self._read_journal(storage)
                    if durable is not None:
                        observation = self._observe(durable, require_app_binding=False)
                        if _journal_has_published_binding(durable, observation):
                            if cancellation:
                                raise active
                            terminal = durable
                            if terminal["state"] != "plugin_published":
                                terminal = _transition(
                                    terminal,
                                    "plugin_published",
                                    published_revision=observation.revision,
                                )
                                self._write_journal(storage, terminal)
                            self._clear_journal(storage, terminal=terminal)
                            if observation.record is None:
                                raise ValueError
                            return PluginMutationResult(observation.record, observation.revision)
                    if commit_started:
                        self.provisioner.rollback(
                            prepared.receipt_id if prepared is not None else ""
                        )
                    else:
                        self.provisioner.abort(transaction_id)
                    if durable is not None:
                        self._clear_journal(storage)
                except BaseException as cleanup_error:
                    if cancellation:
                        raise active
                    if not isinstance(cleanup_error, Exception):
                        raise
                    raise PluginInstallError("plugin_install_recovery_required") from None
                if cancellation:
                    raise active
                raise

    @contextmanager
    def _locked_storage(self) -> Iterator[PinnedDirectory]:
        root = Path(self.registry.state_root)
        lock = _shared_lock(root)
        with lock:
            storage: PinnedDirectory | None = None
            journal_lock = None
            lock_entered = False
            try:
                try:
                    storage = _open_root(root)
                    journal_lock = storage.lock_file(f"{_JOURNAL_BASENAME}.lock")
                    journal_lock.__enter__()
                    lock_entered = True
                    storage.validate_binding()
                except Exception:
                    raise PluginInstallError("plugin_install_recovery_failed") from None
                yield storage
                storage.validate_binding()
            finally:
                exception = sys.exc_info()
                if journal_lock is not None and lock_entered:
                    journal_lock.__exit__(*exception)
                if storage is not None:
                    storage.close()

    def _new_transaction_id(self) -> str:
        try:
            return _transaction_id(self.transaction_id_factory())
        except Exception:
            raise PluginInstallError("plugin_install_transaction_invalid") from None

    def _write_journal(self, storage: PinnedDirectory, document: dict[str, object]) -> None:
        validated = _validate_journal(document)
        payload = canonical_bytes(validated)
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise PluginInstallError("plugin_install_journal_limit_exceeded")
        temporary_name = f".{_JOURNAL_BASENAME}.{uuid4().hex}.part"
        temporary_path = storage.path / temporary_name
        try:
            storage.write_bytes(temporary_name, payload)
            _before_journal_replace(temporary_path, self.journal_path)
            storage.replace(temporary_name, self.journal_path.name)
            _after_journal_write(self.journal_path)
        except BaseException as active:
            try:
                storage.unlink(temporary_name, missing_ok=True)
            except BaseException:
                if not isinstance(active, Exception):
                    raise active
                raise
            raise
        else:
            storage.unlink(temporary_name, missing_ok=True)

    def _clear_journal(
        self,
        storage: PinnedDirectory,
        *,
        terminal: dict[str, object] | None = None,
    ) -> None:
        try:
            storage.unlink(self.journal_path.name, missing_ok=True)
        except BaseException as active:
            if terminal is not None:
                try:
                    if not storage.exists(self.journal_path.name):
                        self._write_journal(storage, terminal)
                except BaseException:
                    raise active
            raise

    def _read_journal(self, storage: PinnedDirectory) -> dict[str, object] | None:
        try:
            if not storage.exists(self.journal_path.name):
                return None
            payload = storage.read_bytes(self.journal_path.name, limit=_MAX_JOURNAL_BYTES)
            parsed = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
            if canonical_bytes(parsed) != payload:
                raise ValueError
            return _validate_journal(parsed)
        except PluginInstallError:
            raise
        except Exception:
            raise PluginInstallError("plugin_install_recovery_failed") from None

    def _observe(self, journal: dict[str, object], *, require_app_binding: bool = True):
        try:
            observation = self.registry.observe_publication(**_publication_identity(journal))
            if (
                require_app_binding
                and observation.published
                and (
                    observation.record is None
                    or observation.record.app_binding != _journal_binding(journal)
                )
            ):
                raise ValueError
            return observation
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise PluginInstallError("plugin_install_recovery_failed") from None

    def _recover_stage(self, journal: dict[str, object]) -> StagedPluginInstall | None:
        stage_name = journal["stage_name"]
        if type(stage_name) is not str:
            raise PluginInstallError("plugin_install_recovery_failed")
        try:
            return self.registry.recover_stage(
                stage_name,
                **_publication_identity(journal),
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise PluginInstallError("plugin_install_recovery_failed") from None

    def _discard_journaled_stage(self, journal: dict[str, object]) -> None:
        if journal["stage_name"] is None:
            return
        staged = self._recover_stage(journal)
        if staged is not None:
            self.registry.discard(staged)

    def _recover_locked(self, storage: PinnedDirectory) -> InstallRecoveryResult:
        journal = self._read_journal(storage)
        if journal is None:
            return InstallRecoveryResult(None, None, "none", False, None, None)
        transaction_id = _transaction_id(journal["transaction_id"])
        plugin_id = _contract_id(journal["plugin_id"])
        state = journal["state"]
        try:
            if state in {"preparing", "prepared"}:
                self.provisioner.abort(transaction_id)
                result = InstallRecoveryResult(
                    transaction_id, plugin_id, "aborted", True, None, None
                )
                self._clear_journal(storage)
                return result
            if state in {"plugin_staging", "plugin_staged"}:
                self.provisioner.abort(transaction_id)
                self._discard_journaled_stage(journal)
                result = InstallRecoveryResult(
                    transaction_id, plugin_id, "aborted", True, None, None
                )
                self._clear_journal(storage)
                return result
            migration = _is_migration_journal(journal)
            observation = self._observe(journal, require_app_binding=not migration)
            if observation.published and (
                not migration or _journal_has_published_binding(journal, observation)
            ):
                if observation.record is None:
                    raise ValueError
                if state == "dependencies_committing" and not migration:
                    raise ValueError
                if state == "dependencies_committed":
                    terminal = _transition(
                        journal,
                        "plugin_published",
                        published_revision=observation.revision,
                    )
                    self._write_journal(storage, terminal)
                elif journal["published_revision"] != observation.revision:
                    raise ValueError
                else:
                    terminal = journal
                result = InstallRecoveryResult(
                    transaction_id,
                    plugin_id,
                    "published",
                    True,
                    observation.record,
                    observation.revision,
                )
                self._clear_journal(storage, terminal=terminal)
                return result
            if state == "plugin_published":
                raise ValueError
            receipt_id = _safe_id(journal["dependency_receipt_id"])
            self.provisioner.rollback(receipt_id)
            self._discard_journaled_stage(journal)
            result = InstallRecoveryResult(
                transaction_id, plugin_id, "rolled_back", True, None, None
            )
            self._clear_journal(storage)
            return result
        except PluginInstallError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise PluginInstallError("plugin_install_recovery_failed") from None


def _safe_id(value: Any) -> str:
    if type(value) is not str or _SAFE_ID_RE.fullmatch(value) is None:
        raise ValueError
    return value


def _transaction_id(value: Any) -> str:
    if type(value) is not str or _TRANSACTION_RE.fullmatch(value) is None:
        raise ValueError
    return value


def _contract_id(value: Any) -> str:
    return parse_app_id(value)


def _digest(value: Any) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError
    return value


def _contract_semver(value: Any) -> str:
    validated = CapabilityRequirement.from_dict(
        {
            "kind": "extension",
            "name": "omega.validation/version",
            "schema_sha256": None,
            "version": value,
        }
    )
    if validated.version is None:
        raise ValueError
    return validated.version


def _copy_capability(item: NamedCapability) -> NamedCapability:
    if type(item) is not NamedCapability:
        raise ValueError
    # Every field, including the kind. A copy that drops one compares unequal
    # to its original, and this copy is what a prepared dependency set is
    # validated against -- the mismatch surfaces as an invalid receipt.
    return NamedCapability(
        item.name, item.schema_sha256, item.version, item.description, item.kind
    )


def _copy_snapshot(item: AppCapabilitySnapshot) -> AppCapabilitySnapshot:
    if type(item) is not AppCapabilitySnapshot:
        raise ValueError
    return AppCapabilitySnapshot(
        app_id=item.app_id,
        publisher_id=item.publisher_id,
        deployment=item.deployment,
        provider_id=item.provider_id,
        protocol_version=item.protocol_version,
        transports=tuple(item.transports),
        remote_origin=item.remote_origin,
        authorization_issuer=item.authorization_issuer,
        authentication=item.authentication,
        tools=tuple(_copy_capability(value) for value in item.tools),
        prompts=tuple(_copy_capability(value) for value in item.prompts),
        resources=tuple(_copy_capability(value) for value in item.resources),
        extensions=tuple(_copy_capability(value) for value in item.extensions),
        capability_revision=item.capability_revision,
        installed=item.installed,
        trusted=item.trusted,
        enabled=item.enabled,
    )


def _copy_record(record: InstalledPluginRecord) -> InstalledPluginRecord:
    if type(record) is not InstalledPluginRecord:
        raise ValueError
    return InstalledPluginRecord.from_dict(record.to_dict(), registry_key=record.plugin_id)


def _copy_receipt(receipt: DependencyCommitReceipt) -> DependencyCommitReceipt:
    if type(receipt) is not DependencyCommitReceipt:
        raise ValueError
    return DependencyCommitReceipt(
        receipt.receipt_id, receipt.prepared_id, tuple(receipt.introduced_app_ids)
    )


def _copy_prepared(prepared: PreparedDependencySet) -> PreparedDependencySet:
    if type(prepared) is not PreparedDependencySet:
        raise PluginInstallError("plugin_dependency_preparation_invalid")
    return PreparedDependencySet(
        prepared.transaction_id,
        prepared.prepared_id,
        prepared.receipt_id,
        tuple(prepared.app_snapshots),
    )


def _copy_mutation(mutation: PluginMutationResult) -> PluginMutationResult:
    if type(mutation) is not PluginMutationResult:
        raise PluginInstallError("plugin_publication_invalid")
    try:
        return PluginMutationResult(_copy_record(mutation.record), _digest(mutation.revision))
    except Exception:
        raise PluginInstallError("plugin_publication_invalid") from None


def _binding_for(verified: VerifiedPlugin, prepared: PreparedDependencySet) -> PluginAppBinding:
    try:
        resolution = resolve_dependencies(verified.manifest, prepared.app_snapshots)
        if not resolution.ready:
            raise ValueError
        return PluginAppBinding(
            required_app_ids=tuple(sorted(item.app_id for item in verified.manifest.required_apps)),
            optional_app_ids=tuple(sorted(item.requirement.app_id for item in resolution.optional)),
            dependency_receipt_id=prepared.receipt_id,
            revision=hashlib.sha256(
                canonical_bytes(
                    {
                        "required_app_ids": sorted(
                            item.app_id for item in verified.manifest.required_apps
                        ),
                        "optional_app_ids": sorted(
                            item.requirement.app_id for item in resolution.optional
                        ),
                        "dependency_receipt_id": prepared.receipt_id,
                    }
                )
            ).hexdigest(),
        )
    except (AttributeError, TypeError, ValueError):
        raise PluginInstallError("plugin_dependency_preparation_invalid") from None


def _journal_binding(journal: dict[str, object]) -> PluginAppBinding:
    try:
        binding = PluginAppBinding.from_dict(journal["app_binding"])
        if binding.dependency_receipt_id != _safe_id(journal["dependency_receipt_id"]):
            raise ValueError
        return binding
    except (KeyError, TypeError, ValueError):
        raise PluginInstallError("plugin_install_recovery_failed") from None


def _copy_dependency_review(item: PluginDependencyReview) -> PluginDependencyReview:
    if type(item) is not PluginDependencyReview:
        raise ValueError
    return PluginDependencyReview(
        app_id=_contract_id(item.app_id),
        publisher_id=_contract_id(item.publisher_id),
        deployment=item.deployment,
        provider_id=None if item.provider_id is None else _contract_id(item.provider_id),
        protocol_version=item.protocol_version,
        transports=tuple(item.transports),
        required=item.required,
        installed=item.installed,
        trusted=item.trusted,
        capability_revision=None
        if item.capability_revision is None
        else _digest(item.capability_revision),
        capabilities=tuple(_copy_capability_review(value) for value in item.capabilities),
    )


def _copy_capability_review(item: PluginCapabilityReview) -> PluginCapabilityReview:
    if type(item) is not PluginCapabilityReview:
        raise ValueError
    return PluginCapabilityReview(item.kind, item.name, item.schema_sha256, item.version)


def _validate_prepared(
    prepared: PreparedDependencySet,
    transaction_id: str,
    requirements: tuple[AppRequirement, ...],
) -> None:
    requested_ids = {item.app_id for item in requirements}
    observed_ids = {item.app_id for item in prepared.app_snapshots}
    if prepared.transaction_id != transaction_id or not observed_ids <= requested_ids:
        raise PluginInstallError("plugin_dependency_preparation_invalid")


def _validate_receipt(
    receipt: DependencyCommitReceipt,
    prepared: PreparedDependencySet,
    requirements: tuple[AppRequirement, ...],
) -> None:
    requested_ids = {item.app_id for item in requirements}
    if (
        receipt.receipt_id != prepared.receipt_id
        or receipt.prepared_id != prepared.prepared_id
        or not set(receipt.introduced_app_ids) <= requested_ids
    ):
        raise PluginInstallError("plugin_dependency_commit_invalid")


def _validate_resolution_coverage(
    required_apps: tuple[AppRequirement, ...],
    optional_apps: tuple[AppRequirement, ...],
    resolution: DependencyResolution,
) -> None:
    required_by_id = {item.app_id: item for item in required_apps}
    optional_by_id = {item.app_id: item for item in optional_apps}
    resolved_required = {item.requirement.app_id: item.requirement for item in resolution.required}
    resolved_optional = {item.requirement.app_id: item.requirement for item in resolution.optional}
    blocked_required = {item.app_id for item in resolution.blockers}
    blocked_optional = {item.app_id for item in resolution.optional_blockers}
    if (
        set(resolved_required) | blocked_required != set(required_by_id)
        or set(resolved_optional) | blocked_optional != set(optional_by_id)
        or any(
            requirement.to_dict() != required_by_id[app_id].to_dict()
            for app_id, requirement in resolved_required.items()
        )
        or any(
            requirement.to_dict() != optional_by_id[app_id].to_dict()
            for app_id, requirement in resolved_optional.items()
        )
    ):
        raise ValueError


def _build_review(
    catalog: VerifiedMarketplaceCatalog,
    entry: MarketplaceCatalogEntry,
    verified: VerifiedPlugin,
    resolution: DependencyResolution,
    expected_revision: str,
) -> PluginInstallReview:
    try:
        _validate_catalog_package(catalog, entry, verified)
        if type(resolution) is not DependencyResolution:
            raise ValueError
        revision = _digest(expected_revision)
        manifest = verified.manifest
        _validate_resolution_coverage(
            manifest.required_apps,
            manifest.optional_apps,
            resolution,
        )
        resolved = {
            item.requirement.app_id: item.app for item in resolution.required + resolution.optional
        }
        dependencies = tuple(
            _dependency_review(requirement, resolved.get(requirement.app_id), required)
            for required, requirements in (
                (True, manifest.required_apps),
                (False, manifest.optional_apps),
            )
            for requirement in requirements
        )
        dependencies = tuple(sorted(dependencies, key=lambda item: item.app_id))
        return PluginInstallReview(
            plugin_id=manifest.plugin_id,
            publisher_id=manifest.publisher_id,
            plugin_version=manifest.plugin_version,
            kind=manifest.kind.value,
            catalog_sha256=catalog.catalog_sha256,
            manifest_sha256=verified.manifest_sha256,
            package_sha256=verified.package_sha256,
            signing_key_id=manifest.signing_key_id,
            expected_registry_revision=revision,
            required_app_ids=tuple(item.app_id for item in manifest.required_apps),
            optional_app_ids=tuple(item.app_id for item in manifest.optional_apps),
            dependencies=dependencies,
        )
    except PluginInstallError:
        raise
    except Exception:
        raise ValueError("plugin install review is invalid") from None


def _validate_catalog_package(
    catalog: VerifiedMarketplaceCatalog,
    entry: MarketplaceCatalogEntry,
    verified: VerifiedPlugin,
) -> None:
    # A retired pre-v2 flat capability-authority package is a compatibility
    # fork the spec forbids (principle 8): reject it here, ahead of the
    # broad identity check below, so review (catalog verification) and
    # install both fail closed with a stable, distinct cause instead of the
    # generic mismatch label.
    if type(verified) is VerifiedPlugin and verified.legacy_authority:
        raise PluginInstallError("plugin_capability_authority_legacy")
    try:
        catalog = _validated_verified_catalog(catalog)
        if (
            type(entry) is not MarketplaceCatalogEntry
            or type(verified) is not VerifiedPlugin
            or entry not in catalog.entries
        ):
            raise ValueError
        manifest = verified.manifest
        expected = (
            manifest.plugin_id,
            manifest.publisher_id,
            manifest.plugin_version,
            verified.manifest_sha256,
            verified.package_sha256,
        )
        observed = (
            entry.plugin_id,
            entry.publisher_id,
            entry.plugin_version,
            entry.manifest_sha256,
            entry.package_sha256,
        )
        _digest(catalog.catalog_sha256)
        if observed != expected:
            raise ValueError
    except Exception:
        raise PluginInstallError("plugin_catalog_package_mismatch") from None


def _dependency_review(
    requirement: AppRequirement,
    snapshot: AppCapabilitySnapshot | None,
    required: bool,
) -> PluginDependencyReview:
    capabilities = tuple(
        sorted(
            (
                PluginCapabilityReview(
                    item.kind.value,
                    item.name,
                    item.schema_sha256,
                    item.version,
                )
                for item in requirement.capabilities
            ),
            key=lambda item: (item.kind, item.name),
        )
    )
    return PluginDependencyReview(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=requirement.deployment.value,
        provider_id=requirement.provider_id,
        protocol_version=requirement.protocol_version,
        transports=tuple(item.value for item in requirement.transports),
        required=required,
        installed=snapshot is not None and snapshot.installed,
        trusted=snapshot is not None and snapshot.trusted,
        capability_revision=None if snapshot is None else snapshot.capability_revision,
        capabilities=capabilities,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError


def _shared_lock(root: Path) -> threading.RLock:
    key = Path(root).absolute()
    with _LOCKS_GUARD:
        return _INSTALL_LOCKS.setdefault(key, threading.RLock())


def _open_root(root: Path) -> PinnedDirectory:
    try:
        return PinnedDirectory.open(root)
    except ValueError:
        try:
            return PinnedDirectory.create(root)
        except ValueError:
            return PinnedDirectory.open(root)


def _new_journal(
    transaction_id: str,
    catalog: VerifiedMarketplaceCatalog,
    verified: VerifiedPlugin,
    expected_revision: str,
) -> dict[str, object]:
    manifest = verified.manifest
    return _validate_journal(
        {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "plugin_id": manifest.plugin_id,
            "publisher_id": manifest.publisher_id,
            "plugin_version": manifest.plugin_version,
            "signing_key_id": manifest.signing_key_id,
            "catalog_sha256": catalog.catalog_sha256,
            "manifest_sha256": verified.manifest_sha256,
            "package_sha256": verified.package_sha256,
            "review_sha256": None,
            "prepared_id": None,
            "stage_name": None,
            "state": "preparing",
            "dependency_receipt_id": None,
            "expected_registry_revision": expected_revision,
            "published_revision": None,
            "app_binding": None,
        }
    )


def _new_sealed_journal(
    transaction_id: str,
    verified: VerifiedPlugin,
    expected_revision: str,
) -> dict[str, object]:
    """Build the initial journal for a locally sealed publisher-verified install.

    No signed marketplace catalog backs this transaction, so ``catalog_sha256``
    carries the sealed package's own digest as a stable, present anchor --
    ``_validate_journal`` only ever checks it is a well-formed digest, never
    that it names a real catalog, so this keeps the journal on the ordinary
    (non-migration) state machine every other install already uses.
    """
    manifest = verified.manifest
    return _validate_journal(
        {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "plugin_id": manifest.plugin_id,
            "publisher_id": manifest.publisher_id,
            "plugin_version": manifest.plugin_version,
            "signing_key_id": manifest.signing_key_id,
            "catalog_sha256": verified.package_sha256,
            "manifest_sha256": verified.manifest_sha256,
            "package_sha256": verified.package_sha256,
            "review_sha256": None,
            "prepared_id": None,
            "stage_name": None,
            "state": "preparing",
            "dependency_receipt_id": None,
            "expected_registry_revision": expected_revision,
            "published_revision": None,
            "app_binding": None,
        }
    )


def _new_migration_journal(
    transaction_id: str,
    verified: VerifiedPlugin,
    expected_revision: str,
    prepared: PreparedDependencySet,
    binding: PluginAppBinding,
) -> dict[str, object]:
    manifest = verified.manifest
    return _validate_journal(
        {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "plugin_id": manifest.plugin_id,
            "publisher_id": manifest.publisher_id,
            "plugin_version": manifest.plugin_version,
            "signing_key_id": manifest.signing_key_id,
            "catalog_sha256": None,
            "manifest_sha256": verified.manifest_sha256,
            "package_sha256": verified.package_sha256,
            "review_sha256": None,
            "prepared_id": prepared.prepared_id,
            "stage_name": None,
            "state": "dependencies_committing",
            "dependency_receipt_id": prepared.receipt_id,
            "expected_registry_revision": expected_revision,
            "published_revision": None,
            "app_binding": binding.to_dict(),
        }
    )


def _transition(
    document: dict[str, object],
    state: str,
    **changes: object,
) -> dict[str, object]:
    updated = dict(document)
    updated.update(changes)
    updated["state"] = state
    return _validate_journal(updated)


def _validate_journal(raw: Any) -> dict[str, object]:
    try:
        if type(raw) is not dict or set(raw) != _JOURNAL_FIELDS:
            raise ValueError
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ValueError
        transaction_id = _transaction_id(raw["transaction_id"])
        for field_name in ("plugin_id", "publisher_id", "signing_key_id"):
            _contract_id(raw[field_name])
        _contract_semver(raw["plugin_version"])
        for field_name in ("manifest_sha256", "package_sha256", "expected_registry_revision"):
            _digest(raw[field_name])
        state = raw["state"]
        if state not in _STATES:
            raise ValueError
        review = _optional_digest(raw["review_sha256"])
        catalog = _optional_digest(raw["catalog_sha256"])
        prepared = _optional_safe_id(raw["prepared_id"])
        stage = _optional_stage(raw["stage_name"])
        if stage is not None and stage != f".installing-{transaction_id}":
            raise ValueError
        receipt = _optional_safe_id(raw["dependency_receipt_id"])
        published = _optional_digest(raw["published_revision"])
        binding = raw["app_binding"]
        if state == "preparing":
            if catalog is None:
                raise ValueError
            if any(
                value is not None
                for value in (review, prepared, stage, receipt, published, binding)
            ):
                raise ValueError
        elif state == "prepared":
            if catalog is None or review is None or prepared is None:
                raise ValueError
            if any(value is not None for value in (stage, receipt, published, binding)):
                raise ValueError
        elif state in {"plugin_staging", "plugin_staged"}:
            if catalog is None or review is None or prepared is None or stage is None:
                raise ValueError
            if receipt is not None or published is not None or binding is not None:
                raise ValueError
        elif state in {"dependencies_committing", "dependencies_committed"}:
            migration = catalog is None and review is None and stage is None
            if not migration and any(
                value is None for value in (catalog, review, prepared, stage, receipt)
            ):
                raise ValueError
            if migration and any(value is None for value in (prepared, receipt)):
                raise ValueError
            if published is not None or binding is None:
                raise ValueError
            if PluginAppBinding.from_dict(binding).dependency_receipt_id != receipt:
                raise ValueError
        else:
            migration = catalog is None and review is None and stage is None
            if not migration and any(
                value is None
                for value in (catalog, review, prepared, stage, receipt, published, binding)
            ):
                raise ValueError
            if migration and any(
                value is None for value in (prepared, receipt, published, binding)
            ):
                raise ValueError
            if PluginAppBinding.from_dict(binding).dependency_receipt_id != receipt:
                raise ValueError
        return dict(raw)
    except Exception:
        raise PluginInstallError("plugin_install_recovery_failed") from None


def _optional_digest(value: Any) -> str | None:
    return None if value is None else _digest(value)


def _optional_safe_id(value: Any) -> str | None:
    return None if value is None else _safe_id(value)


def _optional_stage(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _STAGE_RE.fullmatch(value) is None:
        raise ValueError
    return value


def _is_migration_journal(document: dict[str, object]) -> bool:
    return (
        document["catalog_sha256"] is None
        and document["review_sha256"] is None
        and document["stage_name"] is None
    )


def _journal_has_published_binding(document: dict[str, object], observation: Any) -> bool:
    try:
        return (
            observation.published
            and observation.record is not None
            and observation.record.app_binding == _journal_binding(document)
        )
    except (AttributeError, TypeError, ValueError, PluginInstallError):
        return False


def _publication_identity(document: dict[str, object]) -> dict[str, str]:
    return {
        "plugin_id": _contract_id(document["plugin_id"]),
        "publisher_id": _contract_id(document["publisher_id"]),
        "plugin_version": _contract_semver(document["plugin_version"]),
        "signing_key_id": _contract_id(document["signing_key_id"]),
        "manifest_sha256": _digest(document["manifest_sha256"]),
        "package_sha256": _digest(document["package_sha256"]),
    }


def _after_journal_write(_path: Path) -> None:
    return


def _before_journal_replace(_temporary: Path, _target: Path) -> None:
    return


def _local_seal_store_name(manifest_sha256: str) -> str:
    return f"{_LOCAL_SEAL_STORE_PREFIX}{_digest(manifest_sha256)}.json"


def _write_local_seal_store_entry(
    storage: PinnedDirectory, manifest_sha256: str, seal_bytes: bytes
) -> None:
    """Durably persist one sealed package's ``local_seal.json`` bytes.

    Keyed by the sealed manifest's own digest (not the plugin id) so a later
    reverification -- which sees only the manifest and capability-authority
    asset the registry's own stage/publish materialize -- can find the exact
    seal this device signed for that exact manifest, without the seal itself
    ever needing to travel through the manifest-declared asset set.
    """
    if type(seal_bytes) is not bytes or len(seal_bytes) > _MAX_LOCAL_SEAL_BYTES:
        raise PluginInstallError("local_seal_invalid")
    name = _local_seal_store_name(manifest_sha256)
    temporary_name = f".{name}.{uuid4().hex}.part"
    try:
        storage.write_bytes(temporary_name, seal_bytes)
        storage.replace(temporary_name, name)
    except BaseException as active:
        try:
            storage.unlink(temporary_name, missing_ok=True)
        except BaseException:
            if not isinstance(active, Exception):
                raise active
            raise
        raise
    else:
        storage.unlink(temporary_name, missing_ok=True)


def _delete_local_seal_store_entry(storage: PinnedDirectory, manifest_sha256: str) -> None:
    storage.unlink(_local_seal_store_name(manifest_sha256), missing_ok=True)


def read_local_seal_document(state_root: Path, manifest_sha256: str) -> dict[str, object] | None:
    """Read one durably stored local seal document, or ``None`` if absent/unreadable.

    The one caller outside this module (the plugin lifecycle's combined
    package verifier, dispatching between the Ed25519 and locally-sealed
    verification paths) never treats ``None`` as anything but "no seal on
    file" -- it always fails the reverification closed rather than falling
    back to a weaker check.
    """
    try:
        name = _local_seal_store_name(manifest_sha256)
    except ValueError:
        return None
    try:
        with PinnedDirectory.open(Path(state_root)) as storage:
            if not storage.exists(name):
                return None
            payload = storage.read_bytes(name, limit=_MAX_LOCAL_SEAL_BYTES)
    except (ValueError, OSError, RuntimeError):
        return None
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        if type(document) is not dict or canonical_bytes(document) != payload:
            return None
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return document


def delete_local_seal_document(state_root: Path, manifest_sha256: str) -> None:
    """Best-effort removal of one durably stored local seal document.

    Uninstall calls this after the ordinary uninstall path has already
    removed the registry row and package tree: a failure to remove the seal
    here leaves at most an orphaned, already-inert file keyed by a manifest
    digest nothing installed will ever look up again -- never a correctness
    or security issue, so this never raises.
    """
    try:
        name = _local_seal_store_name(manifest_sha256)
    except ValueError:
        return
    try:
        with PinnedDirectory.open(Path(state_root)) as storage:
            storage.unlink(name, missing_ok=True)
    except (ValueError, OSError, RuntimeError):
        pass


def _discard_scratch_root(root: Path) -> None:
    """Best-effort removal of a throwaway sealing scratch tree.

    Nothing durable is ever recorded from this path alone -- it exists only
    between ``materialize_sealed_package`` and the moment its bytes are fully
    read into memory, so a failure to remove it leaves at most an orphaned
    directory of already-public (unsigned-by-Omega, device-sealed) bytes, never
    a security or consistency issue for the registry itself.
    """
    try:
        shutil.rmtree(root, ignore_errors=True)
    except OSError:
        pass
