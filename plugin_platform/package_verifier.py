"""Fail-closed verification for signed declarative plugin package trees."""

from __future__ import annotations

import json
import os
import re
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from packaging.version import InvalidVersion, Version
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.catalog import TrustTier
from plugin_platform.contracts import (
    AppRequirement,
    AssetKind,
    CapabilityKind,
    PluginAsset,
    PluginManifest,
)
from plugin_platform.filesystem import (
    PinnedPackageTree,
    ensure_contained,
)
from plugin_platform.local_trust import (
    LOCAL_SEAL_NAME,
    LocalTrustError,
    LocalTrustRoot,
    SealedCapabilityAuthority,
)
from plugin_platform.trust import (
    PublisherTrustError,
    TrustedPublisherKeyRing,
    verify_signed_bytes,
)

MAX_MANIFEST_BYTES = 256 * 1024
MAX_SKILL_BYTES = 512 * 1024
MAX_WORKFLOW_BYTES = 256 * 1024
MAX_ICON_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 8 * 1024 * 1024

_MANIFEST_NAME = "plugin.json"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SECRET_WORDS = frozenset(
    {"secret", "secrets", "credential", "credentials", "password", "passwords", "token", "tokens"}
)
_CAPABILITY_AUTHORITY_ROOT_FIELDS = frozenset({"schema_version", "plugin_id", "apps"})
_CAPABILITY_AUTHORITY_GROUP_FIELDS = frozenset(
    {"tools", "resources", "resource_templates", "prompts"}
)
_LEGACY_CAPABILITY_AUTHORITY_FIELDS = (
    _CAPABILITY_AUTHORITY_GROUP_FIELDS | {"runtime_compatibility"}
)
_RUNTIME_COMPATIBILITY_FIELDS = frozenset(
    {"wire_protocol_version", "handshake_mode", "missing_metadata"}
)


class PackageVerificationError(ValueError):
    """A stable, content-free plugin package rejection reason."""


@dataclass(frozen=True)
class VerifiedPlugin:
    """Immutable plugin whose source root is provenance and asset bytes are authoritative."""

    manifest: PluginManifest
    package_root: Path
    manifest_sha256: str
    package_sha256: str
    asset_paths: Mapping[str, Path]
    asset_bytes: Mapping[str, bytes]
    #: The one parsed signed capability authority, per signed app dependency.
    capability_authority: Mapping[str, Mapping[str, object]] | None = None
    #: OMEGA_VERIFIED for the marketplace Ed25519 path (default, every existing
    #: caller); PUBLISHER_VERIFIED only for a package verified through
    #: ``verify_locally_sealed_plugin_package`` -- never assigned any other way.
    trust_tier: TrustTier = TrustTier.OMEGA_VERIFIED
    #: True only when ``tools/authority.json`` is the retired pre-v2 flat
    #: single-app shape.  The shape is identified only far enough to mark
    #: the package; it is never parsed into ``capability_authority`` (spec
    #: principle 8 forbids a compatibility fork). Only the marketplace
    #: Ed25519 path (``verify_plugin_package``) can ever set this -- the
    #: locally-sealed path never produces or accepts the retired shape, so
    #: it always leaves this at the default and cannot grant a trust tier
    #: above ``PUBLISHER_VERIFIED`` through it either way.
    legacy_authority: bool = False


_VERIFIED_REQUIREMENTS_LOCK = threading.Lock()
_VERIFIED_REQUIREMENTS: dict[int, weakref.ReferenceType[AppRequirement]] = {}


def _issue_verified_app_requirements(manifest: PluginManifest) -> None:
    for requirement in (*manifest.required_apps, *manifest.optional_apps):
        identity = id(requirement)

        def discard(
            reference: weakref.ReferenceType[AppRequirement],
            *,
            requirement_id: int = identity,
        ) -> None:
            with _VERIFIED_REQUIREMENTS_LOCK:
                if _VERIFIED_REQUIREMENTS.get(requirement_id) is reference:
                    _VERIFIED_REQUIREMENTS.pop(requirement_id, None)

        reference = weakref.ref(requirement, discard)
        with _VERIFIED_REQUIREMENTS_LOCK:
            _VERIFIED_REQUIREMENTS[identity] = reference


