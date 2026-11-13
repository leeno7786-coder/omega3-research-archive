"""Independent signing fixtures for plugin-platform security tests."""

from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
from plugin_platform.trust import TrustedPublisherKeyRing


_SKILL_BYTES = b"# Workspace\n\nUse the connected workspace deliberately.\n"


def ephemeral_publisher_ring() -> tuple[Ed25519PrivateKey, TrustedPublisherKeyRing]:
    """Return a fresh private key and its independently validated public ring."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    document = {
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
    return private_key, TrustedPublisherKeyRing.from_dict(document)


def valid_catalog_entry_document() -> dict[str, object]:
    """Return the pinned marketplace-v2 entry with every exact key present."""
    return {
        "plugin_id": "test.workspace",
        "publisher_id": "test.publisher",
        "plugin_version": "1.0.0",
        "manifest_sha256": "a" * 64,
        "package_sha256": "b" * 64,
        "package_uri": "https://plugins.example.test/test.workspace/1.0.0",
        "title": "Workspace",
        "summary": "Declarative workspace guidance.",
        "categories": ["productivity"],
        "search_terms": ["workspace"],
        "icon_asset_path": "assets/icon.svg",
        "support_url": "https://support.example.test/workspace",
        "source_url": "https://source.example.test/workspace",
        "trust_tier": "omega_verified",
        "distribution": {"kind": "remote_package"},
        "provenance_sha256": "c" * 64,
        "released_at": "2026-08-28T00:00:00Z",
        "pricing": "free",
        "offline_capable": False,
        "release_history": [{"plugin_version": "0.9.0", "package_sha256": "d" * 64}],
        "update_notes_sha256": "e" * 64,
    }


def marketplace_entry_document(
    *,
    plugin_id: str,
    publisher_id: str,
    plugin_version: str,
    manifest_sha256: str,
    package_sha256: str,
    package_uri: str,
    **overrides: object,
) -> dict[str, object]:
    """Return a complete v2 entry wrapped around one exact execution identity."""
    return {
        **valid_catalog_entry_document(),
        "plugin_id": plugin_id,
        "publisher_id": publisher_id,
        "plugin_version": plugin_version,
        "manifest_sha256": manifest_sha256,
        "package_sha256": package_sha256,
        "package_uri": package_uri,
        "release_history": [],
        **overrides,
    }


def valid_catalog_document(private_key: Ed25519PrivateKey) -> dict[str, object]:
    """Return the pinned catalog fixture signed over its literal unsigned shape."""
    unsigned: dict[str, object] = {
        "schema_version": 2,
        "catalog_id": "test.catalog",
        "publisher_id": "test.publisher",
        "sequence": 1,
        "entries": [valid_catalog_entry_document()],
        "signing_key_id": "test-plugin-key",
    }
    signature = private_key.sign(canonical_bytes(unsigned))
    return {
        **deepcopy(unsigned),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def legacy_v1_catalog_document(private_key: Ed25519PrivateKey) -> dict[str, object]:
    """Return a correctly signed schema-1 catalog that v2 must still reject."""
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "catalog_id": "test.catalog",
        "publisher_id": "test.publisher",
        "sequence": 1,
        "entries": [
            {
                "plugin_id": "test.workspace",
                "publisher_id": "test.publisher",
                "plugin_version": "1.0.0",
                "manifest_sha256": "a" * 64,
                "package_sha256": "b" * 64,
                "package_uri": "https://plugins.example.test/test.workspace/1.0.0",
            }
        ],
        "signing_key_id": "test-plugin-key",
    }
    signature = private_key.sign(canonical_bytes(unsigned))
    return {
        **deepcopy(unsigned),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def build_signed_plugin(root: Path, private_key: Ed25519PrivateKey) -> Path:
    """Build one independently signed, declarative skill-only plugin package."""
    package = root / "signed-plugin"
    skill_path = package / "skills" / "workspace" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(_SKILL_BYTES)
    unsigned: dict[str, object] = {
        "manifest_version": 2,
        "plugin_id": "test.workspace",
        "publisher_id": "test.publisher",
        "plugin_version": "1.0.0",
        "minimum_omega_version": "3.0.0",
        "kind": "skill_only",
        "title": "Workspace",
        "description": "Declarative workspace guidance.",
        "assets": [
            {
                "path": "skills/workspace/SKILL.md",
                "kind": "skill",
                "media_type": "text/markdown",
                "sha256": hashlib.sha256(_SKILL_BYTES).hexdigest(),
            }
        ],
        "entry_points": [
            {
                "entry_point_id": "get_started",
                "title": "Get started",
                "skill_path": "skills/workspace/SKILL.md",
                "required_app_ids": [],
            }
        ],
        "starter_requests": ["Help me get started with Workspace."],
        "required_apps": [],
        "optional_apps": [],
        "schedule_template_paths": [],
        "proactive_event_rules": [],
        "enterprise_roles": [],
        "signing_key_id": "test-plugin-key",
    }
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    (package / "plugin.json").write_bytes(
        canonical_bytes({**deepcopy(unsigned), "signature": signature})
    )
    return package
