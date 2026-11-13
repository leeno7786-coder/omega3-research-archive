"""Device-local trust root that seals publisher-verified packages and
discovered capability authority.

Omega does not review a publisher-verified or personal plugin's behavior.
The upstream registry only authenticates a namespace (``marketplace_importer``);
the operator's own review of a personal plugin never becomes an Omega
endorsement. This module is the boundary where "this device pinned and
reviewed this exact package/authority" becomes a fact a later install or
connection step can trust -- without ever claiming Omega verification. A
sealed document's trust tier is always exactly ``publisher_verified``; this
module has no way to produce ``omega_verified``.

One portable vault key derives two independent secrets with HKDF-SHA256:
an Ed25519 seed that seals packages (label ``omega/plugin-package-seal/v1``)
and an HMAC key that seals discovered capability authority (label
``omega/plugin-authority-seal/v1``, consumed by
``mcp_platform.plugin_discovery``). Both secrets are reconstructable on
demand from the vault key and are never themselves written to disk by this
module; only public verification metadata (a public-key digest) and sealed
documents are meant to persist. ``lock()`` discards both derived secrets from
process memory; every sealing and verification operation fails closed
afterward.

Architecture note: this module may import only the standard library, `core`,
`plugin_platform`, and `mcp_platform.canonical` -- it must never reach into
the network, execution, or product layers. `tests/plugin_platform/test_architecture.py`
enforces that boundary for every module under `plugin_platform/`.
"""

from __future__ import annotations

import base64
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.catalog import DistributionKind, TrustTier
from plugin_platform.contracts import (
    AssetKind,
    PluginAsset,
    PluginManifest,
    _digest,
    _identifier,
    _semver,
    _stable_string,
)
from plugin_platform.filesystem import PinnedDirectory
from plugin_platform.marketplace_importer import VerifiedNormalizedPluginCandidate

_PACKAGE_SEAL_INFO = b"omega/plugin-package-seal/v1"
_AUTHORITY_SEAL_INFO = b"omega/plugin-authority-seal/v1"
#: A distinct, explicitly non-secret identifier for the authority MAC key,
#: derived from the vault key on its own independent HKDF branch. Persisted
#: (``mac_key_sha256``/``authority_mac_key_sha256``) so a verifier can name
#: which key sealed a document -- never computed as a hash of the live HMAC
#: secret itself, which would publish a value derived directly from key
#: material used to authenticate every sealed document.
_AUTHORITY_SEAL_ID_INFO = b"omega/plugin-authority-seal-id/v1"
_PACKAGE_SEAL_DOMAIN = "omega.local-trust.package-seal.v1"
_AUTHORITY_SEAL_DOMAIN = "omega.local-trust.authority-seal.v1"
_MATERIALIZED_SEAL_DOMAIN = "omega.local-trust.materialized-seal.v1"
_VAULT_KEY_BYTES = 32
_DERIVED_KEY_BYTES = 32
_ED25519_SIGNATURE_BYTES = 64
_MAC_BYTES = 32
_AUTHORITY_ASSET_PATH = "tools/authority.json"
_AUTHORITY_SCHEMA_VERSION = 2
LOCAL_SEAL_NAME = "local_seal.json"
MANIFEST_NAME = "plugin.json"
_MATERIALIZED_SEAL_FIELDS = frozenset(
    {
        "domain",
        "plugin_id",
        "publisher_id",
        "plugin_version",
        "trust_tier",
        "manifest_sha256",
        "package_sha256",
        "authority_document_sha256",
        "authority_mac",
        "authority_mac_key_sha256",
        "source_evidence_sha256",
    }
)


class LocalTrustError(Exception):
    """A stable, content-free local-trust rejection reason."""


def _hkdf(vault_key: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_DERIVED_KEY_BYTES,
        salt=None,
        info=info,
    ).derive(vault_key)


def _canonical_base64(value: object) -> bytes:
    try:
        if type(value) is not str:
            raise ValueError("not_base64")
        decoded = base64.b64decode(value, validate=True)
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("not_canonical_base64")
    except (TypeError, ValueError):
        raise LocalTrustError("seal_invalid") from None
    return decoded


@dataclass(frozen=True, slots=True)
class VerifiedPlugin:
    """One locally sealed publisher-verified (or personal) package.

    Every field is re-validated on construction so a forged or corrupted
    instance can never reach ``LocalTrustRoot.verify_package`` as if it were
    real; the ``domain``-prefixed canonical form used to sign and re-verify
    the seal is built entirely from these already-validated fields.
    """

    plugin_id: str
    publisher_id: str
    plugin_version: str
    trust_tier: TrustTier
    distribution: str
    manifest_sha256: str
    package_sha256: str
    artifact_sha256: str | None
    seal_public_key_sha256: str
    signature: str

    def __post_init__(self) -> None:
        try:
            plugin_id = _identifier(self.plugin_id, field="plugin_id")
            publisher_id = _identifier(self.publisher_id, field="publisher_id")
            plugin_version = _semver(self.plugin_version, field="plugin_version")
            trust_tier = TrustTier(self.trust_tier)
            distribution = DistributionKind(self.distribution)
            manifest_sha256 = _digest(self.manifest_sha256, field="manifest_sha256")
            package_sha256 = _digest(self.package_sha256, field="package_sha256")
            artifact_sha256 = (
                None
                if self.artifact_sha256 is None
                else _digest(self.artifact_sha256, field="artifact_sha256")
            )
            seal_public_key_sha256 = _digest(
                self.seal_public_key_sha256, field="seal_public_key_sha256"
            )
            signature = _stable_string(self.signature, field="signature")
            _canonical_base64(signature)
        except (TypeError, ValueError):
            raise LocalTrustError("seal_invalid") from None
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "publisher_id", publisher_id)
        object.__setattr__(self, "plugin_version", plugin_version)
        object.__setattr__(self, "trust_tier", trust_tier)
        object.__setattr__(self, "distribution", distribution.value)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "package_sha256", package_sha256)
        object.__setattr__(self, "artifact_sha256", artifact_sha256)
        object.__setattr__(self, "seal_public_key_sha256", seal_public_key_sha256)
        object.__setattr__(self, "signature", signature)

    def _seal_document(self) -> dict[str, object]:
        return {
            "domain": _PACKAGE_SEAL_DOMAIN,
            "plugin_id": self.plugin_id,
            "publisher_id": self.publisher_id,
            "plugin_version": self.plugin_version,
            "trust_tier": self.trust_tier.value,
            "distribution": self.distribution,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True, slots=True)
class SealedCapabilityAuthority:
    """Public verification metadata and MAC for one sealed authority document."""

    document_sha256: str
    mac: str
    mac_key_sha256: str


@dataclass(frozen=True, slots=True)
class SealedPackageTree:
    """The exact files ``materialize_sealed_package`` wrote under one staging root."""

    root: Path
    plugin_id: str
    publisher_id: str
    plugin_version: str
    manifest_sha256: str
    package_sha256: str


def _package_seal_document(candidate: VerifiedNormalizedPluginCandidate) -> dict[str, object]:
    """Return the exact facts this seal binds, re-derived from the verified candidate.

    Nothing here is taken from a caller-supplied field: identity comes from
    the manifest the importer's own ``verify`` already re-proved, and the
    artifact digest (if any) comes from the candidate's own pinned evidence.
    """

    if type(candidate) is not VerifiedNormalizedPluginCandidate:
        raise LocalTrustError("candidate_unverified")
    if candidate.trust_tier is not TrustTier.PUBLISHER_VERIFIED:
        raise LocalTrustError("candidate_trust_tier_unsupported")
    inner = candidate.candidate
    manifest = candidate.manifest
    try:
        distribution = DistributionKind(inner.distribution)
    except (TypeError, ValueError):
        raise LocalTrustError("candidate_unverified") from None
    artifact = inner.artifact
    artifact_sha256 = None if artifact is None else _digest(artifact.sha256, field="artifact_sha256")
    manifest_sha256 = sha256_hex(manifest.signing_bytes())
    package_sha256 = sha256_hex(
        canonical_bytes(
            {
                "manifest_sha256": manifest_sha256,
                "artifact_sha256": artifact_sha256,
            }
        )
    )
    return {
        "domain": _PACKAGE_SEAL_DOMAIN,
        "plugin_id": manifest.plugin_id,
        "publisher_id": manifest.publisher_id,
        "plugin_version": manifest.plugin_version,
        "trust_tier": candidate.trust_tier.value,
        "distribution": distribution.value,
        "manifest_sha256": manifest_sha256,
        "package_sha256": package_sha256,
        "artifact_sha256": artifact_sha256,
    }


