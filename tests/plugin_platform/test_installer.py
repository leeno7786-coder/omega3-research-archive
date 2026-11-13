"""Focused contracts for atomic plugin and dependency installation."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import paths as core_paths
from mcp_platform.canonical import canonical_bytes
from plugin_platform.catalog import (
    MarketplaceCatalogEntry,
    VerifiedMarketplaceCatalog,
    verify_plugin_catalog,
)
from plugin_platform.dependencies import (
    AppCapabilitySnapshot,
    DependencyResolution,
    NamedCapability,
    resolve_dependencies,
)
from plugin_platform.filesystem import PinnedDirectory
from plugin_platform.installer import (
    DependencyCommitReceipt,
    InstallRecoveryResult,
    PluginDependencyReview,
    PluginInstallError,
    PluginInstallResult,
    PluginInstallReview,
    PluginInstaller,
    PreparedDependencySet,
)
from plugin_platform.package_verifier import VerifiedPlugin, verify_plugin_package
from plugin_platform.contracts import PluginAuthentication
from plugin_platform.registry import PluginAppBinding, PluginRegistry, PluginRegistryError
from plugin_platform.trust import TrustedPublisherKeyRing
from tests.plugin_platform.support import (
    build_signed_plugin,
    ephemeral_publisher_ring,
    marketplace_entry_document,
)


_SKILL = b"# Workspace\n\nUse the workspace deliberately.\n"
_DIGEST = "a" * 64
_TRANSACTION_ID = "0" * 32
_STAGE_NAME = f".installing-{_TRANSACTION_ID}"
_PREPARED_ID = f"prepared-{_TRANSACTION_ID}"
_RECEIPT_ID = f"receipt-{_TRANSACTION_ID}"


class Marker(BaseException):
    pass


def _binding(verified: VerifiedPlugin, receipt_id: str = _RECEIPT_ID) -> PluginAppBinding:
    required = tuple(sorted(item.app_id for item in verified.manifest.required_apps))
    optional: tuple[str, ...] = ()
    revision = hashlib.sha256(
        canonical_bytes(
            {
                "required_app_ids": list(required),
                "optional_app_ids": list(optional),
                "dependency_receipt_id": receipt_id,
            }
        )
    ).hexdigest()
    return PluginAppBinding(required, optional, receipt_id, revision)


def _signed_package(
    root: Path,
    private_key: Ed25519PrivateKey,
    *,
    include_optional: bool = False,
    remote_dependency: bool = False,
    provider_id: str = "workspace",
    with_legacy_authority: bool = False,
) -> Path:
    package = build_signed_plugin(root, private_key)
    skill_path = package / "skills" / "workspace" / "SKILL.md"
    skill_path.write_bytes(_SKILL)
    document = json.loads((package / "plugin.json").read_bytes())
    document["plugin_id"] = "test.workspace"
    document["plugin_version"] = "1.0.0"
    document["kind"] = "mcp_backed"
    document["assets"][0]["sha256"] = hashlib.sha256(_SKILL).hexdigest()
    document["entry_points"][0]["required_app_ids"] = ["test.workspace.app"]
    document["required_apps"] = [
        {
            "app_id": "test.workspace.app",
            "publisher_id": "test.publisher",
            "deployment": "remote" if remote_dependency else "local",
            "provider_id": provider_id,
            "protocol_version": "2026-07-28",
            "transports": ["streamable_http" if remote_dependency else "stdio"],
            "remote_origin": "https://workspace.example" if remote_dependency else None,
            "authorization_issuer": ("https://auth.example/tenant" if remote_dependency else None),
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
    if include_optional:
        document["optional_apps"] = [
            {
                "app_id": "test.optional.app",
                "publisher_id": "test.publisher",
                "deployment": "local",
                "provider_id": "optional",
                "protocol_version": "2026-07-28",
                "transports": ["stdio"],
                "remote_origin": None,
                "authorization_issuer": None,
                "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
                "capabilities": [
                    {
                        "kind": "tool",
                        "name": "optional.list",
                        "schema_sha256": None,
                        "version": None,
                    }
                ],
            }
        ]
    if with_legacy_authority:
        authority_payload = canonical_bytes({"tools": {}})
        authority_path = package / "tools" / "authority.json"
        authority_path.parent.mkdir(parents=True, exist_ok=True)
        authority_path.write_bytes(authority_payload)
        document["assets"].append(
            {
                "path": "tools/authority.json",
                "kind": "capability_authority",
                "media_type": "application/json",
                "sha256": hashlib.sha256(authority_payload).hexdigest(),
            }
        )
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    (package / "plugin.json").write_bytes(canonical_bytes(document))
    return package


def _verifier(ring: TrustedPublisherKeyRing) -> Callable[[Path], VerifiedPlugin]:
    return lambda path: verify_plugin_package(path, ring, "3.0.0")


def _snapshot(*, description: str = "List workspace items.") -> AppCapabilitySnapshot:
    return AppCapabilitySnapshot(
        app_id="test.workspace.app",
        publisher_id="test.publisher",
        deployment="local",  # type: ignore[arg-type]
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
                description=description,
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


class FakeConfirmation:
    def __init__(self, events: list[str], mode: str = "valid") -> None:
        self.events = events
        self.mode = mode
        self.consumed: set[str] = set()
        self.expected_hashes: list[str] = []

    def consume(self, confirmation: object, expected_review_sha256: str) -> None:
        self.events.append("confirm")
        self.expected_hashes.append(expected_review_sha256)
        if self.mode == "accept":
            self.consumed.add(expected_review_sha256)
            return
        if self.mode == "missing" or confirmation is None:
            raise PluginInstallError("plugin_install_confirmation_missing")
        if self.mode == "expired":
            raise PluginInstallError("plugin_install_confirmation_expired")
        if self.mode == "mismatch" or confirmation != expected_review_sha256:
            raise PluginInstallError("plugin_install_confirmation_mismatch")
        if self.mode == "replay" or expected_review_sha256 in self.consumed:
            raise PluginInstallError("plugin_install_confirmation_replayed")
        self.consumed.add(expected_review_sha256)


class FakeProvisioner:
    def __init__(
        self,
        events: list[str],
        *,
        snapshots: tuple[AppCapabilitySnapshot, ...] | None = None,
    ) -> None:
        self.events = events
        self.snapshots = snapshots or (_snapshot(),)
        self.prepared_by_transaction: dict[str, PreparedDependencySet] = {}
        self.receipts: dict[str, DependencyCommitReceipt] = {}
        self.aborted: list[str] = []
        self.rolled_back: list[str] = []
        self.failure: dict[str, BaseException] = {}
        self.after_commit_failure: BaseException | None = None
        self.commit_journals: list[dict[str, object]] = []
        self.receipt_id_override: str | None = None
        self.introduced_app_ids_override: tuple[str, ...] | None = None
        self.journal_path: Path | None = None

    def prepare(self, transaction_id: str, requirements: tuple[Any, ...]) -> PreparedDependencySet:
        self.events.append("prepare")
        if "prepare" in self.failure:
            raise self.failure["prepare"]
        prepared = self.prepared_by_transaction.get(transaction_id)
        if prepared is None:
            prepared = PreparedDependencySet(
                transaction_id=transaction_id,
                prepared_id=f"prepared-{transaction_id}",
                receipt_id=f"receipt-{transaction_id}",
                app_snapshots=self.snapshots,
            )
            self.prepared_by_transaction[transaction_id] = prepared
        return prepared

    def abort(self, transaction_id: str) -> None:
        self.events.append("abort")
        if "abort" in self.failure:
            raise self.failure["abort"]
        self.aborted.append(transaction_id)

    def commit(self, prepared: PreparedDependencySet) -> DependencyCommitReceipt:
        self.events.append("commit")
        journal_path = self.journal_path
        if journal_path is not None and journal_path.exists():
            self.commit_journals.append(json.loads(journal_path.read_bytes()))
        if "commit" in self.failure:
            raise self.failure["commit"]
        receipt = DependencyCommitReceipt(
            receipt_id=self.receipt_id_override or f"receipt-{prepared.transaction_id}",
            prepared_id=prepared.prepared_id,
            introduced_app_ids=self.introduced_app_ids_override
            if self.introduced_app_ids_override is not None
            else ("test.workspace.app",),
        )
        self.receipts[receipt.receipt_id] = receipt
        if self.after_commit_failure is not None:
            raise self.after_commit_failure
        return receipt

    def rollback(self, receipt_id: str) -> None:
        self.events.append("rollback")
        if "rollback" in self.failure:
            raise self.failure["rollback"]
        self.rolled_back.append(receipt_id)


class RecordingRegistry:
    def __init__(self, registry: PluginRegistry, events: list[str]) -> None:
        self.inner = registry
        self.events = events
        self.state_root = registry.state_root
        self.failure: dict[str, BaseException] = {}
        self.publish_then_raise = False
        self.force_stale_revision = False
        self.after_stage_failure: BaseException | None = None
        self.after_publish_failure: BaseException | None = None
        self.stage_requests: list[str | None] = []
        self.stage_journals: list[dict[str, object]] = []

    def snapshot(self):
        return self.inner.snapshot()

    def get(self, plugin_id: str):
        return self.inner.get(plugin_id)

    def stage(self, verified: VerifiedPlugin, *, stage_name: str | None = None):
        self.events.append("stage")
        self.stage_requests.append(stage_name)
        journal_path = Path(self.state_root) / core_paths.PLUGIN_INSTALL_JOURNAL_PATH.name
        if journal_path.exists():
            self.stage_journals.append(json.loads(journal_path.read_bytes()))
        if "stage" in self.failure:
            raise self.failure["stage"]
        staged = (
            self.inner.stage(verified)
            if stage_name is None
            else self.inner.stage(verified, stage_name=stage_name)
        )
        if self.after_stage_failure is not None:
            raise self.after_stage_failure
        return staged

    def publish(self, staged: Any, expected_revision: str, *, app_binding: PluginAppBinding):
        self.events.append("publish")
        if "publish" in self.failure:
            raise self.failure["publish"]
        revision = "0" * 64 if self.force_stale_revision else expected_revision
        result = self.inner.publish(staged, revision, app_binding=app_binding)
        if self.after_publish_failure is not None:
            raise self.after_publish_failure
        if self.publish_then_raise:
            raise PluginInstallError("ambiguous_publish")
        return result

    def discard(self, staged: Any) -> None:
        self.events.append("discard")
        if "discard" in self.failure:
            raise self.failure["discard"]
        self.inner.discard(staged)

    def observe_publication(self, **expected: str):
        return self.inner.observe_publication(**expected)

    def recover_stage(self, stage_name: str, **expected: str):
        return self.inner.recover_stage(stage_name, **expected)

    def reverify_installed(self, plugin_id: str):
        return self.inner.reverify_installed(plugin_id)

    def bind_apps(self, plugin_id: str, *, expected_revision: str, app_binding: PluginAppBinding):
        return self.inner.bind_apps(
            plugin_id,
            expected_revision=expected_revision,
            app_binding=app_binding,
        )

    def preflight_publish(
        self, staged: Any, expected_revision: str, *, app_binding: PluginAppBinding
    ):
        return self.inner.preflight_publish(
            staged,
            expected_revision,
            app_binding=app_binding,
        )

    def preflight_bind_apps(
        self,
        plugin_id: str,
        *,
        expected_revision: str,
        app_binding: PluginAppBinding,
    ):
        return self.inner.preflight_bind_apps(
            plugin_id,
            expected_revision=expected_revision,
            app_binding=app_binding,
        )


def _context(
    tmp_path: Path,
    *,
    confirmation_mode: str = "valid",
    include_optional: bool = False,
    remote_dependency: bool = False,
    with_legacy_authority: bool = False,
) -> dict[str, Any]:
    private_key, ring = ephemeral_publisher_ring()
    package = _signed_package(
        tmp_path / "package",
        private_key,
        include_optional=include_optional,
        remote_dependency=remote_dependency,
        with_legacy_authority=with_legacy_authority,
    )
    verified = verify_plugin_package(package, ring, "3.0.0")
    entry = MarketplaceCatalogEntry.from_dict(
        marketplace_entry_document(
            plugin_id=verified.manifest.plugin_id,
            publisher_id=verified.manifest.publisher_id,
            plugin_version=verified.manifest.plugin_version,
            manifest_sha256=verified.manifest_sha256,
            package_sha256=verified.package_sha256,
            package_uri="https://plugins.example.test/test.workspace/1.0.0",
        )
    )
    unsigned_catalog: dict[str, object] = {
        "schema_version": 2,
        "catalog_id": "test.catalog",
        "publisher_id": "test.publisher",
        "sequence": 1,
        "entries": [entry.to_dict()],
        "signing_key_id": "test-plugin-key",
    }
    catalog_signature = base64.b64encode(
        private_key.sign(canonical_bytes(unsigned_catalog))
    ).decode("ascii")
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_bytes(canonical_bytes({**unsigned_catalog, "signature": catalog_signature}))
    catalog = verify_plugin_catalog(catalog_path, ring)
    events: list[str] = []
    real_registry = PluginRegistry(
        state_root=tmp_path / "plugins",
        verifier=_verifier(ring),
    )
    registry = RecordingRegistry(real_registry, events)
    provisioner = FakeProvisioner(events)
    confirmer = FakeConfirmation(events, confirmation_mode)
    installer = PluginInstaller(
        registry=registry,
        provisioner=provisioner,
        confirmation_verifier=confirmer,
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )
    provisioner.journal_path = installer.journal_path
    return {
        "private_key": private_key,
        "ring": ring,
        "catalog": catalog,
        "entry": entry,
        "verified": verified,
        "events": events,
        "registry": registry,
        "real_registry": real_registry,
        "provisioner": provisioner,
        "confirmer": confirmer,
        "installer": installer,
    }


def _install(context: dict[str, Any], confirmation: object | None = None) -> PluginInstallResult:
    if confirmation is None:
        resolution = resolve_dependencies(
            context["verified"].manifest,
            (_snapshot(),),
        )
        review = context["installer"].review_from_catalog(
            context["catalog"],
            context["entry"],
            context["verified"],
            resolution,
            expected_registry_revision=context["real_registry"].snapshot().revision,
        )
        confirmation = review.sha256
    return context["installer"].install_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        confirmation,
    )


def test_install_orders_prepare_discovery_confirmation_stage_commit_and_publish(
    tmp_path: Path,
) -> None:
    """Catches confirmation before discovery or any partial publication order."""
    context = _context(tmp_path)

    result = _install(context)

    assert context["events"] == ["prepare", "confirm", "stage", "commit", "publish"]
    assert result.record.plugin_id == "test.workspace"
    assert result.record.enabled is False
    assert result.record.blocker is None
    assert result.dependency_receipt.introduced_app_ids == ("test.workspace.app",)
    assert result.review_sha256 == context["confirmer"].expected_hashes[0]
    assert context["installer"].journal_path.exists() is False


def test_real_mcp_provisioner_installs_signed_catalog_dependency_atomically(
    tmp_path: Path,
) -> None:
    """Catches the public provisioner export or real installer boundary drifting apart."""
    from mcp_platform.app_registry import McpAppRegistry
    from mcp_platform.app_sources import TrustedRemoteAppSource
    from mcp_platform.provisioner import McpAppProvisioner
    from mcp_platform.registry import PackageRegistry

    context = _context(
        tmp_path,
        confirmation_mode="accept",
        remote_dependency=True,
    )
    state_root = tmp_path / "mcp"

    class SignedRemoteResolver:
        def resolve(self, requirement: Any) -> TrustedRemoteAppSource:
            return TrustedRemoteAppSource(requirement=requirement)

    app_registry = McpAppRegistry(state_root=state_root)
    provisioner = McpAppProvisioner(
        app_registry=app_registry,
        package_registry=PackageRegistry(state_root=state_root),
        source_resolver=SignedRemoteResolver(),
    )
    context["installer"] = PluginInstaller(
        registry=context["registry"],
        provisioner=provisioner,
        confirmation_verifier=context["confirmer"],
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )

    result = _install(context, confirmation=object())

    assert result.record.plugin_id == "test.workspace"
    assert result.record.enabled is False
    assert result.dependency_receipt.receipt_id == f"mcp-receipt-{_TRANSACTION_ID}"
    assert result.dependency_receipt.introduced_app_ids == ("test.workspace.app",)
    assert tuple(record.app_id for record in app_registry.snapshot().records) == (
        "test.workspace.app",
    )
    assert app_registry.get("test.workspace.app").enabled is False
    assert context["installer"].journal_path.exists() is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugin_id", "other.workspace"),
        ("publisher_id", "other.publisher"),
        ("plugin_version", "2.0.0"),
        ("manifest_sha256", "1" * 64),
        ("package_sha256", "2" * 64),
    ],
)
def test_catalog_package_mismatch_rejects_before_journal_or_provisioning(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    """Catches provisioning from catalog metadata that does not authenticate the package."""
    context = _context(tmp_path)
    raw = context["entry"].to_dict()
    raw[field] = value
    changed = MarketplaceCatalogEntry.from_dict(raw)
    unsigned_catalog: dict[str, object] = {
        "schema_version": 2,
        "catalog_id": "test.catalog",
        "publisher_id": "test.publisher",
        "sequence": 2,
        "entries": [changed.to_dict()],
        "signing_key_id": "test-plugin-key",
    }
    signature = base64.b64encode(
        context["private_key"].sign(canonical_bytes(unsigned_catalog))
    ).decode("ascii")
    catalog_path = tmp_path / "changed-catalog.json"
    catalog_path.write_bytes(canonical_bytes({**unsigned_catalog, "signature": signature}))
    catalog = verify_plugin_catalog(catalog_path, context["ring"])

    with pytest.raises(PluginInstallError, match="^plugin_catalog_package_mismatch$"):
        context["installer"].install_from_catalog(
            catalog,
            changed,
            context["verified"],
            "0" * 64,
        )

    assert context["events"] == []
    assert context["installer"].journal_path.exists() is False


def test_legacy_capability_authority_fails_closed_on_review_and_install(
    tmp_path: Path,
) -> None:
    """Catches the retired flat authority shape being admitted for a new install.

    Spec principle 8 forbids a compatibility fork: catalog review (a preview
    of a fresh install) and the install itself must both fail closed with a
    stable typed cause instead of staging a package the verifier flagged as
    the pre-v2 flat shape.
    """
    context = _context(tmp_path, with_legacy_authority=True)
    assert context["verified"].legacy_authority is True

    with pytest.raises(PluginInstallError, match="^plugin_capability_authority_legacy$"):
        context["installer"].review_from_catalog(
            context["catalog"],
            context["entry"],
            context["verified"],
            resolve_dependencies(context["verified"].manifest, (_snapshot(),)),
            expected_registry_revision=context["real_registry"].snapshot().revision,
        )

    with pytest.raises(PluginInstallError, match="^plugin_capability_authority_legacy$"):
        _install(context)

    assert context["events"] == []
    assert context["installer"].journal_path.exists() is False


def test_review_is_deterministic_content_free_and_binds_exact_dependency_projection(
    tmp_path: Path,
) -> None:
    """Catches review hashes depending on descriptions, ordering, URLs, or mutable callers."""
    context = _context(tmp_path)
    resolution_a = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    resolution_b = resolve_dependencies(
        context["verified"].manifest,
        (_snapshot(description="Different screened provider prose."),),
    )
    revision = context["real_registry"].snapshot().revision

    review_a = context["installer"].review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution_a,
        expected_registry_revision=revision,
    )
    review_b = context["installer"].review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution_b,
        expected_registry_revision=revision,
    )

    assert review_a == review_b
    assert review_a.sha256 == hashlib.sha256(canonical_bytes(review_a.to_dict())).hexdigest()
    assert review_a.required_app_ids == ("test.workspace.app",)
    serialized = repr(review_a) + json.dumps(review_a.to_dict(), sort_keys=True)
    for forbidden in (
        "Different screened",
        "List workspace",
        "https://",
        "credential",
        "OAuth",
        "authority",
        "# Workspace",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("mode", ["missing", "replay", "expired", "mismatch"])
def test_confirmation_failure_aborts_before_plugin_stage_or_dependency_commit(
    tmp_path: Path,
    mode: str,
) -> None:
    """Catches confirmation failure publishing or granting dependency authority."""
    context = _context(tmp_path, confirmation_mode=mode)
    confirmation = None if mode == "missing" else ("wrong" if mode == "mismatch" else None)

    with pytest.raises(PluginInstallError, match="^plugin_install_confirmation_"):
        if mode in {"expired", "replay"}:
            _install(context)
        else:
            _install(context, confirmation)

    assert context["events"] == ["prepare", "confirm", "abort"]
    assert context["real_registry"].snapshot().records == ()
    assert context["installer"].journal_path.exists() is False


def test_required_dependency_blocker_aborts_without_confirmation_or_staging(tmp_path: Path) -> None:
    """Catches treating failed capability discovery as an installable empty result."""
    context = _context(tmp_path)
    context["provisioner"].snapshots = ()

    with pytest.raises(PluginInstallError, match="^plugin_dependencies_unresolved$"):
        _install(context, "unused")

    assert context["events"] == ["prepare", "abort"]
    assert context["installer"].journal_path.exists() is False


@pytest.mark.parametrize("seam", ["prepare", "stage", "commit"])
def test_prepublication_failure_cleans_exact_resources_and_preserves_exception_identity(
    tmp_path: Path,
    seam: str,
) -> None:
    """Catches losing cancellation identity or cleaning in the wrong transaction order."""
    context = _context(tmp_path)
    marker = Marker()
    if seam in {"prepare", "commit"}:
        context["provisioner"].failure[seam] = marker
    else:
        context["registry"].failure[seam] = marker

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    if seam == "prepare":
        assert context["events"] == ["prepare", "abort"]
    elif seam == "stage":
        assert context["events"] == ["prepare", "confirm", "stage", "abort"]
    else:
        assert context["events"] == [
            "prepare",
            "confirm",
            "stage",
            "commit",
            "rollback",
            "discard",
        ]
    assert context["installer"].journal_path.exists() is False


def test_unpublished_publication_failure_rolls_back_receipt_then_discards_stage(
    tmp_path: Path,
) -> None:
    """Catches ambiguous publication blindly retaining committed dependencies."""
    context = _context(tmp_path)
    marker = Marker()
    context["registry"].failure["publish"] = marker

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    assert context["events"] == [
        "prepare",
        "confirm",
        "stage",
        "commit",
        "publish",
        "rollback",
        "discard",
    ]
    assert context["installer"].journal_path.exists() is False


def test_stale_registry_revision_rolls_back_receipt_and_discards_stage(tmp_path: Path) -> None:
    """Catches publication conflict leaving committed dependencies or a plugin stage."""
    context = _context(tmp_path)
    context["registry"].force_stale_revision = True

    with pytest.raises(PluginRegistryError, match="^plugin_registry_conflict$"):
        _install(context)

    assert context["events"] == [
        "prepare",
        "confirm",
        "stage",
        "commit",
        "publish",
        "rollback",
        "discard",
    ]
    assert context["provisioner"].rolled_back == [_RECEIPT_ID]
    assert context["installer"].journal_path.exists() is False


def test_ambiguous_publish_that_durably_succeeded_reconciles_without_rollback(
    tmp_path: Path,
) -> None:
    """Catches rolling back shared dependencies after exact publication already committed."""
    context = _context(tmp_path)
    context["registry"].publish_then_raise = True

    result = _install(context)

    assert result.record.plugin_id == "test.workspace"
    assert "rollback" not in context["events"]
    assert context["installer"].journal_path.exists() is False


def test_cleanup_failure_retains_journal_and_reports_recovery_required(tmp_path: Path) -> None:
    """Catches deleting transaction evidence after dependency abort cannot be proved."""
    context = _context(tmp_path, confirmation_mode="missing")
    context["provisioner"].failure["abort"] = RuntimeError("provider secret")

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_required$") as caught:
        _install(context, None)

    assert caught.value.__cause__ is None
    assert context["installer"].journal_path.exists()
    assert b"provider secret" not in context["installer"].journal_path.read_bytes()


def test_rollback_failure_retains_committed_journal_and_reports_recovery_required(
    tmp_path: Path,
) -> None:
    """Catches deleting the receipt needed after rollback could not be proved."""
    context = _context(tmp_path)
    context["registry"].failure["publish"] = RuntimeError("publish failed")
    context["provisioner"].failure["rollback"] = RuntimeError("provider secret")

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_required$") as caught:
        _install(context)

    assert caught.value.__cause__ is None
    assert context["installer"].journal_path.exists()
    assert b"provider secret" not in context["installer"].journal_path.read_bytes()


def test_discard_failure_retains_staged_journal_and_reports_recovery_required(
    tmp_path: Path,
) -> None:
    """Catches deleting exact stage evidence after discard could not be proved."""
    context = _context(tmp_path)
    context["provisioner"].failure["commit"] = RuntimeError("commit failed")
    context["registry"].failure["discard"] = RuntimeError("provider secret")

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_required$") as caught:
        _install(context)

    assert caught.value.__cause__ is None
    assert context["installer"].journal_path.exists()
    assert b"provider secret" not in context["installer"].journal_path.read_bytes()


def _journal(
    context: dict[str, Any],
    state: str,
    *,
    stage_name: str | None = None,
    receipt_id: str | None = None,
    published_revision: str | None = None,
) -> dict[str, object]:
    verified = context["verified"]
    return {
        "schema_version": 1,
        "transaction_id": _TRANSACTION_ID,
        "plugin_id": verified.manifest.plugin_id,
        "publisher_id": verified.manifest.publisher_id,
        "plugin_version": verified.manifest.plugin_version,
        "signing_key_id": verified.manifest.signing_key_id,
        "catalog_sha256": context["catalog"].catalog_sha256,
        "manifest_sha256": verified.manifest_sha256,
        "package_sha256": verified.package_sha256,
        "review_sha256": None if state == "preparing" else "f" * 64,
        "prepared_id": None if state == "preparing" else _PREPARED_ID,
        "stage_name": stage_name,
        "state": state,
        "dependency_receipt_id": receipt_id,
        "expected_registry_revision": context["real_registry"].snapshot().revision,
        "published_revision": published_revision,
        "app_binding": (
            _binding(verified, receipt_id).to_dict()
            if state in {"dependencies_committing", "dependencies_committed", "plugin_published"}
            else None
        ),
    }


def _write_journal(context: dict[str, Any], document: dict[str, object]) -> None:
    path = context["installer"].journal_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(document))


def test_recovery_without_a_journal_is_an_explicit_incomplete_none_result(tmp_path: Path) -> None:
    """Catches absence being confused with a completed transaction recovery."""
    context = _context(tmp_path)

    assert context["installer"].recover() == InstallRecoveryResult(
        transaction_id=None,
        plugin_id=None,
        recovered_state="none",
        completed=False,
        record=None,
        revision=None,
    )


@pytest.mark.parametrize("state", ["preparing", "prepared"])
def test_recovery_aborts_prepared_or_inflight_transaction_by_transaction_id(
    tmp_path: Path,
    state: str,
) -> None:
    """Catches recovery guessing a prepared ID when prepare did not durably return."""
    context = _context(tmp_path)
    _write_journal(context, _journal(context, state))

    result = context["installer"].recover()

    assert result == InstallRecoveryResult(
        transaction_id=_TRANSACTION_ID,
        plugin_id="test.workspace",
        recovered_state="aborted",
        completed=True,
        record=None,
        revision=None,
    )
    assert context["provisioner"].aborted == [_TRANSACTION_ID]
    assert context["installer"].journal_path.exists() is False


def test_recovery_from_plugin_staged_aborts_then_discards_exact_verified_stage(
    tmp_path: Path,
) -> None:
    """Catches recovery deleting an arbitrary stage or leaving provisioned state behind."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    _write_journal(
        context,
        _journal(context, "plugin_staged", stage_name=staged.stage_path.name),
    )

    result = context["installer"].recover()

    assert result.recovered_state == "aborted"
    assert context["events"] == ["abort", "discard"]
    assert staged.stage_path.exists() is False
    assert context["installer"].journal_path.exists() is False


