from __future__ import annotations

import hashlib
import importlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from mcp_platform.app_registry import InstalledMcpAppRecord, McpAppRegistrySnapshot
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport as ClientMcpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, RiskClass
from plugin_platform.contracts import (
    AppDeployment,
    AppRequirement,
    AssetKind,
    CapabilityKind,
    CapabilityRequirement,
    McpTransport as PluginMcpTransport,
    PluginAuthentication,
    PluginAsset,
    PluginEntryPoint,
    PluginManifest,
)
from plugin_platform.package_verifier import VerifiedPlugin
from plugin_platform.registry import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginAppBinding,
    PluginRegistrySnapshot,
)


APP_REVISION = "2" * 64
ACCOUNT_KEY_HASH = "3" * 64
_DEFAULT_BINDING = object()
INPUT_SCHEMA = MappingProxyType(
    {
        "type": "object",
        "properties": MappingProxyType(
            {"query": MappingProxyType({"type": "string", "minLength": 1})}
        ),
        "required": ("query",),
    }
)
OUTPUT_SCHEMA = MappingProxyType(
    {
        "type": "object",
        "properties": MappingProxyType(
            {"items": MappingProxyType({"type": "array", "items": MappingProxyType({})})}
        ),
    }
)
INPUT_DIGEST = sha256_hex(
    canonical_bytes(
        {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
        }
    )
)
OUTPUT_DIGEST = sha256_hex(
    canonical_bytes(
        {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {}}},
        }
    )
)
PROMPT_ARGUMENTS = {"arguments": [{"name": "topic", "required": True}]}
PROMPT_DIGEST = sha256_hex(canonical_bytes(PROMPT_ARGUMENTS))
SKILL_BYTES = b"# Workspace guide\n\nUse verified workspace capabilities.\n"


def _authentication() -> PluginAuthentication:
    return PluginAuthentication.from_dict(
        {"kind": "none", "scopes": [], "secret_fields": []}
    )


def _catalog() -> Any:
    return importlib.import_module("mcp_platform.plugin_catalog")


def _binding(*, app_ids: tuple[str, ...] = ("example.workspace",)) -> PluginAppBinding:
    document = {
        "required_app_ids": list(app_ids),
        "optional_app_ids": [],
        "dependency_receipt_id": "receipt-1",
    }
    return PluginAppBinding(
        required_app_ids=app_ids,
        optional_app_ids=(),
        dependency_receipt_id="receipt-1",
        revision=hashlib.sha256(canonical_bytes(document)).hexdigest(),
    )


def _plugin(
    *,
    enabled: bool = True,
    status: str = "installed",
    blocker: str | None = None,
    app_binding: PluginAppBinding | None | object = _DEFAULT_BINDING,
) -> InstalledPluginRecord:
    return InstalledPluginRecord(
        plugin_id="example.plugin",
        publisher_id="example.publisher",
        kind="mcp_backed",
        selected_version="1.0.0",
        enabled=enabled,
        status=status,
        blocker=blocker,
        versions=(
            InstalledPluginVersionRecord(
                plugin_version="1.0.0",
                signing_key_id="release",
                manifest_sha256="4" * 64,
                package_sha256="5" * 64,
                state="selected",
            ),
        ),
        app_binding=_binding() if app_binding is _DEFAULT_BINDING else app_binding,
    )


def _app(
    *,
    app_id: str = "example.workspace",
    provider_id: str = "workspace",
    trusted: bool = True,
    installed: bool = True,
) -> InstalledMcpAppRecord:
    capability = CapabilityRequirement(
        kind=CapabilityKind.TOOL,
        name="search",
        schema_sha256=INPUT_DIGEST,
        version=None,
    )
    record = InstalledMcpAppRecord(
        app_id=app_id,
        publisher_id="example.publisher",
        deployment=AppDeployment.LOCAL,
        provider_id=provider_id,
        protocol_version=MCP_PROTOCOL_VERSION,
        transports=(PluginMcpTransport.STDIO,),
        remote_origin=None,
        authorization_issuer=None,
        authentication=_authentication(),
        capabilities=(capability,),
        local_package_version="1.0.0",
        local_package_platform="windows-x86_64",
        local_package_manifest_sha256="6" * 64,
        installed=True,
        trusted=True,
        enabled=False,
    )
    object.__setattr__(record, "trusted", trusted)
    object.__setattr__(record, "installed", installed)
    return record


def _app_snapshot(
    record: InstalledMcpAppRecord | tuple[InstalledMcpAppRecord, ...] | None = None,
) -> McpAppRegistrySnapshot:
    snapshot = object.__new__(McpAppRegistrySnapshot)
    records = () if record is None else (record if type(record) is tuple else (record,))
    object.__setattr__(snapshot, "records", records)
    object.__setattr__(snapshot, "revision", APP_REVISION)
    return snapshot


