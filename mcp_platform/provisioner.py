"""Durable transaction-bound provisioning for dormant MCP app dependencies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import paths as core_paths
from mcp_platform.app_registry import (
    InstalledMcpAppRecord,
    McpAppRegistry,
    McpAppRegistryConflict,
    _record_from_durable,
)
from mcp_platform.app_sources import (
    McpAppSourceResolver,
    TrustedRemoteAppSource,
    VerifiedLocalAppSource,
)
from mcp_platform.canonical import canonical_bytes
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.registry import PackageRegistry
from mcp_platform.state_store import McpStateStore
from plugin_platform.contracts import AppDeployment, AppRequirement, parse_app_id
from plugin_platform.installer import DependencyCommitReceipt, PreparedDependencySet
from plugin_platform.package_verifier import validate_verified_app_requirement


_DOCUMENT_FIELDS = frozenset({"schema_version", "transactions"})
_TRANSACTION_FIELDS = frozenset(
    {
        "transaction_id",
        "state",
        "prepared_id",
        "receipt_id",
        "base_revision",
        "requirements",
        "records",
        "introduced_app_ids",
        "remaining_introduced_app_ids",
        "committed_revision",
        "candidate",
        "previous_records",
        "candidate_revision",
    }
)
_LEGACY_TRANSACTION_FIELDS = _TRANSACTION_FIELDS - {
    "candidate",
    "previous_records",
    "candidate_revision",
}
_STATES = frozenset(
    {
        "preparing",
        "prepared",
        "committing",
        "committed",
        "candidate_promoted",
        "rolling_back",
        "rolled_back",
    }
)
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024


class McpAppProvisionError(RuntimeError):
    """Stable content-free app provisioning failure."""


class McpAppProvisioner:
    """Prepare and atomically publish signed dormant app declarations."""

    def __init__(
        self,
        *,
        app_registry: McpAppRegistry,
        package_registry: PackageRegistry,
        source_resolver: McpAppSourceResolver,
    ) -> None:
        if (
            type(app_registry) is not McpAppRegistry
            or type(package_registry) is not PackageRegistry
        ):
            raise ValueError("mcp_app_provisioner_invalid")
        if Path(app_registry.state_root) != Path(package_registry.state_root):
            raise ValueError("mcp_app_provisioner_invalid")
        if not isinstance(source_resolver, McpAppSourceResolver):
            raise ValueError("mcp_app_provisioner_invalid")
        self.app_registry = app_registry
        self.package_registry = package_registry
        self.source_resolver = source_resolver
        self.state_root = Path(app_registry.state_root)
        self.journal_path = self.state_root / core_paths.MCP_APP_PROVISION_JOURNAL_PATH.name
        self._store = McpStateStore(
            state_root=self.state_root,
            document_path=self.journal_path,
            invalid_path_reason="mcp_app_provision_state_invalid",
        )
        self._prepared_records: dict[str, tuple[InstalledMcpAppRecord, ...]] = {}

    def prepare(
        self,
        transaction_id: str,
        requirements: tuple[AppRequirement, ...],
    ) -> PreparedDependencySet:
        return self._prepare(transaction_id, requirements, candidate=False)

    def prepare_candidate(
        self,
        transaction_id: str,
        requirements: tuple[AppRequirement, ...],
    ) -> PreparedDependencySet:
        """Prepare exact signed candidate records without replacing live records."""

        return self._prepare(transaction_id, requirements, candidate=True)

    def _prepare(
        self,
        transaction_id: str,
        requirements: tuple[AppRequirement, ...],
        *,
        candidate: bool,
    ) -> PreparedDependencySet:
        transaction_id = _transaction_id(transaction_id)
        requirements = _requirements(requirements)
        snapshots = tuple(
            project_declared_capabilities(requirement).snapshot for requirement in requirements
        )
        with self._store.locked():
            document = self._read_document()
            existing_transaction = document["transactions"].get(transaction_id)
            if existing_transaction is not None:
                transaction = _transaction(existing_transaction, key=transaction_id)
                if transaction["requirements"] != [
                    _requirement_document(item) for item in requirements
                ] or transaction["candidate"] is not candidate:
                    raise McpAppProvisionError("mcp_app_provision_replay")
                if transaction["state"] == "prepared" and transaction_id in self._prepared_records:
                    return _prepared(transaction_id, snapshots)
                if transaction["state"] != "preparing":
                    raise McpAppProvisionError("mcp_app_provision_replay")
            else:
                registry = self._registry_snapshot()
                transaction = _new_transaction(
                    transaction_id,
                    requirements,
                    registry.revision,
                    candidate=candidate,
                )
                document["transactions"][transaction_id] = transaction
                self._write_document(document)
            try:
                registry = self._registry_snapshot()
                existing = {record.app_id: record for record in registry.records}
                selected: list[InstalledMcpAppRecord] = []
                introduced: list[str] = []
                previous: list[InstalledMcpAppRecord] = []
                for requirement in requirements:
                    record = existing.get(requirement.app_id)
                    if record is not None:
                        if not _record_matches_requirement(record, requirement):
                            if not candidate:
                                raise McpAppProvisionError("mcp_app_provision_source_invalid")
                            replacement = self._resolve_record(requirement)
                            selected.append(replacement)
                            previous.append(record)
                            continue
                        if requirement.deployment is AppDeployment.LOCAL:
                            verified = InstalledMcpAppRecord.from_source(
                                VerifiedLocalAppSource(
                                    requirement=requirement,
                                    registry=self.package_registry,
                                )
                            )
                            if verified != record:
                                raise McpAppProvisionError("mcp_app_provision_source_invalid")
                        selected.append(record)
                        continue
                    record = self._resolve_record(requirement)
                    selected.append(record)
                    introduced.append(record.app_id)
                transaction["state"] = "prepared"
                transaction["records"] = [
                    record.to_dict() for record in sorted(selected, key=lambda item: item.app_id)
                ]
                transaction["introduced_app_ids"] = sorted(introduced)
                transaction["remaining_introduced_app_ids"] = sorted(introduced)
                transaction["previous_records"] = [
                    record.to_dict() for record in sorted(previous, key=lambda item: item.app_id)
                ]
                _set_transaction(document, transaction)
                self._write_document(document)
                records = tuple(sorted(selected, key=lambda item: item.app_id))
                self._prepared_records[transaction_id] = records
            except BaseException as active:
                self._remove_transaction_after_failure(document, transaction_id, active)
                if not isinstance(active, Exception):
                    raise active
                if isinstance(active, McpAppProvisionError):
                    raise
                raise McpAppProvisionError("mcp_app_provision_source_unavailable") from None
        return _prepared(transaction_id, snapshots)

    def abort(self, transaction_id: str) -> None:
        transaction_id = _transaction_id(transaction_id)
        with self._store.locked():
            document = self._read_document()
            raw = document["transactions"].get(transaction_id)
            if raw is None:
                self._prepared_records.pop(transaction_id, None)
                return
            transaction = _transaction(raw, key=transaction_id)
            if transaction["state"] not in {"preparing", "prepared"}:
                raise McpAppProvisionError("mcp_app_provision_abort_invalid")
            del document["transactions"][transaction_id]
            try:
                self._write_document(document)
            except BaseException as active:
                if not isinstance(active, Exception):
                    raise active
                observed = self._read_document()
                if transaction_id in observed["transactions"]:
                    raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
            self._prepared_records.pop(transaction_id, None)

    def retire_absent_app_ids(self, app_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Drop journal ownership of app records that are already gone.

        Uninstall removes an app record through the registry's exact CAS rather
        than through :meth:`rollback`, so the transaction that introduced it
        stays ``committed`` and keeps naming it. That is not cosmetic. A
        committed transaction's requirements are exactly what
        ``_protected_app_ids`` protects, so the stale entry makes that app
        unremovable by the NEXT uninstall. Worse, a plugin re-released at the
        same version leaves TWO committed transactions naming one app with
        different records, and :meth:`rollback` refuses that on purpose --
        ``mcp_app_provision_recovery_required`` -- because two rollbacks
        disagreeing about what they would delete must stop rather than guess.
        Observed live on 2026-09-07 after a Slack uninstall.

        Only ids genuinely absent from the registry are retired, so a live
        record can never be orphaned. A transaction still owning other apps
        keeps its ``committed`` state and their protection; it is retired only
        once nothing it introduced is left.

        A transaction that ends up owning nothing is DELETED rather than left
        as a ``rolled_back`` shell. The journal exists so an interrupted
        install or rollback can be completed, not as an audit log -- ``abort``
        already deletes on the same reasoning -- and a shell that still names
        the app is what lets two installs of one plugin end up describing it
        differently. Deleting also bounds the journal across repeated
        install/uninstall cycles instead of growing it forever.

        Returns the transaction ids that changed.
        """
        requested = frozenset(_app_id(item) for item in app_ids)
        if not requested:
            return ()
        with self._store.locked():
            registry = self._registry_snapshot()
            absent = requested - {record.app_id for record in registry.records}
            if not absent:
                return ()
            document = self._read_document()
            changed: list[str] = []
            discarded: list[str] = []
            for transaction_id, raw in document["transactions"].items():
                transaction = _transaction(raw, key=transaction_id)
                # A transaction still being prepared belongs to an installer in
                # flight; abort/commit owns it, never this.
                if transaction["state"] in {"preparing", "prepared"}:
                    continue
                remaining = set(transaction["remaining_introduced_app_ids"])
                if not remaining & absent:
                    continue
                remaining -= absent
                if remaining:
                    transaction["remaining_introduced_app_ids"] = sorted(remaining)
                    _set_transaction(document, transaction)
                else:
                    discarded.append(transaction_id)
                changed.append(transaction_id)
            for transaction_id in discarded:
                del document["transactions"][transaction_id]
                self._prepared_records.pop(transaction_id, None)
            if changed:
                self._write_document(document)
            return tuple(sorted(changed))

    def commit(self, prepared: PreparedDependencySet) -> DependencyCommitReceipt:
        prepared = _copy_prepared(prepared)
        with self._store.locked():
            document = self._read_document()
            raw = document["transactions"].get(prepared.transaction_id)
            if raw is None:
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid")
            transaction = _transaction(raw, key=prepared.transaction_id)
            _validate_prepared_against_transaction(prepared, transaction)
            introduced = tuple(transaction["introduced_app_ids"])
            if transaction["state"] == "committed":
                if not self._records_are_exact(transaction):
                    raise McpAppProvisionError("mcp_app_provision_recovery_required")
                return _receipt(transaction)
            if transaction["state"] not in {"prepared", "committing"}:
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid")
            records = self._prepared_records.get(prepared.transaction_id)
            if records is None:
                if transaction["state"] == "committing" and self._records_are_exact(transaction):
                    self._mark_committed(document, transaction)
                    return _receipt(transaction)
                raise McpAppProvisionError("mcp_app_provision_recovery_required")
            install_by_id = {record.app_id: record for record in records}
            installs = tuple(install_by_id[app_id] for app_id in introduced)
            if transaction["state"] == "prepared":
                transaction["state"] = "committing"
                _set_transaction(document, transaction)
                self._write_document(document)
            try:
                result = self.app_registry.apply_exact(
                    install_records=installs,
                    remove_records=(),
                    expected_revision=transaction["base_revision"],
                )
            except BaseException as active:
                if isinstance(active, McpAppRegistryConflict):
                    transaction["state"] = "prepared"
                    _set_transaction(document, transaction)
                    try:
                        self._write_document(document)
                    except BaseException as cleanup_error:
                        if not isinstance(cleanup_error, Exception):
                            raise
                        raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
                    raise McpAppProvisionError("mcp_app_provision_registry_stale") from None
                try:
                    records_are_exact = self._records_are_exact(transaction)
                except BaseException as reconciliation_error:
                    if not isinstance(active, Exception):
                        raise active
                    if not isinstance(reconciliation_error, Exception):
                        raise
                    raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
                if records_are_exact:
                    try:
                        self._mark_committed(document, transaction)
                    except BaseException as cleanup_error:
                        if not isinstance(active, Exception):
                            raise active
                        if not isinstance(cleanup_error, Exception):
                            raise
                        raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
                    if not isinstance(active, Exception):
                        raise active
                    return _receipt(transaction)
                if not isinstance(active, Exception):
                    raise active
                raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
            transaction["state"] = "committed"
            transaction["committed_revision"] = result.revision
            _set_transaction(document, transaction)
            try:
                self._write_document(document)
            except BaseException as active:
                if not isinstance(active, Exception):
                    raise active
                raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
            return _receipt(transaction)

    def candidate_record(self, receipt_id: str, app_id: str) -> InstalledMcpAppRecord:
        """Return one exact durable candidate record bound to a provision receipt."""

        receipt_id = _safe_id(receipt_id)
        app_id = _app_id(app_id)
        with self._store.locked():
            document = self._read_document()
            matches = [
                _transaction(raw, key=transaction_id)
                for transaction_id, raw in document["transactions"].items()
                if type(raw) is dict and raw.get("receipt_id") == receipt_id
            ]
            if len(matches) != 1:
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid")
            transaction = matches[0]
            if not transaction["candidate"] or transaction["state"] not in {
                "committed",
                "candidate_promoted",
            }:
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid")
            records = _record_documents(transaction)
            try:
                raw = records[app_id]
                return _record_from_durable(raw, registry_key=app_id)
            except (KeyError, TypeError, ValueError):
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid") from None

    def promote_candidate_records(
        self,
        receipt_id: str,
        *,
        expected_revision: str,
    ) -> str:
        """Replace changed candidate app records through one exact registry CAS."""

        receipt_id = _safe_id(receipt_id)
        expected_revision = _revision(expected_revision)
        with self._store.locked():
            document = self._read_document()
            matches = [
                _transaction(raw, key=transaction_id)
                for transaction_id, raw in document["transactions"].items()
                if type(raw) is dict and raw.get("receipt_id") == receipt_id
            ]
            if len(matches) != 1:
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid")
            transaction = matches[0]
            if not transaction["candidate"]:
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid")
            candidate = _record_documents(transaction)
            previous = _previous_record_documents(transaction)
            registry = self._registry_snapshot()
            current = {record.app_id: record for record in registry.records}
            if transaction["state"] == "candidate_promoted":
                if any(current.get(app_id).to_dict() != raw for app_id, raw in candidate.items()):
                    raise McpAppProvisionError("mcp_app_provision_recovery_required")
                return registry.revision
            if transaction["state"] != "committed" or registry.revision != expected_revision:
                raise McpAppProvisionError("mcp_app_provision_registry_stale")
            old_records = tuple(
                _record_from_durable(raw, registry_key=app_id)
                for app_id, raw in sorted(previous.items())
            )
            new_records = tuple(
                _record_from_durable(candidate[app_id], registry_key=app_id)
                for app_id in sorted(previous)
            )
            try:
                result = self.app_registry.replace_many_exact(
                    installed_records=old_records,
                    replacement_records=new_records,
                    expected_revision=registry.revision,
                )
            except McpAppRegistryConflict:
                raise McpAppProvisionError("mcp_app_provision_registry_stale") from None
            transaction["state"] = "candidate_promoted"
            transaction["candidate_revision"] = result.revision
            _set_transaction(document, transaction)
            self._write_document(document)
            return result.revision

    def rollback(self, receipt_id: str) -> None:
        receipt_id = _safe_id(receipt_id)
        with self._store.locked():
            document = self._read_document()
            matches = [
                _transaction(raw, key=transaction_id)
                for transaction_id, raw in document["transactions"].items()
                if type(raw) is dict and raw.get("receipt_id") == receipt_id
            ]
            if len(matches) != 1:
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid")
            transaction = matches[0]
            if transaction["state"] not in {
                "prepared",
                "committing",
                "committed",
                "candidate_promoted",
                "rolling_back",
                "rolled_back",
            }:
                raise McpAppProvisionError("mcp_app_provision_receipt_invalid")
            previous_state = transaction["state"]
            if previous_state != "rolled_back":
                if previous_state == "prepared":
                    transaction["remaining_introduced_app_ids"] = []
                transaction["state"] = "rolling_back"
                _set_transaction(document, transaction)
                self._write_document(document)
            protected = _protected_app_ids(document)
            if transaction["candidate"]:
                self._rollback_candidate_records(document, transaction, protected)
                return
            owners = [
                _transaction(raw, key=transaction_id)
                for transaction_id, raw in document["transactions"].items()
                if type(raw) is dict and raw.get("state") in {"rolling_back", "rolled_back"}
            ]
            expected: dict[str, dict[str, object]] = {}
            candidates: set[str] = set()
            for owner in owners:
                owner_records = _record_documents(owner)
                for app_id in set(owner["remaining_introduced_app_ids"]) - protected:
                    expected_record = owner_records[app_id]
                    if app_id in expected and expected[app_id] != expected_record:
                        raise McpAppProvisionError("mcp_app_provision_recovery_required")
                    expected[app_id] = expected_record
                    candidates.add(app_id)
            registry = self._registry_snapshot()
            current = {record.app_id: record for record in registry.records}
            removals: list[InstalledMcpAppRecord] = []
            for app_id in sorted(candidates):
                record = current.get(app_id)
                if record is None:
                    continue
                if record.to_dict() != expected[app_id]:
                    raise McpAppProvisionError("mcp_app_provision_recovery_required")
                removals.append(record)
            try:
                self.app_registry.apply_exact(
                    install_records=(),
                    remove_records=tuple(removals),
                    expected_revision=registry.revision,
                )
            except BaseException as active:
                if not isinstance(active, Exception):
                    raise active
                raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
            for owner in owners:
                owner["state"] = "rolled_back"
                owner["remaining_introduced_app_ids"] = sorted(
                    set(owner["remaining_introduced_app_ids"]) & protected
                )
                owner["committed_revision"] = None
                _set_transaction(document, owner)
            try:
                self._write_document(document)
            except BaseException as active:
                if not isinstance(active, Exception):
                    raise active
                raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
            self._prepared_records.pop(transaction["transaction_id"], None)

    def _rollback_candidate_records(
        self,
        document: dict[str, Any],
        transaction: dict[str, Any],
        protected: set[str],
    ) -> None:
        candidate = _record_documents(transaction)
        previous = _previous_record_documents(transaction)
        registry = self._registry_snapshot()
        current = {record.app_id: record for record in registry.records}
        replacements: list[InstalledMcpAppRecord] = []
        restored: list[InstalledMcpAppRecord] = []
        for app_id, raw in sorted(previous.items()):
            old = _record_from_durable(raw, registry_key=app_id)
            target = _record_from_durable(candidate[app_id], registry_key=app_id)
            observed = current.get(app_id)
            if observed == old:
                continue
            if observed != target:
                raise McpAppProvisionError("mcp_app_provision_recovery_required")
            replacements.append(target)
            restored.append(old)
        if replacements:
            try:
                registry = self.app_registry.replace_many_exact(
                    installed_records=tuple(replacements),
                    replacement_records=tuple(restored),
                    expected_revision=registry.revision,
                )
            except Exception:
                raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
            current = {record.app_id: record for record in registry.records}
        removals: list[InstalledMcpAppRecord] = []
        for app_id in transaction["introduced_app_ids"]:
            if app_id in protected:
                continue
            observed = current.get(app_id)
            if observed is None:
                continue
            expected = _record_from_durable(candidate[app_id], registry_key=app_id)
            if observed != expected:
                raise McpAppProvisionError("mcp_app_provision_recovery_required")
            removals.append(observed)
        if removals:
            try:
                self.app_registry.apply_exact(
                    install_records=(),
                    remove_records=tuple(removals),
                    expected_revision=registry.revision,
                )
            except Exception:
                raise McpAppProvisionError("mcp_app_provision_recovery_required") from None
        transaction["state"] = "rolled_back"
        transaction["remaining_introduced_app_ids"] = sorted(
            set(transaction["introduced_app_ids"]) & protected
        )
        transaction["committed_revision"] = None
        transaction["candidate_revision"] = None
        _set_transaction(document, transaction)
        self._write_document(document)
        self._prepared_records.pop(transaction["transaction_id"], None)

    def _resolve_record(self, requirement: AppRequirement) -> InstalledMcpAppRecord:
        try:
            source = self.source_resolver.resolve(requirement)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise McpAppProvisionError("mcp_app_provision_source_unavailable") from None
        try:
            if requirement.deployment is AppDeployment.LOCAL:
                if type(source) is not VerifiedLocalAppSource:
                    raise ValueError
                returned = InstalledMcpAppRecord.from_source(source)
                record = InstalledMcpAppRecord.from_source(
                    VerifiedLocalAppSource(requirement=requirement, registry=self.package_registry)
                )
                if returned != record:
                    raise ValueError
            else:
                if type(source) is not TrustedRemoteAppSource:
                    raise ValueError
                record = InstalledMcpAppRecord.from_source(source)
            if not _record_matches_requirement(record, requirement):
                raise ValueError
            return record
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise McpAppProvisionError("mcp_app_provision_source_invalid") from None

    def _registry_snapshot(self):
        try:
            return self.app_registry.snapshot()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise McpAppProvisionError("mcp_app_provision_registry_unavailable") from None

    def _records_are_exact(self, transaction: dict[str, Any]) -> bool:
        snapshot = self._registry_snapshot()
        current = {record.app_id: record.to_dict() for record in snapshot.records}
        expected = _record_documents(transaction)
        previous = _previous_record_documents(transaction)
        return all(
            current.get(app_id) == (previous.get(app_id) or record)
            for app_id, record in expected.items()
        )

    def _mark_committed(self, document: dict[str, Any], transaction: dict[str, Any]) -> None:
        transaction["state"] = "committed"
        transaction["committed_revision"] = self._registry_snapshot().revision
        _set_transaction(document, transaction)
        self._write_document(document)

    def _remove_transaction_after_failure(
        self,
        document: dict[str, Any],
        transaction_id: str,
        active: BaseException,
    ) -> None:
        document["transactions"].pop(transaction_id, None)
        self._prepared_records.pop(transaction_id, None)
        try:
            self._write_document(document)
        except BaseException as cleanup_error:
            if not isinstance(active, Exception):
                raise active
            if not isinstance(cleanup_error, Exception):
                raise
            raise McpAppProvisionError("mcp_app_provision_recovery_required") from None

    def _read_document(self) -> dict[str, Any]:
        try:
            payload = self._store.read_bytes()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise McpAppProvisionError("mcp_app_provision_state_invalid") from None
        if payload is None:
            return {"schema_version": 1, "transactions": {}}
        try:
            if len(payload) > _MAX_JOURNAL_BYTES:
                raise ValueError
            parsed = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
            if canonical_bytes(parsed) != payload:
                raise ValueError
            return _validated_document(parsed)
        except McpAppProvisionError:
            raise
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise McpAppProvisionError("mcp_app_provision_state_invalid") from None

    def _write_document(self, document: dict[str, Any]) -> None:
        payload = canonical_bytes(_validated_document(document))
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise McpAppProvisionError("mcp_app_provision_state_invalid")
        try:
            self._store.write_bytes(payload)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise McpAppProvisionError("mcp_app_provision_recovery_required") from None


