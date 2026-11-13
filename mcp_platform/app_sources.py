"""Closed trusted source boundary for dormant MCP app provisioning."""

from __future__ import annotations

import json
import threading
import weakref
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

from mcp_platform.canonical import canonical_bytes
from mcp_platform.package_contracts import McpPackageManifest, ResourceCatalog, ToolCatalog
from mcp_platform.package_verifier import VerifiedPackage
from mcp_platform.registry import PackageRegistry
from plugin_platform.contracts import (
    AppDeployment,
    AppRequirement,
    CapabilityKind,
    McpTransport,
)
from plugin_platform.package_verifier import validate_verified_app_requirement


@dataclass(frozen=True, slots=True)
class _SourceProof:
    reference: weakref.ReferenceType[object]
    authority_fingerprint: bytes


_SOURCE_PROOFS_LOCK = threading.Lock()
_SOURCE_PROOFS: dict[int, _SourceProof] = {}


def _issue_source_proof(source: object, authority_fingerprint: bytes) -> None:
    identity = id(source)

    def discard(
        reference: weakref.ReferenceType[object],
        *,
        source_id: int = identity,
    ) -> None:
        with _SOURCE_PROOFS_LOCK:
            proof = _SOURCE_PROOFS.get(source_id)
            if proof is not None and proof.reference is reference:
                _SOURCE_PROOFS.pop(source_id, None)

    reference = weakref.ref(source, discard)
    proof = _SourceProof(
        reference=reference,
        authority_fingerprint=authority_fingerprint,
    )
    with _SOURCE_PROOFS_LOCK:
        _SOURCE_PROOFS[identity] = proof


def _require_source_proof(source: object, authority_fingerprint: bytes) -> None:
    with _SOURCE_PROOFS_LOCK:
        proof = _SOURCE_PROOFS.get(id(source))
    if (
        proof is None
        or proof.reference() is not source
        or proof.authority_fingerprint != authority_fingerprint
    ):
        raise ValueError("mcp_app_source_invalid")


def _canonical_requirement(requirement: AppRequirement) -> AppRequirement:
    if type(requirement) is not AppRequirement:
        raise ValueError("mcp_app_source_invalid")
    try:
        document = requirement.to_dict()
        document["transports"] = sorted(document["transports"])
        document["capabilities"] = sorted(
            document["capabilities"],
            key=lambda item: (
                item["kind"],
                item["name"],
                item["schema_sha256"] or "",
                item["version"] or "",
            ),
        )
        return AppRequirement.from_dict(document)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("mcp_app_source_invalid") from None


def _package_satisfies_requirement(
    package: VerifiedPackage,
    requirement: AppRequirement,
) -> bool:
    tools = {tool.name: tool for tool in package.tool_catalog.tools}
    resources = {
        item.name
        for item in (
            *package.resource_catalog.resources,
            *package.resource_catalog.resource_templates,
        )
    }
    for capability in requirement.capabilities:
        if capability.kind is CapabilityKind.TOOL:
            tool = tools.get(capability.name)
            if tool is None or (
                capability.schema_sha256 is not None
                and capability.schema_sha256 != tool.input_schema_sha256
            ):
                return False
        elif capability.kind is CapabilityKind.RESOURCE:
            if capability.name not in resources or capability.schema_sha256 is not None:
                return False
        else:
            return False
    return True


def _validate_local_source_state(
    requirement: AppRequirement,
    package: VerifiedPackage,
) -> None:
    manifest = package.manifest
    if (
        requirement.deployment is not AppDeployment.LOCAL
        or requirement.transports != (McpTransport.STDIO,)
        or requirement.remote_origin is not None
        or requirement.authorization_issuer is not None
        or requirement.app_id != manifest.server_id
        or requirement.publisher_id != manifest.publisher_id
        or requirement.protocol_version not in manifest.supported_mcp_protocol_versions
        or not _package_satisfies_requirement(package, requirement)
    ):
        raise ValueError("mcp_app_source_invalid")


def _validate_remote_source_state(requirement: AppRequirement) -> None:
    if (
        requirement.deployment is not AppDeployment.REMOTE
        or requirement.transports != (McpTransport.STREAMABLE_HTTP,)
        or requirement.remote_origin is None
        or requirement.authorization_issuer is None
    ):
        raise ValueError("mcp_app_source_invalid")


