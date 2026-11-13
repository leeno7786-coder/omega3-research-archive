"""Deterministically prepare MCP prompts and stop before any inference submission."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema

from guardian.external_ingress import harden
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.inference.contracts import (
    McpInferenceInput,
    McpInferencePurpose,
    McpPromptSection,
    PreparedMcpInference,
    _thaw_json,
)
from mcp_platform.inference.prompts import (
    MCP_SYSTEM_KERNEL,
    _constraint_field_catalog,
    _constraint_schema,
    purpose_directive_for,
    response_schema_for,
)

_PROMPT_VERSION = "mcp-prompt-v1"
_EXTERNAL_EVIDENCE_KIND = "__omega_mcp_kind__"
_EXTERNAL_EVIDENCE_ENTRIES = "__omega_mcp_entries__"
_EXTERNAL_EVIDENCE_ITEMS = "__omega_mcp_items__"
_EXTERNAL_EVIDENCE_KEY = "__omega_mcp_key__"
_EXTERNAL_EVIDENCE_VALUE = "__omega_mcp_value__"
_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_OPERATIONAL_KEYS = frozenset(
    {
        "credentialfile",
        "credentialpath",
        "clientsecretsfile",
        "clientsecretspath",
        "tokenfile",
        "tokencache",
        "tokencachepath",
        "vaultpath",
        "operationalfile",
        "executable",
        "executablepath",
        "command",
        "cwd",
        "env",
        "environment",
        "oauthconfig",
        "oauthfile",
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{3,}", re.IGNORECASE),
    re.compile(r"ya29\.[A-Za-z0-9._-]{8,}"),
    re.compile(r"1//[A-Za-z0-9_-]{12,}"),
    re.compile("GOCS" + r"PX-[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_CAMEL_ACRONYM_BOUNDARY_RE = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_WORD_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ASSIGNMENT_DELIMITER_RE = re.compile(r"[:=]")
_ASSIGNMENT_BOUNDARIES = "\r\n,;"
_SECRET_DIRECT_WORDS = frozenset(
    {
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "passphrase",
        "passphrases",
        "password",
        "passwords",
    }
)
_SECRET_TOKEN_WORDS = frozenset({"secret", "secrets", "token", "tokens"})
_SECRET_KEY_WORDS = frozenset({"key", "keys"})
_SECRET_SCOPE_WORDS = frozenset(
    {
        "access",
        "api",
        "auth",
        "authorization",
        "bearer",
        "client",
        "id",
        "oauth",
        "private",
        "provider",
        "refresh",
        "session",
        "signing",
        "vault",
    }
)
_SECRET_COLLECTION_WORDS = frozenset(
    {"collection", "collections", "list", "lists"}
)
_SAFE_SECRET_ASSIGNMENT_STATUSES = frozenset(
    {
        "absent",
        "masked",
        "none",
        "not set",
        "null",
        "omitted",
        "redacted",
        "unavailable",
        "unset",
    }
)
_MEMORY_FIELDS = frozenset({"memory_id", "summary", "salience"})
_MAX_OPTIONAL_RECORDS = 512
_TOOL_FIELDS = frozenset(
    {
        "name",
        "input_schema",
        "input_schema_sha256",
        "capability_tags",
        "risk_class",
        "provider_id",
        "account_refs",
    }
)
_SECTION_LAYOUT = (
    ("temporal_grounding", "trusted"),
    ("operator_goal", "untrusted"),
    ("verified_memory_evidence", "trusted"),
    ("signed_tool_projection", "trusted"),
    ("connection_and_authority_bounds", "trusted"),
    ("external_evidence", "untrusted"),
    ("task", "trusted"),
)
_JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_TOOL_REQUIRED_PURPOSES = frozenset(
    {
        McpInferencePurpose.PROVIDER_CAPABILITY_SELECTION,
        McpInferencePurpose.WORKFLOW_PROPOSAL,
        McpInferencePurpose.DIRECT_OPERATION_PROPOSAL,
        McpInferencePurpose.STANDING_OPERATION_PROPOSAL,
    }
)
_ADDITIVE_TOOL_SCHEMA_PURPOSES = frozenset(
    {
        McpInferencePurpose.PROVIDER_CAPABILITY_SELECTION,
        McpInferencePurpose.WORKFLOW_PROPOSAL,
        McpInferencePurpose.DIRECT_OPERATION_PROPOSAL,
        McpInferencePurpose.STANDING_OPERATION_PROPOSAL,
    }
)


def _normalized_key(key: str) -> str:
    normalized = unicodedata.normalize("NFKC", key).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _semantic_label_words(label: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", label)
    normalized = _CAMEL_ACRONYM_BOUNDARY_RE.sub(" ", normalized)
    normalized = _CAMEL_WORD_BOUNDARY_RE.sub(" ", normalized)
    separated = "".join(
        character.casefold()
        if character.isascii() and character.isalnum()
        else " "
        for character in normalized
    )
    return tuple(separated.split())


def _is_secret_label(label: str) -> bool:
    words = _semantic_label_words(label)
    if not words:
        return False
    word_set = set(words)
    if word_set.intersection(_SECRET_DIRECT_WORDS):
        return True
    scoped = bool(word_set.intersection(_SECRET_SCOPE_WORDS))
    if word_set.intersection(_SECRET_TOKEN_WORDS):
        collection_suffix = any(
            word in _SECRET_TOKEN_WORDS
            and any(
                suffix in _SECRET_COLLECTION_WORDS
                for suffix in words[index + 1 :]
            )
            for index, word in enumerate(words)
        )
        return len(words) == 1 or scoped or collection_suffix
    return bool(word_set.intersection(_SECRET_KEY_WORDS)) and scoped


def _is_explicit_safe_secret_status(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        " "
        if unicodedata.category(character) in {"Cc", "Cf"}
        or unicodedata.category(character).startswith("Z")
        else character
        for character in normalized
    )
    normalized = " ".join(normalized.casefold().split())
    if len(normalized) > 32:
        return False
    normalized = normalized.strip().rstrip(".!?,;").strip()
    if normalized[:1] in {"\"", "'"} and normalized[-1:] == normalized[:1]:
        normalized = normalized[1:-1].strip()
    return normalized in _SAFE_SECRET_ASSIGNMENT_STATUSES


def _assignment_segment_start(value: str, delimiter_index: int) -> int:
    return max(value.rfind(boundary, 0, delimiter_index) for boundary in _ASSIGNMENT_BOUNDARIES) + 1


def _contains_exposed_secret_assignment(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    for delimiter in _ASSIGNMENT_DELIMITER_RE.finditer(normalized):
        label = normalized[
            _assignment_segment_start(normalized, delimiter.start()) : delimiter.start()
        ]
        if not _is_secret_label(label):
            continue
        assigned = normalized[delimiter.end() :]
        if not _is_explicit_safe_secret_status(assigned):
            return True
    return False


def _scan_for_forbidden_material(value: Any) -> None:
    """Reject secrets and process/credential material without echoing content."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if _is_secret_label(key):
                raise ValueError("secret_bearing_mcp_inference")
            if normalized in _OPERATIONAL_KEYS or any(
                normalized.endswith(suffix)
                for suffix in ("credentialfile", "credentialpath", "secretspath")
            ):
                raise ValueError("operational_material_in_mcp_inference")
            _scan_for_forbidden_material(child)
        return
    if type(value) is tuple:
        for child in value:
            _scan_for_forbidden_material(child)
        return
    if type(value) is str:
        normalized = unicodedata.normalize("NFKC", value)
        if _contains_exposed_secret_assignment(normalized) or any(
            pattern.search(normalized) is not None for pattern in _SECRET_VALUE_PATTERNS
        ):
            raise ValueError("secret_bearing_mcp_inference")


