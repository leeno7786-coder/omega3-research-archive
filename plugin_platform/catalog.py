"""Closed offline verification for signed marketplace plugin catalogs."""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import threading
import weakref
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.contracts import (
    _canonical_https_authority,
    _digest,
    _https_origin,
    _identifier,
    _package_path,
    _semver,
    _stable_string,
)
from plugin_platform.trust import (
    PublisherTrustError,
    TrustedPublisherKeyRing,
    verify_signed_bytes,
)

MARKETPLACE_ROOT = Path(__file__).resolve().parent / "marketplace"
MARKETPLACE_CATALOG_PATH = MARKETPLACE_ROOT / "catalog.json"
MARKETPLACE_PACKAGES_ROOT = MARKETPLACE_ROOT / "packages"

MAX_CATALOG_BYTES = 1024 * 1024
MAX_TITLE_CHARS = 80
MAX_SUMMARY_CHARS = 280
MAX_CATEGORIES = 4
MAX_SEARCH_TERMS = 12
MAX_RELEASE_HISTORY = 8

CATALOG_SCHEMA_VERSION = 2

_CATALOG_FIELDS = frozenset(
    {
        "schema_version",
        "catalog_id",
        "publisher_id",
        "sequence",
        "entries",
        "signing_key_id",
        "signature",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "plugin_id",
        "publisher_id",
        "plugin_version",
        "manifest_sha256",
        "package_sha256",
        "package_uri",
        "title",
        "summary",
        "categories",
        "search_terms",
        "icon_asset_path",
        "support_url",
        "source_url",
        "trust_tier",
        "distribution",
        "provenance_sha256",
        "released_at",
        "pricing",
        "offline_capable",
        "release_history",
        "update_notes_sha256",
    }
)
_RELEASE_FIELDS = frozenset({"plugin_version", "package_sha256"})
_URI_PATH_RE = re.compile(r"/(?:[A-Za-z0-9._~!$&'()*+,;=:@+-]+/)*[A-Za-z0-9._~!$&'()*+,;=:@+-]+\Z")
_URL_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~!$&'()*+,;=:@+-]+)*\Z")
_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?\Z")
_UTC_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_VERIFIED_CATALOG_MARKER = object()
_VERIFIED_CATALOGS_LOCK = threading.Lock()
_VERIFIED_CATALOGS: dict[int, _VerifiedCatalogIssuance] = {}
_VERIFIED_CATALOG_ENTRIES: dict[int, _VerifiedCatalogEntryIssuance] = {}


class CatalogVerificationError(Exception):
    """A stable, content-free signed-catalog rejection reason."""


class TrustTier(StrEnum):
    """Closed marketplace trust classification assigned by the catalog source."""

    OMEGA_VERIFIED = "omega_verified"
    PUBLISHER_VERIFIED = "publisher_verified"
    PERSONAL = "personal"


class DistributionKind(StrEnum):
    """Closed set of ways one catalogued plugin is delivered and executed."""

    REMOTE_PACKAGE = "remote_package"
    REMOTE_MCP = "remote_mcp"
    PORTABLE_PACKAGE = "portable_package"


class PricingClass(StrEnum):
    """Closed pricing disclosure; unknown is stated, never inferred."""

    FREE = "free"
    PAID = "paid"
    UNKNOWN = "unknown"


def _exact_object(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise ValueError("catalog_invalid")
    return value


def _canonical_package_uri(value: Any) -> str:
    try:
        uri = _stable_string(value, field="package_uri")
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "https"
            or not _canonical_https_authority(parsed)
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
            or parsed.path.startswith("//")
            or _URI_PATH_RE.fullmatch(parsed.path) is None
            or posixpath.normpath(parsed.path) != parsed.path
            or parsed.geturl() != uri
        ):
            raise ValueError("package_uri_invalid")

    except (TypeError, ValueError):
        raise ValueError("package_uri_invalid") from None
    return uri


def _canonical_https_url(value: Any, *, field: str) -> str:
    url = _stable_string(value, field=field)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not _canonical_https_authority(parsed)
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _URL_PATH_RE.fullmatch(parsed.path) is None
        or (parsed.path and posixpath.normpath(parsed.path) != parsed.path)
        or parsed.geturl() != url
    ):
        raise ValueError(f"{field} is not a canonical HTTPS URL")
    return url


def _bounded_text(value: Any, *, field: str, limit: int) -> str:
    text = _stable_string(value, field=field)
    if len(text) > limit:
        raise ValueError(f"{field} exceeds its bound")
    return text


def _slug_tuple(value: Any, *, field: str, minimum: int, maximum: int) -> tuple[str, ...]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} is out of bounds")
    slugs = tuple(_stable_string(item, field=field) for item in value)
    if any(_SLUG_RE.fullmatch(slug) is None for slug in slugs):
        raise ValueError(f"{field} is not a canonical slug list")
    if list(slugs) != sorted(set(slugs)):
        raise ValueError(f"{field} is not a unique ascending slug list")
    return slugs


def _utc_timestamp(value: Any, *, field: str) -> str:
    timestamp = _stable_string(value, field=field)
    if _UTC_TIMESTAMP_RE.fullmatch(timestamp) is None:
        raise ValueError(f"{field} is not a canonical UTC timestamp")
    try:
        datetime.fromisoformat(f"{timestamp[:-1]}+00:00")
    except ValueError:
        raise ValueError(f"{field} is not a real UTC timestamp") from None
    return timestamp


def _strict_bool(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _optional(value: Any, parser: Any, *, field: str) -> Any:
    return None if value is None else parser(value, field=field)


def _enum_member(value: Any, enum: type[StrEnum], *, field: str) -> Any:
    if type(value) is not str:
        raise ValueError(f"{field} must be a declared value")
    try:
        return enum(value)
    except ValueError:
        raise ValueError(f"{field} must be a declared value") from None


@dataclass(frozen=True, slots=True)
class MarketplaceDistribution:
    """Exact delivery descriptor carrying only its own kind-specific metadata."""

    kind: DistributionKind
    remote_origin: str | None = None
    artifact_sha256: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> MarketplaceDistribution:
        if type(raw) is not dict or any(type(key) is not str for key in raw):
            raise ValueError("distribution is not an object")
        kind = _enum_member(raw.get("kind"), DistributionKind, field="distribution kind")
        expected = {
            DistributionKind.REMOTE_PACKAGE: frozenset({"kind"}),
            DistributionKind.REMOTE_MCP: frozenset({"kind", "remote_origin"}),
            DistributionKind.PORTABLE_PACKAGE: frozenset({"kind", "artifact_sha256"}),
        }[kind]
        if set(raw) != expected:
            raise ValueError("distribution carries metadata of another kind")
        if kind is DistributionKind.REMOTE_MCP:
            return cls(
                kind=kind,
                remote_origin=_https_origin(raw["remote_origin"], field="remote_origin"),
            )
        if kind is DistributionKind.PORTABLE_PACKAGE:
            return cls(
                kind=kind,
                artifact_sha256=_digest(raw["artifact_sha256"], field="artifact_sha256"),
            )
        return cls(kind=kind)

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {"kind": self.kind.value}
        if self.kind is DistributionKind.REMOTE_MCP:
            document["remote_origin"] = self.remote_origin
        elif self.kind is DistributionKind.PORTABLE_PACKAGE:
            document["artifact_sha256"] = self.artifact_sha256
        return document


@dataclass(frozen=True, slots=True)
class MarketplaceReleaseRecord:
    """One superseded immutable release pinned by version and package digest."""

    plugin_version: str
    package_sha256: str

    @classmethod
    def from_dict(cls, raw: Any) -> MarketplaceReleaseRecord:
        data = _exact_object(raw, _RELEASE_FIELDS)
        return cls(
            plugin_version=_semver(data["plugin_version"], field="plugin_version"),
            package_sha256=_digest(data["package_sha256"], field="package_sha256"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_version": self.plugin_version,
            "package_sha256": self.package_sha256,
        }


def _release_history(
    value: Any, *, current_version: str
) -> tuple[MarketplaceReleaseRecord, ...]:
    if type(value) is not list or len(value) > MAX_RELEASE_HISTORY:
        raise ValueError("release_history is out of bounds")
    history = tuple(MarketplaceReleaseRecord.from_dict(item) for item in value)
    versions = [item.plugin_version for item in history]
    if len(versions) != len(set(versions)) or current_version in versions:
        raise ValueError("release_history repeats a released version")
    try:
        parsed = [Version(item) for item in versions]
        current = Version(current_version)
    except InvalidVersion:
        raise ValueError("release_history is not ordered") from None
    if any(earlier <= later for earlier, later in zip(parsed, parsed[1:], strict=False)):
        raise ValueError("release_history is not newest first")
    if parsed and parsed[0] >= current:
        raise ValueError("release_history is not a superseded release set")
    return history


@dataclass(frozen=True)
class MarketplaceCatalogEntry:
    """One signed marketplace listing: execution identity plus product metadata."""

    plugin_id: str
    publisher_id: str
    plugin_version: str
    manifest_sha256: str
    package_sha256: str
    package_uri: str
    title: str
    summary: str
    categories: tuple[str, ...]
    search_terms: tuple[str, ...]
    icon_asset_path: str | None
    support_url: str | None
    source_url: str
    trust_tier: TrustTier
    distribution: MarketplaceDistribution
    provenance_sha256: str
    released_at: str
    pricing: PricingClass
    offline_capable: bool
    release_history: tuple[MarketplaceReleaseRecord, ...]
    update_notes_sha256: str | None

    @classmethod
    def from_dict(cls, raw: Any) -> MarketplaceCatalogEntry:
        """Parse one exact marketplace v2 entry under closed field grammar."""
        data = _exact_object(raw, _ENTRY_FIELDS)
        package_uri = _canonical_package_uri(data["package_uri"])
        try:
            plugin_version = _semver(data["plugin_version"], field="plugin_version")
            return cls(
                plugin_id=_identifier(data["plugin_id"], field="plugin_id"),
                publisher_id=_identifier(data["publisher_id"], field="publisher_id"),
                plugin_version=plugin_version,
                manifest_sha256=_digest(data["manifest_sha256"], field="manifest_sha256"),
                package_sha256=_digest(data["package_sha256"], field="package_sha256"),
                package_uri=package_uri,
                title=_bounded_text(data["title"], field="title", limit=MAX_TITLE_CHARS),
                summary=_bounded_text(
                    data["summary"],
                    field="summary",
                    limit=MAX_SUMMARY_CHARS,
                ),
                categories=_slug_tuple(
                    data["categories"],
                    field="categories",
                    minimum=1,
                    maximum=MAX_CATEGORIES,
                ),
                search_terms=_slug_tuple(
                    data["search_terms"],
                    field="search_terms",
                    minimum=0,
                    maximum=MAX_SEARCH_TERMS,
                ),
                icon_asset_path=_optional(
                    data["icon_asset_path"],
                    _package_path,
                    field="icon_asset_path",
                ),
                support_url=_optional(
                    data["support_url"],
                    _canonical_https_url,
                    field="support_url",
                ),
                source_url=_canonical_https_url(data["source_url"], field="source_url"),
                trust_tier=_enum_member(data["trust_tier"], TrustTier, field="trust_tier"),
                distribution=MarketplaceDistribution.from_dict(data["distribution"]),
                provenance_sha256=_digest(
                    data["provenance_sha256"],
                    field="provenance_sha256",
                ),
                released_at=_utc_timestamp(data["released_at"], field="released_at"),
                pricing=_enum_member(data["pricing"], PricingClass, field="pricing"),
                offline_capable=_strict_bool(
                    data["offline_capable"],
                    field="offline_capable",
                ),
                release_history=_release_history(
                    data["release_history"],
                    current_version=plugin_version,
                ),
                update_notes_sha256=_optional(
                    data["update_notes_sha256"],
                    _digest,
                    field="update_notes_sha256",
                ),
            )
        except (TypeError, ValueError):
            raise ValueError("catalog_invalid") from None

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "publisher_id": self.publisher_id,
            "plugin_version": self.plugin_version,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "package_uri": self.package_uri,
            "title": self.title,
            "summary": self.summary,
            "categories": list(self.categories),
            "search_terms": list(self.search_terms),
            "icon_asset_path": self.icon_asset_path,
            "support_url": self.support_url,
            "source_url": self.source_url,
            "trust_tier": self.trust_tier.value,
            "distribution": self.distribution.to_dict(),
            "provenance_sha256": self.provenance_sha256,
            "released_at": self.released_at,
            "pricing": self.pricing.value,
            "offline_capable": self.offline_capable,
            "release_history": [item.to_dict() for item in self.release_history],
            "update_notes_sha256": self.update_notes_sha256,
        }

    @classmethod
    def _from_verified_dict(cls, raw: Any) -> MarketplaceCatalogEntry:
        return cls.from_dict(raw)


@dataclass(frozen=True, slots=True)
class _CatalogEntrySnapshot:
    """Canonical bytes of one issued entry, immune to later attribute shadowing."""

    canonical: bytes

    @classmethod
    def from_entry(cls, entry: MarketplaceCatalogEntry) -> _CatalogEntrySnapshot:
        if type(entry) is not MarketplaceCatalogEntry:
            raise ValueError("catalog_entry_invalid")
        rebuilt = MarketplaceCatalogEntry.from_dict(MarketplaceCatalogEntry.to_dict(entry))
        return cls(canonical=canonical_bytes(rebuilt.to_dict()))

    def to_dict(self) -> dict[str, object]:
        document = json.loads(self.canonical.decode("utf-8"))
        if type(document) is not dict:
            raise ValueError("catalog_entry_invalid")
        return document

    def to_entry(self) -> MarketplaceCatalogEntry:
        return MarketplaceCatalogEntry.from_dict(self.to_dict())

    def sha256(self) -> str:
        return sha256_hex(self.canonical)


@dataclass(frozen=True)
class _MarketplaceCatalog:
    catalog_id: str
    publisher_id: str
    sequence: int
    entries: tuple[MarketplaceCatalogEntry, ...]
    signing_key_id: str
    signature: str

    @classmethod
    def from_dict(cls, raw: Any) -> _MarketplaceCatalog:
        data = _exact_object(raw, _CATALOG_FIELDS)
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != CATALOG_SCHEMA_VERSION
        ):
            raise ValueError("catalog_invalid")
        if type(data["sequence"]) is not int or data["sequence"] < 1:
            raise ValueError("catalog_invalid")
        if type(data["entries"]) is not list:
            raise ValueError("catalog_invalid")
        entries = tuple(MarketplaceCatalogEntry.from_dict(item) for item in data["entries"])
        plugin_ids = tuple(item.plugin_id for item in entries)
        if len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("catalog_invalid")
        return cls(
            catalog_id=_identifier(data["catalog_id"], field="catalog_id"),
            publisher_id=_identifier(data["publisher_id"], field="publisher_id"),
            sequence=data["sequence"],
            entries=entries,
            signing_key_id=_identifier(data["signing_key_id"], field="signing_key_id"),
            signature=_stable_string(data["signature"], field="signature"),
        )

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_id": self.catalog_id,
            "publisher_id": self.publisher_id,
            "sequence": self.sequence,
            "entries": [entry.to_dict() for entry in self.entries],
            "signing_key_id": self.signing_key_id,
        }


