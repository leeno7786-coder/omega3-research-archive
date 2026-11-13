"""Provider-neutral execution boundary for signed plugin MCP apps.

AUTO and the HUD use this module instead of provider-specific connector dispatch.
The runtime deliberately accepts only public registry records and a backend that
already owns the current MCP transport lifecycle; it never constructs an SDK
client or reaches into a provider implementation itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field as dataclass_field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from mcp_platform.app_registry import InstalledMcpAppRecord
from mcp_platform.canonical import canonical_bytes
from mcp_platform.plugin_catalog import (
    PluginCapabilityKind,
    PluginPromptBinding,
    PluginResourceBinding,
    PluginResourceTemplateBinding,
    PluginRuntimeCatalogSnapshot,
    PluginSkillBinding,
    PluginToolBinding,
)
from mcp_platform.operation_contracts import (
    McpAuthoritySnapshot,
    McpConnectionSnapshot,
    McpOperationProposal,
    McpToolAuthority,
)
from mcp_platform.plugin_update_policy import PluginUpdateResult
from mcp_platform.uri_templates import matches_uri_template as _matches_uri_template
from plugin_platform.contracts import CapabilityKind


class PluginRuntimeError(RuntimeError):
    """Stable, content-free plugin execution failure."""


PluginRetryKey = tuple[str, str, str, str]
_PLUGIN_RETRY_KEY: ContextVar[PluginRetryKey | None] = ContextVar(
    "plugin_retry_key", default=None
)


@contextmanager
def plugin_retry_context(key: PluginRetryKey | None):
    """Carry one action's retry lease across the reacquired execution fence."""

    marker = _PLUGIN_RETRY_KEY.set(key)
    try:
        yield
    finally:
        _PLUGIN_RETRY_KEY.reset(marker)


def current_plugin_retry_key() -> PluginRetryKey | None:
    return _PLUGIN_RETRY_KEY.get()


class PluginRetryAfterFence(PluginRuntimeError):
    """Transient backend failure that is retried after lifecycle recovery."""

    def __init__(
        self,
        fallback_error: PluginRuntimeError,
        request_key: PluginRetryKey | None = None,
        recovery_status: object | None = None,
        recovery_generation: int | None = None,
    ) -> None:
        super().__init__("plugin_update_pending")
        if not isinstance(fallback_error, PluginRuntimeError):
            raise ValueError("plugin_retry_error_invalid")
        self.fallback_error = fallback_error
        if request_key is not None and (
            type(request_key) is not tuple
            or len(request_key) != 4
            or any(type(item) is not str or not item for item in request_key)
        ):
            raise ValueError("plugin_retry_key_invalid")
        self.request_key = request_key
        # Captured while the originating action still owns the execution
        # fence. The backend may process another action before the outer
        # boundary prepares this retry, so shared last-status telemetry is not
        # a valid source for the recovery budget.
        self.recovery_status = recovery_status
        if recovery_generation is not None and (
            type(recovery_generation) is not int or recovery_generation < 0
        ):
            raise ValueError("plugin_retry_generation_invalid")
        self.recovery_generation = recovery_generation
        self.outcome: PluginUpdateResult | None = None

    def resolve(self, outcome: PluginUpdateResult) -> None:
        if type(outcome) is not PluginUpdateResult:
            raise ValueError("plugin_retry_result_invalid")
        self.outcome = outcome

    def final_error(self) -> PluginRuntimeError:
        outcome = self.outcome
        if outcome is None or outcome.status == "applied":
            return self.fallback_error
        reason = {
            "approval_required": "plugin_update_approval_required",
            "failed": outcome.error or "plugin_update_failed",
            "not_available": "plugin_update_not_available",
        }[outcome.status]
        error = PluginRuntimeError(reason)
        error.update_result = outcome
        provider_status = getattr(self.fallback_error, "provider_status", None)
        if provider_status is not None:
            error.provider_status = provider_status
        return error


class PluginRuntimeOperation(StrEnum):
    """Closed provider-neutral operations accepted by the one dispatcher."""

    TOOL_CALL = "tools/call"
    PROMPTS_LIST = "prompts/list"
    PROMPT_GET = "prompts/get"
    RESOURCES_LIST = "resources/list"
    RESOURCE_TEMPLATES_LIST = "resources/templates/list"
    RESOURCE_READ = "resources/read"
    SKILL_LOAD = "skills/load"


class PluginBackend(Protocol):
    def execute_sync_for_plugin(
        self,
        binding: object,
        operation: PluginRuntimeOperation,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]: ...

    def call_tool_sync(
        self, app_id: str, name: str, arguments: dict[str, Any]
    ) -> Mapping[str, Any]: ...