def _descriptor(
    *,
    app_id: str = "example.workspace",
    provider_id: str = "workspace",
    **changes: object,
) -> McpConnectionDescriptor:
    capability = CapabilityRequirement(
        kind=CapabilityKind.TOOL,
        name="search",
        schema_sha256=INPUT_DIGEST,
        version=None,
    )
    requirement = AppRequirement(
        app_id=app_id,
        publisher_id="example.publisher",
        deployment=AppDeployment.LOCAL,
        provider_id=provider_id,
        protocol_version=MCP_PROTOCOL_VERSION,
        transports=(PluginMcpTransport.STDIO,),
        remote_origin=None,
        authorization_issuer=None,
        authentication=_authentication(),
        capabilities=(capability,),
    )
    values: dict[str, object] = {
        "app_id": requirement.app_id,
        "publisher_id": requirement.publisher_id,
        "deployment": McpClientDeployment.LOCAL,
        "provider_id": provider_id,
        "protocol_version": MCP_PROTOCOL_VERSION,
        "transport": ClientMcpTransport.STDIO,
        "verified_launch_sha256": "7" * 64,
        "remote_origin": None,
        "authorization_issuer": None,
        "authentication": requirement.authentication,
        "declared_capabilities": {"tool:search": INPUT_DIGEST},
        "extension_versions": {},
        "capability_revision": project_declared_capabilities(
            requirement
        ).snapshot.capability_revision,
        "limits": McpClientLimits(),
        "signed_capability_authority": {
            "tools": {
                "search": {
                    "title": "Search workspace",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "minLength": 1}},
                        "required": ["query"],
                    },
                    "input_schema_sha256": INPUT_DIGEST,
                    "output_schema": {
                        "type": "object",
                        "properties": {"items": {"type": "array", "items": {}}},
                    },
                    "output_schema_sha256": OUTPUT_DIGEST,
                    "risk_class": "read_only",
                    "effect": "read",
                    "requires_revision": False,
                    "max_result_bytes": 65_536,
                    "allowed_mime_types": ["application/json"],
                }
            },
            "resources": {},
            "resource_templates": {},
        },
    }
    values.update(changes)
    return McpConnectionDescriptor(**values)  # type: ignore[arg-type]


def _plugin_snapshot(
    *records: InstalledPluginRecord,
    revision: str | None = None,
) -> PluginRegistrySnapshot:
    by_id = {record.plugin_id: record for record in records}
    document = {
        "schema_version": 2,
        "plugins": {key: by_id[key].to_dict() for key in sorted(by_id)},
    }
    canonical_revision = hashlib.sha256(canonical_bytes(document)).hexdigest()
    return PluginRegistrySnapshot(records, canonical_revision if revision is None else revision)


def _compile(
    *,
    plugin: InstalledPluginRecord | None = None,
    app: InstalledMcpAppRecord | None = None,
    descriptor: McpConnectionDescriptor | BaseException | None = None,
) -> tuple[Any, ...]:
    plugin = _plugin() if plugin is None else plugin
    app = _app() if app is None else app
    descriptor = _descriptor() if descriptor is None else descriptor

    def descriptor_for_app(_plugin_id: str, app_id: str) -> McpConnectionDescriptor:
        assert app_id == "example.workspace"
        if isinstance(descriptor, BaseException):
            raise descriptor
        return descriptor

    return _catalog().compile_plugin_tool_bindings(
        plugin_snapshot=_plugin_snapshot(plugin),
        app_snapshot=_app_snapshot(app),
        descriptor_for_app=descriptor_for_app,
        account_key_hash=ACCOUNT_KEY_HASH,
    )


def test_compiler_emits_one_exact_immutable_signed_binding() -> None:
    bindings = _compile()

    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.tool_name == "plugin__example.plugin__search"
    assert binding.plugin_id == "example.plugin"
    assert binding.plugin_revision == _plugin_snapshot(_plugin()).revision
    assert binding.app_id == "example.workspace"
    assert binding.account_key_hash == ACCOUNT_KEY_HASH
    assert binding.capability == "search"
    assert binding.title == "Search workspace"
    assert binding.description == "Search workspace"
    assert binding.input_schema == INPUT_SCHEMA
    assert binding.input_schema_sha256 == INPUT_DIGEST
    assert binding.output_schema == OUTPUT_SCHEMA
    assert binding.output_schema_sha256 == OUTPUT_DIGEST
    assert binding.risk_class is RiskClass.READ_ONLY
    assert binding.effect == "read"
    assert binding.requires_revision is False
    assert binding.max_result_bytes == 65_536
    assert binding.allowed_mime_types == ("application/json",)
    assert len(binding.candidate_id) == 64
    assert not any(
        value in binding.candidate_id
        for value in (binding.plugin_id, binding.app_id, binding.capability, ACCOUNT_KEY_HASH)
    )
    with pytest.raises(FrozenInstanceError):
        binding.effect = "write"  # type: ignore[misc]
    with pytest.raises(TypeError):
        binding.input_schema["type"] = "array"  # type: ignore[index]


def test_compiler_excludes_binding_without_current_connection_authority() -> None:
    bindings = _catalog().compile_plugin_tool_bindings(
        plugin_snapshot=_plugin_snapshot(_plugin()),
        app_snapshot=_app_snapshot(_app()),
        descriptor_for_app=lambda _plugin_id, _app_id: _descriptor(),
        account_key_hash=ACCOUNT_KEY_HASH,
        connection_revision_provider=lambda *_args: None,
    )

    assert bindings == ()


@pytest.mark.parametrize(
    "plugin",
    [
        _plugin(enabled=False),
        _plugin(blocker="operator_blocked"),
        _plugin(app_binding=None),
    ],
    ids=["disabled", "blocked", "legacy-unbound"],
)
def test_compiler_excludes_ineligible_plugin_state(plugin: InstalledPluginRecord) -> None:
    assert _compile(plugin=plugin) == ()


def test_compiler_excludes_an_uninstalled_plugin_absent_from_the_snapshot() -> None:
    assert _catalog().compile_plugin_tool_bindings(
        plugin_snapshot=_plugin_snapshot(),
        app_snapshot=_app_snapshot(_app()),
        descriptor_for_app=lambda _plugin_id, _app_id: _descriptor(),
        account_key_hash=ACCOUNT_KEY_HASH,
    ) == ()


@pytest.mark.parametrize(
    "app",
    [_app(trusted=False), _app(installed=False)],
    ids=["untrusted", "uninstalled"],
)
def test_compiler_excludes_ineligible_app_state(app: InstalledMcpAppRecord) -> None:
    assert _compile(app=app) == ()


def test_compiler_excludes_disconnected_and_orphan_app() -> None:
    assert _compile(descriptor=LookupError("disconnected")) == ()
    assert _catalog().compile_plugin_tool_bindings(
        plugin_snapshot=_plugin_snapshot(_plugin()),
        app_snapshot=_app_snapshot(None),
        descriptor_for_app=lambda _plugin_id, _app_id: _descriptor(),
        account_key_hash=ACCOUNT_KEY_HASH,
    ) == ()


def test_compiler_excludes_forged_durable_binding_revision() -> None:
    plugin = _plugin()
    assert plugin.app_binding is not None
    object.__setattr__(plugin.app_binding, "revision", "0" * 64)

    with pytest.raises(ValueError, match="plugin_catalog_invalid"):
        _compile(plugin=plugin)


def test_compiler_follows_only_the_durable_app_ownership_binding() -> None:
    assert _compile(plugin=_plugin(app_binding=_binding(app_ids=("example.other",)))) == ()


@pytest.mark.parametrize("drift", ["capability", "schema", "connection", "provider"])
def test_compiler_excludes_live_authority_drift(drift: str) -> None:
    descriptor = _descriptor()
    if drift == "capability":
        descriptor = replace(descriptor, declared_capabilities={"tool:other": INPUT_DIGEST})
    elif drift == "schema":
        app = _app()
        object.__setattr__(app.capabilities[0], "schema_sha256", "8" * 64)
        assert _compile(app=app, descriptor=descriptor) == ()
        return
    elif drift == "connection":
        descriptor = replace(descriptor, app_id="example.other")
    else:
        descriptor = replace(descriptor, provider_id="other")

    assert _compile(descriptor=descriptor) == ()


def test_compiler_rejects_ambiguous_multi_app_tool_names() -> None:
    app_ids = ("example.other", "example.workspace")
    plugin = _plugin(app_binding=_binding(app_ids=app_ids))
    apps = (_app(app_id=app_ids[0]), _app(app_id=app_ids[1]))
    descriptors = {app_id: _descriptor(app_id=app_id) for app_id in app_ids}

    with pytest.raises(ValueError, match="^plugin_tool_name_collision$"):
        _catalog().compile_plugin_tool_bindings(
            plugin_snapshot=_plugin_snapshot(plugin),
            app_snapshot=_app_snapshot(apps),
            descriptor_for_app=lambda _plugin_id, app_id: descriptors[app_id],
            account_key_hash=ACCOUNT_KEY_HASH,
        )


def test_compiler_recomputes_plugin_snapshot_revision_and_rejects_bad_records() -> None:
    plugin = _plugin()
    correct = _plugin_snapshot(plugin)
    with pytest.raises(ValueError, match="^plugin_catalog_invalid$"):
        _catalog().compile_plugin_tool_bindings(
            plugin_snapshot=_plugin_snapshot(plugin, revision="0" * 64),
            app_snapshot=_app_snapshot(_app()),
            descriptor_for_app=lambda _plugin_id, _app_id: _descriptor(),
            account_key_hash=ACCOUNT_KEY_HASH,
        )
    with pytest.raises(ValueError, match="^plugin_catalog_invalid$"):
        _catalog().compile_plugin_tool_bindings(
            plugin_snapshot=PluginRegistrySnapshot((plugin, plugin), correct.revision),
            app_snapshot=_app_snapshot(_app()),
            descriptor_for_app=lambda _plugin_id, _app_id: _descriptor(),
            account_key_hash=ACCOUNT_KEY_HASH,
        )
    malformed = _plugin(status="removed")
    with pytest.raises(ValueError, match="^plugin_catalog_invalid$"):
        _catalog().compile_plugin_tool_bindings(
            plugin_snapshot=_plugin_snapshot(malformed),
            app_snapshot=_app_snapshot(_app()),
            descriptor_for_app=lambda _plugin_id, _app_id: _descriptor(),
            account_key_hash=ACCOUNT_KEY_HASH,
        )


def test_compiler_sorts_by_content_free_candidate_id_without_a_global_cap() -> None:
    assert _compile() == tuple(sorted(_compile(), key=lambda item: item.candidate_id))
    catalog = _catalog()
    assert not hasattr(catalog, "_MAX_PLUGIN_TOOL_BINDINGS")
    assert not hasattr(catalog, "_MAX_PLUGIN_CATALOG_BYTES")


def test_missing_signed_authority_fails_closed_not_empty_catalog() -> None:
    with pytest.raises(ValueError, match="plugin_catalog_authority_missing"):
        _compile(descriptor=replace(_descriptor(), signed_capability_authority=None))


def test_google_drive_signed_schemas_compile_on_generic_path() -> None:
    from mcp_platform.plugin_connections import descriptor_for_record

    capability = CapabilityRequirement(
        kind=CapabilityKind.TOOL,
        name="list_recent_files",
        schema_sha256=INPUT_DIGEST,
        version=None,
    )
    app = InstalledMcpAppRecord(
        app_id="google.workspace.drive.mcp",
        publisher_id="google",
        deployment=AppDeployment.REMOTE,
        provider_id="google.workspace.drive",
        protocol_version=MCP_PROTOCOL_VERSION,
        transports=(PluginMcpTransport.STREAMABLE_HTTP,),
        remote_origin="https://drivemcp.googleapis.com",
        authorization_issuer="https://accounts.google.com",
        authentication=PluginAuthentication.from_dict(
            {
                "kind": "oauth_authorization_code_pkce",
                "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
                "secret_fields": [],
            }
        ),
        capabilities=(capability,),
        local_package_version=None,
        local_package_platform=None,
        local_package_manifest_sha256=None,
        installed=True,
        trusted=True,
        enabled=False,
    )
    authority = {
        "tools": {
            "list_recent_files": {
                "title": "List recent files",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "minLength": 1}},
                    "required": ["query"],
                },
                "input_schema_sha256": INPUT_DIGEST,
                "output_schema": {
                    "type": "object",
                    "properties": {"items": {"type": "array", "items": {}}},
                },
                "output_schema_sha256": OUTPUT_DIGEST,
                "risk_class": "read_only",
                "effect": "read",
                "requires_revision": False,
                "max_result_bytes": 65_536,
                "allowed_mime_types": ["application/json"],
            }
        },
        "resources": {},
        "resource_templates": {},
    }
    descriptor = descriptor_for_record(app, signed_capability_authority=authority)
    plugin = _plugin(app_binding=_binding(app_ids=("google.workspace.drive.mcp",)))

    def descriptor_for_app(_plugin_id: str, app_id: str):
        assert app_id == "google.workspace.drive.mcp"
        return descriptor

    bindings = _catalog().compile_plugin_tool_bindings(
        plugin_snapshot=_plugin_snapshot(plugin),
        app_snapshot=_app_snapshot(app),
        descriptor_for_app=descriptor_for_app,
        account_key_hash=ACCOUNT_KEY_HASH,
    )

    assert descriptor.signed_capability_authority is not None
    assert tuple(item.capability for item in bindings) == ("list_recent_files",)


def _surface_fixture() -> tuple[
    InstalledPluginRecord,
    InstalledMcpAppRecord,
    McpConnectionDescriptor,
    VerifiedPlugin,
]:
    capabilities = (
        CapabilityRequirement(CapabilityKind.TOOL, "search", INPUT_DIGEST, None),
        CapabilityRequirement(CapabilityKind.PROMPT, "brief", PROMPT_DIGEST, None),
        CapabilityRequirement(CapabilityKind.RESOURCE, "handbook", None, None),
        CapabilityRequirement(CapabilityKind.RESOURCE, "project_doc", None, None),
    )
    requirement = AppRequirement(
        app_id="example.workspace",
        publisher_id="example.publisher",
        deployment=AppDeployment.LOCAL,
        provider_id="workspace",
        protocol_version=MCP_PROTOCOL_VERSION,
        transports=(PluginMcpTransport.STDIO,),
        remote_origin=None,
        authorization_issuer=None,
        authentication=_authentication(),
        capabilities=capabilities,
    )
    app = InstalledMcpAppRecord(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=requirement.deployment,
        provider_id=requirement.provider_id,
        protocol_version=requirement.protocol_version,
        transports=requirement.transports,
        remote_origin=None,
        authorization_issuer=None,
        authentication=requirement.authentication,
        capabilities=capabilities,
        local_package_version="1.0.0",
        local_package_platform="windows-x86_64",
        local_package_manifest_sha256="6" * 64,
        installed=True,
        trusted=True,
        enabled=False,
    )
    authority = {
        **_descriptor().signed_capability_authority,
        "resources": {
            "handbook": {
                "uri": "docs://handbook",
                "mime_type": "text/plain",
                "max_bytes": 4_096,
            }
        },
        "resource_templates": {
            "project_doc": {
                "uri_template": "docs://projects/{project}",
                "mime_type": "text/plain",
                "max_bytes": 8_192,
            }
        },
        "prompts": {
            "brief": {
                "title": "Draft a brief",
                **PROMPT_ARGUMENTS,
                "schema_sha256": PROMPT_DIGEST,
                "max_result_bytes": 16_384,
            }
        },
    }
    descriptor = McpConnectionDescriptor(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=McpClientDeployment.LOCAL,
        provider_id=requirement.provider_id,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=ClientMcpTransport.STDIO,
        verified_launch_sha256="7" * 64,
        remote_origin=None,
        authorization_issuer=None,
        authentication=requirement.authentication,
        declared_capabilities={
            f"{item.kind.value}:{item.name}": item.schema_sha256
            for item in capabilities
        },
        extension_versions={},
        capability_revision=project_declared_capabilities(
            requirement
        ).snapshot.capability_revision,
        limits=McpClientLimits(),
        signed_capability_authority=authority,
    )
    skill_sha = sha256_hex(SKILL_BYTES)
    manifest = PluginManifest.from_dict(
        {
            "manifest_version": 2,
            "plugin_id": "example.plugin",
            "publisher_id": "example.publisher",
            "plugin_version": "1.0.0",
            "minimum_omega_version": "3.0.0",
            "kind": "mcp_backed",
            "title": "Example plugin",
            "description": "All runtime surfaces.",
            "assets": [
                {
                    "path": "skills/workspace/SKILL.md",
                    "kind": "skill",
                    "media_type": "text/markdown",
                    "sha256": skill_sha,
                }
            ],
            "entry_points": [
                {
                    "entry_point_id": "workspace_guide",
                    "title": "Workspace guide",
                    "skill_path": "skills/workspace/SKILL.md",
                    "required_app_ids": ["example.workspace"],
                }
            ],
            "starter_requests": ["Guide me through the workspace."],
            "required_apps": [requirement.to_dict()],
            "optional_apps": [],
            "schedule_template_paths": [],
            "proactive_event_rules": [],
            "enterprise_roles": [],
            "signing_key_id": "release",
            "signature": "signed",
        }
    )
    verified = VerifiedPlugin(
        manifest=manifest,
        package_root=Path("unused"),
        manifest_sha256="4" * 64,
        package_sha256="5" * 64,
        asset_paths={"skills/workspace/SKILL.md": Path("unused/SKILL.md")},
        asset_bytes={"skills/workspace/SKILL.md": SKILL_BYTES},
    )
    return _plugin(), app, descriptor, verified