def validate_verified_app_requirement(requirement: AppRequirement) -> AppRequirement:
    """Return only the exact requirement issued by successful package verification."""
    if type(requirement) is not AppRequirement:
        raise ValueError("app_requirement_not_verified")
    with _VERIFIED_REQUIREMENTS_LOCK:
        reference = _VERIFIED_REQUIREMENTS.get(id(requirement))
    if reference is None or reference() is not requirement:
        raise ValueError("app_requirement_not_verified")
    return requirement


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate_key")
        document[key] = value
    return document


def _reject_non_json_constant(value: str) -> None:
    raise ValueError("non_json_constant")


def _read_regular_bytes(
    tree: PinnedPackageTree,
    path: Path,
    *,
    limit: int,
    too_large_reason: str,
) -> bytes:
    try:
        candidate = ensure_contained(tree.root, path)
        relative_path = candidate.relative_to(tree.root).as_posix()
        return tree.read_bytes(relative_path, limit=limit)
    except ValueError as exc:
        if str(exc) == "file_too_large":
            raise PackageVerificationError(too_large_reason) from None
        raise PackageVerificationError("package_tree_invalid") from None
    except (OSError, RuntimeError, TypeError):
        raise PackageVerificationError("package_tree_invalid") from None


def _parse_manifest(payload: bytes) -> PluginManifest:
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PackageVerificationError("manifest_json_invalid") from None
    try:
        if canonical_bytes(document) != payload:
            raise PackageVerificationError("manifest_not_canonical")
    except PackageVerificationError:
        raise
    except (TypeError, ValueError):
        raise PackageVerificationError("manifest_json_invalid") from None
    try:
        return PluginManifest.from_dict(document)
    except (TypeError, ValueError):
        raise PackageVerificationError("manifest_invalid") from None


def _asset_path(root: Path, asset: PluginAsset) -> Path:
    try:
        return ensure_contained(root, root.joinpath(*asset.path.split("/")))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise PackageVerificationError("manifest_invalid") from None


def _secret_bearing_path(path: str) -> bool:
    words = tuple(re.findall(r"[a-z]+|[0-9]+", path.casefold()))
    word_set = set(words)
    return bool(
        word_set.intersection(_SECRET_WORDS)
        or {"api", "key"}.issubset(word_set)
        or {"private", "key"}.issubset(word_set)
    )


def _validate_asset_declaration(asset: PluginAsset) -> int:
    path = Path(asset.path)
    parts = asset.path.split("/")
    if any(not part for part in parts) or "/".join(parts) != asset.path:
        raise PackageVerificationError("manifest_invalid")
    if _secret_bearing_path(asset.path):
        raise PackageVerificationError("secret_material_forbidden")
    if asset.kind is AssetKind.SKILL:
        if parts[0] != "skills" or path.name != "SKILL.md":
            raise PackageVerificationError("asset_invalid")
        return MAX_SKILL_BYTES
    if asset.kind is AssetKind.WORKFLOW:
        if parts[0] != "workflows" or path.suffix != ".json":
            raise PackageVerificationError("asset_invalid")
        return MAX_WORKFLOW_BYTES
    if asset.kind is AssetKind.CAPABILITY_AUTHORITY:
        if asset.path != "tools/authority.json" or asset.media_type != "application/json":
            raise PackageVerificationError("asset_invalid")
        return MAX_WORKFLOW_BYTES
    if (
        asset.kind is not AssetKind.ICON
        or parts[0] != "assets"
        or path.suffix != ".png"
        or asset.media_type != "image/png"
    ):
        raise PackageVerificationError("asset_invalid")
    return MAX_ICON_BYTES