def test_recovery_from_unpublished_dependency_commit_rolls_back_only_receipt_then_discards(
    tmp_path: Path,
) -> None:
    """Catches recovery removing shared pre-existing apps or publishing partial state."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    receipt_id = _RECEIPT_ID
    _write_journal(
        context,
        _journal(
            context,
            "dependencies_committed",
            stage_name=staged.stage_path.name,
            receipt_id=receipt_id,
        ),
    )

    result = context["installer"].recover()

    assert result.recovered_state == "rolled_back"
    assert context["provisioner"].rolled_back == [receipt_id]
    assert context["events"] == ["rollback", "discard"]
    assert context["installer"].journal_path.exists() is False


@pytest.mark.parametrize("journal_state", ["dependencies_committed", "plugin_published"])
def test_recovery_accepts_only_exact_reverified_publication_without_rollback(
    tmp_path: Path,
    journal_state: str,
) -> None:
    """Catches a crash after publication rolling back an already installed transaction."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    published = context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    document = _journal(
        context,
        journal_state,
        stage_name=staged.stage_path.name,
        receipt_id=_RECEIPT_ID,
        published_revision=published.revision if journal_state == "plugin_published" else None,
    )
    document["expected_registry_revision"] = hashlib.sha256(
        canonical_bytes({"schema_version": 1, "plugins": {}})
    ).hexdigest()
    _write_journal(context, document)

    result = context["installer"].recover()

    assert result.recovered_state == "published"
    assert result.record == published.record
    assert context["provisioner"].rolled_back == []
    assert context["installer"].journal_path.exists() is False


