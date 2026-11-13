from __future__ import annotations

import math
from collections import UserDict

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.operation_contracts import (
    McpAuthoritySnapshot,
    McpConnectionSnapshot,
    McpOperationProposal,
    McpToolAuthority,
)
from mcp_platform.package_contracts import RiskClass


def valid_proposal(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "request_id": "request-1",
        "episode_id": "episode-1",
        "server_id": "omega.first_party.external",
        "server_version": "1.0.0",
        "artifact_sha256": "a" * 64,
        "provider_id": "provider.fake",
        "account_ref": "account-1",
        "connection_id": "connection-1",
        "connection_revision": "b" * 64,
        "tool_name": "omega.fake.search",
        "tool_schema_sha256": "c" * 64,
        "arguments": {"query": "quarterly plan"},
        "source_constraints": {"folders": ["inbox"]},
        "destination_constraints": {},
        "expected_provider_revisions": {},
        "authority_kind": "direct_turn",
        "authority_id": "authority-1",
        "authority_revision": "d" * 64,
        "plan_digest": "e" * 64,
        "candidate_id": "1" * 64,
        "plan_step_id": "step-1",
        "connection_authority_revision": "3" * 64,
        "idempotency_key": "idem-request-1",
        "evidence_memory_ids": ["memory-1"],
    }
    document.update(overrides)
    digest_document = dict(document)
    digest_document.pop("proposal_digest", None)
    document["proposal_digest"] = sha256_hex(canonical_bytes(digest_document))
    return document


def test_operation_proposal_round_trip_binds_every_authority_input() -> None:
    proposal = McpOperationProposal.from_dict(valid_proposal())

    assert proposal.proposal_digest == proposal.compute_digest()
    assert proposal.tool_name == "omega.fake.search"
    assert proposal.evidence_memory_ids == ("memory-1",)
    assert proposal.to_dict() == valid_proposal()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update({"unexpected": True}),
        lambda document: document.pop("request_id"),
        lambda document: document.update({"proposal_digest": "0" * 64}),
    ],
)
def test_operation_proposal_is_closed_and_digest_verified(mutation) -> None:
    document = valid_proposal()
    mutation(document)

    with pytest.raises(ValueError):
        McpOperationProposal.from_dict(document)


@pytest.mark.parametrize(
    "field",
    [
        "provider_token",
        "Provider-Token",
        "CLIENT.SECRET",
        "nested_vault_key",
        "authorization",
        "access-token",
        "refreshToken",
        "api_key",
        "private-key",
        "password",
    ],
)
def test_operation_proposal_rejects_secret_fields_at_any_depth(field: str) -> None:
    document = valid_proposal()
    document["arguments"] = {"nested": [{field: "secret-value"}]}
    digest_document = dict(document)
    digest_document.pop("proposal_digest")
    document["proposal_digest"] = sha256_hex(canonical_bytes(digest_document))

    with pytest.raises(ValueError, match="secret") as caught:
        McpOperationProposal.from_dict(document)

    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_operation_proposal_rejects_nonfinite_json(bad: float) -> None:
    document = valid_proposal()
    document["arguments"] = {"score": bad}

    with pytest.raises(ValueError, match="invalid_operation_proposal"):
        McpOperationProposal.from_dict(document)


def test_operation_proposal_rejects_cycles_boundedly() -> None:
    cyclic: dict[str, object] = {}
    cyclic["child"] = cyclic
    document = valid_proposal()
    document["arguments"] = cyclic

    with pytest.raises(ValueError, match="invalid_operation_proposal"):
        McpOperationProposal.from_dict(document)


def test_operation_proposal_rejects_python_tuples_that_are_not_json_arrays() -> None:
    document = valid_proposal(arguments={"query": ["one", "two"]})
    document["arguments"] = {"query": ("one", "two")}

    with pytest.raises(ValueError, match="invalid_operation_proposal"):
        McpOperationProposal.from_dict(document)


def test_operation_proposal_rejects_hostile_mapping_subclasses_without_invoking_them() -> None:
    class Hostile(UserDict):
        def items(self):
            raise AssertionError("hostile-provider-sentinel")

    document = valid_proposal()
    document["arguments"] = Hostile({"query": "safe"})

    with pytest.raises(ValueError, match="invalid_operation_proposal") as caught:
        McpOperationProposal.from_dict(document)

    assert "hostile-provider-sentinel" not in str(caught.value)


