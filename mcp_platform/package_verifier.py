"""Fail-closed offline verification for first-party MCP packages."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sys
import threading
import weakref
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import InvalidVersion, Version

from mcp_platform.artifact_guard import descriptor_file_identity
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import (
    MCP_PROTOCOL_VERSION,
    McpPackageManifest,
    ResourceCatalog,
    ToolCatalog,
)
from mcp_platform.trust_store import KeyRingLookupError, TrustedKeyRing

_FIRST_PARTY_PUBLISHER = "omega.first_party"
_MANIFEST_NAME = "manifest.json"
_TOOL_CATALOG_NAME = "tool_catalog.json"
_RESOURCE_CATALOG_NAME = "resource_catalog.json"


@dataclass(frozen=True, slots=True)
class PackageVerificationLimits:
    """Pre-trust byte ceilings for every signed package input."""

    max_manifest_bytes: int = 1_048_576
    max_tool_catalog_bytes: int = 4_194_304
    max_resource_catalog_bytes: int = 4_194_304
    max_artifact_bytes: int = 268_435_456
    hash_chunk_bytes: int = 65_536

    def __post_init__(self) -> None:
        for name in (
            "max_manifest_bytes",
            "max_tool_catalog_bytes",
            "max_resource_catalog_bytes",
            "max_artifact_bytes",
            "hash_chunk_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_PACKAGE_VERIFICATION_LIMITS = PackageVerificationLimits()


class PackageVerificationError(Exception):
    """A stable, content-free package rejection reason."""


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class VerifiedPackage:
    """Immutable verified package identity and its signed declarations."""

    manifest: McpPackageManifest
    manifest_sha256: str
    artifact_path: Path
    tool_catalog_path: Path
    resource_catalog_path: Path
    tool_catalog: ToolCatalog
    resource_catalog: ResourceCatalog
    artifact_identity: tuple[int, int] = (0, 0)


_ISSUED_PACKAGES_LOCK = threading.Lock()
_ISSUED_PACKAGES: dict[int, tuple[weakref.ReferenceType[VerifiedPackage], str]] = {}


def _package_fingerprint(package: VerifiedPackage) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "manifest": package.manifest.to_dict(),
                "manifest_sha256": package.manifest_sha256,
                "artifact_path": str(package.artifact_path),
                "tool_catalog_path": str(package.tool_catalog_path),
                "resource_catalog_path": str(package.resource_catalog_path),
                "tool_catalog": package.tool_catalog.to_dict(),
                "resource_catalog": package.resource_catalog.to_dict(),
                "artifact_identity": [str(value) for value in package.artifact_identity],
            }
        )
    )


def _issue_verified_package(**fields: Any) -> VerifiedPackage:
    package = VerifiedPackage(**fields)
    package_id = id(package)

    def forget(reference: weakref.ReferenceType[VerifiedPackage]) -> None:
        with _ISSUED_PACKAGES_LOCK:
            retained = _ISSUED_PACKAGES.get(package_id)
            if retained is not None and retained[0] is reference:
                _ISSUED_PACKAGES.pop(package_id, None)

    reference = weakref.ref(package, forget)
    fingerprint = _package_fingerprint(package)
    with _ISSUED_PACKAGES_LOCK:
        _ISSUED_PACKAGES[package_id] = (reference, fingerprint)
    return package


def _is_verifier_issued_package(package: object) -> bool:
    """Return whether this exact, still-immutable object was issued by this verifier."""
    if type(package) is not VerifiedPackage:
        return False
    with _ISSUED_PACKAGES_LOCK:
        retained = _ISSUED_PACKAGES.get(id(package))
    if retained is None or retained[0]() is not package:
        return False
    try:
        return retained[1] == _package_fingerprint(package)
    except (AttributeError, TypeError, ValueError):
        return False


def current_platform_tag() -> str:
    """Return Omega's closed operating-system and architecture tag."""
    operating_system = {
        "win32": "windows",
        "linux": "linux",
        "darwin": "macos",
    }.get(sys.platform)
    machine = platform.machine().lower()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if operating_system is None or architecture is None:
        raise PackageVerificationError("unsupported_platform")
    return f"{operating_system}-{architecture}"


def manifest_signing_bytes(manifest: McpPackageManifest) -> bytes:
    """Return RFC 8785 bytes for the signature-omitted manifest."""
    document = manifest.to_dict()
    document.pop("signature")
    return canonical_bytes(document)


