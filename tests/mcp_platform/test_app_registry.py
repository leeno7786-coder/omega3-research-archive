"""Durable dormant MCP app identity registry tests."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from core import paths as core_paths
from mcp_platform.app_registry import (
    InstalledMcpAppRecord,
    McpAppIdentityCollision,
    McpAppNotInstalled,
    McpAppRegistry,
    McpAppRegistryConflict,
    McpAppRegistryError,
    McpAppRegistrySnapshot,
    McpAppRegistryUnreadable,
)
import mcp_platform.state_store as state_store_module
import runtime.pinned_filesystem as pinned_filesystem_module
from mcp_platform.canonical import canonical_bytes
from mcp_platform.registry import InstalledPackageRecord
from mcp_platform.app_sources import TrustedRemoteAppSource
from plugin_platform.contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppRequirement,
    CapabilityKind,
)
from tests.mcp_platform.test_app_sources import _write_verified_plugin_requirement


def _requirement(
    app_id: str,
    *,
    deployment: str = "local",
    publisher_id: str = "omega.first_party",
    provider_id: str | None = "workspace",
    transports: list[str] | None = None,
    remote_origin: str | None = None,
    authorization_issuer: str | None = None,
    authentication: dict[str, object] | None = None,
) -> AppRequirement:
    selected_transports = transports
    if selected_transports is None:
        selected_transports = ["stdio" if deployment == "local" else "streamable_http"]
    return AppRequirement.from_dict(
        {
            "app_id": app_id,
            "publisher_id": publisher_id,
            "deployment": deployment,
            "provider_id": provider_id,
            "protocol_version": PLUGIN_PROTOCOL_VERSION,
            "transports": selected_transports,
            "remote_origin": remote_origin,
            "authorization_issuer": authorization_issuer,
            "authentication": authentication
            or {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": [
                {
                    "kind": "resource",
                    "name": "workspace_item",
                    "schema_sha256": None,
                    "version": None,
                },
                {
                    "kind": "tool",
                    "name": "list_items",
                    "schema_sha256": "1" * 64,
                    "version": None,
                },
            ],
        }
    )


def _package_record(
    app_id: str,
    *,
    publisher_id: str = "omega.first_party",
    manifest_digest: str = "a" * 64,
) -> InstalledPackageRecord:
    return InstalledPackageRecord(
        server_id=app_id,
        server_version="1.2.3",
        platform="windows-x86_64",
        publisher_id=publisher_id,
        signing_key_id="test-release",
        manifest_digest=manifest_digest,
        artifact_sha256="b" * 64,
        tool_catalog_sha256="c" * 64,
        resource_catalog_sha256="d" * 64,
        network_class="local_only",
        enabled=False,
        status="installed",
        blocker=None,
        crash_count=0,
    )


def _local_record(app_id: str = "omega.first_party.workspace") -> InstalledMcpAppRecord:
    requirement = _requirement(app_id)
    package = _package_record(app_id)
    return InstalledMcpAppRecord(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=requirement.deployment,
        provider_id=requirement.provider_id,
        protocol_version=requirement.protocol_version,
        transports=requirement.transports,
        remote_origin=None,
        authorization_issuer=None,
        authentication=requirement.authentication,
        capabilities=requirement.capabilities,
        local_package_version=package.server_version,
        local_package_platform=package.platform,
        local_package_manifest_sha256=package.manifest_digest,
        installed=True,
        trusted=True,
        enabled=False,
    )


def _remote_record(app_id: str = "example.remote.workspace") -> InstalledMcpAppRecord:
    requirement = _requirement(
        app_id,
        deployment="remote",
        publisher_id="example.publisher",
        remote_origin="https://workspace.example",
        authorization_issuer="https://auth.example/tenant",
    )
    return InstalledMcpAppRecord(
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
        local_package_version=None,
        local_package_platform=None,
        local_package_manifest_sha256=None,
        installed=True,
        trusted=True,
        enabled=False,
    )


def _trusted_remote_record(
    root: Path,
    app_id: str = "example.remote.workspace",
    *,
    provider_id: str | None = "workspace",
    remote_origin: str = "https://workspace.example",
    authorization_issuer: str = "https://auth.example/tenant",
    authentication: dict[str, object] | None = None,
) -> InstalledMcpAppRecord:
    root.mkdir(parents=True)
    requirement = _requirement(
        app_id,
        deployment="remote",
        publisher_id="example.publisher",
        provider_id=provider_id,
        remote_origin=remote_origin,
        authorization_issuer=authorization_issuer,
        authentication=authentication,
    )
    signed = _write_verified_plugin_requirement(root, requirement)
    return InstalledMcpAppRecord.from_source(TrustedRemoteAppSource(requirement=signed))


def test_durable_record_round_trip_retains_signed_authentication(tmp_path: Path) -> None:
    authentication = {
        "kind": "oauth_device_code",
        "scopes": ["repo", "read:org"],
        "secret_fields": [],
    }
    record = _trusted_remote_record(tmp_path / "source", authentication=authentication)

    restored = InstalledMcpAppRecord.from_dict(
        record.to_dict(),
        registry_key=record.app_id,
    )

    assert restored.authentication == record.authentication
    assert restored.authentication.to_dict() == authentication


def _install(
    registry: McpAppRegistry,
    record: InstalledMcpAppRecord,
) -> McpAppRegistrySnapshot:
    return registry.install(
        record,
        expected_revision=registry.snapshot().revision,
    )


def test_public_exact_batch_applies_one_atomic_sorted_install_and_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    left = _trusted_remote_record(tmp_path / "left", "example.remote.left")
    right = _trusted_remote_record(tmp_path / "right", "example.remote.right")
    writes: list[bytes] = []
    original = state_store_module.McpStateStore.write_bytes

    def recording_write(store: object, payload: bytes) -> None:
        writes.append(payload)
        original(store, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(state_store_module.McpStateStore, "write_bytes", recording_write)
    installed = registry.apply_exact(
        install_records=(right, left),
        remove_records=(),
        expected_revision=registry.snapshot().revision,
    )

    assert tuple(record.app_id for record in installed.records) == (
        "example.remote.left",
        "example.remote.right",
    )
    assert len(writes) == 1

    writes.clear()
    removed = registry.apply_exact(
        install_records=(),
        remove_records=(left, right),
        expected_revision=installed.revision,
    )

    assert removed.records == ()
    assert len(writes) == 1


def test_public_exact_batch_rejects_duplicate_overlap_stale_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    record = _trusted_remote_record(tmp_path / "source")
    initial = registry.snapshot()

    for installs, removals in (
        ((record, record), ()),
        ((record,), (record,)),
        ((), (record, record)),
    ):
        with pytest.raises(ValueError, match="^mcp_app_batch_invalid$"):
            registry.apply_exact(
                install_records=installs,
                remove_records=removals,
                expected_revision=initial.revision,
            )

    installed = registry.apply_exact(
        install_records=(record,),
        remove_records=(),
        expected_revision=initial.revision,
    )
    with pytest.raises(McpAppRegistryConflict, match="^mcp_app_registry_conflict$"):
        registry.apply_exact(
            install_records=(),
            remove_records=(record,),
            expected_revision=initial.revision,
        )

    changed = _trusted_remote_record(
        tmp_path / "changed",
        authorization_issuer="https://auth.example/changed",
    )
    with pytest.raises(McpAppIdentityCollision, match="^mcp_app_identity_collision$"):
        registry.apply_exact(
            install_records=(),
            remove_records=(changed,),
            expected_revision=installed.revision,
        )
    assert registry.snapshot() == installed


def test_replace_exact_atomically_migrates_one_trusted_app_identity(tmp_path: Path) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    installed = _trusted_remote_record(
        tmp_path / "installed",
        remote_origin="https://workspace.example",
    )
    replacement = _trusted_remote_record(
        tmp_path / "replacement",
        remote_origin="https://workspace.example/mcp/v1",
    )
    before = _install(registry, installed)

    after = registry.replace_exact(
        expected_record=installed,
        replacement_record=replacement,
        expected_revision=before.revision,
    )

    assert after.records == (replacement,)
    assert registry.get(installed.app_id) == replacement

    with pytest.raises(McpAppIdentityCollision, match="^mcp_app_identity_collision$"):
        registry.replace_exact(
            expected_record=installed,
            replacement_record=replacement,
            expected_revision=after.revision,
        )


def test_single_remove_checks_its_revision_before_reporting_a_missing_identity(
    tmp_path: Path,
) -> None:
    """Catches an unlocked pre-read changing stale-CAS failure precedence."""
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    stale = registry.snapshot().revision
    record = _trusted_remote_record(tmp_path / "source")
    registry.apply_exact(
        install_records=(record,),
        remove_records=(),
        expected_revision=stale,
    )

    with pytest.raises(McpAppRegistryConflict, match="^mcp_app_registry_conflict$"):
        registry.remove("example.remote.absent", expected_revision=stale)


def test_canonical_mcp_app_paths_live_under_the_single_mcp_state_root() -> None:
    assert core_paths.MCP_APPS_REGISTRY_PATH.name == "apps_registry.json"
    assert core_paths.MCP_APP_PROVISION_JOURNAL_PATH.name == "app_provision_journal.json"
    assert (
        core_paths.MCP_APPS_REGISTRY_PATH.parent == core_paths.MCP_APP_PROVISION_JOURNAL_PATH.parent
    )
    assert core_paths.MCP_APPS_REGISTRY_PATH.parent.name == "mcp"


def test_missing_registry_is_empty_but_malformed_registry_is_unreadable(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")

    empty = registry.snapshot()

    assert empty.records == ()
    assert empty.revision == "e570fd7b43129599764a82f23e0f019fe0694cde35926790d5408d7e05cba54e"

    registry.registry_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(
        McpAppRegistryUnreadable,
        match="^mcp_app_registry_unreadable$",
    ):
        registry.snapshot()


def test_snapshot_revision_and_records_are_canonical_and_order_independent(
    tmp_path: Path,
) -> None:
    first = _trusted_remote_record(tmp_path / "first-source", "example.remote.alpha")
    second = _trusted_remote_record(tmp_path / "second-source", "example.remote.zulu")
    left = McpAppRegistry(state_root=tmp_path / "left")
    right = McpAppRegistry(state_root=tmp_path / "right")

    _install(left, second)
    _install(left, first)
    _install(right, first)
    _install(right, second)

    left_snapshot = left.snapshot()
    right_snapshot = right.snapshot()

    assert tuple(record.app_id for record in left_snapshot.records) == (
        "example.remote.alpha",
        "example.remote.zulu",
    )
    assert left_snapshot == right_snapshot
    assert left.registry_path.read_bytes() == right.registry_path.read_bytes()
    with pytest.raises(FrozenInstanceError):
        left_snapshot.revision = "0" * 64


def test_snapshot_direct_constructor_rejects_mutable_records_and_unrelated_revision(
    tmp_path: Path,
) -> None:
    """Catches callers forging the advertised immutable canonical snapshot."""
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    actual = registry.snapshot()

    with pytest.raises(ValueError, match="^mcp_app_snapshot_invalid$"):
        McpAppRegistrySnapshot(records=[], revision=actual.revision)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="^mcp_app_snapshot_invalid$"):
        McpAppRegistrySnapshot(records=actual.records, revision="0" * 64)


def test_local_and_remote_records_have_one_exact_closed_shape() -> None:
    local = _local_record()
    remote = _remote_record()
    exact_fields = {
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

    assert set(local.to_dict()) == exact_fields
    assert set(remote.to_dict()) == exact_fields
    assert (
        local.remote_origin,
        local.authorization_issuer,
        local.local_package_version,
        local.local_package_platform,
        local.local_package_manifest_sha256,
    ) == (None, None, "1.2.3", "windows-x86_64", "a" * 64)
    assert (
        remote.remote_origin,
        remote.authorization_issuer,
        remote.local_package_version,
        remote.local_package_platform,
        remote.local_package_manifest_sha256,
    ) == (
        "https://workspace.example",
        "https://auth.example/tenant",
        None,
        None,
        None,
    )


def test_app_records_are_immutable_canonical_dormant_installs() -> None:
    record = _local_record()

    assert record.installed is True
    assert record.trusted is True
    assert record.enabled is False
    assert tuple(item.kind for item in record.capabilities) == (
        CapabilityKind.RESOURCE,
        CapabilityKind.TOOL,
    )
    with pytest.raises(FrozenInstanceError):
        record.enabled = True
    with pytest.raises((AttributeError, TypeError)):
        record.__dict__["enabled"] = True


def test_record_factory_rejects_nominal_requirement_and_fabricated_package_record(
    tmp_path: Path,
) -> None:
    """Catches nominal public records manufacturing trusted local app authority."""
    with pytest.raises(ValueError, match="^mcp_app_identity_invalid$"):
        InstalledMcpAppRecord.from_requirement(
            _requirement("omega.first_party.workspace"),
            package=_package_record("omega.first_party.workspace"),
        )

    nominal = _local_record()
    forged = InstalledMcpAppRecord.from_dict(
        nominal.to_dict(),
        registry_key=nominal.app_id,
    )
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    with pytest.raises(ValueError, match="^mcp_app_identity_invalid$"):
        registry.install(forged, expected_revision=registry.snapshot().revision)


def test_record_nested_capability_mutation_cannot_change_persisted_authority(
    tmp_path: Path,
) -> None:
    """Catches a post-construction capability ``__dict__`` edit reaching disk."""
    requirement_root = tmp_path / "signed-requirement"
    requirement_root.mkdir()
    signed = _write_verified_plugin_requirement(
        requirement_root,
        _requirement(
            "example.remote.workspace",
            deployment="remote",
            publisher_id="example.publisher",
            remote_origin="https://workspace.example",
            authorization_issuer="https://auth.example/tenant",
        ),
    )
    record = InstalledMcpAppRecord.from_source(TrustedRemoteAppSource(requirement=signed))
    capability = record.capabilities[0]
    original_name = capability.name
    try:
        capability.__dict__["name"] = "forged_tool"
    except (AttributeError, TypeError):
        pass
    registry = McpAppRegistry(state_root=tmp_path / "mcp")

    _install(registry, record)

    assert registry.get(record.app_id).capabilities[0].name == original_name


@pytest.mark.parametrize(
    ("requirement", "package"),
    [
        (
            _requirement(
                "example.remote.workspace",
                deployment="remote",
                publisher_id="example.publisher",
                remote_origin="https://workspace.example",
                authorization_issuer="https://auth.example",
            ),
            _package_record("example.remote.workspace", publisher_id="example.publisher"),
        ),
        (_requirement("omega.first_party.workspace"), None),
        (
            _requirement("omega.first_party.workspace"),
            _package_record("omega.first_party.other"),
        ),
        (
            _requirement("omega.first_party.workspace"),
            _package_record(
                "omega.first_party.workspace",
                publisher_id="other.publisher",
            ),
        ),
    ],
)
def test_record_factory_closes_local_and_remote_package_identity(
    requirement: AppRequirement,
    package: InstalledPackageRecord | None,
) -> None:
    with pytest.raises(ValueError, match="^mcp_app_identity_invalid$"):
        InstalledMcpAppRecord.from_requirement(requirement, package=package)


@pytest.mark.parametrize(
    "changes",
    [
        {"installed": False},
        {"trusted": False},
        {"enabled": True},
        {"installed": 1},
        {"trusted": 1},
        {"enabled": 0},
    ],
)
def test_constructor_rejects_every_non_dormant_or_non_boolean_state(
    changes: dict[str, object],
) -> None:
    baseline = _local_record()

    with pytest.raises(ValueError, match="^mcp_app_state_invalid$"):
        replace(baseline, **changes)


@pytest.mark.parametrize(
    "expected_revision",
    [None, b"0" * 64, True, "A" * 64, "0" * 63, "0" * 65],
)
def test_every_install_mutation_requires_an_exact_current_revision(
    tmp_path: Path,
    expected_revision: object,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")

    with pytest.raises(ValueError, match="^invalid_app_registry_revision$"):
        registry.install(_local_record(), expected_revision=expected_revision)


def test_install_is_exactly_idempotent_and_collision_is_fail_closed(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    record = _trusted_remote_record(tmp_path / "record-source")
    installed = _install(registry, record)
    durable = registry.registry_path.read_bytes()

    repeated = registry.install(record, expected_revision=installed.revision)

    assert repeated == installed
    assert registry.registry_path.read_bytes() == durable

    collision = _trusted_remote_record(
        tmp_path / "collision-source",
        provider_id="different-provider",
    )
    with pytest.raises(
        McpAppIdentityCollision,
        match="^mcp_app_identity_collision$",
    ):
        registry.install(collision, expected_revision=repeated.revision)
    assert registry.registry_path.read_bytes() == durable


def test_stale_revision_wins_over_idempotence_and_changes_nothing(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    before = registry.snapshot()
    record = _trusted_remote_record(tmp_path / "record-source")
    current = registry.install(record, expected_revision=before.revision)
    durable = registry.registry_path.read_bytes()

    with pytest.raises(McpAppRegistryConflict, match="^mcp_app_registry_conflict$"):
        registry.install(record, expected_revision=before.revision)

    assert current == registry.snapshot()
    assert registry.registry_path.read_bytes() == durable


def test_remove_is_revision_checked_and_preserves_unrelated_records(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    first = _trusted_remote_record(tmp_path / "first-source", "example.remote.alpha")
    second = _trusted_remote_record(tmp_path / "second-source", "example.remote.zulu")
    _install(registry, first)
    current = _install(registry, second)

    removed = registry.remove(
        first.app_id,
        expected_revision=current.revision,
    )

    assert removed.records == (second,)
    assert registry.get(second.app_id) == second
    with pytest.raises(McpAppNotInstalled, match="^mcp_app_not_installed$"):
        registry.get(first.app_id)

    durable = registry.registry_path.read_bytes()
    with pytest.raises(McpAppRegistryConflict, match="^mcp_app_registry_conflict$"):
        registry.remove(second.app_id, expected_revision=current.revision)
    assert registry.registry_path.read_bytes() == durable

    with pytest.raises(McpAppNotInstalled, match="^mcp_app_not_installed$"):
        registry.remove("missing.app", expected_revision=removed.revision)
    assert registry.registry_path.read_bytes() == durable


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update({"extra": True}),
        lambda document: document.update({"schema_version": 2}),
        lambda document: document.update({"schema_version": True}),
        lambda document: document.update({"apps": []}),
        lambda document: document["apps"]["omega.first_party.workspace"].update(
            {"unexpected": "forbidden"}
        ),
        lambda document: document["apps"]["omega.first_party.workspace"].update(
            {"remote_origin": "https://unverified.example"}
        ),
        lambda document: document["apps"]["omega.first_party.workspace"].update(
            {"local_package_manifest_sha256": None}
        ),
    ],
)
def test_persisted_document_and_record_fields_are_exact_and_fail_closed(
    tmp_path: Path,
    mutation,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    record = _local_record()
    document: dict[str, object] = {
        "schema_version": 1,
        "apps": {record.app_id: record.to_dict()},
    }
    mutation(document)
    registry.state_root.mkdir(parents=True)
    registry.registry_path.write_bytes(canonical_bytes(document))

    with pytest.raises(
        McpAppRegistryUnreadable,
        match="^mcp_app_registry_unreadable$",
    ):
        registry.snapshot()


def test_duplicate_json_keys_are_unreadable_not_last_value_wins(tmp_path: Path) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    registry.state_root.mkdir(parents=True)
    registry.registry_path.write_text(
        '{"schema_version":1,"schema_version":1,"apps":{}}',
        encoding="utf-8",
    )

    with pytest.raises(
        McpAppRegistryUnreadable,
        match="^mcp_app_registry_unreadable$",
    ):
        registry.snapshot()


def test_registry_document_contains_no_executable_or_private_material(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    _install(registry, _trusted_remote_record(tmp_path / "first-source"))
    _install(
        registry,
        _trusted_remote_record(tmp_path / "second-source", "example.remote.second"),
    )
    document = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    encoded = registry.registry_path.read_text(encoding="utf-8").casefold()

    forbidden_fields = {
        "command",
        "arguments",
        "artifact_path",
        "endpoint",
        "token",
        "secret",
        "credential",
        "account_id",
        "user_id",
        "authorization_code",
    }
    for raw in document["apps"].values():
        assert forbidden_fields.isdisjoint(raw)
    for marker in (
        "bearer ",
        "refresh_token",
        "client_secret",
        "authorization_code",
        "account_id",
        "user_id",
    ):
        assert marker not in encoded


def test_mutation_uses_shared_atomic_replace_then_parent_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    registry.snapshot()
    events: list[tuple[str, Path]] = []
    real_replace = state_store_module.PinnedDirectory.replace
    real_fsync = state_store_module.PinnedDirectory.fsync

    def recording_replace(directory, source_name, target_name):
        if target_name == registry.registry_path.name:
            events.append(("replace", registry.registry_path))
        return real_replace(directory, source_name, target_name)

    def recording_parent_fsync(directory) -> None:
        if events and events[-1][0] == "replace":
            events.append(("fsync", directory.path))
        real_fsync(directory)

    monkeypatch.setattr(state_store_module.PinnedDirectory, "replace", recording_replace)
    monkeypatch.setattr(state_store_module.PinnedDirectory, "fsync", recording_parent_fsync)

    _install(registry, _trusted_remote_record(tmp_path / "first-source"))

    assert events == [
        ("replace", registry.registry_path),
        ("fsync", registry.registry_path.parent),
    ]


def test_partial_atomic_replace_preserves_old_bytes_cleans_temp_and_exception_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    _install(registry, _trusted_remote_record(tmp_path / "first-source"))
    before = registry.registry_path.read_bytes()
    current = registry.snapshot()

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled()
    real_replace = state_store_module.PinnedDirectory.replace

    def cancel_replace(directory, source_name, target_name):
        if str(source_name).startswith(".apps_registry.json."):
            raise cancellation
        return real_replace(directory, source_name, target_name)

    monkeypatch.setattr(state_store_module.PinnedDirectory, "replace", cancel_replace)

    with pytest.raises(Cancelled) as caught:
        registry.install(
            _trusted_remote_record(tmp_path / "second-source", "example.remote.second"),
            expected_revision=current.revision,
        )

    assert caught.value is cancellation
    assert registry.registry_path.read_bytes() == before
    assert list(registry.state_root.glob(".*.part")) == []


def test_registry_rejects_reparse_lock_sidecar_before_opening_it(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    registry.state_root.mkdir(parents=True)
    lock_path = Path(f"{registry.registry_path}.lock")
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"sentinel")
    try:
        os.symlink(outside, lock_path)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="^mcp_state_path_invalid$"):
        registry.snapshot()
    assert outside.read_bytes() == b"sentinel"


def _replace_directory_with_link(path: Path, target: Path) -> Path:
    original = path.with_name(f"{path.name}-before-swap")
    path.rename(original)
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            original.rename(path)
            pytest.fail("junction creation unavailable during pinned state-store test")
    else:
        os.symlink(target, path, target_is_directory=True)
    return original


@pytest.mark.parametrize(
    "rebind_surface",
    [
        "state_root",
        pytest.param(
            "ancestor",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="Windows blocks ancestor rename while a descendant is pinned; exercised on Linux",
            ),
        ),
    ],
)
def test_real_pinned_sidecar_open_never_touches_rebound_state_root_or_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rebind_surface: str,
) -> None:
    """Catches a real sidecar open through a rebound root or higher ancestor."""
    durable_root = tmp_path / "durable"
    state_root = durable_root / "mcp"
    state_root.mkdir(parents=True)
    registry = McpAppRegistry(state_root=state_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    swap_path = state_root
    rebound_state_root = outside
    if rebind_surface == "ancestor":
        rebound_state_root = outside / state_root.name
        rebound_state_root.mkdir()
        swap_path = durable_root
    outside_sidecar = rebound_state_root / f"{registry.registry_path.name}.lock"
    outside_sidecar.write_bytes(b"outside-sidecar-sentinel")
    outside_handle, outside_info = pinned_filesystem_module._open_node(
        outside_sidecar,
        directory=False,
    )
    pinned_filesystem_module._close_native(outside_handle)
    outside_before = set(outside.rglob("*"))
    outside_before_bytes = outside_sidecar.read_bytes()
    opened_outside = False
    locked_outside = False
    real_open = pinned_filesystem_module._open_node
    real_lock = state_store_module.PinnedDirectory.lock_file
    swapped = False
    completed_swap = False

    def observe_real_open(path, **kwargs):
        nonlocal opened_outside
        handle, information = real_open(path, **kwargs)
        if information.identity == outside_info.identity:
            opened_outside = True
        return handle, information

    if os.name == "nt":
        real_platform_lock = pinned_filesystem_module._lock_file_ex

        def observe_platform_lock(handle, *args):
            nonlocal locked_outside
            if pinned_filesystem_module._windows_info(handle).identity == outside_info.identity:
                locked_outside = True
            return real_platform_lock(handle, *args)

        monkeypatch.setattr(
            pinned_filesystem_module,
            "_lock_file_ex",
            observe_platform_lock,
        )
    else:
        real_platform_lock = pinned_filesystem_module.fcntl.flock

        def observe_platform_lock(handle, operation):
            nonlocal locked_outside
            observed = os.fstat(handle)
            if (observed.st_dev, observed.st_ino) == outside_info.identity:
                locked_outside = True
            return real_platform_lock(handle, operation)

        monkeypatch.setattr(
            pinned_filesystem_module.fcntl,
            "flock",
            observe_platform_lock,
        )

    @contextmanager
    def swap_then_real_lock(directory, relative_path, *, timeout=10.0):
        nonlocal swapped, completed_swap
        if not swapped:
            swapped = True
            _replace_directory_with_link(swap_path, outside)
            completed_swap = True
        with real_lock(directory, relative_path, timeout=timeout):
            yield

    monkeypatch.setattr(
        state_store_module.PinnedDirectory,
        "lock_file",
        swap_then_real_lock,
    )
    monkeypatch.setattr(
        pinned_filesystem_module,
        "_open_node",
        observe_real_open,
    )

    with pytest.raises(ValueError, match="^mcp_state_path_invalid$"):
        registry.snapshot()

    assert completed_swap, "The real rename and rebind must finish before path rejection"
    assert opened_outside is False
    assert locked_outside is False
    assert set(outside.rglob("*")) == outside_before
    assert outside_sidecar.read_bytes() == outside_before_bytes


@pytest.mark.skipif(os.name != "nt", reason="Windows pinned-handle rename protection")
def test_windows_pinned_descendant_blocks_ancestor_rename_until_closed(tmp_path: Path) -> None:
    """Prove the kernel rejection directly, outside the registry's error translation."""
    durable_root = tmp_path / "durable"
    state_root = durable_root / "mcp"
    state_root.mkdir(parents=True)
    sentinel = state_root / "sentinel.txt"
    sentinel.write_bytes(b"original-state")
    renamed_root = tmp_path / "renamed"

    with pinned_filesystem_module.PinnedDirectory.open(state_root) as directory:
        with pytest.raises(PermissionError) as blocked:
            durable_root.rename(renamed_root)
        assert blocked.value.winerror in {5, 32}
        directory.validate_binding()
        assert not renamed_root.exists()
        assert sentinel.read_bytes() == b"original-state"

    # Releasing the pinned handles makes the same rename succeed. This excludes
    # an unrelated ACL or fixture failure as the cause of the earlier denial.
    durable_root.rename(renamed_root)
    assert not durable_root.exists()
    assert (renamed_root / "mcp" / "sentinel.txt").read_bytes() == b"original-state"


