"""Fail-closed verification for signed marketplace provider provenance."""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.contracts import (
    _canonical_https_authority,
    _digest,
    _identifier,
    _https_issuer,
    _https_origin,
    _stable_string,
)
from plugin_platform.package_verifier import (
    VerifiedPlugin,
    validate_verified_app_requirement,
)
from plugin_platform.trust import (
    PublisherTrustError,
    TrustedPublisherKeyRing,
    load_embedded_key_ring,
    verify_signed_bytes,
)

BUNDLED_PROVENANCE_PATH = Path(__file__).resolve().parent / "marketplace_provenance.json"
MAX_PROVENANCE_BYTES = 1024 * 1024
_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "catalog_id",
        "publisher_id",
        "sequence",
        "records",
        "signing_key_id",
        "signature",
    }
)
_RECORD_FIELDS = frozenset(
    {
        "plugin_id",
        "provider_publisher_id",
        "publisher_display_name",
        "provider_id",
        "remote_origin",
        "authorization_issuer",
        "authoritative_source_url",
        "trust_tier",
    }
)
#: A documentation path. The trailing slash is optional because a directory URL
#: is how four of the providers Omega ships publish their MCP docs, and refusing
#: it says nothing about the URL's safety. Segments stay non-empty, so an
#: interior ``//`` is still refused and traversal is still caught by normpath.
_SOURCE_PATH_RE = re.compile(
    r"/(?:[A-Za-z0-9._~!$&'()*+,;=:@+-]+/)*[A-Za-z0-9._~!$&'()*+,;=:@+-]+/?\Z"
)
_VERIFIED_PROVENANCE_MARKER = object()
_VERIFIED_PROVENANCE_LOCK = threading.Lock()
_VERIFIED_PROVENANCE: dict[int, _VerifiedProvenanceIssuance] = {}


class ProviderProvenanceVerificationError(ValueError):
    """A stable, content-free provider-provenance rejection reason."""


def _exact_object(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise ValueError("provenance_invalid")
    return value


def _authoritative_source_url(value: Any) -> str:
    try:
        url = _stable_string(value, field="authoritative_source_url")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not _canonical_https_authority(parsed)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or _SOURCE_PATH_RE.fullmatch(parsed.path) is None
            or posixpath.normpath(parsed.path) != parsed.path.rstrip("/")
            or parsed.geturl() != url
        ):
            raise ValueError("provenance_invalid")
        return url
    except (TypeError, ValueError):
        raise ValueError("provenance_invalid") from None


@dataclass(frozen=True, slots=True)
class ProviderProvenanceRecord:
    plugin_id: str
    provider_publisher_id: str
    publisher_display_name: str
    provider_id: str
    remote_origin: str
    authorization_issuer: str
    authoritative_source_url: str
    trust_tier: str

    @classmethod
    def from_dict(cls, raw: Any) -> ProviderProvenanceRecord:
        data = _exact_object(raw, _RECORD_FIELDS)
        display_name = _stable_string(
            data["publisher_display_name"],
            field="publisher_display_name",
        )
        if len(display_name) > 160 or data["trust_tier"] != "omega_verified":
            raise ValueError("provenance_invalid")
        try:
            return cls(
                plugin_id=_identifier(data["plugin_id"], field="plugin_id"),
                provider_publisher_id=_identifier(
                    data["provider_publisher_id"],
                    field="provider_publisher_id",
                ),
                publisher_display_name=display_name,
                provider_id=_identifier(data["provider_id"], field="provider_id"),
                remote_origin=_https_origin(data["remote_origin"], field="remote_origin"),
                authorization_issuer=_https_issuer(
                    data["authorization_issuer"],
                    field="authorization_issuer",
                ),
                authoritative_source_url=_authoritative_source_url(
                    data["authoritative_source_url"]
                ),
                trust_tier="omega_verified",
            )
        except (TypeError, ValueError):
            raise ValueError("provenance_invalid") from None

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "provider_publisher_id": self.provider_publisher_id,
            "publisher_display_name": self.publisher_display_name,
            "provider_id": self.provider_id,
            "remote_origin": self.remote_origin,
            "authorization_issuer": self.authorization_issuer,
            "authoritative_source_url": self.authoritative_source_url,
            "trust_tier": self.trust_tier,
        }


@dataclass(frozen=True)
class _ProviderProvenanceDocument:
    catalog_id: str
    publisher_id: str
    sequence: int
    records: tuple[ProviderProvenanceRecord, ...]
    signing_key_id: str
    signature: str

    @classmethod
    def from_dict(cls, raw: Any) -> _ProviderProvenanceDocument:
        data = _exact_object(raw, _DOCUMENT_FIELDS)
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("provenance_invalid")
        if type(data["sequence"]) is not int or data["sequence"] < 1:
            raise ValueError("provenance_invalid")
        if type(data["records"]) is not list:
            raise ValueError("provenance_invalid")
        records = tuple(ProviderProvenanceRecord.from_dict(item) for item in data["records"])
        plugin_ids = tuple(record.plugin_id for record in records)
        if not records or len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("provenance_invalid")
        try:
            return cls(
                catalog_id=_identifier(data["catalog_id"], field="catalog_id"),
                publisher_id=_identifier(data["publisher_id"], field="publisher_id"),
                sequence=data["sequence"],
                records=records,
                signing_key_id=_identifier(data["signing_key_id"], field="signing_key_id"),
                signature=_stable_string(data["signature"], field="signature"),
            )
        except (TypeError, ValueError):
            raise ValueError("provenance_invalid") from None

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "catalog_id": self.catalog_id,
            "publisher_id": self.publisher_id,
            "sequence": self.sequence,
            "records": [record.to_dict() for record in self.records],
            "signing_key_id": self.signing_key_id,
        }


@dataclass(frozen=True, init=False)
class VerifiedProviderProvenance:
    """Immutable provider provenance issued only after signature verification."""

    catalog_id: str
    publisher_id: str
    sequence: int
    records: tuple[ProviderProvenanceRecord, ...]
    signing_key_id: str
    provenance_sha256: str
    revision: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("use verify_provider_provenance()")

    @classmethod
    def _from_verified(
        cls,
        *,
        document: _ProviderProvenanceDocument,
        provenance_sha256: str,
    ) -> VerifiedProviderProvenance:
        verified = object.__new__(cls)
        object.__setattr__(verified, "catalog_id", document.catalog_id)
        object.__setattr__(verified, "publisher_id", document.publisher_id)
        object.__setattr__(verified, "sequence", document.sequence)
        object.__setattr__(
            verified,
            "records",
            tuple(ProviderProvenanceRecord.from_dict(item.to_dict()) for item in document.records),
        )
        object.__setattr__(verified, "signing_key_id", document.signing_key_id)
        object.__setattr__(verified, "provenance_sha256", provenance_sha256)
        object.__setattr__(verified, "revision", provenance_sha256)
        object.__setattr__(verified, "_verification_marker", _VERIFIED_PROVENANCE_MARKER)
        _issue_verified_provenance(verified)
        return verified

    def _validated(self) -> _VerifiedProvenanceIssuance:
        try:
            if self._verification_marker is not _VERIFIED_PROVENANCE_MARKER:
                raise ValueError
            with _VERIFIED_PROVENANCE_LOCK:
                issuance = _VERIFIED_PROVENANCE.get(id(self))
            if (
                issuance is None
                or issuance.reference() is not self
            ):
                raise ValueError
            current = _ProviderProvenanceSnapshot.from_verified(self)
            if (
                current != issuance.snapshot
                or current.sha256() != issuance.snapshot_sha256
            ):
                raise ValueError
            return issuance
        except (AttributeError, TypeError, ValueError):
            raise ProviderProvenanceVerificationError("provenance_invalid") from None

    def for_plugin(self, plugin_id: str) -> ProviderProvenanceRecord:
        issuance = self._validated()
        try:
            identity = _identifier(plugin_id, field="plugin_id")
        except (TypeError, ValueError):
            raise ProviderProvenanceVerificationError("provider_provenance_not_found") from None
        snapshot = next(
            (item for item in issuance.snapshot.records if item.plugin_id == identity),
            None,
        )
        if snapshot is None:
            raise ProviderProvenanceVerificationError("provider_provenance_not_found")
        return snapshot.to_record()

    def for_verified_plugin(self, plugin: VerifiedPlugin) -> ProviderProvenanceRecord:
        """Bind one signed provider record to the exact verified package app."""

        try:
            issuance = self._validated()
            if type(plugin) is not VerifiedPlugin:
                raise ValueError
            manifest = plugin.manifest
            snapshot = next(
                (
                    item
                    for item in issuance.snapshot.records
                    if item.plugin_id == manifest.plugin_id
                ),
                None,
            )
            if snapshot is None:
                raise ProviderProvenanceVerificationError(
                    "provider_provenance_not_found"
                )
            record = snapshot.to_record()
            requirements = manifest.required_apps + manifest.optional_apps
            if (
                manifest.publisher_id != issuance.snapshot.publisher_id
                or len(requirements) != 1
            ):
                raise ValueError
            requirement = validate_verified_app_requirement(requirements[0])
            if (
                requirement.deployment.value != "remote"
                or requirement.publisher_id != record.provider_publisher_id
                or requirement.provider_id != record.provider_id
                or requirement.remote_origin != record.remote_origin
                or requirement.authorization_issuer != record.authorization_issuer
            ):
                raise ValueError
            return record
        except ProviderProvenanceVerificationError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise ProviderProvenanceVerificationError(
                "provider_provenance_mismatch"
            ) from None