@dataclass(frozen=True, slots=True)
class _VerifiedPackageSnapshot:
    manifest: bytes
    manifest_sha256: str
    artifact_path: Path
    tool_catalog_path: Path
    resource_catalog_path: Path
    tool_catalog: bytes
    resource_catalog: bytes

    @classmethod
    def from_package(cls, package: VerifiedPackage) -> _VerifiedPackageSnapshot:
        if type(package) is not VerifiedPackage:
            raise ValueError("mcp_app_source_invalid")
        try:
            snapshot = cls(
                manifest=canonical_bytes(package.manifest.to_dict()),
                manifest_sha256=package.manifest_sha256,
                artifact_path=Path(package.artifact_path),
                tool_catalog_path=Path(package.tool_catalog_path),
                resource_catalog_path=Path(package.resource_catalog_path),
                tool_catalog=canonical_bytes(package.tool_catalog.to_dict()),
                resource_catalog=canonical_bytes(package.resource_catalog.to_dict()),
            )
            snapshot.to_package()
            return snapshot
        except (AttributeError, TypeError, ValueError):
            raise ValueError("mcp_app_source_invalid") from None

    def to_package(self) -> VerifiedPackage:
        return VerifiedPackage(
            manifest=McpPackageManifest.from_dict(json.loads(self.manifest)),
            manifest_sha256=self.manifest_sha256,
            artifact_path=Path(self.artifact_path),
            tool_catalog_path=Path(self.tool_catalog_path),
            resource_catalog_path=Path(self.resource_catalog_path),
            tool_catalog=ToolCatalog.from_dict(json.loads(self.tool_catalog)),
            resource_catalog=ResourceCatalog.from_dict(json.loads(self.resource_catalog)),
        )


def _authority_fingerprint(
    requirement: AppRequirement,
    package: _VerifiedPackageSnapshot | None,
) -> bytes:
    components = [
        b"local" if package is not None else b"remote",
        canonical_bytes(requirement.to_dict()),
    ]
    if package is not None:
        components.extend(
            (
                package.manifest,
                package.manifest_sha256.encode("utf-8"),
                str(package.artifact_path).encode("utf-8"),
                str(package.tool_catalog_path).encode("utf-8"),
                str(package.resource_catalog_path).encode("utf-8"),
                package.tool_catalog,
                package.resource_catalog,
            )
        )
    digest = sha256()
    for component in components:
        digest.update(len(component).to_bytes(8, "big"))
        digest.update(component)
    return digest.digest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class VerifiedLocalAppSource:
    """A signed local app requirement bound to one verified package."""

    requirement: AppRequirement
    package: VerifiedPackage

    def __init__(self, *, requirement: AppRequirement, registry: PackageRegistry) -> None:
        try:
            validate_verified_app_requirement(requirement)
            if type(registry) is not PackageRegistry:
                raise ValueError
            canonical = _canonical_requirement(requirement)
            package = registry.reverify_installed(canonical.app_id)
            snapshot = _VerifiedPackageSnapshot.from_package(package)
            package = snapshot.to_package()
        except (AttributeError, TypeError, ValueError):
            raise ValueError("mcp_app_source_invalid")
        except Exception:
            raise ValueError("mcp_app_source_invalid") from None
        _validate_local_source_state(canonical, package)
        object.__setattr__(self, "requirement", canonical)
        object.__setattr__(self, "package", snapshot)
        _issue_source_proof(self, _authority_fingerprint(canonical, snapshot))

    def __getattribute__(self, name: str) -> object:
        value = object.__getattribute__(self, name)
        if name == "package" and type(value) is _VerifiedPackageSnapshot:
            return value.to_package()
        return value


@dataclass(frozen=True, slots=True, weakref_slot=True)
class TrustedRemoteAppSource:
    """A remote app whose complete endpoint identity is its signed requirement."""

    requirement: AppRequirement

    def __post_init__(self) -> None:
        try:
            validate_verified_app_requirement(self.requirement)
        except (TypeError, ValueError):
            raise ValueError("mcp_app_source_invalid") from None
        requirement = _canonical_requirement(self.requirement)
        _validate_remote_source_state(requirement)
        object.__setattr__(self, "requirement", requirement)
        _issue_source_proof(self, _authority_fingerprint(requirement, None))


def _validated_source_state(
    source: object,
) -> tuple[AppRequirement, VerifiedPackage | None]:
    """Reconstruct authority only from an exact constructor-issued source."""
    try:
        if type(source) is VerifiedLocalAppSource:
            requirement = _canonical_requirement(source.requirement)
            snapshot = _VerifiedPackageSnapshot.from_package(source.package)
            package = snapshot.to_package()
            _validate_local_source_state(requirement, package)
            _require_source_proof(
                source,
                _authority_fingerprint(requirement, snapshot),
            )
            return requirement, package
        if type(source) is TrustedRemoteAppSource:
            requirement = _canonical_requirement(source.requirement)
            _validate_remote_source_state(requirement)
            _require_source_proof(
                source,
                _authority_fingerprint(requirement, None),
            )
            return requirement, None
    except (AttributeError, TypeError, ValueError):
        raise ValueError("mcp_app_source_invalid") from None
    raise ValueError("mcp_app_source_invalid")


McpAppSource = VerifiedLocalAppSource | TrustedRemoteAppSource


@runtime_checkable
class McpAppSourceResolver(Protocol):
    """Resolve one signed requirement to one closed trusted app source."""

    def resolve(self, requirement: AppRequirement) -> McpAppSource: ...
