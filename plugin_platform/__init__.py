"""Stable public contracts for declarative Omega plugins."""

from .app_projection import AppOnlyPluginProjection, derive_app_only_plugin
from .catalog import (
    MarketplaceCatalogEntry,
    VerifiedMarketplaceCatalog,
    verify_catalog_successor,
    verify_plugin_catalog,
)
from .contracts import AppDeployment, AppRequirement, CapabilityRequirement, PluginManifest
from .dependencies import (
    AppCapabilitySnapshot,
    DependencyResolution,
    NamedCapability,
    resolve_dependencies,
)
from .installer import (
    DependencyProvisioner,
    InstallConfirmationVerifier,
    PluginInstaller,
    PluginInstallResult,
    PluginInstallReview,
)
from .package_verifier import VerifiedPlugin, verify_plugin_package
from .registry import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginRegistry,
    PluginRegistrySnapshot,
    VerifiedInstalledPluginSnapshot,
)
from .trust import TrustedPublisherKeyRing

__all__ = (
    "AppOnlyPluginProjection",
    "derive_app_only_plugin",
    "MarketplaceCatalogEntry",
    "VerifiedMarketplaceCatalog",
    "verify_catalog_successor",
    "verify_plugin_catalog",
    "AppDeployment",
    "AppRequirement",
    "CapabilityRequirement",
    "PluginManifest",
    "AppCapabilitySnapshot",
    "DependencyResolution",
    "NamedCapability",
    "resolve_dependencies",
    "DependencyProvisioner",
    "InstallConfirmationVerifier",
    "PluginInstallResult",
    "PluginInstallReview",
    "PluginInstaller",
    "VerifiedPlugin",
    "verify_plugin_package",
    "InstalledPluginRecord",
    "InstalledPluginVersionRecord",
    "PluginRegistry",
    "PluginRegistrySnapshot",
    "VerifiedInstalledPluginSnapshot",
    "TrustedPublisherKeyRing",
)
