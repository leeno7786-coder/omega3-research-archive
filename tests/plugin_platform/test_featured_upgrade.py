"""Upgrade regressions for the immutable signed marketplace release identities."""

from __future__ import annotations

import base64
import json
import zlib
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest
from packaging.version import Version

from mcp_platform.canonical import canonical_bytes
import mcp_platform.plugin_connections as plugin_connections_module
from plugin_platform.catalog import (
    CatalogVerificationError,
    MarketplaceCatalogEntry,
    VerifiedMarketplaceCatalog,
    verify_plugin_catalog,
)
from plugin_platform.embedded_catalog import EmbeddedCatalogPackageResolver
import plugin_platform.package_verifier as package_verifier_module
from plugin_platform.package_verifier import VerifiedPlugin, verify_plugin_package
from plugin_platform.registry import PluginAppBinding, PluginRegistry
from plugin_platform.trust import (
    TrustedPublisherKeyRing,
    load_embedded_key_ring,
    verify_signed_bytes,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CURRENT_MARKETPLACE_ROOT = _REPOSITORY_ROOT / "plugin_platform" / "marketplace"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_SEQUENCE_6_SNAPSHOT = _FIXTURES / "featured_sequence_6_0a2f82c.snapshot.b64"
_SEQUENCE_6_COMMIT = "0a2f82c2792bda6b6cb3344733f376fd4df091db"
_SEQUENCE_6_PACKED_SHA256 = (
    "f24fb8ef66463bc8d242465af457f737e089d965f7d61d7edfaba02955285ec6"
)
_SEQUENCE_7_SNAPSHOT = _FIXTURES / "featured_sequence_7_3561ac1.snapshot.b64"
_SEQUENCE_7_COMMIT = "3561ac144ad5cae8f2bb8d45fb0ef320462d6cb5"
_SEQUENCE_7_PACKED_SHA256 = (
    "e6fca85793f2b3a282a18d4869117bdda4c35d02c90475ec94d9b6860465432c"
)
_EXPECTED_FILES = frozenset(
    {
        "catalog.json",
        "github.official/plugin.json",
        "github.official/skills/github/SKILL.md",
        "github.official/tools/authority.json",
        "google.workspace.calendar/plugin.json",
        "google.workspace.calendar/skills/calendar/SKILL.md",
        "google.workspace.calendar/tools/authority.json",
        "google.workspace.drive/plugin.json",
        "google.workspace.drive/skills/google-drive/SKILL.md",
        "google.workspace.drive/tools/authority.json",
        "google.workspace.gmail/plugin.json",
        "google.workspace.gmail/skills/gmail/SKILL.md",
        "google.workspace.gmail/tools/authority.json",
    }
)
_SEQUENCE_6_TO_7 = {
    "github.official": ("1.0.2", "1.0.3"),
    "google.workspace.calendar": ("1.0.0", "1.0.1"),
    "google.workspace.drive": ("1.0.0", "1.0.1"),
    "google.workspace.gmail": ("1.0.0", "1.0.1"),
}
#: The versions sequence 7 shipped. Only the PRIOR side is pinned: what the
#: upgrade path has to hold is that every one of these was superseded, not that
#: it landed on a particular version. Pinning the current side made this fail on
#: every release after the one it was written for -- GitHub is on 1.0.5 now, for
#: an issuer correction that has nothing to do with this upgrade.
_SEQUENCE_7_VERSIONS = {
    "github.official": "1.0.3",
    "google.workspace.calendar": "1.0.1",
    "google.workspace.drive": "1.0.1",
    "google.workspace.gmail": "1.0.1",
}
#: Not a fixed list. Every plugin added since sequence 7 belongs here, and
#: adding one is the routine act the whole pipeline exists to make cheap -- a
#: pinned tuple meant every new provider broke three tests that have nothing to
#: say about it. What the upgrade path must hold is that sequence 7's packages
#: were all superseded and none went missing; anything beyond them is an
#: addition, which is allowed.
def _added_after_sequence_7(current_ids) -> set[str]:
    return set(current_ids) - set(_SEQUENCE_7_VERSIONS)
_PRIOR_AUTHORITY_FIELDS = frozenset(
    {"tools", "resources", "resource_templates", "prompts", "runtime_compatibility"}
)


def _materialize_release(
    snapshot_path: Path,
    destination: Path,
    *,
    commit: str,
    packed_sha256: str,
) -> Path:
    encoded_snapshot = "".join(snapshot_path.read_text(encoding="ascii").splitlines())
    packed = base64.b64decode(encoded_snapshot, validate=True)
    assert sha256(packed).hexdigest() == packed_sha256
    snapshot = json.loads(zlib.decompress(packed))
    assert snapshot["commit"] == commit
    assert frozenset(snapshot["files"]) == _EXPECTED_FILES

    destination.mkdir()
    for relative_path, encoded in snapshot["files"].items():
        target = destination.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(encoded, validate=True))
    return destination


def _verified_legacy_catalog(
    path: Path,
    key_ring: TrustedPublisherKeyRing,
) -> dict[str, Any]:
    """Verify a superseded schema-1 catalog by signature alone and return it."""

    payload = path.read_bytes()
    document = json.loads(payload.decode("utf-8"))
    assert canonical_bytes(document) == payload
    assert document["schema_version"] == 1
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    verify_signed_bytes(
        canonical_bytes(unsigned),
        document["signature"],
        key_ring,
        key_id=document["signing_key_id"],
        publisher_id=document["publisher_id"],
        subject_id=document["catalog_id"],
    )
    with pytest.raises(CatalogVerificationError, match="^catalog_invalid$"):
        verify_plugin_catalog(path, key_ring)
    return document


def _legacy_entries(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["plugin_id"]: entry for entry in document["entries"]}


def _entries(
    catalog: VerifiedMarketplaceCatalog,
) -> dict[str, MarketplaceCatalogEntry]:
    return {entry.plugin_id: entry for entry in catalog.entries}


def _verifier(
    key_ring: TrustedPublisherKeyRing,
) -> Callable[[Path], VerifiedPlugin]:
    return lambda path: verify_plugin_package(path, key_ring, "3.0.0")


def _validate_prior_capability_authority(
    document: object,
    _manifest: object,
) -> tuple[dict[str, dict[str, object]], bool]:
    if type(document) is not dict or not set(document).issubset(
        _PRIOR_AUTHORITY_FIELDS
    ):
        raise ValueError("capability_authority_invalid")
    if "runtime_compatibility" in document:
        package_verifier_module._validate_runtime_compatibility(
            document["runtime_compatibility"]
        )
    return {}, False


def _prior_verifier(
    key_ring: TrustedPublisherKeyRing,
) -> Callable[[Path], VerifiedPlugin]:
    def verify(path: Path) -> VerifiedPlugin:
        with patch.object(
            package_verifier_module,
            "_validate_capability_authority",
            _validate_prior_capability_authority,
        ):
            return verify_plugin_package(path, key_ring, "3.0.0")

    return verify


def _binding(verified: VerifiedPlugin, receipt_id: str) -> PluginAppBinding:
    required = tuple(sorted(app.app_id for app in verified.manifest.required_apps))
    optional = tuple(sorted(app.app_id for app in verified.manifest.optional_apps))
    revision = sha256(
        canonical_bytes(
            {
                "required_app_ids": list(required),
                "optional_app_ids": list(optional),
                "dependency_receipt_id": receipt_id,
            }
        )
    ).hexdigest()
    return PluginAppBinding(required, optional, receipt_id, revision)


def _assert_legacy_successor(
    previous: dict[str, Any],
    candidate: VerifiedMarketplaceCatalog,
) -> None:
    """Apply the successor invariants across the schema-1 to schema-2 boundary."""

    assert previous["catalog_id"] == candidate.catalog_id
    assert previous["publisher_id"] == candidate.publisher_id
    assert candidate.sequence > previous["sequence"]