@dataclass(frozen=True, slots=True)
class _ProviderProvenanceRecordSnapshot:
    plugin_id: str
    provider_publisher_id: str
    publisher_display_name: str
    provider_id: str
    remote_origin: str
    authorization_issuer: str
    authoritative_source_url: str
    trust_tier: str

    @classmethod
    def from_record(
        cls,
        record: ProviderProvenanceRecord,
    ) -> _ProviderProvenanceRecordSnapshot:
        if type(record) is not ProviderProvenanceRecord:
            raise ValueError("provenance_invalid")
        rebuilt = ProviderProvenanceRecord.from_dict(
            ProviderProvenanceRecord.to_dict(record)
        )
        return cls(**rebuilt.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "provider_publisher_id": self.provider_publisher_id,
            "publisher_display_name": self.publisher_display_name,
            "provider_id": self.provider_id,
            "remote_origin": self.remote_origin,
            "authorization_issuer": self.authorization_issuer,
            "authoritative_source_url": self.authoritative_source_url,
            "trust_tier": self.trust_tier,
        }

    def to_record(self) -> ProviderProvenanceRecord:
        return ProviderProvenanceRecord.from_dict(self.to_dict())


@dataclass(frozen=True, slots=True)
class _ProviderProvenanceSnapshot:
    catalog_id: str
    publisher_id: str
    sequence: int
    records: tuple[_ProviderProvenanceRecordSnapshot, ...]
    signing_key_id: str
    provenance_sha256: str
    revision: str

    @classmethod
    def from_verified(
        cls,
        provenance: VerifiedProviderProvenance,
    ) -> _ProviderProvenanceSnapshot:
        records = object.__getattribute__(provenance, "records")
        if type(records) is not tuple:
            raise ValueError("provenance_invalid")
        return cls(
            catalog_id=_identifier(
                object.__getattribute__(provenance, "catalog_id"),
                field="catalog_id",
            ),
            publisher_id=_identifier(
                object.__getattribute__(provenance, "publisher_id"),
                field="publisher_id",
            ),
            sequence=cls._sequence(object.__getattribute__(provenance, "sequence")),
            records=tuple(
                _ProviderProvenanceRecordSnapshot.from_record(record)
                for record in records
            ),
            signing_key_id=_identifier(
                object.__getattribute__(provenance, "signing_key_id"),
                field="signing_key_id",
            ),
            provenance_sha256=_digest(
                object.__getattribute__(provenance, "provenance_sha256"),
                field="provenance_sha256",
            ),
            revision=_digest(
                object.__getattribute__(provenance, "revision"),
                field="revision",
            ),
        )

    @staticmethod
    def _sequence(value: object) -> int:
        if type(value) is not int or value < 1:
            raise ValueError("provenance_invalid")
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog_id": self.catalog_id,
            "publisher_id": self.publisher_id,
            "sequence": self.sequence,
            "records": [record.to_dict() for record in self.records],
            "signing_key_id": self.signing_key_id,
            "provenance_sha256": self.provenance_sha256,
            "revision": self.revision,
        }

    def sha256(self) -> str:
        return sha256_hex(canonical_bytes(self.to_dict()))


@dataclass(frozen=True)
class _VerifiedProvenanceIssuance:
    reference: weakref.ReferenceType[VerifiedProviderProvenance]
    snapshot: _ProviderProvenanceSnapshot
    snapshot_sha256: str


def _issue_verified_provenance(provenance: VerifiedProviderProvenance) -> None:
    identity = id(provenance)

    def discard(
        reference: weakref.ReferenceType[VerifiedProviderProvenance],
        *,
        provenance_id: int = identity,
    ) -> None:
        with _VERIFIED_PROVENANCE_LOCK:
            issuance = _VERIFIED_PROVENANCE.get(provenance_id)
            if issuance is not None and issuance.reference is reference:
                _VERIFIED_PROVENANCE.pop(provenance_id, None)

    reference = weakref.ref(provenance, discard)
    snapshot = _ProviderProvenanceSnapshot.from_verified(provenance)
    issuance = _VerifiedProvenanceIssuance(
        reference=reference,
        snapshot=snapshot,
        snapshot_sha256=snapshot.sha256(),
    )
    with _VERIFIED_PROVENANCE_LOCK:
        _VERIFIED_PROVENANCE[identity] = issuance


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate_key")
        document[key] = value
    return document


