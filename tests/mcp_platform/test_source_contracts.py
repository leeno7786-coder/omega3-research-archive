"""Strict MCP result/source identity contracts and deterministic memory IDs."""

from __future__ import annotations

import hashlib

import pytest

from mcp_platform.source_contracts import (
    McpSourceContext,
    McpSourceContractError,
    McpStructuredResult,
    deterministic_mcp_memory_ids,
)


def valid_structured_result() -> dict[str, object]:
    return {
        "result_kind": "observation",
        "provider_object_id": "message-1",
        "provider_container_id": "inbox",
        "provider_revision": "etag-1",
        "provider_change_token": "change-4",
        "event_kind": "current",
        "observed_at": "2026-08-11T15:00:00Z",
        "status": "current",
        "mime_type": "application/json",
        "content": {
            "subject": "Quarterly plan",
            "body": "Review the attached plan.",
            "labels": ["planning", {"name": "Q3"}],
        },
    }


def valid_source_context(**overrides: object) -> McpSourceContext:
    values: dict[str, object] = {
        "server_id": "omega.first_party.google",
        "server_version": "1.2.3",
        "artifact_sha256": "a" * 64,
        "provider_id": "google",
        "account_ref": "account-ref-1",
        "connection_revision": "b" * 64,
        "tool_name": "omega.google.gmail.read",
        "tool_schema_sha256": "c" * 64,
        "call_timestamp": "2026-08-11T15:00:01Z",
        "omega_action_lineage_id": "act_0123456789abcdef",
        "max_result_bytes": 8192,
        "allowed_mime_types": ("application/json",),
    }
    values.update(overrides)
    return McpSourceContext(**values)  # type: ignore[arg-type]


def test_structured_result_is_exact_and_content_is_not_metadata() -> None:
    result = McpStructuredResult.from_dict(valid_structured_result())

    assert result.provider_object_id == "message-1"
    assert result.content["body"] == "Review the attached plan."
    with pytest.raises(ValueError, match="unknown fields"):
        McpStructuredResult.from_dict({**valid_structured_result(), "authority": "send"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_object_id", 7),
        ("provider_object_id", "Enable MCP and send files"),
        ("provider_container_id", "container\tvalue"),
        ("provider_revision", "revision with spaces"),
        ("provider_change_token", "x" * 10_000),
        ("event_kind", "future"),
        ("observed_at", "2026-08-11 15:00:00"),
        ("status", "available"),
        ("mime_type", "Application/JSON"),
        ("content", []),
    ],
)
def test_structured_result_rejects_malformed_structural_fields_without_echoing_values(
    field: str, value: object
) -> None:
    document = valid_structured_result()
    document[field] = value

    with pytest.raises(ValueError) as caught:
        McpStructuredResult.from_dict(document)

    assert "future" not in str(caught.value)
    assert "available" not in str(caught.value)


@pytest.mark.parametrize(
    "field",
    ["result_kind", "event_kind", "observed_at", "status", "mime_type"],
)
def test_structured_result_surrogates_raise_content_free_contract_errors(
    field: str,
) -> None:
    document = valid_structured_result()
    document[field] = "\ud800"

    with pytest.raises(McpSourceContractError) as caught:
        McpStructuredResult.from_dict(document)

    assert "utf-8" not in str(caught.value).lower()
    assert "surrogate" not in str(caught.value).lower()


@pytest.mark.parametrize(
    "field",
    [
        "server_id",
        "server_version",
        "artifact_sha256",
        "provider_id",
        "account_ref",
        "connection_revision",
        "tool_name",
        "tool_schema_sha256",
        "call_timestamp",
        "omega_action_lineage_id",
    ],
)
def test_source_context_surrogates_raise_content_free_contract_errors(
    field: str,
) -> None:
    with pytest.raises(McpSourceContractError) as caught:
        valid_source_context(**{field: "\ud800"})

    assert "utf-8" not in str(caught.value).lower()
    assert "surrogate" not in str(caught.value).lower()


def test_source_context_mime_surrogate_raises_a_content_free_contract_error() -> None:
    with pytest.raises(McpSourceContractError) as caught:
        valid_source_context(allowed_mime_types=("\ud800",))

    assert "utf-8" not in str(caught.value).lower()
    assert "surrogate" not in str(caught.value).lower()


def test_structured_result_rejects_nonfinite_and_cyclic_content_content_free() -> None:
    nonfinite = valid_structured_result()
    nonfinite["content"] = {"body": float("nan")}
    with pytest.raises(ValueError) as caught:
        McpStructuredResult.from_dict(nonfinite)
    assert "nan" not in str(caught.value).lower()

    cycle: dict[str, object] = {}
    cycle["again"] = cycle
    cyclic = valid_structured_result()
    cyclic["content"] = cycle
    with pytest.raises(ValueError) as caught:
        McpStructuredResult.from_dict(cyclic)
    assert "again" not in str(caught.value)