def verify_signature(manifest: McpPackageManifest, key_ring: TrustedKeyRing) -> None:
    """Verify the manifest against the exact embedded-key identity."""
    try:
        raw_public = key_ring.public_key_bytes(manifest.signing_key_id, manifest.server_id)
    except KeyRingLookupError as exc:
        raise PackageVerificationError(str(exc)) from None
    try:
        signature = base64.b64decode(manifest.signature, validate=True)
        Ed25519PublicKey.from_public_bytes(raw_public).verify(
            signature,
            manifest_signing_bytes(manifest),
        )
    except (TypeError, ValueError, InvalidSignature):
        raise PackageVerificationError("invalid_manifest_signature") from None


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON field")
        document[key] = value
    return document


def _read_bounded_file(path: Path, *, limit: int, reason: str) -> bytes:
    try:
        with path.open("rb") as source:
            initial = os.fstat(source.fileno())
            identity = descriptor_file_identity(source.fileno())
            if initial.st_size > limit:
                raise PackageVerificationError("package_file_limit_exceeded")
            payload = bytearray()
            while True:
                remaining = limit + 1 - len(payload)
                if remaining <= 0:
                    raise PackageVerificationError("package_file_limit_exceeded")
                chunk = source.read(min(65_536, remaining))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > limit:
                    raise PackageVerificationError("package_file_limit_exceeded")
            final = os.fstat(source.fileno())
            if (
                final.st_size != initial.st_size
                or final.st_mtime_ns != initial.st_mtime_ns
                or descriptor_file_identity(source.fileno()) != identity
            ):
                raise OSError
        return bytes(payload)
    except PackageVerificationError:
        raise
    except OSError:
        raise PackageVerificationError(reason) from None


