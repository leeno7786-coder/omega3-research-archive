"""Focused contracts for immutable plugin staging and registry publication."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
import plugin_platform.registry as registry_module
from plugin_platform.filesystem import PinnedDirectory
from plugin_platform.package_verifier import VerifiedPlugin, verify_plugin_package
from plugin_platform.registry import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginAppBinding,
    PluginMutationResult,
    PluginPublicationObservation,
    PluginRegistry,
    PluginRegistryError,
    PluginRegistrySnapshot,
    StagedPluginInstall,
)
from plugin_platform.trust import TrustedPublisherKeyRing
from tests.plugin_platform.support import build_signed_plugin, ephemeral_publisher_ring


_ORIGINAL_SKILL = b"# Workspace\n\nUse the connected workspace deliberately.\n"
_REPLACEMENT_SKILL = b"# Changed source\n\nThese bytes were not authenticated.\n"


class RegistryMarker(BaseException):
    pass


def _signed_package(
    root: Path,
    private_key: Ed25519PrivateKey,
    *,
    plugin_id: str = "test.workspace",
    plugin_version: str = "1.0.0",
    signing_key_id: str = "test-plugin-key",
    skill_payload: bytes = _ORIGINAL_SKILL,
) -> Path:
    package = build_signed_plugin(root, private_key)
    skill_path = package / "skills" / "workspace" / "SKILL.md"
    skill_path.write_bytes(skill_payload)
    document = json.loads((package / "plugin.json").read_bytes())
    document["plugin_id"] = plugin_id
    document["plugin_version"] = plugin_version
    document["signing_key_id"] = signing_key_id
    document["kind"] = "mcp_backed"
    document["assets"][0]["sha256"] = hashlib.sha256(skill_payload).hexdigest()
    document["entry_points"][0]["required_app_ids"] = ["test.workspace.app"]
    document["required_apps"] = [
        {
            "app_id": "test.workspace.app",
            "publisher_id": "test.publisher",
            "deployment": "local",
            "provider_id": "workspace",
            "protocol_version": "2026-07-28",
            "transports": ["stdio"],
            "remote_origin": None,
            "authorization_issuer": None,
            "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": [
                {
                    "kind": "tool",
                    "name": "workspace.list",
                    "schema_sha256": None,
                    "version": None,
                }
            ],
        }
    ]
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    (package / "plugin.json").write_bytes(canonical_bytes(document))
    return package


def _verifier(ring: TrustedPublisherKeyRing):
    def verify(path: Path) -> VerifiedPlugin:
        return verify_plugin_package(path, ring, "3.0.0")

    return verify


def _verified(
    tmp_path: Path,
    private_key: Ed25519PrivateKey,
    ring: TrustedPublisherKeyRing,
    *,
    name: str = "one",
    plugin_id: str = "test.workspace",
    plugin_version: str = "1.0.0",
    signing_key_id: str = "test-plugin-key",
    skill_payload: bytes = _ORIGINAL_SKILL,
) -> VerifiedPlugin:
    package = _signed_package(
        tmp_path / name,
        private_key,
        plugin_id=plugin_id,
        plugin_version=plugin_version,
        signing_key_id=signing_key_id,
        skill_payload=skill_payload,
    )
    return verify_plugin_package(package, ring, "3.0.0")


def _registry(
    tmp_path: Path,
    ring: TrustedPublisherKeyRing,
) -> PluginRegistry:
    return PluginRegistry(state_root=tmp_path / "plugins", verifier=_verifier(ring))


def _ring_for_key(
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> TrustedPublisherKeyRing:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return TrustedPublisherKeyRing.from_dict(
        {
            "schema_version": 1,
            "keys": [
                {
                    "key_id": key_id,
                    "publisher_id": "test.publisher",
                    "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                }
            ],
            "revoked_key_ids": [],
            "revoked_publisher_ids": [],
            "revoked_subject_ids": [],
        }
    )


def _publish(
    registry: PluginRegistry,
    verified: VerifiedPlugin,
) -> PluginMutationResult:
    staged = registry.stage(verified)
    return registry.publish(
        staged,
        expected_revision=registry.snapshot().revision,
        app_binding=_binding(verified),
    )


def _binding(verified: VerifiedPlugin) -> PluginAppBinding:
    required = tuple(sorted(item.app_id for item in verified.manifest.required_apps))
    optional: tuple[str, ...] = ()
    receipt = "receipt-test"
    revision = hashlib.sha256(
        canonical_bytes(
            {
                "required_app_ids": list(required),
                "optional_app_ids": list(optional),
                "dependency_receipt_id": receipt,
            }
        )
    ).hexdigest()
    return PluginAppBinding(required, optional, receipt, revision)


def _empty_revision() -> str:
    return hashlib.sha256(canonical_bytes({"schema_version": 2, "plugins": {}})).hexdigest()


def test_app_binding_is_closed_canonical_and_survives_registry_restart(tmp_path: Path) -> None:
    """Catches publishing app ownership without durable, authenticated exact IDs."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    binding = PluginAppBinding(
        required_app_ids=("test.workspace.app",),
        optional_app_ids=(),
        dependency_receipt_id="receipt-test",
        revision=hashlib.sha256(
            canonical_bytes(
                {
                    "required_app_ids": ["test.workspace.app"],
                    "optional_app_ids": [],
                    "dependency_receipt_id": "receipt-test",
                }
            )
        ).hexdigest(),
    )

    result = registry.publish(
        registry.stage(verified),
        expected_revision=registry.snapshot().revision,
        app_binding=binding,
    )

    restarted = _registry(tmp_path, ring).get("test.workspace")
    assert result.record.app_binding == binding
    assert restarted.app_binding == binding
    assert (
        binding.revision
        == hashlib.sha256(
            canonical_bytes(
                {
                    "required_app_ids": ["test.workspace.app"],
                    "optional_app_ids": [],
                    "dependency_receipt_id": "receipt-test",
                }
            )
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"required_app_ids": ("test.workspace.app", "test.workspace.app")},
        {"optional_app_ids": ("test.workspace.app",)},
        {"revision": "f" * 64},
    ],
)
def test_app_binding_rejects_duplicate_overlap_and_tampered_revision(
    changes: dict[str, object],
) -> None:
    """Catches accepting ambiguous app ownership or an altered binding receipt."""
    binding: dict[str, object] = {
        "required_app_ids": ("test.workspace.app",),
        "optional_app_ids": (),
        "dependency_receipt_id": "receipt-test",
        "revision": hashlib.sha256(
            canonical_bytes(
                {
                    "required_app_ids": ["test.workspace.app"],
                    "optional_app_ids": [],
                    "dependency_receipt_id": "receipt-test",
                }
            )
        ).hexdigest(),
    }
    binding.update(changes)

    with pytest.raises(ValueError, match="^plugin app binding is invalid$"):
        PluginAppBinding(**binding)  # type: ignore[arg-type]


def test_app_binding_rejects_a_disjointness_violation_with_a_valid_revision() -> None:
    """Catches optional ownership overlapping an otherwise authentic required owner."""
    with pytest.raises(ValueError, match="^plugin app binding is invalid$"):
        PluginAppBinding(
            required_app_ids=("test.workspace.app",),
            optional_app_ids=("test.workspace.app",),
            dependency_receipt_id="receipt-test",
            revision=hashlib.sha256(
                canonical_bytes(
                    {
                        "required_app_ids": ["test.workspace.app"],
                        "optional_app_ids": ["test.workspace.app"],
                        "dependency_receipt_id": "receipt-test",
                    }
                )
            ).hexdigest(),
        )


def test_registry_rejects_an_oversize_publication_before_writing_unreadable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches write-success/read-failure registry documents at the durable boundary."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    monkeypatch.setattr(registry_module, "_MAX_REGISTRY_BYTES", 1)

    with pytest.raises(PluginRegistryError, match="^plugin_registry_limit_exceeded$"):
        registry.publish(
            registry.stage(verified),
            expected_revision=registry.snapshot().revision,
            app_binding=_binding(verified),
        )

    assert registry.registry_path.exists() is False


def _publication_identity(verified: VerifiedPlugin) -> dict[str, str]:
    return {
        "plugin_id": verified.manifest.plugin_id,
        "publisher_id": verified.manifest.publisher_id,
        "plugin_version": verified.manifest.plugin_version,
        "signing_key_id": verified.manifest.signing_key_id,
        "manifest_sha256": verified.manifest_sha256,
        "package_sha256": verified.package_sha256,
    }


def _assert_error(reason: str, operation: Any) -> None:
    with pytest.raises(PluginRegistryError, match=rf"^{reason}$") as caught:
        operation()
    assert caught.value.__cause__ is None
    assert str(caught.value) == reason


def test_missing_registry_is_one_immutable_empty_snapshot(tmp_path: Path) -> None:
    """Catches treating absent durable state as corruption or returning mutable emptiness."""
    _private_key, ring = ephemeral_publisher_ring()
    snapshot = _registry(tmp_path, ring).snapshot()

    assert snapshot == PluginRegistrySnapshot(records=(), revision=_empty_revision())
    assert isinstance(snapshot.records, tuple)
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = "0" * 64  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"schema_version":1,"plugins":{},"plugins":{}}',
        b'{"plugins":{},"schema_version":1,"unknown":true}',
        b'{"plugins":[],"schema_version":1}',
        b'{ "plugins": {}, "schema_version": 1 }',
    ],
    ids=["invalid-json", "duplicate-key", "extra-field", "wrong-shape", "noncanonical"],
)
def test_corrupt_or_noncanonical_registry_never_reads_as_empty(
    tmp_path: Path,
    payload: bytes,
) -> None:
    """Catches malformed, open, duplicate, or noncanonical durable state becoming empty."""
    _private_key, ring = ephemeral_publisher_ring()
    registry = _registry(tmp_path, ring)
    registry.packages_root.mkdir(parents=True)
    registry.registry_path.write_bytes(payload)

    _assert_error("plugin_registry_unreadable", registry.snapshot)