def _new_transaction(
    transaction_id: str,
    requirements: tuple[AppRequirement, ...],
    revision: str,
    *,
    candidate: bool = False,
) -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "state": "preparing",
        "prepared_id": f"mcp-prepared-{transaction_id}",
        "receipt_id": f"mcp-receipt-{transaction_id}",
        "base_revision": _revision(revision),
        "requirements": [_requirement_document(item) for item in requirements],
        "records": [],
        "introduced_app_ids": [],
        "remaining_introduced_app_ids": [],
        "committed_revision": None,
        "candidate": candidate,
        "previous_records": [],
        "candidate_revision": None,
    }


def _validated_document(document: object) -> dict[str, Any]:
    try:
        data = _exact_object(document, _DOCUMENT_FIELDS)
        if data["schema_version"] != 1 or type(data["transactions"]) is not dict:
            raise ValueError
        transactions = {
            _transaction_id(key): _transaction(raw, key=key)
            for key, raw in data["transactions"].items()
        }
        return {"schema_version": 1, "transactions": transactions}
    except McpAppProvisionError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise McpAppProvisionError("mcp_app_provision_state_invalid") from None


def _transaction(raw: object, *, key: str) -> dict[str, Any]:
    if type(raw) is dict and set(raw) == _LEGACY_TRANSACTION_FIELDS:
        raw = {
            **raw,
            "candidate": False,
            "previous_records": [],
            "candidate_revision": None,
        }
    data = _exact_object(raw, _TRANSACTION_FIELDS)
    if _transaction_id(data["transaction_id"]) != key or data["state"] not in _STATES:
        raise ValueError
    if data["prepared_id"] != f"mcp-prepared-{key}":
        raise ValueError
    if data["receipt_id"] != f"mcp-receipt-{key}":
        raise ValueError
    _revision(data["base_revision"])
    if data["committed_revision"] is not None:
        _revision(data["committed_revision"])
    if data["candidate_revision"] is not None:
        _revision(data["candidate_revision"])
    if type(data["candidate"]) is not bool or type(data["previous_records"]) is not list:
        raise ValueError
    if type(data["requirements"]) is not list or type(data["records"]) is not list:
        raise ValueError
    requirements = [_canonical_requirement_document(item) for item in data["requirements"]]
    requirement_ids = [item["app_id"] for item in requirements]
    if requirement_ids != sorted(requirement_ids) or len(requirement_ids) != len(
        set(requirement_ids)
    ):
        raise ValueError
    records: list[dict[str, object]] = []
    for item in data["records"]:
        if type(item) is not dict or type(item.get("app_id")) is not str:
            raise ValueError
        records.append(InstalledMcpAppRecord.from_dict(item, registry_key=item["app_id"]).to_dict())
    record_ids = [item["app_id"] for item in records]
    if record_ids != sorted(record_ids) or len(record_ids) != len(set(record_ids)):
        raise ValueError
    introduced = _app_id_list(data["introduced_app_ids"])
    remaining = _app_id_list(data["remaining_introduced_app_ids"])
    previous_records: list[dict[str, object]] = []
    for item in data["previous_records"]:
        if type(item) is not dict or type(item.get("app_id")) is not str:
            raise ValueError
        previous_records.append(
            InstalledMcpAppRecord.from_dict(item, registry_key=item["app_id"]).to_dict()
        )
    previous_ids = [item["app_id"] for item in previous_records]
    if (
        previous_ids != sorted(previous_ids)
        or len(previous_ids) != len(set(previous_ids))
        or not set(previous_ids) <= set(record_ids)
        or set(previous_ids) & set(introduced)
        or (previous_ids and not data["candidate"])
    ):
        raise ValueError
    if not set(remaining) <= set(introduced) or not set(introduced) <= set(record_ids):
        raise ValueError
    state = data["state"]
    if state == "preparing" and any((records, introduced, remaining)):
        raise ValueError
    if state != "preparing" and record_ids != requirement_ids:
        raise ValueError
    if state in {"preparing", "prepared", "committing"} and data["committed_revision"] is not None:
        raise ValueError
    if state == "committed" and data["committed_revision"] is None:
        raise ValueError
    if state == "candidate_promoted" and (
        not data["candidate"]
        or data["committed_revision"] is None
        or data["candidate_revision"] is None
    ):
        raise ValueError
    if state not in {"candidate_promoted", "rolling_back"} and data[
        "candidate_revision"
    ] is not None:
        raise ValueError
    if state == "rolled_back" and data["committed_revision"] is not None:
        raise ValueError
    return {
        "transaction_id": key,
        "state": data["state"],
        "prepared_id": data["prepared_id"],
        "receipt_id": data["receipt_id"],
        "base_revision": data["base_revision"],
        "requirements": requirements,
        "records": records,
        "introduced_app_ids": introduced,
        "remaining_introduced_app_ids": remaining,
        "committed_revision": data["committed_revision"],
        "candidate": data["candidate"],
        "previous_records": previous_records,
        "candidate_revision": data["candidate_revision"],
    }