def _validate_runtime_compatibility(value: object) -> None:
    if type(value) is not dict or set(value) != _RUNTIME_COMPATIBILITY_FIELDS:
        raise ValueError("capability_authority_invalid")
    metadata = value["missing_metadata"]
    # Any wire version from the handshake era, not just its newest. The field
    # states which version THIS server speaks, and the client checks the live
    # negotiated version against it exactly -- so pinning it to the newest one
    # made every legacy server that is not on 2025-11-25 unsupportable. Google
    # answers 2025-11-25 and hid that; Slack answers 2025-06-18, and a signed,
    # installed, authorized plugin failed at connect with protocol_mismatch
    # (live 2026-09-07). Still a closed set the SDK recognises, so the drift
    # check it feeds keeps its meaning.
    if (
        value["wire_protocol_version"] not in HANDSHAKE_PROTOCOL_VERSIONS
        or value["handshake_mode"] != "legacy"
        or type(metadata) is not dict
        or set(metadata) != {"server_id", "server_version"}
        or any(
            type(metadata[field]) is not str
            or not metadata[field]
            or len(metadata[field]) > 120
            for field in ("server_id", "server_version")
        )
    ):
        raise ValueError("capability_authority_invalid")


def _validate_capability_authority(
    document: object,
    manifest: PluginManifest,
) -> tuple[dict[str, dict[str, object]], bool]:
    """Validate the signed capability-authority envelope and project it per app.

    This is the only parser of ``tools/authority.json``.  Every reader consumes
    the projection returned here through ``VerifiedPlugin.capability_authority``
    so no surface can re-derive a weaker view of the same signed bytes.

    Returns ``(projected, legacy)``.  A retired pre-v2 flat single-app shape
    is identified only far enough to report ``legacy=True``; per spec
    principle 8 it is never admitted as a second, weaker capability source,
    so ``projected`` is always empty in that case.
    """

    if type(document) is not dict:
        raise ValueError("capability_authority_invalid")
    requirements = {
        requirement.app_id: requirement
        for requirement in (*manifest.required_apps, *manifest.optional_apps)
    }
    if set(document) == _CAPABILITY_AUTHORITY_ROOT_FIELDS:
        if (
            document["schema_version"] != 2
            or document["plugin_id"] != manifest.plugin_id
            or type(document["apps"]) is not dict
        ):
            raise ValueError("capability_authority_invalid")
        apps = document["apps"]
    elif (
        len(requirements) == 1
        and set(document).issubset(_LEGACY_CAPABILITY_AUTHORITY_FIELDS)
    ):
        # Signed v1 authorities predate app partitioning.  The previously
        # shipped release (sequence 6) signed all four packages this way.
        # That shape is retired and forbidden as a compatibility fork: it is
        # identified by its top-level field names only, never parsed into
        # usable capability data.
        return {}, True
    else:
        raise ValueError("capability_authority_invalid")
    if set(apps) != set(requirements):
        raise ValueError("capability_authority_invalid")
    projected: dict[str, dict[str, object]] = {}
    for app_id, authority in apps.items():
        if type(authority) is not dict:
            raise ValueError("capability_authority_invalid")
        authority_fields = frozenset(authority)
        if authority_fields not in (
            _CAPABILITY_AUTHORITY_GROUP_FIELDS,
            _CAPABILITY_AUTHORITY_GROUP_FIELDS | {"runtime_compatibility"},
        ):
            raise ValueError("capability_authority_invalid")
        groups = {
            name: authority.get(name, {})
            for name in _CAPABILITY_AUTHORITY_GROUP_FIELDS
        }
        if any(
            type(policies) is not dict
            or any(type(policy) is not dict for policy in policies.values())
            for policies in groups.values()
        ):
            raise ValueError("capability_authority_invalid")
        declared: dict[CapabilityKind, set[str]] = {
            kind: set() for kind in CapabilityKind
        }
        for capability in requirements[app_id].capabilities:
            declared[capability.kind].add(capability.name)
        if (
            not set(groups["tools"]).issubset(declared[CapabilityKind.TOOL])
            or not set(groups["prompts"]).issubset(declared[CapabilityKind.PROMPT])
            or not (
                set(groups["resources"]) | set(groups["resource_templates"])
            ).issubset(declared[CapabilityKind.RESOURCE])
            or set(groups["resources"]) & set(groups["resource_templates"])
        ):
            raise ValueError("capability_authority_invalid")
        if "runtime_compatibility" in authority:
            _validate_runtime_compatibility(authority["runtime_compatibility"])
            groups["runtime_compatibility"] = authority["runtime_compatibility"]
        projected[app_id] = groups
    return projected, False