def test_current_marketplace_release_upgrades_every_exact_sequence_6_package(
    tmp_path: Path,
) -> None:
    """Catches losing the sequence-6 package, authority, or registry upgrade path."""
    prior_root = _materialize_release(
        _SEQUENCE_6_SNAPSHOT,
        tmp_path / "prior-featured",
        commit=_SEQUENCE_6_COMMIT,
        packed_sha256=_SEQUENCE_6_PACKED_SHA256,
    )
    key_ring = load_embedded_key_ring()
    prior_document = _verified_legacy_catalog(prior_root / "catalog.json", key_ring)

    assert prior_document["sequence"] == 6
    prior_entries = _legacy_entries(prior_document)
    assert set(prior_entries) == set(_SEQUENCE_6_TO_7)
    historical_verifier = _prior_verifier(key_ring)
    current_verifier = _verifier(key_ring)
    for plugin_id, (prior_version, _current_version) in _SEQUENCE_6_TO_7.items():
        prior_entry = prior_entries[plugin_id]
        assert prior_entry["plugin_version"] == prior_version
        prior_package = historical_verifier(prior_root / plugin_id)
        assert (
            prior_package.manifest.plugin_id,
            prior_package.manifest.publisher_id,
            prior_package.manifest.plugin_version,
            prior_package.manifest_sha256,
            prior_package.package_sha256,
        ) == (
            prior_entry["plugin_id"],
            prior_entry["publisher_id"],
            prior_entry["plugin_version"],
            prior_entry["manifest_sha256"],
            prior_entry["package_sha256"],
        )
        historical_registry = PluginRegistry(
            state_root=tmp_path / "registry" / plugin_id,
            verifier=historical_verifier,
        )
        prior_binding = _binding(prior_package, "receipt-prior")
        prior_stage = historical_registry.stage(prior_package)
        installed = historical_registry.publish(
            prior_stage,
            expected_revision=historical_registry.snapshot().revision,
            app_binding=prior_binding,
        )
        assert installed.record.selected_version == prior_version

    for plugin_id in _SEQUENCE_6_TO_7:
        registry = PluginRegistry(
            state_root=tmp_path / "registry" / plugin_id,
            verifier=current_verifier,
        )
        reverified_prior = registry.reverify_installed(plugin_id)
        required_apps = reverified_prior.manifest.required_apps
        assert len(required_apps) == 1
        # Spec principle 8 forbids a compatibility fork: reverifying an
        # already-installed sequence-6 package identifies the retired flat
        # authority shape but never admits it as usable capability data.
        assert reverified_prior.legacy_authority is True
        assert (
            plugin_connections_module._verified_plugin_authority(
                reverified_prior,
                required_apps[0].app_id,
            )
            is None
        )
        assert (
            plugin_connections_module._verified_plugin_authority(
                reverified_prior,
                f"{required_apps[0].app_id}.other",
            )
            is None
        )


