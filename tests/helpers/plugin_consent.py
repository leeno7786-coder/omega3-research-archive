"""Explicit plugin consent for tests.

``PluginConnectionService.start`` used to grant every offered provider scope and
every declared tool capability when a caller named none. A connection now starts
only under an explicit selection (the operator's per-app consent). For tests whose
subject is not consent itself, these helpers state that former full grant
explicitly, so the test still exercises what it was written for.
"""

from __future__ import annotations

from typing import Any, Mapping

from mcp_platform.client_contracts import McpClientDeployment
from plugin_platform.contracts import PluginAuthenticationKind


def full_connection_grant(service: Any, plugin_id: str, app_id: str) -> dict[str, Any]:
    """``PluginConnectionService.start`` keywords granting everything the app offers.

    A local or unauthenticated remote app has no OAuth, so its grant carries no
    provider scopes -- only its declared tools, as ``start`` requires.
    """
    descriptor = service._descriptor(plugin_id, app_id)
    requirement = service._mutation_definition(
        plugin_id, app_id, expected_registry_revision=None
    )[2]
    without_oauth = descriptor.deployment is McpClientDeployment.LOCAL or (
        descriptor.deployment is McpClientDeployment.REMOTE
        and requirement.authentication.kind is PluginAuthenticationKind.NONE
    )
    declared = {
        capability: digest
        for capability, digest in descriptor.declared_capabilities.items()
        if capability.startswith("tool:") and type(digest) is str
    }
    ceiling = tuple(sorted(declared))
    return {
        "scopes": ()
        if without_oauth
        else tuple(service._offered_scopes_provider(plugin_id, descriptor.app_id)),
        "capability_ceiling": ceiling,
        "allowed_tool_schemas": {
            capability.removeprefix("tool:"): declared[capability] for capability in ceiling
        },
    }


def full_permission_settings(selection: Mapping[str, Any]) -> dict[str, Any]:
    """A ``set_permissions`` / ``start_connection`` settings body accepting the full offer.

    ``selection`` is the ``PluginLifecycleService.permissions`` payload; the policy and
    constraints are the ones it already shows as selected, as the HUD form pre-fills them.
    """
    offered = selection["offered"]
    selected = selection["selected"]
    return {
        "provider_scopes": list(offered["provider_scopes"]),
        "capability_ids": list(offered["capability_ids"]),
        "execution_policy": selected["execution_policy"],
        "source_constraints": dict(selected["source_constraints"]),
        "destination_constraints": dict(selected["destination_constraints"]),
    }
