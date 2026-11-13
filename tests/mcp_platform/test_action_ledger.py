from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_platform import action_ledger


def _dispatch(path: Path, action_id: str = "action-1") -> dict:
    return action_ledger.record_dispatch(
        omega_action_id=action_id,
        episode_id="episode-1",
        request_id="request-1",
        server_id="omega.google.drive",
        server_version="1.0.0",
        artifact_sha256="a" * 64,
        provider="google.workspace",
        account="account-1",
        connection_id="connection-1",
        connection_revision="b" * 64,
        capability="drive.search",
        operation="drive.search",
        tool_schema_sha256="c" * 64,
        risk_class="read_only",
        authority_kind="direct_turn",
        authority_id="turn-1",
        authority_revision="d" * 64,
        proposal_digest="e" * 64,
        source_constraints_sha256="f" * 64,
        destination_constraints_sha256="0" * 64,
        idempotency_key="request-1",
        is_write=False,
        path=path,
    )


def test_dispatch_is_durable_on_the_canonical_mcp_path(tmp_path, monkeypatch) -> None:
    from core import paths

    canonical = tmp_path / "guardian" / "state" / "mcp" / "action_ledger.json"
    monkeypatch.setattr(paths, "MCP_ACTION_LEDGER_PATH", canonical)
    record = action_ledger.record_dispatch(
        omega_action_id="default-action",
        episode_id="episode-1",
        capability="drive.search",
        operation="drive.search",
        is_write=False,
    )

    assert record["state"] == "dispatched"
    assert json.loads(canonical.read_text(encoding="utf-8"))["default-action"] == record
    assert not (tmp_path / "guardian" / "state" / "connectors").exists()


def test_dispatch_and_receipt_preserve_signed_lineage(tmp_path) -> None:
    path = tmp_path / "action_ledger.json"
    record = _dispatch(path)
    assert action_ledger.get_action("action-1", path=path) == record

    closed = action_ledger.record_receipt(
        "action-1",
        provider_object_id="file-1",
        provider_revision="rev-1",
        receipt={"id": "file-1", "status": "ok"},
        path=path,
    )
    assert closed["state"] == "confirmed"
    assert (
        action_ledger.match_receipt(
            "file-1", provider="google.workspace", observed_revision="rev-1", path=path
        )["omega_action_id"]
        == "action-1"
    )


def test_dispatch_records_authority_lineage_and_ambiguous_state(tmp_path) -> None:
    path = tmp_path / "action_ledger.json"
    record = _dispatch(path)
    assert record["authority_kind"] == "direct_turn"
    assert record["authority_id"] == "turn-1"
    assert record["authority_revision"] == "d" * 64
    assert record["proposal_digest"] == "e" * 64

    failed = action_ledger.record_failure(
        "action-1",
        "post-dispatch transport failure",
        ambiguous=True,
        path=path,
    )
    assert failed["state"] == "outcome_unknown"


def test_repeated_dispatch_is_idempotent(tmp_path) -> None:
    path = tmp_path / "action_ledger.json"
    first, created = action_ledger.find_or_record_dispatch(
        omega_action_id="action-1",
        episode_id="episode-1",
        capability="drive.search",
        operation="drive.search",
        is_write=False,
        idempotency_key="request-1",
        proposal_digest="proposal-1",
        path=path,
    )
    second, replayed = action_ledger.find_or_record_dispatch(
        omega_action_id="action-2",
        episode_id="episode-1",
        capability="drive.search",
        operation="drive.search",
        is_write=False,
        idempotency_key="request-1",
        proposal_digest="proposal-1",
        path=path,
    )
    assert created is True
    assert replayed is False
    assert second == first
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 1


def test_failed_dispatch_is_retryable_only_with_same_identity(tmp_path) -> None:
    path = tmp_path / "action_ledger.json"
    _dispatch(path)
    failed = action_ledger.record_failure("action-1", "rejected", path=path)
    assert failed["state"] == "failed"
    retry, created = action_ledger.find_or_record_dispatch(
        omega_action_id="action-2",
        episode_id="episode-1",
        capability="drive.search",
        operation="drive.search",
        is_write=False,
        idempotency_key="request-1",
        proposal_digest="e" * 64,
        path=path,
    )
    assert created is True
    assert retry["omega_action_id"] == "action-2"


def test_ambiguous_failure_is_not_blindly_retried(tmp_path) -> None:
    path = tmp_path / "action_ledger.json"
    _dispatch(path)
    unknown = action_ledger.record_failure("action-1", "timeout", ambiguous=True, path=path)
    assert unknown["state"] == "outcome_unknown"
    assert action_ledger.get_action("action-1", path=path)["state"] == "outcome_unknown"


def test_corrupt_state_fails_closed_without_leaking_contents(tmp_path) -> None:
    path = tmp_path / "action_ledger.json"
    sentinel = b"{secret-sentinel"
    path.write_bytes(sentinel)
    with pytest.raises(action_ledger.ActionLedgerUnreadable) as error:
        action_ledger.get_action("missing", path=path)
    assert str(error.value) == "action ledger state is unreadable"
    assert "secret-sentinel" not in str(error.value)
    assert path.read_bytes() == sentinel


def test_receipt_serialization_failure_preserves_authoritative_state(tmp_path) -> None:
    path = tmp_path / "action_ledger.json"
    _dispatch(path)
    before = path.read_bytes()
    with pytest.raises(action_ledger.ActionLedgerUnreadable):
        action_ledger.record_receipt(
            "action-1",
            provider_object_id="file-1",
            receipt={"count": float("nan")},
            path=path,
        )
    assert path.read_bytes() == before


def test_explicit_path_never_reads_or_writes_any_other_store(tmp_path) -> None:
    ledger_root = tmp_path / "ledger"
    path = ledger_root / "explicit.json"
    _dispatch(path)
    assert path.exists()
    assert list(ledger_root.rglob("*.json")) == [path]