@dataclass(frozen=True, init=False)
class VerifiedMarketplaceCatalog:
    """Immutable verified catalog content and its canonical digest revision."""

    catalog_id: str
    publisher_id: str
    sequence: int
    entries: tuple[MarketplaceCatalogEntry, ...]
    signing_key_id: str
    catalog_sha256: str
    revision: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("use verify_plugin_catalog()")

    @classmethod
    def _from_verified(
        cls,
        *,
        catalog_id: str,
        publisher_id: str,
        sequence: int,
        entries: tuple[MarketplaceCatalogEntry, ...],
        signing_key_id: str,
        catalog_sha256: str,
        signer_public_key_sha256: str,
    ) -> VerifiedMarketplaceCatalog:
        if type(sequence) is not int or sequence < 1 or type(entries) is not tuple:
            raise ValueError("catalog_invalid")
        copied: list[MarketplaceCatalogEntry] = []
        for entry in entries:
            if type(entry) is not MarketplaceCatalogEntry:
                raise ValueError("catalog_invalid")
            copied.append(
                MarketplaceCatalogEntry._from_verified_dict(
                    MarketplaceCatalogEntry.to_dict(entry)
                )
            )
        copied_entries = tuple(copied)
        plugin_ids = tuple(entry.plugin_id for entry in copied_entries)
        if len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("catalog_invalid")
        digest = _digest(catalog_sha256, field="catalog_sha256")
        verified = object.__new__(cls)
        object.__setattr__(verified, "catalog_id", _identifier(catalog_id, field="catalog_id"))
        object.__setattr__(
            verified,
            "publisher_id",
            _identifier(publisher_id, field="publisher_id"),
        )
        object.__setattr__(verified, "sequence", sequence)
        object.__setattr__(verified, "entries", copied_entries)
        object.__setattr__(
            verified,
            "signing_key_id",
            _identifier(signing_key_id, field="signing_key_id"),
        )
        object.__setattr__(verified, "catalog_sha256", digest)
        object.__setattr__(verified, "revision", digest)
        object.__setattr__(verified, "_verification_marker", _VERIFIED_CATALOG_MARKER)
        _issue_verified_catalog(
            verified,
            signer_public_key_sha256=_digest(
                signer_public_key_sha256,
                field="signer_public_key_sha256",
            ),
        )
        return verified

    def entry(self, plugin_id: str) -> MarketplaceCatalogEntry:
        """Return the exact signed listing for one plugin identity, or fail closed."""

        try:
            catalog = _validated_verified_catalog(self)
        except ValueError:
            raise CatalogVerificationError("catalog_invalid") from None
        try:
            identity = _identifier(plugin_id, field="plugin_id")
        except (TypeError, ValueError):
            raise CatalogVerificationError("catalog_entry_unavailable") from None
        listing = next(
            (item for item in catalog.entries if item.plugin_id == identity),
            None,
        )
        if listing is None:
            raise CatalogVerificationError("catalog_entry_unavailable")
        return listing


