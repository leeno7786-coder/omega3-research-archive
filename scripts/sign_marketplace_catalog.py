"""Sign marketplace plugin packages and catalog with the operator-held Ed25519 seed."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_platform.canonical import canonical_bytes
from mcp_platform.desktop_secrets import _dpapi_unprotect
from mcp_platform.sealed_secrets import (
    FEATURED_SIGNING_SEED_PATH,
    FEATURED_SIGNING_SEED_PURPOSE,
    SealedSecretError,
    read_sealed_seed,
    require_passphrase,
    write_sealed_file,
)
from plugin_platform.catalog import (
    CATALOG_SCHEMA_VERSION,
    MARKETPLACE_CATALOG_PATH,
    MARKETPLACE_PACKAGES_ROOT,
    MarketplaceCatalogEntry,
)
from plugin_platform.contracts import PluginManifest
from plugin_platform.marketplace_provenance import (
    BUNDLED_PROVENANCE_PATH,
    provenance_digest,
    verify_provider_provenance,
)
from plugin_platform.package_verifier import verify_plugin_package
from plugin_platform.trust import load_embedded_key_ring

MARKETPLACE_KEY_ID = "omega-featured-2026-09-06-r2"
MARKETPLACE_PUBLISHER_ID = "omega.first_party"
PACKAGE_URI_PREFIX = "https://plugins.omega2ai.com/marketplace/packages"
_DEFAULT_SEED_SEALED_PATH = FEATURED_SIGNING_SEED_PATH
_OMEGA_VERSION = "3.0.0"


def marketplace_signing_key_from_seed(
    seed: bytes,
    *,
    subject_id: str,
    key_ring=None,
) -> Ed25519PrivateKey:
    """Return the embedded marketplace signer only for its exact raw seed."""

    if type(seed) is not bytes or len(seed) != 32:
        raise ValueError("marketplace_signing_key_invalid")
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
    except (TypeError, ValueError):
        raise ValueError("marketplace_signing_key_invalid") from None
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    ring = load_embedded_key_ring() if key_ring is None else key_ring
    expected = ring.public_key_bytes(
        MARKETPLACE_KEY_ID,
        MARKETPLACE_PUBLISHER_ID,
        subject_id,
    )
    if public_bytes != expected:
        raise ValueError("marketplace_signing_key_mismatch")
    return private_key


def _signer_from_seed(seed: bytes) -> Ed25519PrivateKey:
    try:
        return marketplace_signing_key_from_seed(seed, subject_id="omega.featured")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def _seed_from_environment() -> bytes | None:
    encoded = os.environ.get("OMEGA_FEATURED_PLUGIN_SIGNING_KEY_B64", "").strip()
    if not encoded:
        return None
    try:
        seed = base64.b64decode(encoded, validate=True)
        if base64.b64encode(seed).decode("ascii") != encoded or len(seed) != 32:
            raise ValueError
    except (TypeError, ValueError):
        raise SystemExit("marketplace_signing_key_invalid") from None
    return seed


def _seed_from_dpapi(path: Path) -> bytes:
    """Unprotect the operator ciphertext in memory; never persist the plaintext."""

    try:
        ciphertext = Path(path).read_bytes()
    except OSError:
        raise SystemExit("marketplace_signing_seed_unreadable") from None
    try:
        seed = _dpapi_unprotect(ciphertext)
    except (OSError, ValueError):
        raise SystemExit("marketplace_signing_seed_locked") from None
    if type(seed) is not bytes or len(seed) != 32:
        raise SystemExit("marketplace_signing_key_invalid")
    return seed


def _seed_from_sealed(path: Path) -> bytes:
    """Open the passphrase-sealed portable seed in memory; never persist the plaintext."""
    try:
        return read_sealed_seed(Path(path), purpose=FEATURED_SIGNING_SEED_PURPOSE)
    except SealedSecretError as exc:
        reason = str(exc)
        if reason == "sealed_secret_unreadable":
            raise SystemExit("marketplace_signing_seed_unreadable") from None
        if reason in ("sealed_passphrase_required", "sealed_secret_locked"):
            raise SystemExit("marketplace_signing_seed_locked") from None
        raise SystemExit("marketplace_signing_key_invalid") from None


def _mint_seed_sealed(path: Path) -> str:
    """Generate the signer in-process and seal it under the operator passphrase.

    Prints only the public key; the seed never touches stdout, the
    environment, or a temporary file. Refuses to overwrite an existing seed.
    """
    target = Path(path)
    if target.exists():
        raise SystemExit("marketplace_signing_seed_exists")
    try:
        passphrase = require_passphrase()
    except SealedSecretError:
        raise SystemExit("marketplace_signing_seed_locked") from None
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    try:
        write_sealed_file(target, seed, passphrase, purpose=FEATURED_SIGNING_SEED_PURPOSE)
    except SealedSecretError as exc:
        raise SystemExit(f"marketplace_signing_seed_{str(exc).removeprefix('sealed_secret_')}") from None
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(public_key).decode("ascii")


def _load_signer(
    seed_dpapi_path: str | None,
    seed_sealed_path: str | None = None,
) -> Ed25519PrivateKey:
    """Explicit custody first, then the environment, then the drive's sealed seed."""
    if seed_dpapi_path is not None:
        return _signer_from_seed(_seed_from_dpapi(Path(seed_dpapi_path)))
    if seed_sealed_path is not None:
        return _signer_from_seed(_seed_from_sealed(Path(seed_sealed_path)))
    seed = _seed_from_environment()
    if seed is None and _DEFAULT_SEED_SEALED_PATH.exists():
        seed = _seed_from_sealed(_DEFAULT_SEED_SEALED_PATH)
    if seed is None:
        raise SystemExit(
            "supply --seed-sealed-path, --seed-dpapi-path or OMEGA_FEATURED_PLUGIN_SIGNING_KEY_B64"
        )
    return _signer_from_seed(seed)


