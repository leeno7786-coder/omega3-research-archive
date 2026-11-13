"""Immutable publisher trust roots and generic Ed25519 verification."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from mcp_platform.canonical import canonical_bytes
from plugin_platform.contracts import _identifier, _stable_string

_RING_FIELDS = frozenset(
    {
        "schema_version",
        "keys",
        "revoked_key_ids",
        "revoked_publisher_ids",
        "revoked_subject_ids",
    }
)
_KEY_FIELDS = frozenset({"key_id", "publisher_id", "public_key_base64"})
_EMBEDDED_KEY_RING = Path(__file__).with_name("trusted_plugin_keys.json")
MAX_TRUST_DOCUMENT_BYTES = 1024 * 1024


class PublisherTrustError(Exception):
    """A stable, content-free publisher trust rejection reason."""


def _exact_object(value: Any, fields: frozenset[str], *, reason: str) -> dict[str, Any]:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise ValueError(reason)
    return value


def _identifier_list(value: Any, *, field: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{field}_invalid")
    identifiers = tuple(_identifier(item, field=field) for item in value)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{field}_invalid")
    return identifiers


def _decode_public_key(value: Any) -> bytes:
    try:
        encoded = _stable_string(value, field="public_key_base64")
        decoded = base64.b64decode(encoded, validate=True)
        if base64.b64encode(decoded).decode("ascii") != encoded or len(decoded) != 32:
            raise ValueError("public_key_invalid")
        Ed25519PublicKey.from_public_bytes(decoded)
    except (TypeError, ValueError):
        raise ValueError("public_key_invalid") from None
    return bytes(decoded)


@dataclass(frozen=True)
class _PublisherKey:
    publisher_id: str
    public_key: bytes


@dataclass(frozen=True, init=False)
class TrustedPublisherKeyRing:
    """Immutable publisher-bound Ed25519 keys and three revocation sets."""

    _keys: Mapping[str, _PublisherKey]
    _revoked_key_ids: frozenset[str]
    _revoked_publisher_ids: frozenset[str]
    _revoked_subject_ids: frozenset[str]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("use TrustedPublisherKeyRing.from_dict()")

    @classmethod
    def _from_validated(
        cls,
        keys: Mapping[str, _PublisherKey],
        revoked_key_ids: frozenset[str],
        revoked_publisher_ids: frozenset[str],
        revoked_subject_ids: frozenset[str],
    ) -> TrustedPublisherKeyRing:
        ring = object.__new__(cls)
        object.__setattr__(ring, "_keys", keys)
        object.__setattr__(ring, "_revoked_key_ids", revoked_key_ids)
        object.__setattr__(ring, "_revoked_publisher_ids", revoked_publisher_ids)
        object.__setattr__(ring, "_revoked_subject_ids", revoked_subject_ids)
        return ring

    @classmethod
    def from_dict(cls, raw: Any) -> TrustedPublisherKeyRing:
        """Validate and isolate one exact publisher key-ring document."""
        data = _exact_object(raw, _RING_FIELDS, reason="trusted_publisher_ring_invalid")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("trusted_publisher_ring_invalid")
        if type(data["keys"]) is not list:
            raise ValueError("trusted_publisher_ring_invalid")

        keys: dict[str, _PublisherKey] = {}
        for value in data["keys"]:
            key = _exact_object(value, _KEY_FIELDS, reason="trusted_publisher_key_invalid")
            key_id = _identifier(key["key_id"], field="key_id")
            if key_id in keys:
                raise ValueError("trusted_publisher_key_duplicate")
            keys[key_id] = _PublisherKey(
                publisher_id=_identifier(key["publisher_id"], field="publisher_id"),
                public_key=_decode_public_key(key["public_key_base64"]),
            )

        return cls._from_validated(
            MappingProxyType(keys),
            frozenset(_identifier_list(data["revoked_key_ids"], field="revoked_key_id")),
            frozenset(
                _identifier_list(data["revoked_publisher_ids"], field="revoked_publisher_id")
            ),
            frozenset(_identifier_list(data["revoked_subject_ids"], field="revoked_subject_id")),
        )

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def public_key_bytes(self, key_id: str, publisher_id: str, subject_id: str) -> bytes:
        """Return the exact bound key or raise a stable trust reason."""
        if key_id in self._revoked_key_ids:
            raise PublisherTrustError("revoked_signing_key")
        if publisher_id in self._revoked_publisher_ids:
            raise PublisherTrustError("revoked_publisher")
        if subject_id in self._revoked_subject_ids:
            raise PublisherTrustError("revoked_subject")
        record = self._keys.get(key_id)
        if record is None:
            raise PublisherTrustError("unknown_signing_key")
        if record.publisher_id != publisher_id:
            raise PublisherTrustError("signing_publisher_mismatch")
        return record.public_key


def verify_signed_bytes(
    signed_bytes: bytes,
    signature: str,
    key_ring: TrustedPublisherKeyRing,
    *,
    key_id: str,
    publisher_id: str,
    subject_id: str,
) -> None:
    """Verify exact bytes with a canonical-base64 Ed25519 signature."""
    try:
        decoded = base64.b64decode(signature, validate=True)
        if (
            type(signature) is not str
            or base64.b64encode(decoded).decode("ascii") != signature
            or len(decoded) != 64
        ):
            raise ValueError("signature_encoding_invalid")
    except (TypeError, ValueError):
        raise PublisherTrustError("signature_encoding_invalid") from None

    public_key = key_ring.public_key_bytes(key_id, publisher_id, subject_id)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(decoded, signed_bytes)
    except (InvalidSignature, TypeError, ValueError):
        raise PublisherTrustError("invalid_signature") from None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate_key")
        document[key] = value
    return document


def load_embedded_key_ring() -> TrustedPublisherKeyRing:
    """Load the repository-owned production ring and reject noncanonical bytes."""
    try:
        observed = _EMBEDDED_KEY_RING.lstat()
        if observed.st_size > MAX_TRUST_DOCUMENT_BYTES:
            raise ValueError("oversized_ring")
        with _EMBEDDED_KEY_RING.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size > MAX_TRUST_DOCUMENT_BYTES or not os.path.samestat(observed, opened):
                raise ValueError("changed_ring")
            encoded = handle.read(opened.st_size)
            after_read = os.fstat(handle.fileno())
        if len(encoded) != opened.st_size or after_read.st_size != opened.st_size:
            raise ValueError("incomplete_ring")
        raw = json.loads(encoded.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        if canonical_bytes(raw) != encoded:
            raise ValueError("noncanonical_ring")
        return TrustedPublisherKeyRing.from_dict(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("embedded_trusted_publisher_ring_unreadable") from None