class PluginRegistryLike(Protocol):
    def get(self, plugin_id: str) -> object: ...


class AppRegistryLike(Protocol):
    def get(self, app_id: str) -> InstalledMcpAppRecord: ...


def _stable_id(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field}_invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field}_invalid")
    return value


def _optional_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    value = _stable_id(value, field=field)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field}_invalid")
    return value


def _freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType(
            {_stable_id(key, field="argument_key"): _freeze(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    if type(value) is tuple:
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise ValueError("plugin_arguments_invalid")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PluginOperationRequest:
    """Immutable model decision handed to the plugin execution boundary."""

    plugin_id: str
    app_id: str | None
    capability: str
    arguments: Mapping[str, Any]
    capability_kind: str = PluginCapabilityKind.TOOL.value
    operation: PluginRuntimeOperation = PluginRuntimeOperation.TOOL_CALL
    authority_revision: str | None = None
    episode_id: str | None = None
    plan_id: str | None = None
    plan_digest: str | None = None
    step_id: str | None = None
    candidate_id: str | None = None
    input_schema_sha256: str | None = None
    account_key_hash: str | None = None
    package_revision: str | None = None
    connection_revision: str | None = None
    catalog_authority_revision: str | None = None
    capability_revision: str | None = None
    binding_revision: str | None = None
    connection: McpConnectionSnapshot | None = None
    authority: McpAuthoritySnapshot | None = None
    tool: McpToolAuthority | None = None
    proposal: McpOperationProposal | None = None
    authority_service: object | None = None
    action_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _stable_id(self.plugin_id, field="plugin_id"))
        try:
            kind = PluginCapabilityKind(self.capability_kind)
            operation = PluginRuntimeOperation(self.operation)
        except (TypeError, ValueError):
            raise ValueError("plugin_operation_invalid") from None
        if (kind is PluginCapabilityKind.SKILL) != (self.app_id is None):
            raise ValueError("plugin_operation_invalid")
        if self.app_id is not None:
            object.__setattr__(self, "app_id", _stable_id(self.app_id, field="app_id"))
        object.__setattr__(self, "capability", _stable_id(self.capability, field="capability"))
        object.__setattr__(self, "capability_kind", kind.value)
        object.__setattr__(self, "operation", operation)
        frozen = _freeze(self.arguments)
        if not isinstance(frozen, Mapping):
            raise ValueError("plugin_arguments_invalid")
        object.__setattr__(self, "arguments", frozen)
        if self.authority_revision is not None:
            object.__setattr__(
                self,
                "authority_revision",
                _stable_id(self.authority_revision, field="authority_revision"),
            )
        for field in (
            "plan_id",
            "plan_digest",
            "candidate_id",
            "input_schema_sha256",
            "account_key_hash",
            "package_revision",
            "connection_revision",
            "catalog_authority_revision",
            "capability_revision",
            "binding_revision",
        ):
            object.__setattr__(
                self,
                field,
                _optional_digest(getattr(self, field), field=field),
            )
        for field in ("episode_id", "step_id", "action_id"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _stable_id(value, field=field))
        if self.connection is not None and type(self.connection) is not McpConnectionSnapshot:
            raise ValueError("plugin_connection_invalid")
        if self.authority is not None and type(self.authority) is not McpAuthoritySnapshot:
            raise ValueError("plugin_authority_invalid")
        if self.tool is not None and type(self.tool) is not McpToolAuthority:
            raise ValueError("plugin_tool_authority_invalid")
        if self.proposal is not None and type(self.proposal) is not McpOperationProposal:
            raise ValueError("plugin_proposal_invalid")


@dataclass(frozen=True, slots=True)
class PluginOperationResult:
    """Bounded, attributed result returned from one plugin operation."""

    plugin_id: str
    app_id: str | None
    capability: str
    status: str
    data: Mapping[str, Any]
    capability_kind: str = PluginCapabilityKind.TOOL.value
    operation: PluginRuntimeOperation = PluginRuntimeOperation.TOOL_CALL
    trust: str = "untrusted_provider_data"
    attribution: Mapping[str, Any] = dataclass_field(default_factory=dict)
    guardian: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _stable_id(self.plugin_id, field="plugin_id"))
        try:
            kind = PluginCapabilityKind(self.capability_kind)
            operation = PluginRuntimeOperation(self.operation)
        except (TypeError, ValueError):
            raise ValueError("plugin_result_invalid") from None
        if (kind is PluginCapabilityKind.SKILL) != (self.app_id is None):
            raise ValueError("plugin_result_invalid")
        if self.app_id is not None:
            object.__setattr__(self, "app_id", _stable_id(self.app_id, field="app_id"))
        object.__setattr__(self, "capability", _stable_id(self.capability, field="capability"))
        object.__setattr__(self, "capability_kind", kind.value)
        object.__setattr__(self, "operation", operation)
        if self.trust not in {"untrusted_provider_data", "verified_local_package"}:
            raise ValueError("plugin_result_invalid")
        if self.status != "completed":
            raise ValueError("plugin_result_status_invalid")
        frozen = _freeze(self.data)
        if not isinstance(frozen, Mapping):
            raise ValueError("plugin_result_invalid")
        object.__setattr__(self, "data", frozen)
        attribution = _freeze(dict(self.attribution))
        guardian = _freeze(dict(self.guardian))
        if not isinstance(attribution, Mapping) or not isinstance(guardian, Mapping):
            raise ValueError("plugin_result_invalid")
        object.__setattr__(self, "attribution", attribution)
        object.__setattr__(self, "guardian", guardian)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "app_id": self.app_id,
            "capability": self.capability,
            "capability_kind": self.capability_kind,
            "operation": self.operation.value,
            "status": self.status,
            "data": _thaw(self.data),
            "trust": self.trust,
            "attribution": _thaw(self.attribution),
            "guardian": _thaw(self.guardian),
        }


