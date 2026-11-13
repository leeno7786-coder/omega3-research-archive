"""Dynamic package resolution from verified marketplace catalog identities."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from plugin_platform.catalog import (
    CatalogVerificationError,
    MarketplaceCatalogEntry,
    VerifiedMarketplaceCatalog,
    validate_verified_catalog_entry,
    verify_plugin_catalog,
)
from plugin_platform.package_verifier import (
    PackageVerificationError,
    verify_plugin_package,
)
from plugin_platform.trust import TrustedPublisherKeyRing, load_embedded_key_ring


class EmbeddedCatalogResolutionError(ValueError):
    """A stable, content-free embedded package resolution rejection."""


def _is_real_directory(path: Path) -> bool:
    """Report whether the path is a plain directory, never a reparse point."""

    try:
        observed = path.lstat()
    except (OSError, ValueError):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(observed, "st_file_attributes", 0)
    return bool(stat.S_ISDIR(observed.st_mode)) and not attributes & reparse_flag


class EmbeddedCatalogPackageResolver:
    """Resolve only the canonical package directory named by a verified entry."""

    def __init__(
        self,
        *,
        root: Path,
        key_ring: TrustedPublisherKeyRing | None = None,
        omega_version: str = "3.0.0",
    ) -> None:
        try:
            self._root = Path(os.path.abspath(os.fspath(Path(root))))
        except (OSError, TypeError, ValueError):
            raise EmbeddedCatalogResolutionError("embedded_package_root_invalid") from None
        try:
            observed = self._root.lstat()
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            attributes = getattr(observed, "st_file_attributes", 0)
            if not stat.S_ISDIR(observed.st_mode) or attributes & reparse_flag:
                raise ValueError
        except (OSError, ValueError):
            raise EmbeddedCatalogResolutionError("embedded_package_root_invalid") from None
        self._key_ring = load_embedded_key_ring() if key_ring is None else key_ring
        self._omega_version = omega_version
        self._catalog_path = self._root / "catalog.json"
        self._packages_root = self._root / "packages"

    def resolve(
        self,
        entry: MarketplaceCatalogEntry,
        *,
        catalog: VerifiedMarketplaceCatalog | None = None,
    ) -> Path:
        """Verify and return ``root/packages/<plugin_id>`` without lookup tables."""

        if catalog is None:
            try:
                catalog = verify_plugin_catalog(self._catalog_path, self._key_ring)
            except CatalogVerificationError:
                raise EmbeddedCatalogResolutionError(
                    "embedded_catalog_context_invalid"
                ) from None
        try:
            entry = validate_verified_catalog_entry(entry, catalog=catalog)
        except (CatalogVerificationError, TypeError, ValueError) as exc:
            reasons = {
                "catalog_entry_unverified": "embedded_catalog_entry_unverified",
                "catalog_entry_context_mismatch": "embedded_catalog_context_mismatch",
            }
            reason = reasons.get(str(exc), "embedded_catalog_entry_invalid")
            raise EmbeddedCatalogResolutionError(reason) from None
        candidate = self._packages_root / entry.plugin_id
        if candidate.parent != self._packages_root:
            raise EmbeddedCatalogResolutionError("embedded_catalog_entry_invalid")
        if not _is_real_directory(self._packages_root):
            raise EmbeddedCatalogResolutionError("embedded_package_root_invalid")
        try:
            observed = candidate.lstat()
        except FileNotFoundError:
            raise EmbeddedCatalogResolutionError("embedded_package_missing") from None
        except (OSError, ValueError):
            raise EmbeddedCatalogResolutionError("embedded_package_path_invalid") from None
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(observed, "st_file_attributes", 0)
        if not stat.S_ISDIR(observed.st_mode) or attributes & reparse_flag:
            raise EmbeddedCatalogResolutionError("embedded_package_path_invalid")
        try:
            verified = verify_plugin_package(candidate, self._key_ring, self._omega_version)
        except (OSError, TypeError, ValueError, PackageVerificationError):
            raise EmbeddedCatalogResolutionError(
                "embedded_package_verification_failed"
            ) from None
        if (
            verified.manifest.plugin_id != entry.plugin_id
            or verified.manifest.publisher_id != entry.publisher_id
            or verified.manifest.plugin_version != entry.plugin_version
            or verified.manifest_sha256 != entry.manifest_sha256
            or verified.package_sha256 != entry.package_sha256
        ):
            raise EmbeddedCatalogResolutionError("embedded_package_catalog_mismatch")
        return candidate


__all__ = ["EmbeddedCatalogPackageResolver", "EmbeddedCatalogResolutionError"]
