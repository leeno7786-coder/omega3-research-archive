"""Offline-only evidence for replacing pre-authentication plugin installations.

This does not issue VerifiedPlugin or admit manifest v1 to runtime. Only a
current, normally verified successor supplies app identity and authority.
"""
from __future__ import annotations

import json
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from packaging.version import Version

from mcp_platform.app_registry import (
    InstalledMcpAppRecord,
    _record_from_durable,
    _validated_record,
)
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.contracts import PluginAsset
from plugin_platform.filesystem import PinnedPackageTree
from plugin_platform.package_verifier import (
    MAX_MANIFEST_BYTES, MAX_PACKAGE_BYTES, VerifiedPlugin,
    verify_plugin_package,
)
from plugin_platform.records import InstalledPluginVersionRecord
from plugin_platform.trust import TrustedPublisherKeyRing, verify_signed_bytes

_MANIFEST_V1 = frozenset({
    "manifest_version", "plugin_id", "publisher_id", "plugin_version",
    "minimum_omega_version", "kind", "title", "description", "assets",
    "entry_points", "starter_requests", "required_apps", "optional_apps",
    "schedule_template_paths", "proactive_event_rules", "enterprise_roles",
    "signing_key_id", "signature",
})
_APP_V1 = frozenset({
    "app_id", "publisher_id", "deployment", "provider_id", "protocol_version",
    "transports", "remote_origin", "authorization_issuer", "capabilities",
})
_RECORD_EXTRA = frozenset({
    "local_package_version", "local_package_platform", "local_package_manifest_sha256",
    "installed", "trusted", "enabled",
})
_INVALID = "plugin_legacy_recovery_invalid"
_OMEGA_VERSION = "3.0.0"


def _json(payload: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(_INVALID)
            result[key] = value
        return result

    value = json.loads(payload, object_pairs_hook=unique)
    if type(value) is not dict or canonical_bytes(value) != payload:
        raise ValueError(_INVALID)
    return value


@dataclass(frozen=True, slots=True, weakref_slot=True)
class LegacySuccessorProof:
    old_manifest_sha256: str
    old_record_bytes: bytes
    replacement_record: InstalledMcpAppRecord
    payloads: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class _LegacySuccessorSnapshot:
    old_manifest_sha256: str
    old_record_bytes: bytes
    replacement_record_bytes: bytes
    payloads: tuple[tuple[str, bytes], ...]


_PROOF_LOCK = threading.Lock()
_ISSUED_PROOFS: dict[
    int,
    tuple[weakref.ReferenceType[LegacySuccessorProof], _LegacySuccessorSnapshot],
] = {}


def _proof_snapshot(proof: object) -> _LegacySuccessorSnapshot:
    if (
        type(proof) is not LegacySuccessorProof
        or type(proof.old_manifest_sha256) is not str
        or type(proof.old_record_bytes) is not bytes
        or type(proof.payloads) is not MappingProxyType
    ):
        raise ValueError(_INVALID)
    replacement = _validated_record(proof.replacement_record)
    payloads = tuple(sorted(proof.payloads.items()))
    if any(type(name) is not str or type(payload) is not bytes for name, payload in payloads):
        raise ValueError(_INVALID)
    return _LegacySuccessorSnapshot(
        old_manifest_sha256=proof.old_manifest_sha256,
        old_record_bytes=proof.old_record_bytes,
        replacement_record_bytes=canonical_bytes(replacement.to_dict()),
        payloads=payloads,
    )


def _issue_proof(proof: LegacySuccessorProof) -> None:
    snapshot = _proof_snapshot(proof)
    identity = id(proof)

    def discard(
        reference: weakref.ReferenceType[LegacySuccessorProof],
        *,
        proof_id: int = identity,
    ) -> None:
        with _PROOF_LOCK:
            current = _ISSUED_PROOFS.get(proof_id)
            if current is not None and current[0] is reference:
                _ISSUED_PROOFS.pop(proof_id, None)

    reference = weakref.ref(proof, discard)
    with _PROOF_LOCK:
        current = _ISSUED_PROOFS.get(identity)
        if current is not None and current[0]() is not None:
            raise ValueError(_INVALID)
        _ISSUED_PROOFS[identity] = (reference, snapshot)


def validated_upgrade(
    proof: LegacySuccessorProof,
) -> tuple[bytes, InstalledMcpAppRecord]:
    """Return only the immutable upgrade snapshot issued for this exact proof."""

    try:
        if type(proof) is not LegacySuccessorProof:
            raise ValueError(_INVALID)
        with _PROOF_LOCK:
            issued = _ISSUED_PROOFS.get(id(proof))
            if issued is None or issued[0]() is not proof:
                raise ValueError(_INVALID)
            snapshot = issued[1]
            if _proof_snapshot(proof) != snapshot:
                raise ValueError(_INVALID)
        raw = _json(snapshot.replacement_record_bytes)
        app_id = raw.get("app_id")
        if type(app_id) is not str:
            raise ValueError(_INVALID)
        replacement = _record_from_durable(raw, registry_key=app_id)
        return snapshot.old_record_bytes, replacement
    except Exception as exc:
        raise ValueError(_INVALID) from exc


def read_legacy_package(
    package: Path,
    version: InstalledPluginVersionRecord,
    ring: TrustedPublisherKeyRing,
) -> tuple[dict, dict[str, bytes]]:
    """Authenticate archival bytes only; never return executable authority."""
    try:
        version = InstalledPluginVersionRecord.from_dict(version.to_dict())
        with PinnedPackageTree.open(package) as tree:
            if tree.total_file_bytes > MAX_PACKAGE_BYTES:
                raise ValueError(_INVALID)
            encoded = tree.read_bytes("plugin.json", limit=MAX_MANIFEST_BYTES)
            raw = _json(encoded)
            if (
                set(raw) != _MANIFEST_V1 or type(raw["manifest_version"]) is not int
                or raw["manifest_version"] != 1 or raw["kind"] != "mcp_backed"
                or raw["publisher_id"] != "omega.first_party"
                or raw["plugin_version"] != version.plugin_version
                or raw["signing_key_id"] != version.signing_key_id
                or sha256_hex(encoded) != version.manifest_sha256
                or type(raw["required_apps"]) is not list or len(raw["required_apps"]) != 1
                or raw["optional_apps"] != []
                or set(raw["required_apps"][0]) != _APP_V1
                or raw["required_apps"][0]["deployment"] != "remote"
            ):
                raise ValueError(_INVALID)
            verify_signed_bytes(
                canonical_bytes({k: v for k, v in raw.items() if k != "signature"}),
                raw["signature"], ring, key_id=raw["signing_key_id"],
                publisher_id=raw["publisher_id"], subject_id=raw["plugin_id"],
            )
            assets = tuple(PluginAsset.from_dict(a) for a in raw["assets"])
            names = [a.path for a in assets]
            if len(set(n.casefold() for n in names)) != len(names):
                raise ValueError(_INVALID)
            expected = {tree.root / "plugin.json", *(tree.root / name for name in names)}
            if tree.files != frozenset(expected):
                raise ValueError(_INVALID)
            payloads = {"plugin.json": encoded}
            for asset in assets:
                payload = tree.read_bytes(asset.path, limit=MAX_PACKAGE_BYTES)
                if sha256_hex(payload) != asset.sha256:
                    raise ValueError(_INVALID)
                payloads[asset.path] = payload
            if sha256_hex(canonical_bytes({
                "manifest_sha256": version.manifest_sha256,
                "assets": {a.path: a.sha256 for a in assets},
            })) != version.package_sha256:
                raise ValueError(_INVALID)
            authority = _json(payloads["tools/authority.json"])
            if set(authority) != {"oauth_scopes", "tools"}:
                raise ValueError(_INVALID)
            tree.validate_bindings()
            return raw, payloads
    except Exception as exc:
        raise ValueError(_INVALID) from exc


def verify_legacy_successor(
    package: Path,
    version: InstalledPluginVersionRecord,
    app_record: dict,
    successor: VerifiedPlugin,
    ring: TrustedPublisherKeyRing,
) -> LegacySuccessorProof:
    """Prove an exact no-permission-change dormant upgrade from signed evidence."""
    try:
        old, payloads = read_legacy_package(package, version, ring)
        if type(successor) is not VerifiedPlugin:
            raise ValueError(_INVALID)
        verified_successor = verify_plugin_package(
            successor.package_root,
            ring,
            _OMEGA_VERSION,
        )
        current = verified_successor.manifest
        if (
            len(current.required_apps) != 1 or current.optional_apps
            or current.plugin_id != old["plugin_id"]
            or current.publisher_id != old["publisher_id"]
            or current.kind.value != old["kind"]
            or Version(current.plugin_version) <= Version(old["plugin_version"])
            or verified_successor.legacy_authority
        ):
            raise ValueError(_INVALID)
        requirement = current.required_apps[0]
        # This constructor demands the current verifier's issued requirement.
        replacement = InstalledMcpAppRecord.from_source(TrustedRemoteAppSource(requirement))
        old_app = old["required_apps"][0]
        new_app = requirement.to_dict()
        if (
            type(app_record) is not dict or set(app_record) != _APP_V1 | _RECORD_EXTRA
            or {k: app_record[k] for k in _APP_V1} != old_app
            or {k: new_app[k] for k in _APP_V1} != old_app
            or app_record["installed"] is not True or app_record["trusted"] is not True
            or app_record["enabled"] is not False
            or any(app_record[k] is not None for k in (
                "local_package_version", "local_package_platform", "local_package_manifest_sha256"
            ))
            or requirement.authentication.kind.value not in {
                "oauth_device_code", "oauth_authorization_code_pkce"
            }
            or requirement.authentication.secret_fields
        ):
            raise ValueError(_INVALID)
        old_authority = _json(payloads["tools/authority.json"])
        scopes = old_authority["oauth_scopes"]
        new_authority = verified_successor.capability_authority[requirement.app_id]
        if (
            type(scopes) is not list or any(type(s) is not str for s in scopes)
            or len(scopes) != len(set(scopes))
            or set(scopes) != set(requirement.authentication.scopes)
            or old_authority["tools"] != new_authority["tools"]
            or any(new_authority[k] for k in ("resources", "resource_templates", "prompts"))
        ):
            raise ValueError(_INVALID)
        proof = LegacySuccessorProof(
            version.manifest_sha256, canonical_bytes(app_record), replacement,
            MappingProxyType(dict(payloads)),
        )
        _issue_proof(proof)
        return proof
    except Exception as exc:
        raise ValueError(_INVALID) from exc
