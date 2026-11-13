"""Verified embedded-catalog package resolution tests."""

from __future__ import annotations

import base64
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
from plugin_platform.catalog import MarketplaceCatalogEntry, verify_plugin_catalog
from plugin_platform.embedded_catalog import (
    EmbeddedCatalogPackageResolver,
    EmbeddedCatalogResolutionError,
)
from plugin_platform.package_verifier import verify_plugin_package
from plugin_platform.trust import load_embedded_key_ring
from tests.plugin_platform.support import (
    build_signed_plugin,
    ephemeral_publisher_ring,
    valid_catalog_document,
)

MARKETPLACE_ROOT = Path("plugin_platform/marketplace")
CATALOG_PATH = MARKETPLACE_ROOT / "catalog.json"


def _signed_catalog_for_package(
    path: Path,
    package: Path,
    private_key: Ed25519PrivateKey,
    ring,
    *,
    package_sha256: str | None = None,
):
    verified = verify_plugin_package(package, ring, "3.0.0")
    document = valid_catalog_document(private_key)
    entry = document["entries"][0]
    entry.update(
        {
            "plugin_id": verified.manifest.plugin_id,
            "publisher_id": verified.manifest.publisher_id,
            "plugin_version": verified.manifest.plugin_version,
            "manifest_sha256": verified.manifest_sha256,
            "package_sha256": package_sha256 or verified.package_sha256,
        }
    )
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(
        private_key.sign(canonical_bytes(unsigned))
    ).decode("ascii")
    path.write_bytes(canonical_bytes(document))
    return verify_plugin_catalog(path, ring)


def _fixture(tmp_path: Path):
    private_key, ring = ephemeral_publisher_ring()
    root = tmp_path / "marketplace"
    root.mkdir()
    (root / "packages").mkdir()
    package = build_signed_plugin(root, private_key)
    canonical_package = root / "packages" / "test.workspace"
    package.rename(canonical_package)
    catalog = _signed_catalog_for_package(
        root / "catalog.json",
        canonical_package,
        private_key,
        ring,
    )
    return private_key, ring, root, canonical_package, catalog.entries[0]


def _resign_manifest(package: Path, private_key: Ed25519PrivateKey) -> None:
    path = package / "plugin.json"
    document = json.loads(path.read_bytes())
    document["title"] = "Changed Workspace"
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(
        private_key.sign(canonical_bytes(unsigned))
    ).decode("ascii")
    path.write_bytes(canonical_bytes(document))


def test_embedded_resolver_uses_catalog_identity_not_a_python_table() -> None:
    catalog = verify_plugin_catalog(CATALOG_PATH, load_embedded_key_ring())
    resolver = EmbeddedCatalogPackageResolver(root=MARKETPLACE_ROOT)

    assert {resolver.resolve(entry).name for entry in catalog.entries} == {
        entry.plugin_id for entry in catalog.entries
    }


def test_resolver_rejects_entry_issued_by_a_different_catalog_signer(
    tmp_path: Path,
) -> None:
    production_ring = load_embedded_key_ring()
    production_catalog = verify_plugin_catalog(CATALOG_PATH, production_ring)
    production_entry = next(
        entry for entry in production_catalog.entries if entry.plugin_id == "github.official"
    )
    attacker_private_key, attacker_ring = ephemeral_publisher_ring()
    document = valid_catalog_document(attacker_private_key)
    document["entries"] = [production_entry.to_dict()]
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(
        attacker_private_key.sign(canonical_bytes(unsigned))
    ).decode("ascii")
    attacker_catalog_path = tmp_path / "attacker-catalog.json"
    attacker_catalog_path.write_bytes(canonical_bytes(document))
    attacker_catalog = verify_plugin_catalog(attacker_catalog_path, attacker_ring)

    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_catalog_context_mismatch$",
    ):
        EmbeddedCatalogPackageResolver(
            root=MARKETPLACE_ROOT,
            key_ring=production_ring,
        ).resolve(attacker_catalog.entries[0])


