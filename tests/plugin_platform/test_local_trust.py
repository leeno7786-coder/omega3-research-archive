"""Device-local sealing of publisher-verified and personal packages.

Omega never reviewed a publisher-verified or personal plugin's behavior; the
upstream registry only authenticated a namespace. ``LocalTrustRoot`` is the
device-local trust anchor that turns "this device pinned and reviewed this
exact package" into a fact a later install step can trust -- without ever
claiming Omega verification. Every test here proves the seal is portable
(reconstructable from the same vault key on any device), key-bound (a
different vault key can never verify it), and fails closed on tampering,
a locked vault, or an unverified input.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin_platform.catalog import TrustTier
from plugin_platform.local_trust import LocalTrustError, LocalTrustRoot, VerifiedPlugin
from plugin_platform.marketplace_importer import (
    MarketplaceImporter,
    VerifiedNormalizedPluginCandidate,
)
from plugin_platform.mcp_registry import McpRegistryClient, McpRegistryServer

_VAULT_KEY = b"k" * 32
_OTHER_VAULT_KEY = b"z" * 32


def _weather_server(**overrides: object) -> McpRegistryServer:
    """Return one publisher-authenticated remote weather listing."""
    server: dict[str, object] = {
        "name": "io.github.example/weather",
        "title": "Weather",
        "description": "Weather forecasts and severe weather alerts.",
        "version": "1.2.0",
        "remotes": [
            {
                "type": "streamable-http",
                "url": "https://mcp.weather.example.com/mcp/v1",
                "headers": [],
            }
        ],
        **overrides,
    }
    document = {
        "servers": [
            {
                "server": server,
                "_meta": {
                    "io.modelcontextprotocol.registry/official": {
                        "status": "active",
                        "publishedAt": "2026-08-01T10:30:00Z",
                        "isLatest": True,
                    }
                },
            }
        ],
        "metadata": {"count": 1},
    }
    payload = json.dumps(document).encode("utf-8")
    page = McpRegistryClient(transport=lambda _request: payload).search()
    assert len(page.servers) == 1
    return page.servers[0]


def normalized_weather_candidate() -> VerifiedNormalizedPluginCandidate:
    """Return one importer-verified remote-MCP candidate (no artifact store needed)."""
    importer = MarketplaceImporter()
    candidate = importer.normalize(_weather_server(), "1.2.0")
    return importer.verify(candidate)


def test_local_seal_is_portable_key_bound_and_never_omega_verified() -> None:
    root = LocalTrustRoot.from_vault_key(b"k" * 32)
    sealed = root.seal_package(normalized_weather_candidate())
    assert sealed.trust_tier == "publisher_verified"
    assert root.verify_package(sealed).package_sha256 == sealed.package_sha256
    with pytest.raises(LocalTrustError):
        LocalTrustRoot.from_vault_key(b"z" * 32).verify_package(sealed)


def test_seal_is_reconstructable_from_the_same_vault_key_on_a_fresh_root() -> None:
    """Catches secret material silently persisting instead of being rederived."""
    sealed = LocalTrustRoot.from_vault_key(_VAULT_KEY).seal_package(
        normalized_weather_candidate()
    )
    reconstructed_root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    verified = reconstructed_root.verify_package(sealed)
    assert verified.plugin_id == sealed.plugin_id
    assert verified.package_sha256 == sealed.package_sha256


def test_seal_package_rejects_anything_but_a_verified_candidate() -> None:
    """Catches sealing an unproven candidate or an unrelated object."""
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    with pytest.raises(LocalTrustError):
        root.seal_package(object())
    importer = MarketplaceImporter()
    unverified = importer.normalize(_weather_server(), "1.2.0")
    with pytest.raises(LocalTrustError):
        root.seal_package(unverified)


def test_tampered_seal_fails_closed() -> None:
    """Catches a mutated field going unnoticed because only the signature was checked."""
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    sealed = root.seal_package(normalized_weather_candidate())
    tampered = dataclasses.replace(sealed, package_sha256="0" * 64)
    with pytest.raises(LocalTrustError):
        root.verify_package(tampered)


def test_tampered_signature_fails_closed() -> None:
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    sealed = root.seal_package(normalized_weather_candidate())
    corrupt_signature = ("A" if sealed.signature[0] != "A" else "B") + sealed.signature[1:]
    tampered = dataclasses.replace(sealed, signature=corrupt_signature)
    with pytest.raises(LocalTrustError):
        root.verify_package(tampered)


def test_locked_vault_fails_closed_for_seal_and_verify() -> None:
    """Catches secret material surviving past an explicit lock."""
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    sealed = root.seal_package(normalized_weather_candidate())
    root.lock()
    with pytest.raises(LocalTrustError):
        root.seal_package(normalized_weather_candidate())
    with pytest.raises(LocalTrustError):
        root.verify_package(sealed)


@pytest.mark.parametrize("vault_key", [b"short", b"", b"x" * 31, b"x" * 33, "not-bytes", None])
def test_vault_key_must_be_exactly_32_bytes(vault_key: object) -> None:
    with pytest.raises(LocalTrustError):
        LocalTrustRoot.from_vault_key(vault_key)


def test_verify_package_rejects_a_non_verified_plugin_object() -> None:
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    with pytest.raises(LocalTrustError):
        root.verify_package(object())


def test_authority_seal_is_key_bound_and_tamper_evident() -> None:
    """Catches the independent authority-MAC key being derived from -- or equal to -- the seal key."""
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    document = {"tools": {"forecast": {"risk_class": "external_unreviewed"}}}
    sealed = root.seal_authority(document)
    root.verify_authority(document, sealed)
    with pytest.raises(LocalTrustError):
        root.verify_authority({"tools": {}}, sealed)
    with pytest.raises(LocalTrustError):
        LocalTrustRoot.from_vault_key(_OTHER_VAULT_KEY).verify_authority(document, sealed)


def test_locked_vault_fails_closed_for_authority_sealing() -> None:
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    document = {"tools": {}}
    sealed = root.seal_authority(document)
    root.lock()
    with pytest.raises(LocalTrustError):
        root.seal_authority(document)
    with pytest.raises(LocalTrustError):
        root.verify_authority(document, sealed)


def test_authority_seal_key_id_is_stable_across_roots_with_the_same_vault_key() -> None:
    """Catches the persisted key id drifting between two roots built from the
    identical vault key -- it must be a pure function of the vault key."""
    document = {"tools": {"forecast": {"risk_class": "external_unreviewed"}}}
    first = LocalTrustRoot.from_vault_key(_VAULT_KEY).seal_authority(document)
    second = LocalTrustRoot.from_vault_key(_VAULT_KEY).seal_authority(document)
    assert first.mac_key_sha256 == second.mac_key_sha256


def test_authority_seal_key_id_differs_across_vault_keys() -> None:
    document = {"tools": {"forecast": {"risk_class": "external_unreviewed"}}}
    same_key = LocalTrustRoot.from_vault_key(_VAULT_KEY).seal_authority(document)
    other_key = LocalTrustRoot.from_vault_key(_OTHER_VAULT_KEY).seal_authority(document)
    assert same_key.mac_key_sha256 != other_key.mac_key_sha256


def test_authority_seal_key_id_is_never_the_raw_authority_key_hash() -> None:
    """The persisted key id must never be ``sha256(the live HMAC secret)`` --
    that would publish a hash of the actual key material used to compute
    every authority MAC next to every sealed document. It must come from its
    own, independent HKDF-derived branch off the vault key instead."""
    from hashlib import sha256

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    document = {"tools": {"forecast": {"risk_class": "external_unreviewed"}}}
    sealed = LocalTrustRoot.from_vault_key(_VAULT_KEY).seal_authority(document)

    raw_authority_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"omega/plugin-authority-seal/v1",
    ).derive(_VAULT_KEY)
    raw_authority_key_hash = sha256(raw_authority_key).hexdigest()

    assert sealed.mac_key_sha256 != raw_authority_key_hash


def test_verified_plugin_is_immutable_and_content_free_on_construction_failure() -> None:
    with pytest.raises(LocalTrustError):
        VerifiedPlugin(
            plugin_id="not an identifier!!",
            publisher_id="example",
            plugin_version="1.2.0",
            trust_tier=TrustTier.PUBLISHER_VERIFIED,
            distribution="remote_mcp",
            manifest_sha256="a" * 64,
            package_sha256="b" * 64,
            artifact_sha256=None,
            seal_public_key_sha256="c" * 64,
            signature="not-base64!",
        )


# --- materialize_sealed_package -----------------------------------------


def _weather_envelope(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "plugin_id": "io.github.example.weather",
        "app_id": "io.github.example.weather",
        "capability_authority": {
            "tools": {
                "forecast": {
                    "title": "Forecast",
                    "risk_class": "external_unreviewed",
                    "schema_sha256": "a" * 64,
                    "publisher_annotations": {},
                }
            },
            "resources": {},
            "resource_templates": {},
            "prompts": {},
        },
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_materialize_sealed_package_produces_a_reverifiable_tree(tmp_path: Path) -> None:
    """The full round trip: materialize, then verify through the public API."""
    from plugin_platform.package_verifier import verify_locally_sealed_plugin_package

    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    destination = tmp_path / "staged"

    tree = root.materialize_sealed_package(candidate, _weather_envelope(), destination)

    assert tree.plugin_id == "io.github.example.weather"
    assert (destination / "plugin.json").is_file()
    assert (destination / "tools" / "authority.json").is_file()
    assert (destination / "local_seal.json").is_file()

    verified = verify_locally_sealed_plugin_package(destination, root)
    assert verified.trust_tier is TrustTier.PUBLISHER_VERIFIED
    assert verified.manifest.plugin_id == "io.github.example.weather"
    assert verified.capability_authority["io.github.example.weather"]["tools"]["forecast"][
        "risk_class"
    ] == "external_unreviewed"


def test_materialized_seal_never_persists_the_raw_authority_key_hash(tmp_path: Path) -> None:
    """The durable ``local_seal.json``'s ``authority_mac_key_sha256`` must be
    the same non-secret key id ``seal_authority`` produces, never
    ``sha256(the live HMAC secret)``."""
    from hashlib import sha256

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    destination = tmp_path / "staged"
    root.materialize_sealed_package(candidate, _weather_envelope(), destination)

    document = json.loads((destination / "local_seal.json").read_text("utf-8"))
    raw_authority_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"omega/plugin-authority-seal/v1",
    ).derive(_VAULT_KEY)
    raw_authority_key_hash = sha256(raw_authority_key).hexdigest()
    assert document["authority_mac_key_sha256"] != raw_authority_key_hash


def test_materialize_sealed_package_rejects_a_mismatched_envelope(tmp_path: Path) -> None:
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    with pytest.raises(LocalTrustError):
        root.materialize_sealed_package(
            candidate,
            _weather_envelope(plugin_id="not.the.right.plugin"),
            tmp_path / "staged",
        )


def test_materialize_sealed_package_rejects_a_reparse_point_destination(tmp_path: Path) -> None:
    """A destination path through a symlink/junction fails closed, never follows it."""
    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    real_target = tmp_path / "real"
    real_target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real_target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not permitted in this environment")
    with pytest.raises(LocalTrustError):
        root.materialize_sealed_package(
            candidate, _weather_envelope(), link / "staged"
        )


def test_verify_locally_sealed_package_rejects_a_different_vault_key(tmp_path: Path) -> None:
    from plugin_platform.package_verifier import (
        PackageVerificationError,
        verify_locally_sealed_plugin_package,
    )

    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    destination = tmp_path / "staged"
    root.materialize_sealed_package(candidate, _weather_envelope(), destination)

    other_root = LocalTrustRoot.from_vault_key(_OTHER_VAULT_KEY)
    with pytest.raises(PackageVerificationError, match="^seal_invalid$"):
        verify_locally_sealed_plugin_package(destination, other_root)


def test_verify_locally_sealed_package_rejects_a_tampered_local_seal(tmp_path: Path) -> None:
    from plugin_platform.package_verifier import (
        PackageVerificationError,
        verify_locally_sealed_plugin_package,
    )

    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    destination = tmp_path / "staged"
    root.materialize_sealed_package(candidate, _weather_envelope(), destination)

    seal_path = destination / "local_seal.json"
    document = json.loads(seal_path.read_text("utf-8"))
    document["plugin_version"] = "9.9.9"
    seal_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(PackageVerificationError, match="^seal_invalid$"):
        verify_locally_sealed_plugin_package(destination, root)


def test_verify_locally_sealed_package_rejects_a_tampered_authority(tmp_path: Path) -> None:
    from plugin_platform.package_verifier import (
        PackageVerificationError,
        verify_locally_sealed_plugin_package,
    )

    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    destination = tmp_path / "staged"
    root.materialize_sealed_package(candidate, _weather_envelope(), destination)

    authority_path = destination / "tools" / "authority.json"
    document = json.loads(authority_path.read_text("utf-8"))
    document["apps"]["io.github.example.weather"]["tools"]["forecast"]["title"] = "Tampered"
    authority_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(PackageVerificationError, match="^asset_digest_mismatch$"):
        verify_locally_sealed_plugin_package(destination, root)


def test_verify_locally_sealed_package_rejects_a_missing_local_seal(tmp_path: Path) -> None:
    """A package tree that dropped ``local_seal.json`` fails closed with a stable cause."""
    from plugin_platform.package_verifier import (
        PackageVerificationError,
        verify_locally_sealed_plugin_package,
    )

    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    destination = tmp_path / "staged"
    root.materialize_sealed_package(candidate, _weather_envelope(), destination)

    (destination / "local_seal.json").unlink()

    with pytest.raises(PackageVerificationError) as exc_info:
        verify_locally_sealed_plugin_package(destination, root)
    assert str(exc_info.value) == "package_tree_invalid"


def test_verify_locally_sealed_package_never_grants_omega_verified(tmp_path: Path) -> None:
    """A seal document that claims omega_verified still fails closed."""
    from plugin_platform.package_verifier import (
        PackageVerificationError,
        verify_locally_sealed_plugin_package,
    )

    root = LocalTrustRoot.from_vault_key(_VAULT_KEY)
    candidate = normalized_weather_candidate()
    destination = tmp_path / "staged"
    root.materialize_sealed_package(candidate, _weather_envelope(), destination)

    seal_path = destination / "local_seal.json"
    document = json.loads(seal_path.read_text("utf-8"))
    document["trust_tier"] = "omega_verified"
    seal_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(PackageVerificationError, match="^seal_invalid$"):
        verify_locally_sealed_plugin_package(destination, root)
