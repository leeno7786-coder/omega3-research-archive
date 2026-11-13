"""Build reproducible, signed-ready marketplace catalog snapshots.

This maintainer command turns captured Official MCP Registry pages plus an
explicit review policy into a deterministic, unsigned marketplace catalog and
package set. It is deliberately split into five commands that never combine
untrusted network input with access to the marketplace signing key:

``fetch-registry``
    Capture bounded canonical registry pages over an injected or loopback
    transport in tests, or the bounded production transport in
    ``mcp_platform.registry_fetch`` otherwise. No key access.
``normalize``
    Turn captured pages into unsigned, provider-neutral candidates via
    ``plugin_platform.marketplace_importer.MarketplaceImporter``, plus a
    review report a maintainer can act on. No network, no key access.
``assemble``
    Deterministically weave package directories (each an unsigned or already
    signed manifest, plus maintainer-curated catalog listing metadata) into
    one unsigned catalog document, honoring an explicit review policy for any
    ``omega_verified`` promotion. No network, no key access.
``sign``
    Delegate to the existing protected signing boundary,
    ``scripts.sign_marketplace_catalog``. No network.
``verify``
    Fully verify a final signed snapshot offline: catalog signature, every
    package signature and digest, and (for ``omega_verified`` entries) the
    signed provider provenance record. No network, no key access.

A package can never nominate its own trust tier. ``assemble`` assigns
``omega_verified`` only to a ``plugin_id`` an explicit review policy document
names together with the provenance digest the bundled provenance record
actually carries for it; every other package is at most
``publisher_verified``. Nothing here invents catalog listing metadata -- a
package directory missing its curated ``catalog_metadata.json`` fails the
whole build rather than being guessed into existence.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import paths as core_paths
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_profiles import OAuthProfileRegistry
from plugin_platform.catalog import (
    CATALOG_SCHEMA_VERSION,
    MARKETPLACE_PACKAGES_ROOT,
    CatalogVerificationError,
    MarketplaceCatalogEntry,
    TrustTier,
    verify_plugin_catalog,
)
from plugin_platform.contracts import PluginManifest, _digest, _https_issuer, _identifier
from plugin_platform.marketplace_importer import (
    UNSEALED_SIGNATURE,
    UNSEALED_SIGNING_KEY_ID,
    MarketplaceImporter,
    NormalizedPluginCandidate,
    PluginImportError,
)
from plugin_platform.marketplace_provenance import (
    BUNDLED_PROVENANCE_PATH,
    ProviderProvenanceVerificationError,
    verify_provider_provenance,
)
from plugin_platform.mcp_registry import (
    McpRegistryClient,
    McpRegistryError,
    McpRegistryRequest,
    McpRegistryServer,
    McpRegistryTransport,
    RegistryStatus,
)
from plugin_platform.package_verifier import PackageVerificationError, verify_plugin_package
from plugin_platform.trust import TrustedPublisherKeyRing, load_embedded_key_ring
import scripts.sign_marketplace_catalog as sign_marketplace_catalog
from scripts.sign_marketplace_catalog import marketplace_package_uri

DEFAULT_OMEGA_VERSION = "3.0.0"
REVIEW_POLICY_SCHEMA_VERSION = 1

#: Committed build inputs for the bundled snapshot: curated catalog listing
#: metadata (one subdirectory per shipped ``plugin_id``) and the review policy
#: naming which of them are ``omega_verified``. Both live outside
#: ``plugin_platform/marketplace/`` deliberately -- that tree's exact file set
#: is pinned by ``package_verifier.verify_plugin_package`` (every file under a
#: package directory must be a declared asset) and by
#: ``tests/plugin_platform/test_featured_upgrade.py`` and
#: ``tests/plugin_platform/test_architecture.py`` -- so reproducing the shipped
#: snapshot never requires widening either pin.
MARKETPLACE_BUILD_ROOT = ROOT / "plugin_platform" / "marketplace_build"
DEFAULT_METADATA_ROOT = MARKETPLACE_BUILD_ROOT / "metadata"
DEFAULT_REVIEW_POLICY_PATH = MARKETPLACE_BUILD_ROOT / "review_policy.json"

#: Where ``assemble``/``verify`` write and read a build run's OUTPUT by
#: default -- deliberately never ``plugin_platform/marketplace/``, the live
#: shipped, signed snapshot the runtime actually reads
#: (``plugin_platform.catalog.MARKETPLACE_CATALOG_PATH``). A default that
#: wrote there is exactly how a bare ``assemble`` invocation once clobbered
#: the shipped catalog's signature; publishing a new shipped snapshot is a
#: separate, deliberate step (an explicit ``--output``/``--root``), never a
#: CLI default. Gitignored: this is a build output, not a source input.
DEFAULT_BUILD_OUTPUT_ROOT = ROOT / "build" / "marketplace"

_CAPTURED_PAGE_NAME_WIDTH = 4
_CATALOG_METADATA_FILENAME = "catalog_metadata.json"
_MANIFEST_FILENAME = "plugin.json"
_SOURCE_EVIDENCE_FILENAME = "source_evidence.json"

_CATALOG_METADATA_FIELDS = frozenset(
    {
        "categories",
        "search_terms",
        "icon_asset_path",
        "support_url",
        "source_url",
        "distribution",
        "pricing",
        "offline_capable",
        "release_history",
        "update_notes_sha256",
        "released_at",
    }
)
_REVIEW_POLICY_FIELDS = frozenset({"schema_version", "promotions"})
_PROMOTION_FIELDS = frozenset({"plugin_id", "provenance_sha256"})

_CATALOG_INVALID = "marketplace_build_catalog_invalid"
_ENTRY_UNKNOWN = "marketplace_build_entry_unknown"
_METADATA_INVALID = "marketplace_build_metadata_invalid"
_OVERLAY_INVALID = "marketplace_build_overlay_invalid"
_PACKAGE_IDENTITY_MISMATCH = "marketplace_build_package_identity_mismatch"
_PACKAGE_INVALID = "marketplace_build_package_invalid"
_PACKAGES_ROOT_INVALID = "marketplace_build_packages_root_invalid"
_POLICY_INVALID = "marketplace_build_policy_invalid"
_POLICY_PROVENANCE_MISMATCH = "marketplace_build_policy_provenance_mismatch"
_PROVENANCE_MISSING = "marketplace_build_provenance_missing"
_SELECTION_AMBIGUOUS = "marketplace_build_selection_ambiguous"
_SELECTION_INVALID = "marketplace_build_selection_invalid"
_SELECTION_UNRESOLVED = "marketplace_build_selection_unresolved"
_SNAPSHOT_MISMATCH = "marketplace_build_snapshot_mismatch"
_TRANSPORT_INVALID = "marketplace_build_transport_invalid"


class BuildError(Exception):
    """A stable, content-free marketplace build rejection reason."""


# --------------------------------------------------------------------------
# fetch-registry: bounded capture, no key access.
# --------------------------------------------------------------------------


def fetch_registry_pages(
    transport: McpRegistryTransport,
    *,
    requests: tuple[McpRegistryRequest, ...],
) -> tuple[bytes, ...]:
    """Capture exactly the bounded registry pages a maintainer explicitly requested.

    Each captured payload is validated through the same bounded registry page
    parser used everywhere else before it is ever written to disk, so a
    capture step can never persist a page the rest of the pipeline would
    refuse to read back. The bytes are otherwise stored verbatim.
    """
    if type(requests) is not tuple or not requests:
        raise BuildError(_SELECTION_INVALID)
    pages: list[bytes] = []
    for request in requests:
        if type(request) is not McpRegistryRequest:
            raise BuildError(_SELECTION_INVALID)
        try:
            payload = transport(request)
        except McpRegistryError:
            raise
        except Exception:
            raise BuildError(_TRANSPORT_INVALID) from None
        if type(payload) is not bytes:
            raise BuildError(_TRANSPORT_INVALID)
        McpRegistryClient(transport=lambda _r, _p=payload: _p).search(
            query=request.query, cursor=request.cursor, limit=request.limit
        )
        pages.append(payload)
    return tuple(pages)


def write_captured_pages(directory: Path, pages: tuple[bytes, ...]) -> None:
    """Persist captured pages verbatim, one file per page, in capture order."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for index, payload in enumerate(pages):
        name = f"page-{index:0{_CAPTURED_PAGE_NAME_WIDTH}d}.json"
        (root / name).write_bytes(payload)


def read_captured_pages(directory: Path) -> tuple[bytes, ...]:
    """Read back captured pages in the exact order they were written."""
    root = Path(directory)
    try:
        names = sorted(path.name for path in root.iterdir() if path.is_file())
    except OSError:
        raise BuildError(_TRANSPORT_INVALID) from None
    return tuple((root / name).read_bytes() for name in names)


# --------------------------------------------------------------------------
# normalize: unsigned candidates plus a review report. No network, no key.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewReportEntry:
    """One maintainer-facing summary line for a candidate awaiting review."""

    plugin_id: str
    registry_name: str
    version: str
    trust_tier: str
    metadata_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "registry_name": self.registry_name,
            "version": self.version,
            "trust_tier": self.trust_tier,
            "metadata_sha256": self.metadata_sha256,
        }


@dataclass(frozen=True, slots=True)
class NormalizeResult:
    """Unsigned normalized candidates plus their maintainer review report."""

    candidates: tuple[NormalizedPluginCandidate, ...]
    review_report: tuple[ReviewReportEntry, ...]


def _parsed_servers(pages: tuple[bytes, ...]) -> dict[tuple[str, str], McpRegistryServer]:
    servers: dict[tuple[str, str], McpRegistryServer] = {}
    for payload in pages:
        try:
            page = McpRegistryClient(transport=lambda _r, _p=payload: _p).search()
        except McpRegistryError as exc:
            raise BuildError(str(exc)) from None
        for server in page.servers:
            if not server.is_latest or server.status is RegistryStatus.DELETED:
                continue
            identity = (server.name, server.version)
            if identity in servers:
                raise BuildError(_SELECTION_AMBIGUOUS)
            servers[identity] = server
    return servers