def _reject_non_json_constant(value: str) -> None:
    raise ValueError("non_json_constant")


def _read_regular_bytes(path: Path) -> bytes:
    try:
        observed = path.lstat()
        attributes = getattr(observed, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISREG(observed.st_mode)
            or attributes & reparse_flag
            or observed.st_size > MAX_PROVENANCE_BYTES
        ):
            raise ValueError
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            opened_attributes = getattr(opened, "st_file_attributes", 0)
            if (
                not os.path.samestat(observed, opened)
                or not stat.S_ISREG(opened.st_mode)
                or opened_attributes & reparse_flag
                or opened.st_size > MAX_PROVENANCE_BYTES
            ):
                raise ValueError
            payload = handle.read(MAX_PROVENANCE_BYTES + 1)
        if len(payload) > MAX_PROVENANCE_BYTES:
            raise ValueError
        return payload
    except (OSError, TypeError, ValueError):
        raise ProviderProvenanceVerificationError("provenance_file_invalid") from None


def verify_provider_provenance(
    path: Path,
    key_ring: TrustedPublisherKeyRing,
) -> VerifiedProviderProvenance:
    """Verify bounded canonical provenance before exposing provider identity."""

    try:
        provenance_path = Path(path)
    except (TypeError, ValueError):
        raise ProviderProvenanceVerificationError("provenance_file_invalid") from None
    payload = _read_regular_bytes(provenance_path)
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ProviderProvenanceVerificationError("provenance_json_invalid") from None
    try:
        canonical = canonical_bytes(raw)
    except (TypeError, ValueError):
        raise ProviderProvenanceVerificationError("provenance_json_invalid") from None
    if canonical != payload:
        raise ProviderProvenanceVerificationError("provenance_not_canonical")
    try:
        document = _ProviderProvenanceDocument.from_dict(raw)
    except (TypeError, ValueError):
        raise ProviderProvenanceVerificationError("provenance_invalid") from None
    try:
        verify_signed_bytes(
            canonical_bytes(document.unsigned_dict()),
            document.signature,
            key_ring,
            key_id=document.signing_key_id,
            publisher_id=document.publisher_id,
            subject_id=document.catalog_id,
        )
        for record in document.records:
            key_ring.public_key_bytes(
                document.signing_key_id,
                document.publisher_id,
                record.plugin_id,
            )
    except PublisherTrustError as exc:
        raise ProviderProvenanceVerificationError(str(exc)) from None
    return VerifiedProviderProvenance._from_verified(
        document=document,
        provenance_sha256=sha256_hex(canonical),
    )


def scaffold_provenance_record(
    *,
    plugin_id: str,
    provider_publisher_id: str,
    publisher_display_name: str,
    provider_id: str,
    remote_origin: str,
    authorization_issuer: str,
    authoritative_source_url: str,
) -> dict[str, object]:
    """Build one UNSIGNED provenance record for a provider being onboarded.

    The trust tier is not a parameter. ``ProviderProvenanceRecord`` admits
    exactly one value, so it is a constant of this format rather than a
    decision -- and the decision is elsewhere on purpose: a listing becomes
    omega_verified only when the review policy names this record together with
    its digest, which both ``build_marketplace_catalog`` and the signing step
    check. Minting the record here keeps that one literal inside the module
    that owns the format, so no maintainer command has to name it.

    Validated on the way out, so a scaffold cannot write a record the release
    path would later reject.
    """
    return ProviderProvenanceRecord.from_dict(
        {
            "plugin_id": plugin_id,
            "provider_publisher_id": provider_publisher_id,
            "publisher_display_name": publisher_display_name,
            "provider_id": provider_id,
            "remote_origin": remote_origin,
            "authorization_issuer": authorization_issuer,
            "authoritative_source_url": authoritative_source_url,
            "trust_tier": "omega_verified",
        }
    ).to_dict()


def provenance_digest(plugin_id: str) -> str:
    """Return the canonical digest of one plugin signed provider-provenance record."""

    provenance = verify_provider_provenance(
        BUNDLED_PROVENANCE_PATH,
        load_embedded_key_ring(),
    )
    return sha256_hex(canonical_bytes(provenance.for_plugin(plugin_id).to_dict()))


__all__ = [
    "scaffold_provenance_record",
    "BUNDLED_PROVENANCE_PATH",
    "ProviderProvenanceRecord",
    "ProviderProvenanceVerificationError",
    "VerifiedProviderProvenance",
    "provenance_digest",
    "verify_provider_provenance",
]
