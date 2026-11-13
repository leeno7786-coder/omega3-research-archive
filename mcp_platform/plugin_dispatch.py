"""Omega-facing catalog and execution bridge for installed MCP plugins.

The signed plugin runtime is the only execution authority.  This module adds
the thin adapter Omega's model/tool rails need: stable namespaced tool names,
an installed-only catalog, and one synchronous invocation shape for AUTO.
Provider names never appear in the dispatcher; the plugin/app/capability tuple
is compiled from durable ownership and revalidated by ``PluginRuntime`` before
any transport work starts.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from mcp_platform.canonical import canonical_bytes
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_catalog import (
    PluginRuntimeCatalogSnapshot,
    PluginToolBinding,
    compile_plugin_runtime_catalog,
    compile_plugin_tool_bindings,
    plugin_tool_registry_entries,
)
from mcp_platform.plugin_runtime import PluginRuntimeError

PLUGIN_TOOL_PREFIX = "plugin__"


class PluginAccountUnconfigured(ValueError):
    """No operator account partition is connected, so no binding can exist.

    This is a settled fact about the connection, not a catalog or registry
    failure: callers asking "is there a candidate?" get an honest no.
    """


def canonical_plugin_tool_name(plugin_id: str, capability: str) -> str:
    """Return the collision-resistant name used on Omega's tool rail."""

    if (
        type(plugin_id) is not str
        or not plugin_id
        or plugin_id.strip() != plugin_id
        or type(capability) is not str
        or not capability
        or capability.strip() != capability
        or any(ord(char) < 32 or ord(char) == 127 for char in plugin_id + capability)
    ):
        raise ValueError("plugin_tool_name_invalid")
    return f"{PLUGIN_TOOL_PREFIX}{plugin_id}__{capability}"


def is_plugin_tool_name(tool_name: object) -> bool:
    return type(tool_name) is str and tool_name.startswith(PLUGIN_TOOL_PREFIX)


def _default_service() -> Any:
    from mcp_platform.plugin_lifecycle import get_default_plugin_lifecycle_service

    return get_default_plugin_lifecycle_service()


def plugin_tool_bindings(*, service: Any | None = None) -> tuple[PluginToolBinding, ...]:
    """Compile planner bindings from durable ownership and signed descriptors only."""
    return plugin_runtime_catalog(service=service).tools


def plugin_runtime_catalog(
    *,
    service: Any | None = None,
) -> PluginRuntimeCatalogSnapshot:
    """Compile every current signed surface without provider or inference work."""

    service = _default_service() if service is None else service
    try:
        account_key = service.operator_account_key()
    except ValueError:
        account_key = ""
    if type(account_key) is not str:
        raise PluginAccountUnconfigured("plugin_account_partition_invalid")
    app_authority_provider = getattr(
        service,
        "planning_app_authority",
        None,
    )
    verified_provider = getattr(service.plugin_registry, "reverify_installed", None)
    return compile_plugin_runtime_catalog(
        plugin_snapshot=service.plugin_registry.snapshot(),
        app_snapshot=service.app_registry.snapshot(),
        descriptor_for_app=service.descriptor_for_plugin_app,
        account_key_hash=sha256(account_key.encode("utf-8")).hexdigest(),
        app_authority_provider=(app_authority_provider if callable(app_authority_provider) else None),
        verified_plugin_provider=(verified_provider if callable(verified_provider) else None),
    )


def resolve_plugin_tool_request(
    text: str,
    *,
    service: Any | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Resolve a unique signed read tool from generic retrieval, or return None.

    This is a thin fast path over ``retrieve_plugin_candidates``.  It never
    invokes a provider, never owns a capability map, and never emits
    provider query grammar.  Write capabilities stay out so they can only
    enter through AUTO's permission-bound plan executor.
    """

    from mcp_platform.plugin_planning import (
        deterministic_plugin_arguments,
        retrieve_plugin_candidates,
    )

    clean = " ".join(str(text or "").split())
    if not clean:
        return None
    bindings = plugin_tool_bindings(service=service)
    if not bindings:
        return None
    reads = tuple(
        item
        for item in retrieve_plugin_candidates(clean, bindings)
        if item.risk_class is RiskClass.READ_ONLY
    )
    if len(reads) != 1:
        return None
    arguments = deterministic_plugin_arguments(clean, reads[0])
    if arguments is None:
        return None
    return reads[0].tool_name, arguments


def plugin_operation_digest(
    *, plugin_id: str, app_id: str, capability: str, arguments: Mapping[str, Any]
) -> str:
    return sha256(
        canonical_bytes(
            {
                "plugin_id": plugin_id,
                "app_id": app_id,
                "capability": capability,
                "arguments": dict(arguments),
            }
        )
    ).hexdigest()


def invoke_plugin_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
    """Retired namespaced invoker. AUTO must use ``plan_and_execute_plugin_request``."""

    raise PluginRuntimeError("plugin_tool_requires_plugin_operation")


__all__ = [
    "PLUGIN_TOOL_PREFIX",
    "PluginToolBinding",
    "canonical_plugin_tool_name",
    "compile_plugin_runtime_catalog",
    "compile_plugin_tool_bindings",
    "is_plugin_tool_name",
    "plugin_operation_digest",
    "plugin_runtime_catalog",
    "plugin_tool_bindings",
    "plugin_tool_registry_entries",
    "resolve_plugin_tool_request",
]