def test_stage_uses_only_authenticated_payloads_and_does_not_publish(
    tmp_path: Path,
) -> None:
    """Catches reopening mutable provenance or mutating registry state during staging."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    source_asset = verified.package_root / verified.asset_paths["skills/workspace/SKILL.md"]
    source_asset.write_bytes(_REPLACEMENT_SKILL)
    calls: list[Path] = []

    def verifier(path: Path) -> VerifiedPlugin:
        calls.append(path)
        return verify_plugin_package(path, ring, "3.0.0")

    registry = PluginRegistry(state_root=tmp_path / "plugins", verifier=verifier)
    staged = registry.stage(verified)

    assert staged.stage_path.parent == registry.packages_root.absolute()
    assert staged.stage_path.name.startswith(".installing-")
    assert (staged.stage_path / "plugin.json").read_bytes() == canonical_bytes(
        verified.manifest.to_dict()
    )
    assert (staged.stage_path / "skills" / "workspace" / "SKILL.md").read_bytes() == (
        _ORIGINAL_SKILL
    )
    assert calls == [staged.stage_path]
    assert registry.snapshot().records == ()
    assert not registry.registry_path.exists()
    assert staged.candidate == InstalledPluginVersionRecord(
        plugin_version="1.0.0",
        signing_key_id="test-plugin-key",
        manifest_sha256=verified.manifest_sha256,
        package_sha256=verified.package_sha256,
        state="candidate",
    )
    with pytest.raises(FrozenInstanceError):
        staged.stage_path = tmp_path  # type: ignore[misc]


def test_stage_uses_exact_caller_supplied_name_and_remains_recoverable(
    tmp_path: Path,
) -> None:
    """Catches allocating an unjournaled random stage instead of the durable identity."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    stage_name = ".installing-0123456789abcdef0123456789abcdef"
    calls: list[Path] = []

    def verifier(path: Path) -> VerifiedPlugin:
        calls.append(path)
        return verify_plugin_package(path, ring, "3.0.0")

    registry = PluginRegistry(state_root=tmp_path / "plugins", verifier=verifier)

    staged = registry.stage(verified, stage_name=stage_name)

    assert staged.stage_path == registry.packages_root / stage_name
    assert calls == [staged.stage_path]
    recovered = registry.recover_stage(stage_name, **_publication_identity(verified))
    assert recovered == staged
    registry.discard(recovered)
    assert staged.stage_path.exists() is False


def test_stage_default_name_remains_random_and_canonical(tmp_path: Path) -> None:
    """Catches the new deterministic seam weakening ordinary random staging."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)

    first = registry.stage(verified)
    second = registry.stage(verified)

    assert first.stage_path.name != second.stage_path.name
    assert re.fullmatch(r"\.installing-[0-9a-f]{32}", first.stage_path.name)
    assert re.fullmatch(r"\.installing-[0-9a-f]{32}", second.stage_path.name)


@pytest.mark.parametrize(
    "stage_name",
    [
        ".installing-" + "A" * 32,
        ".installing-" + "a" * 31,
        ".installing-" + "a" * 33,
        "../.installing-" + "a" * 32,
        ".installing-" + "a" * 32 + "/child",
        ".installing-" + "a" * 32 + "\\child",
        ".INSTALLING-" + "a" * 32,
        Path(".installing-" + "a" * 32),
        b".installing-" + b"a" * 32,
        None,
    ],
    ids=[
        "uppercase-hex",
        "short",
        "long",
        "traversal",
        "forward-separator",
        "back-separator",
        "case-alias",
        "path-object",
        "bytes-content",
        "none-content",
    ],
)
def test_stage_rejects_noncanonical_supplied_name_before_asset_writes(
    tmp_path: Path,
    stage_name: object,
) -> None:
    """Catches unsafe or provider-shaped stage identities reaching materialization."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)

    _assert_error(
        "plugin_stage_invalid",
        lambda: registry.stage(verified, stage_name=stage_name),  # type: ignore[arg-type]
    )

    assert registry.packages_root.exists() is False


def test_stage_rejects_existing_exact_name_without_deleting_it(
    tmp_path: Path,
) -> None:
    """Catches collision cleanup deleting a stage owned by another transaction."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    registry.snapshot()
    suffix = "abcdef0123456789abcdef0123456789"
    exact_name = f".installing-{suffix}"
    exact = registry.packages_root / exact_name
    exact.mkdir()
    (exact / "owner.txt").write_text("exact", encoding="utf-8")

    _assert_error(
        "plugin_stage_invalid",
        lambda: registry.stage(verified, stage_name=exact_name),
    )

    assert (exact / "owner.txt").read_text(encoding="utf-8") == "exact"


def test_stage_rejects_existing_casefold_alias_without_deleting_it(
    tmp_path: Path,
) -> None:
    """Catches distinct filesystem spellings aliasing one durable transaction name."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    registry.snapshot()
    suffix = "abcdef0123456789abcdef0123456789"
    stage_name = f".installing-{suffix}"
    alias_name = f".INSTALLING-{suffix.upper()}"
    alias = registry.packages_root / alias_name
    alias.mkdir()
    (alias / "owner.txt").write_text("alias", encoding="utf-8")

    _assert_error(
        "plugin_stage_invalid",
        lambda: registry.stage(verified, stage_name=stage_name),
    )

    assert (alias / "owner.txt").read_text(encoding="utf-8") == "alias"
    assert stage_name not in os.listdir(registry.packages_root)


def test_stage_rejects_reparse_at_supplied_name_without_following_or_deleting_target(
    tmp_path: Path,
) -> None:
    """Catches supplied-name collision cleanup traversing a reparse target."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    registry.snapshot()
    stage_name = ".installing-abcdef0123456789abcdef0123456789"
    target = tmp_path / "outside-stage"
    target.mkdir()
    marker = target / "owner.txt"
    marker.write_text("outside", encoding="utf-8")
    try:
        (registry.packages_root / stage_name).symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    _assert_error(
        "plugin_stage_invalid",
        lambda: registry.stage(verified, stage_name=stage_name),
    )

    assert marker.read_text(encoding="utf-8") == "outside"
    assert (registry.packages_root / stage_name).is_symlink()


@pytest.mark.parametrize("mapping", ["paths", "payloads"])
@pytest.mark.parametrize("shape", ["missing", "extra", "mismatched"])
def test_stage_rejects_unmatched_authenticated_mappings_before_filesystem_mutation(
    tmp_path: Path,
    mapping: str,
    shape: str,
) -> None:
    """Catches staging a logical path or payload not authenticated by the manifest."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    key = "skills/workspace/SKILL.md"
    if mapping == "paths":
        values: dict[str, Path] = dict(verified.asset_paths)
        if shape == "missing":
            values.clear()
        elif shape == "extra":
            values["skills/extra/SKILL.md"] = Path("skills/extra/SKILL.md")
        else:
            values[key] = Path("skills/other/SKILL.md")
        forged = replace(verified, asset_paths=MappingProxyType(values))
    else:
        payloads: dict[str, bytes] = dict(verified.asset_bytes)
        if shape == "missing":
            payloads.clear()
        elif shape == "extra":
            payloads["skills/extra/SKILL.md"] = b"# Extra\n"
        else:
            payloads[key] = _REPLACEMENT_SKILL
        forged = replace(verified, asset_bytes=MappingProxyType(payloads))
    state_root = tmp_path / "plugins"

    _assert_error(
        "verified_plugin_payload_mismatch",
        lambda: PluginRegistry(state_root=state_root, verifier=_verifier(ring)).stage(forged),
    )
    assert not state_root.exists()


def test_publish_writes_the_exact_closed_content_free_schema(tmp_path: Path) -> None:
    """Catches persisting plugin instructions, dependencies, URLs, credentials, or authority."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    before = registry.snapshot()
    staged = registry.stage(verified)

    result = registry.publish(
        staged, expected_revision=before.revision, app_binding=_binding(verified)
    )

    expected_record = {
        "plugin_id": "test.workspace",
        "publisher_id": "test.publisher",
        "kind": "mcp_backed",
        "selected_version": "1.0.0",
        "enabled": False,
        "status": "installed",
        "blocker": None,
        "app_binding": _binding(verified).to_dict(),
        "versions": [
            {
                "plugin_version": "1.0.0",
                "signing_key_id": "test-plugin-key",
                "manifest_sha256": verified.manifest_sha256,
                "package_sha256": verified.package_sha256,
                "state": "selected",
            }
        ],
    }
    expected_document = {
        "schema_version": 2,
        "plugins": {"test.workspace": expected_record},
    }
    payload = registry.registry_path.read_bytes()
    assert payload == canonical_bytes(expected_document)
    assert json.loads(payload)["plugins"]["test.workspace"] == expected_record
    assert result == PluginMutationResult(
        record=registry.get("test.workspace"), revision=result.revision
    )
    assert result.revision == hashlib.sha256(payload).hexdigest()
    assert registry.snapshot() == PluginRegistrySnapshot(
        records=(result.record,), revision=result.revision
    )
    assert not staged.stage_path.exists()
    published = registry.packages_root / "test.workspace" / "1.0.0"
    assert published.is_dir()
    forbidden = (
        _ORIGINAL_SKILL,
        b"Help me get started",
        b"required_apps",
        b"workspace.list",
        b"https://",
        b"credential",
        b"authority",
        b"signature",
    )
    assert all(fragment not in payload for fragment in forbidden)


def test_repair_same_version_replaces_only_the_exact_disabled_signed_identity(
    tmp_path: Path,
) -> None:
    """Catches weakening ordinary publication to overwrite same-version bytes."""
    private_key, ring = ephemeral_publisher_ring()
    installed = _verified(tmp_path, private_key, ring, name="installed")
    replacement = _verified(
        tmp_path,
        private_key,
        ring,
        name="replacement",
        skill_payload=_REPLACEMENT_SKILL,
    )
    registry = _registry(tmp_path, ring)
    published = _publish(registry, installed)
    staged = registry.stage(replacement)

    repaired = registry.repair_same_version(
        staged,
        expected_revision=published.revision,
        expected_signing_key_id=installed.manifest.signing_key_id,
        expected_manifest_sha256=installed.manifest_sha256,
        expected_package_sha256=installed.package_sha256,
        app_binding=_binding(replacement),
    )

    selected = next(item for item in repaired.record.versions if item.state == "selected")
    assert repaired.record.enabled is False
    assert selected.manifest_sha256 == replacement.manifest_sha256
    assert selected.package_sha256 == replacement.package_sha256
    assert (
        registry.reverify_installed("test.workspace").asset_bytes["skills/workspace/SKILL.md"]
        == _REPLACEMENT_SKILL
    )
    assert staged.stage_path.exists() is False
    assert not any(
        name.startswith(".repair-")
        for name in os.listdir(registry.packages_root / "test.workspace")
    )


def test_repair_same_version_rejects_an_unexpected_installed_fingerprint(
    tmp_path: Path,
) -> None:
    private_key, ring = ephemeral_publisher_ring()
    installed = _verified(tmp_path, private_key, ring, name="installed")
    replacement = _verified(
        tmp_path,
        private_key,
        ring,
        name="replacement",
        skill_payload=_REPLACEMENT_SKILL,
    )
    registry = _registry(tmp_path, ring)
    published = _publish(registry, installed)
    staged = registry.stage(replacement)

    _assert_error(
        "plugin_repair_identity_mismatch",
        lambda: registry.repair_same_version(
            staged,
            expected_revision=published.revision,
            expected_signing_key_id=installed.manifest.signing_key_id,
            expected_manifest_sha256="0" * 64,
            expected_package_sha256=installed.package_sha256,
            app_binding=_binding(replacement),
        ),
    )

    assert registry.reverify_installed("test.workspace").package_sha256 == (
        installed.package_sha256
    )
    assert staged.stage_path.exists()


def test_repair_same_version_requires_the_plugin_to_be_disabled(tmp_path: Path) -> None:
    private_key, ring = ephemeral_publisher_ring()
    installed = _verified(tmp_path, private_key, ring, name="installed")
    replacement = _verified(
        tmp_path,
        private_key,
        ring,
        name="replacement",
        skill_payload=_REPLACEMENT_SKILL,
    )
    registry = _registry(tmp_path, ring)
    published = _publish(registry, installed)
    enabled = registry.set_enabled(
        "test.workspace",
        True,
        expected_revision=published.revision,
    )
    staged = registry.stage(replacement)

    _assert_error(
        "plugin_repair_requires_disabled",
        lambda: registry.repair_same_version(
            staged,
            expected_revision=enabled.revision,
            expected_signing_key_id=installed.manifest.signing_key_id,
            expected_manifest_sha256=installed.manifest_sha256,
            expected_package_sha256=installed.package_sha256,
            app_binding=_binding(replacement),
        ),
    )

    assert registry.get("test.workspace").enabled is True
    assert registry.reverify_installed("test.workspace").package_sha256 == (
        installed.package_sha256
    )


def test_repair_same_version_can_pin_a_retired_verifier_for_the_exact_old_release(
    tmp_path: Path,
) -> None:
    old_private, old_ring = ephemeral_publisher_ring()
    current_private, current_ring = ephemeral_publisher_ring()
    installed = _verified(tmp_path, old_private, old_ring, name="installed")
    replacement = _verified(
        tmp_path,
        current_private,
        current_ring,
        name="replacement",
        skill_payload=_REPLACEMENT_SKILL,
    )
    old_registry = _registry(tmp_path, old_ring)
    published = _publish(old_registry, installed)
    registry = _registry(tmp_path, current_ring)
    staged = registry.stage(replacement)

    repaired = registry.repair_same_version(
        staged,
        expected_revision=published.revision,
        expected_signing_key_id=installed.manifest.signing_key_id,
        expected_manifest_sha256=installed.manifest_sha256,
        expected_package_sha256=installed.package_sha256,
        app_binding=_binding(replacement),
        installed_verifier=_verifier(old_ring),
    )

    assert repaired.record.versions[0].package_sha256 == replacement.package_sha256
    assert registry.reverify_installed("test.workspace").package_sha256 == (
        replacement.package_sha256
    )


def test_repair_same_version_rotates_one_exact_retired_key_identity(tmp_path: Path) -> None:
    old_private = Ed25519PrivateKey.generate()
    current_private = Ed25519PrivateKey.generate()
    old_ring = _ring_for_key(old_private, "retired-featured-key")
    current_ring = _ring_for_key(current_private, "current-featured-key")
    installed = _verified(
        tmp_path,
        old_private,
        old_ring,
        name="installed",
        signing_key_id="retired-featured-key",
    )
    replacement = _verified(
        tmp_path,
        current_private,
        current_ring,
        name="replacement",
        signing_key_id="current-featured-key",
        skill_payload=_REPLACEMENT_SKILL,
    )
    old_registry = _registry(tmp_path, old_ring)
    published = _publish(old_registry, installed)
    registry = _registry(tmp_path, current_ring)
    staged = registry.stage(replacement)

    repaired = registry.repair_same_version(
        staged,
        expected_revision=published.revision,
        expected_signing_key_id="retired-featured-key",
        expected_manifest_sha256=installed.manifest_sha256,
        expected_package_sha256=installed.package_sha256,
        app_binding=_binding(replacement),
        installed_verifier=_verifier(old_ring),
    )

    selected = repaired.record.versions[0]
    assert selected.signing_key_id == "current-featured-key"
    assert registry.reverify_installed("test.workspace").package_sha256 == (
        replacement.package_sha256
    )


def test_repair_same_version_recovers_current_target_with_retired_backup(
    tmp_path: Path,
) -> None:
    old_private, old_ring = ephemeral_publisher_ring()
    current_private, current_ring = ephemeral_publisher_ring()
    installed = _verified(tmp_path, old_private, old_ring, name="installed")
    replacement = _verified(
        tmp_path,
        current_private,
        current_ring,
        name="replacement",
        skill_payload=_REPLACEMENT_SKILL,
    )
    old_registry = _registry(tmp_path, old_ring)
    published = _publish(old_registry, installed)
    registry = _registry(tmp_path, current_ring)
    staged = registry.stage(replacement)
    target = registry.packages_root / "test.workspace" / "1.0.0"
    backup = target.parent / f".repair-1.0.0-{installed.package_sha256}"
    target.rename(backup)
    shutil.copytree(staged.stage_path, target)

    repaired = registry.repair_same_version(
        staged,
        expected_revision=published.revision,
        expected_signing_key_id=installed.manifest.signing_key_id,
        expected_manifest_sha256=installed.manifest_sha256,
        expected_package_sha256=installed.package_sha256,
        app_binding=_binding(replacement),
        installed_verifier=_verifier(old_ring),
    )

    assert repaired.record.versions[0].package_sha256 == replacement.package_sha256
    assert backup.exists() is False
    assert registry.reverify_installed("test.workspace").package_sha256 == (
        replacement.package_sha256
    )


def test_publish_uses_retained_verified_bytes_when_stage_bytes_change(
    tmp_path: Path,
) -> None:
    """Catches making the mutable stage pathname authoritative after staging."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    calls: list[Path] = []

    def verifier(path: Path) -> VerifiedPlugin:
        calls.append(path)
        return verify_plugin_package(path, ring, "3.0.0")

    registry = PluginRegistry(state_root=tmp_path / "plugins", verifier=verifier)
    staged = registry.stage(verified)
    (staged.stage_path / "skills" / "workspace" / "SKILL.md").write_bytes(_REPLACEMENT_SKILL)

    registry.publish(
        staged, expected_revision=registry.snapshot().revision, app_binding=_binding(verified)
    )

    published = registry.packages_root / "test.workspace" / "1.0.0"
    assert (published / "skills" / "workspace" / "SKILL.md").read_bytes() == _ORIGINAL_SKILL
    assert published in calls
    assert staged.stage_path.exists()
    assert len(registry.snapshot().records) == 1


def test_stale_revision_cannot_move_or_publish_a_stage(tmp_path: Path) -> None:
    """Catches a caller overwriting registry state observed before another mutation."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    stale = registry.snapshot().revision
    first = _publish(registry, verified)
    staged = registry.stage(verified)

    _assert_error(
        "plugin_registry_conflict",
        lambda: registry.publish(staged, expected_revision=stale, app_binding=_binding(verified)),
    )
    assert staged.stage_path.exists()
    assert registry.snapshot().revision == first.revision


def test_identical_install_is_idempotent_and_consumes_its_stage(tmp_path: Path) -> None:
    """Catches rewriting or colliding with an identical immutable version."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    first = _publish(registry, verified)
    registry_bytes = registry.registry_path.read_bytes()
    staged = registry.stage(verified)

    second = registry.publish(
        staged, expected_revision=first.revision, app_binding=_binding(verified)
    )

    assert second == first
    assert registry.registry_path.read_bytes() == registry_bytes
    assert not staged.stage_path.exists()


@pytest.mark.parametrize("stage_change", ["mutated", "rebound"])
def test_identical_install_ignores_non_authoritative_stage_changes(
    tmp_path: Path,
    stage_change: str,
) -> None:
    """Catches letting best-effort stage cleanup turn idempotent success into failure."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    first = _publish(registry, verified)
    staged = registry.stage(verified)
    if stage_change == "mutated":
        (staged.stage_path / "skills" / "workspace" / "SKILL.md").write_bytes(_REPLACEMENT_SKILL)
        untouched = staged.stage_path
    else:
        original = staged.stage_path.with_name(f"{staged.stage_path.name}-original")
        staged.stage_path.rename(original)
        rebound = tmp_path / "rebound-identical-stage"
        rebound.mkdir()
        (rebound / "sentinel.txt").write_text("outside", encoding="utf-8")
        rebound.rename(staged.stage_path)
        untouched = staged.stage_path

    second = registry.publish(
        staged, expected_revision=first.revision, app_binding=_binding(verified)
    )

    assert second == first
    assert untouched.exists()
    if stage_change == "mutated":
        assert (untouched / "skills" / "workspace" / "SKILL.md").read_bytes() == _REPLACEMENT_SKILL
    else:
        assert (untouched / "sentinel.txt").read_text(encoding="utf-8") == "outside"
    assert registry.snapshot().revision == first.revision


def test_same_plugin_version_with_different_signed_bytes_is_an_identity_collision(
    tmp_path: Path,
) -> None:
    """Catches replacing an immutable plugin/version identity with different bytes."""
    private_key, ring = ephemeral_publisher_ring()
    first = _verified(tmp_path, private_key, ring, name="first")
    different = _verified(
        tmp_path,
        private_key,
        ring,
        name="different",
        skill_payload=b"# Different signed package\n",
    )
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, first)
    staged = registry.stage(different)

    _assert_error(
        "plugin_identity_collision",
        lambda: registry.publish(
            staged, expected_revision=installed.revision, app_binding=_binding(different)
        ),
    )
    assert staged.stage_path.exists()
    assert registry.snapshot() == PluginRegistrySnapshot(
        records=(installed.record,), revision=installed.revision
    )


def test_casefolded_plugin_alias_cannot_publish_as_a_second_identity(tmp_path: Path) -> None:
    """Catches case-insensitive filesystem aliases becoming distinct registry identities."""
    private_key, ring = ephemeral_publisher_ring()
    lower = _verified(tmp_path, private_key, ring, name="lower")
    alias = _verified(
        tmp_path,
        private_key,
        ring,
        name="alias",
        plugin_id="TEST.WORKSPACE",
    )
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, lower)
    staged = registry.stage(alias)

    _assert_error(
        "plugin_identity_collision",
        lambda: registry.publish(
            staged, expected_revision=installed.revision, app_binding=_binding(alias)
        ),
    )
    assert staged.stage_path.exists()


def test_downgrade_is_rejected_without_consuming_the_stage(tmp_path: Path) -> None:
    """Catches publishing a lower semantic version over a newer installed identity."""
    private_key, ring = ephemeral_publisher_ring()
    newer = _verified(tmp_path, private_key, ring, name="newer", plugin_version="2.0.0")
    older = _verified(tmp_path, private_key, ring, name="older", plugin_version="1.0.0")
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, newer)
    staged = registry.stage(older)

    _assert_error(
        "plugin_downgrade_rejected",
        lambda: registry.publish(
            staged, expected_revision=installed.revision, app_binding=_binding(older)
        ),
    )
    assert staged.stage_path.exists()
    assert registry.snapshot().revision == installed.revision


def test_concurrent_publishers_share_the_full_revision_and_move_lock(tmp_path: Path) -> None:
    """Catches releasing the publication lock between revision check, move, and write."""
    private_key, ring = ephemeral_publisher_ring()
    left = _verified(tmp_path, private_key, ring, name="left", plugin_id="test.left")
    right = _verified(tmp_path, private_key, ring, name="right", plugin_id="test.right")
    registry = _registry(tmp_path, ring)
    expected_revision = registry.snapshot().revision
    stages = [registry.stage(left), registry.stage(right)]
    barrier = threading.Barrier(3)
    outcomes: list[PluginMutationResult | BaseException] = []

    def publish(staged: StagedPluginInstall) -> None:
        barrier.wait()
        try:
            outcomes.append(
                registry.publish(
                    staged,
                    expected_revision=expected_revision,
                    app_binding=_binding(staged.verified),
                )
            )
        except BaseException as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=publish, args=(staged,)) for staged in stages]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert len([item for item in outcomes if isinstance(item, PluginMutationResult)]) == 1
    errors = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(errors) == 1
    assert type(errors[0]) is PluginRegistryError
    assert str(errors[0]) == "plugin_registry_conflict"
    snapshot = registry.snapshot()
    assert len(snapshot.records) == 1
    assert sum(not staged.stage_path.exists() for staged in stages) == 1


@pytest.mark.parametrize("surface", ["state", "packages", "lock"])
def test_reparse_durable_roots_and_lock_are_rejected_before_use(
    tmp_path: Path,
    surface: str,
) -> None:
    """Catches following a reparse at a registry-owned root or lock sidecar."""
    outside = tmp_path / "outside"
    outside.mkdir()
    state_root = tmp_path / "plugins"
    if surface == "state":
        try:
            os.symlink(outside, state_root, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
    else:
        state_root.mkdir()
        if surface == "packages":
            target = outside / "packages"
            target.mkdir()
            link = state_root / "packages"
            is_directory = True
        else:
            target = outside / "registry.lock"
            target.write_bytes(b"outside sentinel")
            link = state_root / "registry.json.lock"
            is_directory = False
        try:
            os.symlink(target, link, target_is_directory=is_directory)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    _private_key, ring = ephemeral_publisher_ring()
    registry = PluginRegistry(state_root=state_root, verifier=_verifier(ring))

    _assert_error("plugin_registry_invalid", registry.snapshot)


def test_publish_does_not_follow_a_reparse_stage(tmp_path: Path) -> None:
    """Catches reading or promoting a rebound stage directory during publication."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    original = staged.stage_path.with_name(f"{staged.stage_path.name}-original")
    staged.stage_path.rename(original)
    outside = tmp_path / "outside-stage"
    outside.mkdir()
    try:
        os.symlink(outside, staged.stage_path, target_is_directory=True)
    except OSError as exc:
        original.rename(staged.stage_path)
        pytest.skip(f"directory symlinks unavailable: {exc}")

    registry.publish(
        staged, expected_revision=registry.snapshot().revision, app_binding=_binding(verified)
    )

    assert not (outside / "plugin.json").exists()
    published = registry.packages_root / "test.workspace" / "1.0.0"
    assert (published / "skills" / "workspace" / "SKILL.md").read_bytes() == _ORIGINAL_SKILL


def test_discard_is_confined_idempotent_and_cannot_delete_a_published_package(
    tmp_path: Path,
) -> None:
    """Catches discard following an arbitrary path or deleting immutable publication."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    registry.discard(staged)
    registry.discard(staged)
    assert not staged.stage_path.exists()

    published = _publish(registry, verified)
    published_path = registry.packages_root / "test.workspace" / "1.0.0"
    forged = replace(staged, stage_path=published_path)
    _assert_error("plugin_stage_invalid", lambda: registry.discard(forged))
    assert published_path.is_dir()
    assert registry.get("test.workspace") == published.record


def test_discard_rejects_an_arbitrary_existing_directory(tmp_path: Path) -> None:
    """Catches stage-name checks without registry-root confinement."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    outside = tmp_path / ".installing-outside"
    outside.mkdir()
    forged = replace(staged, stage_path=outside)

    _assert_error("plugin_stage_invalid", lambda: registry.discard(forged))
    assert outside.is_dir()


def test_discard_rejects_registry_corruption_before_deleting_the_stage(tmp_path: Path) -> None:
    """Catches a mutation proceeding from durable state that could not be read."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    registry.registry_path.write_bytes(b"not-json")

    _assert_error("plugin_registry_unreadable", lambda: registry.discard(staged))
    assert staged.stage_path.is_dir()


def test_verified_installed_snapshot_binds_record_package_and_revision_under_one_lock(
    tmp_path: Path,
) -> None:
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, verified)

    observed = registry.verified_installed_snapshot("test.workspace")

    assert observed.record == installed.record
    assert observed.verified.manifest == verified.manifest
    assert observed.verified.package_sha256 == verified.package_sha256
    assert observed.revision == installed.revision


def test_enable_block_and_clear_are_revision_checked_and_content_free(tmp_path: Path) -> None:
    """Catches state mutation without optimistic concurrency or blocked-enable enforcement."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, verified)

    blocked = registry.block(
        "test.workspace",
        "dependency_unavailable",
        expected_revision=installed.revision,
    )
    assert blocked.record.blocker == "dependency_unavailable"
    assert blocked.record.enabled is False
    _assert_error(
        "plugin_enable_blocked",
        lambda: registry.set_enabled("test.workspace", True, expected_revision=blocked.revision),
    )
    _assert_error(
        "plugin_registry_conflict",
        lambda: registry.clear_block("test.workspace", expected_revision=installed.revision),
    )
    cleared = registry.clear_block("test.workspace", expected_revision=blocked.revision)
    assert cleared.record.blocker is None
    enabled = registry.set_enabled("test.workspace", True, expected_revision=cleared.revision)
    assert enabled.record.enabled is True
    assert registry.get("test.workspace") == enabled.record
    payload = registry.registry_path.read_bytes()
    assert b"dependency_unavailable" not in payload
    assert b'"blocker":null' in payload


def test_higher_version_is_candidate_without_changing_selected_state(
    tmp_path: Path,
) -> None:
    """Catches implicitly activating a signed update during immutable publication."""
    private_key, ring = ephemeral_publisher_ring()
    first = _verified(tmp_path, private_key, ring, name="first", plugin_version="1.0.0")
    higher = _verified(
        tmp_path,
        private_key,
        ring,
        name="higher",
        plugin_version="2.0.0",
        skill_payload=b"# Signed version two\n",
    )
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, first)
    enabled = registry.set_enabled("test.workspace", True, expected_revision=installed.revision)
    blocked = registry.block(
        "test.workspace", "dependency_unavailable", expected_revision=enabled.revision
    )
    old_binding = blocked.record.app_binding
    assert old_binding is not None
    new_binding = PluginAppBinding(
        required_app_ids=old_binding.required_app_ids,
        optional_app_ids=old_binding.optional_app_ids,
        dependency_receipt_id="receipt-update",
        revision=hashlib.sha256(
            canonical_bytes(
                {
                    "required_app_ids": list(old_binding.required_app_ids),
                    "optional_app_ids": list(old_binding.optional_app_ids),
                    "dependency_receipt_id": "receipt-update",
                }
            )
        ).hexdigest(),
    )
    staged = registry.stage(higher)

    updated = registry.publish(
        staged,
        expected_revision=blocked.revision,
        app_binding=new_binding,
        preserve_existing_binding=True,
    )

    assert updated.record.selected_version == "1.0.0"
    assert updated.record.app_binding == old_binding
    assert updated.record.enabled is True
    assert updated.record.blocker == "dependency_unavailable"
    assert updated.record.versions == (
        InstalledPluginVersionRecord(
            plugin_version="1.0.0",
            signing_key_id="test-plugin-key",
            manifest_sha256=first.manifest_sha256,
            package_sha256=first.package_sha256,
            state="selected",
        ),
        InstalledPluginVersionRecord(
            plugin_version="2.0.0",
            signing_key_id="test-plugin-key",
            manifest_sha256=higher.manifest_sha256,
            package_sha256=higher.package_sha256,
            state="candidate",
        ),
    )

    promoted = registry.promote_candidate(
        "test.workspace",
        "2.0.0",
        expected_revision=updated.revision,
        app_binding=new_binding,
    )

    assert promoted.record.selected_version == "2.0.0"
    assert promoted.record.app_binding == new_binding
    assert promoted.record.enabled is True
    assert promoted.record.blocker == "dependency_unavailable"
    assert tuple((item.plugin_version, item.state) for item in promoted.record.versions) == (
        ("1.0.0", "candidate"),
        ("2.0.0", "selected"),
    )
    _assert_error(
        "plugin_registry_conflict",
        lambda: registry.promote_candidate(
            "test.workspace",
            "1.0.0",
            expected_revision=updated.revision,
        ),
    )


def test_candidate_discard_retries_exact_orphan_after_registry_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cleanup failure leaving an unverifiable candidate tree after rollback."""
    from runtime.pinned_filesystem import PinnedTree

    private_key, ring = ephemeral_publisher_ring()
    first = _verified(tmp_path, private_key, ring, name="first", plugin_version="1.0.0")
    higher = _verified(
        tmp_path,
        private_key,
        ring,
        name="higher",
        plugin_version="2.0.0",
        skill_payload=b"# Signed version two\n",
    )
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, first)
    staged = registry.stage(higher)
    updated = registry.publish(
        staged,
        expected_revision=installed.revision,
        app_binding=_binding(higher),
        preserve_existing_binding=True,
    )
    candidate = next(
        item for item in updated.record.versions if item.plugin_version == "2.0.0"
    )
    real_remove = PinnedTree.remove
    calls = 0

    def fail_once(pinned: PinnedTree) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("package_tree_invalid")
        real_remove(pinned)

    monkeypatch.setattr(PinnedTree, "remove", fail_once)

    _assert_error(
        "plugin_registry_recovery_required",
        lambda: registry.discard_candidate(
            "test.workspace",
            "2.0.0",
            expected_revision=updated.revision,
            expected_candidate=candidate,
        ),
    )
    after_failure = registry.snapshot()
    assert after_failure.records == (installed.record,)
    assert (registry.packages_root / "test.workspace" / "2.0.0").is_dir()

    reconciled = registry.discard_candidate(
        "test.workspace",
        "2.0.0",
        expected_revision=after_failure.revision,
        expected_candidate=candidate,
    )

    assert reconciled.record == installed.record
    assert not (registry.packages_root / "test.workspace" / "2.0.0").exists()


@pytest.mark.parametrize("candidate_damage", ("deleted", "corrupt"))
def test_exact_candidate_recovery_discard_does_not_require_candidate_bytes(
    tmp_path: Path,
    candidate_damage: str,
) -> None:
    """A locally sealed registry identity is sufficient to quarantine cleanup."""

    private_key, ring = ephemeral_publisher_ring()
    first = _verified(tmp_path, private_key, ring, name="first", plugin_version="1.0.0")
    higher = _verified(
        tmp_path,
        private_key,
        ring,
        name="higher",
        plugin_version="2.0.0",
        skill_payload=b"# Signed version two\n",
    )
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, first)
    updated = registry.publish(
        registry.stage(higher),
        expected_revision=installed.revision,
        app_binding=_binding(higher),
        preserve_existing_binding=True,
    )
    candidate = next(
        item for item in updated.record.versions if item.plugin_version == "2.0.0"
    )
    candidate_root = registry.packages_root / "test.workspace" / "2.0.0"
    if candidate_damage == "deleted":
        from runtime.pinned_filesystem import PinnedDirectory

        with PinnedDirectory.open(candidate_root.parent) as plugin_root:
            with plugin_root.pin_tree(candidate_root.name) as pinned:
                pinned.remove()
    else:
        (candidate_root / "plugin.toml").write_bytes(b"not a signed manifest")

    reconciled = registry.discard_candidate(
        "test.workspace",
        "2.0.0",
        expected_revision=updated.revision,
        expected_candidate=candidate,
    )

    assert reconciled.record == installed.record
    assert not candidate_root.exists()


def test_exact_update_rollback_restores_predecessor_after_candidate_promotion(
    tmp_path: Path,
) -> None:
    """Rollback is an exact CAS restore, not a forbidden reverse promotion."""

    private_key, ring = ephemeral_publisher_ring()
    first = _verified(tmp_path, private_key, ring, name="first", plugin_version="1.0.0")
    higher = _verified(
        tmp_path,
        private_key,
        ring,
        name="higher",
        plugin_version="2.0.0",
        skill_payload=b"# Signed version two\n",
    )
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, first)
    candidate_binding = _binding(higher)
    published = registry.publish(
        registry.stage(higher),
        expected_revision=installed.revision,
        app_binding=candidate_binding,
        preserve_existing_binding=True,
    )
    candidate = next(
        item for item in published.record.versions if item.plugin_version == "2.0.0"
    )
    promoted = registry.promote_candidate(
        "test.workspace",
        "2.0.0",
        expected_revision=published.revision,
        app_binding=candidate_binding,
    )

    restored = registry.rollback_update_candidate(
        expected_base=installed.record,
        expected_candidate=candidate,
        candidate_binding=candidate_binding,
        expected_revision=promoted.revision,
    )

    assert restored.record == installed.record
    assert not (registry.packages_root / "test.workspace" / "2.0.0").exists()


@pytest.mark.parametrize("seam", ["materialize", "temp_fsync", "before_replace"])
def test_atomic_failure_removes_final_and_preserves_stage_and_registry_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    """Catches partial publication at materialization, registry-fsync, or replace."""
    from plugin_platform import registry as registry_module

    private_key, ring = ephemeral_publisher_ring()
    first = _verified(tmp_path, private_key, ring, name="first")
    higher = _verified(
        tmp_path,
        private_key,
        ring,
        name="higher",
        plugin_version="2.0.0",
        skill_payload=b"# Higher\n",
    )
    registry = _registry(tmp_path, ring)
    installed = _publish(registry, first)
    before = registry.registry_path.read_bytes()
    staged = registry.stage(higher)
    if seam == "materialize":
        monkeypatch.setattr(
            registry_module,
            "_materialize_final",
            lambda *_args: (_ for _ in ()).throw(OSError("injected materialize failure")),
        )
    elif seam == "temp_fsync":
        monkeypatch.setattr(
            registry_module,
            "_after_registry_temp_fsync",
            lambda *_args: (_ for _ in ()).throw(OSError("injected temp fsync failure")),
        )
    else:
        monkeypatch.setattr(
            registry_module,
            "_replace_registry_file",
            lambda *_args: (_ for _ in ()).throw(OSError("injected replace failure")),
        )

    _assert_error(
        "plugin_registry_write_failed",
        lambda: registry.publish(
            staged, expected_revision=installed.revision, app_binding=_binding(higher)
        ),
    )
    assert staged.stage_path.is_dir()
    assert not (registry.packages_root / "test.workspace" / "2.0.0").exists()
    assert registry.registry_path.read_bytes() == before
    assert not tuple(registry.registry_path.parent.glob(".registry.json.*.part"))


def test_partial_materialization_cleanup_failure_requires_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches hiding an unremoved partial final tree behind an ordinary write error."""
    from plugin_platform import registry as registry_module

    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    before = registry.snapshot()
    real_write = registry_module.PinnedDirectory.write_bytes
    final_writes = 0

    def fail_second_final_write(
        directory: registry_module.PinnedDirectory,
        relative_path: str | Path,
        payload: bytes,
        *,
        exclusive: bool = True,
    ) -> None:
        nonlocal final_writes
        if directory.path.name == "1.0.0":
            final_writes += 1
            if final_writes == 2:
                raise ValueError("injected partial materialization failure")
        real_write(directory, relative_path, payload, exclusive=exclusive)

    monkeypatch.setattr(registry_module.PinnedDirectory, "write_bytes", fail_second_final_write)
    monkeypatch.setattr(
        registry_module,
        "_remove_final",
        lambda *_args: (_ for _ in ()).throw(OSError("injected cleanup failure")),
    )

    _assert_error(
        "plugin_registry_recovery_required",
        lambda: registry.publish(
            staged, expected_revision=before.revision, app_binding=_binding(verified)
        ),
    )
    assert final_writes == 2
    assert staged.stage_path.is_dir()
    assert registry.snapshot() == before
    assert (registry.packages_root / "test.workspace" / "1.0.0" / "plugin.json").is_file()


def test_final_cleanup_failure_requires_recovery_and_keeps_registry_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches reporting an ordinary failure when uncommitted final bytes remain."""
    from plugin_platform import registry as registry_module

    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    before = registry.snapshot().revision
    staged = registry.stage(verified)
    monkeypatch.setattr(
        registry_module,
        "_after_registry_temp_fsync",
        lambda *_args: (_ for _ in ()).throw(OSError("injected write failure")),
    )
    monkeypatch.setattr(
        registry_module,
        "_remove_final",
        lambda *_args: (_ for _ in ()).throw(OSError("injected cleanup failure")),
    )

    _assert_error(
        "plugin_registry_recovery_required",
        lambda: registry.publish(staged, expected_revision=before, app_binding=_binding(verified)),
    )
    assert not registry.registry_path.exists()
    assert (registry.packages_root / "test.workspace" / "1.0.0").is_dir()


def test_read_and_mutation_reject_casefolded_aliases_in_durable_state(
    tmp_path: Path,
) -> None:
    """Catches loading two registry keys that alias on case-insensitive filesystems."""
    _private_key, ring = ephemeral_publisher_ring()
    registry = _registry(tmp_path, ring)
    registry.packages_root.mkdir(parents=True)
    version = {
        "plugin_version": "1.0.0",
        "signing_key_id": "test-plugin-key",
        "manifest_sha256": "a" * 64,
        "package_sha256": "b" * 64,
        "state": "selected",
    }
    record = {
        "plugin_id": "test.workspace",
        "publisher_id": "test.publisher",
        "kind": "mcp_backed",
        "selected_version": "1.0.0",
        "enabled": False,
        "status": "installed",
        "blocker": None,
        "versions": [version],
    }
    alias = {**record, "plugin_id": "TEST.WORKSPACE"}
    registry.registry_path.write_bytes(
        canonical_bytes(
            {
                "schema_version": 1,
                "plugins": {
                    "TEST.WORKSPACE": alias,
                    "test.workspace": record,
                },
            }
        )
    )

    _assert_error("plugin_registry_unreadable", registry.snapshot)


def test_public_records_are_frozen_and_exactly_typed(tmp_path: Path) -> None:
    """Catches exposing registry-owned state through mutable record containers."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    result = _publish(registry, verified)

    assert type(result.record) is InstalledPluginRecord
    assert type(result.record.versions[0]) is InstalledPluginVersionRecord
    assert isinstance(result.record.versions, tuple)
    with pytest.raises(FrozenInstanceError):
        result.record.enabled = True  # type: ignore[misc]


def test_publication_lock_is_held_through_failed_write_and_final_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches removing uncommitted final bytes after releasing the sidecar lock."""
    from plugin_platform import registry as registry_module

    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    held = False
    cleaned_while_held = False
    real_lock = registry_module.PinnedDirectory.lock_file

    @contextmanager
    def observed_lock(
        directory: registry_module.PinnedDirectory,
        relative_path: str | Path,
        *,
        timeout: float = 10.0,
    ) -> Iterator[None]:
        nonlocal held
        with real_lock(directory, relative_path, timeout=timeout):
            held = True
            try:
                yield
            finally:
                held = False

    real_cleanup = registry_module._remove_final

    def observed_cleanup(
        final_path: Path,
        expected_identity: tuple[int, int] | None,
    ) -> None:
        nonlocal cleaned_while_held
        cleaned_while_held = held
        real_cleanup(final_path, expected_identity)

    monkeypatch.setattr(registry_module.PinnedDirectory, "lock_file", observed_lock)
    monkeypatch.setattr(
        registry_module,
        "_after_registry_temp_fsync",
        lambda *_args: (_ for _ in ()).throw(OSError("injected write failure")),
    )
    monkeypatch.setattr(registry_module, "_remove_final", observed_cleanup)

    _assert_error(
        "plugin_registry_write_failed",
        lambda: registry.publish(
            staged, expected_revision=_empty_revision(), app_binding=_binding(verified)
        ),
    )
    assert cleaned_while_held is True
    assert staged.stage_path.is_dir()


def test_publish_materializes_retained_authenticated_bytes_after_stage_path_mutation(
    tmp_path: Path,
) -> None:
    """Catches publishing bytes rebound into the mutable stage pathname after verification."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    (staged.stage_path / "skills" / "workspace" / "SKILL.md").write_bytes(_REPLACEMENT_SKILL)

    result = registry.publish(
        staged, expected_revision=registry.snapshot().revision, app_binding=_binding(verified)
    )

    published = registry.packages_root / "test.workspace" / "1.0.0"
    assert (published / "skills" / "workspace" / "SKILL.md").read_bytes() == _ORIGINAL_SKILL
    assert result.record.versions[0].package_sha256 == verified.package_sha256


def test_publish_reverifies_final_materialization_and_removes_rebound_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches committing or retaining final bytes changed after materialization."""
    from plugin_platform import registry as registry_module

    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    real_materialize = registry_module._materialize_final

    def rebound_final(*args: Any) -> None:
        real_materialize(*args)
        target = registry.packages_root / "test.workspace" / "1.0.0"
        (target / "skills" / "workspace" / "SKILL.md").write_bytes(_REPLACEMENT_SKILL)

    monkeypatch.setattr(registry_module, "_materialize_final", rebound_final)

    _assert_error(
        "plugin_identity_collision",
        lambda: registry.publish(
            staged, expected_revision=registry.snapshot().revision, app_binding=_binding(verified)
        ),
    )
    assert staged.stage_path.is_dir()
    assert not (registry.packages_root / "test.workspace" / "1.0.0").exists()
    assert registry.snapshot().records == ()


def test_root_rebind_between_validation_and_lock_cannot_redirect_registry_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches closing validated roots before reopening the sidecar and registry by pathname."""
    from plugin_platform import registry as registry_module

    _private_key, ring = ephemeral_publisher_ring()
    registry = _registry(tmp_path, ring)
    registry.snapshot()
    original_root = registry.state_root.with_name("plugins-before-root-swap")
    outside = tmp_path / "outside-registry-root"
    (outside / "packages").mkdir(parents=True)
    (outside / "registry.json").write_bytes(b"outside corrupt bytes")
    swapped = False
    attempted = False

    real_lock = registry_module.PinnedDirectory.lock_file

    @contextmanager
    def swapping_lock(
        directory: registry_module.PinnedDirectory,
        relative_path: str | Path,
        *,
        timeout: float = 10.0,
    ) -> Iterator[None]:
        nonlocal attempted, swapped
        attempted = True
        registry.state_root.rename(original_root)
        try:
            outside.rename(registry.state_root)
        except OSError:
            original_root.rename(registry.state_root)
        else:
            swapped = True
        with real_lock(directory, relative_path, timeout=timeout):
            yield

    monkeypatch.setattr(registry_module.PinnedDirectory, "lock_file", swapping_lock)

    try:
        snapshot = registry.snapshot()
    except PluginRegistryError as exc:
        assert attempted is True
        assert str(exc) == "plugin_registry_invalid"
    else:
        assert swapped is False
        assert snapshot.records == ()
    assert attempted is True


def test_stage_asset_creation_never_follows_a_swapped_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches validating an asset parent and then opening the leaf through a rebound path."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    outside = tmp_path / "outside-stage-write"
    outside.mkdir()
    original_open = Path.open
    swapped = False

    def swapping_open(path: Path, *args: Any, **kwargs: Any):
        nonlocal swapped
        if not swapped and path.name == "SKILL.md" and ".installing-" in str(path):
            original_parent = path.parent.with_name("workspace-before-stage-swap")
            path.parent.rename(original_parent)
            try:
                os.symlink(outside, path.parent, target_is_directory=True)
            except OSError:
                original_parent.rename(path.parent)
            else:
                swapped = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swapping_open)

    try:
        registry.stage(verified)
    except PluginRegistryError:
        assert swapped is True
    else:
        assert swapped is False
    assert not (outside / "SKILL.md").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows leaf-open sharing contract")
def test_windows_leaf_write_denies_nested_parent_rebind_until_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches allowing a real nested-parent swap at the production os.open seam."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    outside = tmp_path / "outside-windows-leaf-write"
    outside.mkdir()
    real_open = os.open
    real_fsync = os.fsync
    attempted = False
    rebound = False
    outside_write_observed = False
    rebound_parent: Path | None = None

    def swapping_open(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal attempted, rebound, rebound_parent
        candidate = Path(path) if isinstance(path, (str, os.PathLike)) else None
        if (
            not attempted
            and candidate is not None
            and candidate.name == "SKILL.md"
            and ".installing-" in str(candidate)
        ):
            attempted = True
            original_parent = candidate.parent.with_name("workspace-before-windows-leaf-swap")
            try:
                candidate.parent.rename(original_parent)
                outside.rename(candidate.parent)
            except OSError:
                if original_parent.exists() and not candidate.parent.exists():
                    original_parent.rename(candidate.parent)
            else:
                rebound = True
                rebound_parent = candidate.parent
        return real_open(path, *args, **kwargs)

    def observing_fsync(handle: int) -> None:
        nonlocal outside_write_observed
        if rebound_parent is not None and (rebound_parent / "SKILL.md").exists():
            outside_write_observed = True
        real_fsync(handle)

    monkeypatch.setattr(os, "open", swapping_open)
    monkeypatch.setattr(os, "fsync", observing_fsync)

    try:
        registry.stage(verified)
    except PluginRegistryError as exc:
        assert str(exc) in {"plugin_stage_invalid", "plugin_stage_write_failed"}

    assert attempted is True
    assert outside_write_observed is False
    if rebound:
        assert rebound_parent is not None


def test_semver_numeric_prerelease_precedes_alphabetic_prerelease(tmp_path: Path) -> None:
    """Catches using PEP 440, where a numeric SemVer prerelease is parsed as a postrelease."""
    private_key, ring = ephemeral_publisher_ring()
    numeric = _verified(tmp_path, private_key, ring, name="numeric", plugin_version="1.0.0-1")
    alphabetic = _verified(
        tmp_path,
        private_key,
        ring,
        name="alphabetic",
        plugin_version="1.0.0-alpha",
        skill_payload=b"# Alphabetic prerelease\n",
    )
    registry = _registry(tmp_path, ring)
    first = _publish(registry, numeric)

    result = registry.publish(
        registry.stage(alphabetic),
        expected_revision=first.revision,
        app_binding=_binding(alphabetic),
    )

    assert tuple(item.plugin_version for item in result.record.versions) == (
        "1.0.0-1",
        "1.0.0-alpha",
    )


def test_same_semver_precedence_with_different_build_metadata_fails_closed(
    tmp_path: Path,
) -> None:
    """Catches treating SemVer build metadata as an upgrade precedence component."""
    private_key, ring = ephemeral_publisher_ring()
    first_build = _verified(
        tmp_path, private_key, ring, name="build-one", plugin_version="1.0.0+build.1"
    )
    second_build = _verified(
        tmp_path,
        private_key,
        ring,
        name="build-two",
        plugin_version="1.0.0+build.2",
        skill_payload=b"# Different build\n",
    )
    registry = _registry(tmp_path, ring)
    first = _publish(registry, first_build)
    staged = registry.stage(second_build)

    _assert_error(
        "plugin_identity_collision",
        lambda: registry.publish(
            staged, expected_revision=first.revision, app_binding=_binding(second_build)
        ),
    )
    assert registry.snapshot().revision == first.revision


def test_successful_publication_fsyncs_stage_and_destination_parent_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches omitting durability for either the packages-root or plugin-parent entry."""
    from plugin_platform import registry as registry_module

    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    calls: list[Path] = []
    real_fsync = registry_module.fsync_directory

    def observed_fsync(path: Path) -> None:
        calls.append(Path(path))
        real_fsync(path)

    monkeypatch.setattr(registry_module, "fsync_directory", observed_fsync)

    registry.publish(
        staged, expected_revision=registry.snapshot().revision, app_binding=_binding(verified)
    )

    assert registry.packages_root in calls
    assert registry.packages_root / "test.workspace" in calls


def test_observe_publication_returns_exact_frozen_published_state(tmp_path: Path) -> None:
    """Catches trusting registry metadata without reverifying the immutable package tree."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    published = _publish(registry, verified)

    observed = registry.observe_publication(**_publication_identity(verified))

    assert observed == PluginPublicationObservation(
        published=True,
        record=published.record,
        revision=published.revision,
    )
    with pytest.raises(FrozenInstanceError):
        observed.published = False  # type: ignore[misc]
    with pytest.raises(ValueError, match="^invalid publication observation$"):
        PluginPublicationObservation(
            published=True,
            record=None,
            revision=published.revision,
        )


def test_observe_publication_returns_unpublished_only_when_record_and_tree_are_absent(
    tmp_path: Path,
) -> None:
    """Catches guessing publication from either missing durable surface alone."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)

    observed = registry.observe_publication(**_publication_identity(verified))

    assert observed == PluginPublicationObservation(
        published=False,
        record=None,
        revision=_empty_revision(),
    )


@pytest.mark.parametrize("missing_surface", ["record", "tree"])
def test_observe_publication_rejects_partial_durable_publication(
    tmp_path: Path,
    missing_surface: str,
) -> None:
    """Catches treating a record-only or tree-only state as published or unpublished."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    _publish(registry, verified)
    target = registry.packages_root / "test.workspace" / "1.0.0"
    if missing_surface == "tree":
        shutil.rmtree(target)
    else:
        registry.registry_path.write_bytes(canonical_bytes({"schema_version": 1, "plugins": {}}))

    _assert_error(
        "plugin_registry_reconciliation_failed",
        lambda: registry.observe_publication(**_publication_identity(verified)),
    )


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("publisher_id", "other.publisher"),
        ("signing_key_id", "other-signing-key"),
        ("manifest_sha256", "1" * 64),
        ("package_sha256", "2" * 64),
    ],
)
def test_observe_publication_rejects_expected_identity_mismatch(
    tmp_path: Path,
    changed_field: str,
    changed_value: str,
) -> None:
    """Catches reconciling a different publisher, signer, or authenticated package identity."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    _publish(registry, verified)
    expected = _publication_identity(verified)
    expected[changed_field] = changed_value

    _assert_error(
        "plugin_registry_reconciliation_failed",
        lambda: registry.observe_publication(**expected),
    )


def test_observe_publication_rejects_changed_installed_bytes_or_verifier_failure(
    tmp_path: Path,
) -> None:
    """Catches accepting registry digests when installed package verification no longer passes."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    _publish(registry, verified)
    installed_skill = (
        registry.packages_root / "test.workspace" / "1.0.0" / "skills" / "workspace" / "SKILL.md"
    )
    installed_skill.write_bytes(_REPLACEMENT_SKILL)

    _assert_error(
        "plugin_registry_reconciliation_failed",
        lambda: registry.observe_publication(**_publication_identity(verified)),
    )


def test_observe_publication_holds_shared_registry_lock_through_tree_verification(
    tmp_path: Path,
) -> None:
    """Catches releasing the registry lock before the package-tree decision is complete."""
    private_key, ring = ephemeral_publisher_ring()
    armed = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    snapshot_done = threading.Event()
    verifier = _verifier(ring)

    def blocking_verifier(path: Path) -> VerifiedPlugin:
        if armed.is_set() and path.name == "1.0.0":
            entered.set()
            assert release.wait(timeout=5)
        return verifier(path)

    registry = PluginRegistry(state_root=tmp_path / "plugins", verifier=blocking_verifier)
    verified = _verified(tmp_path, private_key, ring)
    _publish(registry, verified)
    armed.set()
    observation: list[PluginPublicationObservation] = []
    observer = threading.Thread(
        target=lambda: observation.append(
            registry.observe_publication(**_publication_identity(verified))
        )
    )
    snapshotter = threading.Thread(target=lambda: (registry.snapshot(), snapshot_done.set()))

    observer.start()
    assert entered.wait(timeout=5)
    snapshotter.start()
    assert snapshot_done.wait(timeout=0.1) is False
    release.set()
    observer.join(timeout=5)
    snapshotter.join(timeout=5)

    assert observer.is_alive() is False
    assert snapshotter.is_alive() is False
    assert snapshot_done.is_set()
    assert observation[0].published is True


def test_recover_stage_returns_exact_verified_stage_for_normal_discard(tmp_path: Path) -> None:
    """Catches reconstructing a cleanup handle without authenticating the staged bytes."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)

    recovered = registry.recover_stage(
        staged.stage_path.name,
        **_publication_identity(verified),
    )

    assert recovered == staged
    registry.discard(recovered)
    assert recovered.stage_path.exists() is False


def test_recover_stage_returns_none_only_for_an_exact_missing_stage(tmp_path: Path) -> None:
    """Catches treating a missing recovery stage as corruption or fabricating a stage handle."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)

    recovered = registry.recover_stage(
        ".installing-00000000000000000000000000000000",
        **_publication_identity(verified),
    )

    assert recovered is None


@pytest.mark.parametrize(
    "stage_name",
    ["../.installing-" + "0" * 32, "1.0.0", ".installing-" + "A" * 32],
)
def test_recover_stage_rejects_noncanonical_or_unconfined_names(
    tmp_path: Path,
    stage_name: str,
) -> None:
    """Catches recovery following arbitrary, traversing, or aliased stage paths."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)

    _assert_error(
        "plugin_registry_reconciliation_failed",
        lambda: registry.recover_stage(stage_name, **_publication_identity(verified)),
    )


@pytest.mark.parametrize("failure", ["identity", "content"])
def test_recover_stage_rejects_mismatched_or_corrupt_stage(
    tmp_path: Path,
    failure: str,
) -> None:
    """Catches abort cleanup acting on a stage not authenticated for this transaction."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    expected = _publication_identity(verified)
    if failure == "identity":
        expected["package_sha256"] = "3" * 64
    else:
        (staged.stage_path / "plugin.json").write_bytes(b"not-json")

    _assert_error(
        "plugin_registry_reconciliation_failed",
        lambda: registry.recover_stage(staged.stage_path.name, **expected),
    )


def test_observe_publication_rejects_registry_kind_drift_from_installed_manifest(
    tmp_path: Path,
) -> None:
    """Catches reconciling valid registry metadata for a different plugin kind."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    _publish(registry, verified)
    document = json.loads(registry.registry_path.read_bytes())
    document["plugins"][verified.manifest.plugin_id]["kind"] = "skill_only"
    registry.registry_path.write_bytes(canonical_bytes(document))

    _assert_error(
        "plugin_registry_reconciliation_failed",
        lambda: registry.observe_publication(**_publication_identity(verified)),
    )


def test_publication_observation_revalidates_and_isolates_nested_record_mutation(
    tmp_path: Path,
) -> None:
    """Catches retaining caller-owned version sequences in a public recovery decision."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    published = _publish(_registry(tmp_path, ring), verified)
    caller_versions = list(published.record.versions)
    caller_record = replace(published.record, versions=caller_versions)  # type: ignore[arg-type]

    observed = PluginPublicationObservation(
        published=True,
        record=caller_record,
        revision=published.revision,
    )
    caller_versions.clear()

    assert observed.record == published.record
    assert observed.record is not caller_record
    assert observed.record is not None
    assert observed.record.versions == published.record.versions
    assert isinstance(observed.record.versions, tuple)


@pytest.mark.parametrize(
    "record_mutation",
    [
        lambda record: replace(record, plugin_id="https://provider.example"),
        lambda record: replace(
            record,
            versions=(replace(record.versions[0], manifest_sha256="1" * 63),),
        ),
        lambda record: replace(record, versions=(object(),)),
        lambda record: replace(record, blocker="credential=DO-NOT-LEAK"),
    ],
    ids=["scalar", "nested-version", "malformed-nested-version", "blocker"],
)
def test_publication_observation_rejects_forged_record_with_content_free_value_error(
    tmp_path: Path,
    record_mutation: Any,
) -> None:
    """Catches direct-constructor record forgery or leaking rejected nested content."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    published = _publish(_registry(tmp_path, ring), verified)
    forged = record_mutation(published.record)

    with pytest.raises(ValueError, match="^invalid publication observation$") as caught:
        PluginPublicationObservation(
            published=True,
            record=forged,
            revision=published.revision,
        )

    assert caught.value.__cause__ is None
    assert str(caught.value) == "invalid publication observation"
    assert "provider" not in str(caught.value)
    assert "credential" not in str(caught.value)


@pytest.mark.parametrize("revision", [None, object(), "credential=DO-NOT-LEAK"])
def test_publication_observation_normalizes_every_invalid_revision_to_value_error(
    revision: Any,
) -> None:
    """Catches leaking registry-boundary errors or rejected revision content."""
    with pytest.raises(ValueError, match="^invalid publication observation$") as caught:
        PluginPublicationObservation(
            published=False,
            record=None,
            revision=revision,
        )

    assert caught.value.__cause__ is None
    assert str(caught.value) == "invalid publication observation"
    assert "credential" not in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows absolute-mutation contract")
@pytest.mark.parametrize("operation", ["create_directory", "replace", "unlink"])
def test_windows_pinned_mutations_reject_rebound_ancestor_before_side_effect(
    tmp_path: Path,
    operation: str,
) -> None:
    """Catches absolute Windows mutations touching a replacement retained-root path."""
    owned = tmp_path / "owned"
    root = owned / "root"
    root.mkdir(parents=True)
    replacement_owned = tmp_path / "replacement-owned"
    replacement_root = replacement_owned / "root"
    replacement_root.mkdir(parents=True)
    if operation == "replace":
        (replacement_root / "source").write_bytes(b"replacement source")
    elif operation == "unlink":
        (replacement_root / "victim").write_bytes(b"replacement victim")

    retained_root = tmp_path / "retained-root"
    pinned = PinnedDirectory.open(root)
    root.rename(retained_root)
    replacement_root.rename(root)
    try:
        with pytest.raises(ValueError, match="^package_tree_invalid$"):
            if operation == "create_directory":
                child = pinned.create_directory("created")
                child.close()
            elif operation == "replace":
                pinned.replace("source", "target")
            else:
                pinned.unlink("victim")

        assert not (root / "created").exists()
        if operation == "replace":
            assert (root / "source").read_bytes() == b"replacement source"
        assert not (root / "target").exists()
        if operation == "unlink":
            assert (root / "victim").read_bytes() == b"replacement victim"
    finally:
        pinned.close()

    released = tmp_path / "released-replacement-root"
    root.rename(released)
    retained_root.rename(root)
    assert root.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-mutation durability contract")
@pytest.mark.parametrize("operation", ["replace", "unlink"])
def test_windows_handle_mutations_fsync_the_retained_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Catches successful handle mutation returning before parent durability."""
    root = tmp_path / "root"
    root.mkdir()
    source_name = "source" if operation == "replace" else "victim"
    (root / source_name).write_bytes(b"owned")
    calls: list[Path] = []
    real_fsync = PinnedDirectory.fsync

    def observed_fsync(directory: PinnedDirectory) -> None:
        calls.append(directory.path)
        real_fsync(directory)

    monkeypatch.setattr(PinnedDirectory, "fsync", observed_fsync)

    with PinnedDirectory.open(root) as pinned:
        if operation == "replace":
            pinned.replace("source", "target")
        else:
            pinned.unlink("victim")

    assert calls == [root.resolve()]


def test_discard_keeps_replacement_stage_content_after_verification_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches verifying one stage tree and recursively deleting a rebound pathname."""
    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    saved_stage = staged.stage_path.with_name(f"{staged.stage_path.name}-saved")
    replacement = tmp_path / "caller-replacement-stage"
    replacement.mkdir()
    marker = replacement / "caller-owned.txt"
    marker.write_bytes(b"caller owned")
    real_verify = registry._verify_stage_path

    def verify_then_attempt_rebind(path: Path) -> VerifiedPlugin:
        observed = real_verify(path)
        try:
            path.rename(saved_stage)
            replacement.rename(path)
        except OSError:
            pass
        return observed

    monkeypatch.setattr(registry, "_verify_stage_path", verify_then_attempt_rebind)

    try:
        registry.discard(staged)
    except PluginRegistryError:
        pass

    replacement_markers = (
        replacement / "caller-owned.txt",
        staged.stage_path / "caller-owned.txt",
    )
    assert any(
        path.read_bytes() == b"caller owned" for path in replacement_markers if path.exists()
    )


@pytest.mark.parametrize("cleanup", ["final", "partial_stage"])
def test_recursive_cleanup_never_deletes_a_rebound_replacement_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str,
) -> None:
    """Catches final/partial cleanup following a path rebound after its safety check."""
    from plugin_platform import filesystem as filesystem_module
    from plugin_platform import registry as registry_module

    target = tmp_path / ("final" if cleanup == "final" else ".installing-" + "f" * 32)
    target.mkdir()
    (target / "original.txt").write_bytes(b"original")
    saved = tmp_path / f"saved-{cleanup}"
    replacement = tmp_path / f"replacement-{cleanup}"
    replacement.mkdir()
    marker = replacement / "caller-owned.txt"
    marker.write_bytes(b"caller owned")
    with PinnedDirectory.open(target.parent) as parent:
        with parent.pin_tree(target.name) as pinned:
            expected_identity = pinned.identity
    attempted = False

    def attempt_rebind(path: Path) -> None:
        nonlocal attempted
        if path != target:
            return
        attempted = True
        try:
            path.rename(saved)
            replacement.rename(path)
        except OSError:
            pass

    monkeypatch.setattr(filesystem_module, "_after_tree_pinned", attempt_rebind)

    if cleanup == "final":
        try:
            registry_module._remove_final(target, expected_identity)
        except (OSError, RuntimeError, ValueError):
            pass
    else:
        registry_module._remove_partial_stage(target, expected_identity)

    assert attempted is True
    replacement_markers = (replacement / "caller-owned.txt", target / "caller-owned.txt")
    assert any(
        path.read_bytes() == b"caller owned" for path in replacement_markers if path.exists()
    )


@pytest.mark.parametrize("cleanup", ["final", "partial_stage"])
def test_recursive_cleanup_rejects_a_replacement_present_before_pin(
    tmp_path: Path,
    cleanup: str,
) -> None:
    """Catches pathname-only cleanup pinning and deleting an earlier replacement."""
    from plugin_platform import registry as registry_module

    target = tmp_path / ("final" if cleanup == "final" else ".installing-" + "e" * 32)
    target.mkdir()
    (target / "original.txt").write_bytes(b"original")
    with PinnedDirectory.open(target.parent) as parent:
        with parent.pin_tree(target.name) as pinned:
            expected_identity = pinned.identity
    saved = tmp_path / f"saved-before-pin-{cleanup}"
    replacement = tmp_path / f"replacement-before-pin-{cleanup}"
    replacement.mkdir()
    marker = replacement / "caller-owned.txt"
    marker.write_bytes(b"caller owned")
    target.rename(saved)
    replacement.rename(target)

    with pytest.raises(ValueError, match="^package_tree_invalid$"):
        if cleanup == "final":
            registry_module._remove_final(target, expected_identity)
        else:
            registry_module._remove_partial_stage(target, expected_identity)

    assert (target / marker.name).read_bytes() == b"caller owned"


def test_registry_temp_cleanup_preserves_active_non_exception_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches registry temp cleanup replacing a live cancellation after fsync."""
    from plugin_platform import registry as registry_module

    private_key, ring = ephemeral_publisher_ring()
    verified = _verified(tmp_path, private_key, ring)
    registry = _registry(tmp_path, ring)
    staged = registry.stage(verified)
    marker = RegistryMarker()
    real_unlink = registry_module.PinnedDirectory.unlink

    def cancel_after_fsync(_temporary: Path) -> None:
        raise marker

    def fail_temp_cleanup(
        directory: PinnedDirectory,
        relative_path: str | Path,
        *,
        missing_ok: bool = False,
    ) -> None:
        if str(relative_path).startswith(".registry.json."):
            raise RuntimeError("temporary cleanup failed")
        real_unlink(directory, relative_path, missing_ok=missing_ok)

    monkeypatch.setattr(registry_module, "_after_registry_temp_fsync", cancel_after_fsync)
    monkeypatch.setattr(registry_module.PinnedDirectory, "unlink", fail_temp_cleanup)

    with pytest.raises(RegistryMarker) as caught:
        registry.publish(
            staged, expected_revision=registry.snapshot().revision, app_binding=_binding(verified)
        )

    assert caught.value is marker