def test_current_marketplace_release_upgrades_every_exact_sequence_7_package(
    tmp_path: Path,
) -> None:
    """Catches a marketplace v2 relocation that breaks the sequence-7 upgrade edge."""
    prior_root = _materialize_release(
        _SEQUENCE_7_SNAPSHOT,
        tmp_path / "prior-marketplace",
        commit=_SEQUENCE_7_COMMIT,
        packed_sha256=_SEQUENCE_7_PACKED_SHA256,
    )
    key_ring = load_embedded_key_ring()
    prior_document = _verified_legacy_catalog(prior_root / "catalog.json", key_ring)
    current_catalog = verify_plugin_catalog(
        _CURRENT_MARKETPLACE_ROOT / "catalog.json",
        key_ring,
    )

    assert prior_document["sequence"] == 7
    # A later sequence, not a specific one. Pinning the number made this fail on
    # every release for a reason that has nothing to do with the upgrade being
    # tested, and a test that fails on every release stops being read.
    assert current_catalog.sequence > prior_document["sequence"]
    _assert_legacy_successor(prior_document, current_catalog)

    prior_entries = _legacy_entries(prior_document)
    current_entries = _entries(current_catalog)
    assert set(prior_entries) == set(_SEQUENCE_7_VERSIONS)
    # Every sequence-7 package is still listed; later additions are welcome.
    assert set(_SEQUENCE_7_VERSIONS) <= set(current_entries)

    current_verifier = _verifier(key_ring)
    current_resolver = EmbeddedCatalogPackageResolver(
        root=_CURRENT_MARKETPLACE_ROOT,
        key_ring=key_ring,
    )
    for plugin_id, prior_version in _SEQUENCE_7_VERSIONS.items():
        prior_entry = prior_entries[plugin_id]
        assert prior_entry["plugin_version"] == prior_version
        assert prior_entry["package_uri"] == (
            f"https://plugins.omega2ai.com/featured/{plugin_id}/{prior_version}"
        )
        prior_package = current_verifier(prior_root / plugin_id)
        assert prior_package.manifest_sha256 == prior_entry["manifest_sha256"]
        assert prior_package.package_sha256 == prior_entry["package_sha256"]

        current_entry = current_entries[plugin_id]
        current_version = current_entry.plugin_version
        assert Version(current_version) > Version(prior_version), plugin_id
        assert current_entry.package_uri == (
            "https://plugins.omega2ai.com/marketplace/packages/"
            f"{plugin_id}/{current_version}"
        )
        assert current_entry.package_sha256 != prior_entry["package_sha256"]
        assert (prior_version, prior_entry["package_sha256"]) in tuple(
            (item.plugin_version, item.package_sha256)
            for item in current_entry.release_history
        )

        registry = PluginRegistry(
            state_root=tmp_path / "registry" / plugin_id,
            verifier=current_verifier,
        )
        prior_binding = _binding(prior_package, "receipt-prior")
        prior_stage = registry.stage(prior_package)
        installed = registry.publish(
            prior_stage,
            expected_revision=registry.snapshot().revision,
            app_binding=prior_binding,
        )
        assert installed.record.selected_version == prior_version

        current_verified = verify_plugin_package(
            current_resolver.resolve(current_entry),
            key_ring,
            "3.0.0",
        )
        current_binding = _binding(current_verified, "receipt-current")
        current_stage = registry.stage(current_verified)
        candidate = registry.publish(
            current_stage,
            expected_revision=registry.snapshot().revision,
            app_binding=current_binding,
            preserve_existing_binding=True,
        )
        assert candidate.record.selected_version == prior_version

        promoted = registry.promote_candidate(
            plugin_id,
            current_version,
            expected_revision=candidate.revision,
            app_binding=current_binding,
        )
        assert promoted.record.selected_version == current_version
        assert registry.reverify_installed(plugin_id).package_sha256 == (
            current_entry.package_sha256
        )


def _verified_current_catalog():
    """The shipped catalog, verified -- the source of truth for what ships."""
    return verify_plugin_catalog(_CURRENT_MARKETPLACE_ROOT / "catalog.json", load_embedded_key_ring())


def test_superseded_marketplace_packages_live_only_under_packages(tmp_path: Path) -> None:
    """Catches a relocation that leaves the retired featured package tree behind."""
    assert not (_REPOSITORY_ROOT / "plugin_platform" / "featured").exists()
    # Against the catalog itself, not a list written here. A plugin added after
    # sequence 7 has no upgrade edge to assert, but it must live under packages/
    # like every other shipped one -- and adding one must not edit this test.
    catalog = _verified_current_catalog()
    assert sorted(
        path.name for path in (_CURRENT_MARKETPLACE_ROOT / "packages").iterdir()
    ) == sorted(entry.plugin_id for entry in catalog.entries)
    assert set(_SEQUENCE_7_VERSIONS) <= {entry.plugin_id for entry in catalog.entries}
    assert sorted(
        path.name
        for path in _CURRENT_MARKETPLACE_ROOT.iterdir()
        if path.name != ".gitattributes"
    ) == ["catalog.json", "packages"]