def test_resolver_uses_the_exact_newer_catalog_context(tmp_path: Path) -> None:
    """A synced catalog may issue an unchanged package without using seq-38 context."""
    private_key, ring, root, package, _old_entry = _fixture(tmp_path)
    newer_path = root / "newer-catalog.json"
    newer = _signed_catalog_for_package(newer_path, package, private_key, ring)
    document = json.loads(newer_path.read_bytes())
    document["sequence"] = 2
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(
        private_key.sign(canonical_bytes(unsigned))
    ).decode("ascii")
    newer_path.write_bytes(canonical_bytes(document))
    newer = verify_plugin_catalog(newer_path, ring)
    resolver = EmbeddedCatalogPackageResolver(root=root, key_ring=ring)

    assert resolver.resolve(newer.entries[0], catalog=newer) == package
    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_catalog_context_mismatch$",
    ):
        resolver.resolve(newer.entries[0], catalog=verify_plugin_catalog(
            root / "catalog.json", ring
        ))


def test_resolver_rejects_verified_entry_mutated_into_another_catalog_entry() -> None:
    ring = load_embedded_key_ring()
    catalog = verify_plugin_catalog(CATALOG_PATH, ring)
    github = next(entry for entry in catalog.entries if entry.plugin_id == "github.official")
    calendar = next(
        entry for entry in catalog.entries if entry.plugin_id == "google.workspace.calendar"
    )
    for field, value in calendar.to_dict().items():
        object.__setattr__(github, field, value)

    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_catalog_entry_invalid$",
    ):
        EmbeddedCatalogPackageResolver(root=MARKETPLACE_ROOT, key_ring=ring).resolve(github)


def test_resolver_rejects_stateful_entry_shadow_after_validation() -> None:
    ring = load_embedded_key_ring()
    catalog = verify_plugin_catalog(CATALOG_PATH, ring)
    github = next(entry for entry in catalog.entries if entry.plugin_id == "github.official")
    calendar = next(
        entry for entry in catalog.entries if entry.plugin_id == "google.workspace.calendar"
    )
    signed_github = github.to_dict()
    forged_calendar = calendar.to_dict()
    for field, value in forged_calendar.items():
        object.__setattr__(github, field, value)
    responses = iter((signed_github, forged_calendar))
    object.__setattr__(github, "to_dict", lambda: next(responses))

    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_catalog_entry_invalid$",
    ):
        EmbeddedCatalogPackageResolver(root=MARKETPLACE_ROOT, key_ring=ring).resolve(github)


def test_resolver_rejects_missing_package_and_package_digest_drift(tmp_path: Path) -> None:
    private_key, ring, root, package, entry = _fixture(tmp_path)
    resolver = EmbeddedCatalogPackageResolver(root=root, key_ring=ring)
    package.rename(tmp_path / "missing-package")

    with pytest.raises(EmbeddedCatalogResolutionError, match="^embedded_package_missing$"):
        resolver.resolve(entry)

    package = tmp_path / "missing-package"
    package.rename(root / "packages" / "test.workspace")
    _resign_manifest(root / "packages" / "test.workspace", private_key)
    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_package_catalog_mismatch$",
    ):
        resolver.resolve(entry)


def test_resolver_rejects_unverified_or_traversing_catalog_identity(tmp_path: Path) -> None:
    _private_key, ring, root, _package, verified_entry = _fixture(tmp_path)
    resolver = EmbeddedCatalogPackageResolver(root=root, key_ring=ring)
    unverified = MarketplaceCatalogEntry.from_dict(verified_entry.to_dict())

    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_catalog_entry_unverified$",
    ):
        resolver.resolve(unverified)

    object.__setattr__(verified_entry, "plugin_id", "../outside")
    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_catalog_entry_invalid$",
    ):
        resolver.resolve(verified_entry)


