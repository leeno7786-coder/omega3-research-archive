"""Immutable MCP package installation and content-free registry tests."""

from __future__ import annotations

import base64
import json
import hashlib
import multiprocessing
import os
import threading
from dataclasses import FrozenInstanceError
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.file_lock import file_lock
import mcp_platform.registry as registry_module
import mcp_platform.state_store as state_store_module
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, McpPackageManifest
from mcp_platform.package_verifier import VerifiedPackage, verify_package
from mcp_platform.registry import (
    McpPackageNotInstalled,
    McpRegistryError,
    McpRegistryUnreadable,
    PackageIdentityCollision,
    PackageRegistry,
)
from mcp_platform.trust_store import TrustedKeyRing
from tests.mcp_platform.test_package_verifier import (
    _resource_catalog,
    _tool_catalog,
    ephemeral_ring,
    host_tag,
    rewrite_and_resign_manifest,
)


Verifier = Callable[[Path], VerifiedPackage]


def build_signed_package(
    root: Path,
    private: Ed25519PrivateKey,
    *,
    signing_key_id: str,
) -> Path:
    """Build the focused registry fixture at the one current MCP revision."""
    package = root / "signed-package"
    package.mkdir()
    artifact = b"fake executable artifact\n"
    tools = _tool_catalog()
    resources = _resource_catalog()
    (package / "omega-mcp-fake.exe").write_bytes(artifact)
    (package / "tool_catalog.json").write_bytes(canonical_bytes(tools))
    (package / "resource_catalog.json").write_bytes(canonical_bytes(resources))
    document: dict[str, object] = {
        "manifest_version": 1,
        "server_id": "omega.first_party.fake",
        "publisher_id": "omega.first_party",
        "server_version": "1.0.0",
        "minimum_omega_version": "3.0.0",
        "supported_mcp_protocol_versions": [MCP_PROTOCOL_VERSION],
        "network_class": "local_only",
        "platform": host_tag(),
        "entrypoint": {"artifact": "omega-mcp-fake.exe", "arguments": []},
        "artifact_sha256": sha256_hex(artifact),
        "tool_catalog_sha256": sha256_hex(canonical_bytes(tools)),
        "resource_catalog_sha256": sha256_hex(canonical_bytes(resources)),
        "capability_tags": ["mail.search"],
        "risk_declarations": ["read_only"],
        "oauth_providers": [],
        "allowed_network_destinations": [],
        "signing_key_id": signing_key_id,
        "signature": base64.b64encode(b"0" * 64).decode("ascii"),
    }
    unsigned = McpPackageManifest.from_dict(document)
    document["signature"] = base64.b64encode(private.sign(unsigned.signing_bytes())).decode("ascii")
    (package / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    return package


def _enable_with_revision_in_process(
    state_root: str,
    expected_revision: str,
    ready: object,
    release: object,
    outcomes: object,
) -> None:
    ready.set()
    release.wait(timeout=5.0)
    try:
        result = PackageRegistry(state_root=Path(state_root)).set_enabled(
            "omega.first_party.fake",
            True,
            expected_revision=expected_revision,
        )
    except McpRegistryError as exc:
        outcomes.put(("error", str(exc)))
    else:
        outcomes.put(("ok", result.revision))


@pytest.fixture
def signing_material() -> tuple[Ed25519PrivateKey, TrustedKeyRing]:
    return ephemeral_ring()


@pytest.fixture
def signed_package(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
) -> Path:
    private, _ring = signing_material
    return build_signed_package(tmp_path, private, signing_key_id="test-release")


@pytest.fixture
def verifier(signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing]) -> Verifier:
    _private, ring = signing_material

    def _verify(package_dir: Path) -> VerifiedPackage:
        return verify_package(
            package_dir,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )

    return _verify


def _retarget_package(
    package: Path,
    private: Ed25519PrivateKey,
    *,
    server_id: str,
    server_version: str = "1.0.0",
) -> None:
    tools_path = package / "tool_catalog.json"
    tools = json.loads(tools_path.read_text(encoding="utf-8"))
    tools["server_id"] = server_id
    tools["server_version"] = server_version
    tools_path.write_bytes(canonical_bytes(tools))

    resources_path = package / "resource_catalog.json"
    resources = json.loads(resources_path.read_text(encoding="utf-8"))
    resources["server_id"] = server_id
    resources["server_version"] = server_version
    resources_path.write_bytes(canonical_bytes(resources))

    rewrite_and_resign_manifest(
        package,
        private,
        {
            "server_id": server_id,
            "server_version": server_version,
            "tool_catalog_sha256": sha256_hex(canonical_bytes(tools)),
            "resource_catalog_sha256": sha256_hex(canonical_bytes(resources)),
        },
    )


def _versioned_package(
    root: Path,
    private: Ed25519PrivateKey,
    version: str,
) -> Path:
    root.mkdir()
    package = build_signed_package(root, private, signing_key_id="test-release")
    _retarget_package(
        package,
        private,
        server_id="omega.first_party.fake",
        server_version=version,
    )
    return package


def test_public_reverify_installed_returns_only_current_registered_signed_bytes(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    """Catches callers manufacturing InstalledPackageRecord/VerifiedPackage authority."""
    registry = PackageRegistry(state_root=tmp_path / "state", verifier=verifier)
    installed = registry.install(signed_package)

    verified = registry.reverify_installed(installed.server_id)

    assert type(verified) is VerifiedPackage
    assert verified.manifest.server_id == installed.server_id
    assert verified.manifest_sha256 == installed.manifest_digest

    installed_manifest = registry.package_path(installed) / "manifest.json"
    installed_manifest.write_bytes(b"{}")
    with pytest.raises(PackageIdentityCollision, match="^package_identity_collision$"):
        registry.reverify_installed(installed.server_id)


def test_install_rejects_signed_downgrade_before_staging_or_publication(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
) -> None:
    private, _ring = signing_material
    newer = _versioned_package(tmp_path / "newer", private, "2.0.0")
    older = _versioned_package(tmp_path / "older", private, "1.9.9")
    registry = PackageRegistry(state_root=tmp_path / "state", verifier=verifier)
    registry.install(newer)

    with pytest.raises(McpRegistryError, match="^package_downgrade_rejected$"):
        registry.install(older)

    assert registry.get("omega.first_party.fake").server_version == "2.0.0"
    assert not list(registry.packages_root.glob(".installing-*"))
    assert not (registry.packages_root / "omega.first_party.fake" / "1.9.9").exists()


@pytest.mark.parametrize("invalid_version", ["release-next", "1.0..1"])
def test_install_rejects_invalid_candidate_version_content_free(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
    invalid_version: str,
) -> None:
    private, _ring = signing_material
    current = _versioned_package(tmp_path / "current", private, "1.0.0")
    invalid = _versioned_package(tmp_path / "invalid", private, invalid_version)
    registry = PackageRegistry(state_root=tmp_path / "state", verifier=verifier)
    registry.install(current)

    with pytest.raises(McpRegistryError, match="^package_version_invalid$") as caught:
        registry.install(invalid)

    assert invalid_version not in str(caught.value)
    assert registry.get("omega.first_party.fake").server_version == "1.0.0"


def test_install_rejects_invalid_current_version_content_free(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
) -> None:
    private, _ring = signing_material
    current = _versioned_package(tmp_path / "current", private, "1.0.0")
    candidate = _versioned_package(tmp_path / "candidate", private, "2.0.0")
    registry = PackageRegistry(state_root=tmp_path / "state", verifier=verifier)
    registry.install(current)
    document = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    document["packages"]["omega.first_party.fake"]["server_version"] = "release-current"
    registry.registry_path.write_bytes(canonical_bytes(document))

    with pytest.raises(McpRegistryError, match="^package_version_invalid$") as caught:
        registry.install(candidate)

    assert "release-current" not in str(caught.value)
    assert json.loads(registry.registry_path.read_text(encoding="utf-8")) == document


def test_install_allows_a_higher_signed_version(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
) -> None:
    private, _ring = signing_material
    current = _versioned_package(tmp_path / "current", private, "1.9.9")
    candidate = _versioned_package(tmp_path / "candidate", private, "2.0.0")
    registry = PackageRegistry(state_root=tmp_path / "state", verifier=verifier)
    registry.install(current)

    installed = registry.install(candidate)

    assert installed.server_version == "2.0.0"
    assert registry.get("omega.first_party.fake").server_version == "2.0.0"


def test_install_verifies_stages_reverifies_and_publishes(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    verified_paths: list[Path] = []

    def recording_verifier(package_dir: Path) -> VerifiedPackage:
        verified_paths.append(Path(package_dir))
        return verifier(package_dir)

    registry = PackageRegistry(
        state_root=tmp_path / "guardian" / "state" / "mcp",
        verifier=recording_verifier,
    )

    record = registry.install(signed_package)

    destination = (
        registry.packages_root / record.server_id / record.server_version / record.platform
    )
    assert record.server_id == "omega.first_party.fake"
    assert record.enabled is False
    assert record.status == "installed"
    assert record.blocker is None
    assert record.crash_count == 0
    assert destination.is_dir()
    assert verified_paths[0] == signed_package
    assert verified_paths[1].name.startswith(".installing-")
    assert verified_paths[1].parent == registry.packages_root
    assert len(verified_paths) == 2


def test_install_rejects_undeclared_source_files(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    (signed_package / "undeclared.txt").write_text("must not install", encoding="utf-8")
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)

    with pytest.raises(ValueError, match="^package_file_set_mismatch$"):
        registry.install(signed_package)

    assert not registry.registry_path.exists()


def test_install_rejects_source_reparse_directory(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    try:
        os.symlink(outside, signed_package / "linked-directory", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)

    with pytest.raises(ValueError, match="^package_file_escape$"):
        registry.install(signed_package)

    assert not registry.registry_path.exists()


def test_failed_staged_reverification_publishes_nothing(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    calls = 0

    def corrupt_staged_copy(package_dir: Path) -> VerifiedPackage:
        nonlocal calls
        calls += 1
        if calls == 2:
            (package_dir / "omega-mcp-fake.exe").write_bytes(b"changed after staging")
        return verifier(package_dir)

    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=corrupt_staged_copy)

    with pytest.raises(Exception, match="artifact_digest_mismatch"):
        registry.install(signed_package)

    assert not registry.registry_path.exists()
    assert not any(registry.packages_root.glob(".installing-*"))
    assert not (registry.packages_root / "omega.first_party.fake").exists()


def test_existing_identical_install_is_idempotent_and_preserves_state(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(signed_package)
    registry.set_enabled(
        "omega.first_party.fake",
        True,
        expected_revision=registry.snapshot().revision,
    )
    registry.block("omega.first_party.fake", "operator_blocked")

    record = registry.install(signed_package)

    assert record.enabled is True
    assert record.blocker == "operator_blocked"
    assert record.crash_count == 0


def test_existing_install_with_undeclared_file_is_not_idempotent(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    record = registry.install(signed_package)
    (registry.package_path(record) / "undeclared.txt").write_text(
        "altered installed tree",
        encoding="utf-8",
    )

    with pytest.raises(PackageIdentityCollision, match="^package_identity_collision$"):
        registry.install(signed_package)


def test_changed_bytes_under_same_identity_are_a_collision(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
) -> None:
    private, _ring = signing_material
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = build_signed_package(first_root, private, signing_key_id="test-release")
    second = build_signed_package(second_root, private, signing_key_id="test-release")
    changed_artifact = b"different valid executable artifact\n"
    (second / "omega-mcp-fake.exe").write_bytes(changed_artifact)
    rewrite_and_resign_manifest(
        second,
        private,
        {"artifact_sha256": sha256_hex(changed_artifact)},
    )
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    original = registry.install(first)

    with pytest.raises(PackageIdentityCollision, match="^package_identity_collision$"):
        registry.install(second)

    assert registry.get(original.server_id) == original
    assert (registry.package_path(original) / "omega-mcp-fake.exe").read_bytes() != changed_artifact


@pytest.mark.parametrize("server_id", ["../escape", "CON", "omega.first_party.fake."])
def test_identity_path_escape_or_windows_alias_is_rejected_before_publication(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
    server_id: str,
) -> None:
    private, _ring = signing_material
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    _retarget_package(package, private, server_id=server_id)
    registry = PackageRegistry(state_root=tmp_path / "state" / "mcp", verifier=verifier)

    with pytest.raises(ValueError, match="^invalid_package_identity$"):
        registry.install(package)

    assert not (tmp_path / "state" / "escape").exists()
    assert not registry.registry_path.exists()


def test_manifest_symlink_escape_is_rejected(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    outside = tmp_path / "outside-manifest.json"
    manifest = signed_package / "manifest.json"
    outside.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        os.symlink(outside, manifest)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)

    with pytest.raises(ValueError, match="^package_file_escape$"):
        registry.install(signed_package)

    assert not registry.registry_path.exists()


def test_registry_corruption_never_reads_as_empty(tmp_path: Path, verifier: Verifier) -> None:
    path = tmp_path / "mcp" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt", encoding="utf-8")
    registry = PackageRegistry(state_root=path.parent, verifier=verifier)

    with pytest.raises(McpRegistryUnreadable, match="^mcp_registry_unreadable$"):
        registry.list_records()


def test_missing_registry_snapshot_has_canonical_empty_revision_and_is_immutable(
    tmp_path: Path,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp")

    snapshot = registry.snapshot()

    assert snapshot.records == ()
    assert snapshot.revision == ("f7148486ad8e40fa2e860301121842422a2d06d34e43f25545397ed4d3ec429d")
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = "0" * 64


def test_snapshot_revision_is_rfc8785_registry_digest_independent_of_install_order(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
) -> None:
    private, _ring = signing_material
    first_root = tmp_path / "first-source"
    second_root = tmp_path / "second-source"
    first_root.mkdir()
    second_root.mkdir()
    first = build_signed_package(first_root, private, signing_key_id="test-release")
    second = build_signed_package(second_root, private, signing_key_id="test-release")
    _retarget_package(second, private, server_id="omega.first_party.second")
    left = PackageRegistry(state_root=tmp_path / "left", verifier=verifier)
    right = PackageRegistry(state_root=tmp_path / "right", verifier=verifier)
    left.install(first)
    left.install(second)
    right.install(second)
    right.install(first)

    left_snapshot = left.snapshot()
    right_snapshot = right.snapshot()

    document = {
        "schema_version": 1,
        "packages": {record.server_id: record.to_dict() for record in left_snapshot.records},
    }
    expected = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert [record.server_id for record in left_snapshot.records] == [
        "omega.first_party.fake",
        "omega.first_party.second",
    ]
    assert left_snapshot == right_snapshot
    assert left_snapshot.revision == expected


def test_set_enabled_compares_revision_writes_once_and_returns_immutable_result(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    installed = registry.install(signed_package)
    before = registry.snapshot()
    writes: list[dict[str, object]] = []
    original_write = registry._write

    def record_write(records: dict[str, object]) -> None:
        writes.append(dict(records))
        original_write(records)  # type: ignore[arg-type]

    monkeypatch.setattr(registry, "_write", record_write)

    result = registry.set_enabled(
        installed.server_id,
        True,
        expected_revision=before.revision,
    )

    assert result.record.enabled is True
    assert result.revision == registry.snapshot().revision
    assert result.revision != before.revision
    assert len(writes) == 1
    with pytest.raises(FrozenInstanceError):
        result.revision = "0" * 64


@pytest.mark.parametrize(
    "expected_revision",
    [None, b"0" * 64, True, "A" * 64, "0" * 63, "0" * 65],
)
def test_set_enabled_requires_exact_lowercase_sha256_revision(
    tmp_path: Path,
    expected_revision: object,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp")

    with pytest.raises(ValueError, match="^invalid_registry_revision$"):
        registry.set_enabled(
            "omega.first_party.fake",
            True,
            expected_revision=expected_revision,
        )


@pytest.mark.parametrize("enabled", [None, 0, 1, "true"])
def test_set_enabled_requires_an_exact_boolean(
    tmp_path: Path,
    enabled: object,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp")

    with pytest.raises(ValueError, match="^invalid_enabled_state$"):
        registry.set_enabled(
            "omega.first_party.fake",
            enabled,
            expected_revision="0" * 64,
        )


def test_stale_or_unknown_set_enabled_never_changes_durable_registry(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    installed = registry.install(signed_package)
    stale = registry.snapshot()
    current = registry.set_enabled(
        installed.server_id,
        True,
        expected_revision=stale.revision,
    )
    durable = registry.registry_path.read_bytes()

    with pytest.raises(McpRegistryError, match="^mcp_registry_conflict$"):
        registry.set_enabled(
            installed.server_id,
            False,
            expected_revision=stale.revision,
        )
    assert registry.registry_path.read_bytes() == durable

    with pytest.raises(McpPackageNotInstalled, match="^package_not_installed$"):
        registry.set_enabled(
            "omega.first_party.missing",
            False,
            expected_revision=current.revision,
        )
    assert registry.registry_path.read_bytes() == durable


def test_cross_process_set_enabled_allows_one_current_revision_writer(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(signed_package)
    expected_revision = registry.snapshot().revision
    context = multiprocessing.get_context("spawn")
    ready = [context.Event(), context.Event()]
    release = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_enable_with_revision_in_process,
            args=(
                str(registry.state_root),
                expected_revision,
                ready[index],
                release,
                outcomes,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    # A cold interpreter start in the freshly spawned child can legitimately
    # take longer than a few seconds; bound the wait generously and name the
    # exact child that never became ready rather than failing on a tight
    # fixed bound that does not distinguish "slow" from "stuck".
    child_ready_timeout_seconds = 60.0
    for index, event in enumerate(ready):
        if not event.wait(timeout=child_ready_timeout_seconds):
            pytest.fail(
                f"child process {index} did not signal ready within "
                f"{child_ready_timeout_seconds}s"
            )
    release.set()
    results = [outcomes.get(timeout=10.0) for _process in processes]
    for process in processes:
        process.join(timeout=10.0)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(kind for kind, _value in results) == ["error", "ok"]
    assert ("error", "mcp_registry_conflict") in results
    assert registry.get("omega.first_party.fake").enabled is True


def test_public_registry_reads_wait_for_the_sidecar_lock(
    tmp_path: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    read_finished = threading.Event()
    result: list[dict[str, object]] = []

    def hold_lock() -> None:
        with file_lock(registry.registry_path):
            lock_acquired.set()
            release_lock.wait(timeout=5.0)

    def read_registry() -> None:
        result.append(registry.load())
        read_finished.set()

    holder = threading.Thread(target=hold_lock)
    reader = threading.Thread(target=read_registry)
    holder.start()
    assert lock_acquired.wait(timeout=2.0)
    reader.start()
    try:
        assert not read_finished.wait(timeout=0.2)
    finally:
        release_lock.set()
        holder.join(timeout=2.0)
        reader.join(timeout=2.0)

    assert read_finished.is_set()
    assert result == [{}]


@pytest.mark.parametrize(
    "operation",
    ["load", "set_enabled", "block", "clear_block", "install"],
)
def test_registry_operations_reject_reparse_lock_sidecar(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
    operation: str,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(signed_package)
    registry_before = registry.registry_path.read_bytes()
    lock_path = Path(f"{registry.registry_path}.lock")
    lock_path.unlink()
    outside_lock = tmp_path / "outside.lock"
    outside_before = b"outside lock sentinel"
    outside_lock.write_bytes(outside_before)
    try:
        os.symlink(outside_lock, lock_path)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="^package_file_escape$"):
        if operation == "load":
            registry.load()
        elif operation == "set_enabled":
            registry.set_enabled(
                "omega.first_party.fake",
                True,
                expected_revision=registry.snapshot().revision,
            )
        elif operation == "block":
            registry.block("omega.first_party.fake", "operator_blocked")
        elif operation == "clear_block":
            registry.clear_block("omega.first_party.fake")
        else:
            registry.install(signed_package)

    assert registry.registry_path.read_bytes() == registry_before
    assert outside_lock.read_bytes() == outside_before


@pytest.mark.parametrize("preinstalled", [False, True])
def test_install_rejects_unsafe_sidecar_without_touching_target(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
    preinstalled: bool,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    if preinstalled:
        registry.install(signed_package)
    else:
        registry.state_root.mkdir(parents=True)
    registry_before = (
        registry.registry_path.read_bytes() if registry.registry_path.exists() else None
    )
    lock_path = Path(f"{registry.registry_path}.lock")
    lock_path.unlink(missing_ok=True)
    outside_lock = tmp_path / "outside-pre-lock.lock"
    outside_before = b"must never be opened"
    outside_lock.write_bytes(outside_before)
    try:
        os.symlink(outside_lock, lock_path)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="^package_file_escape$"):
        registry.install(signed_package)

    after = registry.registry_path.read_bytes() if registry.registry_path.exists() else None
    assert after == registry_before
    assert outside_lock.read_bytes() == outside_before


@pytest.mark.parametrize("reparse_surface", ["state_root", "packages_root"])
def test_registry_reads_reject_reparse_durable_roots(
    tmp_path: Path,
    verifier: Verifier,
    reparse_surface: str,
) -> None:
    real_state = tmp_path / "real-state"
    real_registry = PackageRegistry(state_root=real_state, verifier=verifier)
    real_state.mkdir()
    if reparse_surface == "state_root":
        alias = tmp_path / "state-alias"
        try:
            os.symlink(real_state, alias, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        registry = PackageRegistry(state_root=alias, verifier=verifier)
    else:
        outside_packages = tmp_path / "outside-packages"
        outside_packages.mkdir()
        try:
            os.symlink(
                outside_packages,
                real_registry.packages_root,
                target_is_directory=True,
            )
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        registry = real_registry

    with pytest.raises(ValueError, match="^package_file_escape$"):
        registry.load()


@pytest.mark.parametrize("mutation", ["set_enabled", "block", "clear_block"])
def test_registry_mutations_reject_reparse_state_root(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
    mutation: str,
) -> None:
    real_state = tmp_path / "real-state"
    real_registry = PackageRegistry(state_root=real_state, verifier=verifier)
    real_registry.install(signed_package)
    alias = tmp_path / "state-alias"
    try:
        os.symlink(real_state, alias, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    registry = PackageRegistry(state_root=alias, verifier=verifier)

    with pytest.raises(ValueError, match="^package_file_escape$"):
        if mutation == "set_enabled":
            registry.set_enabled(
                "omega.first_party.fake",
                True,
                expected_revision=registry.snapshot().revision,
            )
        elif mutation == "block":
            registry.block("omega.first_party.fake", "operator_blocked")
        else:
            registry.clear_block("omega.first_party.fake")


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"schema_version": 1, "packages": {}, "extra": True},
        {"schema_version": 2, "packages": {}},
        {"schema_version": True, "packages": {}},
        {"schema_version": 1.0, "packages": {}},
        {"schema_version": 1, "packages": []},
        {
            "schema_version": 1,
            "packages": {
                "wrong-key": {
                    "server_id": "omega.first_party.fake",
                    "server_version": "1.0.0",
                    "platform": "windows-x86_64",
                    "publisher_id": "omega.first_party",
                    "signing_key_id": "test-release",
                    "manifest_digest": "a" * 64,
                    "artifact_sha256": "b" * 64,
                    "tool_catalog_sha256": "c" * 64,
                    "resource_catalog_sha256": "d" * 64,
                    "network_class": "local_only",
                    "enabled": False,
                    "status": "installed",
                    "blocker": None,
                    "crash_count": 0,
                }
            },
        },
    ],
)
def test_registry_schema_is_exact_and_fail_closed(
    tmp_path: Path,
    verifier: Verifier,
    document: dict[str, object],
) -> None:
    path = tmp_path / "mcp" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    registry = PackageRegistry(state_root=path.parent, verifier=verifier)

    with pytest.raises(McpRegistryUnreadable, match="^mcp_registry_unreadable$"):
        registry.load()


@pytest.mark.parametrize("schema_version", [True, 1.0])
@pytest.mark.parametrize("operation", ["load", "snapshot", "cas"])
def test_registry_schema_version_requires_exact_integer_for_every_read_boundary(
    tmp_path: Path,
    verifier: Verifier,
    schema_version: object,
    operation: str,
) -> None:
    path = tmp_path / "mcp" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": schema_version, "packages": {}}),
        encoding="utf-8",
    )
    registry = PackageRegistry(state_root=path.parent, verifier=verifier)

    with pytest.raises(McpRegistryUnreadable, match="^mcp_registry_unreadable$"):
        if operation == "load":
            registry.load()
        elif operation == "snapshot":
            registry.snapshot()
        else:
            registry.set_enabled(
                "omega.first_party.missing",
                True,
                expected_revision="0" * 64,
            )


def test_registry_contains_no_package_content_or_secret_fields(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(signed_package)

    document = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    serialized = json.dumps(document).lower()

    assert set(document) == {"schema_version", "packages"}
    assert set(document["packages"]["omega.first_party.fake"]) == {
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
    for forbidden in (
        "content",
        "token",
        "secret",
        "password",
        "email_body",
        "document_text",
        "arguments",
        "command",
        "url",
        "account",
        "tenant",
    ):
        assert forbidden not in serialized


def test_state_changes_preserve_unrelated_records_and_require_installed_identity(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
) -> None:
    private, _ring = signing_material
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = build_signed_package(first_root, private, signing_key_id="test-release")
    second = build_signed_package(second_root, private, signing_key_id="test-release")
    _retarget_package(second, private, server_id="omega.first_party.second")
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(first)
    second_before = registry.install(second)

    enabled = registry.set_enabled(
        "omega.first_party.fake",
        True,
        expected_revision=registry.snapshot().revision,
    ).record
    blocked = registry.block("omega.first_party.fake", "repeated_crash", increment_crash=True)
    cleared = registry.clear_block("omega.first_party.fake")

    assert enabled.enabled is True
    assert blocked.blocker == "repeated_crash"
    assert blocked.crash_count == 1
    assert cleared.enabled is True
    assert cleared.blocker is None
    assert cleared.crash_count == 0
    assert registry.get("omega.first_party.second") == second_before
    with pytest.raises(McpPackageNotInstalled, match="^package_not_installed$"):
        registry.set_enabled(
            "omega.first_party.missing",
            True,
            expected_revision=registry.snapshot().revision,
        )


def test_record_crash_increments_without_blocking_until_reason_is_supplied(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    # Catches crash accounting that either stays process-local or blocks on crash one.
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(signed_package)

    arguments = {
        "permanent_block_at": 3,
        "permanent_blocker": "repeated_crash",
    }
    first = registry.record_crash("omega.first_party.fake", **arguments)
    second = registry.record_crash("omega.first_party.fake", **arguments)
    third = registry.record_crash("omega.first_party.fake", **arguments)

    assert (first.crash_count, first.blocker) == (1, None)
    assert (second.crash_count, second.blocker) == (2, None)
    assert (third.crash_count, third.blocker) == (3, "repeated_crash")
    assert registry.get("omega.first_party.fake") == third


def test_record_crash_threshold_is_atomic_under_contention(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    # Catches a get/increment/block split that can lose the third-crash decision.
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(signed_package)
    errors: list[BaseException] = []

    def record() -> None:
        try:
            registry.record_crash(
                "omega.first_party.fake",
                permanent_block_at=3,
                permanent_blocker="repeated_crash",
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=record) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    record_after = registry.get("omega.first_party.fake")
    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert record_after.crash_count == 12
    assert record_after.blocker == "repeated_crash"


@pytest.mark.parametrize(
    "arguments",
    [
        {"permanent_block_at": 0, "permanent_blocker": "repeated_crash"},
        {"permanent_block_at": True, "permanent_blocker": "repeated_crash"},
        {"permanent_block_at": 3, "permanent_blocker": "INVALID BLOCKER"},
    ],
)
def test_record_crash_rejects_invalid_threshold_or_blocker(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
    arguments: dict[str, object],
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    registry.install(signed_package)

    with pytest.raises(ValueError):
        registry.record_crash("omega.first_party.fake", **arguments)

    assert registry.get("omega.first_party.fake").crash_count == 0


def test_record_crash_requires_an_installed_identity(
    tmp_path: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)

    with pytest.raises(McpPackageNotInstalled, match="^package_not_installed$"):
        registry.record_crash(
            "omega.first_party.missing",
            permanent_block_at=3,
            permanent_blocker="repeated_crash",
        )


def test_package_registry_uses_shared_atomic_replace_then_parent_fsync(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches the PackageRegistry retaining a second atomic-store implementation.
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
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

    registry.install(signed_package)

    assert events == [
        ("replace", registry.registry_path),
        ("fsync", registry.registry_path.parent),
    ]


def test_package_registry_atomic_cancellation_preserves_prior_bytes_and_identity(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches a wrapper that masks BaseException or publishes a partial registry.
    registry = PackageRegistry(state_root=tmp_path / "mcp", verifier=verifier)
    record = registry.install(signed_package)
    current = registry.snapshot()
    durable = registry.registry_path.read_bytes()

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled()

    real_replace = state_store_module.PinnedDirectory.replace

    def cancel_replace(directory, source_name, target_name):
        if str(source_name).startswith(".registry.json."):
            raise cancellation
        return real_replace(directory, source_name, target_name)

    monkeypatch.setattr(state_store_module.PinnedDirectory, "replace", cancel_replace)

    with pytest.raises(Cancelled) as caught:
        registry.set_enabled(
            record.server_id,
            True,
            expected_revision=current.revision,
        )

    assert caught.value is cancellation
    assert registry.registry_path.read_bytes() == durable
    assert list(registry.state_root.glob(".*.part")) == []


def test_default_registry_path_is_isolated_from_live_guardian_state(
    tmp_path: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(verifier=verifier)

    assert registry.state_root == tmp_path / "guardian" / "state" / "mcp"


def test_package_registry_count_limit_precedes_second_verification_and_staging(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
) -> None:
    private, _ring = signing_material
    first = _versioned_package(tmp_path / "first-limit", private, "1.0.0")
    second_root = tmp_path / "second-limit"
    second_root.mkdir()
    second = build_signed_package(second_root, private, signing_key_id="test-release")
    _retarget_package(
        second,
        private,
        server_id="omega.first_party.second",
        server_version="1.0.0",
    )
    calls: list[Path] = []

    def counting_verifier(path: Path) -> VerifiedPackage:
        calls.append(path)
        return verifier(path)

    registry = PackageRegistry(
        state_root=tmp_path / "bounded",
        verifier=counting_verifier,
        max_records=1,
        max_document_bytes=4_096,
    )
    registry.install(first)
    calls_before = len(calls)
    durable = registry.registry_path.read_bytes()

    with pytest.raises(McpRegistryError, match="^package_registry_limit_exceeded$"):
        registry.install(second)
    assert len(calls) == calls_before
    assert registry.registry_path.read_bytes() == durable
    assert not list(registry.packages_root.glob(".installing-*"))


def test_package_registry_count_limit_still_allows_same_identity_upgrade(
    tmp_path: Path,
    signing_material: tuple[Ed25519PrivateKey, TrustedKeyRing],
    verifier: Verifier,
) -> None:
    private, _ring = signing_material
    first = _versioned_package(tmp_path / "first-upgrade", private, "1.0.0")
    second = _versioned_package(tmp_path / "second-upgrade", private, "2.0.0")
    registry = PackageRegistry(
        state_root=tmp_path / "bounded-upgrade",
        verifier=verifier,
        max_records=1,
        max_document_bytes=4_096,
    )
    registry.install(first)

    upgraded = registry.install(second)

    assert upgraded.server_version == "2.0.0"
    assert len(registry.load()) == 1


def test_package_registry_exact_canonical_write_limit_preserves_prior_bytes(
    tmp_path: Path,
    signed_package: Path,
    verifier: Verifier,
) -> None:
    registry = PackageRegistry(
        state_root=tmp_path / "tiny",
        verifier=verifier,
        max_records=1,
        max_document_bytes=16,
    )
    before = registry.snapshot()
    with pytest.raises(McpRegistryError, match="^package_registry_limit_exceeded$"):
        registry.install(signed_package)
    assert registry.snapshot() == before
    assert not registry.registry_path.exists()
    assert not list(registry.packages_root.glob(".installing-*"))