def _source_evidence_sha256(candidate: VerifiedNormalizedPluginCandidate) -> str:
    """Digest exactly the registry facts a materialized seal binds to."""

    evidence = candidate.candidate.source_evidence
    return sha256_hex(
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


def _envelope_capability_authority(envelope: object) -> dict[str, object]:
    authority = getattr(envelope, "capability_authority", None)
    if not isinstance(authority, Mapping):
        raise LocalTrustError("envelope_invalid")
    plain = _plain(authority)
    if type(plain) is not dict:
        raise LocalTrustError("envelope_invalid")
    return plain


def _authority_document(plugin_id: str, app_id: str, groups: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": _AUTHORITY_SCHEMA_VERSION,
        "plugin_id": plugin_id,
        "apps": {app_id: groups},
    }


_GROUP_CAPABILITY_KIND = {
    "tools": "tool",
    "resources": "resource",
    "resource_templates": "resource",
    "prompts": "prompt",
}


def _capability_requirements(groups: dict[str, object]) -> list[dict[str, object]]:
    """Declare exactly the approved capabilities the sealed authority lists.

    The signed capability-authority validator only admits a capability whose
    name/kind the manifest's own app requirement already declares -- so a
    materialized manifest must declare precisely what was discovered and
    approved, nothing more and nothing the operator never scoped.

    A tool policy (``mcp_platform.plugin_discovery._sealed_tool_policy``)
    carries no generic ``schema_sha256`` field -- only ``input_schema_sha256``/
    ``output_schema_sha256``, matching the signed-authority compile path's
    own convention (``mcp_platform.plugin_catalog.compile_plugin_tool_bindings``
    cross-checks a declared tool capability's ``schema_sha256`` against the
    *input* schema digest alone). Every other kind keeps today's plain
    ``schema_sha256`` field.
    """

    requirements: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for group_name, kind in _GROUP_CAPABILITY_KIND.items():
        policies = groups.get(group_name)
        if not isinstance(policies, Mapping):
            continue
        for name, policy in policies.items():
            key = (kind, str(name))
            if key in seen:
                continue
            seen.add(key)
            schema_sha256 = None
            if isinstance(policy, Mapping):
                candidate_schema = (
                    policy.get("input_schema_sha256")
                    if kind == "tool"
                    else policy.get("schema_sha256")
                )
                if type(candidate_schema) is str:
                    schema_sha256 = candidate_schema
            requirements.append(
                {
                    "kind": kind,
                    "name": name,
                    "schema_sha256": schema_sha256,
                    "version": None,
                }
            )
    return requirements


def _plain(value: object) -> object:
    """Recursively lower ``Mapping``/list-like values to plain JSON containers.

    A capability-authority document built by ``mcp_platform.plugin_discovery``
    nests read-only ``MappingProxyType`` groups; ``canonical_bytes`` only
    accepts literal ``dict``/``list``, so this is the one place that bridges
    the two without asking every caller to flatten its own document first.
    """

    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("authority_document_invalid")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _authority_document_bytes(document: object) -> bytes:
    try:
        if not isinstance(document, Mapping):
            raise ValueError("authority_document_invalid")
        plain_document = _plain(document)
        return canonical_bytes({"domain": _AUTHORITY_SEAL_DOMAIN, "document": plain_document})
    except (TypeError, ValueError):
        raise LocalTrustError("authority_document_invalid") from None


class LocalTrustRoot:
    """One device-local trust anchor derived from a portable vault key.

    Construct only through :meth:`from_vault_key`. The Ed25519 seed and the
    HMAC authority key it derives exist only as attributes of this instance,
    in process memory; :meth:`lock` discards them so a caller can bound how
    long they remain reachable.
    """

    __slots__ = (
        "_private_key",
        "_public_key_bytes",
        "_authority_key",
        "_authority_key_id",
    )

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("use LocalTrustRoot.from_vault_key()")

    @classmethod
    def from_vault_key(cls, vault_key: object) -> LocalTrustRoot:
        """Rederive this device's Ed25519 seal key and authority MAC key.

        The same 32-byte vault key always reconstructs the identical trust
        root; nothing about either derived secret is ever written to disk by
        this module.
        """

        if type(vault_key) is not bytes or len(vault_key) != _VAULT_KEY_BYTES:
            raise LocalTrustError("vault_key_invalid")
        package_seed = _hkdf(vault_key, _PACKAGE_SEAL_INFO)
        authority_key = _hkdf(vault_key, _AUTHORITY_SEAL_INFO)
        #: Independent of ``authority_key`` -- its own HKDF branch off the
        #: vault key, not a hash of the authority key's own bytes -- so this
        #: non-secret identifier can be persisted next to every sealed
        #: document without ever publishing a function of the live secret.
        authority_key_id = _hkdf(vault_key, _AUTHORITY_SEAL_ID_INFO).hex()
        try:
            private_key = Ed25519PrivateKey.from_private_bytes(package_seed)
        except (TypeError, ValueError):
            raise LocalTrustError("vault_key_invalid") from None
        public_key = private_key.public_key()
        public_key_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        root = object.__new__(cls)
        object.__setattr__(root, "_private_key", private_key)
        object.__setattr__(root, "_public_key_bytes", public_key_bytes)
        object.__setattr__(root, "_authority_key", authority_key)
        object.__setattr__(root, "_authority_key_id", authority_key_id)
        return root

    def lock(self) -> None:
        """Discard both derived secrets from process memory. Idempotent."""

        object.__setattr__(self, "_private_key", None)
        object.__setattr__(self, "_public_key_bytes", None)
        object.__setattr__(self, "_authority_key", None)
        object.__setattr__(self, "_authority_key_id", None)

    def _require_unlocked(self) -> tuple[Ed25519PrivateKey, bytes, bytes, str]:
        private_key = self._private_key
        public_key_bytes = self._public_key_bytes
        authority_key = self._authority_key
        authority_key_id = self._authority_key_id
        if (
            private_key is None
            or public_key_bytes is None
            or authority_key is None
            or authority_key_id is None
        ):
            raise LocalTrustError("vault_locked")
        return private_key, public_key_bytes, authority_key, authority_key_id

    def seal_package(self, candidate: VerifiedNormalizedPluginCandidate) -> VerifiedPlugin:
        """Locally sign one importer-verified candidate as ``publisher_verified``."""

        private_key, public_key_bytes, _authority_key, _authority_key_id = self._require_unlocked()
        document = _package_seal_document(candidate)
        payload = canonical_bytes(document)
        signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
        return VerifiedPlugin(
            plugin_id=document["plugin_id"],
            publisher_id=document["publisher_id"],
            plugin_version=document["plugin_version"],
            trust_tier=TrustTier(document["trust_tier"]),
            distribution=document["distribution"],
            manifest_sha256=document["manifest_sha256"],
            package_sha256=document["package_sha256"],
            artifact_sha256=document["artifact_sha256"],
            seal_public_key_sha256=sha256_hex(public_key_bytes),
            signature=signature,
        )

    def verify_package(self, sealed: object) -> VerifiedPlugin:
        """Re-prove one seal against this root's own derived key, or fail closed.

        Nothing about ``sealed`` is trusted beyond what its own signature
        proves: the returned :class:`VerifiedPlugin` is rebuilt entirely from
        the exact bytes the signature covers.
        """

        _private_key, public_key_bytes, _authority_key, _authority_key_id = self._require_unlocked()
        if type(sealed) is not VerifiedPlugin:
            raise LocalTrustError("seal_invalid")
        document = sealed._seal_document()
        payload = canonical_bytes(document)
        signature_bytes = _canonical_base64(sealed.signature)
        if len(signature_bytes) != _ED25519_SIGNATURE_BYTES:
            raise LocalTrustError("seal_invalid")
        try:
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, payload)
        except (InvalidSignature, TypeError, ValueError):
            raise LocalTrustError("seal_invalid") from None
        if sealed.seal_public_key_sha256 != sha256_hex(public_key_bytes):
            raise LocalTrustError("seal_invalid")
        return VerifiedPlugin(
            plugin_id=document["plugin_id"],
            publisher_id=document["publisher_id"],
            plugin_version=document["plugin_version"],
            trust_tier=TrustTier(document["trust_tier"]),
            distribution=document["distribution"],
            manifest_sha256=document["manifest_sha256"],
            package_sha256=document["package_sha256"],
            artifact_sha256=document["artifact_sha256"],
            seal_public_key_sha256=sealed.seal_public_key_sha256,
            signature=sealed.signature,
        )

    def seal_authority(self, document: object) -> SealedCapabilityAuthority:
        """MAC-seal one discovered-capability-authority document.

        The authority MAC key is independent of the package seal's Ed25519
        seed (a different HKDF label), so a compromise of one seal can never
        forge the other.
        """

        _private_key, _public_key_bytes, authority_key, authority_key_id = self._require_unlocked()
        payload = _authority_document_bytes(document)
        mac = hmac.new(authority_key, payload, sha256).digest()
        return SealedCapabilityAuthority(
            document_sha256=sha256_hex(payload),
            mac=base64.b64encode(mac).decode("ascii"),
            mac_key_sha256=authority_key_id,
        )

    def verify_authority(self, document: object, sealed: object) -> None:
        """Re-prove one authority MAC against this root's own key, or fail closed."""

        _private_key, _public_key_bytes, authority_key, authority_key_id = self._require_unlocked()
        if type(sealed) is not SealedCapabilityAuthority:
            raise LocalTrustError("authority_seal_invalid")
        payload = _authority_document_bytes(document)
        mac_bytes = _canonical_base64(sealed.mac)
        if len(mac_bytes) != _MAC_BYTES:
            raise LocalTrustError("authority_seal_invalid")
        expected = hmac.new(authority_key, payload, sha256).digest()
        if (
            not hmac.compare_digest(mac_bytes, expected)
            or sealed.document_sha256 != sha256_hex(payload)
            or sealed.mac_key_sha256 != authority_key_id
        ):
            raise LocalTrustError("authority_seal_invalid")

    def materialize_sealed_package(
        self,
        candidate: VerifiedNormalizedPluginCandidate,
        envelope: object,
        destination: Path,
    ) -> SealedPackageTree:
        """Write a canonical, verifiable sealed package tree under ``destination``.

        Exactly three files, no executable content: ``plugin.json`` (the
        candidate's normalized manifest, extended only with the one
        capability-authority asset declaration this device is about to seal),
        ``tools/authority.json`` (the user's approved capabilities at their
        ``external_unreviewed`` risk, in the same v2 per-app envelope signed
        packages use), and ``local_seal.json`` (this device's Ed25519 proof
        over both, plus the authority MAC and the source-evidence digest).

        ``envelope`` is accepted structurally, not by type check: this module
        may never import ``mcp_platform.plugin_discovery``, which owns
        ``LocalCapabilityEnvelope`` -- see the module docstring's import rail.
        This is a second, independent sealing operation from
        :meth:`seal_package`: it binds the *materialized, asset-declaring*
        manifest a real package tree needs, not the asset-less candidate
        manifest :meth:`seal_package` binds for the bare identity seal.
        """

        private_key, public_key_bytes, _authority_key, _authority_key_id = self._require_unlocked()
        if type(candidate) is not VerifiedNormalizedPluginCandidate:
            raise LocalTrustError("candidate_unverified")
        if candidate.trust_tier is not TrustTier.PUBLISHER_VERIFIED:
            raise LocalTrustError("candidate_trust_tier_unsupported")
        manifest = candidate.manifest
        app_id = getattr(envelope, "app_id", None)
        envelope_plugin_id = getattr(envelope, "plugin_id", None)
        declared_app_ids = {
            item.app_id for item in (*manifest.required_apps, *manifest.optional_apps)
        }
        if (
            envelope_plugin_id != manifest.plugin_id
            or app_id not in declared_app_ids
        ):
            raise LocalTrustError("envelope_binding_invalid")
        groups = _envelope_capability_authority(envelope)
        authority_document = _authority_document(manifest.plugin_id, app_id, groups)
        try:
            authority_bytes = canonical_bytes(authority_document)
        except (TypeError, ValueError):
            raise LocalTrustError("envelope_invalid") from None
        authority_asset = PluginAsset(
            path=_AUTHORITY_ASSET_PATH,
            kind=AssetKind.CAPABILITY_AUTHORITY,
            media_type="application/json",
            sha256=sha256_hex(authority_bytes),
        )
        capabilities = _capability_requirements(groups)
        required_apps = [
            {**item.to_dict(), "capabilities": capabilities}
            if item.app_id == app_id
            else item.to_dict()
            for item in manifest.required_apps
        ]
        optional_apps = [
            {**item.to_dict(), "capabilities": capabilities}
            if item.app_id == app_id
            else item.to_dict()
            for item in manifest.optional_apps
        ]
        try:
            materialized_manifest = PluginManifest.from_dict(
                {
                    **manifest.to_dict(),
                    "assets": [authority_asset.to_dict()],
                    "required_apps": required_apps,
                    "optional_apps": optional_apps,
                }
            )
        except (TypeError, ValueError):
            raise LocalTrustError("candidate_unverified") from None
        manifest_payload = canonical_bytes(materialized_manifest.to_dict())
        manifest_sha256 = sha256_hex(manifest_payload)
        package_sha256 = sha256_hex(
            canonical_bytes(
                {
                    "manifest_sha256": manifest_sha256,
                    "assets": {authority_asset.path: authority_asset.sha256},
                }
            )
        )
        sealed_authority = self.seal_authority(authority_document)
        seal_document = {
            "domain": _MATERIALIZED_SEAL_DOMAIN,
            "plugin_id": manifest.plugin_id,
            "publisher_id": manifest.publisher_id,
            "plugin_version": manifest.plugin_version,
            "trust_tier": candidate.trust_tier.value,
            "manifest_sha256": manifest_sha256,
            "package_sha256": package_sha256,
            "authority_document_sha256": sealed_authority.document_sha256,
            "authority_mac": sealed_authority.mac,
            "authority_mac_key_sha256": sealed_authority.mac_key_sha256,
            "source_evidence_sha256": _source_evidence_sha256(candidate),
        }
        signature = base64.b64encode(
            private_key.sign(canonical_bytes(seal_document))
        ).decode("ascii")
        local_seal_document = {
            **seal_document,
            "seal_public_key_sha256": sha256_hex(public_key_bytes),
            "signature": signature,
        }
        try:
            with PinnedDirectory.create(destination) as staged:
                staged.write_bytes(MANIFEST_NAME, manifest_payload)
                staged.write_bytes(_AUTHORITY_ASSET_PATH, authority_bytes)
                staged.write_bytes(
                    LOCAL_SEAL_NAME, canonical_bytes(local_seal_document)
                )
                staged.fsync()
        except ValueError:
            raise LocalTrustError("materialization_failed") from None
        return SealedPackageTree(
            root=destination,
            plugin_id=manifest.plugin_id,
            publisher_id=manifest.publisher_id,
            plugin_version=manifest.plugin_version,
            manifest_sha256=manifest_sha256,
            package_sha256=package_sha256,
        )

    def verify_materialized_seal(self, local_seal_document: object) -> dict[str, object]:
        """Re-prove one ``local_seal.json`` document, or fail closed.

        The counterpart to the signing step in :meth:`materialize_sealed_package`
        -- a different vault key, a tampered field, or a public-key mismatch
        fails this the same way :meth:`verify_package` fails a tampered
        identity seal. Returns the signed subset of fields (never the caller's
        own unchecked dict) so a caller can only read what this method itself
        just verified.
        """

        _private_key, public_key_bytes, _authority_key, _authority_key_id = self._require_unlocked()
        try:
            if (
                type(local_seal_document) is not dict
                or set(local_seal_document)
                != _MATERIALIZED_SEAL_FIELDS | {"seal_public_key_sha256", "signature"}
            ):
                raise LocalTrustError("seal_invalid")
            seal_document = {
                key: local_seal_document[key] for key in _MATERIALIZED_SEAL_FIELDS
            }
            signature_bytes = _canonical_base64(local_seal_document["signature"])
            if len(signature_bytes) != _ED25519_SIGNATURE_BYTES:
                raise LocalTrustError("seal_invalid")
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
                signature_bytes, canonical_bytes(seal_document)
            )
        except LocalTrustError:
            raise
        except (InvalidSignature, TypeError, ValueError):
            raise LocalTrustError("seal_invalid") from None
        if local_seal_document["seal_public_key_sha256"] != sha256_hex(public_key_bytes):
            raise LocalTrustError("seal_invalid")
        return dict(seal_document)


__all__ = [
    "LOCAL_SEAL_NAME",
    "MANIFEST_NAME",
    "LocalTrustError",
    "LocalTrustRoot",
    "SealedCapabilityAuthority",
    "SealedPackageTree",
    "VerifiedPlugin",
]