def test_mixed_publication_recovery_fails_closed_and_retains_journal(tmp_path: Path) -> None:
    """Catches a record/tree split being guessed as published or safely unpublished."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    published = context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    target = context["real_registry"].packages_root / "test.workspace" / "1.0.0"
    (target / "skills" / "workspace" / "SKILL.md").write_bytes(b"changed")
    document = _journal(
        context,
        "plugin_published",
        stage_name=staged.stage_path.name,
        receipt_id=_RECEIPT_ID,
        published_revision=published.revision,
    )
    _write_journal(context, document)

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_failed$"):
        context["installer"].recover()

    assert context["installer"].journal_path.exists()
    assert context["provisioner"].rolled_back == []


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"schema_version":1,"schema_version":1}',
        b'{ "schema_version": 1 }',
    ],
)
def test_corrupt_duplicate_or_noncanonical_journal_never_means_empty(
    tmp_path: Path,
    payload: bytes,
) -> None:
    """Catches malformed durable recovery evidence being silently discarded."""
    context = _context(tmp_path)
    context["installer"].journal_path.parent.mkdir(parents=True, exist_ok=True)
    context["installer"].journal_path.write_bytes(payload)

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_failed$"):
        context["installer"].recover()

    assert context["installer"].journal_path.read_bytes() == payload


def test_reparse_journal_is_rejected_without_following_or_deleting_target(tmp_path: Path) -> None:
    """Catches recovery following attacker-swapped journal content outside its pinned leaf."""
    context = _context(tmp_path)
    journal_path = context["installer"].journal_path
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside-journal.json"
    payload = canonical_bytes(_journal(context, "preparing"))
    target.write_bytes(payload)
    try:
        journal_path.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable on this platform")

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_failed$"):
        context["installer"].recover()

    assert journal_path.is_symlink()
    assert target.read_bytes() == payload


def test_two_installers_share_one_full_transaction_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a per-call lock allowing two prepare phases to overlap."""
    from plugin_platform import installer as installer_module

    @contextmanager
    def no_sidecar_lock(_directory: object, _name: str):
        yield

    monkeypatch.setattr(installer_module.PinnedDirectory, "lock_file", no_sidecar_lock)
    first = _context(tmp_path)
    second_events: list[str] = []
    second_provisioner = FakeProvisioner(second_events)
    second_confirmer = FakeConfirmation(second_events, mode="accept")
    transaction_ids = iter(("1" * 32,))
    second = PluginInstaller(
        registry=first["registry"],
        provisioner=second_provisioner,
        confirmation_verifier=second_confirmer,
        transaction_id_factory=lambda: next(transaction_ids),
    )
    entered = threading.Event()
    release = threading.Event()
    original_prepare = first["provisioner"].prepare

    def blocking_prepare(transaction_id: str, requirements: tuple[Any, ...]):
        entered.set()
        assert release.wait(timeout=5)
        return original_prepare(transaction_id, requirements)

    first["provisioner"].prepare = blocking_prepare
    errors: list[BaseException] = []
    threads = [
        threading.Thread(target=lambda: _capture(errors, lambda: _install(first))),
        threading.Thread(
            target=lambda: _capture(
                errors,
                lambda: second.install_from_catalog(
                    first["catalog"],
                    first["entry"],
                    first["verified"],
                    "visible-confirmation",
                ),
            )
        ),
    ]

    threads[0].start()
    assert entered.wait(timeout=5)
    threads[1].start()
    assert second_events == []
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert second_events[:2] == ["prepare", "confirm"]