def _validate_asset_content(
    asset: PluginAsset,
    payload: bytes,
    manifest: PluginManifest,
) -> tuple[dict[str, dict[str, object]] | None, bool]:
    if asset.kind is AssetKind.SKILL:
        try:
            text = payload.decode("utf-8")
        except UnicodeError:
            raise PackageVerificationError("asset_invalid") from None
        if text.startswith("#!"):
            raise PackageVerificationError("asset_invalid")
        return None, False
    if asset.kind is AssetKind.WORKFLOW or asset.kind is AssetKind.CAPABILITY_AUTHORITY:
        try:
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_json_constant,
            )
            if canonical_bytes(document) != payload:
                raise ValueError("noncanonical_json")
            if asset.kind is AssetKind.CAPABILITY_AUTHORITY:
                projected, legacy = _validate_capability_authority(document, manifest)
                return (None if legacy else projected), legacy
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PackageVerificationError("asset_invalid") from None
        return None, False
    if not payload.startswith(_PNG_SIGNATURE):
        raise PackageVerificationError("asset_invalid")
    return None, False


def _package_root(package_root: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(Path(package_root))))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise PackageVerificationError("package_tree_invalid") from None


def verify_plugin_package(
    package_root: Path,
    key_ring: TrustedPublisherKeyRing,
    omega_version: str,
) -> VerifiedPlugin:
    """Verify one local signed package completely before exposing authenticated payloads."""
    root = _package_root(package_root)
    try:
        tree = PinnedPackageTree.open(root)
    except ValueError as exc:
        reason = str(exc)
        if reason not in {
            "package_tree_invalid",
            "package_file_set_mismatch",
            "package_tree_too_large",
            "package_tree_too_deep",
        }:
            reason = "package_tree_invalid"
        raise PackageVerificationError(reason) from None
    with tree:
        return _verify_pinned_tree(tree, key_ring, omega_version)


def _verify_pinned_tree(
    tree: PinnedPackageTree,
    key_ring: TrustedPublisherKeyRing,
    omega_version: str,
) -> VerifiedPlugin:
    root = tree.root
    files = tree.files
    if tree.total_file_bytes > MAX_PACKAGE_BYTES:
        raise PackageVerificationError("package_too_large")

    manifest_path = root / _MANIFEST_NAME
    manifest_payload = _read_regular_bytes(
        tree,
        manifest_path,
        limit=MAX_MANIFEST_BYTES,
        too_large_reason="manifest_too_large",
    )
    manifest = _parse_manifest(manifest_payload)
    try:
        verify_signed_bytes(
            manifest.signing_bytes(),
            manifest.signature,
            key_ring,
            key_id=manifest.signing_key_id,
            publisher_id=manifest.publisher_id,
            subject_id=manifest.plugin_id,
        )
    except PublisherTrustError as exc:
        raise PackageVerificationError(str(exc)) from None

    try:
        if Version(omega_version) < Version(manifest.minimum_omega_version):
            raise PackageVerificationError("omega_version_incompatible")
    except (InvalidVersion, TypeError):
        raise PackageVerificationError("omega_version_incompatible") from None

    sorted_assets = tuple(sorted(manifest.assets, key=lambda item: item.path))
    portable_asset_ids = tuple(asset.path.casefold() for asset in sorted_assets)
    if len(portable_asset_ids) != len(set(portable_asset_ids)):
        raise PackageVerificationError("manifest_invalid")
    source_paths: dict[str, Path] = {}
    asset_paths: dict[str, Path] = {}
    asset_limits: dict[str, int] = {}
    for asset in sorted_assets:
        asset_limits[asset.path] = _validate_asset_declaration(asset)
        source_paths[asset.path] = _asset_path(root, asset)
        asset_paths[asset.path] = Path(*asset.path.split("/"))
    expected_files = frozenset({manifest_path, *source_paths.values()})
    if files != expected_files:
        raise PackageVerificationError("package_file_set_mismatch")

    total_read = len(manifest_payload)
    asset_payloads: dict[str, bytes] = {}
    capability_authority: dict[str, dict[str, object]] | None = None
    legacy_authority = False
    for asset in sorted_assets:
        payload = _read_regular_bytes(
            tree,
            source_paths[asset.path],
            limit=asset_limits[asset.path],
            too_large_reason="asset_too_large",
        )
        total_read += len(payload)
        if total_read > MAX_PACKAGE_BYTES:
            raise PackageVerificationError("package_too_large")
        if sha256_hex(payload) != asset.sha256:
            raise PackageVerificationError("asset_digest_mismatch")
        projected, legacy = _validate_asset_content(asset, payload, manifest)
        if projected is not None:
            capability_authority = projected
        if legacy:
            legacy_authority = True
        asset_payloads[asset.path] = payload

    try:
        tree.validate_bindings()
    except ValueError:
        raise PackageVerificationError("package_tree_invalid") from None

    manifest_sha256 = sha256_hex(manifest_payload)
    package_sha256 = sha256_hex(
        canonical_bytes(
            {
                "manifest_sha256": manifest_sha256,
                "assets": {asset.path: asset.sha256 for asset in sorted_assets},
            }
        )
    )
    _issue_verified_app_requirements(manifest)
    return VerifiedPlugin(
        manifest=manifest,
        package_root=root,
        manifest_sha256=manifest_sha256,
        package_sha256=package_sha256,
        asset_paths=MappingProxyType(asset_paths),
        asset_bytes=MappingProxyType(asset_payloads),
        capability_authority=(
            None
            if capability_authority is None
            else MappingProxyType(
                {
                    app_id: MappingProxyType(dict(groups))
                    for app_id, groups in capability_authority.items()
                }
            )
        ),
        legacy_authority=legacy_authority,
    )


