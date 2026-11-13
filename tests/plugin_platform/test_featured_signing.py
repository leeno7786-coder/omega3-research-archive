from __future__ import annotations

import base64
import json
from hashlib import sha256
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
from plugin_platform.trust import load_embedded_key_ring
import scripts.sign_marketplace_catalog as sign_marketplace_catalog
from scripts.sign_marketplace_catalog import (
    MARKETPLACE_KEY_ID,
    marketplace_package_paths,
    marketplace_signing_key_from_seed,
    marketplace_package_uri,
    select_marketplace_package_ids,
    sign_marketplace_manifest,
    sign_marketplace_provenance,
)
from tests.plugin_platform.support import build_signed_plugin


def test_marketplace_manifest_signing_rebinds_the_rotated_key_id(tmp_path) -> None:
    previous_key = Ed25519PrivateKey.generate()
    package = build_signed_plugin(tmp_path, previous_key)
    document = json.loads((package / "plugin.json").read_bytes())
    rotated_key = Ed25519PrivateKey.generate()

    signed = sign_marketplace_manifest(document, rotated_key)

    assert (
        signed["signing_key_id"]
        == MARKETPLACE_KEY_ID
        == "omega-featured-2026-09-06-r2"
    )
    signature = base64.b64decode(signed["signature"], validate=True)
    unsigned = {key: value for key, value in signed.items() if key != "signature"}
    rotated_key.public_key().verify(signature, canonical_bytes(unsigned))