def test_runtime_catalog_projects_every_authorized_surface_in_stable_order() -> None:
    plugin, app, descriptor, verified = _surface_fixture()
    catalog = _catalog()
    authority = catalog.PluginCatalogAppAuthority(
        app_id=app.app_id,
        account_key_hash=ACCOUNT_KEY_HASH,
        connection_revision="8" * 64,
        authority_revision="9" * 64,
        capability_revision=descriptor.capability_revision,
    )

    snapshot = catalog.compile_plugin_runtime_catalog(
        plugin_snapshot=_plugin_snapshot(plugin),
        app_snapshot=_app_snapshot(app),
        descriptor_for_app=lambda _plugin_id, _app_id: descriptor,
        account_key_hash=ACCOUNT_KEY_HASH,
        app_authority_provider=lambda *_args: authority,
        verified_plugin_provider=lambda _plugin_id: verified,
    )

    assert [item.capability_kind for item in snapshot.entries] == [
        "prompt",
        "resource",
        "resource_template",
        "skill",
        "tool",
    ]
    assert snapshot.tools[0].package_revision == "5" * 64
    assert snapshot.resources[0].uri == "docs://handbook"
    assert snapshot.resource_templates[0].uri_template == "docs://projects/{project}"
    assert snapshot.prompts[0].argument_schema_sha256 == PROMPT_DIGEST
    assert snapshot.skills[0].asset_sha256 == sha256_hex(SKILL_BYTES)
    assert tuple(snapshot.entries) == tuple(
        sorted(snapshot.entries, key=lambda item: (item.capability_kind, item.candidate_id))
    )


def test_runtime_catalog_keeps_every_surface_for_multiple_arbitrary_plugins() -> None:
    from mcp_platform.plugin_connections import descriptor_for_record

    first_plugin, first_app, first_descriptor, first_verified = _surface_fixture()
    second_plugin_id = "arbitrary.second-plugin"
    second_app_id = "arbitrary.second-app"
    second_plugin = replace(
        first_plugin,
        plugin_id=second_plugin_id,
        app_binding=_binding(app_ids=(second_app_id,)),
    )
    second_app = replace(
        first_app,
        app_id=second_app_id,
        provider_id="arbitrary-provider",
    )
    second_descriptor = descriptor_for_record(
        second_app,
        signed_capability_authority=first_descriptor.signed_capability_authority,
    )
    manifest_document = first_verified.manifest.to_dict()
    manifest_document["plugin_id"] = second_plugin_id
    manifest_document["required_apps"][0]["app_id"] = second_app_id
    manifest_document["entry_points"][0]["required_app_ids"] = [second_app_id]
    second_verified = replace(
        first_verified,
        manifest=PluginManifest.from_dict(manifest_document),
    )
    descriptors = {
        first_app.app_id: first_descriptor,
        second_app.app_id: second_descriptor,
    }
    verified = {
        first_plugin.plugin_id: first_verified,
        second_plugin.plugin_id: second_verified,
    }
    catalog = _catalog()

    snapshot = catalog.compile_plugin_runtime_catalog(
        plugin_snapshot=_plugin_snapshot(second_plugin, first_plugin),
        app_snapshot=_app_snapshot((first_app, second_app)),
        descriptor_for_app=lambda _plugin_id, app_id: descriptors[app_id],
        account_key_hash=ACCOUNT_KEY_HASH,
        app_authority_provider=lambda _plugin_id, app_id, account, descriptor: (
            catalog.PluginCatalogAppAuthority(
                app_id=app_id,
                account_key_hash=account,
                connection_revision=sha256_hex(canonical_bytes(descriptor.to_dict())),
                authority_revision=sha256_hex(
                    canonical_bytes(descriptor.to_dict()["signed_capability_authority"])
                ),
                capability_revision=descriptor.capability_revision,
            )
        ),
        verified_plugin_provider=verified.__getitem__,
    )

    assert {item.plugin_id for item in snapshot.entries} == {
        first_plugin.plugin_id,
        second_plugin.plugin_id,
    }
    assert {
        plugin_id: [
            item.capability_kind
            for item in snapshot.entries
            if item.plugin_id == plugin_id
        ]
        for plugin_id in (first_plugin.plugin_id, second_plugin.plugin_id)
    } == {
        first_plugin.plugin_id: [
            "prompt",
            "resource",
            "resource_template",
            "skill",
            "tool",
        ],
        second_plugin.plugin_id: [
            "prompt",
            "resource",
            "resource_template",
            "skill",
            "tool",
        ],
    }


def test_runtime_catalog_compiles_zero_bindings_for_legacy_authority_plugin() -> None:
    """Catches a legacy-authority package silently keeping any compiled surface.

    Spec principle 8 forbids a compatibility fork: an installed package the
    verifier flagged ``legacy_authority`` must compile zero tool, resource,
    template, prompt, and skill bindings -- including a skill entry point
    whose app connection is otherwise live -- until the sequence-8 v2
    package replaces it.  A second, unaffected plugin proves the catalog
    keeps compiling normally around it.
    """
    from mcp_platform.plugin_connections import descriptor_for_record

    first_plugin, first_app, first_descriptor, first_verified = _surface_fixture()
    second_plugin_id = "legacy.second-plugin"
    second_app_id = "legacy.second-app"
    second_plugin = replace(
        first_plugin,
        plugin_id=second_plugin_id,
        app_binding=_binding(app_ids=(second_app_id,)),
    )
    second_app = replace(
        first_app,
        app_id=second_app_id,
        provider_id="legacy-provider",
    )
    second_descriptor = descriptor_for_record(second_app, signed_capability_authority=None)
    manifest_document = first_verified.manifest.to_dict()
    manifest_document["plugin_id"] = second_plugin_id
    manifest_document["required_apps"][0]["app_id"] = second_app_id
    manifest_document["entry_points"][0]["required_app_ids"] = [second_app_id]
    second_verified = replace(
        first_verified,
        manifest=PluginManifest.from_dict(manifest_document),
        legacy_authority=True,
    )
    descriptors = {first_app.app_id: first_descriptor, second_app.app_id: second_descriptor}
    verified = {first_plugin.plugin_id: first_verified, second_plugin.plugin_id: second_verified}
    catalog = _catalog()

    snapshot = catalog.compile_plugin_runtime_catalog(
        plugin_snapshot=_plugin_snapshot(first_plugin, second_plugin),
        app_snapshot=_app_snapshot((first_app, second_app)),
        descriptor_for_app=lambda _plugin_id, app_id: descriptors[app_id],
        account_key_hash=ACCOUNT_KEY_HASH,
        app_authority_provider=lambda _plugin_id, app_id, account, descriptor: (
            catalog.PluginCatalogAppAuthority(
                app_id=app_id,
                account_key_hash=account,
                connection_revision=sha256_hex(canonical_bytes(descriptor.to_dict())),
                authority_revision=sha256_hex(canonical_bytes({"app_id": app_id})),
                capability_revision=descriptor.capability_revision,
            )
        ),
        verified_plugin_provider=verified.__getitem__,
    )

    assert {item.plugin_id for item in snapshot.entries} == {first_plugin.plugin_id}
    assert [item.capability_kind for item in snapshot.entries] == [
        "prompt",
        "resource",
        "resource_template",
        "skill",
        "tool",
    ]


def test_local_catalog_authority_is_exact_and_side_effect_free() -> None:
    from mcp_platform.plugin_lifecycle import PluginLifecycleService

    plugin, app, descriptor, verified = _surface_fixture()
    account = ACCOUNT_KEY_HASH
    durable = SimpleNamespace(
        plugin_id=plugin.plugin_id,
        app_id=app.app_id,
        account_key_hash=account,
        connection_revision=sha256_hex(canonical_bytes(descriptor.to_dict())),
        revision=sha256_hex(
            canonical_bytes(descriptor.to_dict()["signed_capability_authority"])
        ),
        capability_ceiling=("tool:search",),
    )

    class _Registry:
        def reverify_installed(self, plugin_id: str) -> VerifiedPlugin:
            assert plugin_id == plugin.plugin_id
            return verified

    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = _Registry()
    service.plugin_grants = None

    def current_credentialless_authority(
        candidate: object,
        *,
        plugin_id: str,
        descriptor: McpConnectionDescriptor,
    ) -> tuple[str, tuple[object, ...]]:
        assert candidate is durable
        assert plugin_id == plugin.plugin_id
        assert descriptor is descriptor_for_current_authority
        return "f" * 64, ()

    descriptor_for_current_authority = descriptor
    service._connection_service = SimpleNamespace(
        authority_store=SimpleNamespace(connection=lambda *_args: durable),
        _credentialless_authority_is_current=current_credentialless_authority,
    )

    authority = service.planning_app_authority(
        plugin.plugin_id,
        app.app_id,
        account,
        descriptor,
    )

    assert authority is not None
    assert authority.connection_revision == durable.connection_revision
    assert authority.authority_revision == sha256_hex(
        canonical_bytes(descriptor.to_dict()["signed_capability_authority"])
    )
    assert authority.capability_revision == descriptor.capability_revision
    # The operator's selected tools travel with the authority the catalog serves from.
    assert authority.capability_ceiling == ("tool:search",)


def test_lifecycle_local_descriptor_binds_reverified_executable(tmp_path: Path) -> None:
    from mcp_platform.plugin_lifecycle import PluginLifecycleService
    from tests.mcp_platform.generic_plugin_install import (
        install_signed_northwind_plugins,
    )
    from tests.mcp_platform.generic_plugin_peers import CALENDAR_APP, CALENDAR_PLUGIN

    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    service = object.__new__(PluginLifecycleService)
    service.package_registry = installed["package_registry"]
    service.plugin_registry = installed["plugin_registry"]
    service.plugin_id_for_app = lambda app_id: (
        CALENDAR_PLUGIN
        if app_id == CALENDAR_APP
        else (_ for _ in ()).throw(KeyError(app_id))
    )

    descriptor = service._descriptor_for_record(
        CALENDAR_PLUGIN,
        installed["app_registry"].get(CALENDAR_APP)
    )
    package = installed["package_registry"].reverify_installed(CALENDAR_APP)

    assert descriptor.verified_launch_sha256 == package.manifest.artifact_sha256
    assert descriptor.verified_launch_sha256 != package.manifest_sha256


