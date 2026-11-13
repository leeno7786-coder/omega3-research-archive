"""Atomic MCP app dependency provisioning tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from mcp_platform.app_registry import InstalledMcpAppRecord, McpAppRegistry
from mcp_platform.app_sources import TrustedRemoteAppSource, VerifiedLocalAppSource
from mcp_platform.canonical import canonical_bytes
from mcp_platform.package_verifier import VerifiedPackage, verify_package
from mcp_platform.provisioner import (
    McpAppProvisionError,
    McpAppProvisioner,
    _protected_app_ids,
)
from mcp_platform.registry import PackageRegistry
from mcp_platform.state_store import McpStateStore
from plugin_platform.contracts import AppRequirement
from plugin_platform.installer import PreparedDependencySet
from tests.mcp_platform.test_app_sources import (
    _build_current_signed_package,
    _requirement as local_requirement,
    _remote_requirement,
    _write_verified_plugin_requirement,
)
from tests.mcp_platform.test_package_verifier import ephemeral_ring, host_tag


_TRANSACTION_ID = "1" * 32


class RecordingRemoteResolver:
    def __init__(self, journal_path: Path) -> None:
        self.journal_path = journal_path
        self.documents: list[dict[str, object]] = []
        self.requirements: list[AppRequirement] = []

    def resolve(self, requirement: AppRequirement) -> TrustedRemoteAppSource:
        self.documents.append(json.loads(self.journal_path.read_bytes()))
        self.requirements.append(requirement)
        return TrustedRemoteAppSource(requirement=requirement)


class MappingRemoteResolver:
    def __init__(self, sources: dict[str, AppRequirement | BaseException]) -> None:
        self.sources = sources
        self.requests: list[str] = []

    def resolve(self, requirement: AppRequirement) -> TrustedRemoteAppSource:
        self.requests.append(requirement.app_id)
        source = self.sources[requirement.app_id]
        if isinstance(source, BaseException):
            raise source
        return TrustedRemoteAppSource(requirement=source)


def _signed_remote_requirement(tmp_path: Path) -> AppRequirement:
    return _write_verified_plugin_requirement(
        tmp_path / "signed-plugin",
        _remote_requirement(),
    )


def _remote_requirement_for(
    app_id: str,
    *,
    publisher_id: str = "example.publisher",
    provider_id: str = "workspace",
    protocol_version: str = "2026-07-28",
    transport: str = "streamable_http",
    origin: str = "https://workspace.example",
    issuer: str = "https://auth.example/tenant",
    capability_name: str = "list_items",
) -> AppRequirement:
    return AppRequirement.from_dict(
        {
            "app_id": app_id,
            "publisher_id": publisher_id,
            "deployment": "remote",
            "provider_id": provider_id,
            "protocol_version": protocol_version,
            "transports": [transport],
            "remote_origin": origin,
            "authorization_issuer": issuer,
            "authentication": {
                "kind": "oauth_authorization_code_pkce",
                "scopes": ["workspace.read"],
                "secret_fields": [],
            },
            "capabilities": [
                {
                    "kind": "tool",
                    "name": capability_name,
                    "schema_sha256": "2" * 64,
                    "version": None,
                }
            ],
        }
    )


def _signed_requirement(tmp_path: Path, name: str, requirement: AppRequirement) -> AppRequirement:
    return _write_verified_plugin_requirement(tmp_path / name, requirement)


def _journal(state_root: Path) -> dict[str, object]:
    return json.loads((state_root / "app_provision_journal.json").read_bytes())


def _provisioner(
    state_root: Path,
    resolver: object,
    *,
    package_registry: PackageRegistry | None = None,
) -> McpAppProvisioner:
    return McpAppProvisioner(
        app_registry=McpAppRegistry(state_root=state_root),
        package_registry=package_registry or PackageRegistry(state_root=state_root),
        source_resolver=resolver,  # type: ignore[arg-type]
    )


def test_prepare_predeclares_receipt_and_durably_precedes_source_resolution(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "mcp"
    journal_path = state_root / "app_provision_journal.json"
    resolver = RecordingRemoteResolver(journal_path)
    provisioner = McpAppProvisioner(
        app_registry=McpAppRegistry(state_root=state_root),
        package_registry=PackageRegistry(state_root=state_root),
        source_resolver=resolver,
    )
    requirement = _signed_remote_requirement(tmp_path)

    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))

    assert len(resolver.documents) == 1
    observed = resolver.documents[0]
    transaction = observed["transactions"][_TRANSACTION_ID]
    assert transaction["state"] == "preparing"
    assert transaction["receipt_id"] == prepared.receipt_id
    assert transaction["requirements"] == [requirement.to_dict()]
    assert prepared.transaction_id == _TRANSACTION_ID
    assert prepared.prepared_id == f"mcp-prepared-{_TRANSACTION_ID}"
    assert prepared.receipt_id == f"mcp-receipt-{_TRANSACTION_ID}"
    assert tuple(snapshot.app_id for snapshot in prepared.app_snapshots) == (requirement.app_id,)
    assert prepared.app_snapshots[0].installed is True
    assert prepared.app_snapshots[0].trusted is True
    assert prepared.app_snapshots[0].enabled is False


def test_prepare_normalizes_order_reuses_exact_dormant_app_and_commit_is_atomic(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "mcp"
    existing_requirement = _signed_requirement(
        tmp_path,
        "existing-plugin",
        _remote_requirement_for("example.remote.existing"),
    )
    missing_requirement = _signed_requirement(
        tmp_path,
        "missing-plugin",
        _remote_requirement_for("example.remote.missing"),
    )
    registry = McpAppRegistry(state_root=state_root)
    existing_record = registry.apply_exact(
        install_records=(
            __import__(
                "mcp_platform.app_registry", fromlist=["InstalledMcpAppRecord"]
            ).InstalledMcpAppRecord.from_source(
                TrustedRemoteAppSource(requirement=existing_requirement)
            ),
        ),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )
    resolver = MappingRemoteResolver({missing_requirement.app_id: missing_requirement})
    provisioner = McpAppProvisioner(
        app_registry=registry,
        package_registry=PackageRegistry(state_root=state_root),
        source_resolver=resolver,
    )

    prepared = provisioner.prepare(
        _TRANSACTION_ID,
        (missing_requirement, existing_requirement),
    )
    receipt = provisioner.commit(prepared)

    assert resolver.requests == [missing_requirement.app_id]
    assert tuple(item.app_id for item in prepared.app_snapshots) == (
        existing_requirement.app_id,
        missing_requirement.app_id,
    )
    assert receipt.receipt_id == prepared.receipt_id
    assert receipt.prepared_id == prepared.prepared_id
    assert receipt.introduced_app_ids == (missing_requirement.app_id,)
    records = provisioner.app_registry.snapshot().records
    assert tuple(item.app_id for item in records) == (
        existing_requirement.app_id,
        missing_requirement.app_id,
    )
    assert all(item.enabled is False for item in records)
    assert existing_record.records[0] == records[0]


def test_candidate_changed_record_stays_dormant_and_restart_rollback_restores_old(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "mcp"
    old_requirement = _signed_requirement(
        tmp_path,
        "old-plugin",
        _remote_requirement_for("example.remote.changed"),
    )
    changed_document = old_requirement.to_dict()
    changed_document["authentication"] = {
        "kind": "oauth_authorization_code_pkce",
        "scopes": ["workspace.admin", "workspace.read"],
        "secret_fields": [],
    }
    changed_requirement = _signed_requirement(
        tmp_path,
        "candidate-plugin",
        AppRequirement.from_dict(changed_document),
    )
    registry = McpAppRegistry(state_root=state_root)
    old_record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=old_requirement)
    )
    registry.apply_exact(
        install_records=(old_record,),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({changed_requirement.app_id: changed_requirement}),
    )

    prepared = provisioner.prepare_candidate(_TRANSACTION_ID, (changed_requirement,))
    receipt = provisioner.commit(prepared)
    candidate = provisioner.candidate_record(receipt.receipt_id, changed_requirement.app_id)

    assert candidate.authentication.scopes == ("workspace.admin", "workspace.read")
    assert registry.get(old_requirement.app_id) == old_record
    with pytest.raises(McpAppProvisionError, match="^mcp_app_provision_registry_stale$"):
        provisioner.promote_candidate_records(
            receipt.receipt_id,
            expected_revision="0" * 64,
        )
    assert registry.get(old_requirement.app_id) == old_record

    provisioner.promote_candidate_records(
        receipt.receipt_id,
        expected_revision=registry.snapshot().revision,
    )
    assert registry.get(old_requirement.app_id) == candidate

    restarted = _provisioner(state_root, MappingRemoteResolver({}))
    restarted.rollback(receipt.receipt_id)

    assert registry.get(old_requirement.app_id) == old_record


def test_abort_removes_only_uncommitted_prepared_transaction(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    provisioner.prepare(_TRANSACTION_ID, (requirement,))

    provisioner.abort(_TRANSACTION_ID)
    provisioner.abort(_TRANSACTION_ID)

    assert provisioner.app_registry.snapshot().records == ()
    assert _TRANSACTION_ID not in _journal(state_root)["transactions"]


def test_rollback_removes_exact_introduced_record_and_preserves_existing(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "mcp"
    existing = _signed_requirement(
        tmp_path,
        "existing",
        _remote_requirement_for("example.remote.existing"),
    )
    introduced = _signed_requirement(
        tmp_path,
        "introduced",
        _remote_requirement_for("example.remote.introduced"),
    )
    resolver = MappingRemoteResolver({existing.app_id: existing, introduced.app_id: introduced})
    provisioner = _provisioner(state_root, resolver)
    first = provisioner.prepare("2" * 32, (existing,))
    provisioner.commit(first)
    second = provisioner.prepare("3" * 32, (existing, introduced))
    receipt = provisioner.commit(second)

    provisioner.rollback(receipt.receipt_id)
    provisioner.rollback(receipt.receipt_id)

    assert tuple(item.app_id for item in provisioner.app_registry.snapshot().records) == (
        existing.app_id,
    )


def test_rollback_preserves_an_introduced_app_shared_by_another_committed_receipt(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    resolver = MappingRemoteResolver({requirement.app_id: requirement})
    provisioner = _provisioner(state_root, resolver)
    owner = provisioner.prepare("4" * 32, (requirement,))
    owner_receipt = provisioner.commit(owner)
    shared = provisioner.prepare("5" * 32, (requirement,))
    shared_receipt = provisioner.commit(shared)

    provisioner.rollback(owner_receipt.receipt_id)

    assert provisioner.app_registry.get(requirement.app_id).enabled is False
    assert shared_receipt.introduced_app_ids == ()


@pytest.mark.parametrize("owner_first", [True, False])
def test_shared_app_is_reclaimed_after_both_receipts_rollback_in_either_order(
    tmp_path: Path,
    owner_first: bool,
) -> None:
    """Catches terminal owner evidence stranding an app after its final consumer exits."""
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    owner = provisioner.prepare("4" * 32, (requirement,))
    owner_receipt = provisioner.commit(owner)
    shared = provisioner.prepare("5" * 32, (requirement,))
    shared_receipt = provisioner.commit(shared)
    first, second = (
        (owner_receipt, shared_receipt) if owner_first else (shared_receipt, owner_receipt)
    )

    provisioner.rollback(first.receipt_id)
    assert provisioner.app_registry.get(requirement.app_id).enabled is False
    provisioner.rollback(second.receipt_id)

    assert provisioner.app_registry.snapshot().records == ()
    provisioner.rollback(first.receipt_id)
    provisioner.rollback(second.receipt_id)
    assert provisioner.app_registry.snapshot().records == ()


def test_commit_rejects_cross_transaction_receipt_replay_and_stale_revision(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    crossed = PreparedDependencySet(
        transaction_id="6" * 32,
        prepared_id=prepared.prepared_id,
        receipt_id=prepared.receipt_id,
        app_snapshots=prepared.app_snapshots,
    )
    with pytest.raises(McpAppProvisionError, match="^mcp_app_provision_receipt_invalid$"):
        provisioner.commit(crossed)

    unrelated = _signed_requirement(
        tmp_path,
        "unrelated",
        _remote_requirement_for("example.remote.unrelated"),
    )
    record_type = __import__(
        "mcp_platform.app_registry", fromlist=["InstalledMcpAppRecord"]
    ).InstalledMcpAppRecord
    registry = provisioner.app_registry
    registry.apply_exact(
        install_records=(record_type.from_source(TrustedRemoteAppSource(requirement=unrelated)),),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )
    with pytest.raises(McpAppProvisionError, match="^mcp_app_provision_registry_stale$"):
        provisioner.commit(prepared)
    assert _journal(state_root)["transactions"][_TRANSACTION_ID]["state"] == "prepared"


def test_commit_requires_the_exact_predeclared_receipt_for_the_same_transaction(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    wrong_receipt = PreparedDependencySet(
        transaction_id=prepared.transaction_id,
        prepared_id=prepared.prepared_id,
        receipt_id=f"mcp-receipt-{'6' * 32}",
        app_snapshots=prepared.app_snapshots,
    )

    with pytest.raises(McpAppProvisionError, match="^mcp_app_provision_receipt_invalid$"):
        provisioner.commit(wrong_receipt)


@pytest.mark.parametrize(
    "changed",
    [
        {"publisher_id": "other.publisher"},
        {"provider_id": "other"},
        {"origin": "https://other.example"},
        {"issuer": "https://auth.other.example/tenant"},
        {"capability_name": "other_tool"},
    ],
)
def test_prepare_rejects_conflicting_signed_remote_sources(
    tmp_path: Path,
    changed: dict[str, str],
) -> None:
    requested = _signed_requirement(
        tmp_path,
        "requested",
        _remote_requirement_for("example.remote.requested"),
    )
    conflicting = _signed_requirement(
        tmp_path,
        "conflicting",
        _remote_requirement_for("example.remote.requested", **changed),
    )
    provisioner = _provisioner(
        tmp_path / "mcp",
        MappingRemoteResolver({requested.app_id: conflicting}),
    )

    with pytest.raises(McpAppProvisionError, match="^mcp_app_provision_source_invalid$"):
        provisioner.prepare(_TRANSACTION_ID, (requested,))


def test_prepare_rejects_duplicate_untrusted_unavailable_and_wrong_source_kind(
    tmp_path: Path,
) -> None:
    signed = _signed_remote_requirement(tmp_path)
    with pytest.raises(ValueError, match="^mcp_app_provision_requirements_invalid$"):
        _provisioner(
            tmp_path / "duplicate",
            MappingRemoteResolver({signed.app_id: signed}),
        ).prepare(_TRANSACTION_ID, (signed, signed))

    nominal = _remote_requirement()
    with pytest.raises(ValueError, match="^mcp_app_provision_requirements_invalid$"):
        _provisioner(
            tmp_path / "untrusted",
            MappingRemoteResolver({nominal.app_id: nominal}),
        ).prepare(_TRANSACTION_ID, (nominal,))

    unavailable = _provisioner(
        tmp_path / "unavailable",
        MappingRemoteResolver({signed.app_id: RuntimeError("secret endpoint")}),
    )
    with pytest.raises(McpAppProvisionError, match="^mcp_app_provision_source_unavailable$"):
        unavailable.prepare(_TRANSACTION_ID, (signed,))


def test_missing_local_app_uses_only_verified_signed_package_source(tmp_path: Path) -> None:
    state_root = tmp_path / "mcp"
    private, ring = ephemeral_ring()
    package_parent = tmp_path / "package-source"
    package_parent.mkdir()
    package_dir = _build_current_signed_package(
        package_parent,
        private,
        signing_key_id="test-release",
    )

    def verifier(path: Path) -> VerifiedPackage:
        return verify_package(
            path,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )

    package_registry = PackageRegistry(state_root=state_root, verifier=verifier)
    verified = verifier(package_dir)
    requirement = _signed_requirement(
        tmp_path,
        "local-plugin",
        local_requirement(verified),
    )

    class LocalResolver:
        def resolve(self, requested: AppRequirement) -> VerifiedLocalAppSource:
            package_registry.install(package_dir)
            return VerifiedLocalAppSource(requirement=requested, registry=package_registry)

    provisioner = _provisioner(
        state_root,
        LocalResolver(),
        package_registry=package_registry,
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))

    assert package_registry.get(requirement.app_id).enabled is False
    assert prepared.app_snapshots[0].deployment.value == "local"
    assert provisioner.app_registry.snapshot().records == ()


class Marker(BaseException):
    pass


def test_ambiguous_registry_commit_reconciles_and_baseexception_replay_keeps_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    original = provisioner.app_registry.apply_exact

    def applied_then_failed(**kwargs: object):
        result = original(**kwargs)  # type: ignore[arg-type]
        raise RuntimeError("registry secret")

    monkeypatch.setattr(provisioner.app_registry, "apply_exact", applied_then_failed)
    receipt = provisioner.commit(prepared)
    assert receipt.introduced_app_ids == (requirement.app_id,)
    assert _journal(state_root)["transactions"][_TRANSACTION_ID]["state"] == "committed"

    second_root = tmp_path / "second-mcp"
    second = _provisioner(
        second_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    second_prepared = second.prepare("7" * 32, (requirement,))
    second_original = second.app_registry.apply_exact
    marker = Marker()

    def applied_then_cancelled(**kwargs: object):
        second_original(**kwargs)  # type: ignore[arg-type]
        raise marker

    monkeypatch.setattr(second.app_registry, "apply_exact", applied_then_cancelled)
    with pytest.raises(Marker) as caught:
        second.commit(second_prepared)
    assert caught.value is marker
    monkeypatch.setattr(second.app_registry, "apply_exact", second_original)
    replay = second.commit(second_prepared)
    assert replay.introduced_app_ids == (requirement.app_id,)


def test_committed_replay_rejects_drift_of_a_reused_dependency(tmp_path: Path) -> None:
    """Catches reconciliation checking introduced records but ignoring reused identity."""
    state_root = tmp_path / "mcp"
    existing = _signed_requirement(
        tmp_path,
        "existing",
        _remote_requirement_for("example.remote.existing"),
    )
    introduced = _signed_requirement(
        tmp_path,
        "introduced",
        _remote_requirement_for("example.remote.introduced"),
    )
    registry = McpAppRegistry(state_root=state_root)
    existing_record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=existing)
    )
    registry.apply_exact(
        install_records=(existing_record,),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )
    provisioner = McpAppProvisioner(
        app_registry=registry,
        package_registry=PackageRegistry(state_root=state_root),
        source_resolver=MappingRemoteResolver({introduced.app_id: introduced}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (existing, introduced))
    provisioner.commit(prepared)
    current = registry.snapshot()
    registry.apply_exact(
        install_records=(),
        remove_records=(existing_record,),
        expected_revision=current.revision,
    )

    with pytest.raises(
        McpAppProvisionError,
        match="^mcp_app_provision_recovery_required$",
    ):
        provisioner.commit(prepared)


def test_rollback_of_never_committing_prepare_preserves_an_external_exact_install(
    tmp_path: Path,
) -> None:
    """Catches a prepared receipt claiming and deleting an app it never published."""
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    registry = provisioner.app_registry
    external_record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=requirement)
    )
    registry.apply_exact(
        install_records=(external_record,),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )

    provisioner.rollback(prepared.receipt_id)

    assert registry.get(requirement.app_id) == external_record


def test_restarted_prepared_transaction_cannot_claim_an_external_exact_install(
    tmp_path: Path,
) -> None:
    """Catches exact state without durable commit intent being treated as ownership."""
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    registry = provisioner.app_registry
    external_record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=requirement)
    )
    registry.apply_exact(
        install_records=(external_record,),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )
    restarted = _provisioner(state_root, MappingRemoteResolver({}))

    with pytest.raises(
        McpAppProvisionError,
        match="^mcp_app_provision_recovery_required$",
    ):
        restarted.commit(prepared)

    assert _journal(state_root)["transactions"][_TRANSACTION_ID]["state"] == "prepared"
    restarted.rollback(prepared.receipt_id)
    assert registry.get(requirement.app_id) == external_record


def test_commit_stale_cas_never_claims_an_external_exact_install(tmp_path: Path) -> None:
    """Catches exact external state being misattributed to a stale transaction receipt."""
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    registry = provisioner.app_registry
    external_record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=requirement)
    )
    registry.apply_exact(
        install_records=(external_record,),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )

    with pytest.raises(
        McpAppProvisionError,
        match="^mcp_app_provision_registry_stale$",
    ):
        provisioner.commit(prepared)

    assert _journal(state_root)["transactions"][_TRANSACTION_ID]["state"] == "prepared"
    provisioner.rollback(prepared.receipt_id)
    assert registry.get(requirement.app_id) == external_record


def test_commit_reconciliation_never_masks_the_active_baseexception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a failed recovery read replacing cancellation from the registry write."""
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        tmp_path / "mcp",
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    active = Marker()
    secondary = Marker()

    def fail_commit(**_kwargs: object) -> None:
        raise active

    def fail_reconciliation():
        raise secondary

    monkeypatch.setattr(provisioner.app_registry, "apply_exact", fail_commit)
    monkeypatch.setattr(provisioner.app_registry, "snapshot", fail_reconciliation)

    with pytest.raises(Marker) as caught:
        provisioner.commit(prepared)

    assert caught.value is active
    assert _journal(tmp_path / "mcp")["transactions"][_TRANSACTION_ID]["state"] == ("committing")


def test_prepare_cleanup_preserves_a_nonexception_raised_after_an_ordinary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cleanup cancellation being replaced by the ordinary source failure."""
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: RuntimeError("source detail")}),
    )
    marker = Marker()
    original = McpStateStore.write_bytes
    journal_writes = 0

    def cancel_after_cleanup_write(store: McpStateStore, payload: bytes) -> None:
        nonlocal journal_writes
        original(store, payload)
        if store.document_path == provisioner.journal_path:
            journal_writes += 1
            if journal_writes == 2:
                raise marker

    monkeypatch.setattr(McpStateStore, "write_bytes", cancel_after_cleanup_write)

    with pytest.raises(Marker) as caught:
        provisioner.prepare(_TRANSACTION_ID, (requirement,))

    assert caught.value is marker


def test_commit_reconciliation_cleanup_preserves_its_active_nonexception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches reconciliation cleanup cancellation being mapped to an ordinary error."""
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    apply_exact = provisioner.app_registry.apply_exact
    marker = Marker()

    def apply_then_fail(**kwargs: object):
        apply_exact(**kwargs)  # type: ignore[arg-type]
        raise RuntimeError("registry detail")

    original = McpStateStore.write_bytes
    journal_writes = 0

    def cancel_after_reconciliation_write(store: McpStateStore, payload: bytes) -> None:
        nonlocal journal_writes
        original(store, payload)
        if store.document_path == provisioner.journal_path:
            journal_writes += 1
            if journal_writes == 2:
                raise marker

    monkeypatch.setattr(provisioner.app_registry, "apply_exact", apply_then_fail)
    monkeypatch.setattr(McpStateStore, "write_bytes", cancel_after_reconciliation_write)

    with pytest.raises(Marker) as caught:
        provisioner.commit(prepared)

    assert caught.value is marker


@pytest.mark.parametrize("corruption", ["missing_reused_record", "premature_revision"])
def test_journal_rejects_state_that_does_not_carry_its_exact_recovery_identity(
    tmp_path: Path,
    corruption: str,
) -> None:
    """Catches a prepared journal omitting records or claiming a commit revision early."""
    state_root = tmp_path / corruption
    requirement = _signed_requirement(
        tmp_path,
        f"signed-{corruption}",
        _remote_requirement_for("example.remote.reused"),
    )
    registry = McpAppRegistry(state_root=state_root)
    record = InstalledMcpAppRecord.from_source(TrustedRemoteAppSource(requirement=requirement))
    registry.apply_exact(
        install_records=(record,),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )
    provisioner = McpAppProvisioner(
        app_registry=registry,
        package_registry=PackageRegistry(state_root=state_root),
        source_resolver=MappingRemoteResolver({}),
    )
    provisioner.prepare(_TRANSACTION_ID, (requirement,))
    document = _journal(state_root)
    transaction = document["transactions"][_TRANSACTION_ID]
    if corruption == "missing_reused_record":
        transaction["records"] = []
    else:
        transaction["committed_revision"] = transaction["base_revision"]
    provisioner.journal_path.write_bytes(canonical_bytes(document))

    restarted = _provisioner(state_root, MappingRemoteResolver({}))
    with pytest.raises(
        McpAppProvisionError,
        match="^mcp_app_provision_state_invalid$",
    ):
        restarted.abort(_TRANSACTION_ID)


@pytest.mark.parametrize(
    "phase",
    ["prepare_prewrite", "prepare_complete", "commit_intent", "rollback_intent"],
)
def test_ordinary_journal_failure_is_content_free_and_retains_recovery_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Catches raw storage errors or erased evidence at transaction journal seams."""
    state_root = tmp_path / phase
    requirement = _signed_requirement(
        tmp_path,
        f"signed-{phase}",
        _remote_requirement_for(f"example.remote.{phase.replace('_', '-')}"),
    )
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = None
    if phase in {"commit_intent", "rollback_intent"}:
        prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    if phase == "rollback_intent":
        provisioner.commit(prepared)
    failure_at = 2 if phase == "prepare_complete" else 1
    original = McpStateStore.write_bytes
    journal_writes = 0

    def fail_selected_write(store: McpStateStore, payload: bytes) -> None:
        nonlocal journal_writes
        if store.document_path == provisioner.journal_path:
            journal_writes += 1
            if journal_writes == failure_at:
                raise RuntimeError("private storage detail")
        original(store, payload)

    monkeypatch.setattr(McpStateStore, "write_bytes", fail_selected_write)

    with pytest.raises(
        McpAppProvisionError,
        match="^mcp_app_provision_recovery_required$",
    ) as caught:
        if phase.startswith("prepare"):
            provisioner.prepare(_TRANSACTION_ID, (requirement,))
        elif phase == "commit_intent":
            provisioner.commit(prepared)
        else:
            provisioner.rollback(prepared.receipt_id)

    assert caught.value.__cause__ is None
    if provisioner.journal_path.exists():
        assert b"private storage detail" not in provisioner.journal_path.read_bytes()


@pytest.mark.parametrize(
    ("phase", "failure_at", "expected_state", "app_present"),
    [
        ("prepare_prewrite", 1, "preparing", False),
        ("prepare_complete", 2, None, False),
        ("commit_intent", 1, "committing", False),
        ("commit_complete", 2, "committed", True),
        ("abort", 1, None, False),
        ("rollback_intent", 1, "rolling_back", True),
        ("rollback_complete", 2, "rolled_back", False),
    ],
)
def test_cancellation_identity_and_evidence_survive_every_journal_write_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure_at: int,
    expected_state: str | None,
    app_present: bool,
) -> None:
    """Catches cancellation masking or evidence loss before/after durable mutations."""
    state_root = tmp_path / phase
    requirement = _signed_requirement(
        tmp_path,
        f"cancel-{phase}",
        _remote_requirement_for(f"example.remote.cancel-{phase.replace('_', '-')}"),
    )
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    prepared = None
    if phase in {
        "commit_intent",
        "commit_complete",
        "abort",
        "rollback_intent",
        "rollback_complete",
    }:
        prepared = provisioner.prepare(_TRANSACTION_ID, (requirement,))
    if phase in {"rollback_intent", "rollback_complete"}:
        provisioner.commit(prepared)
    marker = Marker()
    original = McpStateStore.write_bytes
    journal_writes = 0

    def cancel_after_selected_write(store: McpStateStore, payload: bytes) -> None:
        nonlocal journal_writes
        original(store, payload)
        if store.document_path == provisioner.journal_path:
            journal_writes += 1
            if journal_writes == failure_at:
                raise marker

    monkeypatch.setattr(McpStateStore, "write_bytes", cancel_after_selected_write)

    with pytest.raises(Marker) as caught:
        if phase.startswith("prepare"):
            provisioner.prepare(_TRANSACTION_ID, (requirement,))
        elif phase.startswith("commit"):
            provisioner.commit(prepared)
        elif phase == "abort":
            provisioner.abort(_TRANSACTION_ID)
        else:
            provisioner.rollback(prepared.receipt_id)

    assert caught.value is marker
    document = _journal(state_root)
    transaction = document["transactions"].get(_TRANSACTION_ID)
    assert (None if transaction is None else transaction["state"]) == expected_state
    records = provisioner.app_registry.snapshot().records
    assert (tuple(record.app_id for record in records) == (requirement.app_id,)) is app_present


def test_local_package_cancellation_preserves_identity_and_cleans_prepare_state(
    tmp_path: Path,
) -> None:
    """Catches package-install cancellation publishing an app or erasing its cause."""
    state_root = tmp_path / "mcp"
    private, ring = ephemeral_ring()
    package_parent = tmp_path / "package-cancel"
    package_parent.mkdir()
    package_dir = _build_current_signed_package(
        package_parent,
        private,
        signing_key_id="test-release",
    )

    def verifier(path: Path) -> VerifiedPackage:
        return verify_package(path, ring, omega_version="3.0.0", platform_tag=host_tag())

    package_registry = PackageRegistry(state_root=state_root, verifier=verifier)
    requirement = _signed_requirement(
        tmp_path,
        "local-cancel-plugin",
        local_requirement(verifier(package_dir)),
    )
    marker = Marker()

    class CancellingLocalResolver:
        def resolve(self, _requirement: AppRequirement) -> VerifiedLocalAppSource:
            package_registry.install(package_dir)
            raise marker

    provisioner = _provisioner(
        state_root,
        CancellingLocalResolver(),
        package_registry=package_registry,
    )

    with pytest.raises(Marker) as caught:
        provisioner.prepare(_TRANSACTION_ID, (requirement,))

    assert caught.value is marker
    assert package_registry.get(requirement.app_id).enabled is False
    assert provisioner.app_registry.snapshot().records == ()
    assert _TRANSACTION_ID not in _journal(state_root)["transactions"]


def test_journal_read_failure_is_stable_content_free_and_keeps_durable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches raw storage-read details escaping the public recovery boundary."""
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    provisioner = _provisioner(
        state_root,
        MappingRemoteResolver({requirement.app_id: requirement}),
    )
    provisioner.prepare(_TRANSACTION_ID, (requirement,))
    durable = provisioner.journal_path.read_bytes()
    original = McpStateStore.read_bytes

    def fail_journal_read(store: McpStateStore) -> bytes | None:
        if store.document_path == provisioner.journal_path:
            raise RuntimeError("private read detail")
        return original(store)

    monkeypatch.setattr(McpStateStore, "read_bytes", fail_journal_read)

    with pytest.raises(
        McpAppProvisionError,
        match="^mcp_app_provision_state_invalid$",
    ) as caught:
        provisioner.abort(_TRANSACTION_ID)

    assert caught.value.__cause__ is None
    assert provisioner.journal_path.read_bytes() == durable