AuthorityValidator = Callable[[PluginOperationRequest, InstalledMcpAppRecord], None]
RuntimeCatalogProvider = Callable[[], PluginRuntimeCatalogSnapshot]

_SURFACE_TYPES = {
    PluginCapabilityKind.TOOL.value: PluginToolBinding,
    PluginCapabilityKind.RESOURCE.value: PluginResourceBinding,
    PluginCapabilityKind.RESOURCE_TEMPLATE.value: PluginResourceTemplateBinding,
    PluginCapabilityKind.PROMPT.value: PluginPromptBinding,
    PluginCapabilityKind.SKILL.value: PluginSkillBinding,
}
_ALLOWED_OPERATIONS = {
    PluginCapabilityKind.TOOL.value: frozenset({PluginRuntimeOperation.TOOL_CALL}),
    PluginCapabilityKind.RESOURCE.value: frozenset(
        {PluginRuntimeOperation.RESOURCES_LIST, PluginRuntimeOperation.RESOURCE_READ}
    ),
    PluginCapabilityKind.RESOURCE_TEMPLATE.value: frozenset(
        {
            PluginRuntimeOperation.RESOURCE_TEMPLATES_LIST,
            PluginRuntimeOperation.RESOURCE_READ,
        }
    ),
    PluginCapabilityKind.PROMPT.value: frozenset(
        {PluginRuntimeOperation.PROMPTS_LIST, PluginRuntimeOperation.PROMPT_GET}
    ),
    PluginCapabilityKind.SKILL.value: frozenset({PluginRuntimeOperation.SKILL_LOAD}),
}


def _exact_catalog_binding(
    request: PluginOperationRequest,
    snapshot: PluginRuntimeCatalogSnapshot,
) -> object:
    if type(snapshot) is not PluginRuntimeCatalogSnapshot or request.candidate_id is None:
        raise PluginRuntimeError("plugin_candidate_stale")
    matches = tuple(
        entry for entry in snapshot.entries if entry.candidate_id == request.candidate_id
    )
    if len(matches) != 1:
        raise PluginRuntimeError("plugin_candidate_stale")
    binding = matches[0]
    expected_type = _SURFACE_TYPES.get(request.capability_kind)
    if (
        expected_type is None
        or type(binding) is not expected_type
        or binding.plugin_id != request.plugin_id
        or getattr(binding, "app_id", request.app_id) != request.app_id
        or binding.capability != request.capability
        or binding.capability_kind != request.capability_kind
        or request.operation not in _ALLOWED_OPERATIONS[request.capability_kind]
    ):
        raise PluginRuntimeError("plugin_operation_unsupported")
    expected = {
        "account_key_hash": binding.account_key_hash,
        "package_revision": binding.package_revision,
        "connection_revision": getattr(binding, "connection_revision", None),
        "catalog_authority_revision": getattr(binding, "authority_revision", None),
        "capability_revision": getattr(binding, "capability_revision", None),
        "binding_revision": binding.binding_revision,
    }
    for field_name, current in expected.items():
        requested = getattr(request, field_name)
        if requested is not None and requested != current:
            raise PluginRuntimeError("plugin_authority_invalid")
    return binding