def _capture(errors: list[BaseException], operation: Callable[[], object]) -> None:
    try:
        operation()
    except BaseException as exc:
        errors.append(exc)


def test_journal_transitions_are_exact_canonical_extended_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches reordered states, open fields, noncanonical bytes, or sensitive journal content."""
    from plugin_platform import installer as installer_module

    context = _context(tmp_path)
    documents: list[dict[str, object]] = []

    def observe(path: Path) -> None:
        payload = path.read_bytes()
        document = json.loads(payload)
        assert canonical_bytes(document) == payload
        documents.append(document)

    monkeypatch.setattr(installer_module, "_after_journal_write", observe)

    _install(context)

    states = [document["state"] for document in documents]
    assert states == [
        "preparing",
        "prepared",
        "plugin_staging",
        "plugin_staged",
        "dependencies_committing",
        "dependencies_committed",
        "plugin_published",
    ]
    expected_fields = {
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
    assert all(set(document) == expected_fields for document in documents)
    assert documents[0]["review_sha256"] is None
    assert documents[0]["prepared_id"] is None
    assert documents[3]["app_binding"] is None
    assert documents[4]["app_binding"] == _binding(context["verified"]).to_dict()
    assert documents[-1]["published_revision"] is not None
    serialized = json.dumps(documents, sort_keys=True)
    for forbidden in (
        "List workspace",
        "https://",
        "credential",
        "OAuth",
        "authority",
        "# Workspace",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("failure_call", "expected_events"),
    [
        (1, ["abort"]),
        (2, ["prepare", "abort"]),
        (3, ["prepare", "confirm", "abort"]),
        (4, ["prepare", "confirm", "stage", "abort", "discard"]),
        (
            5,
            ["prepare", "confirm", "stage", "rollback", "discard"],
        ),
        (
            6,
            ["prepare", "confirm", "stage", "commit", "rollback", "discard"],
        ),
    ],
)
def test_atomic_journal_transition_failure_preserves_previous_state_and_exception_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
    expected_events: list[str],
) -> None:
    """Catches truncate-in-place journals or losing a transition cancellation."""
    from plugin_platform import installer as installer_module

    context = _context(tmp_path)
    marker = Marker()
    calls = 0

    def fail_second(_temporary: Path, _target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise marker

    monkeypatch.setattr(installer_module, "_before_journal_replace", fail_second)

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    assert context["events"] == expected_events
    assert context["installer"].journal_path.exists() is False


def test_journal_write_preserves_active_cancellation_when_temp_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches temporary-file cleanup masking the active cancellation identity."""
    from plugin_platform import installer as installer_module

    context = _context(tmp_path)
    marker = Marker()
    real_unlink = installer_module.PinnedDirectory.unlink

    def cancel_before_replace(_temporary: Path, _target: Path) -> None:
        raise marker

    def fail_temporary_cleanup(
        directory: Any,
        relative_path: str | Path,
        *,
        missing_ok: bool = False,
    ) -> None:
        if str(relative_path).startswith(f".{core_paths.PLUGIN_INSTALL_JOURNAL_PATH.name}."):
            raise RuntimeError("temporary cleanup failed")
        real_unlink(directory, relative_path, missing_ok=missing_ok)

    monkeypatch.setattr(installer_module, "_before_journal_replace", cancel_before_replace)
    monkeypatch.setattr(installer_module.PinnedDirectory, "unlink", fail_temporary_cleanup)

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker


def test_durable_plugin_published_journal_ordinary_failure_reconciles_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches rolling back after the terminal journal and publication are both durable."""
    from plugin_platform import installer as installer_module

    context = _context(tmp_path)

    def fail_terminal(path: Path) -> None:
        if json.loads(path.read_bytes())["state"] == "plugin_published":
            raise RuntimeError("ambiguous terminal journal")

    monkeypatch.setattr(installer_module, "_after_journal_write", fail_terminal)

    result = _install(context)

    assert result.record.plugin_id == "test.workspace"
    assert "rollback" not in context["events"]
    assert context["installer"].journal_path.exists() is False


def test_terminal_clear_cancellation_restores_exact_recoverable_publication_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches delete-after-effect cancellation losing the terminal recovery journal."""
    from plugin_platform import installer as installer_module

    context = _context(tmp_path)
    marker = Marker()
    real_unlink = installer_module.PinnedDirectory.unlink
    cancelled = False

    def delete_then_cancel(
        directory: Any,
        relative_path: str | Path,
        *,
        missing_ok: bool = False,
    ) -> None:
        nonlocal cancelled
        real_unlink(directory, relative_path, missing_ok=missing_ok)
        if str(relative_path) == core_paths.PLUGIN_INSTALL_JOURNAL_PATH.name and not cancelled:
            cancelled = True
            raise marker

    monkeypatch.setattr(installer_module.PinnedDirectory, "unlink", delete_then_cancel)

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    payload = context["installer"].journal_path.read_bytes()
    document = json.loads(payload)
    assert canonical_bytes(document) == payload
    assert document["state"] == "plugin_published"
    assert document["published_revision"] == context["real_registry"].snapshot().revision

    recovered = context["installer"].recover()

    assert recovered.recovered_state == "published"
    assert recovered.revision == document["published_revision"]
    assert context["installer"].journal_path.exists() is False