def test_retiring_an_absent_app_releases_it_from_the_protected_set(
    tmp_path: Path,
) -> None:
    """Uninstall removes the record itself, so the journal must let go of it.

    ``_protected_app_ids`` protects every app named by a COMMITTED transaction.
    A record removed through the registry's exact CAS leaves that transaction
    committed and still naming it, so the app stays protected and the next
    rollback silently declines to remove it.
    """
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    resolver = MappingRemoteResolver({requirement.app_id: requirement})
    provisioner = _provisioner(state_root, resolver)
    provisioner.commit(provisioner.prepare(_TRANSACTION_ID, (requirement,)))

    snapshot = provisioner.app_registry.snapshot()
    provisioner.app_registry.apply_exact(
        install_records=(),
        remove_records=tuple(snapshot.records),
        expected_revision=snapshot.revision,
    )
    assert _protected_app_ids(_journal(state_root)) == {requirement.app_id}

    retired = provisioner.retire_absent_app_ids((requirement.app_id,))

    assert retired == (_TRANSACTION_ID,)
    document = _journal(state_root)
    assert _protected_app_ids(document) == set()
    # Owning nothing, it is deleted rather than left as a shell that still
    # names the app -- that shell is what lets two installs of one plugin end
    # up describing it differently, which rollback then refuses to resolve.
    assert _TRANSACTION_ID not in document["transactions"]


