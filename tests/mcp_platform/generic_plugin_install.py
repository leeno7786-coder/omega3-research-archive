"""Product-path PluginInstaller helpers for generic plugin acceptance."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from mcp_platform.app_registry import McpAppRegistry
from mcp_platform.app_sources import TrustedRemoteAppSource, VerifiedLocalAppSource
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, McpPackageManifest
from mcp_platform.package_verifier import verify_package
from mcp_platform.plugin_connections import descriptor_for_record
from mcp_platform.provisioner import McpAppProvisioner
from mcp_platform.registry import PackageRegistry
from plugin_platform.catalog import verify_plugin_catalog
from plugin_platform.dependencies import resolve_dependencies
from plugin_platform.installer import PluginInstaller, PluginRegistry
from plugin_platform.package_verifier import verify_plugin_package
from tests.mcp_platform.generic_plugin_peers import (
    CALENDAR_APP,
    CALENDAR_MIRROR_PLUGIN,
    CALENDAR_PLUGIN,
    LEDGER_APP,
    LEDGER_PLUGIN,
    TRACKER_APP,
    TRACKER_ISSUER,
    TRACKER_ORIGIN,
    TRACKER_PLUGIN,
    ZERO_TOOL_PEERS,
    catalogs_for,
)
from tests.mcp_platform.test_package_verifier import ephemeral_ring, host_tag
from tests.plugin_platform.support import (
    ephemeral_publisher_ring,
    marketplace_entry_document,
)

_SKILL = b"# Northwind\n\nUse the signed Northwind plugin deliberately.\n"
_OMEGA_VERSION = "3.0.0"


class _DigestConfirmation:
    def consume(self, confirmation: object, expected_review_sha256: str) -> None:
        if type(confirmation) is not str or confirmation != expected_review_sha256:
            from plugin_platform.installer import PluginInstallError

            raise PluginInstallError("plugin_install_confirmation_mismatch")


class _SignedAppResolver:
    def __init__(self, package_registry: PackageRegistry) -> None:
        self._package_registry = package_registry

    def resolve(self, requirement: Any) -> TrustedRemoteAppSource | VerifiedLocalAppSource:
        if requirement.deployment.value == "remote":
            return TrustedRemoteAppSource(requirement=requirement)
        return VerifiedLocalAppSource(requirement=requirement, registry=self._package_registry)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _prompt_policy(peer: str) -> dict[str, object]:
    if peer != "prompt":
        return {}
    arguments = [{"name": "topic", "required": True}]
    return {
        "records.summarize": {
            "title": "Summarize records",
            "arguments": arguments,
            "schema_sha256": sha256_hex(canonical_bytes({"arguments": arguments})),
            "max_result_bytes": 4096,
        }
    }


def signed_authority_from_catalog(
    tool_catalog: Any,
    resource_catalog: Any | None = None,
    *,
    prompts: Mapping[str, object] | None = None,
) -> dict[str, object]:
    resources = () if resource_catalog is None else resource_catalog.resources
    templates = () if resource_catalog is None else resource_catalog.resource_templates
    return {
        "tools": {
            tool.name: {
                "title": tool.title,
                "input_schema": _plain_json(tool.input_schema),
                "input_schema_sha256": tool.input_schema_sha256,
                "output_schema": _plain_json(tool.output_schema),
                "output_schema_sha256": tool.output_schema_sha256,
                "risk_class": tool.risk_class.value,
                "effect": tool.effect,
                "requires_revision": tool.requires_revision,
                "max_result_bytes": tool.max_result_bytes,
                "allowed_mime_types": list(tool.allowed_mime_types),
            }
            for tool in tool_catalog.tools
        },
        "resources": {
            item.name: {
                "uri": item.uri,
                "mime_type": item.mime_type,
                "max_bytes": item.max_bytes,
            }
            for item in resources
        },
        "resource_templates": {
            item.name: {
                "uri_template": item.uri_template,
                "mime_type": item.mime_type,
                "max_bytes": item.max_bytes,
            }
            for item in templates
        },
        "prompts": dict(prompts or {}),
    }


def signed_authority_from_package(package_registry: PackageRegistry, app_id: str) -> dict[str, object]:
    package = package_registry.reverify_installed(app_id)
    return signed_authority_from_catalog(package.tool_catalog, package.resource_catalog)


def descriptor_from_installed(
    app_registry: McpAppRegistry,
    package_registry: PackageRegistry,
    app_id: str,
) -> Any:
    record = app_registry.get(app_id)
    descriptor = descriptor_for_record(
        record,
        signed_capability_authority=signed_authority_from_package(package_registry, app_id),
    )
    if record.deployment.value == "local":
        descriptor = replace(
            descriptor,
            verified_launch_sha256=(
                package_registry.reverify_installed(app_id).manifest.artifact_sha256
            ),
        )
    return descriptor


def _app_spec(
    peer: str,
    *,
    tracker_authentication_kind: str = "none",
    tracker_remote_origin: str = TRACKER_ORIGIN,
    tracker_scopes: tuple[str, ...] = ("issues.read", "issues.write"),
    tracker_app_id: str = TRACKER_APP,
) -> dict[str, object]:
    tool_catalog, resource_catalog = catalogs_for(peer)
    capabilities = [
        {
            "kind": "tool",
            "name": tool.name,
            "schema_sha256": tool.input_schema_sha256,
            "version": None,
        }
        for tool in tool_catalog.tools
    ]
    capabilities.extend(
        {
            "kind": "resource",
            "name": item.name,
            "schema_sha256": None,
            "version": None,
        }
        for item in (*resource_catalog.resources, *resource_catalog.resource_templates)
    )
    for name, policy in _prompt_policy(peer).items():
        capabilities.append(
            {
                "kind": "prompt",
                "name": name,
                "schema_sha256": policy["schema_sha256"],
                "version": None,
            }
        )
    if peer == "tracker" or peer in ZERO_TOOL_PEERS:
        app_id = tracker_app_id if peer == "tracker" else ZERO_TOOL_PEERS[peer][1]
        return {
            "app_id": app_id,
            "publisher_id": "omega.first_party",
            "deployment": "remote",
            "provider_id": "northwind",
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": ["streamable_http"],
            "remote_origin": tracker_remote_origin,
            "authorization_issuer": TRACKER_ISSUER,
            "authentication": {
                "kind": tracker_authentication_kind,
                "scopes": (
                    []
                    if tracker_authentication_kind == "none"
                    else list(tracker_scopes)
                ),
                "secret_fields": [],
            },
            "capabilities": capabilities,
        }
    app_id = CALENDAR_APP if peer == "calendar" else LEDGER_APP
    return {
        "app_id": app_id,
        "publisher_id": "omega.first_party",
        "deployment": "local",
        "provider_id": "northwind",
        "protocol_version": MCP_PROTOCOL_VERSION,
        "transports": ["stdio"],
        "remote_origin": None,
        "authorization_issuer": None,
        "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
        "capabilities": capabilities,
    }


def _write_mcp_package(root: Path, peer: str, private: Any) -> Path:
    tool_catalog, resource_catalog = catalogs_for(peer)
    package = root / f"mcp-{peer}"
    package.mkdir(parents=True)
    artifact = f"northwind-{peer}-artifact\n".encode("utf-8")
    artifact_name = f"northwind-{peer}.exe"
    (package / artifact_name).write_bytes(artifact)
    tools = tool_catalog.to_dict()
    resources = resource_catalog.to_dict()
    (package / "tool_catalog.json").write_bytes(canonical_bytes(tools))
    (package / "resource_catalog.json").write_bytes(canonical_bytes(resources))
    document: dict[str, object] = {
        "manifest_version": 1,
        "server_id": tool_catalog.server_id,
        "publisher_id": "omega.first_party",
        "server_version": "1.0.0",
        "minimum_omega_version": _OMEGA_VERSION,
        "supported_mcp_protocol_versions": [MCP_PROTOCOL_VERSION],
        "network_class": "local_only",
        "platform": host_tag(),
        "entrypoint": {"artifact": artifact_name, "arguments": []},
        "artifact_sha256": sha256_hex(artifact),
        "tool_catalog_sha256": sha256_hex(canonical_bytes(tools)),
        "resource_catalog_sha256": sha256_hex(canonical_bytes(resources)),
        "capability_tags": [tool.name.replace("_", ".") for tool in tool_catalog.tools],
        "risk_declarations": (
            [tool.risk_class.value for tool in tool_catalog.tools] or ["read_only"]
        ),
        "oauth_providers": [],
        "allowed_network_destinations": [],
        "signing_key_id": "test-release",
        "signature": base64.b64encode(b"0" * 64).decode("ascii"),
    }
    unsigned = McpPackageManifest.from_dict(document)
    document["signature"] = base64.b64encode(private.sign(unsigned.signing_bytes())).decode("ascii")
    (package / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    return package


def _write_plugin_package(
    root: Path,
    *,
    plugin_id: str,
    peer: str,
    private_key: Any,
    tracker_authentication_kind: str = "none",
    plugin_version: str = "1.0.0",
    tracker_remote_origin: str = TRACKER_ORIGIN,
    tracker_scopes: tuple[str, ...] = ("issues.read", "issues.write"),
    tracker_app_id: str = TRACKER_APP,
    include_optional_tracker: bool = False,
    include_required_tracker: bool = False,
    plugin_kind: str = "mcp_backed",
) -> Path:
    package = root / f"plugin-{plugin_id}-{plugin_version}"
    skill_path = package / "skills" / "northwind" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(_SKILL)
    tool_catalog, resource_catalog = catalogs_for(peer)
    spec = _app_spec(
        peer,
        tracker_authentication_kind=tracker_authentication_kind,
        tracker_remote_origin=tracker_remote_origin,
        tracker_scopes=tracker_scopes,
        tracker_app_id=tracker_app_id,
    )
    optional_tracker = _app_spec(
        "tracker",
        tracker_authentication_kind=tracker_authentication_kind,
        tracker_remote_origin=tracker_remote_origin,
        tracker_scopes=tracker_scopes,
        tracker_app_id=tracker_app_id,
    )
    apps = {
        spec["app_id"]: signed_authority_from_catalog(
            tool_catalog,
            resource_catalog,
            prompts=_prompt_policy(peer),
        )
    }
    if include_optional_tracker or include_required_tracker:
        tracker_catalog, tracker_resources = catalogs_for("tracker")
        apps[optional_tracker["app_id"]] = signed_authority_from_catalog(
            tracker_catalog,
            tracker_resources,
        )
    authority = canonical_bytes(
        {
            "schema_version": 2,
            "plugin_id": plugin_id,
            "apps": apps,
        }
    )
    authority_path = package / "tools" / "authority.json"
    authority_path.parent.mkdir(parents=True)
    authority_path.write_bytes(authority)
    unsigned: dict[str, object] = {
        "manifest_version": 2,
        "plugin_id": plugin_id,
        "publisher_id": "test.publisher",
        "plugin_version": plugin_version,
        "minimum_omega_version": _OMEGA_VERSION,
        "kind": plugin_kind,
        "title": plugin_id,
        "description": f"Signed {plugin_id} plugin.",
        "assets": [
            {
                "path": "skills/northwind/SKILL.md",
                "kind": "skill",
                "media_type": "text/markdown",
                "sha256": hashlib.sha256(_SKILL).hexdigest(),
            },
            {
                "path": "tools/authority.json",
                "kind": "capability_authority",
                "media_type": "application/json",
                "sha256": hashlib.sha256(authority).hexdigest(),
            },
        ],
        "entry_points": [
            {
                "entry_point_id": "get_started",
                "title": "Get started",
                "skill_path": "skills/northwind/SKILL.md",
                "required_app_ids": [
                    spec["app_id"],
                    *([optional_tracker["app_id"]] if include_required_tracker else []),
                ],
            }
        ],
        "starter_requests": [f"Help me use {plugin_id}."],
        "required_apps": [
            spec,
            *([optional_tracker] if include_required_tracker else []),
        ],
        "optional_apps": [
            optional_tracker
        ] if include_optional_tracker and not include_required_tracker else [],
        "schedule_template_paths": [],
        "proactive_event_rules": [],
        "enterprise_roles": [],
        "signing_key_id": "test-plugin-key",
    }
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    (package / "plugin.json").write_bytes(canonical_bytes({**unsigned, "signature": signature}))
    return package


def stage_signed_northwind_candidate(
    tmp_path: Path,
    installed: dict[str, Any],
    *,
    plugin_id: str = TRACKER_PLUGIN,
    peer: str = "tracker",
    plugin_version: str = "2.0.0",
    tracker_authentication_kind: str = "oauth_authorization_code_pkce",
    tracker_remote_origin: str = TRACKER_ORIGIN,
    tracker_scopes: tuple[str, ...] = ("issues.read", "issues.write"),
    tracker_app_id: str = TRACKER_APP,
    include_optional_tracker: bool = False,
) -> tuple[Any, Any]:
    """Stage one exact signed update through the real installer/provisioner."""

    catalog, verified, review = review_signed_northwind_candidate(
        tmp_path,
        installed,
        plugin_id=plugin_id,
        peer=peer,
        plugin_version=plugin_version,
        tracker_authentication_kind=tracker_authentication_kind,
        tracker_remote_origin=tracker_remote_origin,
        tracker_scopes=tracker_scopes,
        tracker_app_id=tracker_app_id,
        include_optional_tracker=include_optional_tracker,
    )
    result = installed["installer"].install_from_catalog(
        catalog,
        catalog.entries[0],
        verified,
        review.sha256,
        preserve_existing_binding=True,
    )
    return verified, result


def review_signed_northwind_candidate(
    tmp_path: Path,
    installed: dict[str, Any],
    *,
    plugin_id: str = TRACKER_PLUGIN,
    peer: str = "tracker",
    plugin_version: str = "2.0.0",
    tracker_authentication_kind: str = "oauth_authorization_code_pkce",
    tracker_remote_origin: str = TRACKER_ORIGIN,
    tracker_scopes: tuple[str, ...] = ("issues.read", "issues.write"),
    tracker_app_id: str = TRACKER_APP,
    include_optional_tracker: bool = False,
) -> tuple[Any, Any, Any]:
    """Sign and review one exact update release without installing it.

    Returns ``(catalog, verified, review)``: the signed catalog carrying the
    release, its verified package, and the installer review whose ``sha256`` is
    the confirmation an update consumes.
    """

    private_key = installed["plugin_private_key"]
    key_ring = installed["plugin_key_ring"]
    package = _write_plugin_package(
        tmp_path / "candidate-packages",
        plugin_id=plugin_id,
        peer=peer,
        private_key=private_key,
        tracker_authentication_kind=tracker_authentication_kind,
        plugin_version=plugin_version,
        tracker_remote_origin=tracker_remote_origin,
        tracker_scopes=tracker_scopes,
        tracker_app_id=tracker_app_id,
        include_optional_tracker=include_optional_tracker,
    )
    verified = verify_plugin_package(package, key_ring, _OMEGA_VERSION)
    entry_document = marketplace_entry_document(
        plugin_id=plugin_id,
        publisher_id="test.publisher",
        plugin_version=plugin_version,
        manifest_sha256=verified.manifest_sha256,
        package_sha256=verified.package_sha256,
        package_uri=f"https://plugins.northwind.test/{plugin_id}/{plugin_version}",
    )
    unsigned_catalog: dict[str, object] = {
        "schema_version": 2,
        "catalog_id": "northwind.update-catalog",
        "publisher_id": "test.publisher",
        "sequence": 2,
        "entries": [entry_document],
        "signing_key_id": "test-plugin-key",
    }
    catalog_path = tmp_path / f"northwind-update-{plugin_version}.json"
    catalog_path.write_bytes(
        canonical_bytes(
            {
                **unsigned_catalog,
                "signature": base64.b64encode(
                    private_key.sign(canonical_bytes(unsigned_catalog))
                ).decode("ascii"),
            }
        )
    )
    catalog = verify_plugin_catalog(catalog_path, key_ring)
    snapshots = tuple(
        project_declared_capabilities(requirement).snapshot
        for requirement in (
            verified.manifest.required_apps + verified.manifest.optional_apps
        )
    )
    resolution = resolve_dependencies(verified.manifest, snapshots)
    registry = installed["plugin_registry"]
    installer = installed["installer"]
    review = installer.review_from_catalog(
        catalog,
        catalog.entries[0],
        verified,
        resolution,
        expected_registry_revision=registry.snapshot().revision,
    )
    return catalog, verified, review


def install_signed_northwind_plugins(
    tmp_path: Path,
    *,
    peers: tuple[str, ...] = ("calendar", "tracker", "ledger"),
    extra_plugin_ids: tuple[tuple[str, str], ...] = (),
    tracker_authentication_kind: str = "none",
    tracker_remote_origin: str = TRACKER_ORIGIN,
    tracker_scopes: tuple[str, ...] = ("issues.read", "issues.write"),
    optional_tracker_plugin_ids: tuple[str, ...] = (),
    composite_plugin_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Install signed generic MCP-backed plugins through PluginInstaller."""

    plugin_private, plugin_ring = ephemeral_publisher_ring()
    mcp_private, mcp_ring = ephemeral_ring()
    mcp_root = tmp_path / "mcp"
    plugin_root = tmp_path / "plugins"
    package_registry = PackageRegistry(
        state_root=mcp_root,
        verifier=lambda path: verify_package(
            path,
            mcp_ring,
            omega_version=_OMEGA_VERSION,
            platform_tag=host_tag(),
        ),
    )
    for peer in peers:
        package_registry.install(_write_mcp_package(tmp_path / "mcp-packages", peer, mcp_private))
    app_registry = McpAppRegistry(state_root=mcp_root)
    plugin_registry = PluginRegistry(
        state_root=plugin_root,
        verifier=lambda path: verify_plugin_package(path, plugin_ring, _OMEGA_VERSION),
    )
    installer = PluginInstaller(
        registry=plugin_registry,
        provisioner=McpAppProvisioner(
            app_registry=app_registry,
            package_registry=package_registry,
            source_resolver=_SignedAppResolver(package_registry),
        ),
        confirmation_verifier=_DigestConfirmation(),
    )
    wanted = [(CALENDAR_PLUGIN, "calendar"), (TRACKER_PLUGIN, "tracker"), (LEDGER_PLUGIN, "ledger")]
    wanted.extend(
        (plugin_id, peer)
        for peer, (plugin_id, _app_id, _kind, _capability_id) in ZERO_TOOL_PEERS.items()
        if peer in peers
    )
    wanted = [item for item in wanted if item[1] in peers]
    wanted.extend(extra_plugin_ids)
    catalog_entries = []
    verified_by_id: dict[str, Any] = {}
    for plugin_id, peer in wanted:
        package = _write_plugin_package(
            tmp_path / "plugin-packages",
            plugin_id=plugin_id,
            peer=peer,
            private_key=plugin_private,
            tracker_authentication_kind=tracker_authentication_kind,
            tracker_remote_origin=tracker_remote_origin,
            tracker_scopes=tracker_scopes,
            include_optional_tracker=plugin_id in optional_tracker_plugin_ids,
            include_required_tracker=plugin_id in composite_plugin_ids,
            plugin_kind=("composite" if plugin_id in composite_plugin_ids else "mcp_backed"),
        )
        verified = verify_plugin_package(package, plugin_ring, _OMEGA_VERSION)
        verified_by_id[plugin_id] = verified
        catalog_entries.append(
            marketplace_entry_document(
                plugin_id=plugin_id,
                publisher_id="test.publisher",
                plugin_version="1.0.0",
                manifest_sha256=verified.manifest_sha256,
                package_sha256=verified.package_sha256,
                package_uri=f"https://plugins.northwind.test/{plugin_id}/1.0.0",
            )
        )
    unsigned_catalog: dict[str, object] = {
        "schema_version": 2,
        "catalog_id": "northwind.catalog",
        "publisher_id": "test.publisher",
        "sequence": 1,
        "entries": catalog_entries,
        "signing_key_id": "test-plugin-key",
    }
    catalog_path = tmp_path / "northwind-catalog.json"
    catalog_path.write_bytes(
        canonical_bytes(
            {
                **unsigned_catalog,
                "signature": base64.b64encode(
                    plugin_private.sign(canonical_bytes(unsigned_catalog))
                ).decode("ascii"),
            }
        )
    )
    catalog = verify_plugin_catalog(catalog_path, plugin_ring)
    for entry, (plugin_id, _peer) in zip(catalog.entries, wanted, strict=True):
        verified = verified_by_id[plugin_id]
        snapshots = tuple(
            project_declared_capabilities(requirement).snapshot
            for requirement in (verified.manifest.required_apps + verified.manifest.optional_apps)
        )
        resolution = resolve_dependencies(verified.manifest, snapshots)
        review = installer.review_from_catalog(
            catalog,
            entry,
            verified,
            resolution,
            expected_registry_revision=plugin_registry.snapshot().revision,
        )
        installer.install_from_catalog(catalog, entry, verified, review.sha256)
        plugin_registry.set_enabled(
            plugin_id,
            True,
            expected_revision=plugin_registry.snapshot().revision,
        )
    return {
        "plugin_registry": plugin_registry,
        "app_registry": app_registry,
        "package_registry": package_registry,
        "installer": installer,
        "plugin_private_key": plugin_private,
        "plugin_key_ring": plugin_ring,
    }
