"""Cross-cutting security acceptance for the signed MCP foundation."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import urllib.request
from pathlib import Path

import anyio
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.host import McpHost
from mcp_platform.inference import prepare_mcp_inference
from mcp_platform.package_contracts import McpPackageManifest
from mcp_platform.package_verifier import PackageVerificationError, verify_package
from mcp_platform.redaction import RedactingTextSink
from mcp_platform.registry import McpRegistryError, PackageRegistry
from mcp_platform.supervisor import McpProcessSupervisor
from tests.mcp_platform.test_authority import Harness
from tests.mcp_platform.test_inference_prepare import _input
from tests.mcp_platform.test_package_contracts import valid_manifest
from tests.mcp_platform.test_package_verifier import (
    build_signed_package,
    ephemeral_ring,
    host_tag,
    mutate_package,
    rewrite_and_resign_manifest,
    ring_for,
)
from tests.mcp_platform.test_supervisor import FakeClock, FakeSessionFactory

pytest_plugins = ("tests.mcp_platform.test_supervisor",)


def test_retired_session_surface_cannot_be_discovered() -> None:
    assert importlib.util.find_spec("mcp_platform.session") is None


def _verifier_for(ring):
    def verify(path: Path):
        return verify_package(
            path,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )

    return verify


def _retarget_version(
    package: Path,
    private_key: Ed25519PrivateKey,
    version: str,
) -> None:
    """Produce another fully signed release; expectations never use this helper."""
    catalog_digests: dict[str, str] = {}
    for name, manifest_field in (
        ("tool_catalog.json", "tool_catalog_sha256"),
        ("resource_catalog.json", "resource_catalog_sha256"),
    ):
        path = package / name
        document = json.loads(path.read_text(encoding="utf-8"))
        document["server_version"] = version
        encoded = canonical_bytes(document)
        path.write_bytes(encoded)
        catalog_digests[manifest_field] = sha256_hex(encoded)
    rewrite_and_resign_manifest(
        package,
        private_key,
        {"server_version": version, **catalog_digests},
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unsigned", "unknown_signing_key"),
        ("changed", "artifact_digest_mismatch"),
        ("revoked_key", "revoked_signing_key"),
        ("revoked_package", "revoked_server"),
    ],
)
def test_unsigned_changed_and_revoked_packages_never_verify(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    private_key, ring = ephemeral_ring()
    package = build_signed_package(
        tmp_path,
        private_key,
        signing_key_id="test-release",
    )
    if case == "unsigned":
        other_key = Ed25519PrivateKey.generate()
        ring = ring_for(other_key, key_id="another-release")
    elif case == "changed":
        mutate_package(package, "artifact")
    elif case == "revoked_key":
        ring = ring_for(
            private_key,
            key_id="active-release",
            revoked_key_ids=["test-release"],
        )
    else:
        ring = ring_for(
            private_key,
            revoked_server_ids=["omega.first_party.fake"],
        )

    with pytest.raises(PackageVerificationError, match=f"^{reason}$"):
        _verifier_for(ring)(package)


def test_signed_package_verification_is_strictly_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key, ring = ephemeral_ring()
    package = build_signed_package(
        tmp_path,
        private_key,
        signing_key_id="test-release",
    )
    network_attempts: list[str] = []

    def reject_network(*_args, **_kwargs):
        network_attempts.append("attempted")
        raise AssertionError("package verification attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(urllib.request, "urlopen", reject_network)

    verified = _verifier_for(ring)(package)

    assert verified.manifest.server_id == "omega.first_party.fake"
    assert network_attempts == []


def test_registry_rejects_a_validly_signed_package_downgrade(tmp_path: Path) -> None:
    """Replacing 2.0.0 with a signed 1.0.0 release must fail this test."""
    private_key, ring = ephemeral_ring()
    newer_root = tmp_path / "newer"
    older_root = tmp_path / "older"
    newer_root.mkdir()
    older_root.mkdir()
    newer = build_signed_package(
        newer_root,
        private_key,
        signing_key_id="test-release",
    )
    older = build_signed_package(
        older_root,
        private_key,
        signing_key_id="test-release",
    )
    _retarget_version(newer, private_key, "2.0.0")
    _retarget_version(older, private_key, "1.0.0")
    registry = PackageRegistry(
        state_root=tmp_path / "state",
        verifier=_verifier_for(ring),
    )
    registry.install(newer)

    with pytest.raises(McpRegistryError, match="^package_downgrade_rejected$"):
        registry.install(older)

    assert registry.get("omega.first_party.fake").server_version == "2.0.0"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--clientsecret=opaque-process-material"],
        ["--apikey", "opaque-process-material"],
        ["--accesstoken:opaque-process-material"],
        ["--oauthclientsecret=opaque-process-material"],
        ["--oauthrefreshtoken", "opaque-process-material"],
        ["--provideraccesstoken:opaque-process-material"],
    ],
)
def test_manifest_cannot_publish_secret_process_arguments(
    arguments: list[str],
) -> None:
    """Putting credential material in a signed process argument must fail this test."""
    document = valid_manifest()
    document["entrypoint"] = {
        "artifact": "omega-mcp-fake.exe",
        "arguments": arguments,
    }

    with pytest.raises(
        ValueError,
        match="^entrypoint_arguments_secret_bearing$",
    ) as caught:
        McpPackageManifest.from_dict(document)

    assert "opaque-process-material" not in str(caught.value)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_entrypoint_traversal_and_symlink_escape_never_verify(tmp_path: Path) -> None:
    private_key, ring = ephemeral_ring()
    package = build_signed_package(
        tmp_path,
        private_key,
        signing_key_id="test-release",
    )
    rewrite_and_resign_manifest(
        package,
        private_key,
        {"entrypoint": {"artifact": "../outside.exe", "arguments": []}},
    )
    with pytest.raises(PackageVerificationError, match="^entrypoint_escape$"):
        _verifier_for(ring)(package)

    clean_root = tmp_path / "symlink"
    clean_root.mkdir()
    symlink_package = build_signed_package(
        clean_root,
        private_key,
        signing_key_id="test-release",
    )
    outside = tmp_path / "outside.exe"
    artifact = symlink_package / "omega-mcp-fake.exe"
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    try:
        os.symlink(outside, artifact)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(PackageVerificationError, match="^entrypoint_escape$"):
        _verifier_for(ring)(symlink_package)


def test_error_stderr_prompt_and_action_ledger_never_retain_secrets(
    tmp_path: Path,
) -> None:
    secret = "Bear" + "er opaque.acceptance.material"

    sink = RedactingTextSink(omega_root=tmp_path)
    sink.write(secret + "\n")
    assert secret not in sink.getvalue()

    with pytest.raises(ValueError, match="^secret_bearing_mcp_inference$") as caught:
        prepare_mcp_inference(_input(operator_goal=secret))
    assert secret not in str(caught.value)

    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    harness = Harness(authority_root)
    harness.supervisor.error = RuntimeError(secret)
    outcome = anyio.run(harness.gateway.execute, harness.proposal)
    assert outcome.status == "failed"
    assert secret not in harness.ledger_path.read_text(encoding="utf-8")
    assert secret not in repr(outcome)


class _ContentFreeAuthority:
    @staticmethod
    def wait_for_cleanup_sync(_timeout_s: float) -> bool:
        return True


def test_host_status_exposes_no_secret_or_process_material(
    installed_packages,
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_ContentFreeAuthority(),
    )
    host.start()
    try:
        serialized = json.dumps(host.status()).casefold()
    finally:
        host.shutdown()

    for forbidden in (
        "content",
        "token",
        "secret",
        "password",
        "arguments",
        "command",
        "account",
        "tenant",
    ):
        assert forbidden not in serialized