def _binding_attribution(
    binding: object,
    operation: PluginRuntimeOperation,
) -> dict[str, object]:
    attribution = {
        "plugin_id": binding.plugin_id,
        "app_id": getattr(binding, "app_id", None),
        "account_key_hash": binding.account_key_hash,
        "package_revision": binding.package_revision,
        "connection_revision": getattr(binding, "connection_revision", ""),
        "authority_revision": getattr(binding, "authority_revision", ""),
        "capability_revision": getattr(binding, "capability_revision", ""),
        "binding_revision": binding.binding_revision,
        "candidate_id": binding.candidate_id,
        "capability_kind": binding.capability_kind,
        "capability": binding.capability,
        "operation": operation.value,
    }
    if type(binding) is PluginSkillBinding:
        attribution["app_authorities"] = [
            item.to_dict() for item in binding.app_authorities
        ]
    return attribution


def _validate_surface_request(
    binding: object,
    operation: PluginRuntimeOperation,
    arguments: Mapping[str, Any],
) -> None:
    if type(binding) is PluginToolBinding:
        try:
            Draft202012Validator(_thaw(binding.input_schema)).validate(
                _thaw(arguments)
            )
        except (SchemaError, ValidationError, TypeError, ValueError):
            raise PluginRuntimeError("plugin_request_invalid") from None
        return
    if operation in {
        PluginRuntimeOperation.RESOURCES_LIST,
        PluginRuntimeOperation.RESOURCE_TEMPLATES_LIST,
        PluginRuntimeOperation.PROMPTS_LIST,
    }:
        if arguments:
            raise PluginRuntimeError("plugin_request_invalid")
        return
    if operation is PluginRuntimeOperation.SKILL_LOAD:
        if arguments:
            raise PluginRuntimeError("plugin_skill_invalid")
        return
    if type(binding) is PluginResourceBinding:
        if arguments:
            raise PluginRuntimeError("plugin_request_invalid")
        return
    if type(binding) is PluginResourceTemplateBinding:
        uri = arguments.get("uri")
        if type(uri) is not str or set(arguments) != {"uri"}:
            raise PluginRuntimeError("plugin_request_invalid")
        try:
            from mcp_platform.client_contracts import McpClientLimits

            matches = _matches_uri_template(
                binding.uri_template,
                uri,
                McpClientLimits(),
            )
        except Exception:
            raise PluginRuntimeError("plugin_request_invalid") from None
        if not matches:
            raise PluginRuntimeError("plugin_request_invalid")
        return
    if type(binding) is PluginPromptBinding:
        schema = _thaw(binding.argument_schema)
        definitions = {
            item["name"]: item
            for item in schema["arguments"]
            if type(item) is dict
        }
        if (
            set(arguments) - set(definitions)
            or any(type(value) is not str for value in arguments.values())
            or any(
                definition["required"] and name not in arguments
                for name, definition in definitions.items()
            )
        ):
            raise PluginRuntimeError("plugin_request_invalid")


def _normalized_content_is_valid(content: object) -> bool:
    if type(content) is not dict:
        return False
    if content.get("type") == "text":
        return set(content) == {"type", "text"} and type(content["text"]) is str
    if content.get("type") != "resource":
        return False
    resource = content.get("resource")
    return (
        set(content) == {"type", "resource"}
        and type(resource) is dict
        and set(resource) == {"uri", "mime_type", "text"}
        and all(type(resource[name]) is str for name in ("uri", "mime_type", "text"))
    )


def _validate_prompt_result(raw: Mapping[str, Any]) -> None:
    if set(raw) != {"description", "messages"}:
        raise PluginRuntimeError("plugin_result_invalid")
    description = raw["description"]
    messages = raw["messages"]
    if (description is not None and type(description) is not str) or type(messages) is not list:
        raise PluginRuntimeError("plugin_result_invalid")
    for message in messages:
        if (
            type(message) is not dict
            or set(message) != {"role", "content"}
            or message["role"] not in {"user", "assistant"}
            or type(message["content"]) is not dict
        ):
            raise PluginRuntimeError("plugin_result_invalid")
        if not _normalized_content_is_valid(message["content"]):
            raise PluginRuntimeError("plugin_result_invalid")