def verify_locally_sealed_plugin_package(
    package_root: Path,
    trust_root: LocalTrustRoot,
    *,
    external_seal_document: Mapping[str, object] | None = None,
) -> VerifiedPlugin:
    """Verify one locally-sealed publisher-verified package tree completely.

    The manifest carries no usable Ed25519 marketplace signature -- it is
    deliberately unsealed until this device seals it (see
    ``plugin_platform.marketplace_importer``). This function trusts
    ``local_seal.json``'s device-local seal instead, and can never return a
    tier above ``TrustTier.PUBLISHER_VERIFIED``. Omega-verified packages keep
    using :func:`verify_plugin_package` unchanged; this is a separate
    function, not a branch inside it, so neither verification path can widen
    the other's trust conclusion.

    ``external_seal_document`` is for the one caller that cannot keep
    ``local_seal.json`` inside the tree it re-verifies: the durable plugin
    registry materializes a published version from a ``VerifiedPlugin``'s own
    manifest-declared assets only (see ``plugin_platform.registry``), which
    the device-local seal deliberately is not -- it authenticates the package,
    so it is never part of what the package digest covers. When given, the
    tree must carry only ``plugin.json`` and ``tools/authority.json``; the
    seal is taken from this already-shaped mapping instead of being read from
    the tree, and is re-verified exactly as strictly as a tree-resident seal.
    """
    root = _package_root(package_root)
    try:
        tree = PinnedPackageTree.open(root)
    except ValueError as exc:
        reason = str(exc)
        if reason not in {
            "package_tree_invalid",
            "package_file_set_mismatch",
            "package_tree_too_large",
            "package_tree_too_deep",
        }:
            reason = "package_tree_invalid"
        raise PackageVerificationError(reason) from None
    with tree:
        return _verify_locally_sealed_pinned_tree(
            tree, trust_root, external_seal_document=external_seal_document
        )