@dataclass(frozen=True)
class _VerifiedCatalogIssuance:
    reference: weakref.ReferenceType[VerifiedMarketplaceCatalog]
    snapshot_sha256: str
    signer_public_key_sha256: str
    entry_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class _VerifiedCatalogEntryIssuance:
    reference: weakref.ReferenceType[MarketplaceCatalogEntry]
    snapshot: _CatalogEntrySnapshot
    catalog_id: str
    publisher_id: str
    signing_key_id: str
    catalog_sha256: str
    signer_public_key_sha256: str


def _catalog_snapshot_sha256(catalog: VerifiedMarketplaceCatalog) -> str:
    entries = object.__getattribute__(catalog, "entries")
    if type(entries) is not tuple:
        raise ValueError("catalog_invalid")
    snapshots = tuple(_CatalogEntrySnapshot.from_entry(entry) for entry in entries)
    return sha256_hex(
        canonical_bytes(
            {
                "catalog_id": catalog.catalog_id,
                "publisher_id": catalog.publisher_id,
                "sequence": catalog.sequence,
                "entries": [entry.to_dict() for entry in snapshots],
                "signing_key_id": catalog.signing_key_id,
                "catalog_sha256": catalog.catalog_sha256,
                "revision": catalog.revision,
            }
        )
    )


