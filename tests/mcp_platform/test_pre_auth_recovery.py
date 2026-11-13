"""Migration must authenticate old bytes without making v1 executable again."""
from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.package_verifier import verify_plugin_package, PackageVerificationError
from plugin_platform.records import InstalledPluginVersionRecord
from plugin_platform.trust import TrustedPublisherKeyRing, load_embedded_key_ring

ROOT = Path(__file__).resolve().parents[2]


def legacy_fixture(tmp_path, plugin_id="github.official", *, change=None, key=None):
    current = verify_plugin_package(
        ROOT / "plugin_platform/marketplace/packages" / plugin_id,
        load_embedded_key_ring(), "3.0.0",
    )
    raw = current.manifest.to_dict()
    raw["manifest_version"] = 1
    raw["plugin_version"] = "1.0.0"
    auth = raw["required_apps"][0].pop("authentication")
    authority = {
        "oauth_scopes": auth["scopes"],
        "tools": dict(current.capability_authority[raw["required_apps"][0]["app_id"]]["tools"]),
    }
    if change:
        change(raw, authority)
    payloads = dict(current.asset_bytes)
    payloads["tools/authority.json"] = canonical_bytes(authority)
    for asset in raw["assets"]:
        asset["sha256"] = sha256_hex(payloads[asset["path"]])
    key = key or Ed25519PrivateKey.generate()
    raw["signing_key_id"] = "fixture-old"
    raw.pop("signature")
    raw["signature"] = base64.b64encode(key.sign(canonical_bytes(raw))).decode()
    encoded = canonical_bytes(raw)
    package = tmp_path / "old"
    for name, payload in {"plugin.json": encoded, **payloads}.items():
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    record = InstalledPluginVersionRecord(
        plugin_version="1.0.0", signing_key_id="fixture-old",
        manifest_sha256=sha256_hex(encoded),
        package_sha256=sha256_hex(canonical_bytes({
            "manifest_sha256": sha256_hex(encoded),
            "assets": {a["path"]: a["sha256"] for a in raw["assets"]},
        })), state="selected",
    )
    ring_raw = json.loads((ROOT / "plugin_platform/trusted_plugin_keys.json").read_bytes())
    ring_raw["keys"].append({"key_id": "fixture-old", "publisher_id": "omega.first_party",
                  "public_key_base64": base64.b64encode(key.public_key().public_bytes(
                      serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()})
    ring = TrustedPublisherKeyRing.from_dict(ring_raw)
    app = {**raw["required_apps"][0], "local_package_version": None,
           "local_package_platform": None, "local_package_manifest_sha256": None,
           "installed": True, "trusted": True, "enabled": False}
    return package, record, app, current, ring


@pytest.mark.parametrize("plugin_id", [
    "github.official", "google.workspace.drive", "google.workspace.calendar",
])
def test_signed_old_package_yields_only_a_dormant_exact_successor(tmp_path, plugin_id):
    from mcp_platform.pre_auth_recovery import verify_legacy_successor

    package, record, app, current, ring = legacy_fixture(tmp_path, plugin_id)
    proof = verify_legacy_successor(package, record, app, current, ring)

    assert proof.replacement_record.app_id == app["app_id"]
    assert proof.replacement_record.enabled is False
    assert proof.old_manifest_sha256 == record.manifest_sha256
    assert set(proof.replacement_record.authentication.scopes) == set(
        json.loads((package / "tools/authority.json").read_bytes())["oauth_scopes"]
    )
    with pytest.raises(PackageVerificationError, match="manifest_invalid"):
        verify_plugin_package(package, ring, "3.0.0")


@pytest.mark.parametrize("change", [
    lambda raw, authority: authority["oauth_scopes"].append("extra.scope"),
    lambda raw, authority: authority["tools"].clear(),
    lambda raw, authority: raw["required_apps"][0].update(remote_origin="https://other.example/mcp"),
    lambda raw, authority: raw.update(extra_field=True),
])
def test_even_signed_permission_or_identity_deltas_are_not_auto_migrated(tmp_path, change):
    from mcp_platform.pre_auth_recovery import verify_legacy_successor

    args = legacy_fixture(tmp_path, change=change)
    with pytest.raises(ValueError, match="^plugin_legacy_recovery_invalid$"):
        verify_legacy_successor(*args)


@pytest.mark.parametrize("damage", ["asset", "pin", "app", "signature", "noncanonical"])
def test_recovery_rejects_invalid_legacy_evidence(tmp_path, damage):
    from mcp_platform.pre_auth_recovery import verify_legacy_successor

    package, record, app, current, ring = legacy_fixture(tmp_path)
    if damage == "asset":
        (package / "tools/authority.json").write_bytes(b"{}")
    elif damage == "pin":
        record = replace(record, package_sha256="0" * 64)
    elif damage == "app":
        app["remote_origin"] = "https://other.example/mcp"
    elif damage == "signature":
        raw = json.loads((package / "plugin.json").read_bytes())
        raw["signature"] = base64.b64encode(bytes(64)).decode()
        (package / "plugin.json").write_bytes(canonical_bytes(raw))
    else:
        (package / "plugin.json").write_bytes((package / "plugin.json").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="^plugin_legacy_recovery_invalid$"):
        verify_legacy_successor(package, record, app, current, ring)


@pytest.mark.parametrize("damage", [None, "stale", "uncovered", "forged"])
def test_pre_auth_registry_upgrade_requires_exact_verified_coverage(tmp_path, damage):
    from mcp_platform.app_registry import McpAppRegistry
    from mcp_platform.pre_auth_recovery import verify_legacy_successor

    args = legacy_fixture(tmp_path)
    proof = verify_legacy_successor(*args)
    app = args[2]
    root = tmp_path / "state"
    root.mkdir()
    apps = {app["app_id"]: app}
    if damage == "uncovered":
        apps["uncovered.app"] = app
    payload = canonical_bytes({"schema_version": 1, "apps": apps})
    path = root / "apps_registry.json"
    path.write_bytes(payload)
    registry = McpAppRegistry(state_root=root)
    digest = "0" * 64 if damage == "stale" else sha256_hex(payload)
    proofs = (replace(proof),) if damage == "forged" else (proof,)
    if damage:
        with pytest.raises(ValueError, match="^plugin_legacy_recovery_invalid$"):
            registry.upgrade_pre_authentication_exact(expected_payload_sha256=digest, proofs=proofs)
        assert path.read_bytes() == payload
    else:
        snapshot = registry.upgrade_pre_authentication_exact(expected_payload_sha256=digest, proofs=proofs)
        assert snapshot.records == (proof.replacement_record,)
        assert registry.snapshot() == snapshot
        assert registry.get(app["app_id"]).enabled is False


def test_mutable_successor_projection_cannot_hide_signed_permission_changes(tmp_path):
    from mcp_platform.pre_auth_recovery import verify_legacy_successor

    package, record, app, current, ring = legacy_fixture(
        tmp_path, change=lambda raw, authority: authority["tools"].clear(),
    )
    forged_groups = {**current.capability_authority[app["app_id"]], "tools": {}}
    forged = replace(current, capability_authority={app["app_id"]: forged_groups})
    with pytest.raises(ValueError, match="^plugin_legacy_recovery_invalid$"):
        verify_legacy_successor(package, record, app, forged, ring)


@pytest.mark.parametrize("field", ["old_manifest_sha256", "old_record_bytes", "payloads", "replacement"])
def test_issued_proof_cannot_be_mutated_after_verification(tmp_path, field):
    from mcp_platform.pre_auth_recovery import validated_upgrade, verify_legacy_successor

    proof = verify_legacy_successor(*legacy_fixture(tmp_path))
    if field == "replacement":
        object.__setattr__(proof.replacement_record, "capabilities", ())
    else:
        value = {"old_manifest_sha256": "0" * 64, "old_record_bytes": b"{}", "payloads": {}}[field]
        object.__setattr__(proof, field, value)
    with pytest.raises(ValueError, match="^plugin_legacy_recovery_invalid$"):
        validated_upgrade(proof)