@pytest.mark.parametrize("rebind_surface", ["state_root", "ancestor"])
def test_real_pinned_temp_write_never_touches_rebound_state_root_or_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rebind_surface: str,
) -> None:
    """Catches a root or ancestor swap at the real temporary-file write seam."""
    durable_root = tmp_path / "durable"
    state_root = durable_root / "mcp"
    registry = McpAppRegistry(state_root=state_root)
    first = _trusted_remote_record(tmp_path / "first-source")
    current = _install(registry, first)
    before = registry.registry_path.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    swap_path = state_root
    if rebind_surface == "ancestor":
        (outside / state_root.name).mkdir()
        swap_path = durable_root
    outside_before = set(outside.rglob("*"))
    real_write = state_store_module.PinnedDirectory.write_bytes
    swapped_root: Path | None = None
    swap_blocked = False

    def swap_then_real_write(directory, relative_path, payload, *, exclusive=True):
        nonlocal swap_blocked, swapped_root
        if str(relative_path).startswith(".apps_registry.json."):
            try:
                swapped_root = _replace_directory_with_link(swap_path, outside)
            except PermissionError as exc:
                assert os.name == "nt", "POSIX must exercise the real rebind"
                assert exc.winerror in {5, 32}
                swap_blocked = True
        return real_write(directory, relative_path, payload, exclusive=exclusive)

    monkeypatch.setattr(
        state_store_module.PinnedDirectory,
        "write_bytes",
        swap_then_real_write,
    )

    try:
        registry.install(
            _trusted_remote_record(tmp_path / "second-source", "example.remote.second"),
            expected_revision=current.revision,
        )
    except ValueError as exc:
        assert str(exc) == "mcp_state_path_invalid"
        assert swapped_root is not None
    else:
        assert swap_blocked

    assert swap_blocked is (os.name == "nt")
    assert set(outside.rglob("*")) == outside_before
    if os.name == "nt":
        assert swapped_root is None
        assert before != registry.registry_path.read_bytes()
        # At this seam the sidecar file is already open, so both root and
        # ancestor renames are blocked on Windows. After locked() closes it,
        # the same operation must complete, excluding unrelated access errors.
        released_root = _replace_directory_with_link(swap_path, outside)
        assert released_root.is_dir()
        assert swap_path.resolve() == outside.resolve()
    else:
        assert swapped_root is not None
        original_state = swapped_root if rebind_surface == "state_root" else swapped_root / state_root.name
        assert (original_state / registry.registry_path.name).read_bytes() == before


