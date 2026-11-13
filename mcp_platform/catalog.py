"""Signed MCP catalog projection and runtime drift reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import (
    ResourceCatalog,
    ResourceDefinition,
    ResourceTemplateDefinition,
    ToolCatalog,
    ToolDefinition,
)
from mcp_platform.package_verifier import VerifiedPackage

from .capabilities import reconcile_live_capabilities


class McpCatalogDrift(Exception):
    """A stable, content-free runtime catalog rejection."""


@dataclass(frozen=True)
class VerifiedCatalogProjection:
    """Only identity and authority-bearing fields from signed package material."""

    server_id: str
    server_version: str
    publisher_id: str
    package_manifest_sha256: str
    tools: tuple[ToolDefinition, ...]
    resources: tuple[ResourceDefinition, ...]
    resource_templates: tuple[ResourceTemplateDefinition, ...]

    @classmethod
    def from_signed_catalogs(
        cls,
        *,
        package_manifest_sha256: str,
        publisher_id: str,
        tool_catalog: ToolCatalog,
        resource_catalog: ResourceCatalog,
    ) -> VerifiedCatalogProjection:
        if (tool_catalog.server_id, tool_catalog.server_version) != (
            resource_catalog.server_id,
            resource_catalog.server_version,
        ):
            raise McpCatalogDrift("signed_catalog_identity_mismatch")
        return cls(
            server_id=tool_catalog.server_id,
            server_version=tool_catalog.server_version,
            publisher_id=publisher_id,
            package_manifest_sha256=package_manifest_sha256,
            tools=tool_catalog.tools,
            resources=resource_catalog.resources,
            resource_templates=resource_catalog.resource_templates,
        )

    @classmethod
    def from_verified_package(cls, package: VerifiedPackage) -> VerifiedCatalogProjection:
        return cls.from_signed_catalogs(
            package_manifest_sha256=package.manifest_sha256,
            publisher_id=package.manifest.publisher_id,
            tool_catalog=package.tool_catalog,
            resource_catalog=package.resource_catalog,
        )

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "server_id": self.server_id,
            "server_version": self.server_version,
            "publisher_id": self.publisher_id,
            "package_manifest_sha256": self.package_manifest_sha256,
            "tools": [
                {key: value for key, value in tool.to_dict().items() if key != "title"}
                for tool in self.tools
            ],
            "resources": [resource.to_dict() for resource in self.resources],
            "resource_templates": [template.to_dict() for template in self.resource_templates],
        }


def _unique_by(items: list[Any], attribute: str, reason: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for item in items:
        identity = str(getattr(item, attribute))
        if identity in indexed:
            raise McpCatalogDrift(reason)
        indexed[identity] = item
    return indexed


def reconcile_runtime_catalog(
    signed: VerifiedCatalogProjection,
    runtime_tools: list[Any],
    runtime_resources: list[Any],
    runtime_templates: list[Any],
) -> VerifiedCatalogProjection:
    """Require the complete runtime identity/schema set to equal the signed set."""
    tools = _unique_by(runtime_tools, "name", "tool_catalog_drift")
    signed_tools = {tool.name: tool for tool in signed.tools}
    if set(tools) != set(signed_tools):
        raise McpCatalogDrift("tool_catalog_drift")
    for name, definition in signed_tools.items():
        runtime = tools[name]
        try:
            input_digest = sha256_hex(canonical_bytes(runtime.input_schema))
            output_digest = sha256_hex(canonical_bytes(runtime.output_schema))
        except (TypeError, ValueError):
            raise McpCatalogDrift("tool_catalog_drift") from None
        if (
            input_digest != definition.input_schema_sha256
            or output_digest != definition.output_schema_sha256
        ):
            raise McpCatalogDrift("tool_catalog_drift")

    resources = _unique_by(runtime_resources, "uri", "resource_catalog_drift")
    signed_resources = {resource.uri: resource for resource in signed.resources}
    if set(resources) != set(signed_resources):
        raise McpCatalogDrift("resource_catalog_drift")
    for uri, definition in signed_resources.items():
        if getattr(resources[uri], "mime_type", None) != definition.mime_type:
            raise McpCatalogDrift("resource_catalog_drift")

    templates = _unique_by(runtime_templates, "uri_template", "resource_template_catalog_drift")
    signed_templates = {template.uri_template: template for template in signed.resource_templates}
    if set(templates) != set(signed_templates):
        raise McpCatalogDrift("resource_template_catalog_drift")
    for uri_template, definition in signed_templates.items():
        if getattr(templates[uri_template], "mime_type", None) != definition.mime_type:
            raise McpCatalogDrift("resource_template_catalog_drift")

    return signed


__all__ = [
    "McpCatalogDrift",
    "VerifiedCatalogProjection",
    "reconcile_live_capabilities",
    "reconcile_runtime_catalog",
]