def test_terminal_clear_ordinary_ambiguity_never_loses_published_recovery_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches repeated post-delete ordinary failures turning publication into no journal."""
    from plugin_platform import installer as installer_module

    context = _context(tmp_path)
    real_unlink = installer_module.PinnedDirectory.unlink
    failures_remaining = 2

    def delete_then_fail(
        directory: Any,
        relative_path: str | Path,
        *,
        missing_ok: bool = False,
    ) -> None:
        nonlocal failures_remaining
        real_unlink(directory, relative_path, missing_ok=missing_ok)
        if str(relative_path) == core_paths.PLUGIN_INSTALL_JOURNAL_PATH.name and failures_remaining:
            failures_remaining -= 1
            raise RuntimeError("ambiguous terminal clear")

    monkeypatch.setattr(installer_module.PinnedDirectory, "unlink", delete_then_fail)

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_failed$"):
        _install(context)

    payload = context["installer"].journal_path.read_bytes()
    document = json.loads(payload)
    assert canonical_bytes(document) == payload
    assert document["state"] == "plugin_published"
    assert document["published_revision"] == context["real_registry"].snapshot().revision

    recovered = context["installer"].recover()

    assert recovered.recovered_state == "published"
    assert recovered.revision == document["published_revision"]
    assert context["installer"].journal_path.exists() is False


def test_public_install_records_are_deeply_immutable_and_reject_direct_forgery(
    tmp_path: Path,
) -> None:
    """Catches mutable sequences, inconsistent receipts/results, and unsafe public IDs."""
    context = _context(tmp_path)
    result = _install(context)
    prepared = next(iter(context["provisioner"].prepared_by_transaction.values()))
    receipt = result.dependency_receipt

    with pytest.raises(FrozenInstanceError):
        prepared.prepared_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="^prepared dependency set is invalid$"):
        PreparedDependencySet(
            transaction_id="https://provider.example",
            prepared_id="prepared-safe",
            receipt_id="receipt-safe",
            app_snapshots=prepared.app_snapshots,
        )
    with pytest.raises(ValueError, match="^prepared dependency set is invalid$"):
        PreparedDependencySet(
            transaction_id=prepared.transaction_id,
            prepared_id=prepared.prepared_id,
            receipt_id=prepared.receipt_id,
            app_snapshots=list(prepared.app_snapshots),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="^dependency commit receipt is invalid$"):
        DependencyCommitReceipt(
            receipt_id=receipt.receipt_id,
            prepared_id="different-prepared",
            introduced_app_ids=("https://provider.example",),
        )
    with pytest.raises(ValueError, match="^plugin install result is invalid$"):
        PluginInstallResult(
            transaction_id=result.transaction_id,
            review_sha256=result.review_sha256,
            record=replace(result.record, status="broken"),
            dependency_receipt=receipt,
            revision=result.revision,
        )
    with pytest.raises(ValueError, match="^install recovery result is invalid$"):
        InstallRecoveryResult(
            transaction_id=None,
            plugin_id=None,
            recovered_state="published",
            completed=False,
            record=result.record,
            revision=result.revision,
        )


def test_review_constructor_rejects_hash_forgery_duplicates_and_unsafe_content(
    tmp_path: Path,
) -> None:
    """Catches bypassing review construction with duplicate or provider-shaped identities."""
    context = _context(tmp_path)
    resolution = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    review = context["installer"].review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution,
        expected_registry_revision=context["real_registry"].snapshot().revision,
    )

    with pytest.raises(FrozenInstanceError):
        review.sha256 = "0" * 64  # type: ignore[misc]
    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        replace(review, required_app_ids=("test.workspace.app", "test.workspace.app"))
    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        replace(review, dependencies=review.dependencies + review.dependencies)
    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        replace(review, kind="app_only")
    with pytest.raises(ValueError, match="^plugin install review is invalid$") as caught:
        replace(review, plugin_id="https://provider.example/?credential=DO-NOT-LEAK")
    assert caught.value.__cause__ is None
    assert "provider" not in str(caught.value)
    assert "credential" not in str(caught.value)


@pytest.mark.parametrize("lane", ["required", "optional"])
def test_skill_only_review_requires_zero_app_dependencies(
    tmp_path: Path,
    lane: str,
) -> None:
    """Catches a skill-only review carrying authority from either dependency lane."""
    context = _context(tmp_path)
    resolution = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    review = context["installer"].review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution,
        expected_registry_revision=context["real_registry"].snapshot().revision,
    )
    dependency = review.dependencies[0]
    changes: dict[str, object] = {
        "kind": "skill_only",
        "required_app_ids": (),
        "optional_app_ids": (),
        "dependencies": (),
    }
    if lane == "required":
        changes.update(
            required_app_ids=(dependency.app_id,),
            dependencies=(dependency,),
        )
    else:
        changes.update(
            optional_app_ids=(dependency.app_id,),
            dependencies=(replace(dependency, required=False),),
        )

    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        replace(review, **changes)


@pytest.mark.parametrize("required_count", [0, 2])
def test_mcp_backed_review_requires_exactly_one_required_app(
    tmp_path: Path,
    required_count: int,
) -> None:
    """Catches an MCP-backed review with too little or too much required authority."""
    context = _context(tmp_path)
    resolution = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    review = context["installer"].review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution,
        expected_registry_revision=context["real_registry"].snapshot().revision,
    )
    dependency = review.dependencies[0]
    required_ids: tuple[str, ...] = ()
    dependencies: tuple[PluginDependencyReview, ...] = ()
    if required_count == 2:
        second = replace(dependency, app_id="test.second.app")
        required_ids = (dependency.app_id, second.app_id)
        dependencies = (second, dependency)

    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        replace(
            review,
            required_app_ids=required_ids,
            optional_app_ids=(),
            dependencies=dependencies,
        )


def test_composite_review_requires_at_least_two_required_apps(tmp_path: Path) -> None:
    """Catches optional or single-app evidence satisfying composite cardinality."""
    context = _context(tmp_path, include_optional=True)
    resolution = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    review = context["installer"].review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution,
        expected_registry_revision=context["real_registry"].snapshot().revision,
    )

    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        replace(review, kind="composite")


def test_installer_source_and_outputs_contain_no_authority_or_provider_content(
    tmp_path: Path,
) -> None:
    """Catches results or journal surfaces acquiring package/provider material."""
    context = _context(tmp_path)
    result = _install(context)
    rendered = repr(result)
    for forbidden in (
        "List workspace",
        "https://",
        "credential",
        "OAuth",
        "authority",
        "# Workspace",
    ):
        assert forbidden not in rendered


def _use_recoverable_transaction(context: dict[str, Any]) -> None:
    context["installer"].transaction_id_factory = lambda: _TRANSACTION_ID


def test_transaction_id_is_exact_lowercase_hex_before_journaling(tmp_path: Path) -> None:
    """Catches provider-shaped transaction IDs entering stage and recovery identities."""
    context = _context(tmp_path)
    context["installer"].transaction_id_factory = lambda: "transaction-0001"

    with pytest.raises(PluginInstallError, match="^plugin_install_transaction_invalid$"):
        _install(context)

    assert context["events"] == []
    assert context["installer"].journal_path.exists() is False


def test_stage_identity_is_durable_before_exact_named_stage_side_effect(tmp_path: Path) -> None:
    """Catches an unjournaled random stage surviving a crash after materialization."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)

    _install(context)

    assert context["registry"].stage_requests == [_STAGE_NAME]
    assert len(context["registry"].stage_journals) == 1
    stage_journal = context["registry"].stage_journals[0]
    assert stage_journal["state"] == "plugin_staging"
    assert stage_journal["transaction_id"] == _TRANSACTION_ID
    assert stage_journal["stage_name"] == _STAGE_NAME


def test_stage_after_effect_cancellation_discards_exact_journaled_stage_and_reraises(
    tmp_path: Path,
) -> None:
    """Catches cancellation between stage durability and its return leaking storage."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)
    marker = Marker()
    context["registry"].after_stage_failure = marker

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    assert context["registry"].stage_requests == [_STAGE_NAME]
    assert list(context["real_registry"].packages_root.glob(".installing-*")) == []
    assert context["installer"].journal_path.exists() is False


def test_plugin_staging_hard_crash_recovery_discards_exact_materialized_stage(
    tmp_path: Path,
) -> None:
    """Catches restart recovery treating a maybe-created deterministic stage as absent."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(
        context["verified"],
        stage_name=_STAGE_NAME,
    )
    document = _journal(context, "plugin_staging", stage_name=_STAGE_NAME)
    document["transaction_id"] = _TRANSACTION_ID
    document["prepared_id"] = _PREPARED_ID
    _write_journal(context, document)

    result = context["installer"].recover()

    assert result.recovered_state == "aborted"
    assert staged.stage_path.exists() is False
    assert context["events"] == ["abort", "discard"]
    assert context["installer"].journal_path.exists() is False


def test_recovery_rejects_another_transactions_stage_without_touching_it(
    tmp_path: Path,
) -> None:
    """Catches a valid but cross-transaction stage name authorizing discard."""
    context = _context(tmp_path)
    other_transaction_id = "1" * 32
    other_stage_name = f".installing-{other_transaction_id}"
    staged = context["real_registry"].stage(
        context["verified"],
        stage_name=other_stage_name,
    )
    document = _journal(context, "plugin_staged", stage_name=other_stage_name)
    assert document["transaction_id"] == _TRANSACTION_ID
    _write_journal(context, document)

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_failed$"):
        context["installer"].recover()

    assert staged.stage_path.exists()
    assert context["events"] == []
    assert context["installer"].journal_path.exists()


def test_predeclared_receipt_is_durable_before_dependency_commit_side_effect(
    tmp_path: Path,
) -> None:
    """Catches commit authority becoming durable before its rollback identity."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)

    _install(context)

    prepared = context["provisioner"].prepared_by_transaction[_TRANSACTION_ID]
    assert prepared.receipt_id == _RECEIPT_ID
    assert len(context["provisioner"].commit_journals) == 1
    commit_journal = context["provisioner"].commit_journals[0]
    assert commit_journal["state"] == "dependencies_committing"
    assert commit_journal["dependency_receipt_id"] == _RECEIPT_ID
    assert commit_journal["app_binding"] == _binding(context["verified"]).to_dict()


def test_legacy_migration_journals_its_closed_binding_before_dependency_commit(
    tmp_path: Path,
) -> None:
    """Catches a crash after migration commit stranding app changes without recovery evidence."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    current = json.loads(context["real_registry"].registry_path.read_bytes())
    legacy_record = dict(current["plugins"]["test.workspace"])
    legacy_record.pop("app_binding")
    context["real_registry"].registry_path.write_bytes(
        canonical_bytes({"schema_version": 1, "plugins": {"test.workspace": legacy_record}})
    )

    context["installer"].reconcile_installed_binding("test.workspace")

    assert len(context["provisioner"].commit_journals) == 1
    journal = context["provisioner"].commit_journals[0]
    assert journal["state"] == "dependencies_committing"
    assert journal["app_binding"] == _binding(context["verified"]).to_dict()
    assert journal["dependency_receipt_id"] == _RECEIPT_ID


def test_restarted_migration_recovery_rolls_back_an_unbound_committed_receipt(
    tmp_path: Path,
) -> None:
    """Catches a crash after migration commit retaining app effects without a bound owner."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    current = json.loads(context["real_registry"].registry_path.read_bytes())
    legacy_record = dict(current["plugins"]["test.workspace"])
    legacy_record.pop("app_binding")
    context["real_registry"].registry_path.write_bytes(
        canonical_bytes({"schema_version": 1, "plugins": {"test.workspace": legacy_record}})
    )
    prepared = context["provisioner"].prepare(
        _TRANSACTION_ID,
        context["verified"].manifest.required_apps,
    )
    context["provisioner"].commit(prepared)
    document = {
        "schema_version": 1,
        "transaction_id": _TRANSACTION_ID,
        "plugin_id": "test.workspace",
        "publisher_id": "test.publisher",
        "plugin_version": "1.0.0",
        "signing_key_id": "test-plugin-key",
        "catalog_sha256": None,
        "manifest_sha256": context["verified"].manifest_sha256,
        "package_sha256": context["verified"].package_sha256,
        "review_sha256": None,
        "prepared_id": _PREPARED_ID,
        "stage_name": None,
        "state": "dependencies_committed",
        "dependency_receipt_id": _RECEIPT_ID,
        "expected_registry_revision": context["real_registry"].snapshot().revision,
        "published_revision": None,
        "app_binding": _binding(context["verified"]).to_dict(),
    }
    _write_journal(context, document)
    restarted = PluginInstaller(
        registry=context["registry"],
        provisioner=context["provisioner"],
        confirmation_verifier=context["confirmer"],
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )

    recovery = restarted.recover()

    assert recovery.recovered_state == "rolled_back"
    assert context["provisioner"].rolled_back == [_RECEIPT_ID]
    assert context["real_registry"].get("test.workspace").app_binding is None
    assert restarted.journal_path.exists() is False


def test_registry_capacity_rejection_prevents_a_dependency_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches discovering an unwriteable binding only after dependency authority changes."""
    import plugin_platform.registry as registry_module

    context = _context(tmp_path)
    monkeypatch.setattr(registry_module, "_MAX_REGISTRY_BYTES", 1)

    with pytest.raises(PluginRegistryError):
        _install(context)
    assert "commit" not in context["events"]