def _requirements(value: tuple[AppRequirement, ...]) -> tuple[AppRequirement, ...]:
    if type(value) is not tuple:
        raise ValueError("mcp_app_provision_requirements_invalid")
    try:
        requirements = tuple(validate_verified_app_requirement(item) for item in value)
    except (TypeError, ValueError):
        raise ValueError("mcp_app_provision_requirements_invalid") from None
    ordered = tuple(sorted(requirements, key=lambda item: item.app_id))
    if len({item.app_id for item in ordered}) != len(ordered):
        raise ValueError("mcp_app_provision_requirements_invalid")
    return ordered


def _canonical_requirement_document(value: object) -> dict[str, object]:
    return _requirement_document(AppRequirement.from_dict(value))


def _requirement_document(requirement: AppRequirement) -> dict[str, object]:
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
    return document


def _record_matches_requirement(
    record: InstalledMcpAppRecord,
    requirement: AppRequirement,
) -> bool:
    document = record.to_dict()
    observed = {
        key: document[key]
        for key in (
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
        )
    }
    observed["transports"] = sorted(observed["transports"])
    observed["capabilities"] = sorted(
        observed["capabilities"],
        key=lambda item: (
            item["kind"],
            item["name"],
            item["schema_sha256"] or "",
            item["version"] or "",
        ),
    )
    return observed == _requirement_document(requirement)


