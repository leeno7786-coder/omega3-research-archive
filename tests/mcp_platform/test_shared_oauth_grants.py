"""Generic shared-profile grant ownership contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_grants import PluginGrantService
from tests.mcp_platform.test_plugin_grants import (
    _ACCOUNT,
    _PARTITION,
    _Broker,
    _Permissions,
    _activation,
    _prepared,
)


@pytest.fixture
def generic_grants(
    tmp_path: Path,
) -> tuple[PluginGrantService, _Broker, _Permissions]:
    broker = _Broker()
    permissions = _Permissions([])
    return (
        PluginGrantService(
            broker=broker,
            authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
            permission_store=permissions,
            journal_path=tmp_path / "grants.json",
        ),
        broker,
        permissions,
    )


def test_distinct_profiles_share_scope_union_only_through_exact_partition(
    generic_grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, _permissions = generic_grants
    documents = _prepared(
        "suite.documents",
        "suite.documents.mcp",
        profile_id="profile.documents",
        scopes=("documents.read",),
        state="6",
    )
    messages = _prepared(
        "suite.messages",
        "suite.messages.mcp",
        profile_id="profile.messages",
        scopes=("messages.read",),
        state="7",
    )
    service.activate(documents, _activation(documents))
    service.activate(messages, _activation(messages))

    assert service.union_scopes(_PARTITION, ("events.read",)) == (
        "documents.read",
        "events.read",
        "messages.read",
    )


@pytest.mark.anyio
async def test_shared_partition_deletion_belongs_only_to_last_generic_dependent(
    generic_grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, permissions = generic_grants
    first = _prepared(
        "suite.documents",
        "suite.documents.mcp",
        profile_id="profile.documents",
        scopes=("documents.read",),
        state="8",
    )
    second = _prepared(
        "suite.messages",
        "suite.messages.mcp",
        profile_id="profile.messages",
        scopes=("messages.read",),
        state="9",
    )
    service.activate(first, _activation(first))
    service.activate(second, _activation(second))

    remaining = await service.revoke("suite.documents", account_key_sha256=_ACCOUNT)
    deleted = await service.revoke("suite.messages", account_key_sha256=_ACCOUNT)

    assert remaining.provider_token_deleted is False
    assert remaining.remaining_plugin_ids == ("suite.messages",)
    assert deleted.provider_token_deleted is True
    assert permissions.invalidated == ["suite.documents", "suite.messages"]


def test_generic_grant_journal_does_not_encode_provider_or_issuer_names(
    generic_grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, _permissions = generic_grants
    prepared = _prepared(
        "suite.documents",
        "suite.documents.mcp",
        profile_id="profile.documents",
        scopes=("documents.read",),
        state="a",
    )
    service.activate(prepared, _activation(prepared))

    journal = service.journal_path.read_text(encoding="utf-8").lower()

    assert "provider_id" not in journal
    assert "authorization_issuer" not in journal