def _validate_tool_result(raw: Mapping[str, Any], binding: PluginToolBinding) -> None:
    if set(raw) != {"content", "structured_content", "is_error"}:
        raise PluginRuntimeError("plugin_result_invalid")
    content = raw["content"]
    structured = raw["structured_content"]
    if (
        type(content) is not list
        or type(raw["is_error"]) is not bool
        or any(not _normalized_content_is_valid(item) for item in content)
        or len(canonical_bytes(dict(raw))) > binding.max_result_bytes
    ):
        raise PluginRuntimeError("plugin_result_invalid")
    mime_types: set[str] = set()
    if structured is not None:
        mime_types.add("application/json")
        try:
            Draft202012Validator(_thaw(binding.output_schema)).validate(structured)
        except (SchemaError, ValidationError, TypeError, ValueError):
            raise PluginRuntimeError("plugin_result_invalid") from None
    for item in content:
        if item["type"] == "text":
            mime_types.add("text/plain")
        else:
            mime_types.add(item["resource"]["mime_type"])
    if not mime_types.issubset(binding.allowed_mime_types):
        raise PluginRuntimeError("plugin_result_invalid")


def _resource_contents(
    raw: Mapping[str, Any],
    binding: PluginResourceBinding | PluginResourceTemplateBinding,
    arguments: Mapping[str, Any],
) -> None:
    contents = raw.get("contents")
    expected_uri = (
        binding.uri
        if type(binding) is PluginResourceBinding
        else arguments.get("uri")
    )
    if (
        type(contents) is not list
        or type(expected_uri) is not str
        or not expected_uri
        or type(binding) is PluginResourceBinding
        and arguments
        or type(binding) is PluginResourceTemplateBinding
        and set(arguments) != {"uri"}
    ):
        raise PluginRuntimeError("plugin_result_invalid")
    total_bytes = 0
    for content in contents:
        if (
            type(content) is not dict
            or set(content) != {"uri", "mime_type", "text"}
            or content["uri"] != expected_uri
            or content["mime_type"] != binding.mime_type
            or type(content["text"]) is not str
        ):
            raise PluginRuntimeError("plugin_result_invalid")
        total_bytes += len(content["text"].encode("utf-8"))
    if total_bytes > binding.max_result_bytes:
        raise PluginRuntimeError("plugin_result_invalid")


def _listed_surface(
    raw: Mapping[str, Any],
    binding: PluginResourceBinding | PluginResourceTemplateBinding | PluginPromptBinding,
    operation: PluginRuntimeOperation,
) -> None:
    if operation is PluginRuntimeOperation.RESOURCES_LIST:
        field = "resources"
        expected = {"name": binding.capability, "uri": binding.uri, "mime_type": binding.mime_type}
    elif operation is PluginRuntimeOperation.RESOURCE_TEMPLATES_LIST:
        field = "resource_templates"
        expected = {
            "name": binding.capability,
            "uri_template": binding.uri_template,
            "mime_type": binding.mime_type,
        }
    else:
        field = "prompts"
        expected = {
            "name": binding.capability,
            "arguments": _thaw(binding.argument_schema)["arguments"],
        }
    items = raw.get(field)
    if type(items) is not list or not any(
        type(item) is dict
        and all(item.get(key) == value for key, value in expected.items())
        for item in items
    ):
        raise PluginRuntimeError("plugin_result_invalid")


def _validate_surface_result(
    raw: Mapping[str, Any],
    binding: object,
    operation: PluginRuntimeOperation,
    arguments: Mapping[str, Any],
) -> None:
    try:
        canonical_bytes(dict(raw))
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise PluginRuntimeError("plugin_result_invalid") from None
    if operation is PluginRuntimeOperation.TOOL_CALL:
        _validate_tool_result(raw, binding)
    elif operation is PluginRuntimeOperation.RESOURCE_READ:
        _resource_contents(raw, binding, arguments)
    elif operation in {
        PluginRuntimeOperation.RESOURCES_LIST,
        PluginRuntimeOperation.RESOURCE_TEMPLATES_LIST,
        PluginRuntimeOperation.PROMPTS_LIST,
    }:
        _listed_surface(raw, binding, operation)
    elif operation is PluginRuntimeOperation.PROMPT_GET:
        _validate_prompt_result(raw)
        if len(canonical_bytes(dict(raw))) > binding.max_result_bytes:
            raise PluginRuntimeError("plugin_result_invalid")