def test_resolver_rejects_symlink_or_reparse_package_directory(tmp_path: Path) -> None:
    _private_key, ring, root, package, entry = _fixture(tmp_path)
    outside = tmp_path / "outside-package"
    package.rename(outside)
    try:
        os.symlink(outside, package, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_package_path_invalid$",
    ):
        EmbeddedCatalogPackageResolver(root=root, key_ring=ring).resolve(entry)


def test_resolver_rejects_symlink_or_reparse_catalog_root(tmp_path: Path) -> None:
    _private_key, ring, root, _package, entry = _fixture(tmp_path)
    linked_root = tmp_path / "linked-marketplace"
    try:
        os.symlink(root, linked_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_package_root_invalid$",
    ):
        EmbeddedCatalogPackageResolver(root=linked_root, key_ring=ring).resolve(entry)


def test_resolver_rejects_symlink_or_reparse_packages_directory(tmp_path: Path) -> None:
    """Catches package resolution escaping through a redirected packages level."""
    _private_key, ring, root, _package, entry = _fixture(tmp_path)
    packages = root / "packages"
    outside = tmp_path / "outside-packages"
    packages.rename(outside)
    try:
        os.symlink(outside, packages, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(
        EmbeddedCatalogResolutionError,
        match="^embedded_package_root_invalid$",
    ):
        EmbeddedCatalogPackageResolver(root=root, key_ring=ring).resolve(entry)


def test_no_shipped_catalog_entry_declares_zero_capabilities() -> None:
    """A plugin anyone can install and nobody can use must not ship.

    A package with no declared capability installs, signs and authorizes
    normally and then refuses every connection at `_discovery_anchor` with
    `plugin_capability_invalid`, because there is no capability to anchor the
    discovery on. That is exactly right for a pass-1 scaffold on a maintainer's
    machine -- the refusal is what banks the token for the capture -- and it is
    the one shape a shipped catalog must never contain.

    `paypal.official` and `webflow.official` shipped that way until 2026-09-08.
    The two-pass flow makes producing one routine, so this is the check that the
    scaffold came back out before release.
    """
    catalog = json.loads(
        Path("plugin_platform/marketplace/catalog.json").read_bytes().decode("utf-8")
    )
    empty = []
    for entry in catalog["entries"]:
        manifest = json.loads(
            Path(f"plugin_platform/marketplace/packages/{entry['plugin_id']}/plugin.json")
            .read_bytes()
            .decode("utf-8")
        )
        declared = sum(len(app["capabilities"]) for app in manifest["required_apps"])
        if declared == 0:
            empty.append(entry["plugin_id"])

    assert empty == [], (
        "these ship in the signed catalog and can never connect: " + ", ".join(sorted(empty))
    )


def test_every_shipped_catalog_entry_has_a_package_on_disk() -> None:
    """Retiring a provider touches six documents; this catches five of them.

    A package removed from the tree but left in the catalog, the provenance
    record or the review policy fails at signing as an opaque error naming none
    of them. Reading the catalog and looking for the package is the cheap half
    of that check, and it runs without the signing key.
    """
    catalog = json.loads(
        Path("plugin_platform/marketplace/catalog.json").read_bytes().decode("utf-8")
    )
    provenance = json.loads(
        Path("plugin_platform/marketplace_provenance.json").read_bytes().decode("utf-8")
    )
    policy = json.loads(
        Path("plugin_platform/marketplace_build/review_policy.json").read_bytes().decode("utf-8")
    )

    listed = {entry["plugin_id"] for entry in catalog["entries"]}
    on_disk = {
        path.parent.name
        for path in Path("plugin_platform/marketplace/packages").glob("*/plugin.json")
    }
    build_metadata = {
        path.parent.name
        for path in Path("plugin_platform/marketplace_build/metadata").glob(
            "*/catalog_metadata.json"
        )
    }

    assert listed == on_disk
    assert listed == build_metadata
    assert listed == {record["plugin_id"] for record in provenance["records"]}
    assert listed == {item["plugin_id"] for item in policy["promotions"]}