@pytest.mark.parametrize("failed_operation", ["write_bytes", "replace", "unlink"])
def test_pinned_write_failures_use_the_state_error_and_preserve_the_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_operation: str,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    current = _install(registry, _trusted_remote_record(tmp_path / "first-source"))
    before = registry.registry_path.read_bytes()
    second = _trusted_remote_record(tmp_path / "second-source", "example.remote.second")
    failure = ValueError("package_tree_invalid")
    original = getattr(state_store_module.PinnedDirectory, failed_operation)

    def fail_pinned_operation(directory, relative_path, *args, **kwargs):
        if str(relative_path).startswith(".apps_registry.json."):
            raise failure
        return original(directory, relative_path, *args, **kwargs)

    monkeypatch.setattr(state_store_module.PinnedDirectory, failed_operation, fail_pinned_operation)

    with pytest.raises(ValueError, match="^mcp_state_path_invalid$") as caught:
        registry.install(second, expected_revision=current.revision)

    assert caught.value.__cause__ is failure
    if failed_operation == "unlink":
        assert registry.registry_path.read_bytes() != before
    else:
        assert registry.registry_path.read_bytes() == before


@pytest.mark.parametrize("failed_operation", ["write_bytes", "replace"])
def test_pinned_write_and_cleanup_failures_preserve_both_causes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_operation: str,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    current = _install(registry, _trusted_remote_record(tmp_path / "first-source"))
    before = registry.registry_path.read_bytes()
    second = _trusted_remote_record(tmp_path / "second-source", "example.remote.second")
    primary = ValueError("primary filesystem failure")
    cleanup = RuntimeError("temporary cleanup failed")
    original = getattr(state_store_module.PinnedDirectory, failed_operation)
    real_unlink = state_store_module.PinnedDirectory.unlink

    def fail_primary(directory, relative_path, *args, **kwargs):
        if str(relative_path).startswith(".apps_registry.json."):
            raise primary
        return original(directory, relative_path, *args, **kwargs)

    def fail_cleanup(directory, relative_path, *, missing_ok=False):
        if str(relative_path).startswith(".apps_registry.json."):
            raise cleanup
        return real_unlink(directory, relative_path, missing_ok=missing_ok)

    monkeypatch.setattr(state_store_module.PinnedDirectory, failed_operation, fail_primary)
    monkeypatch.setattr(state_store_module.PinnedDirectory, "unlink", fail_cleanup)

    with pytest.raises(ValueError, match="^mcp_state_path_invalid$") as caught:
        registry.install(second, expected_revision=current.revision)

    assert caught.value.__cause__ is cleanup
    assert cleanup.__context__ is primary
    assert registry.registry_path.read_bytes() == before
    assert bool(list(registry.state_root.glob(".apps_registry.json.*.part"))) is (
        failed_operation == "replace"
    )


@pytest.mark.parametrize("cleanup_after_delete", [False, True])
def test_active_non_exception_identity_survives_temp_cleanup_failure_with_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_after_delete: bool,
) -> None:
    """Catches cleanup replacing cancellation or losing ambiguous temp evidence."""
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    current = _install(registry, _trusted_remote_record(tmp_path / "first-source"))
    before = registry.registry_path.read_bytes()

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled()
    real_replace = state_store_module.PinnedDirectory.replace
    real_unlink = state_store_module.PinnedDirectory.unlink

    def cancel_document_replace(directory, source_name, target_name):
        if str(source_name).startswith(".apps_registry.json."):
            raise cancellation
        return real_replace(directory, source_name, target_name)

    def fail_document_cleanup(directory, relative_path, *, missing_ok=False):
        if str(relative_path).startswith(".apps_registry.json."):
            if cleanup_after_delete:
                real_unlink(directory, relative_path, missing_ok=missing_ok)
            raise RuntimeError("temporary cleanup failed")
        return real_unlink(directory, relative_path, missing_ok=missing_ok)

    monkeypatch.setattr(state_store_module.PinnedDirectory, "replace", cancel_document_replace)
    monkeypatch.setattr(state_store_module.PinnedDirectory, "unlink", fail_document_cleanup)

    with pytest.raises(Cancelled) as caught:
        registry.install(
            _trusted_remote_record(tmp_path / "second-source", "example.remote.second"),
            expected_revision=current.revision,
        )

    assert caught.value is cancellation
    assert registry.registry_path.read_bytes() == before
    temporary_files = list(registry.state_root.glob(".apps_registry.json.*.part"))
    assert bool(temporary_files) is (not cleanup_after_delete)


def test_concurrent_current_revision_writers_never_silently_lose_an_update(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry(state_root=tmp_path / "mcp")
    expected = registry.snapshot().revision
    records = (
        _trusted_remote_record(tmp_path / "first-source", "example.remote.alpha"),
        _trusted_remote_record(tmp_path / "second-source", "example.remote.zulu"),
    )
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def write(record: InstalledMcpAppRecord) -> None:
        barrier.wait(timeout=2.0)
        try:
            registry.install(record, expected_revision=expected)
        except McpAppRegistryConflict as exc:
            outcomes.append(("conflict", str(exc)))
        else:
            outcomes.append(("installed", record.app_id))

    threads = [threading.Thread(target=write, args=(record,)) for record in records]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(item[0] for item in outcomes) == ["conflict", "installed"]
    assert ("conflict", "mcp_app_registry_conflict") in outcomes
    assert len(registry.snapshot().records) == 1


def test_default_app_registry_path_is_isolated_under_current_mcp_state_root(
    tmp_path: Path,
) -> None:
    registry = McpAppRegistry()

    assert registry.state_root == tmp_path / "guardian" / "state" / "mcp"
    assert registry.registry_path == registry.state_root / "apps_registry.json"


def test_state_store_uses_one_immutable_read_write_ceiling_without_partial_publish(
    tmp_path: Path,
) -> None:
    store = state_store_module.McpStateStore(
        state_root=tmp_path / "mcp-limit",
        document_path=tmp_path / "mcp-limit" / "state.json",
        invalid_path_reason="mcp_state_path_invalid",
        max_document_bytes=4,
    )
    with store.locked():
        store.write_bytes(b"1234")
        with pytest.raises(ValueError, match="^mcp_state_payload_limit_exceeded$"):
            store.write_bytes(b"12345")
        assert store.read_bytes() == b"1234"
    assert store.max_document_bytes == 4
    with pytest.raises(AttributeError):
        store.max_document_bytes = 5  # type: ignore[misc]
    with pytest.raises(ValueError, match="max_document_bytes"):
        state_store_module.McpStateStore(
            state_root=tmp_path / "bad-limit",
            document_path=tmp_path / "bad-limit" / "state.json",
            invalid_path_reason="mcp_state_path_invalid",
            max_document_bytes=True,  # type: ignore[arg-type]
        )


def test_app_registry_count_and_exact_canonical_write_limits_preflight_atomically(
    tmp_path: Path,
) -> None:
    left = _trusted_remote_record(tmp_path / "left-limit", "example.remote.left")
    right = _trusted_remote_record(tmp_path / "right-limit", "example.remote.right")
    registry = McpAppRegistry(
        state_root=tmp_path / "bounded",
        max_records=1,
        max_document_bytes=2_048,
    )
    installed = registry.install(left, expected_revision=registry.snapshot().revision)
    durable = registry.registry_path.read_bytes()

    with pytest.raises(McpAppRegistryError, match="^mcp_app_registry_limit_exceeded$"):
        registry.install(right, expected_revision=installed.revision)
    assert registry.registry_path.read_bytes() == durable
    assert registry.snapshot() == installed

    tiny = McpAppRegistry(
        state_root=tmp_path / "tiny",
        max_records=2,
        max_document_bytes=16,
    )
    before = tiny.snapshot()
    with pytest.raises(McpAppRegistryError, match="^mcp_app_registry_limit_exceeded$"):
        tiny.install(left, expected_revision=before.revision)
    assert tiny.snapshot() == before
