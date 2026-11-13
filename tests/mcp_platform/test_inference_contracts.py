from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mcp_platform.inference import (
    McpInferenceInput,
    McpInferencePurpose,
    PreparedMcpInference,
)


EXPECTED_PURPOSES = {
    "goal_interpretation",
    "adaptive_interview",
    "provider_capability_selection",
    "learning_boundary_recommendation",
    "workflow_proposal",
    "direct_operation_proposal",
    "standing_operation_proposal",
    "cross_provider_transformation_proposal",
    "user_facing_explanation",
}


def _valid_input() -> McpInferenceInput:
    return McpInferenceInput(
        purpose=McpInferencePurpose.GOAL_INTERPRETATION,
        temporal_line="Current time: 2026-08-11T15:00:00Z",
        operator_goal="Organize the quarterly planning work.",
        memory_evidence=[
            {
                "memory_id": "memory-1",
                "summary": "The operator prefers concise status updates.",
                "salience": 0.8,
            }
        ],
        signed_tool_projection=[
            {
                "name": "omega.fake.search",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "input_schema_sha256": "a" * 64,
                "capability_tags": ["mail.search"],
                "risk_class": "read_only",
                "provider_id": "provider.fake",
                "account_refs": ["account-1"],
            }
        ],
        provider_id="provider.fake",
        account_refs=["account-1"],
        connection_revisions={"connection-1": "b" * 64},
        experience_revision="c" * 64,
        authority_revision="d" * 64,
        authority_bounds={
            "capability_ceiling": ["mail.search"],
            "source_constraints": {"folders": ["inbox"]},
            "destination_constraints": {},
        },
        untrusted_evidence=[
            {"evidence_id": "evidence-1", "content": "Planning note"}
        ],
        max_input_tokens=4096,
    )


def test_purpose_contract_contains_exactly_the_nine_prompt_families() -> None:
    assert {purpose.value for purpose in McpInferencePurpose} == EXPECTED_PURPOSES


def test_prepared_contract_stops_at_the_exact_open_lane_fields() -> None:
    assert set(PreparedMcpInference.__dataclass_fields__) == {
        "request_id",
        "prompt_version",
        "purpose",
        "lane_hint",
        "system_kernel",
        "sections",
        "response_schema",
        "memory_ids",
        "tool_schema_refs",
        "provider_id",
        "account_refs",
        "connection_revisions",
        "experience_revision",
        "authority_revision",
        "max_input_tokens",
        "estimated_input_tokens",
        "omission_counts",
        "request_digest",
    }


def test_input_contract_owns_immutable_copies_of_nested_json() -> None:
    source = _valid_input()
    original = source.memory_evidence[0]["summary"]
    with pytest.raises(TypeError):
        source.memory_evidence[0]["summary"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        source.max_input_tokens = 2  # type: ignore[misc]
    assert source.memory_evidence[0]["summary"] == original


def test_input_contract_rejects_unknown_fields_by_constructor_shape() -> None:
    values = _valid_input().__dict__
    with pytest.raises(TypeError):
        McpInferenceInput(**values, endpoint="https://model.invalid")