def _issue_verified_catalog(
    catalog: VerifiedMarketplaceCatalog,
    *,
    signer_public_key_sha256: str,
) -> None:
    catalog_identity = id(catalog)

    def discard_catalog(
        reference: weakref.ReferenceType[VerifiedMarketplaceCatalog],
        *,
        catalog_id: int = catalog_identity,
    ) -> None:
        with _VERIFIED_CATALOGS_LOCK:
            issuance = _VERIFIED_CATALOGS.get(catalog_id)
            if issuance is not None and issuance.reference is reference:
                _VERIFIED_CATALOGS.pop(catalog_id, None)

    catalog_reference = weakref.ref(catalog, discard_catalog)
    entries = object.__getattribute__(catalog, "entries")
    if type(entries) is not tuple:
        raise ValueError("catalog_invalid")
    snapshots = tuple(_CatalogEntrySnapshot.from_entry(entry) for entry in entries)
    catalog_issuance = _VerifiedCatalogIssuance(
        reference=catalog_reference,
        snapshot_sha256=_catalog_snapshot_sha256(catalog),
        signer_public_key_sha256=signer_public_key_sha256,
        entry_sha256s=tuple(snapshot.sha256() for snapshot in snapshots),
    )
    entry_issuances: list[tuple[int, _VerifiedCatalogEntryIssuance]] = []
    for entry, snapshot in zip(entries, snapshots, strict=True):
        entry_identity = id(entry)

        def discard_entry(
            reference: weakref.ReferenceType[MarketplaceCatalogEntry],
            *,
            entry_id: int = entry_identity,
        ) -> None:
            with _VERIFIED_CATALOGS_LOCK:
                issuance = _VERIFIED_CATALOG_ENTRIES.get(entry_id)
                if issuance is not None and issuance.reference is reference:
                    _VERIFIED_CATALOG_ENTRIES.pop(entry_id, None)

        entry_reference = weakref.ref(entry, discard_entry)
        entry_issuances.append(
            (
                entry_identity,
                _VerifiedCatalogEntryIssuance(
                    reference=entry_reference,
                    snapshot=snapshot,
                    catalog_id=catalog.catalog_id,
                    publisher_id=catalog.publisher_id,
                    signing_key_id=catalog.signing_key_id,
                    catalog_sha256=catalog.catalog_sha256,
                    signer_public_key_sha256=signer_public_key_sha256,
                ),
            )
        )
    with _VERIFIED_CATALOGS_LOCK:
        _VERIFIED_CATALOGS[catalog_identity] = catalog_issuance
        for entry_identity, issuance in entry_issuances:
            _VERIFIED_CATALOG_ENTRIES[entry_identity] = issuance


def validate_verified_catalog_entry(
    value: Any,
    *,
    catalog: VerifiedMarketplaceCatalog,
) -> MarketplaceCatalogEntry:
    """Return an unmodified entry issued by the configured signed catalog."""

    if type(value) is not MarketplaceCatalogEntry:
        raise ValueError("catalog_entry_unverified")
    try:
        catalog = _validated_verified_catalog(catalog)
    except ValueError:
        raise ValueError("catalog_entry_context_mismatch") from None
    with _VERIFIED_CATALOGS_LOCK:
        entry_issuance = _VERIFIED_CATALOG_ENTRIES.get(id(value))
        catalog_issuance = _VERIFIED_CATALOGS.get(id(catalog))
    if entry_issuance is None or entry_issuance.reference() is not value:
        raise ValueError("catalog_entry_unverified")
    if catalog_issuance is None or catalog_issuance.reference() is not catalog:
        raise ValueError("catalog_entry_context_mismatch")
    try:
        current = _CatalogEntrySnapshot.from_entry(value)
        if current != entry_issuance.snapshot:
            raise ValueError("catalog_entry_invalid")
        entry_sha256 = entry_issuance.snapshot.sha256()
        if (
            entry_issuance.catalog_id != catalog.catalog_id
            or entry_issuance.publisher_id != catalog.publisher_id
            or entry_issuance.signing_key_id != catalog.signing_key_id
            or entry_issuance.catalog_sha256 != catalog.catalog_sha256
            or entry_issuance.signer_public_key_sha256
            != catalog_issuance.signer_public_key_sha256
            or entry_sha256 not in catalog_issuance.entry_sha256s
        ):
            raise ValueError("catalog_entry_context_mismatch")
        return entry_issuance.snapshot.to_entry()
    except ValueError as exc:
        if str(exc) == "catalog_entry_context_mismatch":
            raise
        raise ValueError("catalog_entry_invalid") from None
    except (AttributeError, TypeError):
        raise ValueError("catalog_entry_invalid") from None


def _validated_verified_catalog(value: Any) -> VerifiedMarketplaceCatalog:
    try:
        if (
            type(value) is not VerifiedMarketplaceCatalog
            or value._verification_marker is not _VERIFIED_CATALOG_MARKER
        ):
            raise ValueError("catalog_invalid")
        with _VERIFIED_CATALOGS_LOCK:
            issuance = _VERIFIED_CATALOGS.get(id(value))
        if (
            issuance is None
            or issuance.reference() is not value
            or issuance.snapshot_sha256 != _catalog_snapshot_sha256(value)
        ):
            raise ValueError("catalog_invalid")
        return value
    except (AttributeError, TypeError, ValueError):
        raise ValueError("catalog_invalid") from None