def _prepared(transaction_id: str, snapshots: tuple[Any, ...]) -> PreparedDependencySet:
    return PreparedDependencySet(
        transaction_id=transaction_id,
        prepared_id=f"mcp-prepared-{transaction_id}",
        receipt_id=f"mcp-receipt-{transaction_id}",
        app_snapshots=snapshots,
    )


def _copy_prepared(value: object) -> PreparedDependencySet:
    try:
        if type(value) is not PreparedDependencySet:
            raise ValueError
        return PreparedDependencySet(
            transaction_id=value.transaction_id,
            prepared_id=value.prepared_id,
            receipt_id=value.receipt_id,
            app_snapshots=tuple(value.app_snapshots),
        )
    except (AttributeError, TypeError, ValueError):
        raise McpAppProvisionError("mcp_app_provision_receipt_invalid") from None


def _validate_prepared_against_transaction(
    prepared: PreparedDependencySet,
    transaction: dict[str, Any],
) -> None:
    requirements = tuple(AppRequirement.from_dict(item) for item in transaction["requirements"])
    expected_snapshots = tuple(
        project_declared_capabilities(requirement).snapshot for requirement in requirements
    )
    if (
        prepared.transaction_id != transaction["transaction_id"]
        or prepared.prepared_id != transaction["prepared_id"]
        or prepared.receipt_id != transaction["receipt_id"]
        or prepared.app_snapshots != expected_snapshots
    ):
        raise McpAppProvisionError("mcp_app_provision_receipt_invalid")