def _stable_string(value: Any, *, error: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(error)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(error) from None
    return value


def _validate_memories(source: McpInferenceInput) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for raw in source.memory_evidence:
        if set(raw) != _MEMORY_FIELDS:
            raise ValueError("invalid_mcp_memory_evidence")
        memory_id = _stable_string(raw["memory_id"], error="invalid_mcp_memory_evidence")
        summary = _stable_string(raw["summary"], error="invalid_mcp_memory_evidence")
        salience = raw["salience"]
        if (
            type(salience) not in {int, float}
            or not math.isfinite(salience)
            or salience < 0
            or salience > 1
            or memory_id in seen_ids
        ):
            raise ValueError("invalid_mcp_memory_evidence")
        seen_ids.add(memory_id)
        parsed.append(
            {
                "memory_id": memory_id,
                "attributed_summary": harden(summary),
                "salience": salience,
            }
        )
    return sorted(parsed, key=lambda item: (-float(item["salience"]), str(item["memory_id"])))


def _string_list(value: Any, *, error: str, allow_empty: bool = True) -> list[str]:
    if type(value) is not tuple:
        raise ValueError(error)
    parsed = [_stable_string(item, error=error) for item in value]
    if (not allow_empty and not parsed) or len(parsed) != len(set(parsed)):
        raise ValueError(error)
    return parsed


def _contains_schema_reference(value: Any) -> bool:
    if type(value) is dict:
        return any(
            key in {"$ref", "$dynamicRef"} or _contains_schema_reference(child)
            for key, child in value.items()
        )
    if type(value) is list:
        return any(_contains_schema_reference(child) for child in value)
    return False


def _validate_tools(source: McpInferenceInput) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for raw in source.signed_tool_projection:
        if set(raw) != _TOOL_FIELDS:
            raise ValueError("invalid_mcp_signed_tool_projection")
        name = _stable_string(raw["name"], error="invalid_mcp_signed_tool_projection")
        digest = _stable_string(
            raw["input_schema_sha256"], error="invalid_mcp_signed_tool_projection"
        )
        provider_id = _stable_string(
            raw["provider_id"], error="invalid_mcp_signed_tool_projection"
        )
        if _HEX_RE.fullmatch(digest) is None or name in seen_names:
            raise ValueError("invalid_mcp_signed_tool_projection")
        raw_schema = raw["input_schema"]
        if not isinstance(raw_schema, Mapping):
            raise ValueError("invalid_mcp_signed_tool_projection")
        schema = _thaw_json(raw_schema)
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except (jsonschema.SchemaError, TypeError, ValueError):
            raise ValueError("invalid_mcp_signed_tool_projection") from None
        if (
            _contains_schema_reference(schema)
            or sha256_hex(canonical_bytes(schema)) != digest
        ):
            raise ValueError("invalid_mcp_signed_tool_projection")
        capability_tags = _string_list(
            raw["capability_tags"], error="invalid_mcp_signed_tool_projection"
        )
        account_refs = _string_list(
            raw["account_refs"],
            error="invalid_mcp_signed_tool_projection",
            allow_empty=False,
        )
        if not set(account_refs).issubset(source.account_refs):
            raise ValueError("invalid_mcp_signed_tool_projection")
        risk_class = _stable_string(
            raw["risk_class"], error="invalid_mcp_signed_tool_projection"
        )
        seen_names.add(name)
        parsed.append(
            {
                "name": name,
                "input_schema": schema,
                "input_schema_sha256": digest,
                "capability_tags": capability_tags,
                "risk_class": risk_class,
                "provider_id": provider_id,
                "account_refs": sorted(account_refs),
                "constraint_field_catalog": list(
                    _constraint_field_catalog(schema)
                ),
            }
        )
    return sorted(parsed, key=lambda item: str(item["name"]))


def _harden_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        entries: list[dict[str, Any]] = []
        if any(type(key) is not str for key in value):
            raise ValueError("invalid_mcp_external_evidence")
        for key in sorted(value):
            child = value[key]
            entries.append(
                {
                    _EXTERNAL_EVIDENCE_KEY: harden(key),
                    _EXTERNAL_EVIDENCE_VALUE: _harden_json(child),
                }
            )
        return {
            _EXTERNAL_EVIDENCE_KIND: "mapping",
            _EXTERNAL_EVIDENCE_ENTRIES: entries,
        }
    if type(value) in {list, tuple}:
        return {
            _EXTERNAL_EVIDENCE_KIND: "sequence",
            _EXTERNAL_EVIDENCE_ITEMS: [_harden_json(child) for child in value],
        }
    if type(value) is str:
        return harden(value)
    return value


def _sorted_external_evidence(source: McpInferenceInput) -> list[Any]:
    try:
        evidence = [_thaw_json(item) for item in source.untrusted_evidence]
        evidence.sort(key=canonical_bytes)
        return [_harden_json(item) for item in evidence]
    except Exception:
        raise ValueError("invalid_mcp_external_evidence") from None


def _json_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _signed_tool_prompt_record(tool: dict[str, object]) -> dict[str, object]:
    return {
        "name": tool["name"],
        "input_schema": tool["input_schema"],
        "input_schema_sha256": tool["input_schema_sha256"],
        "capability_tags": tool["capability_tags"],
        "risk_class": tool["risk_class"],
        "constraint_field_catalog": tool["constraint_field_catalog"],
    }


def _provider_constraint_catalog_record(
    provider_id: str,
    constraint_field_catalog: set[str] | tuple[str, ...],
) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "constraint_field_catalog": sorted(constraint_field_catalog),
    }