def test_journal_capacity_rejection_prevents_any_dependency_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches journal bytes becoming unreadable after an accepted side effect."""
    import plugin_platform.installer as installer_module

    context = _context(tmp_path)
    monkeypatch.setattr(installer_module, "_MAX_JOURNAL_BYTES", 1)

    with pytest.raises(PluginInstallError, match="^plugin_install_journal_limit_exceeded$"):
        _install(context)

    assert context["events"] == ["abort"]


def test_real_durable_migration_preserves_shared_app_and_private_state_bytes(
    tmp_path: Path,
) -> None:
    """Catches migration reconstructing an existing app or touching adjacent private state."""
    from mcp_platform.app_registry import McpAppRegistry
    from mcp_platform.app_sources import TrustedRemoteAppSource
    from mcp_platform.provisioner import McpAppProvisioner
    from mcp_platform.registry import PackageRegistry

    context = _context(tmp_path, remote_dependency=True)
    state_root = tmp_path / "mcp"

    class SignedRemoteResolver:
        def resolve(self, requirement: Any) -> TrustedRemoteAppSource:
            return TrustedRemoteAppSource(requirement=requirement)

    app_registry = McpAppRegistry(state_root=state_root)
    provisioner = McpAppProvisioner(
        app_registry=app_registry,
        package_registry=PackageRegistry(state_root=state_root),
        source_resolver=SignedRemoteResolver(),
    )
    requirement = context["verified"].manifest.required_apps
    shared = provisioner.prepare("1" * 32, requirement)
    provisioner.commit(shared)
    app_before = app_registry.get("test.workspace.app").to_dict()
    sentinels = {
        state_root / "account-state.bin": b"opaque-account-state",
        state_root / "connection-state.bin": b"opaque-connection-state",
        state_root / "token-state.bin": b"opaque-token-state",
    }
    for path, payload in sentinels.items():
        path.write_bytes(payload)

    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    current = json.loads(context["real_registry"].registry_path.read_bytes())
    legacy_record = dict(current["plugins"]["test.workspace"])
    legacy_record.pop("app_binding")
    context["real_registry"].registry_path.write_bytes(
        canonical_bytes({"schema_version": 1, "plugins": {"test.workspace": legacy_record}})
    )
    installer = PluginInstaller(
        registry=context["registry"],
        provisioner=provisioner,
        confirmation_verifier=context["confirmer"],
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )

    migrated = installer.reconcile_installed_binding("test.workspace")

    assert migrated.record.app_binding is not None
    assert migrated.record.app_binding.required_app_ids == ("test.workspace.app",)
    assert migrated.record.app_binding.optional_app_ids == ()
    assert migrated.record.app_binding.dependency_receipt_id == f"mcp-receipt-{_TRANSACTION_ID}"
    assert app_registry.get("test.workspace.app").to_dict() == app_before
    assert {path: path.read_bytes() for path in sentinels} == sentinels


def test_real_durable_migration_keeps_an_unmatched_app_owner_unbound(tmp_path: Path) -> None:
    """Catches migration claiming an app whose durable signed declaration does not match."""
    from mcp_platform.app_registry import InstalledMcpAppRecord, McpAppRegistry
    from mcp_platform.app_sources import TrustedRemoteAppSource
    from mcp_platform.provisioner import McpAppProvisionError, McpAppProvisioner
    from mcp_platform.registry import PackageRegistry

    context = _context(tmp_path, remote_dependency=True)
    state_root = tmp_path / "mcp"

    class SignedRemoteResolver:
        def resolve(self, requirement: Any) -> TrustedRemoteAppSource:
            return TrustedRemoteAppSource(requirement=requirement)

    app_registry = McpAppRegistry(state_root=state_root)
    provisioner = McpAppProvisioner(
        app_registry=app_registry,
        package_registry=PackageRegistry(state_root=state_root),
        source_resolver=SignedRemoteResolver(),
    )
    mismatched_package = _signed_package(
        tmp_path / "mismatched",
        context["private_key"],
        remote_dependency=True,
        provider_id="other-provider",
    )
    mismatched = verify_plugin_package(
        mismatched_package, context["ring"], "3.0.0"
    ).manifest.required_apps[0]
    app_registry.install(
        InstalledMcpAppRecord.from_source(TrustedRemoteAppSource(requirement=mismatched)),
        expected_revision=app_registry.snapshot().revision,
    )
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    current = json.loads(context["real_registry"].registry_path.read_bytes())
    legacy_record = dict(current["plugins"]["test.workspace"])
    legacy_record.pop("app_binding")
    context["real_registry"].registry_path.write_bytes(
        canonical_bytes({"schema_version": 1, "plugins": {"test.workspace": legacy_record}})
    )
    installer = PluginInstaller(
        registry=context["registry"],
        provisioner=provisioner,
        confirmation_verifier=context["confirmer"],
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )

    with pytest.raises(McpAppProvisionError, match="^mcp_app_provision_source_invalid$"):
        installer.reconcile_installed_binding("test.workspace")

    assert context["real_registry"].get("test.workspace").app_binding is None


def test_legacy_registry_binding_migration_reverifies_and_preserves_shared_dependencies(
    tmp_path: Path,
) -> None:
    """Catches migration inferring ownership or rebuilding installed app state."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    current = json.loads(context["real_registry"].registry_path.read_bytes())
    legacy_record = dict(current["plugins"]["test.workspace"])
    legacy_record.pop("app_binding")
    context["real_registry"].registry_path.write_bytes(
        canonical_bytes({"schema_version": 1, "plugins": {"test.workspace": legacy_record}})
    )
    context["provisioner"].introduced_app_ids_override = ()

    migrated = context["installer"].reconcile_installed_binding("test.workspace")

    assert migrated.record.app_binding == _binding(context["verified"])
    assert migrated.record.app_binding is not None
    assert migrated.record.app_binding.required_app_ids == ("test.workspace.app",)
    assert context["provisioner"].rolled_back == []
    assert context["real_registry"].get("test.workspace").app_binding == migrated.record.app_binding


def test_tampered_legacy_package_stays_without_an_interaction_binding(tmp_path: Path) -> None:
    """Catches migration publishing ownership after installed package authentication fails."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    current = json.loads(context["real_registry"].registry_path.read_bytes())
    legacy_record = dict(current["plugins"]["test.workspace"])
    legacy_record.pop("app_binding")
    context["real_registry"].registry_path.write_bytes(
        canonical_bytes({"schema_version": 1, "plugins": {"test.workspace": legacy_record}})
    )
    (
        context["real_registry"].packages_root / "test.workspace" / "1.0.0" / "plugin.json"
    ).write_bytes(b"tampered")

    with pytest.raises(PluginRegistryError, match="^plugin_registry_reconciliation_failed$"):
        context["installer"].reconcile_installed_binding("test.workspace")

    assert context["real_registry"].get("test.workspace").app_binding is None


def test_recovery_rejects_tampered_journal_binding_before_any_rollback(tmp_path: Path) -> None:
    """Catches recovery treating altered closed ownership evidence as a valid receipt."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    document = _journal(
        context,
        "dependencies_committed",
        stage_name=staged.stage_path.name,
        receipt_id=_RECEIPT_ID,
    )
    assert isinstance(document["app_binding"], dict)
    document["app_binding"] = {**document["app_binding"], "revision": "f" * 64}
    _write_journal(context, document)

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_failed$"):
        context["installer"].recover()

    assert context["provisioner"].rolled_back == []
    assert staged.stage_path.exists()


def test_published_recovery_requires_the_exact_journaled_app_binding(tmp_path: Path) -> None:
    """Catches recovery accepting a package record owned by a different selected optional app set."""
    context = _context(tmp_path, include_optional=True)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    published = context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    selected_optional = PluginAppBinding(
        required_app_ids=("test.workspace.app",),
        optional_app_ids=("test.optional.app",),
        dependency_receipt_id=_RECEIPT_ID,
        revision=hashlib.sha256(
            canonical_bytes(
                {
                    "required_app_ids": ["test.workspace.app"],
                    "optional_app_ids": ["test.optional.app"],
                    "dependency_receipt_id": _RECEIPT_ID,
                }
            )
        ).hexdigest(),
    )
    document = _journal(
        context,
        "plugin_published",
        stage_name=staged.stage_path.name,
        receipt_id=_RECEIPT_ID,
        published_revision=published.revision,
    )
    document["app_binding"] = selected_optional.to_dict()
    _write_journal(context, document)

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_failed$"):
        context["installer"].recover()

    assert context["installer"].journal_path.exists()


def test_commit_after_effect_cancellation_rolls_back_predeclared_receipt_and_reraises(
    tmp_path: Path,
) -> None:
    """Catches cancellation after commit taking effect being mistaken for pre-commit abort."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)
    marker = Marker()
    context["provisioner"].after_commit_failure = marker

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    assert context["provisioner"].rolled_back == [_RECEIPT_ID]
    assert context["events"][-2:] == ["rollback", "discard"]
    assert context["installer"].journal_path.exists() is False


def test_dependencies_committing_hard_crash_recovery_rolls_back_predeclared_receipt(
    tmp_path: Path,
) -> None:
    """Catches restart recovery guessing whether dependency commit took effect."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(
        context["verified"],
        stage_name=_STAGE_NAME,
    )
    document = _journal(
        context,
        "dependencies_committing",
        stage_name=_STAGE_NAME,
        receipt_id=_RECEIPT_ID,
    )
    document["transaction_id"] = _TRANSACTION_ID
    document["prepared_id"] = _PREPARED_ID
    _write_journal(context, document)

    result = context["installer"].recover()

    assert result.recovered_state == "rolled_back"
    assert context["provisioner"].rolled_back == [_RECEIPT_ID]
    assert staged.stage_path.exists() is False
    assert context["installer"].journal_path.exists() is False