def sign_marketplace_manifest(
    document: dict[str, object],
    private_key: Ed25519PrivateKey,
    *,
    package_path: Path | None = None,
) -> dict[str, object]:
    """Sign one marketplace manifest under the current immutable key identity."""

    unsigned = PluginManifest.from_dict(document).unsigned_dict()
    if package_path is not None:
        root = Path(package_path)
        for asset in unsigned["assets"]:
            asset_path = root / asset["path"]
            try:
                asset["sha256"] = sha256(asset_path.read_bytes()).hexdigest()
            except (OSError, TypeError, ValueError):
                raise ValueError("marketplace_asset_invalid") from None
    unsigned["signing_key_id"] = MARKETPLACE_KEY_ID
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature}


def sign_marketplace_provenance(
    document: dict[str, object],
    private_key: Ed25519PrivateKey,
) -> dict[str, object]:
    """Sign the exact provider-provenance document under the current key."""

    expected = {
        "schema_version",
        "catalog_id",
        "publisher_id",
        "sequence",
        "records",
        "signing_key_id",
        "signature",
    }
    if type(document) is not dict or set(document) != expected:
        raise ValueError("marketplace_provenance_invalid")
    unsigned = {
        "schema_version": document["schema_version"],
        "catalog_id": document["catalog_id"],
        "publisher_id": document["publisher_id"],
        "sequence": document["sequence"],
        "records": document["records"],
        "signing_key_id": MARKETPLACE_KEY_ID,
    }
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature}


def marketplace_package_paths(document: dict[str, object]) -> dict[str, Path]:
    """Derive canonical package directories only from catalog plugin identities."""

    if type(document) is not dict or type(document.get("entries")) is not list:
        raise ValueError("marketplace_catalog_invalid")
    paths: dict[str, Path] = {}
    try:
        for raw in document["entries"]:
            entry = MarketplaceCatalogEntry.from_dict(raw)
            if entry.plugin_id in paths:
                raise ValueError
            paths[entry.plugin_id] = MARKETPLACE_PACKAGES_ROOT / entry.plugin_id
    except (TypeError, ValueError):
        raise ValueError("marketplace_catalog_invalid") from None
    if not paths:
        raise ValueError("marketplace_catalog_invalid")
    return paths


def select_marketplace_package_ids(requested: Sequence[str] | None) -> tuple[str, ...]:
    """Resolve an explicit rotation subset, or every marketplace package by default."""

    try:
        document = json.loads(MARKETPLACE_CATALOG_PATH.read_bytes().decode("utf-8"))
        package_paths = marketplace_package_paths(document)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("marketplace_catalog_invalid") from None
    if requested is None:
        return tuple(package_paths)
    selected = tuple(dict.fromkeys(requested))
    if not selected or any(plugin_id not in package_paths for plugin_id in selected):
        raise ValueError("marketplace_package_unknown")
    return selected


def marketplace_package_uri(plugin_id: str, plugin_version: str) -> str:
    """Return the one immutable release URI a catalogued version may declare."""

    return f"{PACKAGE_URI_PREFIX}/{plugin_id}/{plugin_version}"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-id", action="append", dest="plugin_ids")
    parser.add_argument(
        "--seed-dpapi-path",
        type=Path,
        dest="seed_dpapi_path",
        help="read an explicitly named operator DPAPI-protected seed instead of the environment",
    )
    parser.add_argument(
        "--seed-sealed-path",
        nargs="?",
        const=str(_DEFAULT_SEED_SEALED_PATH),
        default=None,
        dest="seed_sealed_path",
        help="read the passphrase-sealed portable seed (OMEGA_SEAL_PASSPHRASE)",
    )
    parser.add_argument(
        "--mint-seed-sealed-path",
        nargs="?",
        const=str(_DEFAULT_SEED_SEALED_PATH),
        default=None,
        dest="mint_seed_sealed_path",
        help="mint a new signer sealed under OMEGA_SEAL_PASSPHRASE, print its public key, exit",
    )
    parser.add_argument(
        "--catalog-only",
        action="store_true",
        dest="catalog_only",
        help="re-sign the provenance and catalog only; leave every package's signed bytes as shipped",
    )
    options = parser.parse_args(argv)
    if options.mint_seed_sealed_path is not None:
        print(f"public_key_base64={_mint_seed_sealed(Path(options.mint_seed_sealed_path))}")
        return
    if options.catalog_only and options.plugin_ids:
        raise SystemExit("marketplace_catalog_only_excludes_packages")
    try:
        selected_ids = (
            frozenset()
            if options.catalog_only
            else frozenset(select_marketplace_package_ids(options.plugin_ids))
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    private_key = _load_signer(options.seed_dpapi_path, options.seed_sealed_path)
    ring = load_embedded_key_ring()
    try:
        raw_catalog = json.loads(MARKETPLACE_CATALOG_PATH.read_bytes().decode("utf-8"))
        package_paths = marketplace_package_paths(raw_catalog)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SystemExit("marketplace_catalog_invalid") from exc

    raw_provenance = json.loads(BUNDLED_PROVENANCE_PATH.read_bytes().decode("utf-8"))
    BUNDLED_PROVENANCE_PATH.write_bytes(
        canonical_bytes(sign_marketplace_provenance(raw_provenance, private_key))
    )
    provenance = verify_provider_provenance(BUNDLED_PROVENANCE_PATH, ring)

    verified_by_id: dict[str, object] = {}
    for plugin_id, package_path in package_paths.items():
        manifest_path = package_path / "plugin.json"
        if plugin_id in selected_ids:
            document = json.loads(manifest_path.read_bytes().decode("utf-8"))
            manifest_path.write_bytes(
                canonical_bytes(
                    sign_marketplace_manifest(
                        document,
                        private_key,
                        package_path=package_path,
                    )
                )
            )
        verified = verify_plugin_package(package_path, ring, _OMEGA_VERSION)
        if verified.manifest.plugin_id != plugin_id:
            raise SystemExit("marketplace_package_identity_mismatch")
        provenance.for_verified_plugin(verified)
        verified_by_id[plugin_id] = verified

    entries = []
    for item in raw_catalog["entries"]:
        plugin_id = item["plugin_id"]
        verified = verified_by_id[plugin_id]
        plugin_version = verified.manifest.plugin_version
        if item["plugin_version"] != plugin_version:
            raise SystemExit("marketplace_catalog_version_mismatch")
        if item["package_uri"] != marketplace_package_uri(plugin_id, plugin_version):
            raise SystemExit("marketplace_package_uri_mismatch")
        entries.append(
            {
                **item,
                "manifest_sha256": verified.manifest_sha256,
                "package_sha256": verified.package_sha256,
                "provenance_sha256": provenance_digest(plugin_id),
            }
        )
    unsigned_catalog = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_id": raw_catalog["catalog_id"],
        "publisher_id": raw_catalog["publisher_id"],
        "sequence": raw_catalog["sequence"],
        "entries": [MarketplaceCatalogEntry.from_dict(entry).to_dict() for entry in entries],
        "signing_key_id": MARKETPLACE_KEY_ID,
    }
    catalog_signature = base64.b64encode(
        private_key.sign(canonical_bytes(unsigned_catalog))
    ).decode("ascii")
    MARKETPLACE_CATALOG_PATH.write_bytes(
        canonical_bytes({**unsigned_catalog, "signature": catalog_signature})
    )


if __name__ == "__main__":
    main()
