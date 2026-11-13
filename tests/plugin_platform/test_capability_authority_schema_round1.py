from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
from plugin_platform.package_verifier import (
    PackageVerificationError,
    verify_plugin_package,
)
from plugin_platform.trust import TrustedPublisherKeyRing
from tests.plugin_platform.support import build_signed_plugin, ephemeral_publisher_ring
from tests.plugin_platform.test_package_verifier import (
    _add_asset,
    _assert_rejected,
    _manifest,
    _write_signed_manifest,
)


def _capability(kind: str, name: str, digest: str) -> dict[str, object]:
    return {
        "kind": kind,
        "name": name,
        "schema_sha256": digest,
        "version": None,
    }


def _remote_requirement(
    app_id: str,
    capabilities: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "app_id": app_id,
        "publisher_id": "provider.publisher",
        "deployment": "remote",
        "provider_id": "shared.provider",
        "protocol_version": "2026-07-28",
        "transports": ["streamable_http"],
        "remote_origin": "https://provider.example.test/mcp/v1",
        "authorization_issuer": "https://auth.example.test",
        "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
        "capabilities": capabilities,
    }


def _app_authority(
    *,
    tools: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
    resource_templates: tuple[str, ...] = (),
    prompts: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "tools": {name: {} for name in tools},
        "resources": {name: {} for name in resources},
        "resource_templates": {name: {} for name in resource_templates},
        "prompts": {name: {} for name in prompts},
    }


