"""Signed plugin catalog verification and anti-rollback tests."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.catalog import (
    MARKETPLACE_CATALOG_PATH,
    CatalogVerificationError,
    DistributionKind,
    MarketplaceCatalogEntry,
    PricingClass,
    TrustTier,
    VerifiedMarketplaceCatalog,
    verify_catalog_successor,
    verify_plugin_catalog,
)
from plugin_platform.marketplace_provenance import provenance_digest
from plugin_platform.trust import (
    PublisherTrustError,
    TrustedPublisherKeyRing,
    verify_signed_bytes,
)
from plugin_platform.trust import load_embedded_key_ring as _load_embedded_key_ring
from tests.plugin_platform.support import (
    ephemeral_publisher_ring,
    legacy_v1_catalog_document,
    valid_catalog_document,
    valid_catalog_entry_document,
)

_NONCANONICAL_PACKAGE_URIS = (
    "http://plugins.example.test/test.workspace/1.0.0",
    "https://user@plugins.example.test/test.workspace/1.0.0",
    "https://user:pass@plugins.example.test/test.workspace/1.0.0",
    "https://plugins.example.test/test.workspace/1.0.0?download=true",
    "https://plugins.example.test/test.workspace/1.0.0#release",
    "https://plugins.example.test:443/test.workspace/1.0.0",
    "https://plugins.example.test:8443/test.workspace/1.0.0",
    "https://plugins.example.test/test.workspace/../other/1.0.0",
    "https://plugins.example.test//test.workspace/1.0.0",
    "https://plugins.example.test/%74est.workspace/1.0.0",
    "https://PLUGINS.example.test/test.workspace/1.0.0",
    "https://plugins.example.test/",
)

_BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CANONICAL_ED25519_TERMINALS = "AQgw"


def _sign_catalog(document: dict[str, object], private_key: Ed25519PrivateKey) -> dict[str, object]:
    unsigned = deepcopy(document)
    unsigned.pop("signature", None)
    signed = deepcopy(unsigned)
    signed["signature"] = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    return signed


def _write_catalog(path: Path, document: dict[str, object]) -> None:
    path.write_bytes(canonical_bytes(document))


def _append_duplicate_plugin_identity(document: dict[str, object]) -> None:
    entries = document["entries"]
    assert isinstance(entries, list)
    duplicate = deepcopy(entries[0])
    assert isinstance(duplicate, dict)
    duplicate["plugin_version"] = "2.0.0"
    entries.append(duplicate)


def _verified_catalog(
    tmp_path: Path,
) -> tuple[VerifiedMarketplaceCatalog, Ed25519PrivateKey, TrustedPublisherKeyRing]:
    private_key, ring = ephemeral_publisher_ring()
    path = tmp_path / "catalog.json"
    _write_catalog(path, valid_catalog_document(private_key))
    return verify_plugin_catalog(path, ring), private_key, ring


def _verify_catalog_document(
    path: Path,
    document: dict[str, object],
    private_key: Ed25519PrivateKey,
    ring: TrustedPublisherKeyRing,
) -> VerifiedMarketplaceCatalog:
    signed = _sign_catalog(document, private_key)
    _write_catalog(path, signed)
    return verify_plugin_catalog(path, ring)


def test_catalog_entry_is_closed_validated_and_immutable() -> None:
    """Catches permissive entry fields, identity drift, and mutable verified entries."""
    raw = valid_catalog_entry_document()
    entry = MarketplaceCatalogEntry.from_dict(raw)
    raw["plugin_id"] = "mutated.plugin"

    assert entry.plugin_id == "test.workspace"
    with pytest.raises(FrozenInstanceError):
        entry.plugin_id = "mutated.plugin"  # type: ignore[misc]
    with pytest.raises(ValueError):
        MarketplaceCatalogEntry.from_dict({**raw, "unexpected": True})


@pytest.mark.parametrize("package_uri", _NONCANONICAL_PACKAGE_URIS)
def test_catalog_entry_rejects_noncanonical_package_uri(package_uri: str) -> None:
    """Catches URI admission outside canonical credential-free HTTPS package paths."""
    raw = {**valid_catalog_entry_document(), "package_uri": package_uri}

    with pytest.raises(ValueError, match="package_uri_invalid"):
        MarketplaceCatalogEntry.from_dict(raw)


def test_verify_catalog_returns_immutable_canonical_identity(tmp_path: Path) -> None:
    """Catches hashing a projection, retaining caller data, or returning mutable entries."""
    private_key, ring = ephemeral_publisher_ring()
    document = valid_catalog_document(private_key)
    expected_digest = sha256_hex(canonical_bytes(document))
    path = tmp_path / "catalog.json"
    _write_catalog(path, document)

    verified = verify_plugin_catalog(path, ring)
    entries = document["entries"]
    assert isinstance(entries, list)
    first = entries[0]
    assert isinstance(first, dict)
    first["plugin_id"] = "mutated.plugin"

    assert verified.catalog_id == "test.catalog"
    assert verified.publisher_id == "test.publisher"
    assert verified.sequence == 1
    assert tuple(item.plugin_id for item in verified.entries) == ("test.workspace",)
    assert verified.catalog_sha256 == expected_digest
    assert verified.revision == expected_digest
    with pytest.raises(FrozenInstanceError):
        verified.sequence = 2  # type: ignore[misc]


def test_verified_catalog_has_no_public_constructor() -> None:
    """Catches callers forging a catalog without canonical signature verification."""
    entry = MarketplaceCatalogEntry.from_dict(valid_catalog_entry_document())

    with pytest.raises(TypeError, match=r"^use verify_plugin_catalog\(\)$"):
        VerifiedMarketplaceCatalog(
            catalog_id="test.catalog",
            publisher_id="test.publisher",
            sequence=1,
            entries=(entry,),
            signing_key_id="test-plugin-key",
            catalog_sha256="c" * 64,
            revision="c" * 64,
        )


def test_verify_catalog_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """Catches last-wins JSON parsing at the signed catalog boundary."""
    _, ring = ephemeral_publisher_ring()
    path = tmp_path / "catalog.json"
    path.write_bytes(b'{"schema_version":2,"schema_version":2,"catalog_id":"secret-material"}')

    with pytest.raises(CatalogVerificationError, match="^catalog_json_invalid$"):
        verify_plugin_catalog(path, ring)


def test_verify_catalog_rejects_noncanonical_json_bytes(tmp_path: Path) -> None:
    """Catches admitting semantically equal bytes that are not RFC 8785 canonical."""
    private_key, ring = ephemeral_publisher_ring()
    document = valid_catalog_document(private_key)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(CatalogVerificationError, match="^catalog_not_canonical$"):
        verify_plugin_catalog(path, ring)


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda document: document.update({"unknown": "secret"}), "catalog_invalid"),
        (lambda document: document.update({"sequence": 0}), "catalog_invalid"),
        (_append_duplicate_plugin_identity, "catalog_invalid"),
    ],
)
def test_verify_catalog_rejects_unknown_fields_nonpositive_sequence_and_duplicates(
    tmp_path: Path, mutator: object, reason: str
) -> None:
    """Catches open catalog parsing, zero sequence, or duplicate plugin identities."""
    private_key, ring = ephemeral_publisher_ring()
    document = valid_catalog_document(private_key)
    assert callable(mutator)
    mutator(document)
    document = _sign_catalog(document, private_key)
    path = tmp_path / "catalog.json"
    _write_catalog(path, document)

    with pytest.raises(CatalogVerificationError, match=rf"^{reason}$"):
        verify_plugin_catalog(path, ring)


@pytest.mark.parametrize(
    ("ring_mutation", "reason"),
    [
        ({"revoked_key_ids": ["test-plugin-key"]}, "revoked_signing_key"),
        ({"revoked_publisher_ids": ["test.publisher"]}, "revoked_publisher"),
        ({"revoked_subject_ids": ["test.catalog"]}, "revoked_subject"),
        ({"revoked_subject_ids": ["test.workspace"]}, "revoked_subject"),
    ],
)
def test_verify_catalog_enforces_all_catalog_trust_revocations(
    tmp_path: Path, ring_mutation: dict[str, object], reason: str
) -> None:
    """Catches bypassing key, publisher, catalog, or listed-plugin revocation."""
    private_key, _ = ephemeral_publisher_ring()
    # Rebuild the fixture ring around the key that actually signed the catalog.
    encoded_public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    ring_document: dict[str, object] = {
        "schema_version": 1,
        "keys": [
            {
                "key_id": "test-plugin-key",
                "publisher_id": "test.publisher",
                "public_key_base64": encoded_public_key,
            }
        ],
        "revoked_key_ids": [],
        "revoked_publisher_ids": [],
        "revoked_subject_ids": [],
    }
    ring_document.update(ring_mutation)
    ring = TrustedPublisherKeyRing.from_dict(ring_document)
    path = tmp_path / "catalog.json"
    _write_catalog(path, valid_catalog_document(private_key))

    with pytest.raises(CatalogVerificationError, match=rf"^{reason}$"):
        verify_plugin_catalog(path, ring)


def test_verify_catalog_rejects_wrong_publisher_key_binding(tmp_path: Path) -> None:
    """Catches validating a catalog under a key bound to another publisher."""
    private_key, ring = ephemeral_publisher_ring()
    document = valid_catalog_document(private_key)
    document["publisher_id"] = "other.publisher"
    document = _sign_catalog(document, private_key)
    path = tmp_path / "catalog.json"
    _write_catalog(path, document)

    with pytest.raises(CatalogVerificationError, match="^signing_publisher_mismatch$"):
        verify_plugin_catalog(path, ring)


def test_verify_catalog_rejects_invalid_signature_and_post_sign_mutation(tmp_path: Path) -> None:
    """Catches accepting a signature made by another key or data changed after signing."""
    private_key, ring = ephemeral_publisher_ring()
    document = valid_catalog_document(private_key)
    entries = document["entries"]
    assert isinstance(entries, list)
    first = entries[0]
    assert isinstance(first, dict)
    first["package_sha256"] = "c" * 64
    path = tmp_path / "catalog.json"
    _write_catalog(path, document)

    with pytest.raises(CatalogVerificationError, match="^invalid_signature$"):
        verify_plugin_catalog(path, ring)


def test_verify_catalog_rejects_oversized_bytes_before_json_parsing(tmp_path: Path) -> None:
    """Catches parsing or retaining a catalog beyond the closed one-MiB boundary."""
    _, ring = ephemeral_publisher_ring()
    path = tmp_path / "catalog.json"
    path.write_bytes(b"{" + b"x" * (1024 * 1024))

    with pytest.raises(CatalogVerificationError, match="^catalog_too_large$"):
        verify_plugin_catalog(path, ring)


def test_verify_catalog_rejects_nonregular_input_without_path_disclosure(tmp_path: Path) -> None:
    """Catches following directories or including filesystem paths in rejection errors."""
    _, ring = ephemeral_publisher_ring()
    secret_path = tmp_path / "private-catalog-location"
    secret_path.mkdir()

    with pytest.raises(CatalogVerificationError) as caught:
        verify_plugin_catalog(secret_path, ring)

    assert str(caught.value) == "catalog_file_invalid"
    assert str(secret_path) not in str(caught.value)


def test_catalog_errors_never_contain_signed_material(tmp_path: Path) -> None:
    """Catches exception chaining or validation text that discloses catalog values."""
    private_key, ring = ephemeral_publisher_ring()
    secret_uri = "https://plugins.example.test/secret-package-name/1.0.0?private=true"
    document = valid_catalog_document(private_key)
    entries = document["entries"]
    assert isinstance(entries, list)
    first = entries[0]
    assert isinstance(first, dict)
    first["package_uri"] = secret_uri
    document = _sign_catalog(document, private_key)
    path = tmp_path / "catalog.json"
    _write_catalog(path, document)

    with pytest.raises(CatalogVerificationError) as caught:
        verify_plugin_catalog(path, ring)

    assert str(caught.value) == "catalog_invalid"
    assert secret_uri not in str(caught.value)
    assert caught.value.__cause__ is None


def test_catalog_successor_rejects_identity_change_rollback_and_equivocation(
    tmp_path: Path,
) -> None:
    """Catches successor checks accepting any object not issued by verification."""
    private_key, ring = ephemeral_publisher_ring()
    previous_document = valid_catalog_document(private_key)
    previous_document["sequence"] = 2
    previous = _verify_catalog_document(
        tmp_path / "previous.json",
        previous_document,
        private_key,
        ring,
    )

    other_catalog_document = deepcopy(previous_document)
    other_catalog_document["catalog_id"] = "other.catalog"
    other_catalog = _verify_catalog_document(
        tmp_path / "other-catalog.json",
        other_catalog_document,
        private_key,
        ring,
    )

    other_key = Ed25519PrivateKey.generate()
    other_key_id = "other-plugin-key"
    other_public_key = base64.b64encode(
        other_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    other_ring = TrustedPublisherKeyRing.from_dict(
        {
            "schema_version": 1,
            "keys": [
                {
                    "key_id": other_key_id,
                    "publisher_id": "other.publisher",
                    "public_key_base64": other_public_key,
                }
            ],
            "revoked_key_ids": [],
            "revoked_publisher_ids": [],
            "revoked_subject_ids": [],
        }
    )
    other_publisher_document = valid_catalog_document(other_key)
    other_publisher_document["publisher_id"] = "other.publisher"
    other_publisher_document["signing_key_id"] = other_key_id
    other_publisher_document["sequence"] = 2
    other_entries = other_publisher_document["entries"]
    assert isinstance(other_entries, list)
    assert isinstance(other_entries[0], dict)
    other_entries[0]["publisher_id"] = "other.publisher"
    other_publisher = _verify_catalog_document(
        tmp_path / "other-publisher.json",
        other_publisher_document,
        other_key,
        other_ring,
    )

    rollback_document = deepcopy(previous_document)
    rollback_document["sequence"] = 1
    rollback = _verify_catalog_document(
        tmp_path / "rollback.json",
        rollback_document,
        private_key,
        ring,
    )

    equivocation_document = deepcopy(previous_document)
    equivocation_entries = equivocation_document["entries"]
    assert isinstance(equivocation_entries, list)
    assert isinstance(equivocation_entries[0], dict)
    equivocation_entries[0]["package_sha256"] = "f" * 64
    equivocation = _verify_catalog_document(
        tmp_path / "equivocation.json",
        equivocation_document,
        private_key,
        ring,
    )

    for candidate, reason in (
        (other_publisher, "catalog_identity_changed"),
        (other_catalog, "catalog_identity_changed"),
        (rollback, "catalog_sequence_rollback"),
        (equivocation, "catalog_sequence_equivocation"),
    ):
        with pytest.raises(CatalogVerificationError, match=rf"^{reason}$"):
            verify_catalog_successor(previous, candidate)


def test_catalog_successor_admits_identical_replay_and_higher_sequence(tmp_path: Path) -> None:
    """Catches rejecting idempotent replay or requiring adjacent rather than increasing sequence."""
    previous, private_key, ring = _verified_catalog(tmp_path)
    higher_document = valid_catalog_document(private_key)
    higher_document["sequence"] = previous.sequence + 5
    higher = _verify_catalog_document(
        tmp_path / "higher.json",
        higher_document,
        private_key,
        ring,
    )

    assert verify_catalog_successor(previous, previous) is None
    assert verify_catalog_successor(previous, higher) is None


def test_catalog_schema_is_closed_and_accepts_the_pinned_document() -> None:
    """Catches schema/runtime drift or an open catalog object in published contracts."""
    schema_path = Path("schema/plugins/plugin_catalog.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    private_key = Ed25519PrivateKey.generate()
    document = valid_catalog_document(private_key)

    Draft202012Validator(schema).validate(document)
    invalid = deepcopy(document)
    invalid["unknown"] = True
    assert list(Draft202012Validator(schema).iter_errors(invalid))


def test_catalog_signature_schema_matches_runtime_canonical_padding_bits() -> None:
    """Catches schema admission of nonzero Ed25519 Base64 padding-bit aliases."""
    schema = json.loads(
        Path("schema/plugins/plugin_catalog.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    _private_key, ring = ephemeral_publisher_ring()
    document = valid_catalog_document(Ed25519PrivateKey.generate())

    observed_terminals: list[str] = []
    for low_padding_source in range(4):
        encoded = base64.b64encode(bytes(63) + bytes([low_padding_source])).decode("ascii")
        terminal = encoded[-3]
        observed_terminals.append(terminal)
        document["signature"] = encoded
        assert list(validator.iter_errors(document)) == []
        with pytest.raises(PublisherTrustError, match=r"^invalid_signature$"):
            verify_signed_bytes(
                b"catalog padding parity",
                encoded,
                ring,
                key_id="test-plugin-key",
                publisher_id="test.publisher",
                subject_id="test.catalog",
            )

        canonical_index = _BASE64_ALPHABET.index(terminal)
        for alias_offset in range(1, 16):
            alias = encoded[:-3] + _BASE64_ALPHABET[canonical_index + alias_offset] + "=="
            document["signature"] = alias
            assert list(validator.iter_errors(document))
            with pytest.raises(
                PublisherTrustError,
                match=r"^signature_encoding_invalid$",
            ):
                verify_signed_bytes(
                    b"catalog padding parity",
                    alias,
                    ring,
                    key_id="test-plugin-key",
                    publisher_id="test.publisher",
                    subject_id="test.catalog",
                )

    assert "".join(observed_terminals) == _CANONICAL_ED25519_TERMINALS


@pytest.mark.parametrize(
    "package_uri",
    [
        "https://plugins.example.test/plugin.pkg",
        "https://plugins.example.test/plugin.pkg/1.0.0",
    ],
)
def test_catalog_runtime_and_schema_accept_nonempty_canonical_package_paths(
    package_uri: str,
) -> None:
    schema = json.loads(
        Path("schema/plugins/plugin_catalog.schema.json").read_text(encoding="utf-8")
    )
    document = valid_catalog_document(Ed25519PrivateKey.generate())
    entries = document["entries"]
    assert isinstance(entries, list)
    entry = entries[0]
    assert isinstance(entry, dict)
    entry["package_uri"] = package_uri

    assert MarketplaceCatalogEntry.from_dict(entry).package_uri == package_uri
    assert list(Draft202012Validator(schema).iter_errors(document)) == []


@pytest.mark.parametrize("package_uri", _NONCANONICAL_PACKAGE_URIS)
def test_catalog_schema_rejects_noncanonical_package_uris(package_uri: str) -> None:
    """Catches schema admission of a package URI the runtime rejects."""
    schema = json.loads(
        Path("schema/plugins/plugin_catalog.schema.json").read_text(encoding="utf-8")
    )
    private_key = Ed25519PrivateKey.generate()
    document = valid_catalog_document(private_key)
    entries = document["entries"]
    assert isinstance(entries, list)
    entry = entries[0]
    assert isinstance(entry, dict)
    entry["package_uri"] = package_uri

    assert list(Draft202012Validator(schema).iter_errors(document))


def _entry_document(**changes: object) -> dict[str, object]:
    return {**valid_catalog_entry_document(), **changes}


def test_marketplace_v2_entry_has_bounded_product_metadata() -> None:
    """Catches a bundled catalog that ships without v2 product or trust metadata."""
    catalog = verify_plugin_catalog(MARKETPLACE_CATALOG_PATH, _load_embedded_key_ring())
    github = catalog.entry("github.official")

    assert github.trust_tier is TrustTier.OMEGA_VERIFIED
    assert github.categories == ("development",)
    assert github.distribution.kind is DistributionKind.REMOTE_PACKAGE
    assert github.provenance_sha256 == provenance_digest("github.official")
    assert github.pricing is PricingClass.FREE
    assert github.offline_capable is False
    assert github.package_uri == (
        "https://plugins.omega2ai.com/marketplace/packages/github.official/"
        f"{github.plugin_version}"
    )
    assert github.title and len(github.title) <= 80
    assert github.summary and len(github.summary) <= 280
    assert github.search_terms and len(github.search_terms) <= 12
    assert github.source_url.startswith("https://")
    assert github.released_at.endswith("Z")
    assert github.release_history
    assert all(
        item.plugin_version != github.plugin_version for item in github.release_history
    )


def test_marketplace_v2_catalog_exposes_only_signed_entry_identities() -> None:
    """Catches an entry accessor that fabricates or leaks unlisted plugin identities."""
    catalog = verify_plugin_catalog(MARKETPLACE_CATALOG_PATH, _load_embedded_key_ring())

    assert catalog.entry("github.official") in catalog.entries
    with pytest.raises(CatalogVerificationError, match="^catalog_entry_unavailable$"):
        catalog.entry("not.a.marketplace.plugin")


def test_catalog_v1_is_rejected_without_compatibility_parser(tmp_path: Path) -> None:
    """Catches a v1 compatibility lane surviving the marketplace v2 upgrade."""
    private_key, ring = ephemeral_publisher_ring()
    path = tmp_path / "catalog.json"
    _write_catalog(path, legacy_v1_catalog_document(private_key))

    with pytest.raises(CatalogVerificationError, match="^catalog_invalid$"):
        verify_plugin_catalog(path, ring)


def test_catalog_v1_signature_stays_verifiable_outside_the_v2_parser(
    tmp_path: Path,
) -> None:
    """Catches a v2 upgrade that also destroys prior signed-release evidence."""
    private_key, ring = ephemeral_publisher_ring()
    document = legacy_v1_catalog_document(private_key)
    unsigned = {key: value for key, value in document.items() if key != "signature"}

    signature = document["signature"]
    assert isinstance(signature, str)
    verify_signed_bytes(
        canonical_bytes(unsigned),
        signature,
        ring,
        key_id="test-plugin-key",
        publisher_id="test.publisher",
        subject_id="test.catalog",
    )


@pytest.mark.parametrize("field", sorted(valid_catalog_entry_document()))
def test_marketplace_v2_entry_requires_every_exact_key(field: str) -> None:
    """Catches optional-by-omission v2 metadata at the signed catalog boundary."""
    raw = valid_catalog_entry_document()
    del raw[field]

    with pytest.raises(ValueError, match="^catalog_invalid$"):
        MarketplaceCatalogEntry.from_dict(raw)


def test_marketplace_v2_entry_rejects_unknown_keys() -> None:
    """Catches open v2 entry parsing that carries unreviewed publisher fields."""
    with pytest.raises(ValueError, match="^catalog_invalid$"):
        MarketplaceCatalogEntry.from_dict(_entry_document(vendor_extension="secret"))


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"title": ""}, "title"),
        ({"title": "t" * 81}, "title"),
        ({"title": " Workspace"}, "title"),
        ({"summary": ""}, "summary"),
        ({"summary": "s" * 281}, "summary"),
        ({"categories": []}, "categories"),
        ({"categories": ["a", "b", "c", "d", "e"]}, "categories"),
        ({"categories": ["productivity", "productivity"]}, "categories"),
        ({"categories": ["productivity", "development"]}, "categories"),
        ({"categories": ["Productivity"]}, "categories"),
        ({"categories": "productivity"}, "categories"),
        ({"search_terms": ["a"] * 13}, "search_terms"),
        ({"search_terms": ["workspace", "workspace"]}, "search_terms"),
        ({"search_terms": ["workspace", "alpha"]}, "search_terms"),
        ({"search_terms": ["Workspace"]}, "search_terms"),
        ({"icon_asset_path": "../outside/icon.svg"}, "icon_asset_path"),
        ({"icon_asset_path": "C:/icon.svg"}, "icon_asset_path"),
        ({"icon_asset_path": ""}, "icon_asset_path"),
        ({"support_url": "http://support.example.test/workspace"}, "support_url"),
        ({"support_url": "https://support.example.test:443/x"}, "support_url"),
        ({"support_url": "https://support.example.test/x?y=1"}, "support_url"),
        ({"source_url": None}, "source_url"),
        ({"source_url": "https://user@source.example.test/x"}, "source_url"),
        ({"source_url": "https://source.example.test/../x"}, "source_url"),
        ({"trust_tier": "official"}, "trust_tier"),
        ({"trust_tier": None}, "trust_tier"),
        ({"provenance_sha256": "C" * 64}, "provenance_sha256"),
        ({"provenance_sha256": "c" * 63}, "provenance_sha256"),
        ({"released_at": "2026-08-28"}, "released_at"),
        ({"released_at": "2026-08-28T00:00:00+00:00"}, "released_at"),
        ({"released_at": "2026-13-28T00:00:00Z"}, "released_at"),
        ({"pricing": "trial"}, "pricing"),
        ({"offline_capable": "false"}, "offline_capable"),
        ({"offline_capable": 0}, "offline_capable"),
        ({"update_notes_sha256": "not-a-digest"}, "update_notes_sha256"),
        ({"plugin_version": "1.0"}, "plugin_version"),
    ],
)
def test_marketplace_v2_entry_rejects_out_of_bound_metadata(
    changes: dict[str, object], field: str
) -> None:
    """Catches unbounded or non-canonical v2 display, trust, and release metadata."""
    assert field in valid_catalog_entry_document()

    with pytest.raises(ValueError, match="^catalog_invalid$"):
        MarketplaceCatalogEntry.from_dict(_entry_document(**changes))


@pytest.mark.parametrize(
    "nullable",
    ["icon_asset_path", "support_url", "update_notes_sha256"],
)
def test_marketplace_v2_entry_admits_only_declared_absent_metadata(
    nullable: str,
) -> None:
    """Catches absent optional metadata being coerced into a fabricated value."""
    entry = MarketplaceCatalogEntry.from_dict(_entry_document(**{nullable: None}))

    assert getattr(entry, nullable) is None


@pytest.mark.parametrize(
    ("distribution", "admitted"),
    [
        ({"kind": "remote_package"}, True),
        ({"kind": "remote_mcp", "remote_origin": "https://mcp.example.test/mcp/v1"}, True),
        ({"kind": "portable_package", "artifact_sha256": "f" * 64}, True),
        ({"kind": "remote_package", "remote_origin": "https://mcp.example.test"}, False),
        ({"kind": "remote_mcp"}, False),
        ({"kind": "portable_package"}, False),
        ({"kind": "remote_mcp", "remote_origin": "http://mcp.example.test"}, False),
        ({"kind": "portable_package", "artifact_sha256": "zz"}, False),
        ({"kind": "sideloaded"}, False),
        ("remote_package", False),
    ],
)
def test_marketplace_v2_distribution_is_exact_for_each_kind(
    distribution: object, admitted: bool
) -> None:
    """Catches a distribution object that accepts metadata of another kind."""
    raw = _entry_document(distribution=distribution)

    if admitted:
        assert isinstance(distribution, dict)
        parsed = MarketplaceCatalogEntry.from_dict(raw)
        assert parsed.distribution.kind is DistributionKind(distribution["kind"])
        assert parsed.to_dict()["distribution"] == distribution
        return
    with pytest.raises(ValueError, match="^catalog_invalid$"):
        MarketplaceCatalogEntry.from_dict(raw)


@pytest.mark.parametrize(
    "history",
    [
        [{"plugin_version": "0.9.0", "package_sha256": "d" * 64}] * 2,
        [{"plugin_version": "1.0.0", "package_sha256": "d" * 64}],
        [{"plugin_version": "9.0.0", "package_sha256": "d" * 64}],
        [
            {"plugin_version": f"0.{index}.0", "package_sha256": "d" * 64}
            for index in range(9)
        ],
        [{"plugin_version": "0.9.0", "package_sha256": "d" * 64, "notes": "x"}],
        [{"plugin_version": "0.9.0"}],
        [
            {"plugin_version": "0.8.0", "package_sha256": "d" * 64},
            {"plugin_version": "0.9.0", "package_sha256": "e" * 64},
        ],
    ],
)
def test_marketplace_v2_release_history_is_bounded_and_ordered(
    history: list[dict[str, object]],
) -> None:
    """Catches duplicate, current, unbounded, open, or unordered release history."""
    with pytest.raises(ValueError, match="^catalog_invalid$"):
        MarketplaceCatalogEntry.from_dict(_entry_document(release_history=history))


def test_marketplace_v2_release_history_admits_bounded_descending_releases() -> None:
    """Catches rejecting the exact bounded newest-first history the catalog ships."""
    history = [
        {"plugin_version": "0.9.0", "package_sha256": "d" * 64},
        {"plugin_version": "0.8.0", "package_sha256": "e" * 64},
    ]

    entry = MarketplaceCatalogEntry.from_dict(_entry_document(release_history=history))

    assert tuple(item.plugin_version for item in entry.release_history) == (
        "0.9.0",
        "0.8.0",
    )
    assert entry.to_dict()["release_history"] == history


def test_marketplace_v2_catalog_version_is_exactly_two(tmp_path: Path) -> None:
    """Catches a catalog admitting any schema version other than the pinned v2."""
    private_key, ring = ephemeral_publisher_ring()
    path = tmp_path / "catalog.json"
    for schema_version in (0, 1, 3, "2", True):
        document = valid_catalog_document(private_key)
        document["schema_version"] = schema_version
        _write_catalog(path, _sign_catalog(document, private_key))
        with pytest.raises(CatalogVerificationError, match="^catalog_invalid$"):
            verify_plugin_catalog(path, ring)

    # RFC 8785 renders 2.0 as the integer 2, so the float is the same document.
    float_document = valid_catalog_document(private_key)
    float_document["schema_version"] = 2.0
    assert canonical_bytes(float_document) == canonical_bytes(
        valid_catalog_document(private_key)
    )

    _write_catalog(path, valid_catalog_document(private_key))
    assert verify_plugin_catalog(path, ring).sequence == 1
