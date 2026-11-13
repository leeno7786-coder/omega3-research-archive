from __future__ import annotations

from collections.abc import Mapping

import pytest
from jsonschema import Draft202012Validator

from mcp_platform.inference import McpInferencePurpose, response_schema_for
from mcp_platform.inference.prompts import MCP_SYSTEM_KERNEL, purpose_directive_for


OPERATION_PROPERTIES = {
    "tool_name",
    "arguments",
    "source_constraints",
    "destination_constraints",
    "expected_provider_revisions",
    "rationale",
    "evidence_memory_ids",
}
FORBIDDEN_AUTHORITY_PROPERTIES = {
    "package_identity",
    "connection_revision",
    "authority",
    "authority_id",
    "authority_revision",
    "approval",
    "approval_digest",
    "grant_id",
    "idempotency_key",
    "provider_token",
    "switch",
    "switch_enabled",
    "install",
    "update",
    "dispatch",
    "active",
    "schedule_owner",
}


def _property_names(schema: object) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in schema.values():
            names.update(_property_names(value))
    elif isinstance(schema, (list, tuple)):
        for value in schema:
            names.update(_property_names(value))
    return names


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(child) for child in value]
    return value


def test_every_prompt_family_has_one_closed_required_response_schema() -> None:
    for purpose in McpInferencePurpose:
        schema = response_schema_for(purpose)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_all_nine_response_schemas_are_draft_2020_12_valid() -> None:
    for purpose in McpInferencePurpose:
        schema = _plain_json(response_schema_for(purpose))
        Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    "purpose",
    [
        McpInferencePurpose.DIRECT_OPERATION_PROPOSAL,
        McpInferencePurpose.STANDING_OPERATION_PROPOSAL,
    ],
)
def test_static_operation_contract_uses_only_closed_typed_constraint_and_revision_shapes(
    purpose: McpInferencePurpose,
) -> None:
    schema = response_schema_for(purpose)
    for name in ("source_constraints", "destination_constraints"):
        constraints = schema["properties"][name]
        assert constraints["additionalProperties"] is False
        assert set(constraints["properties"]) == {"match", "clauses"}
        clause = constraints["properties"]["clauses"]["items"]
        assert clause["additionalProperties"] is False
        assert set(clause["properties"]) == {"field_index", "operator", "value"}

    revisions = schema["properties"]["expected_provider_revisions"]
    assert revisions["type"] == "array"
    assert revisions["items"]["additionalProperties"] is False
    assert set(revisions["items"]["properties"]) == {"object_ref", "revision"}


def test_response_schemas_are_detached_immutable_contracts() -> None:
    first = response_schema_for(McpInferencePurpose.GOAL_INTERPRETATION)
    with pytest.raises(TypeError):
        first["type"] = "array"  # type: ignore[index]
    second = response_schema_for(McpInferencePurpose.GOAL_INTERPRETATION)
    assert second["type"] == "object"


def test_stable_kernel_denies_authority_and_execution() -> None:
    kernel = MCP_SYSTEM_KERNEL.lower()
    for required in (
        "untrusted evidence",
        "never enable external mcp",
        "never create authority",
        "never call a tool",
        "return only the requested json",
    ):
        assert required in kernel


def test_all_nine_purposes_have_distinct_stable_directives() -> None:
    directives = {
        purpose: purpose_directive_for(purpose) for purpose in McpInferencePurpose
    }
    assert len(set(directives.values())) == 9
    assert all(directive.strip() == directive for directive in directives.values())
    assert all("call" not in directive.casefold() for directive in directives.values())


@pytest.mark.parametrize(
    "purpose",
    [
        McpInferencePurpose.DIRECT_OPERATION_PROPOSAL,
        McpInferencePurpose.STANDING_OPERATION_PROPOSAL,
    ],
)
def test_operation_schemas_emit_only_non_authoritative_proposal_material(
    purpose: McpInferencePurpose,
) -> None:
    schema = response_schema_for(purpose)
    assert set(schema["properties"]) == OPERATION_PROPERTIES
    assert FORBIDDEN_AUTHORITY_PROPERTIES.isdisjoint(_property_names(schema))


def test_workflow_schema_cannot_emit_grants_approval_state_or_schedule_ownership() -> None:
    schema = response_schema_for(McpInferencePurpose.WORKFLOW_PROPOSAL)
    assert FORBIDDEN_AUTHORITY_PROPERTIES.isdisjoint(_property_names(schema))
    assert {"workflow_name", "objective", "steps"}.issubset(schema["properties"])


def test_cross_provider_schema_is_an_explicit_transformation_proposal() -> None:
    schema = response_schema_for(
        McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL
    )
    assert {
        "source_provider_id",
        "source_account_ref",
        "destination_provider_id",
        "destination_account_ref",
        "transformation",
    }.issubset(schema["properties"])
    assert {"tool_name", "arguments", "dispatch"}.isdisjoint(schema["properties"])
    assert FORBIDDEN_AUTHORITY_PROPERTIES.isdisjoint(_property_names(schema))


def test_cross_provider_directive_requires_distinct_provider_sides() -> None:
    directive = purpose_directive_for(
        McpInferencePurpose.CROSS_PROVIDER_TRANSFORMATION_PROPOSAL
    )
    assert "distinct" in directive.casefold()
    assert (
        "field_index indexes the selected provider's materialized "
        "constraint_field_catalog"
    ) in directive


def test_response_schema_rejects_non_enum_purpose() -> None:
    with pytest.raises(ValueError, match="purpose"):
        response_schema_for("goal_interpretation")  # type: ignore[arg-type]
