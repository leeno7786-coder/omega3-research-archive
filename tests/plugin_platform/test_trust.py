"""Publisher trust tests: every assertion names a fail-closed trust break."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import plugin_platform.trust as trust_module
from mcp_platform.canonical import canonical_bytes
from plugin_platform.trust import (
    PublisherTrustError,
    TrustedPublisherKeyRing,
    load_embedded_key_ring,
    verify_signed_bytes,
)
from tests.plugin_platform.support import ephemeral_publisher_ring


def _ring_document(private_key: Ed25519PrivateKey) -> dict[str, object]:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "schema_version": 1,
        "keys": [
            {
                "key_id": "test-plugin-key",
                "publisher_id": "test.publisher",
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
            }
        ],
        "revoked_key_ids": [],
        "revoked_publisher_ids": [],
        "revoked_subject_ids": [],
    }


def test_ring_is_validated_immutable_and_mutation_isolated() -> None:
    """Catches public construction or retention of caller-owned mutable key data."""
    private_key = Ed25519PrivateKey.generate()
    raw = _ring_document(private_key)
    expected = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    ring = TrustedPublisherKeyRing.from_dict(raw)
    keys = raw["keys"]
    assert isinstance(keys, list)
    key = keys[0]
    assert isinstance(key, dict)
    key["public_key_base64"] = base64.b64encode(b"x" * 32).decode("ascii")

    assert ring.public_key_bytes("test-plugin-key", "test.publisher", "test.workspace") == expected
    with pytest.raises(TypeError, match="use TrustedPublisherKeyRing.from_dict"):
        TrustedPublisherKeyRing()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"revoked_key_ids": ["test-plugin-key"]}, "revoked_signing_key"),
        ({"revoked_publisher_ids": ["test.publisher"]}, "revoked_publisher"),
        ({"revoked_subject_ids": ["test.workspace"]}, "revoked_subject"),
        ({"keys": []}, "unknown_signing_key"),
    ],
)
def test_key_lookup_rejects_each_untrusted_dimension(
    mutation: dict[str, object], reason: str
) -> None:
    """Catches omission of a key, publisher, or subject revocation lookup."""
    private_key = Ed25519PrivateKey.generate()
    raw = _ring_document(private_key)
    raw.update(mutation)
    ring = TrustedPublisherKeyRing.from_dict(raw)

    with pytest.raises(PublisherTrustError, match=rf"^{reason}$"):
        ring.public_key_bytes("test-plugin-key", "test.publisher", "test.workspace")


def test_key_lookup_rejects_wrong_publisher_binding() -> None:
    """Catches accepting a trusted key under a publisher it never authenticated."""
    _, ring = ephemeral_publisher_ring()

    with pytest.raises(PublisherTrustError, match="^signing_publisher_mismatch$"):
        ring.public_key_bytes("test-plugin-key", "other.publisher", "test.workspace")


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": True},
        {"schema_version": True},
        {"revoked_subject_ids": ["bad id"]},
        {"keys": []},
    ],
)
def test_ring_rejects_unknown_or_malformed_fields(mutation: dict[str, object]) -> None:
    """Catches permissive ring parsing and bool-as-integer schema admission."""
    private_key = Ed25519PrivateKey.generate()
    raw = _ring_document(private_key)
    raw.update(mutation)
    if mutation == {"keys": []}:
        raw["keys"] = [
            {
                "key_id": "test-plugin-key",
                "publisher_id": "test.publisher",
                "public_key_base64": base64.b64encode(b"short").decode("ascii"),
            }
        ]

    with pytest.raises(ValueError):
        TrustedPublisherKeyRing.from_dict(raw)


def test_verify_signed_bytes_accepts_exact_ed25519_signature() -> None:
    """Catches verification against altered bytes or the wrong ring lookup identity."""
    private_key, ring = ephemeral_publisher_ring()
    payload = b"canonical signed bytes"
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")

    assert (
        verify_signed_bytes(
            payload,
            signature,
            ring,
            key_id="test-plugin-key",
            publisher_id="test.publisher",
            subject_id="test.workspace",
        )
        is None
    )


@pytest.mark.parametrize(
    "signature",
    [
        "not base64!",
        base64.b64encode(b"s" * 63).decode("ascii"),
        base64.b64encode(b"s" * 64).decode("ascii").rstrip("="),
    ],
)
def test_verify_signed_bytes_rejects_noncanonical_signature_encoding(signature: str) -> None:
    """Catches permissive base64 decoding and non-64-byte Ed25519 signatures."""
    _, ring = ephemeral_publisher_ring()

    with pytest.raises(PublisherTrustError, match="^signature_encoding_invalid$"):
        verify_signed_bytes(
            b"payload",
            signature,
            ring,
            key_id="test-plugin-key",
            publisher_id="test.publisher",
            subject_id="test.workspace",
        )


def test_verify_signed_bytes_rejects_invalid_signature_without_content() -> None:
    """Catches signature errors leaking signed bytes, signature text, or identity values."""
    private_key, ring = ephemeral_publisher_ring()
    signed_material = b"private catalog material that must not escape"
    signature = base64.b64encode(private_key.sign(b"different bytes")).decode("ascii")

    with pytest.raises(PublisherTrustError) as caught:
        verify_signed_bytes(
            signed_material,
            signature,
            ring,
            key_id="test-plugin-key",
            publisher_id="test.publisher",
            subject_id="test.workspace",
        )

    assert str(caught.value) == "invalid_signature"
    assert signed_material.decode("ascii") not in str(caught.value)
    assert signature not in str(caught.value)


def test_ring_validation_does_not_modify_its_input() -> None:
    """Catches validation that normalizes or deletes fields in the caller's document."""
    private_key = Ed25519PrivateKey.generate()
    raw = _ring_document(private_key)
    before = deepcopy(raw)

    TrustedPublisherKeyRing.from_dict(raw)

    assert raw == before


