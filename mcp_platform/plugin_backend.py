"""Production backend that binds plugin operations to current MCP clients."""

from __future__ import annotations

import logging

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from dataclasses import replace
from typing import Any
from uuid import uuid4

from mcp_platform.client_contracts import (
    McpConnectionDescriptor,
    McpClientError,
    McpClientErrorReason,
    McpOperationKind,
    McpOperationRequest,
    McpOperationStatus,
)
from mcp_platform.client import McpProviderDriftStatus, McpProviderRecovery
from mcp_platform.canonical import sha256_hex
from mcp_platform.plugin_connections import (
    PluginConnectionService,
    PluginExecutionWorkerPreparation,
)
from mcp_platform.plugin_catalog import (
    PluginPromptBinding,
    PluginResourceBinding,
    PluginResourceTemplateBinding,
    PluginSkillBinding,
    PluginToolBinding,
)
from mcp_platform.plugin_execution_worker import (
    PluginExecutionWorkerError,
)
from mcp_platform.plugin_runtime import (
    PluginRetryKey,
    PluginRetryAfterFence,
    PluginRuntimeError,
    PluginRuntimeOperation,
    current_plugin_retry_key,
)
from mcp_platform.supervisor import McpSupervisorError
from plugin_platform.contracts import AssetKind, PluginManifest
from plugin_platform.package_verifier import VerifiedPlugin

# One callable shape: the backend hands the host's single transport loop a
# zero-argument coroutine factory plus a timeout and receives the result.
# Production wires the supervisor's ``submit_plugin_operation_sync``; tests
# inject a direct runner.  The backend never owns a loop of its own.
PluginOperationSubmitter = Callable[..., Any]

_REMOTE_BINDING_TYPES = (
    PluginToolBinding,
    PluginResourceBinding,
    PluginResourceTemplateBinding,
    PluginPromptBinding,
)
_OPERATION_KINDS = {
    ("tool", PluginRuntimeOperation.TOOL_CALL): (
        McpOperationKind.TOOL_CALL,
        lambda capability: f"tool:{capability}",
    ),
    ("prompt", PluginRuntimeOperation.PROMPTS_LIST): (
        McpOperationKind.PROMPTS_LIST,
        lambda _capability: "prompts/list",
    ),
    ("prompt", PluginRuntimeOperation.PROMPT_GET): (
        McpOperationKind.PROMPT_GET,
        lambda capability: f"prompt:{capability}",
    ),
    ("resource", PluginRuntimeOperation.RESOURCES_LIST): (
        McpOperationKind.RESOURCES_LIST,
        lambda _capability: "resources/list",
    ),
    ("resource", PluginRuntimeOperation.RESOURCE_READ): (
        McpOperationKind.RESOURCE_READ,
        lambda capability: f"resource:{capability}",
    ),
    ("resource_template", PluginRuntimeOperation.RESOURCE_TEMPLATES_LIST): (
        McpOperationKind.RESOURCE_TEMPLATES_LIST,
        lambda _capability: "resources/templates/list",
    ),
    ("resource_template", PluginRuntimeOperation.RESOURCE_READ): (
        McpOperationKind.RESOURCE_READ,
        lambda capability: f"resource:{capability}",
    ),
}


_LOG = logging.getLogger("omega.mcp.plugin_backend")

_KNOWN_FAILURE_FIELDS = frozenset(
    {"error", "reason", "failure_reason", "message", "detail", "code"}
)


def _closed_value(value: object, allowed: frozenset[str]) -> str:
    """The value when it is a member of one of Omega's own closed sets.

    A worker result that failed shape validation is, by definition, not a
    trusted source. Its status and error are still worth reporting when they
    are Omega's own literals -- that is what turns `plugin_operation_failed`
    into an answer -- and anything else is described by its SHAPE rather than
    printed, so provider text never reaches the runtime log.
    """
    if value is None:
        return "absent"
    if type(value) is str:
        return value if value in allowed else "outside the closed set"
    if isinstance(value, Mapping):
        # A structured error: name only the keys Omega itself defines, and
        # follow them one level looking for a closed reason.
        named = sorted(set(value) & _KNOWN_FAILURE_FIELDS)
        for key in named:
            inner = value[key]
            if type(inner) is str and inner in allowed:
                return f"{{{key}: {inner}}}"
        return f"mapping keys={named or 'none'} (+{len(set(value) - _KNOWN_FAILURE_FIELDS)} unnamed)"
    return f"{type(value).__name__}"


def _operation_statuses() -> frozenset[str]:
    from mcp_platform.client_contracts import McpOperationStatus

    return frozenset(item.value for item in McpOperationStatus)