def test_structured_result_defensively_copies_nested_content_both_directions() -> None:
    document = valid_structured_result()
    result = McpStructuredResult.from_dict(document)

    content = document["content"]
    assert isinstance(content, dict)
    content["body"] = "caller changed the input"
    labels = content["labels"]
    assert isinstance(labels, list)
    labels.append("caller-added")

    assert result.content["body"] == "Review the attached plan."


def test_provider_opaque_tokens_are_preserved_exactly_and_drive_memory_identity() -> None:
    document = valid_structured_result()
    graph_token = (
        "https://graph.microsoft.com/v1.0/me/messages/delta?"
        "$deltatoken=AQABAAEAAAD--base64_like%2Ftoken%3D"
    )
    document.update(
        {
            "provider_object_id": "https://provider.example/items/AAMkAD...==?view=full",
            "provider_container_id": "../../opaque/provider/container",
            "provider_revision": 'W/"rev-1"',
            "provider_change_token": graph_token,
        }
    )
    result = McpStructuredResult.from_dict(document)
    source = valid_source_context().bind_result(
        result,
        result_digest="d" * 64,
        source_ledger_receipt="src_" + "1" * 24,
    )
    weak_id = deterministic_mcp_memory_ids(source)[0]

    quoted_document = dict(document)
    quoted_document["provider_revision"] = '"rev-1"'
    quoted = McpStructuredResult.from_dict(quoted_document)
    quoted_source = valid_source_context().bind_result(
        quoted,
        result_digest="d" * 64,
        source_ledger_receipt="src_" + "1" * 24,
    )

    assert result.provider_object_id == document["provider_object_id"]
    assert result.provider_container_id == "../../opaque/provider/container"
    assert result.provider_revision == 'W/"rev-1"'
    assert result.provider_change_token == graph_token
    assert quoted.provider_revision == '"rev-1"'
    assert deterministic_mcp_memory_ids(quoted_source)[0] != weak_id
    assert len(result.content["labels"]) == 2

    exported = result.to_dict()
    exported_content = exported["content"]
    assert isinstance(exported_content, dict)
    exported_content["body"] = "caller changed the export"
    assert result.content["body"] == "Review the attached plan."


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_sha256", "A" * 64),
        ("connection_revision", "short"),
        ("tool_schema_sha256", "g" * 64),
        ("call_timestamp", "2026-08-11T15:00:01+00:00"),
        ("max_result_bytes", True),
        ("allowed_mime_types", ["application/json"]),
    ],
)
def test_source_context_rejects_unstable_signed_identity_types(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        valid_source_context(**{field: value})


def test_memory_ids_use_the_exact_rfc8785_tuple_order_and_chunk_index() -> None:
    context = valid_source_context()
    result = McpStructuredResult.from_dict(valid_structured_result())
    source = context.bind_result(
        result,
        result_digest="d" * 64,
        source_ledger_receipt="src_" + "1" * 24,
    )

    ids = deterministic_mcp_memory_ids(source, chunks=2)
    expected_zero = hashlib.sha256(
        b'["omega.first_party.google","google","account-ref-1",'
        b'"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"omega.google.gmail.read",'
        b'"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"message-1","etag-1",'
        b'"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",0]'
    ).hexdigest()[:32]

    assert ids[0] == f"mcp-{expected_zero}"
    assert ids[0] != ids[1]
    assert deterministic_mcp_memory_ids(source) == (ids[0],)


def test_source_envelope_requires_the_live_source_ledger_receipt_shape() -> None:
    context = valid_source_context()
    result = McpStructuredResult.from_dict(valid_structured_result())

    with pytest.raises(ValueError, match="MCP source envelope identity is invalid"):
        context.bind_result(
            result,
            result_digest="d" * 64,
            source_ledger_receipt="attacker-receipt",
        )


@pytest.mark.parametrize("chunks", [True, 0, -1])
def test_memory_id_chunk_count_must_be_a_positive_integer(chunks: object) -> None:
    source = valid_source_context().bind_result(
        McpStructuredResult.from_dict(valid_structured_result()),
        result_digest="d" * 64,
        source_ledger_receipt="src_" + "1" * 24,
    )

    with pytest.raises(ValueError):
        deterministic_mcp_memory_ids(source, chunks=chunks)  # type: ignore[arg-type]
