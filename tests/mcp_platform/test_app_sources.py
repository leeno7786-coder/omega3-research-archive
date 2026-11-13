"""Trusted signed MCP app source-boundary tests."""

from __future__ import annotations

import base64
import gc
import json
import weakref
from dataclasses import fields, replace
from pathlib import Path
from collections.abc import Callable

import pytest

import mcp_platform.app_sources as app_sources_module
from mcp_platform.app_sources import (
    McpAppSourceResolver,
    TrustedRemoteAppSource,
    VerifiedLocalAppSource,
    _validated_source_state,
)
from mcp_platform.app_registry import InstalledMcpAppRecord, McpAppRegistry
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, McpPackageManifest
from mcp_platform.package_verifier import VerifiedPackage, verify_package
from mcp_platform.registry import PackageRegistry
from plugin_platform.contracts import AppRequirement, CapabilityKind
from plugin_platform.package_verifier import verify_plugin_package
from tests.plugin_platform.support import (
    build_signed_plugin,
    ephemeral_publisher_ring,
)
from tests.mcp_platform.test_package_verifier import (
    _resource_catalog,
    _tool_catalog,
    ephemeral_ring,
    host_tag,
)


def _build_current_signed_package(
    root: Path,
    private,
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


def _requirement(
    package: VerifiedPackage,
    *,
    app_id: str | None = None,
    publisher_id: str | None = None,
    transports: list[str] | None = None,
) -> AppRequirement:
    tool = package.tool_catalog.tools[0]
    resource = package.resource_catalog.resources[0]
    return AppRequirement.from_dict(
        {
            "app_id": app_id or package.manifest.server_id,
            "publisher_id": publisher_id or package.manifest.publisher_id,
            "deployment": "local",
            "provider_id": None,
            "protocol_version": package.manifest.supported_mcp_protocol_versions[0],
            "transports": transports or ["stdio"],
            "remote_origin": None,
            "authorization_issuer": None,
            "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": [
                {
                    "kind": "tool",
                    "name": tool.name,
                    "schema_sha256": tool.input_schema_sha256,
                    "version": None,
                },
                {
                    "kind": "resource",
                    "name": resource.name,
                    "schema_sha256": None,
                    "version": None,
                },
            ],
        }
    )


def _change_capability(
    requirement: AppRequirement,
    *,
    name: str | None = None,
    schema_sha256: str | None = None,
) -> AppRequirement:
    document = requirement.to_dict()
    tool = next(item for item in document["capabilities"] if item["kind"] == "tool")
    if name is not None:
        tool["name"] = name
    if schema_sha256 is not None:
        tool["schema_sha256"] = schema_sha256
    return AppRequirement.from_dict(document)


def _remote_requirement(
    *,
    origin: str = "https://workspace.example",
    issuer: str = "https://auth.example/tenant",
) -> AppRequirement:
    return AppRequirement.from_dict(
        {
            "app_id": "example.remote.workspace",
            "publisher_id": "example.publisher",
            "deployment": "remote",
            "provider_id": "workspace",
            "protocol_version": "2026-07-28",
            "transports": ["streamable_http"],
            "remote_origin": origin,
            "authorization_issuer": issuer,
            "authentication": {
                "kind": "oauth_authorization_code_pkce",
                "scopes": ["workspace.read"],
                "secret_fields": [],
            },
            "capabilities": [
                {
                    "kind": "tool",
                    "name": "list_items",
                    "schema_sha256": "2" * 64,
                    "version": None,
                }
            ],
        }
    )


def _write_verified_plugin_requirement(
    root: Path,
    requirement: AppRequirement,
) -> AppRequirement:
    private, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(root, private)
    document = json.loads((package / "plugin.json").read_bytes())
    document["kind"] = "mcp_backed"
    document["required_apps"] = [requirement.to_dict()]
    document["entry_points"][0]["required_app_ids"] = [requirement.app_id]
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(private.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    (package / "plugin.json").write_bytes(canonical_bytes(document))
    return verify_plugin_package(package, ring, "3.0.0").manifest.required_apps[0]


@pytest.fixture
def verified_requirement_factory(
    tmp_path: Path,
) -> Callable[[AppRequirement], AppRequirement]:
    count = 0

    def issue(requirement: AppRequirement) -> AppRequirement:
        nonlocal count
        count += 1
        root = tmp_path / f"plugin-requirement-{count}"
        root.mkdir()
        return _write_verified_plugin_requirement(root, requirement)

    return issue


@pytest.fixture
def installed_package_registry(tmp_path: Path) -> PackageRegistry:
    private, ring = ephemeral_ring()
    source_root = tmp_path / "mcp-package-source"
    source_root.mkdir()
    package_dir = _build_current_signed_package(
        source_root,
        private,
        signing_key_id="test-release",
    )

    def verifier(package_root: Path) -> VerifiedPackage:
        return verify_package(
            package_root,
            ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )

    registry = PackageRegistry(state_root=tmp_path / "mcp-state", verifier=verifier)
    registry.install(package_dir)
    return registry


@pytest.fixture
def verified_package(tmp_path: Path) -> VerifiedPackage:
    private, ring = ephemeral_ring()
    package_dir = _build_current_signed_package(
        tmp_path,
        private,
        signing_key_id="test-release",
    )
    return verify_package(
        package_dir,
        ring,
        omega_version="3.0.0",
        platform_tag=host_tag(),
    )


def test_local_source_contains_exact_real_verified_package_and_matching_requirement(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    verified_package = installed_package_registry.reverify_installed("omega.first_party.fake")
    requirement = verified_requirement_factory(_requirement(verified_package))

    source = VerifiedLocalAppSource(
        requirement=requirement,
        registry=installed_package_registry,
    )

    assert type(source.package) is VerifiedPackage
    assert source.package.manifest_sha256 == verified_package.manifest_sha256
    assert source.requirement.app_id == source.package.manifest.server_id
    assert source.requirement.publisher_id == source.package.manifest.publisher_id
    assert source.requirement.protocol_version in (
        source.package.manifest.supported_mcp_protocol_versions
    )
    assert tuple(item.kind for item in source.requirement.capabilities) == (
        CapabilityKind.RESOURCE,
        CapabilityKind.TOOL,
    )


def test_sources_require_verifier_issued_requirement_and_installed_package_provenance(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    package = installed_package_registry.reverify_installed("omega.first_party.fake")
    nominal_local = _requirement(package)
    signed_local = verified_requirement_factory(nominal_local)
    nominal_remote = _remote_requirement()
    signed_remote = verified_requirement_factory(nominal_remote)

    with pytest.raises(ValueError, match="^mcp_app_source_invalid$"):
        VerifiedLocalAppSource(
            requirement=nominal_local,
            registry=installed_package_registry,
        )
    with pytest.raises(ValueError, match="^mcp_app_source_invalid$"):
        TrustedRemoteAppSource(requirement=nominal_remote)

    local = VerifiedLocalAppSource(
        requirement=signed_local,
        registry=installed_package_registry,
    )
    remote = TrustedRemoteAppSource(requirement=signed_remote)

    assert type(local.package) is VerifiedPackage
    assert local.package.manifest.server_id == signed_local.app_id
    assert remote.requirement.remote_origin == signed_remote.remote_origin


@pytest.mark.parametrize("source_kind", ["local", "remote"])
def test_record_factory_rejects_exact_source_instances_without_minted_provenance(
    installed_package_registry: PackageRegistry,
    source_kind: str,
) -> None:
    """Catches exact source types forged by bypassing their validating constructors."""
    if source_kind == "local":
        package = installed_package_registry.reverify_installed("omega.first_party.fake")
        source = object.__new__(VerifiedLocalAppSource)
        object.__setattr__(source, "requirement", _requirement(package))
        object.__setattr__(
            source,
            "package",
            replace(package, manifest_sha256="0" * 64),
        )
    else:
        source = object.__new__(TrustedRemoteAppSource)
        object.__setattr__(source, "requirement", _remote_requirement())

    with pytest.raises(ValueError, match="^mcp_app_identity_invalid$"):
        InstalledMcpAppRecord.from_source(source)


_SOURCE_AUTHORITY_MUTATIONS = (
    "remote_requirement",
    "remote_origin",
    "remote_issuer",
    "local_requirement",
    "local_package_snapshot",
    "local_installed_identity",
)


def _mint_then_mutate_source_authority(
    mutation: str,
    *,
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> VerifiedLocalAppSource | TrustedRemoteAppSource:
    if mutation.startswith("remote_"):
        source = TrustedRemoteAppSource(
            requirement=verified_requirement_factory(_remote_requirement())
        )
        document = source.requirement.to_dict()
        if mutation == "remote_requirement":
            document["app_id"] = "example.remote.forged"
            document["publisher_id"] = "forged.publisher"
        elif mutation == "remote_origin":
            document["remote_origin"] = "https://forged.example"
        else:
            document["authorization_issuer"] = "https://forged-auth.example"
        object.__setattr__(source, "requirement", AppRequirement.from_dict(document))
        return source

    package = installed_package_registry.reverify_installed("omega.first_party.fake")
    source = VerifiedLocalAppSource(
        requirement=verified_requirement_factory(_requirement(package)),
        registry=installed_package_registry,
    )
    if mutation == "local_requirement":
        document = source.requirement.to_dict()
        document["capabilities"] = [
            item for item in document["capabilities"] if item["kind"] == "tool"
        ]
        object.__setattr__(source, "requirement", AppRequirement.from_dict(document))
    elif mutation == "local_package_snapshot":
        snapshot = object.__getattribute__(source, "package")
        object.__setattr__(
            source,
            "package",
            replace(snapshot, manifest_sha256="0" * 64),
        )
    else:
        snapshot = object.__getattribute__(source, "package")
        manifest = json.loads(snapshot.manifest)
        manifest["server_id"] = "omega.first_party.forged"
        object.__setattr__(
            source,
            "package",
            replace(
                snapshot,
                manifest=canonical_bytes(manifest),
                manifest_sha256="1" * 64,
            ),
        )
        document = source.requirement.to_dict()
        document["app_id"] = "omega.first_party.forged"
        object.__setattr__(source, "requirement", AppRequirement.from_dict(document))
    return source


@pytest.mark.parametrize("mutation", _SOURCE_AUTHORITY_MUTATIONS)
def test_minted_source_proof_cannot_be_reused_after_authority_replacement(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
    mutation: str,
) -> None:
    """Catches proofs that bind only source identity rather than minted authority."""
    source = _mint_then_mutate_source_authority(
        mutation,
        installed_package_registry=installed_package_registry,
        verified_requirement_factory=verified_requirement_factory,
    )

    with pytest.raises(ValueError, match="^mcp_app_source_invalid$"):
        _validated_source_state(source)


@pytest.mark.parametrize("mutation", _SOURCE_AUTHORITY_MUTATIONS)
def test_proof_reuse_cannot_write_an_installed_app_record(
    tmp_path: Path,
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
    mutation: str,
) -> None:
    source = _mint_then_mutate_source_authority(
        mutation,
        installed_package_registry=installed_package_registry,
        verified_requirement_factory=verified_requirement_factory,
    )
    registry = McpAppRegistry(state_root=tmp_path / "app-state")
    expected = registry.snapshot().revision
    assert not registry.registry_path.exists()

    with pytest.raises(ValueError, match="^mcp_app_identity_invalid$"):
        registry.install(
            InstalledMcpAppRecord.from_source(source),
            expected_revision=expected,
        )

    assert not registry.registry_path.exists()


def test_source_proofs_survive_simulated_identity_reuse_without_stale_admission(
    monkeypatch: pytest.MonkeyPatch,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    simulated_identity = -8675309
    monkeypatch.setattr(app_sources_module, "id", lambda _source: simulated_identity, raising=False)
    requirement = verified_requirement_factory(_remote_requirement())
    expired = TrustedRemoteAppSource(requirement=requirement)
    expired_reference = weakref.ref(expired)
    current = TrustedRemoteAppSource(requirement=requirement)

    del expired
    gc.collect()

    assert expired_reference() is None
    validated, package = _validated_source_state(current)
    assert validated.remote_origin == "https://workspace.example"
    assert package is None

    forged = object.__new__(TrustedRemoteAppSource)
    object.__setattr__(forged, "requirement", current.requirement)
    with pytest.raises(ValueError, match="^mcp_app_source_invalid$"):
        _validated_source_state(forged)


def test_source_nested_authority_is_isolated_from_post_construction_dict_mutation(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    package = installed_package_registry.reverify_installed("omega.first_party.fake")
    signed = verified_requirement_factory(_requirement(package))
    source = VerifiedLocalAppSource(
        requirement=signed,
        registry=installed_package_registry,
    )
    exposed_package = source.package
    remote = TrustedRemoteAppSource(requirement=verified_requirement_factory(_remote_requirement()))

    with pytest.raises((AttributeError, TypeError)):
        source.requirement.__dict__["publisher_id"] = "forged.publisher"
    with pytest.raises((AttributeError, TypeError)):
        remote.requirement.__dict__["remote_origin"] = "https://forged.example"
    object.__setattr__(exposed_package, "manifest_sha256", "0" * 64)
    exposed_package.manifest.__dict__["publisher_id"] = "forged.publisher"

    assert source.requirement.publisher_id == "omega.first_party"
    assert source.package.manifest_sha256 != "0" * 64
    assert source.package.manifest.publisher_id == "omega.first_party"
    assert remote.requirement.remote_origin == "https://workspace.example"


@pytest.mark.parametrize(
    "requirement_factory",
    [
        lambda package: _requirement(package, app_id="omega.first_party.other"),
        lambda package: _requirement(package, publisher_id="other.publisher"),
        lambda package: _requirement(package, transports=["streamable_http"]),
        lambda _package: _remote_requirement(),
        lambda package: _change_capability(
            _requirement(package),
            name="undeclared_tool",
        ),
        lambda package: _change_capability(
            _requirement(package),
            schema_sha256="f" * 64,
        ),
    ],
)
def test_local_source_rejects_unverified_or_mismatched_authority(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
    requirement_factory,
) -> None:
    package = installed_package_registry.reverify_installed("omega.first_party.fake")
    requirement = verified_requirement_factory(requirement_factory(package))

    with pytest.raises(ValueError, match="^mcp_app_source_invalid$"):
        VerifiedLocalAppSource(
            requirement=requirement,
            registry=installed_package_registry,
        )


def test_local_source_rejects_non_registry_package_authority(
    verified_package: VerifiedPackage,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    requirement = verified_requirement_factory(_requirement(verified_package))

    with pytest.raises(ValueError, match="^mcp_app_source_invalid$"):
        VerifiedLocalAppSource(requirement=requirement, registry=object())  # type: ignore[arg-type]


def test_local_source_rejects_post_verification_manifest_identity_replacement(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    verified_package = installed_package_registry.reverify_installed("omega.first_party.fake")
    requirement = verified_requirement_factory(_requirement(verified_package))
    record = installed_package_registry.get("omega.first_party.fake")
    manifest_path = installed_package_registry.package_path(record) / "manifest.json"
    manifest_path.write_bytes(b"{}")

    with pytest.raises(ValueError, match="^mcp_app_source_invalid$"):
        VerifiedLocalAppSource(
            requirement=requirement,
            registry=installed_package_registry,
        )


def test_remote_source_has_no_endpoint_input_beside_signed_requirement(
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    requirement = verified_requirement_factory(_remote_requirement())

    source = TrustedRemoteAppSource(requirement=requirement)

    assert source.requirement.remote_origin == "https://workspace.example"
    assert source.requirement.authorization_issuer == "https://auth.example/tenant"
    assert {field.name for field in fields(TrustedRemoteAppSource)} == {"requirement"}


@pytest.mark.parametrize(
    "requirement_factory",
    [
        lambda package: _requirement(package),
        lambda _package: _remote_requirement(origin="https://other.example"),
        lambda _package: _remote_requirement(issuer="https://other-auth.example"),
    ],
)
def test_remote_source_identity_is_only_the_exact_signed_requirement(
    verified_package: VerifiedPackage,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
    requirement_factory,
) -> None:
    requirement = verified_requirement_factory(requirement_factory(verified_package))
    if requirement.deployment.value == "local":
        with pytest.raises(ValueError, match="^mcp_app_source_invalid$"):
            TrustedRemoteAppSource(requirement=requirement)
        return

    source = TrustedRemoteAppSource(requirement=requirement)
    assert source.requirement.remote_origin == requirement.remote_origin
    assert source.requirement.authorization_issuer == requirement.authorization_issuer


def test_source_records_expose_no_raw_or_private_authority_fields(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    local_fields = {field.name for field in fields(VerifiedLocalAppSource)}
    remote_fields = {field.name for field in fields(TrustedRemoteAppSource)}
    forbidden = {
        "command",
        "arguments",
        "endpoint",
        "token",
        "secret",
        "credential",
        "account",
        "user",
        "headers",
        "cookies",
    }

    assert local_fields == {"requirement", "package"}
    assert remote_fields == {"requirement"}
    assert forbidden.isdisjoint(local_fields)
    assert forbidden.isdisjoint(remote_fields)
    verified_package = installed_package_registry.reverify_installed("omega.first_party.fake")
    VerifiedLocalAppSource(
        requirement=verified_requirement_factory(_requirement(verified_package)),
        registry=installed_package_registry,
    )
    TrustedRemoteAppSource(requirement=verified_requirement_factory(_remote_requirement()))


def test_source_records_are_frozen_slotted_and_mutation_is_closed(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    verified_package = installed_package_registry.reverify_installed("omega.first_party.fake")
    local = VerifiedLocalAppSource(
        requirement=verified_requirement_factory(_requirement(verified_package)),
        registry=installed_package_registry,
    )
    remote = TrustedRemoteAppSource(requirement=verified_requirement_factory(_remote_requirement()))

    for source in (local, remote):
        with pytest.raises((AttributeError, TypeError)):
            source.__dict__["token"] = "forbidden"
        with pytest.raises((AttributeError, TypeError)):
            source.requirement = _remote_requirement()


def test_public_resolver_protocol_accepts_only_the_closed_resolution_shape(
    installed_package_registry: PackageRegistry,
    verified_requirement_factory: Callable[[AppRequirement], AppRequirement],
) -> None:
    verified_package = installed_package_registry.reverify_installed("omega.first_party.fake")
    local = VerifiedLocalAppSource(
        requirement=verified_requirement_factory(_requirement(verified_package)),
        registry=installed_package_registry,
    )

    class Resolver:
        def resolve(self, requirement: AppRequirement) -> VerifiedLocalAppSource:
            assert requirement == local.requirement
            return local

    resolver: McpAppSourceResolver = Resolver()

    assert isinstance(resolver, McpAppSourceResolver)
    assert resolver.resolve(local.requirement) is local