def test_cancellation_identity_survives_cleanup_failure_with_recovery_evidence(
    tmp_path: Path,
) -> None:
    """Catches cleanup failure translating an active non-Exception cancellation."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)
    marker = Marker()
    context["registry"].failure["stage"] = marker
    context["provisioner"].failure["abort"] = RuntimeError("provider secret")

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    assert context["installer"].journal_path.exists()


def test_cleanup_originated_cancellation_preserves_identity_and_recovery_evidence(
    tmp_path: Path,
) -> None:
    """Catches cancellation from cleanup being normalized behind an ordinary failure."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)
    marker = Marker()
    context["registry"].failure["stage"] = RuntimeError("stage failed")
    context["provisioner"].failure["abort"] = marker

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    assert context["installer"].journal_path.exists()


def test_recovery_preserves_cleanup_cancellation_identity_and_durable_evidence(
    tmp_path: Path,
) -> None:
    """Catches explicit recovery translating a non-Exception cleanup signal."""
    context = _context(tmp_path)
    marker = Marker()
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    _write_journal(
        context,
        _journal(
            context,
            "dependencies_committed",
            stage_name=staged.stage_path.name,
            receipt_id=_RECEIPT_ID,
        ),
    )
    context["provisioner"].failure["rollback"] = marker

    with pytest.raises(Marker) as caught:
        context["installer"].recover()

    assert caught.value is marker
    assert context["installer"].journal_path.exists()
    assert staged.stage_path.exists()


def test_published_after_effect_cancellation_advances_evidence_then_reraises_identity(
    tmp_path: Path,
) -> None:
    """Catches durable publication converting KeyboardInterrupt-like signals to success."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)
    marker = Marker()
    context["registry"].after_publish_failure = marker

    with pytest.raises(Marker) as caught:
        _install(context)

    assert caught.value is marker
    journal = json.loads(context["installer"].journal_path.read_bytes())
    assert journal["state"] == "plugin_published"
    assert journal["published_revision"] == context["real_registry"].snapshot().revision
    assert context["provisioner"].rolled_back == []


def test_plugin_published_recovery_requires_exact_journaled_revision(tmp_path: Path) -> None:
    """Catches terminal recovery clearing evidence for a different registry revision."""
    context = _context(tmp_path)
    staged = context["real_registry"].stage(context["verified"], stage_name=_STAGE_NAME)
    published = context["real_registry"].publish(
        staged,
        context["real_registry"].snapshot().revision,
        app_binding=_binding(context["verified"]),
    )
    document = _journal(
        context,
        "plugin_published",
        stage_name=staged.stage_path.name,
        receipt_id=_RECEIPT_ID,
        published_revision="f" * 64,
    )
    assert document["published_revision"] != published.revision
    _write_journal(context, document)

    with pytest.raises(PluginInstallError, match="^plugin_install_recovery_failed$"):
        context["installer"].recover()

    assert context["installer"].journal_path.exists()
    assert context["provisioner"].rolled_back == []


def _extra_snapshot() -> AppCapabilitySnapshot:
    return replace(
        _snapshot(),
        app_id="test.unrequested.app",
        provider_id="unrequested",
    )


def test_prepared_snapshots_reject_unrequested_identity_before_confirmation(tmp_path: Path) -> None:
    """Catches provisioner output smuggling an undeclared app into reviewed state."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)
    context["provisioner"].snapshots = (_snapshot(), _extra_snapshot())

    with pytest.raises(PluginInstallError, match="^plugin_dependency_preparation_invalid$"):
        _install(context)

    assert context["events"] == ["prepare", "abort"]
    assert context["confirmer"].expected_hashes == []


def test_review_rejects_missing_required_resolution_coverage(tmp_path: Path) -> None:
    """Catches a ready result omitting a required app and its blocker."""
    context = _context(tmp_path)
    forged = DependencyResolution((), (), (), (), True)

    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        context["installer"].review_from_catalog(
            context["catalog"],
            context["entry"],
            context["verified"],
            forged,
            expected_registry_revision=context["real_registry"].snapshot().revision,
        )


def test_review_rejects_missing_optional_resolution_or_blocker_coverage(tmp_path: Path) -> None:
    """Catches absent optional evidence being projected as a reviewed unresolved app."""
    context = _context(tmp_path, include_optional=True)
    resolved = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    assert resolved.optional_blockers
    forged = DependencyResolution(
        resolved.required,
        resolved.optional,
        resolved.blockers,
        (),
        resolved.ready,
    )

    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        context["installer"].review_from_catalog(
            context["catalog"],
            context["entry"],
            context["verified"],
            forged,
            expected_registry_revision=context["real_registry"].snapshot().revision,
        )


def test_review_required_flags_must_match_required_and_optional_lanes(tmp_path: Path) -> None:
    """Catches a direct review constructor relabeling required authority as optional."""
    context = _context(tmp_path)
    resolution = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    review = context["installer"].review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution,
        expected_registry_revision=context["real_registry"].snapshot().revision,
    )
    forged_dependency = replace(review.dependencies[0], required=False)

    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        replace(review, dependencies=(forged_dependency,))


@pytest.mark.parametrize(
    ("receipt_id_override", "introduced_app_ids_override"),
    [
        ("different-receipt", None),
        (None, ("test.unrequested.app",)),
    ],
)
def test_commit_receipt_must_match_predeclared_identity_and_requested_apps(
    tmp_path: Path,
    receipt_id_override: str | None,
    introduced_app_ids_override: tuple[str, ...] | None,
) -> None:
    """Catches a provider returning a different receipt or undeclared introduced app."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)
    context["provisioner"].receipt_id_override = receipt_id_override
    context["provisioner"].introduced_app_ids_override = introduced_app_ids_override

    with pytest.raises(PluginInstallError, match="^plugin_dependency_commit_invalid$"):
        _install(context)

    assert context["provisioner"].rolled_back == [_RECEIPT_ID]
    assert context["real_registry"].snapshot().records == ()


class SharedDependencyProvisioner(FakeProvisioner):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.installed_app_ids = {"test.workspace.app"}
        self.introduced_app_ids_override = ("test.optional.app",)

    def commit(self, prepared: PreparedDependencySet) -> DependencyCommitReceipt:
        failure = self.after_commit_failure
        self.after_commit_failure = None
        receipt = super().commit(prepared)
        self.after_commit_failure = failure
        self.installed_app_ids.update(receipt.introduced_app_ids)
        if failure is not None:
            raise failure
        return receipt

    def rollback(self, receipt_id: str) -> None:
        receipt = self.receipts.get(receipt_id)
        super().rollback(receipt_id)
        if receipt is not None:
            self.installed_app_ids.difference_update(receipt.introduced_app_ids)


def test_ambiguous_dependency_commit_rollback_preserves_preexisting_shared_app(
    tmp_path: Path,
) -> None:
    """Catches abort-by-transaction leaking introduced apps or removing shared ones."""
    context = _context(tmp_path, include_optional=True)
    _use_recoverable_transaction(context)
    provisioner = SharedDependencyProvisioner(context["events"])
    provisioner.journal_path = context["installer"].journal_path
    marker = RuntimeError("ambiguous commit")
    provisioner.after_commit_failure = marker
    context["installer"].provisioner = provisioner
    context["provisioner"] = provisioner

    with pytest.raises(RuntimeError, match="^ambiguous commit$"):
        _install(context)

    assert provisioner.installed_app_ids == {"test.workspace.app"}
    assert provisioner.rolled_back == [_RECEIPT_ID]


def test_public_records_use_exact_transaction_semver_protocol_and_transport_contracts(
    tmp_path: Path,
) -> None:
    """Catches public constructors accepting identities the signed contracts reject."""
    context = _context(tmp_path)
    result = _install(context)
    resolution = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    review = context["installer"].review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution,
        expected_registry_revision=context["real_registry"].snapshot().revision,
    )
    dependency = review.dependencies[0]

    with pytest.raises(ValueError, match="^prepared dependency set is invalid$"):
        PreparedDependencySet(
            transaction_id="transaction-0001",
            prepared_id="prepared-safe",
            receipt_id="receipt-safe",
            app_snapshots=(_snapshot(),),
        )
    with pytest.raises(ValueError, match="^plugin install review is invalid$"):
        replace(review, plugin_version="1.0.0-01")
    with pytest.raises(ValueError, match="^plugin dependency review is invalid$"):
        replace(dependency, protocol_version="2026-99-99")
    with pytest.raises(ValueError, match="^plugin dependency review is invalid$"):
        replace(dependency, transports=("udp",))
    with pytest.raises(ValueError, match="^plugin dependency review is invalid$"):
        replace(dependency, deployment="remote", transports=("stdio",))
    with pytest.raises(ValueError, match="^plugin dependency review is invalid$"):
        replace(
            dependency,
            installed=False,
            trusted=True,
            capability_revision=None,
        )
    with pytest.raises(ValueError, match="^plugin install result is invalid$"):
        replace(result, transaction_id="transaction-0001")


def test_enabled_idempotent_publication_constructs_result_before_journal_clear(
    tmp_path: Path,
) -> None:
    """Catches an exact enabled reinstall clearing recovery evidence before result validation."""
    context = _context(tmp_path)
    _use_recoverable_transaction(context)
    first = _install(context)
    enabled = context["real_registry"].set_enabled(
        "test.workspace",
        True,
        expected_revision=first.revision,
    )
    second_events: list[str] = []
    second_provisioner = FakeProvisioner(second_events)
    second_confirmer = FakeConfirmation(second_events)
    second_installer = PluginInstaller(
        registry=context["registry"],
        provisioner=second_provisioner,
        confirmation_verifier=second_confirmer,
        transaction_id_factory=lambda: "1" * 32,
    )
    second_provisioner.journal_path = second_installer.journal_path
    resolution = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    review = second_installer.review_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        resolution,
        expected_registry_revision=enabled.revision,
    )

    result = second_installer.install_from_catalog(
        context["catalog"],
        context["entry"],
        context["verified"],
        review.sha256,
    )

    assert result.record.enabled is True
    assert result.revision != enabled.revision
    assert second_installer.journal_path.exists() is False


def test_enabled_published_recovery_result_is_a_coherent_terminal_pair(
    tmp_path: Path,
) -> None:
    """Catches recovery records rejecting a valid enabled idempotent installation."""
    context = _context(tmp_path)
    first = _install(context)
    enabled = context["real_registry"].set_enabled(
        "test.workspace",
        True,
        expected_revision=first.revision,
    )

    recovered = InstallRecoveryResult(
        transaction_id="2" * 32,
        plugin_id="test.workspace",
        recovered_state="published",
        completed=True,
        record=enabled.record,
        revision=enabled.revision,
    )

    assert recovered.record is not None
    assert recovered.record.enabled is True
    with pytest.raises(ValueError, match="^install recovery result is invalid$"):
        replace(recovered, record=replace(enabled.record, status="blocked"))


def test_installer_rejects_object_forged_catalog_before_provisioning(tmp_path: Path) -> None:
    """Catches type-only admission of catalog state never issued by the verifier."""
    context = _context(tmp_path)
    authentic = context["catalog"]
    forged = object.__new__(VerifiedMarketplaceCatalog)
    for field_name in (
        "catalog_id",
        "publisher_id",
        "sequence",
        "entries",
        "signing_key_id",
        "catalog_sha256",
        "revision",
    ):
        object.__setattr__(forged, field_name, getattr(authentic, field_name))
    resolution = resolve_dependencies(context["verified"].manifest, (_snapshot(),))
    authentic_review = context["installer"].review_from_catalog(
        authentic,
        context["entry"],
        context["verified"],
        resolution,
        expected_registry_revision=context["real_registry"].snapshot().revision,
    )

    with pytest.raises(PluginInstallError, match="^plugin_catalog_package_mismatch$"):
        context["installer"].install_from_catalog(
            forged,
            context["entry"],
            context["verified"],
            authentic_review.sha256,
        )

    assert context["events"] == []
    assert context["installer"].journal_path.exists() is False


def test_installer_uses_canonical_journal_basename_for_injected_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a runtime-only journal filename diverging from core.paths authority."""
    from plugin_platform import installer as installer_module

    context = _context(tmp_path)
    expected_name = core_paths.PLUGIN_INSTALL_JOURNAL_PATH.name
    observed_locks: list[str] = []
    observed_temporary_names: list[str] = []
    real_lock = installer_module.PinnedDirectory.lock_file
    real_write = installer_module.PinnedDirectory.write_bytes

    @contextmanager
    def observe_lock(
        directory: PinnedDirectory,
        relative_path: str | Path,
        *,
        timeout: float = 10.0,
    ) -> Any:
        observed_locks.append(str(relative_path))
        with real_lock(directory, relative_path, timeout=timeout):
            yield

    def observe_write(
        directory: PinnedDirectory,
        relative_path: str | Path,
        payload: bytes,
        *,
        exclusive: bool = True,
    ) -> None:
        if str(relative_path).endswith(".part"):
            observed_temporary_names.append(str(relative_path))
        real_write(directory, relative_path, payload, exclusive=exclusive)

    monkeypatch.setattr(installer_module.PinnedDirectory, "lock_file", observe_lock)
    monkeypatch.setattr(installer_module.PinnedDirectory, "write_bytes", observe_write)

    _install(context)

    assert context["installer"].journal_path == context["real_registry"].state_root / expected_name
    assert f"{expected_name}.lock" in observed_locks
    assert not any(name.startswith("plugin-install") for name in observed_locks)
    assert any(name.startswith(f".{expected_name}.") for name in observed_temporary_names)
    assert not any(name.startswith(".plugin-install.") for name in observed_temporary_names)