def test_operation_proposal_owns_an_immutable_copy() -> None:
    document = valid_proposal()
    proposal = McpOperationProposal.from_dict(document)
    arguments = document["arguments"]
    assert isinstance(arguments, dict)
    arguments["query"] = "caller mutation"
    source = document["source_constraints"]
    assert isinstance(source, dict)
    source["folders"] = ["all"]

    assert proposal.arguments["query"] == "quarterly plan"
    assert proposal.source_constraints["folders"] == ("inbox",)
    with pytest.raises(TypeError):
        proposal.arguments["query"] = "mutation"  # type: ignore[index]


def test_trusted_snapshots_are_exact_validated_and_immutable() -> None:
    connection_source = {"folders": ["inbox"]}
    connection = McpConnectionSnapshot(
        connection_id="connection-1",
        connection_revision="b" * 64,
        server_id="omega.first_party.external",
        server_version="1.0.0",
        artifact_sha256="a" * 64,
        provider_id="provider.fake",
        account_ref="account-1",
        enabled=True,
        capability_ceiling=("mail.search",),
        allowed_tool_schemas={"omega.fake.search": "c" * 64},
        source_constraints=connection_source,
        destination_constraints={},
    )
    authority = McpAuthoritySnapshot(
        authority_kind="direct_turn",
        authority_id="authority-1",
        authority_revision="d" * 64,
        episode_id="episode-1",
        job_id="",
        job_revision="",
        connection_id="connection-1",
        connection_revision="b" * 64,
        server_id="omega.first_party.external",
        server_version="1.0.0",
        artifact_sha256="a" * 64,
        tool_name="omega.fake.search",
        tool_schema_sha256="c" * 64,
        capability_tags=("mail.search",),
        source_constraints={"folders": ["inbox"]},
        destination_constraints={},
        risk_classes=("read_only",),
        direct_execution=True,
        standing_execution=False,
        allow_send_publish=False,
        allow_destructive=False,
        allow_cross_provider=False,
    )
    connection_source["folders"] = ["everything"]

    assert connection.source_constraints["folders"] == ("inbox",)
    assert authority.direct_execution is True
    with pytest.raises(TypeError):
        connection.allowed_tool_schemas["other"] = "e" * 64  # type: ignore[index]


def test_standing_authority_requires_a_bound_job_revision() -> None:
    with pytest.raises(ValueError, match="invalid_authority_snapshot"):
        McpAuthoritySnapshot(
            authority_kind="standing_grant",
            authority_id="grant-1",
            authority_revision="d" * 64,
            episode_id="episode-1",
            job_id="job-1",
            job_revision="",
            connection_id="connection-1",
            connection_revision="b" * 64,
            server_id="omega.first_party.external",
            server_version="1.0.0",
            artifact_sha256="a" * 64,
            tool_name="omega.fake.search",
            tool_schema_sha256="c" * 64,
            capability_tags=("mail.search",),
            source_constraints={},
            destination_constraints={},
            risk_classes=("read_only",),
            direct_execution=False,
            standing_execution=True,
            allow_send_publish=False,
            allow_destructive=False,
            allow_cross_provider=False,
        )


def test_plugin_authority_material_is_exact_and_immutable() -> None:
    authority = McpAuthoritySnapshot(
        authority_kind="direct_turn",
        authority_id="authority-1",
        authority_revision="d" * 64,
        episode_id="episode-1",
        job_id="",
        job_revision="",
        connection_id="connection-1",
        connection_revision="b" * 64,
        server_id="omega.first_party.external",
        server_version="1.0.0",
        artifact_sha256="a" * 64,
        tool_name="omega.fake.create",
        tool_schema_sha256="c" * 64,
        capability_tags=("mail.create",),
        source_constraints={},
        destination_constraints={},
        risk_classes=("additive_write",),
        direct_execution=True,
        standing_execution=False,
        allow_send_publish=False,
        allow_destructive=False,
        allow_cross_provider=False,
        plan_digest="e" * 64,
        account_key_hash="f" * 64,
        candidate_id="1" * 64,
        plan_step_id="step-1",
        arguments_sha256="2" * 64,
        connection_authority_revision="3" * 64,
        effect="create",
    )
    tool = McpToolAuthority(
        tool_name="omega.fake.create",
        input_schema_sha256="c" * 64,
        capability_tags=("mail.create",),
        risk_class=RiskClass.ADDITIVE_WRITE,
        effect="create",
        requires_revision=False,
    )

    assert authority.plan_digest == "e" * 64
    assert authority.account_key_hash == "f" * 64
    assert tool.risk_class is RiskClass.ADDITIVE_WRITE