def _receipt(transaction: dict[str, Any]) -> DependencyCommitReceipt:
    return DependencyCommitReceipt(
        receipt_id=transaction["receipt_id"],
        prepared_id=transaction["prepared_id"],
        introduced_app_ids=tuple(transaction["introduced_app_ids"]),
    )


def _record_documents(transaction: dict[str, Any]) -> dict[str, dict[str, object]]:
    return {item["app_id"]: item for item in transaction["records"]}


def _previous_record_documents(
    transaction: dict[str, Any],
) -> dict[str, dict[str, object]]:
    return {item["app_id"]: item for item in transaction["previous_records"]}


def _set_transaction(document: dict[str, Any], transaction: dict[str, Any]) -> None:
    document["transactions"][transaction["transaction_id"]] = transaction


def _protected_app_ids(document: dict[str, Any]) -> set[str]:
    protected: set[str] = set()
    for transaction_id, raw in document["transactions"].items():
        transaction = _transaction(raw, key=transaction_id)
        if transaction["state"] in {"committing", "committed"}:
            protected.update(item["app_id"] for item in transaction["requirements"])
    return protected


def _app_id_list(value: object) -> list[str]:
    if type(value) is not list:
        raise ValueError
    app_ids = [_app_id(item) for item in value]
    if app_ids != sorted(app_ids) or len(app_ids) != len(set(app_ids)):
        raise ValueError
    return app_ids


def _app_id(value: object) -> str:
    return parse_app_id(value)


def _transaction_id(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("mcp_app_provision_transaction_invalid")
    return value


def _safe_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or not value[0].isalnum()
        or not value[-1].isalnum()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in value)
    ):
        raise ValueError("mcp_app_provision_receipt_invalid")
    return value


def _revision(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError
    return value


def _exact_object(raw: object, fields: frozenset[str]) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != fields or any(type(key) is not str for key in raw):
        raise ValueError
    return raw


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError
