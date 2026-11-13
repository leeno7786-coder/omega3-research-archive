"""End-to-end acceptance for the closed plugin-foundation boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization

from mcp_platform.canonical import canonical_bytes
from plugin_platform.app_projection import AppOnlyPluginProjection, derive_app_only_plugin
from plugin_platform.catalog import (
    MarketplaceCatalogEntry,
    VerifiedMarketplaceCatalog,
    verify_catalog_successor,
    verify_plugin_catalog,
)
from plugin_platform.contracts import (
    AppDeployment,
    AppRequirement,
    CapabilityRequirement,
    PluginAuthentication,
    PluginManifest,
)
from plugin_platform.dependencies import (
    AppCapabilitySnapshot,
    DependencyResolution,
    NamedCapability,
    resolve_dependencies,
)
from plugin_platform.installer import (
    DependencyCommitReceipt,
    DependencyProvisioner,
    InstallConfirmationVerifier,
    PluginInstallResult,
    PluginInstallReview,
    PluginInstaller,
    PreparedDependencySet,
)
from plugin_platform.package_verifier import VerifiedPlugin, verify_plugin_package
from plugin_platform.registry import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginRegistry,
    PluginRegistrySnapshot,
    VerifiedInstalledPluginSnapshot,
)
from plugin_platform.trust import TrustedPublisherKeyRing
from tests.plugin_platform.support import (
    ephemeral_publisher_ring,
    marketplace_entry_document,
)


_PUBLIC_API = {
    "AppOnlyPluginProjection": AppOnlyPluginProjection,
    "derive_app_only_plugin": derive_app_only_plugin,
    "MarketplaceCatalogEntry": MarketplaceCatalogEntry,
    "VerifiedMarketplaceCatalog": VerifiedMarketplaceCatalog,
    "verify_catalog_successor": verify_catalog_successor,
    "verify_plugin_catalog": verify_plugin_catalog,
    "AppDeployment": AppDeployment,
    "AppRequirement": AppRequirement,
    "CapabilityRequirement": CapabilityRequirement,
    "PluginManifest": PluginManifest,
    "AppCapabilitySnapshot": AppCapabilitySnapshot,
    "DependencyResolution": DependencyResolution,
    "NamedCapability": NamedCapability,
    "resolve_dependencies": resolve_dependencies,
    "DependencyProvisioner": DependencyProvisioner,
    "InstallConfirmationVerifier": InstallConfirmationVerifier,
    "PluginInstallResult": PluginInstallResult,
    "PluginInstallReview": PluginInstallReview,
    "PluginInstaller": PluginInstaller,
    "VerifiedPlugin": VerifiedPlugin,
    "verify_plugin_package": verify_plugin_package,
    "InstalledPluginRecord": InstalledPluginRecord,
    "InstalledPluginVersionRecord": InstalledPluginVersionRecord,
    "PluginRegistry": PluginRegistry,
    "PluginRegistrySnapshot": PluginRegistrySnapshot,
    "VerifiedInstalledPluginSnapshot": VerifiedInstalledPluginSnapshot,
    "TrustedPublisherKeyRing": TrustedPublisherKeyRing,
}
_EXPECTED_PUBLIC_NAMES = tuple(_PUBLIC_API)
_PUBLIC_MODULES = {name: value.__module__ for name, value in _PUBLIC_API.items()}
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SKILL_BYTES = b"# Signed Workspace\n\nUse only the reviewed workspace capability.\n"
_TRANSACTION_ID = "8" * 32


class _OneUseConfirmation:
    def __init__(self, token: str) -> None:
        self.token = token
        self.consumed: set[str] = set()

    def consume(self, confirmation: object, expected_review_sha256: str) -> None:
        if confirmation != self.token or expected_review_sha256 != self.token:
            raise ValueError("confirmation_mismatch")
        if self.token in self.consumed:
            raise ValueError("confirmation_replayed")
        self.consumed.add(self.token)


class _DeterministicProvisioner:
    def __init__(self, snapshot: AppCapabilitySnapshot) -> None:
        self.snapshot = snapshot
        self.prepared_requirements: tuple[AppRequirement, ...] | None = None
        self.committed_receipt_ids: list[str] = []
        self.aborted_transaction_ids: list[str] = []
        self.rolled_back_receipt_ids: list[str] = []

    def prepare(
        self,
        transaction_id: str,
        requirements: tuple[AppRequirement, ...],
    ) -> PreparedDependencySet:
        self.prepared_requirements = requirements
        return PreparedDependencySet(
            transaction_id=transaction_id,
            prepared_id=f"prepared-{transaction_id}",
            receipt_id=f"receipt-{transaction_id}",
            app_snapshots=(self.snapshot,),
        )

    def abort(self, transaction_id: str) -> None:
        self.aborted_transaction_ids.append(transaction_id)

    def commit(self, prepared: PreparedDependencySet) -> DependencyCommitReceipt:
        self.committed_receipt_ids.append(prepared.receipt_id)
        return DependencyCommitReceipt(
            receipt_id=prepared.receipt_id,
            prepared_id=prepared.prepared_id,
            introduced_app_ids=(self.snapshot.app_id,),
        )

    def rollback(self, receipt_id: str) -> None:
        self.rolled_back_receipt_ids.append(receipt_id)


def _write_signed_plugin(package: Path, signing_key: Any) -> None:
    skill_path = package / "skills" / "workspace" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(_SKILL_BYTES)
    unsigned: dict[str, object] = {
        "manifest_version": 2,
        "plugin_id": "test.foundation.workspace",
        "publisher_id": "test.publisher",
        "plugin_version": "1.0.0",
        "minimum_omega_version": "3.0.0",
        "kind": "mcp_backed",
        "title": "Workspace Foundation",
        "description": "Signed declarative workspace guidance.",
        "assets": [
            {
                "path": "skills/workspace/SKILL.md",
                "kind": "skill",
                "media_type": "text/markdown",
                "sha256": hashlib.sha256(_SKILL_BYTES).hexdigest(),
            }
        ],
        "entry_points": [
            {
                "entry_point_id": "get_started",
                "title": "Get started",
                "skill_path": "skills/workspace/SKILL.md",
                "required_app_ids": ["test.foundation.app"],
            }
        ],
        "starter_requests": ["Help me get started with Workspace Foundation."],
        "required_apps": [
            {
                "app_id": "test.foundation.app",
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
        ],
        "optional_apps": [],
        "schedule_template_paths": [],
        "proactive_event_rules": [],
        "enterprise_roles": [],
        "signing_key_id": "test-plugin-key",
    }
    signature = base64.b64encode(signing_key.sign(canonical_bytes(unsigned))).decode("ascii")
    (package / "plugin.json").write_bytes(canonical_bytes({**unsigned, "signature": signature}))


def _write_signed_catalog(
    path: Path,
    signing_key: Any,
    verified: VerifiedPlugin,
) -> None:
    unsigned: dict[str, object] = {
        "schema_version": 2,
        "catalog_id": "test.foundation.catalog",
        "publisher_id": "test.publisher",
        "sequence": 1,
        "entries": [
            marketplace_entry_document(
                plugin_id=verified.manifest.plugin_id,
                publisher_id=verified.manifest.publisher_id,
                plugin_version=verified.manifest.plugin_version,
                manifest_sha256=verified.manifest_sha256,
                package_sha256=verified.package_sha256,
                package_uri="https://plugins.example.test/test.foundation.workspace/1.0.0",
            )
        ],
        "signing_key_id": "test-plugin-key",
    }
    signature = base64.b64encode(signing_key.sign(canonical_bytes(unsigned))).decode("ascii")
    path.write_bytes(canonical_bytes({**unsigned, "signature": signature}))


def _app_snapshot() -> AppCapabilitySnapshot:
    return AppCapabilitySnapshot(
        app_id="test.foundation.app",
        publisher_id="test.publisher",
        deployment=AppDeployment.LOCAL,
        provider_id="workspace",
        protocol_version="2026-07-28",
        transports=("stdio",),  # type: ignore[arg-type]
        remote_origin=None,
        authorization_issuer=None,
        authentication=PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        tools=(
            NamedCapability(
                name="workspace.list",
                schema_sha256=None,
                version=None,
                description="List workspace items.",
            ),
        ),
        prompts=(),
        resources=(),
        extensions=(),
        capability_revision="c" * 64,
        installed=True,
        trusted=True,
        enabled=False,
    )


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if hasattr(value, "value") and type(getattr(value, "value")) is str:
        return getattr(value, "value")
    return value


def _assert_content_free(text: str) -> None:
    lowered = text.casefold()
    for forbidden in (
        "client_secret",
        "api_key",
        "api-key",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "private_key",
        "private key",
        "private",
        "credential",
        "account",
        "account_id",
        "tenant",
        "tenant_id",
        "authority",
        "grant",
        "package_bytes",
        "resource_content",
        "skill_bytes",
        "raw_content",
        "begin openssh private key",
        "begin private key",
        _SKILL_BYTES.decode("utf-8").casefold(),
    ):
        assert forbidden not in lowered
    assert re.search(r"https?://[^\s/]+@", text, flags=re.IGNORECASE) is None


def test_foundation_root_exports_only_the_closed_stable_surface() -> None:
    """Catches missing or extra root exports that expose implementation internals."""
    consumer = """
import importlib
import json
import sys
sys.path.insert(0, json.loads({root!r}))
import plugin_platform

expected_names = tuple(json.loads({names!r}))
expected_modules = json.loads({modules!r})
from plugin_platform import ({imports})
actual = tuple(plugin_platform.__all__)
assert actual == expected_names, (actual, expected_names)
for name in actual:
    expected = getattr(importlib.import_module(expected_modules[name]), name)
    exported = getattr(plugin_platform, name)
    assert exported is expected, f"{{name}}: root export identity mismatch"
""".format(
        root=json.dumps(str(_REPOSITORY_ROOT)),
        names=json.dumps(_EXPECTED_PUBLIC_NAMES),
        modules=json.dumps(_PUBLIC_MODULES, sort_keys=True),
        imports=", ".join(_EXPECTED_PUBLIC_NAMES),
    )
    completed = subprocess.run(
        [sys.executable, "-c", consumer],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_foundation_root_contract_rejects_same_module_wrong_export_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches comparing only module labels instead of exact exported identities."""
    package = tmp_path / "plugin_platform"
    package.mkdir()
    names_by_module: dict[str, list[str]] = {}
    for name, module_name in _PUBLIC_MODULES.items():
        names_by_module.setdefault(module_name, []).append(name)
    init_lines: list[str] = []
    for module_name, names in names_by_module.items():
        relative_module = module_name.removeprefix("plugin_platform.")
        declarations = "\n".join(f"class {name}:\n    pass\n" for name in names)
        (package / f"{relative_module}.py").write_text(declarations, encoding="utf-8")
        init_lines.append(f"from .{relative_module} import {', '.join(names)}")
    init_lines.extend(
        (
            f"__all__ = {_EXPECTED_PUBLIC_NAMES!r}",
            "AppRequirement = PluginManifest",
        )
    )
    (package / "__init__.py").write_text("\n".join(init_lines), encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_REPOSITORY_ROOT", tmp_path)

    with pytest.raises(AssertionError) as exc_info:
        test_foundation_root_exports_only_the_closed_stable_surface()

    assert "AppRequirement" in str(exc_info.value)


def test_signed_foundation_installs_dormant_and_persists_only_content_free_state(
    tmp_path: Path,
) -> None:
    """Catches trust, transaction, reload, projection, or privacy boundary drift."""
    signing_key, key_ring = ephemeral_publisher_ring()
    signing_secret = signing_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    package = tmp_path / "package"
    _write_signed_plugin(package, signing_key)
    verified = verify_plugin_package(package, key_ring, "3.0.0")
    catalog_path = tmp_path / "catalog.json"
    _write_signed_catalog(catalog_path, signing_key, verified)
    catalog = verify_plugin_catalog(catalog_path, key_ring)
    entry = catalog.entries[0]

    app = _app_snapshot()
    resolution = resolve_dependencies(verified.manifest, (app,))
    assert resolution.ready is True
    assert tuple(item.requirement.app_id for item in resolution.required) == (
        "test.foundation.app",
    )

    state_root = tmp_path / "state"

    def verifier(path: Path) -> VerifiedPlugin:
        return verify_plugin_package(path, key_ring, "3.0.0")

    registry = PluginRegistry(state_root=state_root, verifier=verifier)
    provisioner = _DeterministicProvisioner(app)
    preview_installer = PluginInstaller(
        registry=registry,
        provisioner=provisioner,
        confirmation_verifier=_OneUseConfirmation("0" * 64),
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )
    review = preview_installer.review_from_catalog(
        catalog,
        entry,
        verified,
        resolution,
        expected_registry_revision=registry.snapshot().revision,
    )
    confirmation = _OneUseConfirmation(review.sha256)
    installer = PluginInstaller(
        registry=registry,
        provisioner=provisioner,
        confirmation_verifier=confirmation,
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )

    result = installer.install_from_catalog(catalog, entry, verified, review.sha256)

    assert confirmation.consumed == {review.sha256}
    with pytest.raises(ValueError, match="^confirmation_replayed$"):
        confirmation.consume(review.sha256, review.sha256)
    assert provisioner.prepared_requirements == verified.manifest.required_apps
    assert provisioner.committed_receipt_ids == [f"receipt-{_TRANSACTION_ID}"]
    assert provisioner.aborted_transaction_ids == []
    assert provisioner.rolled_back_receipt_ids == []
    assert result.record.enabled is False
    assert result.record.status == "installed"
    assert result.record.blocker is None
    assert result.record.versions[0].state == "selected"
    assert installer.journal_path.exists() is False

    before_reload = registry.snapshot()
    after_reload = PluginRegistry(state_root=state_root, verifier=verifier).snapshot()
    assert after_reload == before_reload
    assert after_reload.revision == result.revision
    assert after_reload.records == (result.record,)

    projection = derive_app_only_plugin(
        app,
        "Workspace App",
        "Conversational access to reviewed workspace capabilities.",
    )
    assert projection.plugin_id != result.record.plugin_id
    assert projection.enabled is False
    assert projection.skill_assets == ()
    assert tuple(item.app_id for item in projection.required_apps) == ("test.foundation.app",)

    public_records = (
        catalog,
        entry,
        resolution,
        review,
        result,
        before_reload,
        after_reload,
        projection,
    )
    public_text = "\n".join(
        repr(record) + "\n" + json.dumps(_jsonable(record), sort_keys=True, separators=(",", ":"))
        for record in public_records
    )
    _assert_content_free(public_text)
    registry_bytes = registry.registry_path.read_bytes()
    _assert_content_free(registry_bytes.decode("utf-8"))
    assert _SKILL_BYTES not in registry_bytes

    persisted_files = tuple(path for path in tmp_path.rglob("*") if path.is_file())
    assert persisted_files
    assert all(signing_secret not in path.read_bytes() for path in persisted_files)
    assert installer.journal_path not in persisted_files