def normalize_captured_pages(
    pages: tuple[bytes, ...],
    *,
    selections: tuple[tuple[str, str], ...],
    importer: MarketplaceImporter,
) -> NormalizeResult:
    """Normalize exactly the maintainer-requested ``(name, version)`` listings."""
    if type(selections) is not tuple or not selections:
        raise BuildError(_SELECTION_INVALID)
    if type(importer) is not MarketplaceImporter:
        raise BuildError(_SELECTION_INVALID)
    servers = _parsed_servers(pages)

    candidates: list[NormalizedPluginCandidate] = []
    report: list[ReviewReportEntry] = []
    for identity in selections:
        server = servers.get(identity)
        if server is None:
            raise BuildError(_SELECTION_UNRESOLVED)
        try:
            candidate = importer.normalize(server, identity[1])
        except PluginImportError as exc:
            raise BuildError(str(exc)) from None
        candidates.append(candidate)
        report.append(
            ReviewReportEntry(
                plugin_id=candidate.manifest.plugin_id,
                registry_name=server.name,
                version=candidate.manifest.plugin_version,
                trust_tier=candidate.trust_tier.value,
                metadata_sha256=candidate.source_evidence.metadata_sha256,
            )
        )
    return NormalizeResult(candidates=tuple(candidates), review_report=tuple(report))


def write_normalized_candidate(packages_root: Path, candidate: NormalizedPluginCandidate) -> Path:
    """Write one unsealed candidate manifest as a package directory ``assemble`` can read.

    This directory holds only what ``package_verifier.verify_plugin_package``
    itself would later expect to find (the manifest, and any declared asset
    files) -- nothing else is ever written alongside it, so a package
    directory's exact file set is never widened by build-time bookkeeping.
    """
    if type(candidate) is not NormalizedPluginCandidate:
        raise BuildError(_PACKAGE_INVALID)
    package_dir = Path(packages_root) / candidate.manifest.plugin_id
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / _MANIFEST_FILENAME).write_bytes(canonical_bytes(candidate.manifest.to_dict()))
    return package_dir


def write_candidate_evidence(metadata_root: Path, candidate: NormalizedPluginCandidate) -> Path:
    """Write one candidate's registry evidence into the separate metadata tree.

    Evidence lives under ``metadata_root``, never inside the package
    directory itself: ``assemble`` reads a package directory's exact file set
    through the same strict verifier the installer uses, and build-time
    bookkeeping must never widen it. This evidence is what lets ``assemble``
    compute a ``provenance_sha256`` for a listing no review policy promoted,
    without ever inventing one.
    """
    if type(candidate) is not NormalizedPluginCandidate:
        raise BuildError(_PACKAGE_INVALID)
    entry_dir = Path(metadata_root) / candidate.manifest.plugin_id
    entry_dir.mkdir(parents=True, exist_ok=True)
    evidence = candidate.source_evidence
    (entry_dir / _SOURCE_EVIDENCE_FILENAME).write_bytes(
        canonical_bytes(
            {
                "registry_name": evidence.registry_name,
                "namespace_kind": evidence.namespace_kind.value,
                "version": evidence.version,
                "metadata_sha256": evidence.metadata_sha256,
                "artifact_sha256": evidence.artifact_sha256,
            }
        )
    )
    return entry_dir


def write_review_report(path: Path, report: tuple[ReviewReportEntry, ...]) -> None:
    """Persist the maintainer-facing review report as canonical JSON."""
    Path(path).write_bytes(canonical_bytes([entry.to_dict() for entry in report]))