def _load_json(path: Path, *, reason: str, limit: int) -> Any:
    try:
        return json.loads(
            _read_bounded_file(path, limit=limit, reason=reason).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except PackageVerificationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise PackageVerificationError(reason) from None


def _parse_manifest(raw: Any) -> McpPackageManifest:
    try:
        return McpPackageManifest.from_dict(raw)
    except ValueError as original_error:
        message = str(original_error)
        if "entrypoint.artifact must be a relative package path" in message:
            raise PackageVerificationError("entrypoint_escape") from None

        # The Task 1 contract rejects unsupported protocols. Construct a checked
        # interim value only so verification can authenticate the original bytes
        # before applying the protocol gate in its required order.
        if type(raw) is dict and "supported_mcp_protocol_versions" in raw:
            protocols = raw["supported_mcp_protocol_versions"]
            valid_protocol_shape = (
                type(protocols) is list
                and bool(protocols)
                and all(
                    type(item) is str
                    and bool(item)
                    and item.strip() == item
                    and not any(ord(character) < 32 or ord(character) == 127 for character in item)
                    for item in protocols
                )
                and len(protocols) == len(set(protocols))
            )
            if valid_protocol_shape and protocols != [MCP_PROTOCOL_VERSION]:
                surrogate = dict(raw)
                surrogate["supported_mcp_protocol_versions"] = [MCP_PROTOCOL_VERSION]
                try:
                    parsed = McpPackageManifest.from_dict(surrogate)
                except ValueError:
                    pass
                else:
                    return replace(
                        parsed,
                        supported_mcp_protocol_versions=tuple(protocols),
                    )
        raise PackageVerificationError("invalid_package_manifest") from None


def _safe_package_file(package_root: Path, relative_path: str) -> Path:
    try:
        candidate = (package_root / relative_path).resolve(strict=True)
        candidate.relative_to(package_root)
    except (OSError, RuntimeError, ValueError):
        raise PackageVerificationError("entrypoint_escape") from None
    if not candidate.is_file():
        raise PackageVerificationError("entrypoint_escape")
    return candidate


def _canonical_catalog_document(
    path: Path,
    *,
    mismatch_reason: str,
    limit: int,
) -> tuple[Any, bytes]:
    document = _load_json(path, reason=mismatch_reason, limit=limit)
    try:
        canonical = canonical_bytes(document)
    except ValueError:
        raise PackageVerificationError(mismatch_reason) from None
    return document, canonical


def verify_package(
    package_dir: Path,
    key_ring: TrustedKeyRing,
    omega_version: str,
    platform_tag: str,
    *,
    limits: PackageVerificationLimits = DEFAULT_PACKAGE_VERIFICATION_LIMITS,
) -> VerifiedPackage:
    """Verify a package completely offline before returning its identity."""
    if type(limits) is not PackageVerificationLimits:
        raise ValueError("package_verification_limits_invalid")
    try:
        package_root = Path(package_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        raise PackageVerificationError("invalid_package_manifest") from None
    if not package_root.is_dir():
        raise PackageVerificationError("invalid_package_manifest")

    raw_manifest = _load_json(
        package_root / _MANIFEST_NAME,
        reason="invalid_package_manifest",
        limit=limits.max_manifest_bytes,
    )
    manifest = _parse_manifest(raw_manifest)
    if manifest.publisher_id != _FIRST_PARTY_PUBLISHER:
        raise PackageVerificationError("publisher_not_first_party")

    verify_signature(manifest, key_ring)

    try:
        if Version(manifest.minimum_omega_version) > Version(omega_version):
            raise PackageVerificationError("minimum_omega_version_not_met")
    except InvalidVersion:
        raise PackageVerificationError("minimum_omega_version_not_met") from None
    if manifest.platform != platform_tag:
        raise PackageVerificationError("unsupported_platform")
    if manifest.supported_mcp_protocol_versions != (MCP_PROTOCOL_VERSION,):
        raise PackageVerificationError("unsupported_mcp_protocol")

    artifact_path = _safe_package_file(package_root, manifest.artifact)
    try:
        with artifact_path.open("rb") as artifact_file:
            artifact_stat = os.fstat(artifact_file.fileno())
            artifact_identity = descriptor_file_identity(artifact_file.fileno())
            if artifact_stat.st_size > limits.max_artifact_bytes:
                raise PackageVerificationError("package_file_limit_exceeded")
            digest = hashlib.sha256()
            observed_size = 0
            while True:
                remaining = limits.max_artifact_bytes + 1 - observed_size
                if remaining <= 0:
                    raise PackageVerificationError("package_file_limit_exceeded")
                chunk = artifact_file.read(min(limits.hash_chunk_bytes, remaining))
                if not chunk:
                    break
                observed_size += len(chunk)
                if observed_size > limits.max_artifact_bytes:
                    raise PackageVerificationError("package_file_limit_exceeded")
                digest.update(chunk)
            final_stat = os.fstat(artifact_file.fileno())
            if (
                final_stat.st_size != artifact_stat.st_size
                or final_stat.st_mtime_ns != artifact_stat.st_mtime_ns
                or descriptor_file_identity(artifact_file.fileno()) != artifact_identity
            ):
                raise OSError
        artifact_digest = digest.hexdigest()
    except PackageVerificationError:
        raise
    except OSError:
        raise PackageVerificationError("artifact_digest_mismatch") from None
    if artifact_digest != manifest.artifact_sha256:
        raise PackageVerificationError("artifact_digest_mismatch")

    tool_catalog_path = _safe_package_file(package_root, _TOOL_CATALOG_NAME)
    tool_document, tool_bytes = _canonical_catalog_document(
        tool_catalog_path,
        mismatch_reason="tool_catalog_digest_mismatch",
        limit=limits.max_tool_catalog_bytes,
    )
    if sha256_hex(tool_bytes) != manifest.tool_catalog_sha256:
        raise PackageVerificationError("tool_catalog_digest_mismatch")

    resource_catalog_path = _safe_package_file(package_root, _RESOURCE_CATALOG_NAME)
    resource_document, resource_bytes = _canonical_catalog_document(
        resource_catalog_path,
        mismatch_reason="resource_catalog_digest_mismatch",
        limit=limits.max_resource_catalog_bytes,
    )
    if sha256_hex(resource_bytes) != manifest.resource_catalog_sha256:
        raise PackageVerificationError("resource_catalog_digest_mismatch")

    try:
        tool_catalog = ToolCatalog.from_dict(tool_document)
        resource_catalog = ResourceCatalog.from_dict(resource_document)
    except ValueError:
        raise PackageVerificationError("catalog_identity_mismatch") from None
    expected_identity = (manifest.server_id, manifest.server_version)
    if (tool_catalog.server_id, tool_catalog.server_version) != expected_identity or (
        resource_catalog.server_id,
        resource_catalog.server_version,
    ) != expected_identity:
        raise PackageVerificationError("catalog_identity_mismatch")

    return _issue_verified_package(
        manifest=manifest,
        manifest_sha256=sha256_hex(canonical_bytes(manifest.to_dict())),
        artifact_path=artifact_path,
        tool_catalog_path=tool_catalog_path,
        resource_catalog_path=resource_catalog_path,
        tool_catalog=tool_catalog,
        resource_catalog=resource_catalog,
        artifact_identity=artifact_identity,
    )