def _validate_regular_file(path: Path) -> os.stat_result:
    try:
        observed = path.lstat()
    except (OSError, ValueError):
        raise CatalogVerificationError("catalog_file_invalid") from None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(observed, "st_file_attributes", 0)
    if not stat.S_ISREG(observed.st_mode) or attributes & reparse_flag:
        raise CatalogVerificationError("catalog_file_invalid")
    return observed


def _read_catalog_bytes(path: Path) -> bytes:
    observed = _validate_regular_file(path)
    if observed.st_size > MAX_CATALOG_BYTES:
        raise CatalogVerificationError("catalog_too_large")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            attributes = getattr(opened, "st_file_attributes", 0)
            if (
                not os.path.samestat(observed, opened)
                or not stat.S_ISREG(opened.st_mode)
                or attributes & reparse_flag
            ):
                raise CatalogVerificationError("catalog_file_invalid")
            payload = handle.read(MAX_CATALOG_BYTES + 1)
    except CatalogVerificationError:
        raise
    except (OSError, ValueError):
        raise CatalogVerificationError("catalog_file_invalid") from None
    if len(payload) > MAX_CATALOG_BYTES:
        raise CatalogVerificationError("catalog_too_large")
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate_key")
        document[key] = value
    return document


def _reject_non_json_constant(value: str) -> None:
    raise ValueError("non_json_constant")


def _parse_catalog_bytes(payload: bytes) -> tuple[_MarketplaceCatalog, bytes]:
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise CatalogVerificationError("catalog_json_invalid") from None
    try:
        canonical = canonical_bytes(document)
    except (TypeError, ValueError):
        raise CatalogVerificationError("catalog_json_invalid") from None
    if canonical != payload:
        raise CatalogVerificationError("catalog_not_canonical")
    try:
        return _MarketplaceCatalog.from_dict(document), canonical
    except (TypeError, ValueError):
        raise CatalogVerificationError("catalog_invalid") from None


def verify_plugin_catalog(
    path: Path, key_ring: TrustedPublisherKeyRing
) -> VerifiedMarketplaceCatalog:
    """Verify a bounded canonical local catalog before exposing its entries."""
    try:
        catalog_path = Path(path)
    except (TypeError, ValueError):
        raise CatalogVerificationError("catalog_file_invalid") from None
    payload = _read_catalog_bytes(catalog_path)
    catalog, canonical = _parse_catalog_bytes(payload)
    try:
        verify_signed_bytes(
            canonical_bytes(catalog.unsigned_dict()),
            catalog.signature,
            key_ring,
            key_id=catalog.signing_key_id,
            publisher_id=catalog.publisher_id,
            subject_id=catalog.catalog_id,
        )
        signer_public_key = key_ring.public_key_bytes(
            catalog.signing_key_id,
            catalog.publisher_id,
            catalog.catalog_id,
        )
        for entry in catalog.entries:
            key_ring.public_key_bytes(
                catalog.signing_key_id,
                catalog.publisher_id,
                entry.plugin_id,
            )
    except PublisherTrustError as exc:
        raise CatalogVerificationError(str(exc)) from None

    catalog_sha256 = sha256_hex(canonical)
    return VerifiedMarketplaceCatalog._from_verified(
        catalog_id=catalog.catalog_id,
        publisher_id=catalog.publisher_id,
        sequence=catalog.sequence,
        entries=catalog.entries,
        signing_key_id=catalog.signing_key_id,
        catalog_sha256=catalog_sha256,
        signer_public_key_sha256=sha256_hex(signer_public_key),
    )


def verify_catalog_successor(
    previous: VerifiedMarketplaceCatalog, candidate: VerifiedMarketplaceCatalog
) -> None:
    """Fail closed on identity changes, rollback, or same-sequence equivocation."""
    try:
        previous = _validated_verified_catalog(previous)
        candidate = _validated_verified_catalog(candidate)
    except ValueError:
        raise CatalogVerificationError("catalog_invalid") from None
    if (
        previous.catalog_id != candidate.catalog_id
        or previous.publisher_id != candidate.publisher_id
    ):
        raise CatalogVerificationError("catalog_identity_changed")
    if candidate.sequence < previous.sequence:
        raise CatalogVerificationError("catalog_sequence_rollback")
    if candidate.sequence == previous.sequence:
        if candidate.catalog_sha256 != previous.catalog_sha256:
            raise CatalogVerificationError("catalog_sequence_equivocation")
        return