def test_marketplace_manifest_signing_refreshes_declared_asset_digests(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    package = build_signed_plugin(tmp_path, private_key)
    manifest_path = package / "plugin.json"
    document = json.loads(manifest_path.read_bytes())
    asset_path = package / document["assets"][0]["path"]
    asset_path.write_bytes(b"release-normalized asset\n")

    signed = sign_marketplace_manifest(
        document,
        private_key,
        package_path=package,
    )

    assert signed["assets"][0]["sha256"] == sha256(asset_path.read_bytes()).hexdigest()


def test_marketplace_signing_can_rotate_one_package_without_resigning_others() -> None:
    assert select_marketplace_package_ids(["github.official"]) == ("github.official",)
    assert "google.workspace.drive" in select_marketplace_package_ids(None)
    with pytest.raises(ValueError, match="^marketplace_package_unknown$"):
        select_marketplace_package_ids(["not.a.marketplace.plugin"])


def test_marketplace_signing_discovers_canonical_directories_from_catalog_identity() -> None:
    document = json.loads(
        Path("plugin_platform/marketplace/catalog.json").read_text(encoding="utf-8")
    )

    paths = marketplace_package_paths(document)

    assert tuple(paths) == tuple(entry["plugin_id"] for entry in document["entries"])
    assert all(path.name == plugin_id for plugin_id, path in paths.items())
    assert all(path.parent.name == "packages" for path in paths.values())


def test_marketplace_provenance_signing_binds_rotated_key_identity() -> None:
    private_key = Ed25519PrivateKey.generate()
    document = {
        "schema_version": 1,
        "catalog_id": "omega.marketplace.provenance",
        "publisher_id": "omega.first_party",
        "sequence": 1,
        "records": [],
        "signing_key_id": "previous-key",
        "signature": "previous-signature",
    }

    signed = sign_marketplace_provenance(document, private_key)

    assert signed["signing_key_id"] == MARKETPLACE_KEY_ID
    unsigned = {key: value for key, value in signed.items() if key != "signature"}
    private_key.public_key().verify(
        base64.b64decode(signed["signature"], validate=True),
        canonical_bytes(unsigned),
    )


@pytest.mark.parametrize(
    "key_id",
    [
        "omega-featured-2026-08-26",
        "omega-featured-2026-08-27",
        "omega-featured-2026-08-27-r2",
        "omega-featured-2026-08-28-r1",
        "omega-featured-2026-09-06-r2",
    ],
)
def test_marketplace_rotation_retains_every_nonrevoked_public_key(key_id: str) -> None:
    public_key = load_embedded_key_ring().public_key_bytes(
        key_id,
        "omega.first_party",
        "omega.featured",
    )

    assert len(public_key) == 32


def test_marketplace_package_uri_is_the_pinned_immutable_release_form() -> None:
    """Catches a release URI that does not name the exact relocated package version."""
    document = json.loads(
        Path("plugin_platform/marketplace/catalog.json").read_text(encoding="utf-8")
    )

    for entry in document["entries"]:
        expected = marketplace_package_uri(entry["plugin_id"], entry["plugin_version"])
        assert entry["package_uri"] == expected
        assert expected.startswith("https://plugins.omega2ai.com/marketplace/packages/")


def test_marketplace_signing_seed_fails_closed_without_operator_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a signer that proceeds without the operator-held protected seed."""
    monkeypatch.delenv("OMEGA_FEATURED_PLUGIN_SIGNING_KEY_B64", raising=False)
    monkeypatch.delenv("OMEGA_SEAL_PASSPHRASE_FILE", raising=False)
    monkeypatch.setattr(sign_marketplace_catalog, "_DEFAULT_SEED_SEALED_PATH", tmp_path / "absent")

    with pytest.raises(SystemExit, match="^marketplace_signing_seed_unreadable$"):
        sign_marketplace_catalog._seed_from_dpapi(tmp_path / "absent.dpapi")

    corrupt = tmp_path / "corrupt.dpapi"
    corrupt.write_bytes(b"not-dpapi-ciphertext")
    with pytest.raises(SystemExit, match="^marketplace_signing_seed_locked$"):
        sign_marketplace_catalog._seed_from_dpapi(corrupt)

    with pytest.raises(SystemExit, match="seed-dpapi-path"):
        sign_marketplace_catalog._load_signer(None)


def test_marketplace_signing_rejects_a_seed_outside_the_embedded_key_identity() -> None:
    """Catches signing releases with any key the shipped trust ring does not bind."""
    foreign_seed = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )

    with pytest.raises(ValueError, match="^marketplace_signing_key_mismatch$"):
        marketplace_signing_key_from_seed(foreign_seed, subject_id="omega.featured")
    with pytest.raises(ValueError, match="^marketplace_signing_key_invalid$"):
        marketplace_signing_key_from_seed(foreign_seed[:31], subject_id="omega.featured")


def test_marketplace_signing_seed_opens_from_portable_sealed_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sealed seed depends on the passphrase alone, never on this machine."""
    from mcp_platform import sealed_secrets

    monkeypatch.delenv("OMEGA_FEATURED_PLUGIN_SIGNING_KEY_B64", raising=False)
    monkeypatch.setattr(sign_marketplace_catalog, "_DEFAULT_SEED_SEALED_PATH", tmp_path / "absent")
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_FILE_ENV, raising=False)
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "portable-pw")
    seed = bytes(range(32))
    sealed = tmp_path / "featured.sealed"
    sealed_secrets.write_sealed_file(
        sealed, seed, "portable-pw", purpose=sealed_secrets.FEATURED_SIGNING_SEED_PURPOSE
    )

    assert sign_marketplace_catalog._seed_from_sealed(sealed) == seed
    assert seed not in sealed.read_bytes()

    with pytest.raises(SystemExit, match="^marketplace_signing_seed_unreadable$"):
        sign_marketplace_catalog._seed_from_sealed(tmp_path / "absent.sealed")
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "wrong")
    with pytest.raises(SystemExit, match="^marketplace_signing_seed_locked$"):
        sign_marketplace_catalog._seed_from_sealed(sealed)
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_ENV)
    with pytest.raises(SystemExit, match="^marketplace_signing_seed_locked$"):
        sign_marketplace_catalog._seed_from_sealed(sealed)
    with pytest.raises(SystemExit, match="seed-sealed-path"):
        sign_marketplace_catalog._load_signer(None)


def test_mint_seed_sealed_prints_only_the_public_key_and_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_platform import sealed_secrets

    seed = bytes(range(32))
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    monkeypatch.setattr(sign_marketplace_catalog.Ed25519PrivateKey, "generate", lambda: private_key)
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "portable-pw")
    destination = tmp_path / "custody" / "featured.sealed"

    sign_marketplace_catalog.main(["--mint-seed-sealed-path", str(destination)])

    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    output = capsys.readouterr().out
    assert output == "public_key_base64=" + base64.b64encode(public_key).decode("ascii") + "\n"
    assert base64.b64encode(seed).decode("ascii") not in output
    assert sign_marketplace_catalog._seed_from_sealed(destination) == seed
    with pytest.raises(SystemExit, match="^marketplace_signing_seed_exists$"):
        sign_marketplace_catalog.main(["--mint-seed-sealed-path", str(destination)])
