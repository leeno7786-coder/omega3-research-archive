"""AUTO's provider-neutral plugin operation bridge.

The execution phase receives an explicit signed plugin/app/capability request
and delegates it to :class:`mcp_platform.plugin_runtime.PluginRuntime`.  There
is deliberately no provider, workspace, or connector dispatch in this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any

from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_runtime import (
    PluginOperationRequest,
    PluginRetryAfterFence,
    PluginRuntime,
    PluginRuntimeError,
    plugin_retry_context,
)
from mcp_platform.plugin_authority import plugin_plan_digest
from mcp_platform.plugin_planning import plain_arguments


def _runtime_from_client(runtime_client: Any) -> PluginRuntime:
    if type(runtime_client) is PluginRuntime:
        return runtime_client
    runtime = getattr(runtime_client, "plugin_runtime", None)
    if type(runtime) is PluginRuntime:
        return runtime
    if runtime_client is not None:
        try:
            from runtime.client import RuntimeClient
        except Exception:
            RuntimeClient = None
        if RuntimeClient is not None and type(runtime_client) is RuntimeClient:
            from mcp_platform.plugin_runtime import get_default_plugin_runtime

            return get_default_plugin_runtime()
    raise PluginRuntimeError("plugin_runtime_unavailable")


def _signed_binding_for(operation: Mapping[str, Any], runtime_client: Any) -> Any:
    plugin_id = operation.get("plugin_id", "")
    app_id = operation.get("app_id", "")
    capability = operation.get("capability", "")
    loader = getattr(runtime_client, "plugin_tool_bindings", None)
    if callable(loader):
        bindings = loader()
    else:
        from mcp_platform.plugin_dispatch import plugin_tool_bindings

        bindings = plugin_tool_bindings()
    match = next(
        (
            item
            for item in bindings
            if getattr(item, "plugin_id", None) == plugin_id
            and getattr(item, "app_id", None) == app_id
            and getattr(item, "capability", None) == capability
        ),
        None,
    )
    if match is None:
        raise PluginRuntimeError("plugin_tool_unavailable")
    return match


def _execution_scope(runtime_client: Any) -> object:
    factory = getattr(runtime_client, "plugin_execution_mutation_scope", None)
    return nullcontext() if not callable(factory) else factory()


def run_plugin_operation(
    operation: Mapping[str, Any],
    *,
    runtime_client: Any,
    permission_mode: str | None = None,
) -> dict[str, Any]:
    retry_key = None
    for attempt in range(2):
        try:
            with _execution_scope(runtime_client), plugin_retry_context(retry_key):
                return _run_plugin_operation_unfenced(
                    operation,
                    runtime_client=runtime_client,
                    permission_mode=permission_mode,
                )
        except PluginRetryAfterFence as deferred:
            if attempt == 0 and deferred.outcome is not None and deferred.outcome.status == "applied":
                runtime = _runtime_from_client(runtime_client)
                prepare_retry = getattr(getattr(runtime, "_backend", None), "prepare_plugin_retry", None)
                if callable(prepare_retry):
                    prepare_retry(deferred.request_key, deferred.recovery_status)
                retry_key = deferred.request_key
                continue
            raise deferred.final_error()


def _run_plugin_operation_unfenced(
    operation: Mapping[str, Any],
    *,
    runtime_client: Any,
    permission_mode: str | None = None,
) -> dict[str, Any]:
    """Execute one explicit plugin operation and return a stable AUTO result."""

    if not isinstance(operation, Mapping):
        raise PluginRuntimeError("plugin_operation_invalid")
    arguments = operation.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise PluginRuntimeError("plugin_arguments_invalid")
    plugin_id = operation.get("plugin_id", "")
    app_id = operation.get("app_id", "")
    capability = operation.get("capability", "")
    runtime = _runtime_from_client(runtime_client)
    binding = _signed_binding_for(operation, runtime_client)
    # Plugin settings/authority own this boundary. ``permission_mode`` remains
    # accepted for compatibility with older callers but is intentionally not a
    # second chat-side plugin gate.
    if getattr(binding, "risk_class", None) is not RiskClass.READ_ONLY:
        # This compatibility helper has no authority/grant input. A digest of
        # the request is not operator consent and must never authorize a write.
        raise PluginRuntimeError("plugin_authority_required")
    request = PluginOperationRequest(
        plugin_id=plugin_id,
        app_id=app_id,
        capability=capability,
        arguments=dict(arguments),
        authority_revision=operation.get("authority_revision"),
        candidate_id=getattr(binding, "candidate_id", None),
        input_schema_sha256=getattr(binding, "input_schema_sha256", None),
        account_key_hash=getattr(binding, "account_key_hash", None),
        package_revision=getattr(binding, "package_revision", None),
        connection_revision=getattr(binding, "connection_revision", None),
        catalog_authority_revision=getattr(binding, "authority_revision", None),
        capability_revision=getattr(binding, "capability_revision", None),
        binding_revision=getattr(binding, "binding_revision", None),
    )
    result = runtime.invoke_tool(request)
    payload = result.to_dict()
    return {
        "status": "success",
        "plugin_result": payload,
        "receipt_summary": (f"{payload['plugin_id']}:{payload['capability']} completed"),
    }


def run_plugin_plan_step(
    binding: Any,
    plan: Any,
    step: Any,
    *,
    runtime_client: Any,
    connection: Any = None,
    authority: Any = None,
    tool: Any = None,
    proposal: Any = None,
    authority_service: Any = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    """Hand one already-authorized plan step to the provider-neutral runtime."""
    if getattr(binding, "risk_class", RiskClass.READ_ONLY) is not RiskClass.READ_ONLY and authority is None:
        raise PluginRuntimeError("plugin_authority_required")
    request = PluginOperationRequest(
        plugin_id=binding.plugin_id,
        app_id=binding.app_id,
        capability=binding.capability,
        arguments=plain_arguments(step.arguments),
        authority_revision=binding.connection_revision,
        episode_id=plan.episode_id,
        plan_id=plan.plan_id,
        plan_digest=plugin_plan_digest(plan),
        step_id=step.step_id,
        candidate_id=step.candidate_id,
        input_schema_sha256=binding.input_schema_sha256,
        connection=connection,
        authority=authority,
        tool=tool,
        proposal=proposal,
        authority_service=authority_service,
        action_id=action_id,
    )
    return _runtime_from_client(runtime_client).invoke_tool(request).to_dict()


__all__ = ["run_plugin_operation", "run_plugin_plan_step"]
