"""Offline trust roots for signed first-party MCP packages."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_RING_FIELDS = frozenset(
    {"schema_version", "keys", "revoked_key_ids", "revoked_server_ids"}
)
_KEY_FIELDS = frozenset({"key_id", "public_key_base64"})
_EMBEDDED_KEY_RING = Path(__file__).with_name("trusted_release_keys.json")


class KeyRingLookupError(Exception):
    """A content-free key lookup failure safe to translate at the boundary."""


def _exact_object(value: Any, expected: frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError(f"{label} must be an object")
    if set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    return value


def _stable_string(value: Any, *, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be a stable string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must be a stable string")
    return value


def _unique_strings(value: Any, *, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{label} must be a list")
    parsed = tuple(_stable_string(item, label=f"{label} item") for item in value)
    if len(parsed) != len(set(parsed)):
        raise ValueError(f"{label} cannot contain duplicates")
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("trusted key ring contains duplicate fields")
        document[key] = value
    return document


@dataclass(frozen=True, init=False)
class TrustedKeyRing:
    """Immutable release keys and revocations shipped with Omega."""

    _keys: Mapping[str, bytes]
    _revoked_key_ids: frozenset[str]
    _revoked_server_ids: frozenset[str]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("use TrustedKeyRing.from_dict()")

    @classmethod
    def _from_validated(
        cls,
        keys: Mapping[str, bytes],
        revoked_key_ids: frozenset[str],
        revoked_server_ids: frozenset[str],
    ) -> TrustedKeyRing:
        ring = object.__new__(cls)
        object.__setattr__(ring, "_keys", keys)
        object.__setattr__(ring, "_revoked_key_ids", revoked_key_ids)
        object.__setattr__(ring, "_revoked_server_ids", revoked_server_ids)
        return ring

    @classmethod
    def from_dict(cls, raw: Any) -> TrustedKeyRing:
        data = _exact_object(raw, _RING_FIELDS, label="trusted key ring")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("trusted key ring schema_version must be 1")
        if type(data["keys"]) is not list:
            raise ValueError("trusted key ring keys must be a list")

        keys: dict[str, bytes] = {}
        for raw_key in data["keys"]:
            key = _exact_object(raw_key, _KEY_FIELDS, label="trusted key")
            key_id = _stable_string(key["key_id"], label="key_id")
            encoded = _stable_string(key["public_key_base64"], label="public_key_base64")
            if key_id in keys:
                raise ValueError("trusted key IDs cannot contain duplicates")
            try:
                public_bytes = base64.b64decode(encoded, validate=True)
                if base64.b64encode(public_bytes).decode("ascii") != encoded:
                    raise ValueError("public key must use canonical base64")
                Ed25519PublicKey.from_public_bytes(public_bytes)
            except (TypeError, ValueError) as exc:
                raise ValueError("public key must be a base64 Ed25519 public key") from exc
            if len(public_bytes) != 32:
                raise ValueError("public key must be a 32-byte Ed25519 public key")
            keys[key_id] = public_bytes

        revoked_key_ids = _unique_strings(data["revoked_key_ids"], label="revoked_key_ids")
        revoked_server_ids = _unique_strings(
            data["revoked_server_ids"], label="revoked_server_ids"
        )
        if set(keys).intersection(revoked_key_ids):
            raise ValueError("a trusted key cannot also be revoked")
        if any(not server_id.startswith("omega.first_party.") for server_id in revoked_server_ids):
            raise ValueError("revoked server IDs must be first-party server IDs")
        return cls._from_validated(
            MappingProxyType(keys),
            frozenset(revoked_key_ids),
            frozenset(revoked_server_ids),
        )

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def public_key_bytes(self, key_id: str, server_id: str) -> bytes:
        """Return a trusted key or raise a stable, content-free reason."""
        if key_id in self._revoked_key_ids:
            raise KeyRingLookupError("revoked_signing_key")
        if server_id in self._revoked_server_ids:
            raise KeyRingLookupError("revoked_server")
        public_key = self._keys.get(key_id)
        if public_key is None:
            raise KeyRingLookupError("unknown_signing_key")
        return public_key


def load_embedded_key_ring() -> TrustedKeyRing:
    """Load the repository-owned trust document without external I/O."""
    try:
        raw = json.loads(
            _EMBEDDED_KEY_RING.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("embedded trusted key ring is unreadable") from exc
    return TrustedKeyRing.from_dict(raw)
