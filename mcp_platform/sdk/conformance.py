"""Content-free conformance accounting for first-party MCP servers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from mcp import types

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import (
    MCP_PROTOCOL_VERSION,
    ResourceCatalog,
    ToolCatalog,
)

_MISSING = object()


@dataclass(frozen=True)
class ConformanceIdentity:
    server_id: str
    server_version: str
    protocol_version: str
    capabilities: types.ServerCapabilities


@dataclass(frozen=True)
class ConformanceReport:
    """Counts and stable failure reason codes; never probe content."""

    passed: bool
    checks_run: int
    passed_checks: int
    failed_checks: int
    reason_codes: tuple[str, ...]


class ConformanceAdapter(Protocol):
    """Narrow synchronous observation/probe boundary for conformance runners."""

    def identity(self) -> ConformanceIdentity: ...

    def list_tools(self) -> Sequence[types.Tool]: ...

    def list_prompts(self) -> Sequence[types.Prompt]: ...

    def list_resources(self) -> Sequence[types.Resource]: ...

    def list_resource_templates(self) -> Sequence[types.ResourceTemplate]: ...

    def rejects_invalid_input(self) -> bool: ...

    def rejects_invalid_output(self) -> bool: ...

    def refuses_sampling(self) -> bool: ...

    def refuses_roots(self) -> bool: ...

    def stdout_is_protocol_only(self) -> bool: ...

    def enforces_maximum_result(self) -> bool: ...

    def operational_status(self) -> Mapping[str, object]: ...


def run_conformance(
    server_adapter: ConformanceAdapter,
    tool_catalog: ToolCatalog,
    resource_catalog: ResourceCatalog,
    *,
    prompts: Sequence[types.Prompt] = (),
    extensions: Mapping[str, Mapping[str, object]] | None = None,
) -> ConformanceReport:
    """Run every closed-world check and retain only counts/reason codes."""
    identity = _observe(lambda: server_adapter.identity())
    tools = _observe(lambda: server_adapter.list_tools())
    observed_prompts = _observe(lambda: server_adapter.list_prompts())
    resources = _observe(lambda: server_adapter.list_resources())
    templates = _observe(lambda: server_adapter.list_resource_templates())
    invalid_input = _observe(lambda: server_adapter.rejects_invalid_input())
    invalid_output = _observe(lambda: server_adapter.rejects_invalid_output())
    sampling = _observe(lambda: server_adapter.refuses_sampling())
    roots = _observe(lambda: server_adapter.refuses_roots())
    stdout = _observe(lambda: server_adapter.stdout_is_protocol_only())
    maximum = _observe(lambda: server_adapter.enforces_maximum_result())
    status = _observe(lambda: server_adapter.operational_status())
    results = [
        (
            _identity_matches(identity, tool_catalog, resource_catalog),
            "server_identity_mismatch",
        ),
        (
            _version_matches(identity, tool_catalog, resource_catalog),
            "server_version_mismatch",
        ),
        (
            _protocol_matches(identity),
            "protocol_version_mismatch",
        ),
        (
            _tools_match(tools, tool_catalog),
            "tool_catalog_mismatch",
        ),
        (
            _resources_match(resources, resource_catalog),
            "resource_catalog_mismatch",
        ),
        (
            _templates_match(templates, resource_catalog),
            "resource_template_catalog_mismatch",
        ),
        (invalid_input is True, "invalid_input_accepted"),
        (invalid_output is True, "invalid_output_accepted"),
        (
            _prompt_capability_matches(identity, expected=bool(prompts)),
            "prompt_capability_mismatch",
        ),
        (sampling is True, "sampling_request_not_refused"),
        (roots is True, "roots_request_not_refused"),
        (
            _capability_absent(identity, "tasks"),
            "tasks_capability_advertised",
        ),
        (stdout is True, "stdout_pollution_detected"),
        (maximum is True, "maximum_result_not_enforced"),
        (_status_is_safe(status), "operational_status_sensitive_key"),
        (_prompts_match(observed_prompts, prompts), "prompt_catalog_mismatch"),
        (
            _extensions_match(identity, extensions or {}),
            "extension_advertisement_mismatch",
        ),
    ]
    reasons = tuple(reason for passed, reason in results if not passed)
    failed = len(reasons)
    total = len(results)
    return ConformanceReport(
        passed=failed == 0,
        checks_run=total,
        passed_checks=total - failed,
        failed_checks=failed,
        reason_codes=reasons,
    )


def _observe(call: Callable[[], object]) -> object:
    try:
        return call()
    except Exception:
        return _MISSING


def _valid_identity(value: object) -> bool:
    return (
        type(value) is ConformanceIdentity
        and type(value.server_id) is str
        and type(value.server_version) is str
        and type(value.protocol_version) is str
        and type(value.capabilities) is types.ServerCapabilities
    )


def _identity_matches(
    identity: object,
    tool_catalog: ToolCatalog,
    resource_catalog: ResourceCatalog,
) -> bool:
    return (
        _valid_identity(identity)
        and identity.server_id == tool_catalog.server_id
        and identity.server_id == resource_catalog.server_id
    )


def _version_matches(
    identity: object,
    tool_catalog: ToolCatalog,
    resource_catalog: ResourceCatalog,
) -> bool:
    return (
        _valid_identity(identity)
        and identity.server_version == tool_catalog.server_version
        and identity.server_version == resource_catalog.server_version
    )


def _protocol_matches(identity: object) -> bool:
    return _valid_identity(identity) and identity.protocol_version == MCP_PROTOCOL_VERSION


def _capability_absent(identity: object, name: str) -> bool:
    if not _valid_identity(identity):
        return False
    return getattr(identity.capabilities, name, _MISSING) is None


def _prompt_capability_matches(identity: object, *, expected: bool) -> bool:
    if not _valid_identity(identity):
        return False
    capability = identity.capabilities.prompts
    if not expected:
        return capability is None
    return capability == types.PromptsCapability(listChanged=True)


def _extensions_match(
    identity: object,
    expected: Mapping[str, Mapping[str, object]],
) -> bool:
    if not _valid_identity(identity):
        return False
    try:
        return canonical_bytes(identity.capabilities.extensions or {}) == canonical_bytes(
            dict(expected)
        )
    except Exception:
        return False


def _tools_match(runtime: object, catalog: ToolCatalog) -> bool:
    if type(runtime) not in (list, tuple) or len(runtime) != len(catalog.tools):
        return False
    for actual, signed in zip(runtime, catalog.tools, strict=True):
        if type(actual) is not types.Tool:
            return False
        try:
            if (
                type(actual.name) is not str
                or type(actual.title) is not str
                or type(actual.input_schema) is not dict
                or type(actual.output_schema) is not dict
            ):
                return False
            input_digest = sha256_hex(canonical_bytes(actual.input_schema))
            output_digest = sha256_hex(canonical_bytes(actual.output_schema))
            if (
                actual.name != signed.name
                or actual.title != signed.title
                or input_digest != signed.input_schema_sha256
                or output_digest != signed.output_schema_sha256
            ):
                return False
        except Exception:
            return False
    return True


def _prompts_match(runtime: object, expected: Sequence[types.Prompt]) -> bool:
    if type(runtime) not in (list, tuple) or any(
        type(prompt) is not types.Prompt for prompt in runtime
    ):
        return False
    try:
        actual_documents = [
            prompt.model_dump(mode="json", by_alias=True, exclude_none=True) for prompt in runtime
        ]
        expected_documents = [
            prompt.model_dump(mode="json", by_alias=True, exclude_none=True) for prompt in expected
        ]
        return canonical_bytes(actual_documents) == canonical_bytes(expected_documents)
    except Exception:
        return False


def _resources_match(runtime: object, catalog: ResourceCatalog) -> bool:
    if type(runtime) not in (list, tuple) or any(
        type(resource) is not types.Resource for resource in runtime
    ):
        return False
    try:
        if any(
            type(resource.uri) is not str
            or type(resource.name) is not str
            or type(resource.mime_type) is not str
            for resource in runtime
        ):
            return False
        actual = [(str(resource.uri), resource.name, resource.mime_type) for resource in runtime]
    except Exception:
        return False
    return actual == [
        (resource.uri, resource.name, resource.mime_type) for resource in catalog.resources
    ]


def _templates_match(runtime: object, catalog: ResourceCatalog) -> bool:
    if type(runtime) not in (list, tuple) or any(
        type(template) is not types.ResourceTemplate for template in runtime
    ):
        return False
    try:
        if any(
            type(template.uri_template) is not str
            or type(template.name) is not str
            or type(template.mime_type) is not str
            for template in runtime
        ):
            return False
        actual = [
            (template.uri_template, template.name, template.mime_type) for template in runtime
        ]
    except Exception:
        return False
    return actual == [
        (template.uri_template, template.name, template.mime_type)
        for template in catalog.resource_templates
    ]


def _status_is_safe(value: object) -> bool:
    try:
        return isinstance(value, Mapping) and not _has_sensitive_status_key(value, seen=set())
    except Exception:
        return False


def _has_sensitive_status_key(value: object, *, seen: set[int]) -> bool:
    identity = id(value)
    if identity in seen:
        return False
    if isinstance(value, Mapping):
        seen.add(identity)
        for key, nested in value.items():
            if type(key) is not str:
                return True
            lowered = key.casefold()
            if any(term in lowered for term in ("content", "token", "secret")):
                return True
            if _has_sensitive_status_key(nested, seen=seen):
                return True
        return False
    if type(value) in (list, tuple):
        seen.add(identity)
        return any(_has_sensitive_status_key(item, seen=seen) for item in value)
    return type(value) not in (str, int, float, bool, type(None))