def _authority(
    plugin_id: str,
    apps: dict[str, dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "plugin_id": plugin_id,
        "apps": apps,
    }


def _signed_package_with_authority(
    root: Path,
    private_key: Ed25519PrivateKey,
    *,
    plugin_id: str,
    required_apps: list[dict[str, object]],
    optional_apps: list[dict[str, object]] | None = None,
    authority: dict[str, object],
) -> Path:
    package = build_signed_plugin(root, private_key)
    manifest = _manifest(package)
    manifest["plugin_id"] = plugin_id
    manifest["kind"] = "composite" if len(required_apps) > 1 else "mcp_backed"
    manifest["required_apps"] = copy.deepcopy(required_apps)
    manifest["optional_apps"] = copy.deepcopy(optional_apps or [])
    manifest["entry_points"][0]["required_app_ids"] = [
        app["app_id"] for app in required_apps
    ]
    _write_signed_manifest(package, private_key, manifest)
    _add_asset(
        package,
        private_key,
        relative_path="tools/authority.json",
        kind="capability_authority",
        media_type="application/json",
        payload=canonical_bytes(authority),
    )
    return package


def _single_app_package(
    tmp_path: Path,
    private_key: Ed25519PrivateKey,
    authority: dict[str, object],
) -> Path:
    requirement = _remote_requirement(
        "provider.shared.mcp",
        [
            _capability("tool", "read_item", "1" * 64),
            _capability("prompt", "summarize_item", "2" * 64),
            _capability("resource", "item", "3" * 64),
        ],
    )
    return _signed_package_with_authority(
        tmp_path,
        private_key,
        plugin_id="test.workspace",
        required_apps=[requirement],
        authority=authority,
    )


def test_v2_authority_partitions_composite_and_shared_app_subsets(tmp_path: Path) -> None:
    """Catches one plugin/app authority union leaking into another signed owner."""
    private_key, ring = ephemeral_publisher_ring()
    shared = _remote_requirement(
        "provider.shared.mcp",
        [
            _capability("tool", "read_item", "1" * 64),
            _capability("tool", "write_item", "2" * 64),
        ],
    )
    calendar = _remote_requirement(
        "provider.calendar.mcp",
        [_capability("tool", "list_events", "3" * 64)],
    )
    optional = _remote_requirement(
        "provider.optional.mcp",
        [_capability("prompt", "draft_note", "4" * 64)],
    )
    composite_authority = _authority(
        "test.composite",
        {
            "provider.calendar.mcp": _app_authority(tools=("list_events",)),
            "provider.optional.mcp": _app_authority(prompts=("draft_note",)),
            "provider.shared.mcp": _app_authority(tools=("read_item",)),
        },
    )
    composite = _signed_package_with_authority(
        tmp_path / "composite",
        private_key,
        plugin_id="test.composite",
        required_apps=[shared, calendar],
        optional_apps=[optional],
        authority=composite_authority,
    )
    second_authority = _authority(
        "test.second",
        {"provider.shared.mcp": _app_authority(tools=("write_item",))},
    )
    second = _signed_package_with_authority(
        tmp_path / "second",
        private_key,
        plugin_id="test.second",
        required_apps=[shared],
        authority=second_authority,
    )

    first_verified = verify_plugin_package(composite, ring, "3.0.0")
    second_verified = verify_plugin_package(second, ring, "3.0.0")

    first_document = json.loads(first_verified.asset_bytes["tools/authority.json"])
    second_document = json.loads(second_verified.asset_bytes["tools/authority.json"])
    assert first_document["apps"]["provider.shared.mcp"]["tools"] == {
        "read_item": {}
    }
    assert second_document["apps"]["provider.shared.mcp"]["tools"] == {
        "write_item": {}
    }


def test_legacy_single_app_authority_is_identified_but_never_admitted(
    tmp_path: Path,
) -> None:
    """Catches the retired flat shape being parsed into usable capability data.

    Spec principle 8 forbids a compatibility fork: the pre-v2 flat single-app
    ``tools/authority.json`` is identified only far enough to mark the
    package, never parsed into a per-app capability projection.
    """

    private_key, ring = ephemeral_publisher_ring()
    package = _single_app_package(tmp_path, private_key, {"tools": {}})

    verified = verify_plugin_package(package, ring, "3.0.0")

    assert verified.manifest.plugin_id == "test.workspace"
    assert verified.legacy_authority is True
    assert verified.capability_authority is None


def test_legacy_authority_with_populated_groups_is_still_not_admitted(
    tmp_path: Path,
) -> None:
    """Catches a legacy document with real-looking tool entries slipping through."""

    private_key, ring = ephemeral_publisher_ring()
    package = _single_app_package(
        tmp_path,
        private_key,
        {"tools": {"read_item": {}}},
    )

    verified = verify_plugin_package(package, ring, "3.0.0")

    assert verified.legacy_authority is True
    assert verified.capability_authority is None


def test_v2_authority_is_never_marked_legacy(tmp_path: Path) -> None:
    private_key, ring = ephemeral_publisher_ring()
    package = _single_app_package(
        tmp_path,
        private_key,
        _authority("test.workspace", {"provider.shared.mcp": _app_authority()}),
    )

    verified = verify_plugin_package(package, ring, "3.0.0")

    assert verified.legacy_authority is False
    assert verified.capability_authority is not None


def test_current_single_app_authority_envelope_is_admitted(tmp_path: Path) -> None:
    """Catches the v2 envelope being rejected alongside the retired flat shape."""

    private_key, ring = ephemeral_publisher_ring()
    package = _single_app_package(
        tmp_path,
        private_key,
        _authority("test.workspace", {"provider.shared.mcp": _app_authority()}),
    )

    verified = verify_plugin_package(package, ring, "3.0.0")

    assert verified.manifest.plugin_id == "test.workspace"
    assert verified.capability_authority is not None
    assert set(verified.capability_authority) == {"provider.shared.mcp"}


def test_legacy_plugin_wide_authority_is_rejected_for_multiple_apps(
    tmp_path: Path,
) -> None:
    """Catches a legacy root capability union leaking across app partitions."""

    private_key, ring = ephemeral_publisher_ring()
    first = _remote_requirement(
        "provider.first.mcp",
        [_capability("tool", "read_first", "1" * 64)],
    )
    second = _remote_requirement(
        "provider.second.mcp",
        [_capability("tool", "read_second", "2" * 64)],
    )
    package = _signed_package_with_authority(
        tmp_path,
        private_key,
        plugin_id="test.composite",
        required_apps=[first, second],
        authority={"tools": {}},
    )

    _assert_rejected(package, ring, "asset_invalid")


@pytest.mark.parametrize(
    "mutation",
    (
        "schema_version",
        "missing_root_key",
        "extra_root_key",
        "plugin_id",
    ),
)
def test_v2_authority_rejects_root_identity_or_key_mismatch(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catches authority bytes that are not bound to the exact package plugin."""
    private_key, ring = ephemeral_publisher_ring()
    document = _authority(
        "test.workspace",
        {"provider.shared.mcp": _app_authority(tools=("read_item",))},
    )
    if mutation == "schema_version":
        document["schema_version"] = 1
    elif mutation == "missing_root_key":
        del document["plugin_id"]
    elif mutation == "extra_root_key":
        document["provider_id"] = "forbidden.provider"
    else:
        document["plugin_id"] = "test.other"
    package = _single_app_package(tmp_path, private_key, document)

    _assert_rejected(package, ring, "asset_invalid")


@pytest.mark.parametrize("mutation", ("missing", "extra", "cross_app"))
def test_v2_authority_rejects_app_partition_mismatch(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catches missing, undeclared, or cross-app capability authority."""
    private_key, ring = ephemeral_publisher_ring()
    first = _remote_requirement(
        "provider.first.mcp",
        [_capability("tool", "first_tool", "1" * 64)],
    )
    second = _remote_requirement(
        "provider.second.mcp",
        [_capability("tool", "second_tool", "2" * 64)],
    )
    apps = {
        "provider.first.mcp": _app_authority(tools=("first_tool",)),
        "provider.second.mcp": _app_authority(tools=("second_tool",)),
    }
    if mutation == "missing":
        del apps["provider.second.mcp"]
    elif mutation == "extra":
        apps["provider.undeclared.mcp"] = _app_authority()
    else:
        apps["provider.first.mcp"] = _app_authority(tools=("second_tool",))
    package = _signed_package_with_authority(
        tmp_path,
        private_key,
        plugin_id="test.composite",
        required_apps=[first, second],
        authority=_authority("test.composite", apps),
    )

    _assert_rejected(package, ring, "asset_invalid")


@pytest.mark.parametrize(
    "mutation",
    ("missing_group", "extra_group", "non_mapping_group", "non_mapping_policy"),
)
def test_v2_authority_rejects_non_exact_app_shape(tmp_path: Path, mutation: str) -> None:
    """Catches open-ended or structurally ambiguous per-app authority."""
    private_key, ring = ephemeral_publisher_ring()
    app = _app_authority(tools=("read_item",))
    if mutation == "missing_group":
        del app["prompts"]
    elif mutation == "extra_group":
        app["oauth_scopes"] = []
    elif mutation == "non_mapping_group":
        app["resources"] = []
    else:
        app["tools"]["read_item"] = []
    package = _single_app_package(
        tmp_path,
        private_key,
        _authority("test.workspace", {"provider.shared.mcp": app}),
    )

    _assert_rejected(package, ring, "asset_invalid")


@pytest.mark.parametrize(
    ("group", "capability"),
    (
        ("tools", "summarize_item"),
        ("prompts", "read_item"),
        ("resources", "read_item"),
        ("resource_templates", "summarize_item"),
    ),
)
def test_v2_authority_rejects_manifest_kind_mismatch(
    tmp_path: Path,
    group: str,
    capability: str,
) -> None:
    """Catches signing a declared capability through the wrong operation surface."""
    private_key, ring = ephemeral_publisher_ring()
    app = _app_authority()
    app[group][capability] = {}
    package = _single_app_package(
        tmp_path,
        private_key,
        _authority("test.workspace", {"provider.shared.mcp": app}),
    )

    _assert_rejected(package, ring, "asset_invalid")


def test_v2_authority_rejects_resource_and_template_alias(tmp_path: Path) -> None:
    """Catches one resource capability being authorized as two URI surfaces."""
    private_key, ring = ephemeral_publisher_ring()
    app = _app_authority(resources=("item",), resource_templates=("item",))
    package = _single_app_package(
        tmp_path,
        private_key,
        _authority("test.workspace", {"provider.shared.mcp": app}),
    )

    _assert_rejected(package, ring, "asset_invalid")


def test_v2_authority_asset_tamper_still_fails_signed_digest(tmp_path: Path) -> None:
    """Catches valid-looking replacement authority bytes after manifest signing."""
    private_key, ring = ephemeral_publisher_ring()
    document = _authority(
        "test.workspace",
        {"provider.shared.mcp": _app_authority(tools=("read_item",))},
    )
    package = _single_app_package(tmp_path, private_key, document)
    tampered = copy.deepcopy(document)
    tampered["apps"]["provider.shared.mcp"]["tools"] = {}
    (package / "tools" / "authority.json").write_bytes(canonical_bytes(tampered))

    _assert_rejected(package, ring, "asset_digest_mismatch")


def test_v2_runtime_compatibility_remains_closed_per_app(tmp_path: Path) -> None:
    """Catches moving compatibility metadata outside its exact signed app partition."""
    private_key, ring = ephemeral_publisher_ring()
    app = _app_authority(tools=("read_item",))
    app["runtime_compatibility"] = {
        "wire_protocol_version": "2025-11-25",
        "handshake_mode": "legacy",
        "missing_metadata": {
            "server_id": "StatelessServer",
            "server_version": "ESF",
        },
    }
    package = _single_app_package(
        tmp_path,
        private_key,
        _authority("test.workspace", {"provider.shared.mcp": app}),
    )

    verified = verify_plugin_package(package, ring, "3.0.0")
    assert verified.asset_bytes["tools/authority.json"] == canonical_bytes(
        _authority("test.workspace", {"provider.shared.mcp": app})
    )

    invalid = copy.deepcopy(app)
    invalid["runtime_compatibility"]["provider_id"] = "forbidden.provider"
    invalid_package = _single_app_package(
        tmp_path / "invalid",
        private_key,
        _authority("test.workspace", {"provider.shared.mcp": invalid}),
    )
    _assert_rejected(invalid_package, ring, "asset_invalid")