def test_lifecycle_retains_local_package_verifier_for_worker_authority(
    tmp_path: Path,
) -> None:
    from mcp_platform.plugin_lifecycle import PluginLifecycleService
    from mcp_platform.registry import McpPackageNotInstalled

    service = PluginLifecycleService(state_root=tmp_path)
    try:
        with pytest.raises(McpPackageNotInstalled, match="^package_not_installed$"):
            service.package_registry.reverify_installed("missing.local.app")
    finally:
        service.close()


def test_runtime_catalog_projects_signed_skill_only_plugin_without_fake_app() -> None:
    plugin, _app, _descriptor_value, verified = _surface_fixture()
    manifest_document = verified.manifest.to_dict()
    manifest_document["kind"] = "skill_only"
    manifest_document["required_apps"] = []
    manifest_document["entry_points"][0]["required_app_ids"] = []
    skill_verified = replace(
        verified,
        manifest=PluginManifest.from_dict(manifest_document),
    )
    skill_plugin = replace(
        plugin,
        kind="skill_only",
        app_binding=None,
    )

    def no_descriptor(_plugin_id: str, _app_id: str) -> McpConnectionDescriptor:
        raise AssertionError("skill-only catalog must not resolve a fake app")

    snapshot = _catalog().compile_plugin_runtime_catalog(
        plugin_snapshot=_plugin_snapshot(skill_plugin),
        app_snapshot=_app_snapshot(),
        descriptor_for_app=no_descriptor,
        account_key_hash=ACCOUNT_KEY_HASH,
        verified_plugin_provider=lambda _plugin_id: skill_verified,
    )

    assert snapshot.tools == ()
    assert snapshot.resources == ()
    assert snapshot.resource_templates == ()
    assert snapshot.prompts == ()
    assert [item.capability for item in snapshot.skills] == ["workspace_guide"]
    assert snapshot.skills[0].required_app_ids == ()
    assert snapshot.skills[0].app_authorities == ()


@pytest.mark.parametrize("failure", ["unsigned", "mutated", "stale", "traversal"])
def test_runtime_catalog_excludes_unverified_skill_assets(failure: str) -> None:
    plugin, app, descriptor, verified = _surface_fixture()
    catalog = _catalog()
    authority = catalog.PluginCatalogAppAuthority(
        app_id=app.app_id,
        account_key_hash=ACCOUNT_KEY_HASH,
        connection_revision="8" * 64,
        authority_revision="9" * 64,
        capability_revision=descriptor.capability_revision,
    )
    provider = None
    if failure == "mutated":
        def provider(_plugin_id: str) -> VerifiedPlugin:
            return replace(
                verified,
                asset_bytes={"skills/workspace/SKILL.md": SKILL_BYTES + b"mutated"},
            )
    elif failure == "stale":
        def provider(_plugin_id: str) -> VerifiedPlugin:
            return replace(verified, package_sha256="a" * 64)
    elif failure == "traversal":
        traversal_path = "../outside/SKILL.md"
        traversal_manifest = replace(
            verified.manifest,
            assets=(
                PluginAsset(
                    path=traversal_path,
                    kind=AssetKind.SKILL,
                    media_type="text/markdown",
                    sha256=sha256_hex(SKILL_BYTES),
                ),
            ),
            entry_points=(
                PluginEntryPoint(
                    entry_point_id="workspace_guide",
                    title="Workspace guide",
                    skill_path=traversal_path,
                    required_app_ids=(app.app_id,),
                ),
            ),
        )
        def provider(_plugin_id: str) -> VerifiedPlugin:
            return replace(
                verified,
                manifest=traversal_manifest,
                asset_paths={traversal_path: Path("outside/SKILL.md")},
                asset_bytes={traversal_path: SKILL_BYTES},
            )

    snapshot = catalog.compile_plugin_runtime_catalog(
        plugin_snapshot=_plugin_snapshot(plugin),
        app_snapshot=_app_snapshot(app),
        descriptor_for_app=lambda _plugin_id, _app_id: descriptor,
        account_key_hash=ACCOUNT_KEY_HASH,
        app_authority_provider=lambda *_args: authority,
        verified_plugin_provider=provider,
    )

    assert snapshot.skills == ()