def _normalized_candidate(tmp_path: Path) -> Any:
    """Return one normalized registry candidate built by the real importer."""
    from tests.plugin_platform.test_marketplace_importer import (
        importer as _marketplace_importer,
        registry_weather_server,
    )

    return _marketplace_importer().normalize(registry_weather_server(), "1.2.0")


def _weather_envelope(**overrides: object) -> Any:
    from tests.plugin_platform.test_local_trust import _weather_envelope as _real_envelope

    return _real_envelope(**overrides)


class _SelfSatisfyingProvisioner:
    """A provisioner whose snapshots always echo back each requirement's own
    declared identity and capabilities -- exactly enough for ``resolve_dependencies``
    to report a fresh, never-before-installed remote app as ready."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def prepare(
        self, transaction_id: str, requirements: tuple[Any, ...]
    ) -> PreparedDependencySet:
        from mcp_platform.capabilities import project_declared_capabilities

        self.events.append("prepare")
        snapshots = tuple(project_declared_capabilities(item).snapshot for item in requirements)
        return PreparedDependencySet(
            transaction_id=transaction_id,
            prepared_id=f"prepared-{transaction_id}",
            receipt_id=f"receipt-{transaction_id}",
            app_snapshots=snapshots,
        )

    def abort(self, transaction_id: str) -> None:
        self.events.append("abort")

    def commit(self, prepared: PreparedDependencySet) -> DependencyCommitReceipt:
        self.events.append("commit")
        return DependencyCommitReceipt(
            receipt_id=prepared.receipt_id,
            prepared_id=prepared.prepared_id,
            introduced_app_ids=tuple(item.app_id for item in prepared.app_snapshots),
        )

    def rollback(self, receipt_id: str) -> None:
        self.events.append("rollback")


def _dispatching_verifier(
    state_root: Path, ring: TrustedPublisherKeyRing, trust_root: Any
) -> Callable[[Path], VerifiedPlugin]:
    """A minimal stand-in for the combined verifier ``PluginLifecycleService``
    configures: Ed25519 for an ordinary signed tree, the device-local seal
    (read from the durable seal store, matching a registry-published tree
    that never carries ``local_seal.json`` itself) for an unsealed one."""

    from plugin_platform.installer import read_local_seal_document
    from plugin_platform.marketplace_importer import UNSEALED_SIGNING_KEY_ID
    from plugin_platform.package_verifier import (
        PackageVerificationError,
        verify_locally_sealed_plugin_package,
    )

    def verify(path: Path) -> VerifiedPlugin:
        root = Path(path)
        sealed = False
        manifest_bytes = b""
        try:
            manifest_bytes = (root / "plugin.json").read_bytes()
            manifest_document = json.loads(manifest_bytes)
            sealed = manifest_document.get("signing_key_id") == UNSEALED_SIGNING_KEY_ID
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            sealed = False
        if not sealed:
            return verify_plugin_package(root, ring, "3.0.0")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        seal_document = read_local_seal_document(state_root, manifest_sha256)
        if seal_document is None:
            raise PackageVerificationError("local_seal_unavailable")
        return verify_locally_sealed_plugin_package(
            root, trust_root, external_seal_document=seal_document
        )

    return verify


def _sealed_context(tmp_path: Path) -> dict[str, Any]:
    """A minimal ``accept_normalized`` harness: a real registry configured with
    a combined verifier, a self-satisfying provisioner, and a real vault-keyed
    ``LocalTrustRoot`` -- everything ``accept_normalized`` itself needs, none
    of the Ed25519 catalog machinery ``_context`` builds for the other path."""

    from plugin_platform.local_trust import LocalTrustRoot

    trust_root = LocalTrustRoot.from_vault_key(b"k" * 32)
    _private_key, ring = ephemeral_publisher_ring()
    events: list[str] = []
    plugins_root = tmp_path / "plugins"
    real_registry = PluginRegistry(
        state_root=plugins_root,
        verifier=_dispatching_verifier(plugins_root, ring, trust_root),
    )
    registry = RecordingRegistry(real_registry, events)
    provisioner = _SelfSatisfyingProvisioner()
    confirmer = FakeConfirmation(events, "valid")
    installer = PluginInstaller(
        registry=registry,
        provisioner=provisioner,
        confirmation_verifier=confirmer,
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )
    return {
        "trust_root": trust_root,
        "events": events,
        "registry": registry,
        "real_registry": real_registry,
        "provisioner": provisioner,
        "confirmer": confirmer,
        "installer": installer,
        "review_sha256": "b" * 64,
    }


def test_installer_accepts_only_a_verified_normalized_candidate(tmp_path: Path) -> None:
    """Catches an install source admitted before its evidence and digests verified."""
    from plugin_platform.local_trust import LocalTrustRoot
    from plugin_platform.marketplace_importer import MarketplaceImporter

    context = _sealed_context(tmp_path)
    candidate = _normalized_candidate(tmp_path)
    verified = MarketplaceImporter().verify(candidate)

    result = context["installer"].accept_normalized(
        verified,
        _weather_envelope(),
        context["trust_root"],
        context["review_sha256"],
        expected_review_sha256=context["review_sha256"],
    )

    assert result.record.plugin_id == "io.github.example.weather"
    assert result.record.status == "installed"
    assert type(context["trust_root"]) is LocalTrustRoot


@pytest.mark.parametrize("kind", ["unverified", "lookalike", "manifest", "none"])
def test_installer_refuses_a_normalized_candidate_that_is_not_the_marker(
    tmp_path: Path, kind: str
) -> None:
    """Catches an acceptance path satisfied by duck typing or a raw registry record."""
    context = _sealed_context(tmp_path)
    candidate = _normalized_candidate(tmp_path)

    class Lookalike:
        candidate = None
        manifest = candidate
        trust_tier = None

    supplied: object = {
        "unverified": candidate,
        "lookalike": Lookalike(),
        "manifest": candidate.manifest,
        "none": None,
    }[kind]

    with pytest.raises(PluginInstallError) as exc_info:
        context["installer"].accept_normalized(
            supplied,
            _weather_envelope(),
            context["trust_root"],
            "irrelevant",
            expected_review_sha256="a" * 64,
        )

    assert str(exc_info.value) == "plugin_install_candidate_unverified"


def test_installer_keeps_no_raw_registry_overload(tmp_path: Path) -> None:
    """Catches a second door that installs a registry record without normalization."""
    context = _context(tmp_path)
    entry_points = tuple(
        name
        for name in dir(context["installer"])
        if not name.startswith("_") and "registry_record" in name
    )

    assert entry_points == ()
    assert hasattr(context["installer"], "review_from_catalog")
    assert hasattr(context["installer"], "install_from_catalog")