# --------------------------------------------------------------------------
# assemble: deterministic unsigned catalog. No network, no signing key.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    """An explicit, closed list of promotions to ``omega_verified``.

    Absence from this document can never imply verification: any
    ``plugin_id`` not named here is at most ``publisher_verified``.
    """

    promotions: Mapping[str, str]

    @classmethod
    def from_dict(cls, raw: Any) -> ReviewPolicy:
        if type(raw) is not dict or set(raw) != _REVIEW_POLICY_FIELDS:
            raise BuildError(_POLICY_INVALID)
        if raw["schema_version"] != REVIEW_POLICY_SCHEMA_VERSION:
            raise BuildError(_POLICY_INVALID)
        if type(raw["promotions"]) is not list:
            raise BuildError(_POLICY_INVALID)
        promotions: dict[str, str] = {}
        for item in raw["promotions"]:
            if type(item) is not dict or set(item) != _PROMOTION_FIELDS:
                raise BuildError(_POLICY_INVALID)
            try:
                plugin_id = _identifier(item["plugin_id"], field="plugin_id")
                digest = _digest(item["provenance_sha256"], field="provenance_sha256")
            except (TypeError, ValueError):
                raise BuildError(_POLICY_INVALID) from None
            if plugin_id in promotions:
                raise BuildError(_POLICY_INVALID)
            promotions[plugin_id] = digest
        return cls(promotions=MappingProxyType(promotions))

    def promoted_provenance(self, plugin_id: str) -> str | None:
        return self.promotions.get(plugin_id)


def load_review_policy(path: Path) -> ReviewPolicy:
    try:
        raw = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise BuildError(_POLICY_INVALID) from None
    return ReviewPolicy.from_dict(raw)


@dataclass(frozen=True, slots=True)
class AssembledCatalog:
    """A deterministic unsigned catalog document, ready for ``sign``.

    ``registry_name_aliases`` lets a caller look a listing up by the exact
    registry ``namespace/server`` name it was normalized from, in addition to
    its canonical ``plugin_id`` -- both name the one listing this build
    assembled, never two different ones.
    """

    document: Mapping[str, object]
    entries_by_id: Mapping[str, MarketplaceCatalogEntry]
    registry_name_aliases: Mapping[str, str]

    @property
    def catalog_unsigned_bytes(self) -> bytes:
        return canonical_bytes(dict(self.document))

    def entry(self, plugin_id: str) -> MarketplaceCatalogEntry:
        if type(plugin_id) is not str:
            raise BuildError(_ENTRY_UNKNOWN)
        resolved = self.registry_name_aliases.get(plugin_id, plugin_id)
        found = self.entries_by_id.get(resolved)
        if found is None:
            raise BuildError(_ENTRY_UNKNOWN)
        return found


def _read_source_evidence(metadata_root: Path, plugin_id: str) -> dict[str, object] | None:
    evidence_path = Path(metadata_root) / plugin_id / _SOURCE_EVIDENCE_FILENAME
    try:
        evidence_doc = json.loads(evidence_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return evidence_doc if type(evidence_doc) is dict else None


def _registry_name_alias(metadata_root: Path, plugin_id: str) -> str | None:
    """Return the registry ``namespace/server`` name a candidate was normalized from.

    Best-effort and read-only: a first-party package carries no such evidence
    at all, and that is not an error -- it simply has no registry alias.
    """
    evidence_doc = _read_source_evidence(metadata_root, plugin_id)
    if evidence_doc is None:
        return None
    name = evidence_doc.get("registry_name")
    return name if type(name) is str else None


_AUTHENTICATION_OVERLAY_FILENAME = "authentication.json"
_AUTHENTICATION_OVERLAY_FIELDS = frozenset({"kind", "authorization_issuer", "scopes", "profile_id"})
_OVERLAY_KIND = "oauth_authorization_code_pkce"
_MAX_OVERLAY_BYTES = 16_384


def _load_authentication_overlay(metadata_root: Path, plugin_id: str) -> dict[str, object] | None:
    """Return the parsed overlay, or None when this plugin_id has no authentication.json.

    The overlay is the one maintainer-authored widening the builder accepts:
    it may put OAuth on a candidate whose registry listing declared no
    authentication, and nothing else. Every malformed document is rejected.
    """
    path = Path(metadata_root) / plugin_id / _AUTHENTICATION_OVERLAY_FILENAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise BuildError(_OVERLAY_INVALID) from None
    try:
        if not raw or len(raw) > _MAX_OVERLAY_BYTES:
            raise ValueError
        document = json.loads(raw.decode("utf-8"))
        if type(document) is not dict or set(document) != _AUTHENTICATION_OVERLAY_FIELDS:
            raise ValueError
        if document["kind"] != _OVERLAY_KIND:
            raise ValueError
        issuer = _https_issuer(document["authorization_issuer"], field="overlay authorization_issuer")
        scopes = document["scopes"]
        if (
            type(scopes) is not list
            or not scopes
            or any(type(item) is not str or not item or item.strip() != item for item in scopes)
            or len(scopes) != len(set(scopes))
        ):
            raise ValueError
        profile_id = _identifier(document["profile_id"], field="overlay profile_id")
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise BuildError(_OVERLAY_INVALID) from None
    return {
        "kind": _OVERLAY_KIND,
        "authorization_issuer": issuer,
        "scopes": list(scopes),
        "profile_id": profile_id,
    }


def _apply_authentication_overlay(
    manifest_doc: dict[str, object],
    overlay: dict[str, object],
    *,
    profile_registry: OAuthProfileRegistry,
) -> dict[str, object]:
    """Return a manifest document with its single none-candidate app set to OAuth.

    Only a candidate whose one required app declares ``authentication.kind ==
    "none"`` is eligible (a listing that already declares secret fields keeps
    them; the overlay never rewrites declared identity). The ``profile_id``
    must resolve in the signed OAuth profile catalog and that profile must name
    the same issuer, so the manifest can never point a user at an issuer the
    profile catalog did not sign for.
    """
    apps = manifest_doc.get("required_apps")
    if type(apps) is not list or len(apps) != 1 or type(apps[0]) is not dict:
        raise BuildError(_OVERLAY_INVALID)
    app = apps[0]
    try:
        profile = profile_registry.resolve_by_profile_id(str(overlay["profile_id"]))
    except ValueError:
        raise BuildError(_OVERLAY_INVALID) from None
    if profile.authorization_issuer != overlay["authorization_issuer"]:
        raise BuildError(_OVERLAY_INVALID)
    overlaid_app = {
        **app,
        "authentication": {
            "kind": _OVERLAY_KIND,
            "scopes": list(overlay["scopes"]),
            "secret_fields": [],
        },
        "authorization_issuer": overlay["authorization_issuer"],
        "provider_id": profile.provider_id,
    }
    if app == overlaid_app:
        # Already applied by an earlier assemble of the same inputs: reproducible.
        return manifest_doc
    authentication = app.get("authentication")
    if type(authentication) is not dict or authentication.get("kind") != "none":
        raise BuildError(_OVERLAY_INVALID)
    return {**manifest_doc, "required_apps": [overlaid_app]}


def _load_catalog_metadata(metadata_root: Path, plugin_id: str) -> dict[str, object]:
    path = Path(metadata_root) / plugin_id / _CATALOG_METADATA_FILENAME
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise BuildError(_METADATA_INVALID) from None
    if type(raw) is not dict or set(raw) != _CATALOG_METADATA_FIELDS:
        raise BuildError(_METADATA_INVALID)
    return raw


def _unsealed_digests(manifest: PluginManifest) -> tuple[str, str]:
    manifest_sha256 = sha256_hex(canonical_bytes(manifest.to_dict()))
    package_sha256 = sha256_hex(
        canonical_bytes(
            {
                "manifest_sha256": manifest_sha256,
                "assets": {asset.path: asset.sha256 for asset in manifest.assets},
            }
        )
    )
    return manifest_sha256, package_sha256


def _signed_digests(
    package_dir: Path,
    manifest: PluginManifest,
    *,
    key_ring: TrustedPublisherKeyRing,
    omega_version: str,
) -> tuple[str, str]:
    try:
        verified = verify_plugin_package(package_dir, key_ring, omega_version)
    except PackageVerificationError as exc:
        raise BuildError(str(exc)) from None
    if verified.manifest.plugin_id != manifest.plugin_id:
        raise BuildError(_PACKAGE_IDENTITY_MISMATCH)
    return verified.manifest_sha256, verified.package_sha256


def _bundled_provenance_digest(
    plugin_id: str, *, provenance_path: Path, key_ring: TrustedPublisherKeyRing
) -> str:
    try:
        provenance = verify_provider_provenance(provenance_path, key_ring)
        return sha256_hex(canonical_bytes(provenance.for_plugin(plugin_id).to_dict()))
    except ProviderProvenanceVerificationError:
        raise BuildError(_POLICY_PROVENANCE_MISMATCH) from None


def _trust_tier_and_provenance(
    metadata_root: Path,
    plugin_id: str,
    *,
    review_policy: ReviewPolicy,
    provenance_path: Path,
    key_ring: TrustedPublisherKeyRing,
) -> tuple[TrustTier, str]:
    promoted_digest = review_policy.promoted_provenance(plugin_id)
    if promoted_digest is not None:
        actual_digest = _bundled_provenance_digest(
            plugin_id, provenance_path=provenance_path, key_ring=key_ring
        )
        if actual_digest != promoted_digest:
            raise BuildError(_POLICY_PROVENANCE_MISMATCH)
        return TrustTier.OMEGA_VERIFIED, actual_digest
    evidence_doc = _read_source_evidence(metadata_root, plugin_id)
    if evidence_doc is None:
        raise BuildError(_PROVENANCE_MISSING)
    return TrustTier.PUBLISHER_VERIFIED, sha256_hex(canonical_bytes(evidence_doc))


def _assemble_entry(
    package_dir: Path,
    *,
    metadata_root: Path,
    review_policy: ReviewPolicy,
    provenance_path: Path,
    key_ring: TrustedPublisherKeyRing,
    omega_version: str,
    profile_registry: Callable[[], OAuthProfileRegistry],
) -> MarketplaceCatalogEntry:
    manifest_path = package_dir / _MANIFEST_FILENAME
    try:
        manifest_doc = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise BuildError(_PACKAGE_INVALID) from None
    overlay = _load_authentication_overlay(metadata_root, package_dir.name)
    if overlay is not None:
        if type(manifest_doc) is not dict:
            raise BuildError(_PACKAGE_INVALID)
        manifest_doc = _apply_authentication_overlay(
            manifest_doc, overlay, profile_registry=profile_registry()
        )
    try:
        manifest = PluginManifest.from_dict(manifest_doc)
    except (TypeError, ValueError):
        raise BuildError(_OVERLAY_INVALID if overlay is not None else _PACKAGE_INVALID) from None
    if manifest.plugin_id != package_dir.name:
        raise BuildError(_PACKAGE_IDENTITY_MISMATCH)
    if overlay is not None and manifest.signing_key_id == UNSEALED_SIGNING_KEY_ID:
        # The later ``sign`` step signs the overlaid content; a sealed package is
        # never rewritten here (its overlay must already be inside its signature).
        manifest_path.write_bytes(canonical_bytes(manifest.to_dict()))
    metadata = _load_catalog_metadata(metadata_root, manifest.plugin_id)

    if manifest.signing_key_id == UNSEALED_SIGNING_KEY_ID:
        if manifest.signature != UNSEALED_SIGNATURE:
            raise BuildError(_PACKAGE_INVALID)
        manifest_sha256, package_sha256 = _unsealed_digests(manifest)
    else:
        manifest_sha256, package_sha256 = _signed_digests(
            package_dir, manifest, key_ring=key_ring, omega_version=omega_version
        )

    trust_tier, provenance_sha256 = _trust_tier_and_provenance(
        metadata_root,
        manifest.plugin_id,
        review_policy=review_policy,
        provenance_path=provenance_path,
        key_ring=key_ring,
    )

    entry_dict = {
        "plugin_id": manifest.plugin_id,
        "publisher_id": manifest.publisher_id,
        "plugin_version": manifest.plugin_version,
        "manifest_sha256": manifest_sha256,
        "package_sha256": package_sha256,
        "package_uri": marketplace_package_uri(manifest.plugin_id, manifest.plugin_version),
        "title": manifest.title,
        "summary": manifest.description,
        "trust_tier": trust_tier.value,
        "provenance_sha256": provenance_sha256,
        **metadata,
    }
    try:
        return MarketplaceCatalogEntry.from_dict(entry_dict)
    except ValueError:
        raise BuildError(_CATALOG_INVALID) from None


def assemble_catalog(
    packages_root: Path,
    *,
    metadata_root: Path = DEFAULT_METADATA_ROOT,
    catalog_id: str,
    publisher_id: str,
    sequence: int,
    signing_key_id: str,
    review_policy: ReviewPolicy,
    provenance_path: Path = BUNDLED_PROVENANCE_PATH,
    key_ring: TrustedPublisherKeyRing | None = None,
    omega_version: str = DEFAULT_OMEGA_VERSION,
    profile_registry: OAuthProfileRegistry | None = None,
    profile_catalog_path: Path = core_paths.OAUTH_PROFILE_CATALOG_PATH,
) -> AssembledCatalog:
    """Deterministically assemble every package directory into one unsigned catalog.

    ``profile_registry`` resolves ``authentication.json`` overlays; when a
    package needs one and none was supplied, the shipped signed OAuth profile
    catalog is loaded once, verified by the embedded ring.

    ``packages_root`` holds only exact, installer-shaped package directories
    (a manifest plus its declared assets, and nothing else) -- the same shape
    ``package_verifier.verify_plugin_package`` demands, so a package directory
    already shipped as part of a signed snapshot can be read here unmodified.
    Everything ``assemble`` needs beyond that (curated catalog listing
    metadata, and registry evidence for a listing no policy promoted) lives
    under the separate ``metadata_root`` tree, one subdirectory per
    ``plugin_id``, so build-time bookkeeping never widens a package
    directory's exact verified file set.
    """
    root = Path(packages_root)
    metadata = Path(metadata_root)
    if not root.is_dir() or not metadata.is_dir():
        raise BuildError(_PACKAGES_ROOT_INVALID)
    if type(sequence) is not int or sequence < 1:
        raise BuildError(_CATALOG_INVALID)
    if type(review_policy) is not ReviewPolicy:
        raise BuildError(_POLICY_INVALID)
    ring = load_embedded_key_ring() if key_ring is None else key_ring
    loaded_registry: list[OAuthProfileRegistry] = (
        [] if profile_registry is None else [profile_registry]
    )

    def _profile_registry() -> OAuthProfileRegistry:
        if not loaded_registry:
            try:
                loaded_registry.append(
                    OAuthProfileRegistry.from_path(
                        Path(profile_catalog_path), load_embedded_key_ring()
                    )
                )
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                raise BuildError(_OVERLAY_INVALID) from None
        return loaded_registry[0]

    try:
        catalog_id = _identifier(catalog_id, field="catalog_id")
        publisher_id = _identifier(publisher_id, field="publisher_id")
        signing_key_id = _identifier(signing_key_id, field="signing_key_id")
    except (TypeError, ValueError):
        raise BuildError(_CATALOG_INVALID) from None

    package_dirs = sorted(
        (path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name
    )
    entries = [
        _assemble_entry(
            package_dir,
            metadata_root=metadata,
            review_policy=review_policy,
            provenance_path=provenance_path,
            key_ring=ring,
            omega_version=omega_version,
            profile_registry=_profile_registry,
        )
        for package_dir in package_dirs
    ]
    if not entries:
        raise BuildError(_PACKAGES_ROOT_INVALID)
    plugin_ids = tuple(entry.plugin_id for entry in entries)
    if len(plugin_ids) != len(set(plugin_ids)):
        raise BuildError(_CATALOG_INVALID)
    aliases = {
        alias: entry.plugin_id
        for entry in entries
        if (alias := _registry_name_alias(metadata, entry.plugin_id)) is not None
    }

    document: dict[str, object] = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_id": catalog_id,
        "publisher_id": publisher_id,
        "sequence": sequence,
        "entries": [entry.to_dict() for entry in entries],
        "signing_key_id": signing_key_id,
    }
    try:
        canonical_bytes(document)
    except (TypeError, ValueError):
        raise BuildError(_CATALOG_INVALID) from None
    return AssembledCatalog(
        document=MappingProxyType(document),
        entries_by_id=MappingProxyType({entry.plugin_id: entry for entry in entries}),
        registry_name_aliases=MappingProxyType(aliases),
    )


def write_assembled_catalog(path: Path, assembled: AssembledCatalog) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(assembled.catalog_unsigned_bytes)


# --------------------------------------------------------------------------
# sign: delegate to the existing protected signing boundary. No network.
# --------------------------------------------------------------------------


def sign(argv: Sequence[str] | None = None) -> None:
    """Delegate signing to the existing protected boundary, unmodified.

    This command never reads or holds key material itself; it only forwards
    to ``scripts.sign_marketplace_catalog.main``, which opens the operator's
    sealed or DPAPI seed in memory (or reads the environment override) and
    never prints or persists it.
    """
    sign_marketplace_catalog.main(argv)


# --------------------------------------------------------------------------
# verify: full offline verification of the final signed snapshot.
# --------------------------------------------------------------------------


def verify_snapshot(
    root: Path,
    *,
    key_ring: TrustedPublisherKeyRing | None = None,
    provenance_path: Path = BUNDLED_PROVENANCE_PATH,
    omega_version: str = DEFAULT_OMEGA_VERSION,
):
    """Fully verify one signed marketplace snapshot directory offline."""
    ring = load_embedded_key_ring() if key_ring is None else key_ring
    catalog_path = Path(root) / "catalog.json"
    packages_root = Path(root) / "packages"
    try:
        catalog = verify_plugin_catalog(catalog_path, ring)
    except CatalogVerificationError as exc:
        raise BuildError(str(exc)) from None

    provenance = None
    if any(entry.trust_tier is TrustTier.OMEGA_VERIFIED for entry in catalog.entries):
        try:
            provenance = verify_provider_provenance(provenance_path, ring)
        except ProviderProvenanceVerificationError as exc:
            raise BuildError(str(exc)) from None

    for entry in catalog.entries:
        package_dir = packages_root / entry.plugin_id
        try:
            verified = verify_plugin_package(package_dir, ring, omega_version)
        except PackageVerificationError as exc:
            raise BuildError(str(exc)) from None
        if (
            verified.manifest.plugin_id,
            verified.manifest.publisher_id,
            verified.manifest.plugin_version,
            verified.manifest_sha256,
            verified.package_sha256,
        ) != (
            entry.plugin_id,
            entry.publisher_id,
            entry.plugin_version,
            entry.manifest_sha256,
            entry.package_sha256,
        ):
            raise BuildError(_SNAPSHOT_MISMATCH)
        if entry.trust_tier is TrustTier.OMEGA_VERIFIED:
            assert provenance is not None
            try:
                provenance.for_verified_plugin(verified)
            except ProviderProvenanceVerificationError as exc:
                raise BuildError(str(exc)) from None
    return catalog


# --------------------------------------------------------------------------
# CLI wiring.
# --------------------------------------------------------------------------


def _cmd_fetch_registry(args: argparse.Namespace) -> None:
    from mcp_platform.registry_fetch import build_registry_page_fetch

    transport = (
        build_registry_page_fetch(url=args.loopback_url)
        if args.loopback_url is not None
        else build_registry_page_fetch()
    )
    pages = fetch_registry_pages(transport, requests=(McpRegistryRequest(),))
    write_captured_pages(Path(args.output_dir), pages)


def _parse_selection(item: str) -> tuple[str, str]:
    """Parse one ``--select name@version`` argument, or fail closed cleanly."""
    if type(item) is not str or item.count("@") != 1:
        raise BuildError(_SELECTION_INVALID)
    name, version = item.split("@", 1)
    if not name or not version:
        raise BuildError(_SELECTION_INVALID)
    return name, version


def _cmd_normalize(args: argparse.Namespace) -> None:
    pages = read_captured_pages(Path(args.captured_dir))
    selections = tuple(_parse_selection(item) for item in args.select)
    result = normalize_captured_pages(
        pages, selections=selections, importer=MarketplaceImporter()
    )
    for candidate in result.candidates:
        write_normalized_candidate(Path(args.packages_root), candidate)
        write_candidate_evidence(Path(args.metadata_root), candidate)
    write_review_report(Path(args.review_report), result.review_report)


def _cmd_assemble(args: argparse.Namespace) -> None:
    review_policy = load_review_policy(Path(args.review_policy))
    assembled = assemble_catalog(
        Path(args.packages_root),
        metadata_root=Path(args.metadata_root),
        catalog_id=args.catalog_id,
        publisher_id=args.publisher_id,
        sequence=args.sequence,
        signing_key_id=args.signing_key_id,
        review_policy=review_policy,
    )
    write_assembled_catalog(Path(args.output), assembled)


def _cmd_sign(args: argparse.Namespace) -> None:
    forwarded: list[str] = []
    for plugin_id in args.plugin_ids or ():
        forwarded += ["--plugin-id", plugin_id]
    if args.catalog_only:
        forwarded += ["--catalog-only"]
    if args.use_default_seed_path:
        forwarded += ["--seed-dpapi-path"]
    elif args.seed_dpapi_path is not None:
        forwarded += ["--seed-dpapi-path", args.seed_dpapi_path]
    elif args.use_default_sealed_seed_path:
        forwarded += ["--seed-sealed-path"]
    elif args.seed_sealed_path is not None:
        forwarded += ["--seed-sealed-path", args.seed_sealed_path]
    sign(forwarded)


def _cmd_verify(args: argparse.Namespace) -> None:
    verify_snapshot(Path(args.root))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch-registry")
    fetch.add_argument("--output-dir", required=True)
    fetch.add_argument("--loopback-url", default=None)
    fetch.set_defaults(handler=_cmd_fetch_registry)

    normalize = subparsers.add_parser("normalize")
    normalize.add_argument("--captured-dir", required=True)
    normalize.add_argument("--packages-root", required=True)
    normalize.add_argument("--metadata-root", required=True)
    normalize.add_argument("--review-report", required=True)
    normalize.add_argument("--select", action="append", required=True, help="name@version")
    normalize.set_defaults(handler=_cmd_normalize)

    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--packages-root", default=str(MARKETPLACE_PACKAGES_ROOT))
    assemble.add_argument("--metadata-root", default=str(DEFAULT_METADATA_ROOT))
    assemble.add_argument("--review-policy", default=str(DEFAULT_REVIEW_POLICY_PATH))
    assemble.add_argument("--catalog-id", required=True)
    assemble.add_argument("--publisher-id", required=True)
    assemble.add_argument("--sequence", type=int, required=True)
    assemble.add_argument("--signing-key-id", required=True)
    assemble.add_argument("--output", default=str(DEFAULT_BUILD_OUTPUT_ROOT / "catalog.json"))
    assemble.set_defaults(handler=_cmd_assemble)

    sign_parser = subparsers.add_parser("sign")
    sign_parser.add_argument("--plugin-id", action="append", dest="plugin_ids")
    sign_parser.add_argument("--seed-dpapi-path", dest="seed_dpapi_path", default=None)
    sign_parser.add_argument(
        "--use-default-seed-path",
        action="store_true",
        help="read the operator DPAPI seed from its default release-secrets path",
    )
    sign_parser.add_argument("--seed-sealed-path", dest="seed_sealed_path", default=None)
    sign_parser.add_argument(
        "--use-default-sealed-seed-path",
        action="store_true",
        help="read the passphrase-sealed portable seed from guardian/release-secrets",
    )
    sign_parser.add_argument(
        "--catalog-only",
        action="store_true",
        help="re-sign provenance and catalog only; never touch the shipped package bytes",
    )
    sign_parser.set_defaults(handler=_cmd_sign)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", default=str(DEFAULT_BUILD_OUTPUT_ROOT))
    verify.set_defaults(handler=_cmd_verify)

    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