def test_a_live_record_is_never_retired(tmp_path: Path) -> None:
    """The guard against orphaning an app that is still installed."""
    state_root = tmp_path / "mcp"
    requirement = _signed_remote_requirement(tmp_path)
    resolver = MappingRemoteResolver({requirement.app_id: requirement})
    provisioner = _provisioner(state_root, resolver)
    provisioner.commit(provisioner.prepare(_TRANSACTION_ID, (requirement,)))

    assert provisioner.retire_absent_app_ids((requirement.app_id,)) == ()

    document = _journal(state_root)
    assert document["transactions"][_TRANSACTION_ID]["state"] == "committed"
    assert _protected_app_ids(document) == {requirement.app_id}


def test_a_transaction_still_owning_another_app_keeps_it_protected(
    tmp_path: Path,
) -> None:
    """Retiring one app of a pair must not release the other."""
    state_root = tmp_path / "mcp"
    kept = _signed_requirement(tmp_path, "kept", _remote_requirement_for("example.remote.kept"))
    removed = _signed_requirement(
        tmp_path, "removed", _remote_requirement_for("example.remote.removed")
    )
    resolver = MappingRemoteResolver({kept.app_id: kept, removed.app_id: removed})
    provisioner = _provisioner(state_root, resolver)
    provisioner.commit(provisioner.prepare(_TRANSACTION_ID, (kept, removed)))

    snapshot = provisioner.app_registry.snapshot()
    provisioner.app_registry.apply_exact(
        install_records=(),
        remove_records=tuple(r for r in snapshot.records if r.app_id == removed.app_id),
        expected_revision=snapshot.revision,
    )
    assert provisioner.retire_absent_app_ids((removed.app_id,)) == (_TRANSACTION_ID,)

    document = _journal(state_root)
    transaction = document["transactions"][_TRANSACTION_ID]
    assert transaction["state"] == "committed"
    assert transaction["remaining_introduced_app_ids"] == [kept.app_id]
    assert kept.app_id in _protected_app_ids(document)


