"""Closed immutable contracts for MCP inference preparation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from mcp_platform.canonical import canonical_bytes, sha256_hex

_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_JSON_DEPTH = 48
_MAX_JSON_NODES = 20_000
_MAX_JSON_BYTES = 1_048_576
_MAX_STRING_BYTES = 65_536
_MAX_INPUT_TOKENS = 1_000_000
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_TRUST_LABELS = frozenset({"trusted", "untrusted"})


class McpInferencePurpose(str, Enum):
    """The nine bounded reasoning purposes available to the MCP lane."""

    GOAL_INTERPRETATION = "goal_interpretation"
    ADAPTIVE_INTERVIEW = "adaptive_interview"
    PROVIDER_CAPABILITY_SELECTION = "provider_capability_selection"
    LEARNING_BOUNDARY_RECOMMENDATION = "learning_boundary_recommendation"
    WORKFLOW_PROPOSAL = "workflow_proposal"
    DIRECT_OPERATION_PROPOSAL = "direct_operation_proposal"
    STANDING_OPERATION_PROPOSAL = "standing_operation_proposal"
    CROSS_PROVIDER_TRANSFORMATION_PROPOSAL = (
        "cross_provider_transformation_proposal"
    )
    USER_FACING_EXPLANATION = "user_facing_explanation"


def _stable_string(
    value: Any,
    *,
    error: str,
    allow_empty: bool = False,
    max_bytes: int = _MAX_STRING_BYTES,
) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError(error)
    if value.strip() != value:
        raise ValueError(error)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError(error) from None
    if len(encoded) > max_bytes:
        raise ValueError(error)
    return value


def _digest(value: Any, *, error: str) -> str:
    digest = _stable_string(value, error=error, max_bytes=64)
    if _HEX_RE.fullmatch(digest) is None:
        raise ValueError(error)
    return digest


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _freeze_json(value: Any, *, error: str) -> Any:
    """Own bounded JSON without invoking arbitrary mapping subclasses."""

    seen: set[int] = set()
    nodes = 0
    frozen_input = type(value) in {tuple, _MAPPING_PROXY_TYPE}

    def copy(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise ValueError(error)
        if item is None or type(item) in {bool, int}:
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError(error)
            return item
        if type(item) is str:
            try:
                encoded = item.encode("utf-8")
            except UnicodeError:
                raise ValueError(error) from None
            if len(encoded) > _MAX_STRING_BYTES:
                raise ValueError(error)
            return item
        mapping_types = {_MAPPING_PROXY_TYPE} if frozen_input else {dict}
        sequence_types = {tuple} if frozen_input else {list}
        if type(item) in mapping_types:
            identity = id(item)
            if identity in seen:
                raise ValueError(error)
            seen.add(identity)
            try:
                result: dict[str, Any] = {}
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError(error)
                    _stable_string(key, error=error)
                    result[key] = copy(child, depth + 1)
                return MappingProxyType(result)
            finally:
                seen.remove(identity)
        if type(item) in sequence_types:
            identity = id(item)
            if identity in seen:
                raise ValueError(error)
            seen.add(identity)
            try:
                return tuple(copy(child, depth + 1) for child in item)
            finally:
                seen.remove(identity)
        raise ValueError(error)

    try:
        frozen = copy(value, 0)
        if len(canonical_bytes(_thaw_json(frozen))) > _MAX_JSON_BYTES:
            raise ValueError(error)
        return frozen
    except ValueError:
        raise ValueError(error) from None
    except (OverflowError, RecursionError, TypeError, UnicodeError):
        raise ValueError(error) from None


def _string_tuple(value: Any, *, error: str, allow_empty: bool = True) -> tuple[str, ...]:
    if type(value) not in {list, tuple}:
        raise ValueError(error)
    parsed = tuple(_stable_string(item, error=error) for item in value)
    if (not allow_empty and not parsed) or len(parsed) != len(set(parsed)):
        raise ValueError(error)
    return parsed


def _revision_mapping(value: Any, *, error: str) -> Mapping[str, str]:
    if type(value) not in {dict, _MAPPING_PROXY_TYPE}:
        raise ValueError(error)
    if not value:
        raise ValueError(error)
    result: dict[str, str] = {}
    for key, revision in value.items():
        stable_key = _stable_string(key, error=error)
        result[stable_key] = _digest(revision, error=error)
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True)
class McpPromptSection:
    """One trust-labeled prompt section in deterministic presentation order."""

    label: str
    trust: str
    content: str

    def __post_init__(self) -> None:
        error = "invalid_mcp_prompt_section"
        object.__setattr__(self, "label", _stable_string(self.label, error=error))
        if self.trust not in _TRUST_LABELS:
            raise ValueError(error)
        object.__setattr__(
            self,
            "content",
            _stable_string(self.content, error=error, allow_empty=True),
        )

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "trust": self.trust, "content": self.content}


@dataclass(frozen=True)
class McpInferenceInput:
    """Trusted and attributed inputs accepted by the preparation-only seam."""

    purpose: McpInferencePurpose
    temporal_line: str
    operator_goal: str
    memory_evidence: tuple[Mapping[str, Any], ...]
    signed_tool_projection: tuple[Mapping[str, Any], ...]
    provider_id: str
    account_refs: tuple[str, ...]
    connection_revisions: Mapping[str, str]
    experience_revision: str
    authority_revision: str
    authority_bounds: Mapping[str, Any]
    untrusted_evidence: tuple[Any, ...]
    max_input_tokens: int

    def __post_init__(self) -> None:
        error = "invalid_mcp_inference_input"
        if type(self.purpose) is not McpInferencePurpose:
            raise ValueError(error)
        object.__setattr__(
            self, "temporal_line", _stable_string(self.temporal_line, error=error)
        )
        object.__setattr__(
            self, "operator_goal", _stable_string(self.operator_goal, error=error)
        )
        memory = _freeze_json(
            self.memory_evidence, error="invalid_mcp_memory_evidence"
        )
        tools = _freeze_json(self.signed_tool_projection, error=error)
        evidence = _freeze_json(self.untrusted_evidence, error=error)
        bounds = _freeze_json(self.authority_bounds, error=error)
        if (
            type(memory) is not tuple
            or type(tools) is not tuple
            or type(evidence) is not tuple
            or not isinstance(bounds, Mapping)
        ):
            raise ValueError(error)
        if any(not isinstance(item, Mapping) for item in memory + tools):
            raise ValueError(error)
        object.__setattr__(self, "memory_evidence", memory)
        object.__setattr__(self, "signed_tool_projection", tools)
        object.__setattr__(self, "untrusted_evidence", evidence)
        object.__setattr__(self, "authority_bounds", bounds)
        object.__setattr__(self, "provider_id", _stable_string(self.provider_id, error=error))
        object.__setattr__(
            self,
            "account_refs",
            _string_tuple(self.account_refs, error=error, allow_empty=False),
        )
        object.__setattr__(
            self,
            "connection_revisions",
            _revision_mapping(self.connection_revisions, error=error),
        )
        object.__setattr__(
            self, "experience_revision", _digest(self.experience_revision, error=error)
        )
        object.__setattr__(
            self, "authority_revision", _digest(self.authority_revision, error=error)
        )
        if (
            type(self.max_input_tokens) is not int
            or self.max_input_tokens <= 0
            or self.max_input_tokens > _MAX_INPUT_TOKENS
        ):
            raise ValueError(error)


@dataclass(frozen=True)
class PreparedMcpInference:
    """A deterministic inference request that deliberately has no submission state."""

    request_id: str
    prompt_version: str
    purpose: McpInferencePurpose
    lane_hint: str
    system_kernel: str
    sections: tuple[McpPromptSection, ...]
    response_schema: Mapping[str, Any]
    memory_ids: tuple[str, ...]
    tool_schema_refs: tuple[str, ...]
    provider_id: str
    account_refs: tuple[str, ...]
    connection_revisions: Mapping[str, str]
    experience_revision: str
    authority_revision: str
    max_input_tokens: int
    estimated_input_tokens: int
    omission_counts: Mapping[str, int]
    request_digest: str

    def __post_init__(self) -> None:
        error = "invalid_prepared_mcp_inference"
        for field in ("request_id", "prompt_version", "system_kernel", "provider_id"):
            object.__setattr__(self, field, _stable_string(getattr(self, field), error=error))
        if type(self.purpose) is not McpInferencePurpose or self.lane_hint != "mcp":
            raise ValueError(error)
        if type(self.sections) not in {list, tuple} or any(
            type(section) is not McpPromptSection for section in self.sections
        ):
            raise ValueError(error)
        object.__setattr__(self, "sections", tuple(self.sections))
        schema = _freeze_json(self.response_schema, error=error)
        if not isinstance(schema, Mapping):
            raise ValueError(error)
        object.__setattr__(self, "response_schema", schema)
        object.__setattr__(self, "memory_ids", _string_tuple(self.memory_ids, error=error))
        object.__setattr__(
            self, "tool_schema_refs", _string_tuple(self.tool_schema_refs, error=error)
        )
        object.__setattr__(
            self,
            "account_refs",
            _string_tuple(self.account_refs, error=error, allow_empty=False),
        )
        object.__setattr__(
            self,
            "connection_revisions",
            _revision_mapping(self.connection_revisions, error=error),
        )
        object.__setattr__(
            self, "experience_revision", _digest(self.experience_revision, error=error)
        )
        object.__setattr__(
            self, "authority_revision", _digest(self.authority_revision, error=error)
        )
        if (
            type(self.max_input_tokens) is not int
            or type(self.estimated_input_tokens) is not int
            or self.max_input_tokens <= 0
            or self.estimated_input_tokens <= 0
            or self.estimated_input_tokens > self.max_input_tokens
        ):
            raise ValueError(error)
        if type(self.omission_counts) not in {dict, _MAPPING_PROXY_TYPE} or set(
            self.omission_counts
        ) != {"memory", "tools", "external_evidence"}:
            raise ValueError(error)
        omissions = dict(self.omission_counts)
        if any(type(value) is not int or value < 0 for value in omissions.values()):
            raise ValueError(error)
        object.__setattr__(self, "omission_counts", MappingProxyType(omissions))
        object.__setattr__(self, "request_digest", _digest(self.request_digest, error=error))
        if sha256_hex(canonical_bytes(self.to_dict(include_digest=False))) != self.request_digest:
            raise ValueError(error)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        document: dict[str, object] = {
            "request_id": self.request_id,
            "prompt_version": self.prompt_version,
            "purpose": self.purpose.value,
            "lane_hint": self.lane_hint,
            "system_kernel": self.system_kernel,
            "sections": [section.to_dict() for section in self.sections],
            "response_schema": _thaw_json(self.response_schema),
            "memory_ids": list(self.memory_ids),
            "tool_schema_refs": list(self.tool_schema_refs),
            "provider_id": self.provider_id,
            "account_refs": list(self.account_refs),
            "connection_revisions": dict(self.connection_revisions),
            "experience_revision": self.experience_revision,
            "authority_revision": self.authority_revision,
            "max_input_tokens": self.max_input_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "omission_counts": dict(self.omission_counts),
        }
        if include_digest:
            document["request_digest"] = self.request_digest
        return document


__all__ = [
    "McpInferenceInput",
    "McpInferencePurpose",
    "McpPromptSection",
    "PreparedMcpInference",
]