def _client_error_reasons() -> frozenset[str]:
    from mcp_platform.client_contracts import McpClientErrorReason

    return frozenset(item.value for item in McpClientErrorReason)


def _plugin_failure_reason(result: Mapping[str, Any]) -> str | None:
    """Translate one closed MCP error into the plugin runtime vocabulary."""
    error = result.get("error")
    if type(error) is not dict or set(error) != {"reason"}:
        return None
    reason = error["reason"]
    if type(reason) is not str or reason not in _client_error_reasons():
        return None
    return {
        McpClientErrorReason.AUTHORIZATION_REQUIRED.value: "plugin_authorization_required",
        McpClientErrorReason.TRANSPORT_UNAVAILABLE.value: "plugin_connection_unavailable",
        McpClientErrorReason.REQUEST_TIMEOUT.value: "plugin_operation_timeout",
    }.get(reason, reason if reason.startswith("plugin_") else f"plugin_{reason}")


def _runtime_failure_reason(reason: McpClientErrorReason) -> str:
    return {
        McpClientErrorReason.AUTHORIZATION_REQUIRED: "plugin_authorization_required",
        McpClientErrorReason.TRANSPORT_UNAVAILABLE: "plugin_connection_unavailable",
        McpClientErrorReason.REQUEST_TIMEOUT: "plugin_operation_timeout",
    }.get(reason, f"plugin_{reason.value}")


def _worker_result_identity_matches(
    result: Mapping[str, Any],
    *,
    request: McpOperationRequest,
    binding: object,
) -> bool:
    return (
        result.get("request_id") == request.request_id
        and result.get("app_id") == binding.app_id
        and result.get("capability_id") == request.capability_id
        and result.get("capability_revision") == binding.capability_revision
    )



def _emit_plugin_failure(
    binding: object | None,
    operation: PluginRuntimeOperation,
    error: PluginRuntimeError,
    *,
    plugin_id: str = "",
    app_id: str = "",
    capability: str = "",
) -> None:
    """Persist the final backend failure with the closed cause intact."""
    try:
        from guardian.terminal_feedback import emit_data

        reason = str(error or "").strip()
        if not reason.startswith("plugin_"):
            reason = "plugin_operation_failed"
        provider_status = getattr(error, "provider_status", None)
        provider_reason = getattr(getattr(provider_status, "reason", None), "value", None)
        plugin_id = plugin_id or str(getattr(binding, "plugin_id", "") or "")
        app_id = app_id or str(getattr(binding, "app_id", "") or "")
        capability = capability or str(getattr(binding, "capability", "") or "")
        emit_data(
            "auto",
            "plugin_backend",
            status="fail",
            ok=False,
            summary=reason,
            reason=reason,
            provider_reason=provider_reason,
            operation=operation.value,
            plugin_id=plugin_id,
            app_id=app_id,
            capability=capability,
        )
    except Exception:
        pass


class CurrentMcpPluginBackend:
    """Resolve an installed plugin app and invoke its signed current MCP client."""

    def __init__(
        self,
        *,
        connection_service: PluginConnectionService,
        descriptor_provider: Callable[[str, str], McpConnectionDescriptor],
        plugin_id_for_app: Mapping[str, str] | Callable[[str], str],
        account_key_provider: Callable[[], str],
        operation_submitter: PluginOperationSubmitter,
        operation_timeout_seconds: float = 120.0,
        execution_worker: object | None = None,
        verified_plugin_provider: Callable[[str], VerifiedPlugin] | None = None,
        provider_update_lookup: Callable[[McpProviderDriftStatus], McpProviderRecovery] | None = None,
    ) -> None:
        if (
            not callable(descriptor_provider)
            or not callable(account_key_provider)
            or not callable(operation_submitter)
        ):
            raise ValueError("plugin_backend_provider_invalid")
        if not isinstance(plugin_id_for_app, Mapping) and not callable(plugin_id_for_app):
            raise ValueError("plugin_backend_mapping_invalid")
        if (
            type(operation_timeout_seconds) not in {int, float}
            or not asyncio.iscoroutinefunction(
                getattr(connection_service, "prepare_execution_worker", None)
            )
            or operation_timeout_seconds <= 0
        ):
            raise ValueError("plugin_backend_limits_invalid")
        self._connection_service = connection_service
        self._descriptor_provider = descriptor_provider
        self._plugin_id_for_app = plugin_id_for_app
        self._account_key_provider = account_key_provider
        self._operation_submitter = operation_submitter
        self._operation_timeout_seconds = float(operation_timeout_seconds)
        if verified_plugin_provider is not None and not callable(verified_plugin_provider):
            raise ValueError("plugin_backend_provider_invalid")
        self._verified_plugin_provider = verified_plugin_provider
        if provider_update_lookup is not None and not callable(provider_update_lookup):
            raise ValueError("plugin_backend_provider_invalid")
        self._provider_update_lookup = provider_update_lookup
        # The backend never builds its own worker.  A privately constructed
        # worker would execute provider operations behind a second, unfenced
        # lifecycle authority whose failed reaps are invisible to the shared
        # worker's global admission and close accounting.
        if execution_worker is None or not all(
            callable(getattr(execution_worker, method, None))
            for method in ("run_sync", "close")
        ):
            raise ValueError("plugin_backend_worker_invalid")
        self._execution_worker = execution_worker
        self._closed = False
        self._drift_status: McpProviderDriftStatus | None = None
        self._deferred_retry_candidate: tuple[str, str] | None = None
        # Retry leases are owned by the originating action, never by the
        # backend instance.  This prevents another concurrent operation from
        # consuming the one lookup budget while the original fence is being
        # reacquired.
        self._plugin_retry_states: dict[
            PluginRetryKey, McpProviderDriftStatus | None
        ] = {}
        self._plugin_retry_lock = threading.Lock()

    @property
    def drift_status(self) -> McpProviderDriftStatus | None:
        """The last provider failure available to the action/status caller."""
        return self._drift_status

    def _record_provider_failure(
        self,
        reason: McpClientErrorReason,
        *,
        phase: str,
        plugin_id: str | None = None,
    ) -> None:
        prior = self._drift_status
        self._drift_status = McpProviderDriftStatus(
            phase=phase,
            reason=reason,
            plugin_id=(
                plugin_id
                if plugin_id is not None
                else (prior.plugin_id if prior is not None else None)
            ),
            update_lookup_attempted=(
                prior.update_lookup_attempted if prior is not None else False
            ),
            retry_attempted=(prior.retry_attempted if prior is not None else False),
            reconnect_required=reason
            in {
                McpClientErrorReason.AUTHORIZATION_REQUIRED,
                McpClientErrorReason.AUTHORIZATION_INVALID,
            }
            or (prior.reconnect_required if prior is not None else False),
            approval_required=(prior.approval_required if prior is not None else False),
            approval_diff=(prior.approval_diff if prior is not None else ()),
        )

    def _record_result_failure(
        self,
        result: Mapping[str, Any],
        *,
        phase: str,
        plugin_id: str | None = None,
    ) -> None:
        error = result.get("error")
        if not isinstance(error, Mapping) or type(error.get("reason")) is not str:
            return
        try:
            reason = McpClientErrorReason(error["reason"])
        except ValueError:
            return
        self._record_provider_failure(reason, phase=phase, plugin_id=plugin_id)

    def _should_recover(self) -> bool:
        status = self._drift_status
        return status is not None and status.reason in {
            McpClientErrorReason.CAPABILITY_DRIFT,
            McpClientErrorReason.IDENTITY_MISMATCH,
            McpClientErrorReason.PROTOCOL_MISMATCH,
            McpClientErrorReason.TRANSPORT_UNAVAILABLE,
        }

    def _lookup_update_once(self) -> bool:
        status = self._drift_status
        lookup = self._provider_update_lookup
        if status is None or lookup is None or status.update_lookup_attempted:
            return False
        try:
            recovery = lookup(status)
        except Exception:
            recovery = McpProviderRecovery(status="reconnect_required")
        if type(recovery) is not McpProviderRecovery:
            recovery = McpProviderRecovery(status="reconnect_required")
        updated = recovery.applied
        self._drift_status = replace(
            status,
            update_lookup_attempted=True,
            retry_attempted=updated,
            # Approval is a distinct terminal action outcome.  It must not
            # be reported as reconnect-required because that would prompt
            # OAuth again instead of asking the user to approve the signed
            # authority change.
            reconnect_required=recovery.status == "reconnect_required",
            approval_required=recovery.status == "approval_required",
            approval_diff=recovery.approval_diff,
        )
        self._deferred_retry_candidate = recovery.retry_candidate
        self._deferred_retry_generation = recovery.generation
        return updated

    def _register_deferred_retry(
        self, error: PluginRuntimeError, request_key: PluginRetryKey
    ) -> PluginRetryAfterFence | None:
        candidate = self._deferred_retry_candidate
        if candidate is None:
            return None
        retry = PluginRetryAfterFence(
            error,
            request_key,
            self._drift_status,
            self._deferred_retry_generation,
        )
        lookup = self._provider_update_lookup
        owner = getattr(lookup, "__self__", None)
        register = getattr(owner, "_register_deferred_update_retry", None)
        if not callable(register):
            return None
        try:
            register(candidate[0], candidate[1], retry)
        except (OSError, TypeError, ValueError):
            return None
        return retry

    def prepare_plugin_retry(
        self,
        request_key: PluginRetryKey | None,
        recovery_status: object | None = None,
    ) -> None:
        """Arm the originating action's one post-update retry budget."""

        if request_key is None:
            return
        if (
            type(request_key) is not tuple
            or len(request_key) != 4
            or any(type(item) is not str or not item for item in request_key)
        ):
            raise ValueError("plugin_retry_key_invalid")
        if not isinstance(recovery_status, McpProviderDriftStatus):
            return
        status = replace(
            recovery_status,
            update_lookup_attempted=True,
            retry_attempted=True,
            reconnect_required=False,
            approval_required=False,
            approval_diff=(),
        )
        with self._plugin_retry_lock:
            self._plugin_retry_states[request_key] = status

    def _operation_retry_key(
        self, plugin_id: str, app_id: str, capability: str
    ) -> PluginRetryKey:
        current = current_plugin_retry_key()
        if current is not None and current[1:] == (plugin_id, app_id, capability):
            return current
        return (uuid4().hex, plugin_id, app_id, capability)

    def _take_retry_state(
        self, request_key: PluginRetryKey
    ) -> McpProviderDriftStatus | None:
        with self._plugin_retry_lock:
            if request_key not in self._plugin_retry_states:
                return None
            return self._plugin_retry_states.pop(request_key)

    def _surface_provider_failure(self, error: PluginRuntimeError) -> None:
        status = self._drift_status
        if status is None:
            return
        if self._should_recover() and self._provider_update_lookup is None:
            status = replace(status, reconnect_required=True)
            self._drift_status = status
        # PluginRuntime preserves the exact exception instance.  Attaching
        # this Omega-owned typed status gives action callers a phase/reason
        # without putting provider text or credentials in the error string.
        error.provider_status = status

    def _plugin_id(self, app_id: str) -> str:
        try:
            value = (
                self._plugin_id_for_app(app_id)
                if callable(self._plugin_id_for_app)
                else self._plugin_id_for_app[app_id]
            )
        except (KeyError, TypeError, ValueError):
            raise PluginRuntimeError("plugin_not_installed") from None
        if type(value) is not str or not value or value.strip() != value:
            raise PluginRuntimeError("plugin_not_installed")
        return value

    def _submit(self, operation: Callable[[], Awaitable[Any]], *, timeout_s: float) -> Any:
        if self._closed:
            raise PluginRuntimeError("plugin_runtime_closed")
        handoff = getattr(self, "_lifecycle_mutation_handoff", None)
        submitted = handoff(operation) if callable(handoff) else operation
        try:
            return self._operation_submitter(submitted, timeout_s=timeout_s)
        except TimeoutError:
            raise PluginRuntimeError("plugin_operation_timeout") from None
        except McpSupervisorError as exc:
            if str(exc) == "supervisor_operation_timeout":
                raise PluginRuntimeError("plugin_operation_timeout") from None
            raise

    async def _prepare_operation(
        self,
        binding: object,
        operation: PluginRuntimeOperation,
        arguments: dict[str, Any],
    ) -> tuple[PluginExecutionWorkerPreparation, McpOperationRequest]:
        if (
            type(binding) not in _REMOTE_BINDING_TYPES
            or type(operation) is not PluginRuntimeOperation
            or type(arguments) is not dict
        ):
            raise PluginRuntimeError("plugin_operation_unsupported")
        specification = _OPERATION_KINDS.get((binding.capability_kind, operation))
        if specification is None:
            raise PluginRuntimeError("plugin_operation_unsupported")
        account_key = self._account_key_provider()
        if type(account_key) is not str or not account_key or account_key.strip() != account_key:
            raise PluginRuntimeError("plugin_account_unconfigured")
        try:
            preparation = await self._connection_service.prepare_execution_worker(
                binding.plugin_id,
                app_id=binding.app_id,
                account_key=account_key,
                capability_kind=binding.capability_kind,
                capability_id=binding.capability,
            )
        except McpClientError as exc:
            self._record_provider_failure(
                exc.reason,
                phase=getattr(exc, "phase", operation.value),
                plugin_id=binding.plugin_id,
            )
            if exc.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED:
                raise PluginRuntimeError("plugin_authorization_required") from None
            raise PluginRuntimeError(_runtime_failure_reason(exc.reason)) from None
        except ValueError as exc:
            if str(exc) in {
                "plugin_authorization_required",
                "oauth_reauthorization_required",
            }:
                self._record_provider_failure(
                    McpClientErrorReason.AUTHORIZATION_REQUIRED,
                    phase=operation.value,
                    plugin_id=binding.plugin_id,
                )
                raise PluginRuntimeError("plugin_authorization_required") from None
            raise PluginRuntimeError("plugin_operation_failed") from None
        if type(preparation) is not PluginExecutionWorkerPreparation:
            raise PluginRuntimeError("plugin_operation_failed")
        descriptor = self._descriptor_provider(binding.plugin_id, binding.app_id)
        authority = preparation.authority
        if (
            type(descriptor) is not McpConnectionDescriptor
            or descriptor != preparation.descriptor
            or authority.plugin_id != binding.plugin_id
            or authority.app_id != binding.app_id
            or authority.account_key_sha256 != binding.account_key_hash
            or authority.package_revision != binding.package_revision
            or authority.connection_revision != binding.connection_revision
            or authority.authority_revision != binding.authority_revision
            or authority.capability_revision != binding.capability_revision
            or authority.capability_kind != binding.capability_kind
            or authority.capability_id != binding.capability
            or descriptor.capability_revision != binding.capability_revision
        ):
            raise PluginRuntimeError("plugin_authority_invalid")
        mcp_operation, capability_id = specification
        loop = asyncio.get_running_loop()
        request = McpOperationRequest(
            request_id=uuid4().hex,
            app_id=binding.app_id,
            operation=mcp_operation,
            capability_id=capability_id(binding.capability),
            arguments=arguments,
            capability_revision=binding.capability_revision,
            deadline_monotonic=loop.time() + self._operation_timeout_seconds,
            cancellation_id=uuid4().hex,
            task_id=None,
            input_request_id=None,
            request_state=None,
        )
        return preparation, request

    @staticmethod
    def _worker_body(
        preparation: PluginExecutionWorkerPreparation,
        request: McpOperationRequest,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "descriptor": preparation.descriptor.to_dict(),
            "request": request.to_dict(),
        }
        if preparation.local_execution is not None:
            body["local_execution"] = preparation.local_execution.to_dict()
        return body

    def _execute_mcp_once(
        self,
        binding: object,
        operation: PluginRuntimeOperation,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        try:
            preparation, request = self._submit(
                lambda: self._prepare_operation(binding, operation, arguments),
                timeout_s=self._operation_timeout_seconds + 5.0,
            )
            remaining = request.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise PluginExecutionWorkerError("plugin_operation_timeout")
            result = self._execution_worker.run_sync(
                authority=preparation.authority,
                body=self._worker_body(preparation, request),
                credential=dict(preparation.credential),
                timeout_seconds=remaining,
            )
        except PluginRuntimeError:
            raise
        except PluginExecutionWorkerError as exc:
            if str(exc) == "plugin_operation_timeout":
                raise PluginRuntimeError("plugin_operation_timeout") from None
            if str(exc) == "plugin_runtime_closed":
                raise PluginRuntimeError("plugin_runtime_closed") from None
            if str(exc) in {
                "plugin_worker_reap_failed",
                "plugin_worker_reap_pending",
            }:
                raise PluginRuntimeError("plugin_worker_reap_pending") from None
            raise PluginRuntimeError("plugin_operation_failed") from None
        except Exception as exc:
            raise PluginRuntimeError("plugin_operation_failed") from exc
        if not isinstance(result, Mapping):
            raise PluginRuntimeError("plugin_operation_failed")
        common = {
            "request_id",
            "app_id",
            "capability_id",
            "capability_revision",
            "status",
        }
        if not common.issubset(result):
            # Which FIELDS, because `plugin_operation_failed` is raised from
            # nineteen places here and reads identically at every one. Only
            # Omega's own vocabulary is named: the expected field names, and a
            # status or error that is a member of a closed Omega enum. Anything
            # else is counted, never printed -- a worker that answered wrongly
            # is not a trusted source of key names or values, and provider text
            # does not belong in the runtime log.
            _LOG.warning(
                "worker result envelope invalid: missing=%s unexpected=%s (+%d unnamed)"
                " status=%s reason=%s",
                sorted((common | {"data", "error"}) - set(result)) or "none",
                sorted((set(result) - common) & _KNOWN_FAILURE_FIELDS) or "none",
                len((set(result) - common) - _KNOWN_FAILURE_FIELDS),
                _closed_value(result.get("status"), _operation_statuses()),
                _closed_value(
                    next(
                        (result[key] for key in ("error", "reason") if key in result),
                        None,
                    ),
                    _client_error_reasons(),
                ),
            )
            raise PluginRuntimeError("plugin_operation_failed")
        if not _worker_result_identity_matches(result, request=request, binding=binding):
            raise PluginRuntimeError("plugin_authority_invalid")
        if result["status"] == McpOperationStatus.FAILED.value:
            if set(result) != {*common, "error"}:
                raise PluginRuntimeError("plugin_operation_failed")
            self._record_result_failure(
                result,
                phase=request.operation.value,
                plugin_id=binding.plugin_id,
            )
            reason = _plugin_failure_reason(result)
            if reason is None:
                raise PluginRuntimeError("plugin_operation_failed")
            raise PluginRuntimeError(reason)
        if set(result) != {*common, "data"}:
            # Which FIELDS, because `plugin_operation_failed` is raised from
            # nineteen places here and reads identically at every one. Only
            # Omega's own vocabulary is named: the expected field names, and a
            # status or error that is a member of a closed Omega enum. Anything
            # else is counted, never printed -- a worker that answered wrongly
            # is not a trusted source of key names or values, and provider text
            # does not belong in the runtime log.
            _LOG.warning(
                "worker result envelope invalid: missing=%s unexpected=%s (+%d unnamed)"
                " status=%s reason=%s",
                sorted({*common, "data"} - set(result)) or "none",
                sorted((set(result) - common) & _KNOWN_FAILURE_FIELDS) or "none",
                len((set(result) - common) - _KNOWN_FAILURE_FIELDS),
                _closed_value(result.get("status"), _operation_statuses()),
                _closed_value(
                    next(
                        (result[key] for key in ("error", "reason") if key in result),
                        None,
                    ),
                    _client_error_reasons(),
                ),
            )
            raise PluginRuntimeError("plugin_operation_failed")
        if result["status"] != McpOperationStatus.COMPLETED.value:
            raise PluginRuntimeError("plugin_operation_failed")
        data = result["data"]
        if not isinstance(data, Mapping):
            raise PluginRuntimeError("plugin_operation_failed")
        return data

    def _execute_mcp_unfenced(
        self,
        binding: object,
        operation: PluginRuntimeOperation,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        request_key = self._operation_retry_key(
            binding.plugin_id, binding.app_id, binding.capability
        )
        retry_state = self._take_retry_state(request_key)
        if retry_state is None:
            self._drift_status = None
        else:
            self._drift_status = retry_state
        self._deferred_retry_candidate = None
        self._deferred_retry_generation = None
        for attempt in range(2):
            try:
                result = self._execute_mcp_once(binding, operation, arguments)
                # Keep the completed recovery snapshot available to the
                # action/status caller.  A fresh action clears this shared
                # telemetry at entry unless it is resuming its own retry
                # lease, so retaining it here does not consume or widen the
                # action-scoped retry budget.
                return result
            except PluginRuntimeError as error:
                self._surface_provider_failure(error)
                if attempt or not self._should_recover():
                    raise
                if not self._lookup_update_once():
                    deferred = self._register_deferred_retry(error, request_key)
                    if deferred is not None:
                        self._surface_provider_failure(error)
                        raise deferred
                    self._surface_provider_failure(error)
                    raise
        raise PluginRuntimeError("plugin_operation_failed")

    def execute_sync_for_plugin(
        self,
        binding: object,
        operation: PluginRuntimeOperation,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        if type(binding) is PluginSkillBinding:
            try:
                return self._load_skill(binding, operation, arguments)
            except PluginRuntimeError as exc:
                _emit_plugin_failure(binding, operation, exc)
                raise
        guard = getattr(self, "_lifecycle_mutation_guard", None)
        scope = nullcontext() if not callable(guard) else guard()
        try:
            with scope:
                return self._execute_mcp_unfenced(binding, operation, arguments)
        except PluginRuntimeError as exc:
            _emit_plugin_failure(binding, operation, exc)
            raise

    def _load_skill(
        self,
        binding: PluginSkillBinding,
        operation: PluginRuntimeOperation,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        if (
            operation is not PluginRuntimeOperation.SKILL_LOAD
            or arguments
            or self._verified_plugin_provider is None
        ):
            raise PluginRuntimeError("plugin_skill_invalid")
        try:
            verified = self._verified_plugin_provider(binding.plugin_id)
            manifest = PluginManifest.from_dict(verified.manifest.to_dict())
            if (
                type(verified) is not VerifiedPlugin
                or manifest != verified.manifest
                or manifest.plugin_id != binding.plugin_id
                or verified.package_sha256 != binding.package_revision
            ):
                raise ValueError
            entry_points = tuple(
                entry
                for entry in manifest.entry_points
                if entry.entry_point_id == binding.capability
            )
            if len(entry_points) != 1:
                raise ValueError
            entry = entry_points[0]
            assets = tuple(
                asset
                for asset in manifest.assets
                if asset.path == entry.skill_path and asset.kind is AssetKind.SKILL
            )
            if len(assets) != 1:
                raise ValueError
            asset = assets[0]
            payload = verified.asset_bytes.get(asset.path)
            if (
                entry.skill_path != binding.asset_path
                or tuple(sorted(entry.required_app_ids)) != binding.required_app_ids
                or asset.media_type != binding.media_type
                or asset.sha256 != binding.asset_sha256
                or type(payload) is not bytes
                or sha256_hex(payload) != binding.asset_sha256
            ):
                raise ValueError
            text = payload.decode("utf-8", errors="strict")
        except (AttributeError, LookupError, TypeError, UnicodeError, ValueError):
            raise PluginRuntimeError("plugin_skill_invalid") from None
        return {
            "asset_path": binding.asset_path,
            "media_type": binding.media_type,
            "text": text,
        }

    async def _prepare_tool_operation(
        self,
        plugin_id: str,
        app_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[PluginExecutionWorkerPreparation, McpOperationRequest]:
        account_key = self._account_key_provider()
        if type(account_key) is not str or not account_key or account_key.strip() != account_key:
            raise PluginRuntimeError("plugin_account_unconfigured")
        try:
            preparation = await self._connection_service.prepare_execution_worker(
                plugin_id,
                app_id=app_id,
                account_key=account_key,
                capability_kind="tool",
                capability_id=name,
            )
        except McpClientError as exc:
            self._record_provider_failure(
                exc.reason,
                phase=getattr(exc, "phase", PluginRuntimeOperation.TOOL_CALL.value),
                plugin_id=plugin_id,
            )
            if exc.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED:
                raise PluginRuntimeError("plugin_authorization_required") from None
            raise PluginRuntimeError(_runtime_failure_reason(exc.reason)) from None
        except ValueError as exc:
            if str(exc) in {
                "plugin_authorization_required",
                "oauth_reauthorization_required",
            }:
                self._record_provider_failure(
                    McpClientErrorReason.AUTHORIZATION_REQUIRED,
                    phase=PluginRuntimeOperation.TOOL_CALL.value,
                    plugin_id=plugin_id,
                )
                raise PluginRuntimeError("plugin_authorization_required") from None
            raise PluginRuntimeError("plugin_operation_failed") from None
        if type(preparation) is not PluginExecutionWorkerPreparation:
            raise PluginRuntimeError("plugin_operation_failed")
        descriptor = self._descriptor_provider(plugin_id, app_id)
        authority = preparation.authority
        if (
            type(descriptor) is not McpConnectionDescriptor
            or descriptor != preparation.descriptor
            or authority.plugin_id != plugin_id
            or authority.app_id != app_id
            or authority.capability_kind != "tool"
            or authority.capability_id != name
            or authority.capability_revision != descriptor.capability_revision
        ):
            raise PluginRuntimeError("plugin_authority_invalid")
        loop = asyncio.get_running_loop()
        request = McpOperationRequest(
            request_id=uuid4().hex,
            app_id=app_id,
            operation=McpOperationKind.TOOL_CALL,
            capability_id=f"tool:{name}",
            arguments=arguments,
            capability_revision=descriptor.capability_revision,
            deadline_monotonic=loop.time() + self._operation_timeout_seconds,
            cancellation_id=uuid4().hex,
            task_id=None,
            input_request_id=None,
            request_state=None,
        )
        return preparation, request

    def _call_tool_once(
        self,
        plugin_id: str,
        app_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        try:
            preparation, request = self._submit(
                lambda: self._prepare_tool_operation(
                    plugin_id,
                    app_id,
                    name,
                    arguments,
                ),
                timeout_s=self._operation_timeout_seconds + 5.0,
            )
            remaining = request.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise PluginExecutionWorkerError("plugin_operation_timeout")
            result = self._execution_worker.run_sync(
                authority=preparation.authority,
                body=self._worker_body(preparation, request),
                credential=dict(preparation.credential),
                timeout_seconds=remaining,
            )
        except PluginRuntimeError:
            raise
        except PluginExecutionWorkerError as exc:
            if str(exc) == "plugin_operation_timeout":
                raise PluginRuntimeError("plugin_operation_timeout") from None
            if str(exc) == "plugin_runtime_closed":
                raise PluginRuntimeError("plugin_runtime_closed") from None
            if str(exc) in {
                "plugin_worker_reap_failed",
                "plugin_worker_reap_pending",
            }:
                raise PluginRuntimeError("plugin_worker_reap_pending") from None
            raise PluginRuntimeError("plugin_operation_failed") from None
        except Exception as exc:
            raise PluginRuntimeError("plugin_operation_failed") from exc
        if not isinstance(result, Mapping):
            raise PluginRuntimeError("plugin_operation_failed")
        common = {
            "request_id",
            "app_id",
            "capability_id",
            "capability_revision",
            "status",
        }
        if not common.issubset(result):
            raise PluginRuntimeError("plugin_operation_failed")
        if (
            result["request_id"] != request.request_id
            or result["app_id"] != app_id
            or result["capability_id"] != f"tool:{name}"
            or result["capability_revision"] != preparation.authority.capability_revision
        ):
            raise PluginRuntimeError("plugin_authority_invalid")
        if result["status"] == McpOperationStatus.FAILED.value:
            if set(result) != {*common, "error"}:
                raise PluginRuntimeError("plugin_operation_failed")
            self._record_result_failure(
                result,
                phase=request.operation.value,
                plugin_id=plugin_id,
            )
            reason = _plugin_failure_reason(result)
            if reason is None:
                raise PluginRuntimeError("plugin_operation_failed")
            raise PluginRuntimeError(reason)
        if set(result) != {*common, "data"}:
            raise PluginRuntimeError("plugin_operation_failed")
        if result["status"] != McpOperationStatus.COMPLETED.value:
            if result.get("error") == {"reason": McpClientErrorReason.AUTHORIZATION_REQUIRED.value}:
                raise PluginRuntimeError("plugin_authorization_required")
            raise PluginRuntimeError("plugin_operation_failed")
        data = result.get("data")
        if not isinstance(data, Mapping):
            raise PluginRuntimeError("plugin_operation_failed")
        return data

    def _call_tool_unfenced(
        self,
        plugin_id: str,
        app_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        request_key = self._operation_retry_key(plugin_id, app_id, name)
        retry_state = self._take_retry_state(request_key)
        if retry_state is None:
            self._drift_status = None
        else:
            self._drift_status = retry_state
        self._deferred_retry_candidate = None
        self._deferred_retry_generation = None
        for attempt in range(2):
            try:
                result = self._call_tool_once(plugin_id, app_id, name, arguments)
                # Preserve the completed recovery snapshot for the caller;
                # the next action resets it at entry while its retry lease is
                # kept separately in _plugin_retry_states.
                return result
            except PluginRuntimeError as error:
                self._surface_provider_failure(error)
                if attempt or not self._should_recover():
                    raise
                if not self._lookup_update_once():
                    deferred = self._register_deferred_retry(error, request_key)
                    if deferred is not None:
                        self._surface_provider_failure(error)
                        raise deferred
                    self._surface_provider_failure(error)
                    raise
        raise PluginRuntimeError("plugin_operation_failed")

    def call_tool_sync_for_plugin(
        self,
        plugin_id: str,
        app_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        guard = getattr(self, "_lifecycle_mutation_guard", None)
        scope = nullcontext() if not callable(guard) else guard()
        try:
            with scope:
                return self._call_tool_unfenced(
                    plugin_id,
                    app_id,
                    name,
                    arguments,
                )
        except PluginRuntimeError as exc:
            _emit_plugin_failure(
                None,
                PluginRuntimeOperation.TOOL_CALL,
                exc,
                plugin_id=plugin_id,
                app_id=app_id,
                capability=name,
            )
            raise

    def call_tool_sync(
        self,
        app_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        return self.call_tool_sync_for_plugin(self._plugin_id(app_id), app_id, name, arguments)

    def close(self) -> None:
        if self._closed:
            return
        # A failed reap keeps the exact worker generation live and fenced.
        # Do not retire connection authority or make close idempotently vanish
        # until the worker proves that tree is dead on an exact retry.
        self._execution_worker.close()
        try:
            operation = self._connection_service.close_all
            handoff = getattr(self, "_lifecycle_mutation_handoff", None)
            submitted = handoff(operation) if callable(handoff) else operation
            self._operation_submitter(submitted, timeout_s=10.0)
        except (PluginRuntimeError, RuntimeError):
            pass
        self._closed = True


__all__ = ["CurrentMcpPluginBackend", "PluginOperationSubmitter"]