def test_a_reinstall_after_retirement_leaves_exactly_one_owning_transaction(
    tmp_path: Path,
) -> None:
    """The live 2026-09-07 failure, end to end.

    Slack was uninstalled and reinstalled after being re-released at the same
    version. Its first transaction stayed committed, so a second committed
    transaction ended up naming the same app with a DIFFERENT record, and
    ``rollback`` then refused the pair outright -- two rollbacks disagreeing
    about what they would delete must stop, not guess. Retiring on uninstall
    is what keeps the second install starting from nothing.
    """
    state_root = tmp_path / "mcp"
    first = _signed_requirement(
        tmp_path, "first", _remote_requirement_for("example.remote.app")
    )
    second = _signed_requirement(
        tmp_path,
        "second",
        _remote_requirement_for("example.remote.app", capability_name="list_items_v2"),
    )
    provisioner = _provisioner(state_root, MappingRemoteResolver({first.app_id: first}))
    provisioner.commit(provisioner.prepare("7" * 32, (first,)))

    snapshot = provisioner.app_registry.snapshot()
    provisioner.app_registry.apply_exact(
        install_records=(),
        remove_records=tuple(snapshot.records),
        expected_revision=snapshot.revision,
    )
    provisioner.retire_absent_app_ids((first.app_id,))

    reinstalled = _provisioner(state_root, MappingRemoteResolver({second.app_id: second}))
    receipt = reinstalled.commit(reinstalled.prepare("8" * 32, (second,)))

    document = _journal(state_root)
    naming = [
        key
        for key, raw in document["transactions"].items()
        if first.app_id in json.dumps(raw)
    ]
    assert naming == ["8" * 32]
    # The retired original no longer disputes the record, so rollback resolves.
    reinstalled.rollback(receipt.receipt_id)
    assert reinstalled.app_registry.snapshot().records == ()