def _provider_constraint_catalog_records(
    tools: list[dict[str, object]],
) -> list[dict[str, object]]:
    catalogs: dict[str, set[str]] = {}
    for tool in tools:
        catalogs.setdefault(str(tool["provider_id"]), set()).update(
            str(path) for path in tool["constraint_field_catalog"]
        )
    return [
        _provider_constraint_catalog_record(provider_id, catalogs[provider_id])
        for provider_id in sorted(catalogs)
    ]


def _signed_tool_projection_content(
    purpose: McpInferencePurpose,
    tools: list[dict[str, object]],
) -> object:
    records = [_signed_tool_prompt_record(tool) for tool in tools]
    if purpose is McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL:
        return {
            "tools": records,
            "provider_constraint_catalogs": _provider_constraint_catalog_records(
                tools
            ),
        }
    return records


def _tool_authority_record(tool: dict[str, object]) -> dict[str, object]:
    return {
        "name": tool["name"],
        "provider_id": tool["provider_id"],
        "account_refs": tool["account_refs"],
    }


def _closed_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _evidence_schema(memory_ids: list[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "array",
        "uniqueItems": True,
    }
    if memory_ids:
        schema["items"] = {"enum": memory_ids}
    else:
        schema["items"] = False
        schema["maxItems"] = 0
    return schema


def _response_evidence_properties(
    schema: dict[str, Any],
    purpose: McpInferencePurpose,
) -> list[dict[str, Any]]:
    if purpose in {
        McpInferencePurpose.DIRECT_OPERATION_PROPOSAL,
        McpInferencePurpose.STANDING_OPERATION_PROPOSAL,
        McpInferencePurpose.PROVIDER_CAPABILITY_SELECTION,
    }:
        return [branch["properties"] for branch in schema["oneOf"]]
    return [schema["properties"]]


def _bind_response_evidence_schema(
    schema: dict[str, Any],
    purpose: McpInferencePurpose,
    memory_ids: list[str],
) -> None:
    for properties in _response_evidence_properties(schema, purpose):
        properties["evidence_memory_ids"] = _evidence_schema(memory_ids)


def _operation_response_schema(
    base: dict[str, Any],
    tools: list[dict[str, object]],
) -> dict[str, Any]:
    branches: list[dict[str, Any]] = []
    for tool in tools:
        properties = _thaw_json(base["properties"])
        properties["tool_name"] = {"const": tool["name"]}
        properties["arguments"] = _thaw_json(tool["input_schema"])
        field_count = len(tool["constraint_field_catalog"])
        properties["source_constraints"] = _constraint_schema(field_count)
        properties["destination_constraints"] = _constraint_schema(field_count)
        branches.append(_closed_schema(properties))
    schema = {
        "$schema": _JSON_SCHEMA_DRAFT,
        "type": "object",
        "oneOf": branches,
    }
    if "$defs" in base:
        schema["$defs"] = _thaw_json(base["$defs"])
    return schema


def _provider_selection_response_schema(
    base: dict[str, Any],
    tools: list[dict[str, object]],
) -> dict[str, Any]:
    branches: list[dict[str, Any]] = []
    for tool in tools:
        for account_ref in tool["account_refs"]:
            properties = _thaw_json(base["properties"])
            properties["provider_id"] = {"const": tool["provider_id"]}
            properties["account_ref"] = {"const": account_ref}
            properties["tool_name"] = {"const": tool["name"]}
            properties["tool_schema_sha256"] = {
                "const": tool["input_schema_sha256"]
            }
            branches.append(_closed_schema(properties))
    return {
        "$schema": _JSON_SCHEMA_DRAFT,
        "type": "object",
        "oneOf": branches,
    }


def _workflow_response_schema(
    base: dict[str, Any],
    tools: list[dict[str, object]],
) -> dict[str, Any]:
    schema = _thaw_json(base)
    step_base = schema["properties"]["steps"]["items"]
    schema["properties"]["source_constraints"] = _constraint_schema(0)
    schema["properties"]["destination_constraints"] = _constraint_schema(0)
    branches: list[dict[str, Any]] = []
    for tool in tools:
        for account_ref in tool["account_refs"]:
            properties = _thaw_json(step_base["properties"])
            properties["provider_id"] = {"const": tool["provider_id"]}
            properties["account_ref"] = {"const": account_ref}
            properties["tool_name"] = {"const": tool["name"]}
            properties["arguments"] = _thaw_json(tool["input_schema"])
            field_count = len(tool["constraint_field_catalog"])
            properties["source_constraints"] = _constraint_schema(field_count)
            properties["destination_constraints"] = _constraint_schema(field_count)
            branches.append(_closed_schema(properties))
    schema["$schema"] = _JSON_SCHEMA_DRAFT
    schema["properties"]["steps"]["items"] = {"oneOf": branches}
    return schema


def _ownership_branch(
    provider_field: str,
    account_field: str,
    constraint_field: str,
    provider_id: str,
    account_refs: list[str],
    field_count: int,
) -> dict[str, Any]:
    return {
        "properties": {
            provider_field: {"const": provider_id},
            account_field: {"enum": account_refs},
            constraint_field: _constraint_schema(field_count),
        },
        "required": [provider_field, account_field, constraint_field],
    }


def _distinct_provider_rule(provider_id: str) -> dict[str, Any]:
    return {
        "if": {
            "properties": {"source_provider_id": {"const": provider_id}},
            "required": ["source_provider_id"],
        },
        "then": {
            "properties": {
                "destination_provider_id": {"not": {"const": provider_id}}
            }
        },
    }


def _cross_provider_response_schema(
    base: dict[str, Any],
    tools: list[dict[str, object]],
) -> dict[str, Any]:
    ownership: dict[str, set[str]] = {}
    for tool in tools:
        provider_id = str(tool["provider_id"])
        ownership.setdefault(provider_id, set()).update(tool["account_refs"])
    catalog_records = _provider_constraint_catalog_records(tools)
    catalogs = {
        str(record["provider_id"]): tuple(record["constraint_field_catalog"])
        for record in catalog_records
    }
    providers = sorted(ownership)
    accounts = sorted({account for values in ownership.values() for account in values})
    schema = _thaw_json(base)
    schema["$schema"] = _JSON_SCHEMA_DRAFT
    properties = schema["properties"]
    properties["source_provider_id"] = {"type": "string", "enum": providers}
    properties["destination_provider_id"] = {"type": "string", "enum": providers}
    properties["source_account_ref"] = {"type": "string", "enum": accounts}
    properties["destination_account_ref"] = {"type": "string", "enum": accounts}
    properties["transformation"] = _closed_schema(
        {
            "description": {"type": "string"},
            "field_mappings": {
                "type": "array",
                "minItems": 1,
                "items": _closed_schema(
                    {
                        "source_field": {"type": "string"},
                        "destination_field": {"type": "string"},
                        "transform": {"type": "string"},
                    }
                ),
            },
            "unmapped_field_policy": {"type": "string"},
        }
    )
    source_ownership = {
        "oneOf": [
            _ownership_branch(
                "source_provider_id",
                "source_account_ref",
                "source_constraints",
                provider_id,
                sorted(ownership[provider_id]),
                len(catalogs[provider_id]),
            )
            for provider_id in providers
        ]
    }
    destination_ownership = {
        "oneOf": [
            _ownership_branch(
                "destination_provider_id",
                "destination_account_ref",
                "destination_constraints",
                provider_id,
                sorted(ownership[provider_id]),
                len(catalogs[provider_id]),
            )
            for provider_id in providers
        ]
    }
    distinct_provider_rules = [
        _distinct_provider_rule(provider_id) for provider_id in providers
    ]
    schema["allOf"] = [
        source_ownership,
        destination_ownership,
        *distinct_provider_rules,
    ]
    return schema


def _bound_response_schema(
    source: McpInferenceInput,
    tools: list[dict[str, object]],
    memory_ids: list[str],
) -> dict[str, Any]:
    schema = _thaw_json(response_schema_for(source.purpose))
    if source.purpose in {
        McpInferencePurpose.DIRECT_OPERATION_PROPOSAL,
        McpInferencePurpose.STANDING_OPERATION_PROPOSAL,
    }:
        schema = _operation_response_schema(schema, tools)
    elif source.purpose is McpInferencePurpose.PROVIDER_CAPABILITY_SELECTION:
        schema = _provider_selection_response_schema(schema, tools)
    elif source.purpose is McpInferencePurpose.WORKFLOW_PROPOSAL:
        schema = _workflow_response_schema(schema, tools)
    if source.purpose is McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL:
        schema = _cross_provider_response_schema(schema, tools)
    schema.setdefault("$schema", _JSON_SCHEMA_DRAFT)
    _bind_response_evidence_schema(schema, source.purpose, memory_ids)
    return schema


def _response_evidence_schema_count(
    schema: dict[str, Any],
    purpose: McpInferencePurpose,
) -> int:
    return sum(
        "evidence_memory_ids" in properties
        for properties in _response_evidence_properties(schema, purpose)
    )


def _canonical_character_count(value: Any) -> int:
    return len(canonical_bytes(value).decode("utf-8"))


_EMPTY_JSON_STRING_CHARACTERS = _canonical_character_count("")
_EMPTY_CONSTRAINT_SCHEMA_CHARACTERS = _canonical_character_count(
    _constraint_schema(0)
)
_EMPTY_SOURCE_OWNERSHIP_BRANCH_CHARACTERS = _canonical_character_count(
    _ownership_branch(
        "source_provider_id",
        "source_account_ref",
        "source_constraints",
        "",
        [],
        0,
    )
)
_EMPTY_DESTINATION_OWNERSHIP_BRANCH_CHARACTERS = _canonical_character_count(
    _ownership_branch(
        "destination_provider_id",
        "destination_account_ref",
        "destination_constraints",
        "",
        [],
        0,
    )
)
_EMPTY_DISTINCT_PROVIDER_RULE_CHARACTERS = _canonical_character_count(
    _distinct_provider_rule("")
)
_EMPTY_PROVIDER_CONSTRAINT_CATALOG_RECORD_CHARACTERS = _canonical_character_count(
    _provider_constraint_catalog_record("", ())
)


def _ownership_branch_characters(
    empty_branch_characters: int,
    provider_characters: int,
    account_characters: tuple[int, ...],
    constraint_schema_characters: int,
) -> int:
    return (
        empty_branch_characters
        + provider_characters
        - _EMPTY_JSON_STRING_CHARACTERS
        + sum(account_characters)
        + max(0, len(account_characters) - 1)
        + constraint_schema_characters
        - _EMPTY_CONSTRAINT_SCHEMA_CHARACTERS
    )


def _distinct_provider_rule_characters(provider_characters: int) -> int:
    return _EMPTY_DISTINCT_PROVIDER_RULE_CHARACTERS + 2 * (
        provider_characters - _EMPTY_JSON_STRING_CHARACTERS
    )


def _provider_constraint_catalog_record_characters(
    provider_characters: int,
    catalog_path_characters: tuple[int, ...],
) -> int:
    return (
        _EMPTY_PROVIDER_CONSTRAINT_CATALOG_RECORD_CHARACTERS
        + provider_characters
        - _EMPTY_JSON_STRING_CHARACTERS
        + sum(catalog_path_characters)
        + max(0, len(catalog_path_characters) - 1)
    )


@dataclass(frozen=True)
class _ToolBudgetProjection:
    signed_prompt_characters: int
    authority_prompt_characters: int
    schema_delta_without_memory: int
    added_evidence_schema_count: int
    provider_id: str
    provider_characters: int
    account_refs: tuple[str, ...]
    account_characters: tuple[int, ...]
    constraint_field_catalog: tuple[str, ...]
    constraint_field_characters: tuple[int, ...]


def _tool_budget_projections(
    source: McpInferenceInput,
    tools: list[dict[str, object]],
) -> list[_ToolBudgetProjection]:
    zero_schema = _bound_response_schema(source, [], [])
    zero_schema_characters = _canonical_character_count(zero_schema)
    zero_evidence_count = _response_evidence_schema_count(zero_schema, source.purpose)
    projections: list[_ToolBudgetProjection] = []
    cross_provider = (
        source.purpose
        is McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL
    )
    identity_characters: dict[str, int] = {}

    def identity_size(value: str) -> int:
        if value not in identity_characters:
            identity_characters[value] = _canonical_character_count(value)
        return identity_characters[value]

    for tool in tools:
        constraint_field_catalog = tuple(
            str(path) for path in tool["constraint_field_catalog"]
        )
        if cross_provider:
            schema_delta_without_memory = 0
            added_evidence_schema_count = 0
            provider_id = str(tool["provider_id"])
            account_refs = tuple(str(item) for item in tool["account_refs"])
            provider_characters = identity_size(provider_id)
            account_characters = tuple(identity_size(item) for item in account_refs)
            constraint_field_characters = tuple(
                identity_size(path) for path in constraint_field_catalog
            )
        else:
            one_schema = _bound_response_schema(source, [tool], [])
            schema_delta_without_memory = (
                _canonical_character_count(one_schema) - zero_schema_characters
            )
            added_evidence_schema_count = (
                _response_evidence_schema_count(one_schema, source.purpose)
                - zero_evidence_count
            )
            provider_id = ""
            account_refs = ()
            provider_characters = 0
            account_characters = ()
            constraint_field_characters = ()
        projections.append(
            _ToolBudgetProjection(
                signed_prompt_characters=_canonical_character_count(
                    _signed_tool_prompt_record(tool)
                ),
                authority_prompt_characters=_canonical_character_count(
                    _tool_authority_record(tool)
                ),
                schema_delta_without_memory=schema_delta_without_memory,
                added_evidence_schema_count=added_evidence_schema_count,
                provider_id=provider_id,
                provider_characters=provider_characters,
                account_refs=account_refs,
                account_characters=account_characters,
                constraint_field_catalog=constraint_field_catalog,
                constraint_field_characters=constraint_field_characters,
            )
        )
    return projections


def _sections(
    source: McpInferenceInput,
    memories: list[dict[str, object]],
    tools: list[dict[str, object]],
    evidence: list[Any],
) -> tuple[McpPromptSection, ...]:
    signed_tools = _signed_tool_projection_content(source.purpose, tools)
    tool_provider_accounts = [_tool_authority_record(tool) for tool in tools]
    authority = {
        "provider_id": source.provider_id,
        "account_refs": sorted(source.account_refs),
        "connection_revisions": dict(source.connection_revisions),
        "experience_revision": source.experience_revision,
        "authority_revision": source.authority_revision,
        "authority_bounds": _thaw_json(source.authority_bounds),
        "tool_provider_accounts": tool_provider_accounts,
    }
    contents = (
        source.temporal_line,
        harden(source.operator_goal),
        _json_text(memories),
        _json_text(signed_tools),
        _json_text(authority),
        _json_text(evidence),
        purpose_directive_for(source.purpose),
    )
    return tuple(
        McpPromptSection(label=label, trust=trust, content=content)
        for (label, trust), content in zip(_SECTION_LAYOUT, contents, strict=True)
    )


def _estimate_tokens(
    sections: tuple[McpPromptSection, ...],
    response_schema: dict[str, Any],
) -> int:
    return math.ceil(_rendered_character_count(sections, response_schema) / 4)


def _rendered_character_count(
    sections: tuple[McpPromptSection, ...],
    response_schema: dict[str, Any],
) -> int:
    rendered = [MCP_SYSTEM_KERNEL]
    rendered.extend(
        f"[{section.label}|{section.trust}]\n{section.content}" for section in sections
    )
    rendered.append(_json_text(response_schema))
    return len("\n".join(rendered))


class _IncrementalBudget:
    """Monotonic character budget with one canonical visit per candidate field."""

    def __init__(
        self,
        source: McpInferenceInput,
        selected_tools: list[dict[str, object]],
    ) -> None:
        response_schema = _bound_response_schema(source, selected_tools, [])
        self._characters = _rendered_character_count(
            _sections(source, [], selected_tools, []), response_schema
        )
        self._maximum_characters = source.max_input_tokens * 4
        self._purpose = source.purpose
        self._memory_count = 0
        self._tool_count = len(selected_tools)
        self._evidence_count = 0
        self._evidence_schema_count = _response_evidence_schema_count(
            response_schema, source.purpose
        )
        self._evidence_schema_extra = 0
        self._cross_provider_ownership: dict[str, set[str]] = {}
        self._cross_provider_accounts: set[str] = set()
        self._cross_provider_catalogs: dict[str, set[str]] = {}
        self._constraint_schema_character_cache = {
            0: _EMPTY_CONSTRAINT_SCHEMA_CHARACTERS
        }
        if (
            source.purpose
            is McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL
        ):
            for tool in selected_tools:
                provider_id = str(tool["provider_id"])
                account_refs = {str(item) for item in tool["account_refs"]}
                self._cross_provider_ownership.setdefault(provider_id, set()).update(
                    account_refs
                )
                self._cross_provider_accounts.update(account_refs)
                self._cross_provider_catalogs.setdefault(provider_id, set()).update(
                    str(path) for path in tool["constraint_field_catalog"]
                )

    @property
    def within_limit(self) -> bool:
        return self._characters <= self._maximum_characters

    def _try_add(self, characters: int) -> bool:
        if characters < 0:
            raise ValueError("invalid_mcp_budget_projection")
        if self._characters + characters > self._maximum_characters:
            return False
        self._characters += characters
        return True

    def _constraint_schema_characters(self, field_count: int) -> int:
        if field_count not in self._constraint_schema_character_cache:
            self._constraint_schema_character_cache[field_count] = (
                _canonical_character_count(_constraint_schema(field_count))
            )
        return self._constraint_schema_character_cache[field_count]

    def try_add_memory(self, memory: dict[str, object]) -> bool:
        record_characters = _canonical_character_count(memory)
        memory_id_characters = _canonical_character_count(str(memory["memory_id"]))
        list_delta = record_characters + int(self._memory_count > 0)
        if self._memory_count:
            evidence_schema_delta = memory_id_characters + 1
        else:
            evidence_schema_delta = (
                _canonical_character_count(_evidence_schema([str(memory["memory_id"])]))
                - _canonical_character_count(_evidence_schema([]))
            )
        delta = list_delta + self._evidence_schema_count * evidence_schema_delta
        if not self._try_add(delta):
            return False
        self._memory_count += 1
        self._evidence_schema_extra += evidence_schema_delta
        return True

    def try_add_tool(self, projection: _ToolBudgetProjection) -> bool:
        prompt_delta = (
            projection.signed_prompt_characters
            + projection.authority_prompt_characters
            + 2 * int(self._tool_count > 0)
        )
        schema_delta = 0
        if self._purpose in _ADDITIVE_TOOL_SCHEMA_PURPOSES:
            schema_delta = (
                projection.schema_delta_without_memory
                + int(self._tool_count > 0)
                + projection.added_evidence_schema_count
                * self._evidence_schema_extra
            )
        elif (
            self._purpose
            is McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL
        ):
            provider_accounts = self._cross_provider_ownership.get(
                projection.provider_id
            )
            account_sizes = dict(
                zip(
                    projection.account_refs,
                    projection.account_characters,
                    strict=True,
                )
            )
            new_global_accounts = set(projection.account_refs).difference(
                self._cross_provider_accounts
            )
            old_catalog = self._cross_provider_catalogs.get(
                projection.provider_id, set()
            )
            new_catalog = old_catalog.union(projection.constraint_field_catalog)
            catalog_path_sizes = dict(
                zip(
                    projection.constraint_field_catalog,
                    projection.constraint_field_characters,
                    strict=True,
                )
            )
            if projection.provider_id not in self._cross_provider_catalogs:
                prompt_delta += (
                    _provider_constraint_catalog_record_characters(
                        projection.provider_characters,
                        projection.constraint_field_characters,
                    )
                    + int(bool(self._cross_provider_catalogs))
                )
            else:
                added_paths = new_catalog.difference(old_catalog)
                prompt_delta += sum(
                    catalog_path_sizes[path] for path in added_paths
                ) + max(0, len(added_paths) - int(not old_catalog))
            constraint_schema_characters = self._constraint_schema_characters(
                len(new_catalog)
            )
            schema_delta = 2 * sum(
                account_sizes[account] + 1 for account in new_global_accounts
            )
            if provider_accounts is None:
                schema_delta += 2 * (projection.provider_characters + 1)
                schema_delta += _ownership_branch_characters(
                    _EMPTY_SOURCE_OWNERSHIP_BRANCH_CHARACTERS,
                    projection.provider_characters,
                    projection.account_characters,
                    constraint_schema_characters,
                ) + 1
                schema_delta += _ownership_branch_characters(
                    _EMPTY_DESTINATION_OWNERSHIP_BRANCH_CHARACTERS,
                    projection.provider_characters,
                    projection.account_characters,
                    constraint_schema_characters,
                ) + 1
                schema_delta += _distinct_provider_rule_characters(
                    projection.provider_characters
                ) + 1
            else:
                new_provider_accounts = set(projection.account_refs).difference(
                    provider_accounts
                )
                schema_delta += 2 * sum(
                    account_sizes[account] + 1
                    for account in new_provider_accounts
                )
                old_constraint_characters = self._constraint_schema_characters(
                    len(old_catalog)
                )
                schema_delta += 2 * (
                    constraint_schema_characters - old_constraint_characters
                )
        if not self._try_add(prompt_delta + schema_delta):
            return False
        self._tool_count += 1
        self._evidence_schema_count += projection.added_evidence_schema_count
        if (
            self._purpose
            is McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL
        ):
            accounts = set(projection.account_refs)
            self._cross_provider_ownership.setdefault(
                projection.provider_id, set()
            ).update(accounts)
            self._cross_provider_accounts.update(accounts)
            self._cross_provider_catalogs.setdefault(
                projection.provider_id, set()
            ).update(projection.constraint_field_catalog)
        return True

    def try_add_evidence(self, record: Any) -> bool:
        delta = _canonical_character_count(record) + int(self._evidence_count > 0)
        if not self._try_add(delta):
            return False
        self._evidence_count += 1
        return True


def prepare_mcp_inference(source: McpInferenceInput) -> PreparedMcpInference:
    """Prepare one deterministic request without submitting it anywhere."""

    if type(source) is not McpInferenceInput:
        raise ValueError("invalid_mcp_inference_input")
    if (
        len(source.memory_evidence)
        + len(source.signed_tool_projection)
        + len(source.untrusted_evidence)
        > _MAX_OPTIONAL_RECORDS
    ):
        raise ValueError("mcp_inference_record_limit_exceeded")
    _scan_for_forbidden_material(
        {
            "temporal_line": source.temporal_line,
            "operator_goal": source.operator_goal,
            "memory_evidence": source.memory_evidence,
            "signed_tool_projection": source.signed_tool_projection,
            "provider_id": source.provider_id,
            "account_refs": source.account_refs,
            "connection_revisions": source.connection_revisions,
            "experience_revision": source.experience_revision,
            "authority_revision": source.authority_revision,
            "authority_bounds": source.authority_bounds,
            "untrusted_evidence": source.untrusted_evidence,
        }
    )
    memories = _validate_memories(source)
    tools = _validate_tools(source)
    evidence = _sorted_external_evidence(source)
    tool_projections = _tool_budget_projections(source, tools)
    if source.purpose in _TOOL_REQUIRED_PURPOSES and not tools:
        raise ValueError("no_applicable_signed_tool")
    if (
        source.purpose is McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL
        and len({str(tool["provider_id"]) for tool in tools}) < 2
    ):
        raise ValueError("insufficient_cross_provider_ownership")

    selected_memories: list[dict[str, object]] = []
    selected_tools: list[dict[str, object]] = []
    selected_tool_names: set[str] = set()
    selected_evidence: list[Any] = []
    omissions = {"memory": 0, "tools": 0, "external_evidence": 0}
    if source.purpose is McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL:
        reserved_providers: set[str] = set()
        for tool in tools:
            provider_id = str(tool["provider_id"])
            if provider_id not in reserved_providers:
                selected_tools.append(tool)
                selected_tool_names.add(str(tool["name"]))
                reserved_providers.add(provider_id)
                if len(reserved_providers) == 2:
                    break
        budget = _IncrementalBudget(source, selected_tools)
        if not budget.within_limit:
            raise ValueError("no_applicable_cross_provider_ownership_within_budget")
    elif source.purpose in _TOOL_REQUIRED_PURPOSES:
        budget = _IncrementalBudget(source, [])
        for tool, projection in zip(tools, tool_projections, strict=True):
            if budget.try_add_tool(projection):
                selected_tools.append(tool)
                selected_tool_names.add(str(tool["name"]))
                break
        if not selected_tools:
            raise ValueError("no_applicable_signed_tool_within_budget")
    else:
        budget = _IncrementalBudget(source, [])
        if not budget.within_limit:
            raise ValueError("mcp_inference_budget_too_small")

    for memory in memories:
        if budget.try_add_memory(memory):
            selected_memories.append(memory)
        else:
            omissions["memory"] += 1
    for tool, projection in zip(tools, tool_projections, strict=True):
        tool_name = str(tool["name"])
        if tool_name in selected_tool_names:
            continue
        if budget.try_add_tool(projection):
            selected_tools.append(tool)
            selected_tool_names.add(tool_name)
        else:
            omissions["tools"] += 1
    for record in evidence:
        if budget.try_add_evidence(record):
            selected_evidence.append(record)
        else:
            omissions["external_evidence"] += 1

    selected_tools.sort(key=lambda item: str(item["name"]))
    sections = _sections(
        source, selected_memories, selected_tools, selected_evidence
    )
    response_schema = _bound_response_schema(
        source,
        selected_tools,
        [str(memory["memory_id"]) for memory in selected_memories],
    )
    estimated = _estimate_tokens(sections, response_schema)
    if estimated > source.max_input_tokens:
        raise ValueError("invalid_mcp_budget_projection")
    base: dict[str, Any] = {
        "prompt_version": _PROMPT_VERSION,
        "purpose": source.purpose.value,
        "lane_hint": "mcp",
        "system_kernel": MCP_SYSTEM_KERNEL,
        "sections": [section.to_dict() for section in sections],
        "response_schema": response_schema,
        "memory_ids": [str(item["memory_id"]) for item in selected_memories],
        "tool_schema_refs": [
            f"{tool['name']}:{tool['input_schema_sha256']}" for tool in selected_tools
        ],
        "provider_id": source.provider_id,
        "account_refs": sorted(source.account_refs),
        "connection_revisions": dict(source.connection_revisions),
        "experience_revision": source.experience_revision,
        "authority_revision": source.authority_revision,
        "max_input_tokens": source.max_input_tokens,
        "estimated_input_tokens": estimated,
        "omission_counts": omissions,
    }
    request_id = f"mcp-inf-{sha256_hex(canonical_bytes(base))[:24]}"
    request_digest = sha256_hex(
        canonical_bytes({"request_id": request_id, **base})
    )
    return PreparedMcpInference(
        request_id=request_id,
        prompt_version=_PROMPT_VERSION,
        purpose=source.purpose,
        lane_hint="mcp",
        system_kernel=MCP_SYSTEM_KERNEL,
        sections=sections,
        response_schema=response_schema,
        memory_ids=tuple(base["memory_ids"]),
        tool_schema_refs=tuple(base["tool_schema_refs"]),
        provider_id=source.provider_id,
        account_refs=tuple(sorted(source.account_refs)),
        connection_revisions=source.connection_revisions,
        experience_revision=source.experience_revision,
        authority_revision=source.authority_revision,
        max_input_tokens=source.max_input_tokens,
        estimated_input_tokens=estimated,
        omission_counts=omissions,
        request_digest=request_digest,
    )


__all__ = ["prepare_mcp_inference"]