def _decode_screened_provider_result(original: Any, screened: Any) -> Any:
    if isinstance(original, Mapping):
        entries = screened.get("__omega_mcp_entries__") if type(screened) is dict else None
        if type(entries) is not list or len(entries) != len(original):
            raise PluginRuntimeError("plugin_result_invalid")
        decoded: dict[str, Any] = {}
        for (key, value), entry in zip(
            sorted(original.items(), key=lambda item: item[0]),
            entries,
            strict=True,
        ):
            if (
                type(entry) is not dict
                or set(entry) != {"__omega_mcp_key__", "__omega_mcp_value__"}
                or entry["__omega_mcp_key__"] != key
            ):
                raise PluginRuntimeError("plugin_result_invalid")
            decoded[key] = _decode_screened_provider_result(
                value,
                entry["__omega_mcp_value__"],
            )
        return decoded
    if type(original) is list:
        if type(screened) is not list or len(screened) != len(original):
            raise PluginRuntimeError("plugin_result_invalid")
        return [
            _decode_screened_provider_result(left, right)
            for left, right in zip(original, screened, strict=True)
        ]
    return screened


def _screen_provider_result(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from mcp_platform.ingress import _encode_external_mappings, _guardian_result

        original = dict(value)
        encoded = _encode_external_mappings(original)
        screened, guardian = _guardian_result(encoded, source_name="plugin-result")
        decoded = _decode_screened_provider_result(original, screened)
        if type(decoded) is not dict:
            raise ValueError
        return decoded, guardian
    except PluginRuntimeError:
        raise
    except Exception:
        raise PluginRuntimeError("plugin_result_invalid") from None


class PluginRuntime:
    """Execute signed plugin capabilities through one provider-neutral backend."""

    def __init__(
        self,
        *,
        plugin_registry: PluginRegistryLike,
        app_registry: AppRegistryLike,
        backend: PluginBackend,
        authority_validator: AuthorityValidator | None = None,
        mutation_scope: Callable[[], object] | None = None,
        catalog_provider: RuntimeCatalogProvider | None = None,
    ) -> None:
        self._plugin_registry = plugin_registry
        self._app_registry = app_registry
        self._backend = backend
        self._authority_validator = authority_validator
        self._mutation_scope = mutation_scope
        if catalog_provider is not None and not callable(catalog_provider):
            raise ValueError("plugin_catalog_provider_invalid")
        self._catalog_provider = catalog_provider

    def invoke_tool(self, request: PluginOperationRequest) -> PluginOperationResult:
        if (
            type(request) is not PluginOperationRequest
            or request.capability_kind != PluginCapabilityKind.TOOL.value
            or request.operation is not PluginRuntimeOperation.TOOL_CALL
        ):
            raise PluginRuntimeError("plugin_operation_unsupported")
        return self.execute(request)

    def execute(self, request: PluginOperationRequest) -> PluginOperationResult:
        # Preserve a key supplied by an outer AUTO/plan action boundary.  A
        # nested runtime scope must not clear that action's retry lease.
        retry_key: PluginRetryKey | None = current_plugin_retry_key()
        for attempt in range(2):
            scope = nullcontext() if self._mutation_scope is None else self._mutation_scope()
            try:
                with scope, plugin_retry_context(retry_key):
                    return self._execute_unfenced(request)
            except PluginRetryAfterFence as deferred:
                if attempt == 0 and deferred.outcome is None:
                    raise
                if attempt == 0 and deferred.outcome is not None and deferred.outcome.status == "applied":
                    prepare_retry = getattr(self._backend, "prepare_plugin_retry", None)
                    if callable(prepare_retry):
                        prepare_retry(deferred.request_key, deferred.recovery_status)
                    request = self._refresh_request_for_retry(request)
                    retry_key = deferred.request_key
                    continue
                raise deferred.final_error()
        raise PluginRuntimeError("plugin_operation_failed")

    def _refresh_request_for_retry(
        self, request: PluginOperationRequest
    ) -> PluginOperationRequest:
        """Rebind the same request to the newly published compatible package."""

        if self._catalog_provider is None or request.candidate_id is None:
            return request
        try:
            snapshot = self._catalog_provider()
            matches = tuple(
                entry
                for entry in snapshot.entries
                if entry.plugin_id == request.plugin_id
                and getattr(entry, "app_id", request.app_id) == request.app_id
                and entry.capability == request.capability
                and entry.capability_kind == request.capability_kind
            )
            if len(matches) != 1:
                raise ValueError
            binding = matches[0]
            current = {
                "account_key_hash": binding.account_key_hash,
                "connection_revision": getattr(binding, "connection_revision", None),
                "catalog_authority_revision": getattr(binding, "authority_revision", None),
                "capability_revision": getattr(binding, "capability_revision", None),
            }
            for field_name, value in current.items():
                requested = getattr(request, field_name)
                if requested is not None and requested != value:
                    raise PluginRuntimeError("plugin_authority_invalid")
            return replace(
                request,
                candidate_id=binding.candidate_id,
                package_revision=binding.package_revision,
                connection_revision=current["connection_revision"],
                catalog_authority_revision=current["catalog_authority_revision"],
                capability_revision=current["capability_revision"],
                binding_revision=binding.binding_revision,
                input_schema_sha256=getattr(binding, "input_schema_sha256", None),
            )
        except PluginRuntimeError:
            raise
        except Exception:
            raise PluginRuntimeError("plugin_authority_invalid") from None

    def _execute_unfenced(
        self,
        request: PluginOperationRequest,
    ) -> PluginOperationResult:
        if type(request) is not PluginOperationRequest:
            raise PluginRuntimeError("plugin_request_invalid")
        try:
            plugin = self._plugin_registry.get(request.plugin_id)
        except PluginRuntimeError:
            raise
        except Exception:
            raise PluginRuntimeError("plugin_not_installed") from None
        if getattr(plugin, "status", None) != "installed":
            raise PluginRuntimeError("plugin_not_installed")
        if getattr(plugin, "enabled", None) is not True:
            raise PluginRuntimeError("plugin_disabled")
        binding: object | None = None
        if self._catalog_provider is not None:
            try:
                binding = _exact_catalog_binding(request, self._catalog_provider())
            except PluginRuntimeError:
                raise
            except Exception:
                raise PluginRuntimeError("plugin_catalog_unavailable") from None
        elif (
            request.capability_kind != PluginCapabilityKind.TOOL.value
            or request.operation is not PluginRuntimeOperation.TOOL_CALL
        ):
            raise PluginRuntimeError("plugin_catalog_unavailable")
        if binding is not None:
            _validate_surface_request(
                binding,
                request.operation,
                request.arguments,
            )
        record: InstalledMcpAppRecord | None = None
        if request.capability_kind != PluginCapabilityKind.SKILL.value:
            try:
                record = self._app_registry.get(request.app_id)
            except PluginRuntimeError:
                raise
            except Exception:
                raise PluginRuntimeError("plugin_app_not_installed") from None
            if (
                type(record) is not InstalledMcpAppRecord
                or not record.installed
                or not record.trusted
            ):
                raise PluginRuntimeError("plugin_app_untrusted")
        declared_kind = {
            PluginCapabilityKind.TOOL.value: CapabilityKind.TOOL,
            PluginCapabilityKind.PROMPT.value: CapabilityKind.PROMPT,
            PluginCapabilityKind.RESOURCE.value: CapabilityKind.RESOURCE,
            PluginCapabilityKind.RESOURCE_TEMPLATE.value: CapabilityKind.RESOURCE,
        }.get(request.capability_kind)
        if declared_kind is not None and record is not None and not any(
            item.kind is declared_kind and item.name == request.capability
            for item in record.capabilities
        ):
            raise PluginRuntimeError("capability_not_declared")
        if self._authority_validator is not None and record is not None:
            try:
                self._authority_validator(request, record)
            except PluginRuntimeError:
                raise
            except Exception:
                raise PluginRuntimeError("plugin_authority_invalid") from None
        if request.authority is not None:
            if (
                request.connection is None
                or request.tool is None
                or request.proposal is None
                or request.authority_service is None
                or not callable(getattr(request.authority_service, "evaluate_operation", None))
            ):
                raise PluginRuntimeError("plugin_authority_invalid")
            try:
                decision = request.authority_service.evaluate_operation(
                    request.proposal,
                    connection=request.connection,
                    authority=request.authority,
                    tool=request.tool,
                )
            except PluginRuntimeError:
                raise
            except Exception:
                raise PluginRuntimeError("plugin_authority_invalid") from None
            if getattr(decision, "allowed", None) is not True:
                raise PluginRuntimeError("plugin_authority_rejected")
        action_id = request.action_id
        ledger = None
        if request.episode_id is not None:
            try:
                from mcp_platform import action_ledger as ledger_module

                ledger = ledger_module
                action_id = action_id or ledger.mint_action_id()
                ledger.record_dispatch(
                    omega_action_id=action_id,
                    episode_id=request.episode_id,
                    capability=request.capability,
                    operation=request.operation.value,
                    is_write=request.authority is not None,
                    provider=(
                        request.connection.provider_id
                        if request.connection is not None
                        else request.plugin_id
                    ),
                    account=(
                        request.connection.account_ref
                        if request.connection is not None
                        else ""
                    ),
                    connection_id=(
                        request.connection.connection_id
                        if request.connection is not None
                        else ""
                    ),
                    connection_revision=(
                        request.connection.connection_revision
                        if request.connection is not None
                        else ""
                    ),
                    tool_schema_sha256=request.input_schema_sha256 or "",
                    authority_kind=(
                        request.authority.authority_kind
                        if request.authority is not None
                        else ""
                    ),
                    authority_id=(
                        request.authority.authority_id
                        if request.authority is not None
                        else ""
                    ),
                    authority_revision=(
                        request.authority.authority_revision
                        if request.authority is not None
                        else ""
                    ),
                    proposal_digest=(
                        request.proposal.proposal_digest
                        if request.proposal is not None
                        else ""
                    ),
                    idempotency_key=(
                        request.proposal.idempotency_key
                        if request.proposal is not None
                        else action_id
                    ),
                )
            except Exception:
                raise PluginRuntimeError("plugin_action_ledger_unavailable") from None
        try:
            generic = getattr(self._backend, "execute_sync_for_plugin", None)
            if binding is not None:
                if not callable(generic):
                    raise PluginRuntimeError("plugin_runtime_unavailable")
                raw = generic(
                    binding,
                    request.operation,
                    _thaw(request.arguments),
                )
            else:
                plugin_aware = getattr(self._backend, "call_tool_sync_for_plugin", None)
                if callable(plugin_aware):
                    raw = plugin_aware(
                        request.plugin_id,
                        request.app_id,
                        request.capability,
                        _thaw(request.arguments),
                    )
                else:
                    raw = self._backend.call_tool_sync(
                        request.app_id,
                        request.capability,
                        _thaw(request.arguments),
                    )
        except PluginRuntimeError as exc:
            if isinstance(exc, PluginRetryAfterFence):
                raise
            if ledger is not None and action_id is not None:
                ledger.record_failure(action_id, str(exc), ambiguous=request.authority is not None)
            raise
        except Exception:
            if ledger is not None and action_id is not None:
                ledger.record_failure(action_id, "plugin_operation_failed", ambiguous=True)
            raise PluginRuntimeError("plugin_operation_failed") from None
        except BaseException as exc:
            if ledger is not None and action_id is not None:
                try:
                    ledger.record_failure(
                        action_id,
                        "plugin_operation_cancelled",
                        ambiguous=request.authority is not None,
                    )
                except BaseException as ledger_error:
                    exc.add_note(f"plugin cancellation ledger cleanup failed: {ledger_error}")
            raise
        if not isinstance(raw, Mapping):
            if ledger is not None and action_id is not None:
                ledger.record_failure(action_id, "plugin_operation_failed", ambiguous=True)
            raise PluginRuntimeError("plugin_operation_failed")
        projected = dict(raw)
        attribution: dict[str, object] = {}
        guardian: dict[str, object] = {}
        trust = "untrusted_provider_data"
        if binding is not None:
            attribution = _binding_attribution(binding, request.operation)
            if request.capability_kind == PluginCapabilityKind.SKILL.value:
                trust = "verified_local_package"
            else:
                _validate_surface_result(
                    projected,
                    binding,
                    request.operation,
                    request.arguments,
                )
                projected, guardian = _screen_provider_result(projected)
        try:
            result = PluginOperationResult(
                plugin_id=request.plugin_id,
                app_id=request.app_id,
                capability=request.capability,
                capability_kind=request.capability_kind,
                operation=request.operation,
                status="completed",
                data=projected,
                trust=trust,
                attribution=attribution,
                guardian=guardian,
            )
        except (TypeError, ValueError):
            if ledger is not None and action_id is not None:
                ledger.record_failure(action_id, "plugin_operation_failed", ambiguous=True)
            raise PluginRuntimeError("plugin_operation_failed") from None
        if ledger is not None and action_id is not None:
            try:
                ledger.record_receipt(
                    action_id,
                    receipt=result.to_dict()["data"],
                )
            except Exception:
                raise PluginRuntimeError("plugin_action_ledger_unavailable") from None
        return result


def get_default_plugin_runtime() -> PluginRuntime:
    """Return the process-owned runtime backed by the plugin lifecycle service."""
    from mcp_platform.plugin_lifecycle import get_default_plugin_lifecycle_service

    runtime = get_default_plugin_lifecycle_service().plugin_runtime
    if type(runtime) is not PluginRuntime:
        raise RuntimeError("plugin_runtime_unavailable")
    return runtime


__all__ = [
    "PluginOperationRequest",
    "PluginOperationResult",
    "PluginRetryAfterFence",
    "PluginRetryKey",
    "current_plugin_retry_key",
    "plugin_retry_context",
    "PluginRuntime",
    "PluginRuntimeError",
    "PluginRuntimeOperation",
    "get_default_plugin_runtime",
]
