"""Canonical JSON and digest primitives for signed MCP documents."""

import hashlib
import math
from typing import Any

import rfc8785


def strict_json_value(value: Any, *, path: str = "$") -> Any:
    """Return JSON-only data, rejecting values RFC 8785 cannot sign safely."""
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite JSON")
        return value
    if type(value) is list:
        return [strict_json_value(item, path=f"{path}[]") for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError(f"{path} keys must be strings")
        return {
            key: strict_json_value(item, path=f"{path}.{key}") for key, item in value.items()
        }
    raise ValueError(f"{path} is not a JSON value")


def canonical_bytes(document: Any) -> bytes:
    """Return the UTF-8 RFC 8785 canonical representation of JSON data."""
    return rfc8785.dumps(strict_json_value(document))


def sha256_hex(payload: bytes) -> str:
    """Return a lowercase SHA-256 digest for immutable signed-package bytes."""
    return hashlib.sha256(payload).hexdigest()