def test_embedded_production_ring_is_canonical_and_binds_release_catalogs() -> None:
    """Package and OAuth catalogs use separate keys under Omega's publisher."""
    ring = load_embedded_key_ring()

    assert ring.key_ids == (
        "omega-featured-2026-08-26",
        "omega-featured-2026-08-27",
        "omega-featured-2026-08-27-r2",
        "omega-featured-2026-08-28-r1",
        "omega-featured-2026-09-06-r2",
        "omega-oauth-2026-09-06-r2",
    )
    assert len(
        ring.public_key_bytes(
            "omega-featured-2026-08-26",
            "omega.first_party",
            "omega.featured",
        )
    ) == 32
    assert len(
        ring.public_key_bytes(
            "omega-featured-2026-08-27",
            "omega.first_party",
            "omega.featured",
        )
    ) == 32
    assert len(
        ring.public_key_bytes(
            "omega-featured-2026-08-27-r2",
            "omega.first_party",
            "omega.featured",
        )
    ) == 32
    assert len(
        ring.public_key_bytes(
            "omega-featured-2026-08-28-r1",
            "omega.first_party",
            "omega.featured",
        )
    ) == 32
    assert len(
        ring.public_key_bytes(
            "omega-featured-2026-09-06-r2",
            "omega.first_party",
            "omega.featured",
        )
    ) == 32
    assert len(
        ring.public_key_bytes(
            "omega-oauth-2026-09-06-r2",
            "omega.first_party",
            "omega.featured",
        )
    ) == 32
    with pytest.raises(PublisherTrustError, match="^revoked_signing_key$"):
        ring.public_key_bytes(
            "omega-featured-google-2026",
            "omega.first_party",
            "omega.featured",
        )
    with pytest.raises(PublisherTrustError, match="^unknown_signing_key$"):
        ring.public_key_bytes("test-plugin-key", "test.publisher", "test.workspace")
    with pytest.raises(PublisherTrustError, match="^unknown_signing_key$"):
        ring.public_key_bytes("omega-oauth-2026-08-26", "omega.first_party", "omega.featured")
    # The OAuth profile signer rotates atomically with its catalog: the
    # previous key leaves the ring in the same commit (2026-09-06 rotation).
    with pytest.raises(PublisherTrustError, match="^unknown_signing_key$"):
        ring.public_key_bytes("omega-oauth-2026-09-04-r1", "omega.first_party", "omega.featured")
    # The first 2026-09-06 pair was dropped when a day-zero reset destroyed both
    # sealed seeds; nothing can be signed with a key whose secret half is gone.
    for retired in ("omega-featured-2026-09-06-r1", "omega-oauth-2026-09-06-r1"):
        with pytest.raises(PublisherTrustError, match="^unknown_signing_key$"):
            ring.public_key_bytes(retired, "omega.first_party", "omega.featured")


def test_oversized_embedded_ring_rejects_before_read_or_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches unbounded trust-document reads or parsing before the size gate."""

    class OversizedRingPath:
        open_called = False

        def lstat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=1024 * 1024 + 1)

        def open(self, *_args: object, **_kwargs: object) -> object:
            self.open_called = True
            raise AssertionError("oversized trust document must not be opened")

    oversized_path = OversizedRingPath()
    parse_called = False
    parse_json = json.loads

    def record_parse(*args: object, **kwargs: object) -> object:
        nonlocal parse_called
        parse_called = True
        return parse_json(*args, **kwargs)

    monkeypatch.setattr(trust_module, "_EMBEDDED_KEY_RING", oversized_path)
    monkeypatch.setattr(trust_module.json, "loads", record_parse)

    with pytest.raises(ValueError, match="^embedded_trusted_publisher_ring_unreadable$"):
        load_embedded_key_ring()

    assert oversized_path.open_called is False
    assert parse_called is False


def test_embedded_ring_rejects_terminal_lf_as_noncanonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an RFC 8785 exception for a source-control terminal newline."""
    document = {
        "schema_version": 1,
        "keys": [],
        "revoked_key_ids": [],
        "revoked_publisher_ids": [],
        "revoked_subject_ids": [],
    }
    path = tmp_path / "trusted_plugin_keys.json"
    path.write_bytes(canonical_bytes(document) + b"\n")
    monkeypatch.setattr(trust_module, "_EMBEDDED_KEY_RING", path)

    with pytest.raises(ValueError, match="^embedded_trusted_publisher_ring_unreadable$"):
        load_embedded_key_ring()


def test_embedded_ring_artifact_is_exact_rfc8785_bytes() -> None:
    """Catches committing whitespace outside the canonical production ring object."""
    path = Path(trust_module.__file__).with_name("trusted_plugin_keys.json")
    encoded = path.read_bytes()

    assert encoded == canonical_bytes(json.loads(encoded.decode("utf-8")))
