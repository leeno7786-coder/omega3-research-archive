"""Offline verification tests for signed first-party MCP packages."""

from __future__ import annotations

import base64
import copy
import dataclasses
import gc
import json
import os
import threading
import weakref
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import mcp_platform.package_verifier as package_verifier_module

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, McpPackageManifest
from mcp_platform.package_verifier import (
    PackageVerificationError,
    current_platform_tag,
    manifest_signing_bytes,
    verify_package,
)
from mcp_platform.trust_store import TrustedKeyRing, load_embedded_key_ring


def host_tag() -> str:
    return "windows-x86_64"


def ephemeral_ring() -> tuple[Ed25519PrivateKey, TrustedKeyRing]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    ring = TrustedKeyRing.from_dict(
        {
            "schema_version": 1,
            "keys": [
                {
                    "key_id": "test-release",
                    "public_key_base64": base64.b64encode(public).decode("ascii"),
                }
            ],
            "revoked_key_ids": [],
            "revoked_server_ids": [],
        }
    )
    return private, ring


def ring_for(
    private: Ed25519PrivateKey,
    *,
    key_id: str = "test-release",
    revoked_key_ids: list[str] | None = None,
    revoked_server_ids: list[str] | None = None,
) -> TrustedKeyRing:
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return TrustedKeyRing.from_dict(
        {
            "schema_version": 1,
            "keys": [
                {
                    "key_id": key_id,
                    "public_key_base64": base64.b64encode(public).decode("ascii"),
                }
            ],
            "revoked_key_ids": revoked_key_ids or [],
            "revoked_server_ids": revoked_server_ids or [],
        }
    )


def _tool_catalog() -> dict[str, object]:
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    output_schema = {"type": "object", "properties": {"results": {"type": "array"}}}
    return {
        "catalog_version": 1,
        "server_id": "omega.first_party.fake",
        "server_version": "1.0.0",
        "tools": [
            {
                "name": "mail_search",
                "title": "Search mail",
                "input_schema": input_schema,
                "output_schema": output_schema,
                "input_schema_sha256": sha256_hex(canonical_bytes(input_schema)),
                "output_schema_sha256": sha256_hex(canonical_bytes(output_schema)),
                "capability_tags": ["mail.search"],
                "provider_extension_tags": [],
                "risk_class": "read_only",
                "effect": "searches mail",
                "requires_revision": True,
                "max_result_bytes": 4096,
                "allowed_mime_types": ["application/json"],
            }
        ],
    }


def _resource_catalog() -> dict[str, object]:
    return {
        "catalog_version": 1,
        "server_id": "omega.first_party.fake",
        "server_version": "1.0.0",
        "resources": [
            {
                "uri": "omega://mail/inbox",
                "name": "Inbox",
                "mime_type": "application/json",
                "max_bytes": 4096,
                "capability_tags": ["mail.search"],
            }
        ],
        "resource_templates": [],
    }


def build_signed_package(
    root: Path,
    private: Ed25519PrivateKey,
    *,
    signing_key_id: str,
) -> Path:
    package = root / "signed-package"
    package.mkdir()
    artifact = b"fake executable artifact\n"
    tools = _tool_catalog()
    resources = _resource_catalog()
    (package / "omega-mcp-fake.exe").write_bytes(artifact)
    (package / "tool_catalog.json").write_bytes(canonical_bytes(tools))
    (package / "resource_catalog.json").write_bytes(canonical_bytes(resources))
    document: dict[str, object] = {
        "manifest_version": 1,
        "server_id": "omega.first_party.fake",
        "publisher_id": "omega.first_party",
        "server_version": "1.0.0",
        "minimum_omega_version": "3.0.0",
        "supported_mcp_protocol_versions": [MCP_PROTOCOL_VERSION],
        "network_class": "local_only",
        "platform": host_tag(),
        "entrypoint": {"artifact": "omega-mcp-fake.exe", "arguments": []},
        "artifact_sha256": sha256_hex(artifact),
        "tool_catalog_sha256": sha256_hex(canonical_bytes(tools)),
        "resource_catalog_sha256": sha256_hex(canonical_bytes(resources)),
        "capability_tags": ["mail.search"],
        "risk_declarations": ["read_only"],
        "oauth_providers": [],
        "allowed_network_destinations": [],
        "signing_key_id": signing_key_id,
        "signature": base64.b64encode(b"0" * 64).decode("ascii"),
    }
    unsigned = McpPackageManifest.from_dict(document)
    document["signature"] = base64.b64encode(private.sign(unsigned.signing_bytes())).decode("ascii")
    (package / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    return package


def mutate_package(package: Path, mutation: str) -> None:
    if mutation == "signature":
        document = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        document["signature"] = base64.b64encode(b"1" * 64).decode("ascii")
        (package / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    elif mutation == "artifact":
        (package / "omega-mcp-fake.exe").write_bytes(b"changed artifact")
    elif mutation == "tool_catalog":
        (package / "tool_catalog.json").write_bytes(b"{}")
    elif mutation == "resource_catalog":
        (package / "resource_catalog.json").write_bytes(b"{}")
    else:
        raise AssertionError(f"unsupported test mutation: {mutation}")


def rewrite_and_resign_manifest(
    package: Path,
    private: Ed25519PrivateKey,
    updates: dict[str, object],
) -> None:
    path = package / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(updates)
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(private.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    path.write_text(json.dumps(document), encoding="utf-8")


def assert_rejects(
    package: Path,
    ring: TrustedKeyRing,
    reason: str,
    *,
    omega_version: str = "3.0.0",
    platform_tag: str | None = None,
) -> None:
    with pytest.raises(PackageVerificationError, match=f"^{reason}$"):
        verify_package(
            package,
            ring,
            omega_version=omega_version,
            platform_tag=platform_tag or host_tag(),
        )


def test_valid_signed_package_accepts(tmp_path: Path) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")

    verified = verify_package(package, ring, omega_version="3.0.0", platform_tag=host_tag())

    assert verified.manifest.server_id == "omega.first_party.fake"
    assert verified.artifact_path.is_file()


def test_verifier_binds_the_exact_native_artifact_identity(tmp_path: Path) -> None:
    """Catches returning a digest-only package that a later path swap can reuse."""
    from mcp_platform.artifact_guard import descriptor_file_identity

    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")

    verified = verify_package(package, ring, omega_version="3.0.0", platform_tag=host_tag())

    with verified.artifact_path.open("rb") as artifact_file:
        expected_identity = descriptor_file_identity(artifact_file.fileno())
    assert verified.artifact_identity == expected_identity
    assert verified.artifact_identity != (0, 0)


def test_portable_artifact_guard_fails_closed_before_retaining_a_path_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches authenticating one inode but later executing a mutable POSIX pathname."""
    import mcp_platform.artifact_guard as artifact_guard_module

    artifact = tmp_path / "portable-server"
    artifact.write_bytes(b"signed portable artifact")
    status = os.stat(artifact)
    monkeypatch.setattr(artifact_guard_module.sys, "platform", "linux")

    with pytest.raises(
        artifact_guard_module.ArtifactGuardError,
        match="^artifact_guard_unsupported_platform$",
    ):
        artifact_guard_module.ArtifactLaunchGuard.acquire(
            package_root=tmp_path,
            artifact_path=artifact,
            expected_sha256=sha256_hex(artifact.read_bytes()),
            expected_identity=(status.st_dev, status.st_ino),
            expected_component_identities=(
                (0, os.stat(tmp_path).st_dev, os.stat(tmp_path).st_ino),
                (0, status.st_dev, status.st_ino),
            ),
        )


def test_only_the_exact_verifier_issued_package_can_reach_guard_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches nominal/copy/replace/object-new packages borrowing signed launch fields."""
    from mcp_platform.client_contracts import McpClientError
    from mcp_platform.transports import stdio as stdio_module

    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    verified = verify_package(package, ring, omega_version="3.0.0", platform_tag=host_tag())
    nominal = type(verified)(
        manifest=verified.manifest,
        manifest_sha256=verified.manifest_sha256,
        artifact_path=verified.artifact_path,
        tool_catalog_path=verified.tool_catalog_path,
        resource_catalog_path=verified.resource_catalog_path,
        tool_catalog=verified.tool_catalog,
        resource_catalog=verified.resource_catalog,
        artifact_identity=verified.artifact_identity,
    )
    object_new = object.__new__(type(verified))
    for field in dataclasses.fields(verified):
        object.__setattr__(object_new, field.name, getattr(verified, field.name))
    forged = (nominal, copy.copy(verified), dataclasses.replace(verified), object_new)

    acquire_calls = 0

    def forbidden_acquire(**_kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        raise AssertionError("native guard acquisition reached by an unissued package")

    monkeypatch.setattr(stdio_module.ArtifactLaunchGuard, "acquire", forbidden_acquire)

    for candidate in forged:
        with pytest.raises(McpClientError, match="^identity_mismatch$"):
            stdio_module.VerifiedStdioLaunchGuard.acquire(
                candidate,
                package_root=package,
                environment={
                    "OMEGA_MCP_SERVER_ID": verified.manifest.server_id,
                    "OMEGA_MCP_PACKAGE_ROOT": str(package.resolve()),
                },
            )

    assert acquire_calls == 0


def test_post_issuance_verified_package_mutation_revokes_launch_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches object.__setattr__ changing an already verifier-issued launch identity."""
    from mcp_platform.client_contracts import McpClientError
    from mcp_platform.transports import stdio as stdio_module

    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    verified = verify_package(package, ring, omega_version="3.0.0", platform_tag=host_tag())
    object.__setattr__(verified, "artifact_identity", (verified.artifact_identity[0], 1))

    monkeypatch.setattr(
        stdio_module.ArtifactLaunchGuard,
        "acquire",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("mutated package reached native guard acquisition")
        ),
    )

    with pytest.raises(McpClientError, match="^identity_mismatch$"):
        stdio_module.VerifiedStdioLaunchGuard.acquire(
            verified,
            package_root=package,
            environment={
                "OMEGA_MCP_SERVER_ID": verified.manifest.server_id,
                "OMEGA_MCP_PACKAGE_ROOT": str(package.resolve()),
            },
        )


def test_verifier_issuance_survives_weakref_gc_callback_lock_reentry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches fingerprinting under the issuance lock while an older receipt is collected."""
    from mcp_platform import package_verifier as verifier_module

    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    prior_holder = [verify_package(package, ring, omega_version="3.0.0", platform_tag=host_tag())]
    prior_id = id(prior_holder[0])
    prior_reference = weakref.ref(prior_holder[0])
    original_fingerprint = verifier_module._package_fingerprint
    fingerprint_entered = threading.Event()

    def collect_prior_during_fingerprint(candidate: object) -> str:
        fingerprint_entered.set()
        prior_holder.clear()
        gc.collect()
        return original_fingerprint(candidate)  # type: ignore[arg-type]

    monkeypatch.setattr(
        verifier_module,
        "_package_fingerprint",
        collect_prior_during_fingerprint,
    )
    issued: list[object] = []
    failures: list[BaseException] = []

    def issue_again() -> None:
        try:
            issued.append(
                verify_package(
                    package,
                    ring,
                    omega_version="3.0.0",
                    platform_tag=host_tag(),
                )
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=issue_again, name="verified-package-gc", daemon=True)
    worker.start()
    assert fingerprint_entered.wait(timeout=2)
    worker.join(timeout=2)

    assert not worker.is_alive(), "verifier issuance deadlocked in the weakref callback"
    assert failures == []
    assert len(issued) == 1
    assert prior_reference() is None
    assert prior_id not in verifier_module._ISSUED_PACKAGES
    assert verifier_module._is_verifier_issued_package(issued[0]) is True


@pytest.mark.parametrize("mutation", ["signature", "artifact", "tool_catalog", "resource_catalog"])
def test_changed_signed_material_rejects(tmp_path: Path, mutation: str) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    mutate_package(package, mutation)

    with pytest.raises(PackageVerificationError):
        verify_package(package, ring, omega_version="3.0.0", platform_tag=host_tag())


def test_manifest_signing_bytes_omit_only_signature() -> None:
    document = {
        "manifest_version": 1,
        "server_id": "omega.first_party.fake",
        "publisher_id": "omega.first_party",
        "server_version": "1.0.0",
        "minimum_omega_version": "3.0.0",
        "supported_mcp_protocol_versions": [MCP_PROTOCOL_VERSION],
        "network_class": "local_only",
        "platform": host_tag(),
        "entrypoint": {"artifact": "server.exe", "arguments": []},
        "artifact_sha256": "a" * 64,
        "tool_catalog_sha256": "b" * 64,
        "resource_catalog_sha256": "c" * 64,
        "capability_tags": ["mail.search"],
        "risk_declarations": ["read_only"],
        "oauth_providers": [],
        "allowed_network_destinations": [],
        "signing_key_id": "test-release",
        "signature": base64.b64encode(b"x" * 64).decode("ascii"),
    }
    manifest = McpPackageManifest.from_dict(document)

    assert manifest_signing_bytes(manifest) == canonical_bytes(
        {key: value for key, value in document.items() if key != "signature"}
    )


@pytest.mark.parametrize(
    "document",
    [
        {
            "schema_version": 1,
            "keys": [],
            "revoked_key_ids": [],
            "revoked_server_ids": [],
            "unexpected": True,
        },
        {"schema_version": 1, "keys": [], "revoked_key_ids": []},
        {
            "schema_version": 1,
            "keys": [
                {"key_id": "duplicate", "public_key_base64": base64.b64encode(b"a" * 32).decode()},
                {"key_id": "duplicate", "public_key_base64": base64.b64encode(b"b" * 32).decode()},
            ],
            "revoked_key_ids": [],
            "revoked_server_ids": [],
        },
        {
            "schema_version": 1,
            "keys": [{"key_id": "short", "public_key_base64": base64.b64encode(b"x").decode()}],
            "revoked_key_ids": [],
            "revoked_server_ids": [],
        },
        {
            "schema_version": 1,
            "keys": [{"key_id": "both", "public_key_base64": base64.b64encode(b"x" * 32).decode()}],
            "revoked_key_ids": ["both"],
            "revoked_server_ids": [],
        },
        {
            "schema_version": 1,
            "keys": [],
            "revoked_key_ids": [],
            "revoked_server_ids": ["third_party.server"],
        },
    ],
)
def test_trusted_key_ring_rejects_malformed_or_ambiguous_state(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        TrustedKeyRing.from_dict(document)


def test_trusted_key_ring_cannot_bypass_validated_factory() -> None:
    with pytest.raises(TypeError):
        TrustedKeyRing(
            _keys={"unvalidated": b"x" * 32},
            _revoked_key_ids=frozenset(),
            _revoked_server_ids=frozenset(),
        )


def test_embedded_key_ring_starts_empty_and_fail_closed(tmp_path: Path) -> None:
    ring = load_embedded_key_ring()

    assert ring.key_ids == ()
    with pytest.raises(PackageVerificationError, match="^unknown_signing_key$"):
        private = Ed25519PrivateKey.generate()
        package = build_signed_package(
            tmp_path,
            private,
            signing_key_id="not-embedded",
        )
        verify_package(package, ring, omega_version="3.0.0", platform_tag=host_tag())


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unknown_key", "unknown_signing_key"),
        ("revoked_key", "revoked_signing_key"),
        ("revoked_server", "revoked_server"),
        ("publisher", "publisher_not_first_party"),
        ("signature", "invalid_manifest_signature"),
        ("artifact", "artifact_digest_mismatch"),
        ("tool_catalog", "tool_catalog_digest_mismatch"),
        ("resource_catalog", "resource_catalog_digest_mismatch"),
        ("minimum_version", "minimum_omega_version_not_met"),
        ("platform", "unsupported_platform"),
        ("protocol", "unsupported_mcp_protocol"),
        ("catalog_identity", "catalog_identity_mismatch"),
    ],
)
def test_verification_failure_reasons_are_stable(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    if case == "unknown_key":
        rewrite_and_resign_manifest(package, private, {"signing_key_id": "unknown-release"})
    elif case == "revoked_key":
        ring = ring_for(private, key_id="another-active", revoked_key_ids=["test-release"])
    elif case == "revoked_server":
        ring = ring_for(private, revoked_server_ids=["omega.first_party.fake"])
    elif case == "publisher":
        rewrite_and_resign_manifest(package, private, {"publisher_id": "third_party"})
    elif case == "signature":
        mutate_package(package, "signature")
    elif case in {"artifact", "tool_catalog", "resource_catalog"}:
        mutate_package(package, case)
    elif case == "minimum_version":
        rewrite_and_resign_manifest(package, private, {"minimum_omega_version": "3.0.1"})
    elif case == "platform":
        rewrite_and_resign_manifest(package, private, {"platform": "linux-arm64"})
    elif case == "protocol":
        rewrite_and_resign_manifest(
            package,
            private,
            {"supported_mcp_protocol_versions": ["2024-11-05"]},
        )
    elif case == "catalog_identity":
        tools = json.loads((package / "tool_catalog.json").read_text(encoding="utf-8"))
        tools["server_version"] = "2.0.0"
        (package / "tool_catalog.json").write_bytes(canonical_bytes(tools))
        rewrite_and_resign_manifest(
            package,
            private,
            {"tool_catalog_sha256": sha256_hex(canonical_bytes(tools))},
        )
    else:
        raise AssertionError(case)

    assert_rejects(package, ring, reason)


@pytest.mark.parametrize("artifact", ["../outside.exe", r"C:\outside.exe", r"\outside.exe"])
def test_entrypoint_traversal_or_rooted_path_rejects(tmp_path: Path, artifact: str) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    rewrite_and_resign_manifest(
        package, private, {"entrypoint": {"artifact": artifact, "arguments": []}}
    )

    assert_rejects(package, ring, "entrypoint_escape")


def test_entrypoint_symlink_escape_rejects(tmp_path: Path) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    outside = tmp_path / "outside.exe"
    outside.write_bytes((package / "omega-mcp-fake.exe").read_bytes())
    artifact = package / "omega-mcp-fake.exe"
    artifact.unlink()
    try:
        os.symlink(outside, artifact)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    assert_rejects(package, ring, "entrypoint_escape")


def test_errors_do_not_disclose_signed_material_or_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    signature = manifest["signature"]
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    artifact = (package / "omega-mcp-fake.exe").read_bytes()
    environment_secret = "environment-value-must-not-leak"
    monkeypatch.setenv("OMEGA_TEST_SECRET", environment_secret)
    mutate_package(package, "signature")

    with pytest.raises(PackageVerificationError) as caught:
        verify_package(package, ring, omega_version="3.0.0", platform_tag=host_tag())

    message = str(caught.value)
    for forbidden in (
        signature,
        base64.b64encode(public).decode("ascii"),
        artifact.decode("ascii"),
        environment_secret,
    ):
        assert forbidden not in message


def test_current_platform_tag_is_a_closed_supported_value() -> None:
    assert current_platform_tag() in {
        "windows-x86_64",
        "windows-arm64",
        "linux-x86_64",
        "linux-arm64",
        "macos-x86_64",
        "macos-arm64",
    }


def test_package_verification_limits_are_positive_and_immutable() -> None:
    limits_type = package_verifier_module.PackageVerificationLimits
    limits = limits_type(
        max_manifest_bytes=101,
        max_tool_catalog_bytes=102,
        max_resource_catalog_bytes=103,
        max_artifact_bytes=104,
        hash_chunk_bytes=11,
    )
    assert dataclasses.asdict(limits) == {
        "max_manifest_bytes": 101,
        "max_tool_catalog_bytes": 102,
        "max_resource_catalog_bytes": 103,
        "max_artifact_bytes": 104,
        "hash_chunk_bytes": 11,
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_artifact_bytes = 105  # type: ignore[misc]
    for field in dataclasses.fields(limits):
        with pytest.raises(ValueError, match=field.name):
            limits_type(**{field.name: 0})


@pytest.mark.parametrize(
    ("filename", "limit_field"),
    [
        ("manifest.json", "max_manifest_bytes"),
        ("tool_catalog.json", "max_tool_catalog_bytes"),
        ("resource_catalog.json", "max_resource_catalog_bytes"),
        ("omega-mcp-fake.exe", "max_artifact_bytes"),
    ],
)
def test_every_untrusted_package_file_is_capped_before_whole_read(
    tmp_path: Path,
    filename: str,
    limit_field: str,
) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    size = (package / filename).stat().st_size
    limits = package_verifier_module.PackageVerificationLimits(**{limit_field: size - 1})

    with pytest.raises(PackageVerificationError, match="^package_file_limit_exceeded$"):
        verify_package(
            package,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
            limits=limits,
        )


def test_artifact_hashing_is_streamed_in_bounded_chunks_and_keeps_identity_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    artifact_path = package / "omega-mcp-fake.exe"
    original_open = Path.open
    reads: list[int] = []

    class _ReadProbe:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def __enter__(self) -> _ReadProbe:
            self._wrapped.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> object:
            return self._wrapped.__exit__(*args)  # type: ignore[attr-defined]

        def fileno(self) -> int:
            return self._wrapped.fileno()  # type: ignore[attr-defined,no-any-return]

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert 0 < size <= 7
            return self._wrapped.read(size)  # type: ignore[attr-defined,no-any-return]

    def probed_open(path: Path, *args: object, **kwargs: object) -> object:
        opened = original_open(path, *args, **kwargs)
        return _ReadProbe(opened) if path == artifact_path else opened

    monkeypatch.setattr(Path, "open", probed_open)
    verified = verify_package(
        package,
        ring,
        omega_version="3.0.0",
        platform_tag=host_tag(),
        limits=package_verifier_module.PackageVerificationLimits(hash_chunk_bytes=7),
    )

    assert verified.artifact_identity != (0, 0)
    assert reads and all(size == 7 for size in reads)


def test_artifact_hash_read_request_never_exceeds_remaining_artifact_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    artifact_path = package / "omega-mcp-fake.exe"
    artifact_limit = artifact_path.stat().st_size
    original_open = Path.open
    reads: list[int] = []

    class _ReadProbe:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def __enter__(self) -> _ReadProbe:
            self._wrapped.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> object:
            return self._wrapped.__exit__(*args)  # type: ignore[attr-defined]

        def fileno(self) -> int:
            return self._wrapped.fileno()  # type: ignore[attr-defined,no-any-return]

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            return self._wrapped.read(size)  # type: ignore[attr-defined,no-any-return]

    def probed_open(path: Path, *args: object, **kwargs: object) -> object:
        opened = original_open(path, *args, **kwargs)
        return _ReadProbe(opened) if path == artifact_path else opened

    monkeypatch.setattr(Path, "open", probed_open)
    verify_package(
        package,
        ring,
        omega_version="3.0.0",
        platform_tag=host_tag(),
        limits=package_verifier_module.PackageVerificationLimits(
            max_artifact_bytes=artifact_limit,
            hash_chunk_bytes=artifact_limit + 100_000,
        ),
    )

    assert reads
    assert all(0 < size <= artifact_limit + 1 for size in reads)


def test_artifact_stream_rejects_before_after_native_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, ring = ephemeral_ring()
    package = build_signed_package(tmp_path, private, signing_key_id="test-release")
    real_identity = package_verifier_module.descriptor_file_identity
    calls = 0

    def changing_identity(file_descriptor: int) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        identity = real_identity(file_descriptor)
        if calls >= 4:
            return identity[0], identity[1] + 1
        return identity

    monkeypatch.setattr(
        package_verifier_module,
        "descriptor_file_identity",
        changing_identity,
    )
    with pytest.raises(PackageVerificationError, match="^artifact_digest_mismatch$"):
        verify_package(
            package,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )
