from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
from plugin_platform.package_verifier import verify_plugin_package
from plugin_platform.trust import load_embedded_key_ring
from tests.plugin_platform.support import build_signed_plugin, ephemeral_publisher_ring
from tests.plugin_platform.test_package_verifier import (
    _add_asset,
    _assert_rejected,
    _manifest,
    _write_signed_manifest,
)


def _authority(apps: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": 2,
        "plugin_id": "test.workspace",
        "apps": {} if apps is None else apps,
    }


def _empty_app_authority() -> dict[str, object]:
    return {
        "tools": {},
        "resources": {},
        "resource_templates": {},
        "prompts": {},
    }


def _add_empty_remote_app(package: Path, private_key: Ed25519PrivateKey) -> str:
    app_id = "provider.workspace.mcp"
    manifest = _manifest(package)
    manifest["kind"] = "mcp_backed"
    manifest["entry_points"][0]["required_app_ids"] = [app_id]
    manifest["required_apps"] = [
        {
            "app_id": app_id,
            "publisher_id": "provider.publisher",
            "deployment": "remote",
            "provider_id": "provider.workspace",
            "protocol_version": "2026-07-28",
            "transports": ["streamable_http"],
            "remote_origin": "https://provider.example.test/mcp/v1",
            "authorization_issuer": "https://auth.example.test",
            "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": [],
        }
    ]
    _write_signed_manifest(package, private_key, manifest)
    return app_id


def test_capability_authority_asset_must_be_canonical_json() -> None:
    from plugin_platform.contracts import AssetKind

    assert AssetKind.CAPABILITY_AUTHORITY.value == "capability_authority"


def test_capability_authority_asset_path_must_be_tools_authority_json(tmp_path: Path) -> None:
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    _add_asset(
        package,
        private_key,
        relative_path="authority.json",
        kind="capability_authority",
        media_type="application/json",
        payload=canonical_bytes(_authority()),
    )
    _assert_rejected(package, ring, "asset_invalid")


def test_capability_authority_rejects_noncanonical_json(tmp_path: Path) -> None:
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    _add_asset(
        package,
        private_key,
        relative_path="tools/authority.json",
        kind="capability_authority",
        media_type="application/json",
        payload=b'{ "schema_version": 2 }',
    )
    _assert_rejected(package, ring, "asset_invalid")


def test_canonical_capability_authority_is_admitted(tmp_path: Path) -> None:
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    payload = canonical_bytes(_authority())
    _add_asset(
        package,
        private_key,
        relative_path="tools/authority.json",
        kind="capability_authority",
        media_type="application/json",
        payload=payload,
    )
    verified = verify_plugin_package(package, ring, "3.0.0")
    assert verified.asset_bytes["tools/authority.json"] == payload
    assert hashlib.sha256(payload).hexdigest() in {
        asset.sha256
        for asset in verified.manifest.assets
        if asset.path == "tools/authority.json"
    }


def test_provider_neutral_runtime_compatibility_is_closed_signed_asset_data(
    tmp_path: Path,
) -> None:
    """Catches runtime compatibility becoming an unsigned provider/host branch."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    app_id = _add_empty_remote_app(package, private_key)
    compatibility = {
        "wire_protocol_version": "2025-11-25",
        "handshake_mode": "legacy",
        "missing_metadata": {
            "server_id": "StatelessServer",
            "server_version": "ESF",
        },
    }
    app_authority = _empty_app_authority()
    app_authority["runtime_compatibility"] = compatibility
    payload = canonical_bytes(_authority({app_id: app_authority}))
    _add_asset(
        package,
        private_key,
        relative_path="tools/authority.json",
        kind="capability_authority",
        media_type="application/json",
        payload=payload,
    )

    verified = verify_plugin_package(package, ring, "3.0.0")
    assert verified.asset_bytes["tools/authority.json"] == payload

    invalid_package = build_signed_plugin(tmp_path / "invalid", private_key)
    assert _add_empty_remote_app(invalid_package, private_key) == app_id
    compatibility["provider_id"] = "forbidden.branch"
    _add_asset(
        invalid_package,
        private_key,
        relative_path="tools/authority.json",
        kind="capability_authority",
        media_type="application/json",
        payload=canonical_bytes(_authority({app_id: app_authority})),
    )
    _assert_rejected(invalid_package, ring, "asset_invalid")


def test_capability_authority_rejects_authentication_ownership(tmp_path: Path) -> None:
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    app_id = _add_empty_remote_app(package, private_key)
    authority = _empty_app_authority()
    authority["oauth_scopes"] = ["scope.read"]
    _add_asset(
        package,
        private_key,
        relative_path="tools/authority.json",
        kind="capability_authority",
        media_type="application/json",
        payload=canonical_bytes(_authority({app_id: authority})),
    )

    _assert_rejected(package, ring, "asset_invalid")


@pytest.mark.parametrize(
    "plugin_id",
    (
        "github.official",
        "google.workspace.calendar",
        "google.workspace.drive",
        "google.workspace.gmail",
    ),
)
def test_marketplace_authorities_are_signed_v2_app_partitions(plugin_id: str) -> None:
    package = Path("plugin_platform/marketplace/packages") / plugin_id
    verified = verify_plugin_package(package, load_embedded_key_ring(), "3.0.0")
    authority = json.loads(verified.asset_bytes["tools/authority.json"])

    assert set(authority) == {"schema_version", "plugin_id", "apps"}
    assert authority["schema_version"] == 2
    assert authority["plugin_id"] == plugin_id
    assert set(authority["apps"]) == {
        requirement.app_id
        for requirement in (*verified.manifest.required_apps, *verified.manifest.optional_apps)
    }
    for app_authority in authority["apps"].values():
        assert {
            "tools",
            "resources",
            "resource_templates",
            "prompts",
        }.issubset(app_authority)
