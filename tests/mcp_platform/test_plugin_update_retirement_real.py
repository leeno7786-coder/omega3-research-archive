from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_protocol import OAuthNetworkError, OAuthProviderError
from mcp_platform.oauth_vault import OAuthClientCredential
from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_connections import (
    PluginConnectionService,
    PluginUpdateConnectionRecovery,
)
from mcp_platform.plugin_grants import PluginGrantService
from tests.mcp_platform.test_oauth_broker import (
    _Clock,
    _Protocol,
    _Registry,
    _TrackingVault,
    _broker,
    _callback,
    _commit,
    _profile,
    _request,
)
from tests.mcp_platform.test_plugin_grants import _Permissions, _activation


async def _authorize(
    broker: object,
    *,
    plugin_id: str,
    scopes: tuple[str, ...],
    app_id: str | None = None,
):
    request = replace(
        _request(plugin_id=plugin_id),
        app_id=(
            _request(plugin_id=plugin_id).app_id
            if app_id is None
            else app_id
        ),
        scopes=tuple(sorted(scopes)),
    )
    started = await broker.begin(request)
    return await broker.complete(
        profile_id=started.profile_id,
        callback_uri=_callback(started),
    )


def _real_services(tmp_path: Path):
    clock = _Clock()
    registry = _Registry(_profile())
    vault = _TrackingVault(tmp_path / "vault", clock)
    vault.store_credential(
        "oauth-client/example.desktop/secret",
        OAuthClientCredential("signed-public-client", "desktop-secret"),
        expected_revision=None,
    )
    protocol = _Protocol()
    broker = _broker(registry, vault, protocol, clock)
    authority = PluginAuthorityStore(path=tmp_path / "authority.json")
    permissions = _Permissions([])
    grants = PluginGrantService(
        broker=broker,
        authority_store=authority,
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    return clock, registry, vault, protocol, broker, authority, permissions, grants


def _recovery(
    grants: PluginGrantService,
    prepared: object,
    *,
    checkpoint_id: str,
) -> PluginUpdateConnectionRecovery:
    grant_recovery = grants.begin_update_recovery(
        checkpoint_id,
        plugin_id=prepared.plugin_id,
        account_key_sha256=prepared.account_key_sha256,
        app_ids=(prepared.app_id,),
    )
    lineage = grants.authority_store.checkpoint_connection_lineage(
        prepared.plugin_id,
        prepared.app_id,
        prepared.account_key_sha256,
    )
    material = {
        "checkpoint_id": checkpoint_id,
        "plugin_id": prepared.plugin_id,
        "account_key_sha256": prepared.account_key_sha256,
        "predecessor_app_ids": [prepared.app_id],
        "candidate_app_ids": [],
        "grant_recovery": grant_recovery,
        "authority_lineages": [lineage],
        "live_sessions": [
            {
                "app_id": prepared.app_id,
                "had_live_session": False,
                "connection_revision": "",
            }
        ],
    }
    material["revision"] = sha256_hex(
        canonical_bytes(
            {
                "domain": "omega.plugin-connection-update-recovery.v1",
                "material": material,
            }
        )
    )
    return PluginUpdateConnectionRecovery.from_dict(material)


@pytest.mark.anyio
async def test_real_vault_removed_app_preserves_exact_shared_dependent_and_scope(
    tmp_path: Path,
) -> None:
    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        authority,
        permissions,
        grants,
    ) = _real_services(tmp_path)
    survivor = await _authorize(
        broker,
        plugin_id="plugin.beta",
        scopes=("read:user",),
    )
    survivor_authority = grants.activate(survivor, _activation(survivor))
    removed = await _authorize(
        broker,
        plugin_id="plugin.alpha",
        scopes=("repo",),
    )
    grants.activate(removed, _activation(removed))
    assert removed.partition_key == survivor.partition_key
    recovery = _recovery(
        grants,
        removed,
        checkpoint_id="plugin-update-recovery-" + "1" * 64,
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants

    await connection.retire_predecessor_for_update(
        recovery,
        app_id=removed.app_id,
    )

    assert grants.current(
        removed.plugin_id,
        app_id=removed.app_id,
        account_key_sha256=removed.account_key_sha256,
    ) is None
    survivor_record = grants.current(
        survivor.plugin_id,
        app_id=survivor.app_id,
        account_key_sha256=survivor.account_key_sha256,
    )
    assert survivor_record is not None
    assert survivor_record.authority == survivor_authority
    assert authority.connection(
        survivor.plugin_id,
        survivor.app_id,
        survivor.account_key_sha256,
    ) == survivor_authority
    with pytest.raises(
        ValueError,
        match="^plugin_connection_authority_not_found$",
    ):
        authority.connection(
            removed.plugin_id,
            removed.app_id,
            removed.account_key_sha256,
        )
    partition = vault.load_token_partition(removed.partition_key)
    assert partition.dependent_plugin_ids == (survivor.plugin_id,)
    active = tuple(
        item
        for item in broker.committed_grant_receipts()
        if item.status in {"active", "reauthorization_required"}
    )
    assert tuple((item.plugin_id, item.app_id) for item in active) == (
        (survivor.plugin_id, survivor.app_id),
    )
    assert grants.union_scopes(
        removed.partition_key,
        ("candidate.scope",),
    ) == ("candidate.scope", "read:user")
    assert permissions.invalidated == []

    await connection.retire_predecessor_for_update(
        recovery,
        app_id=removed.app_id,
    )
    assert vault.load_token_partition(removed.partition_key) == partition


@pytest.mark.anyio
async def test_shared_partition_revocation_crash_retries_exactly_after_restart(
    tmp_path: Path,
) -> None:
    (
        clock,
        registry,
        vault,
        protocol,
        broker,
        _authority,
        permissions,
        grants,
    ) = _real_services(tmp_path)
    survivor = await _authorize(
        broker,
        plugin_id="plugin.beta",
        scopes=("read:user",),
    )
    grants.activate(survivor, _activation(survivor))
    removed = await _authorize(
        broker,
        plugin_id="plugin.alpha",
        scopes=("repo",),
    )
    grants.activate(removed, _activation(removed))
    recovery = _recovery(
        grants,
        removed,
        checkpoint_id="plugin-update-recovery-" + "4" * 64,
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants

    def crash_after_shared_vault_commit(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected_after_shared_vault_commit")

    grants._complete_disconnect = crash_after_shared_vault_commit
    with pytest.raises(RuntimeError, match="^injected_after_shared_vault_commit$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=removed.app_id,
        )

    assert grants.pending_disconnect(
        removed.plugin_id,
        app_id=removed.app_id,
        account_key_sha256=removed.account_key_sha256,
    ) is not None
    assert vault.load_token_partition(removed.partition_key).dependent_plugin_ids == (
        survivor.plugin_id,
    )
    assert tuple(
        (item.plugin_id, item.status)
        for item in broker.committed_grant_receipts()
    ) == ((survivor.plugin_id, "active"),)
    assert protocol.revoke_calls == []

    restarted_vault = _TrackingVault(tmp_path / "vault", clock)
    restarted_broker = _broker(registry, restarted_vault, protocol, clock)
    restarted_grants = PluginGrantService(
        broker=restarted_broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    restarted = object.__new__(PluginConnectionService)
    restarted._grants = restarted_grants

    await restarted.retire_predecessor_for_update(
        recovery,
        app_id=removed.app_id,
    )

    assert restarted_grants.pending_disconnect(
        removed.plugin_id,
        app_id=removed.app_id,
        account_key_sha256=removed.account_key_sha256,
    ) is None
    assert restarted_grants.current(
        removed.plugin_id,
        app_id=removed.app_id,
        account_key_sha256=removed.account_key_sha256,
    ) is None
    assert restarted_grants.current(
        survivor.plugin_id,
        app_id=survivor.app_id,
        account_key_sha256=survivor.account_key_sha256,
    ) is not None
    assert restarted_vault.load_token_partition(
        removed.partition_key
    ).dependent_plugin_ids == (survivor.plugin_id,)
    assert tuple(
        (item.plugin_id, item.status)
        for item in restarted_broker.committed_grant_receipts()
    ) == ((survivor.plugin_id, "active"),)
    assert protocol.revoke_calls == []


@pytest.mark.anyio
async def test_real_vault_completed_shared_retry_rejects_replacement_plugin_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _clock,
        _registry,
        vault,
        protocol,
        broker,
        _authority,
        _permissions,
        grants,
    ) = _real_services(tmp_path)
    survivor = await _authorize(
        broker,
        plugin_id="plugin.beta",
        scopes=("read:user",),
    )
    grants.activate(survivor, _activation(survivor))
    removed = await _authorize(
        broker,
        plugin_id="plugin.alpha",
        scopes=("repo",),
    )
    grants.activate(removed, _activation(removed))
    recovery = _recovery(
        grants,
        removed,
        checkpoint_id="plugin-update-recovery-" + "5" * 64,
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants
    complete = grants._complete_disconnect

    def crash_after_tombstone(*args: object, **kwargs: object) -> object:
        complete(*args, **kwargs)
        raise RuntimeError("injected_after_shared_tombstone")

    monkeypatch.setattr(grants, "_complete_disconnect", crash_after_tombstone)
    with pytest.raises(RuntimeError, match="^injected_after_shared_tombstone$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=removed.app_id,
        )
    monkeypatch.setattr(grants, "_complete_disconnect", complete)
    replacement = await _authorize(
        broker,
        plugin_id=removed.plugin_id,
        app_id="app.replacement",
        scopes=("repo",),
    )
    assert replacement.partition_key == removed.partition_key
    grants.activate(replacement, _activation(replacement))

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=removed.app_id,
        )

    assert grants.current(
        replacement.plugin_id,
        app_id=replacement.app_id,
        account_key_sha256=replacement.account_key_sha256,
    ) is not None
    assert vault.load_token_partition(
        removed.partition_key
    ).dependent_plugin_ids == (removed.plugin_id, survivor.plugin_id)
    assert protocol.revoke_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider_error",
    (
        OAuthNetworkError("oauth_provider_unreachable"),
        OAuthProviderError("oauth_provider_rejected"),
    ),
    ids=("network", "provider-response"),
)
async def test_real_vault_last_dependent_failure_survives_restart_and_retries_exactly(
    tmp_path: Path,
    provider_error: Exception,
) -> None:
    (
        clock,
        registry,
        vault,
        protocol,
        broker,
        authority,
        permissions,
        grants,
    ) = _real_services(tmp_path)
    removed = await _authorize(
        broker,
        plugin_id="plugin.alpha",
        scopes=("repo",),
    )
    grants.activate(removed, _activation(removed))
    recovery = _recovery(
        grants,
        removed,
        checkpoint_id="plugin-update-recovery-" + "2" * 64,
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants
    protocol.revoke_error = provider_error

    with pytest.raises(type(provider_error), match=f"^{provider_error}$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=removed.app_id,
        )
    assert grants.pending_disconnect(
        removed.plugin_id,
        app_id=removed.app_id,
        account_key_sha256=removed.account_key_sha256,
    ) is not None
    with pytest.raises(
        ValueError,
        match="^plugin_connection_authority_not_found$",
    ):
        authority.connection(
            removed.plugin_id,
            removed.app_id,
            removed.account_key_sha256,
        )
    assert vault.load_token_partition(
        removed.partition_key
    ).revocation_pending_plugin_id == removed.plugin_id

    protocol.revoke_error = None
    restarted_vault = _TrackingVault(tmp_path / "vault", clock)
    restarted_broker = _broker(registry, restarted_vault, protocol, clock)
    restarted_grants = PluginGrantService(
        broker=restarted_broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    restarted = object.__new__(PluginConnectionService)
    restarted._grants = restarted_grants

    await restarted.retire_predecessor_for_update(
        recovery,
        app_id=removed.app_id,
    )

    assert restarted_grants.pending_disconnect(
        removed.plugin_id,
        app_id=removed.app_id,
        account_key_sha256=removed.account_key_sha256,
    ) is None
    assert restarted_grants.current(
        removed.plugin_id,
        app_id=removed.app_id,
        account_key_sha256=removed.account_key_sha256,
    ) is None
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        restarted_vault.load_token_partition(removed.partition_key)
    assert tuple(
        item
        for item in restarted_broker.committed_grant_receipts()
        if item.status in {"active", "reauthorization_required"}
    ) == ()
    assert restarted_grants.union_scopes(
        removed.partition_key,
        ("candidate.scope",),
    ) == ("candidate.scope",)
    assert len(protocol.revoke_calls) == 2
    assert permissions.invalidated == []

    await restarted.retire_predecessor_for_update(
        recovery,
        app_id=removed.app_id,
    )
    assert len(protocol.revoke_calls) == 2


@pytest.mark.anyio
async def test_real_vault_completed_last_dependent_retry_rejects_recreated_partition(
    tmp_path: Path,
) -> None:
    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        _authority,
        _permissions,
        grants,
    ) = _real_services(tmp_path)
    removed = await _authorize(
        broker,
        plugin_id="plugin.alpha",
        scopes=("repo",),
    )
    grants.activate(removed, _activation(removed))
    recovery = _recovery(
        grants,
        removed,
        checkpoint_id="plugin-update-recovery-" + "6" * 64,
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants
    await connection.retire_predecessor_for_update(
        recovery,
        app_id=removed.app_id,
    )
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        vault.load_token_partition(removed.partition_key)

    replacement = await _authorize(
        broker,
        plugin_id=removed.plugin_id,
        app_id="app.replacement",
        scopes=("repo",),
    )
    assert replacement.partition_key == removed.partition_key
    committed_replacement = broker.commit(
        replacement,
        replace(_commit(), connection_generation=2),
    )
    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=removed.app_id,
        )

    assert vault.load_token_partition(replacement.partition_key).dependent_plugin_ids == (
        replacement.plugin_id,
    )
    assert tuple(
        item.receipt_id
        for item in broker.committed_grant_receipts()
        if item.status in {"active", "reauthorization_required"}
    ) == (committed_replacement.receipt_id,)


@pytest.mark.anyio
async def test_unsealed_same_plugin_receipt_cannot_be_revoked_by_stale_retirement(
    tmp_path: Path,
) -> None:
    (
        _clock,
        _registry,
        vault,
        protocol,
        broker,
        _authority,
        _permissions,
        grants,
    ) = _real_services(tmp_path)
    removed = await _authorize(
        broker,
        plugin_id="plugin.alpha",
        scopes=("repo",),
    )
    grants.activate(removed, _activation(removed))
    recovery = _recovery(
        grants,
        removed,
        checkpoint_id="plugin-update-recovery-" + "3" * 64,
    )
    unsealed = await _authorize(
        broker,
        plugin_id="plugin.alpha",
        app_id="app.unsealed",
        scopes=("repo", "write:user"),
    )
    grants.activate(unsealed, _activation(unsealed))
    assert unsealed.partition_key == removed.partition_key
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=removed.app_id,
        )

    assert protocol.revoke_calls == []
    assert grants.current(
        unsealed.plugin_id,
        app_id=unsealed.app_id,
        account_key_sha256=unsealed.account_key_sha256,
    ) is not None
    partition = vault.load_token_partition(unsealed.partition_key)
    assert partition.dependent_plugin_ids == (unsealed.plugin_id,)
    assert tuple(
        item.app_id
        for item in broker.committed_grant_receipts()
        if item.status in {"active", "reauthorization_required"}
    ) == (unsealed.app_id,)