def _verify_locally_sealed_pinned_tree(
    tree: PinnedPackageTree,
    trust_root: LocalTrustRoot,
    *,
    external_seal_document: Mapping[str, object] | None = None,
) -> VerifiedPlugin:
    root = tree.root
    files = tree.files
    if tree.total_file_bytes > MAX_PACKAGE_BYTES:
        raise PackageVerificationError("package_too_large")

    manifest_path = root / _MANIFEST_NAME
    manifest_payload = _read_regular_bytes(
        tree, manifest_path, limit=MAX_MANIFEST_BYTES, too_large_reason="manifest_too_large"
    )
    manifest = _parse_manifest(manifest_payload)

    if external_seal_document is None:
        seal_path = root / LOCAL_SEAL_NAME
        seal_payload = _read_regular_bytes(
            tree, seal_path, limit=MAX_MANIFEST_BYTES, too_large_reason="manifest_too_large"
        )
        try:
            seal_raw = json.loads(
                seal_payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_json_constant,
            )
            if canonical_bytes(seal_raw) != seal_payload:
                raise PackageVerificationError("local_seal_not_canonical")
        except PackageVerificationError:
            raise
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PackageVerificationError("local_seal_invalid") from None
    else:
        seal_path = None
        if type(external_seal_document) is not dict:
            raise PackageVerificationError("local_seal_invalid")
        seal_raw = external_seal_document

    try:
        seal_document = trust_root.verify_materialized_seal(seal_raw)
    except LocalTrustError as exc:
        raise PackageVerificationError(str(exc)) from None

    if (
        seal_document["trust_tier"] != TrustTier.PUBLISHER_VERIFIED.value
        or seal_document["plugin_id"] != manifest.plugin_id
        or seal_document["publisher_id"] != manifest.publisher_id
        or seal_document["plugin_version"] != manifest.plugin_version
        or seal_document["manifest_sha256"] != sha256_hex(manifest_payload)
    ):
        raise PackageVerificationError("local_seal_identity_mismatch")

    sorted_assets = tuple(sorted(manifest.assets, key=lambda item: item.path))
    if (
        len(sorted_assets) != 1
        or sorted_assets[0].path != "tools/authority.json"
        or sorted_assets[0].kind is not AssetKind.CAPABILITY_AUTHORITY
    ):
        raise PackageVerificationError("manifest_invalid")
    authority_asset = sorted_assets[0]
    _validate_asset_declaration(authority_asset)
    authority_path = _asset_path(root, authority_asset)
    expected_files = (
        frozenset({manifest_path, authority_path})
        if seal_path is None
        else frozenset({manifest_path, authority_path, seal_path})
    )
    if files != expected_files:
        raise PackageVerificationError("package_file_set_mismatch")

    authority_payload = _read_regular_bytes(
        tree, authority_path, limit=MAX_WORKFLOW_BYTES, too_large_reason="asset_too_large"
    )
    if sha256_hex(authority_payload) != authority_asset.sha256:
        raise PackageVerificationError("asset_digest_mismatch")
    try:
        authority_document = json.loads(
            authority_payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
        if canonical_bytes(authority_document) != authority_payload:
            raise PackageVerificationError("asset_invalid")
    except PackageVerificationError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PackageVerificationError("asset_invalid") from None

    sealed_authority = SealedCapabilityAuthority(
        document_sha256=seal_document["authority_document_sha256"],
        mac=seal_document["authority_mac"],
        mac_key_sha256=seal_document["authority_mac_key_sha256"],
    )
    try:
        trust_root.verify_authority(authority_document, sealed_authority)
    except LocalTrustError as exc:
        raise PackageVerificationError(str(exc)) from None

    projected, legacy = _validate_capability_authority(authority_document, manifest)
    if legacy:
        # The retired pre-v2 flat single-app shape is a marketplace Ed25519
        # migration artifact only (see ``_verify_pinned_tree``); the local
        # seal is always freshly written in the v2 ``apps`` shape, so seeing
        # the flat shape here means the tree is tampered or malformed.
        raise PackageVerificationError("capability_authority_invalid")

    try:
        tree.validate_bindings()
    except ValueError:
        raise PackageVerificationError("package_tree_invalid") from None

    manifest_sha256 = sha256_hex(manifest_payload)
    package_sha256 = sha256_hex(
        canonical_bytes(
            {
                "manifest_sha256": manifest_sha256,
                "assets": {authority_asset.path: authority_asset.sha256},
            }
        )
    )
    if package_sha256 != seal_document["package_sha256"]:
        raise PackageVerificationError("local_seal_identity_mismatch")

    _issue_verified_app_requirements(manifest)
    return VerifiedPlugin(
        manifest=manifest,
        package_root=root,
        manifest_sha256=manifest_sha256,
        package_sha256=package_sha256,
        asset_paths=MappingProxyType(
            {authority_asset.path: Path(*authority_asset.path.split("/"))}
        ),
        asset_bytes=MappingProxyType({authority_asset.path: authority_payload}),
        capability_authority=MappingProxyType(
            {app_id: MappingProxyType(dict(groups)) for app_id, groups in projected.items()}
        ),
        trust_tier=TrustTier.PUBLISHER_VERIFIED,
    )
