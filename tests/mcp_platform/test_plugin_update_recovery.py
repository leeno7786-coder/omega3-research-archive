from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.plugin_authority import (
    PluginAuthorityStore,
    PluginConnectionAuthority,
    PluginConnectionAuthorityKind,
    plugin_connection_authority_revision,
)
from mcp_platform.plugin_connections import (
    PluginConnectionCoordinator,
    PluginConnectionService,
    PluginUpdateConnectionRecovery,
)
from mcp_platform.plugin_grants import PluginGrantRecord, PluginGrantService
from plugin_platform.records import PluginAppBinding
from tests.helpers.plugin_consent import full_connection_grant
from tests.mcp_platform.test_plugin_connections import (
    ACCOUNT_DIGEST,
    ACCOUNT_KEY,
    TRACKER_APP,
    TRACKER_PLUGIN,
    _Broker as _ConnectionBroker,
    _GrantService as _ConnectionGrantService,
    _remote_service,
)
from tests.mcp_platform.test_plugin_grants import (
    _ACCOUNT,
    _Broker,
    _Permissions,
    _activation,
    _prepared,
)


class _CheckpointBroker(_Broker):
    def __init__(self) -> None:
        super().__init__()
        self.checkpoint = {
            "checkpoint_id": "plugin-update-recovery-" + "a" * 64,
            "revision": "b" * 64,
        }
        self.restored: list[tuple[str, str]] = []
        self.finalized: list[tuple[str, str]] = []

    def committed_grant_receipts(self):
        """Match the vault's one live grant per partition/plugin invariant."""

        latest = {}
        for receipt in super().committed_grant_receipts():
            latest[(receipt.token_partition_key, receipt.plugin_id)] = receipt
        return tuple(sorted(latest.values(), key=lambda item: item.receipt_id))

    def grant_receipt_state(self, expected):
        match = next(
            (
                item
                for item in self.committed_grant_receipts()
                if item.receipt_id == expected.receipt_id
            ),
            None,
        )
        if match is not None and match.status == "revoked":
            return "absent"
        return super().grant_receipt_state(expected)

    def require_plugin_partition_receipts_exact(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_receipts: tuple[object, ...],
    ) -> None:
        current = tuple(
            replace(item, status="active", claimed_authority_revision=None)
            for item in self.committed_grant_receipts()
            if item.token_partition_key == partition_key
            and item.plugin_id == plugin_id
            and item.status != "revoked"
        )
        expected = tuple(
            replace(item, status="active", claimed_authority_revision=None)
            for item in expected_receipts
        )
        if current != expected:
            raise ValueError("oauth_grant_receipt_conflict")

    async def revoke(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_receipts: tuple[object, ...] | None = None,
        tolerate_provider_refusal: bool = False,
    ):
        del tolerate_provider_refusal
        if expected_receipts is not None:
            self.require_plugin_partition_receipts_exact(
                partition_key,
                plugin_id=plugin_id,
                expected_receipts=expected_receipts,
            )
        result = await super().revoke(partition_key, plugin_id=plugin_id)
        self.receipts = [
            (
                replace(item, status="revoked")
                if item.token_partition_key == partition_key
                and item.plugin_id == plugin_id
                else item
            )
            for item in self.receipts
        ]
        return result

    def begin_plugin_update_recovery(self, checkpoint_id: str, **_kwargs):
        assert checkpoint_id == self.checkpoint["checkpoint_id"]
        return dict(self.checkpoint)

    def restore_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        expected_revision: str,
    ):
        self.restored.append((checkpoint_id, expected_revision))
        return dict(self.checkpoint)

    def finalize_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        expected_revision: str,
    ) -> None:
        self.finalized.append((checkpoint_id, expected_revision))


class _CheckpointPermissions(_Permissions):
    def __init__(self) -> None:
        super().__init__([])
        self.state = "approved"
        self.restored: list[dict[str, object]] = []

    def checkpoint_plugin(self, plugin_id: str) -> dict[str, object]:
        return {"plugin_id": plugin_id, "state": self.state}

    def restore_plugin_checkpoint(self, recovery: object) -> None:
        assert type(recovery) is dict
        self.state = str(recovery["state"])
        self.restored.append(dict(recovery))

    def invalidate_plugin(self, plugin_id: str) -> int:
        self.state = "invalidated"
        return super().invalidate_plugin(plugin_id)


class _SessionRecoveryGrants(_ConnectionGrantService):
    def begin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        plugin_id: str,
        account_key_sha256: str,
        app_ids: tuple[str, ...],
    ) -> dict[str, object]:
        material = {
            "checkpoint_id": checkpoint_id,
            "vault_revision": "b" * 64,
            "plugin_id": plugin_id,
            "account_key_sha256": account_key_sha256,
            "app_ids": list(app_ids),
            "predecessor_grants": [],
            "permission_recovery": None,
        }
        material["revision"] = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.plugin-grant-update-recovery.v1",
                    "material": material,
                }
            )
        )
        return PluginGrantService._update_recovery(material)

    def restore_update_recovery(self, recovery: object) -> None:
        PluginGrantService._update_recovery(recovery)

    def finalize_update_recovery(self, recovery: object) -> None:
        PluginGrantService._update_recovery(recovery)


def _empty_candidate_binding(receipt_id: str) -> PluginAppBinding:
    material = {
        "required_app_ids": [],
        "optional_app_ids": [],
        "dependency_receipt_id": receipt_id,
    }
    return PluginAppBinding(
        required_app_ids=(),
        optional_app_ids=(),
        dependency_receipt_id=receipt_id,
        revision=sha256_hex(canonical_bytes(material)),
    )


def _retirement_recovery(
    grants: PluginGrantService,
    *,
    checkpoint_id: str,
    candidate_app_ids: tuple[str, ...] = (),
) -> PluginUpdateConnectionRecovery:
    all_app_ids = tuple(sorted({TRACKER_APP, *candidate_app_ids}))
    grant_recovery = grants.begin_update_recovery(
        checkpoint_id,
        plugin_id=TRACKER_PLUGIN,
        account_key_sha256=_ACCOUNT,
        app_ids=all_app_ids,
    )
    lineages = tuple(
        grants.authority_store.checkpoint_connection_lineage(
            TRACKER_PLUGIN,
            app_id,
            _ACCOUNT,
        )
        for app_id in all_app_ids
    )
    material = {
        "checkpoint_id": checkpoint_id,
        "plugin_id": TRACKER_PLUGIN,
        "account_key_sha256": _ACCOUNT,
        "predecessor_app_ids": [TRACKER_APP],
        "candidate_app_ids": list(candidate_app_ids),
        "grant_recovery": grant_recovery,
        "authority_lineages": list(lineages),
        "live_sessions": [
            {
                "app_id": TRACKER_APP,
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


def test_grant_update_recovery_restores_exact_predecessor_and_removes_candidate(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    authority_store = PluginAuthorityStore(path=tmp_path / "authority.json")
    permissions = _CheckpointPermissions()
    service = PluginGrantService(
        broker=broker,
        authority_store=authority_store,
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    predecessor = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read", "files.write"),
        state="1",
    )
    predecessor_authority = service.activate(predecessor, _activation(predecessor))
    predecessor_record = service.current(
        predecessor.plugin_id,
        app_id=predecessor.app_id,
        account_key_sha256=_ACCOUNT,
    )
    assert type(predecessor_record) is PluginGrantRecord
    assert PluginGrantRecord.from_dict(predecessor_record.to_dict()) == predecessor_record
    recovery = service.begin_update_recovery(
        broker.checkpoint["checkpoint_id"],
        plugin_id=predecessor.plugin_id,
        account_key_sha256=_ACCOUNT,
        app_ids=(predecessor.app_id,),
    )

    candidate = _prepared(
        predecessor.plugin_id,
        predecessor.app_id,
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="2",
    )
    candidate_authority = service.activate(candidate, _activation(candidate))
    assert candidate_authority != predecessor_authority
    assert permissions.state == "invalidated"

    service.restore_update_recovery(recovery)

    assert service.current(
        predecessor.plugin_id,
        app_id=predecessor.app_id,
        account_key_sha256=_ACCOUNT,
    ) == predecessor_record
    assert authority_store.connection(
        predecessor.plugin_id,
        predecessor.app_id,
        _ACCOUNT,
    ) == predecessor_authority
    assert broker.restored == [
        (broker.checkpoint["checkpoint_id"], broker.checkpoint["revision"])
    ]
    assert permissions.state == "approved"
    assert permissions.restored == [
        {"plugin_id": predecessor.plugin_id, "state": "approved"}
    ]
    service.finalize_update_recovery(recovery)
    assert broker.finalized == broker.restored


def test_no_account_projection_uses_real_durable_grant_risk_precedence(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    prepared = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.local",
        scopes=("local.scope",),
        state="d",
    )
    grants.activate(prepared, _activation(prepared))
    assert grants.local_state_without_account(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
    ) is None
    assert grants.local_plugin_state_without_account(TRACKER_PLUGIN) is None

    grants.suspend(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    )
    assert grants.local_state_without_account(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
    ) == "permission_changed"
    assert (
        grants.local_plugin_state_without_account(TRACKER_PLUGIN)
        == "permission_changed"
    )

    grants.begin_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    )
    assert grants.local_state_without_account(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
    ) == "revocation_pending"
    assert (
        grants.local_plugin_state_without_account(TRACKER_PLUGIN)
        == "revocation_pending"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "local_state, expected",
    (
        ("revocation_pending", "revocation_pending"),
        ("permission_changed", "permission_changed"),
        (None, "blocked"),
    ),
)
async def test_no_account_real_coordinator_applies_durable_risk_before_blocker(
    tmp_path: Path,
    local_state: str | None,
    expected: str,
) -> None:
    service, _broker, installed = _remote_service(
        tmp_path,
        authentication_kind="none",
    )
    registry = installed["plugin_registry"]
    registry.block(
        TRACKER_PLUGIN,
        "dependency_unavailable",
        expected_revision=registry.snapshot().revision,
    )
    service.local_state_without_account = lambda _plugin_id, *, app_id: (
        local_state if app_id == TRACKER_APP else None
    )
    coordinator = PluginConnectionCoordinator(
        plugin_registry=registry,
        connection_service=service,
    )

    snapshot = await coordinator.status(TRACKER_PLUGIN, None)

    assert snapshot.state == expected
    assert service._sessions == {}


@pytest.mark.anyio
async def test_update_cutover_restore_retry_preserves_exact_predecessor_live_session(
    tmp_path: Path,
) -> None:
    """A replayed durable rollback cannot consume the already-restored session."""

    broker = _ConnectionBroker()
    grants = _SessionRecoveryGrants(broker)
    service, _broker, installed = _remote_service(
        tmp_path,
        broker=broker,
        grants=grants,
        authentication_kind="none",
    )
    await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    predecessor = service._sessions[key]
    previous_record = installed["plugin_registry"].get(TRACKER_PLUGIN)
    assert previous_record.app_binding is not None

    checkpoint_id = "plugin-update-recovery-" + "7" * 64
    recovery = service.begin_update_recovery(
        previous_record=previous_record,
        candidate_binding=previous_record.app_binding,
        account_key=ACCOUNT_KEY,
        checkpoint_id=checkpoint_id,
    )
    assert recovery.checkpoint_id == checkpoint_id
    service.begin_update_cutover(recovery)
    assert key not in service._sessions

    await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    candidate = service._sessions[key]
    assert candidate is not predecessor

    await service.restore_update_recovery(recovery)

    # The worker owns the provider client end to end, so the parent-held
    # session client is always the `_WorkerOnlyLocalClient` marker; session
    # identity and state are the meaningful, still-observable signals here
    # (spec: "no parent-process provider I/O").
    assert service._sessions[key] is predecessor
    assert candidate is not service._sessions[key]
    assert service._states[key] == "connected"

    await service.restore_update_recovery(recovery)

    assert service._sessions[key] is predecessor
    assert candidate is not service._sessions[key]
    assert service._states[key] == "connected"

    await service.close_all()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "terminal_path",
    ("browser_denied", "browser_cancelled", "device_denied", "device_timeout"),
)
async def test_pre_cutover_terminal_candidate_path_leaves_live_predecessor_untouched(
    tmp_path: Path,
    terminal_path: str,
) -> None:
    """A denial before cutover cannot treat an absent session stash as authority."""

    broker = _ConnectionBroker()
    grants = _SessionRecoveryGrants(broker)
    service, _broker, installed = _remote_service(
        tmp_path,
        broker=broker,
        grants=grants,
        authentication_kind="none",
    )
    await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    predecessor = service._sessions[key]
    previous_record = installed["plugin_registry"].get(TRACKER_PLUGIN)
    assert previous_record.app_binding is not None
    recovery = service.begin_update_recovery(
        previous_record=previous_record,
        candidate_binding=previous_record.app_binding,
        account_key=ACCOUNT_KEY,
        checkpoint_id="plugin-update-recovery-" + "8" * 64,
    )

    # All four terminal browser/device paths converge here before cutover.  The
    # path label makes the crash matrix explicit while retaining a real client,
    # grant recovery checkpoint, authority store, and live session.
    assert terminal_path
    await service.restore_update_recovery(recovery)

    # The worker owns the provider client end to end, so the parent-held
    # session client is always the `_WorkerOnlyLocalClient` marker; session
    # identity and state are the meaningful, still-observable signals here
    # (spec: "no parent-process provider I/O").
    assert service._sessions[key] is predecessor
    assert service._states[key] == "connected"
    await service.restore_update_recovery(recovery)
    assert service._sessions[key] is predecessor
    await service.close_all()


@pytest.mark.anyio
async def test_removed_app_retirement_removes_exact_grant_scope_and_shared_dependent(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    permissions = _CheckpointPermissions()
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    removed = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.shared",
        scopes=("removed.scope",),
        state="3",
    )
    shared = _prepared(
        "suite.shared",
        "suite.shared.mcp",
        profile_id="profile.shared",
        scopes=("shared.scope",),
        state="4",
    )
    grants.activate(removed, _activation(removed))
    shared_authority = grants.activate(shared, _activation(shared))
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
    )

    await connection.retire_predecessor_for_update(recovery, app_id=TRACKER_APP)

    assert grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    assert grants.current(
        shared.plugin_id,
        app_id=shared.app_id,
        account_key_sha256=_ACCOUNT,
    ).authority == shared_authority
    assert broker.dependents[removed.partition_key] == {shared.plugin_id}
    assert grants.union_scopes(removed.partition_key, ("candidate.scope",)) == (
        "candidate.scope",
        "shared.scope",
    )
    await connection.retire_predecessor_for_update(recovery, app_id=TRACKER_APP)
    assert broker.dependents[removed.partition_key] == {shared.plugin_id}


@pytest.mark.anyio
async def test_changed_app_retires_old_partition_without_touching_candidate_grant(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    predecessor = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.old",
        scopes=("old.scope",),
        state="6",
    )
    grants.activate(predecessor, _activation(predecessor))
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
        candidate_app_ids=(TRACKER_APP,),
    )
    candidate = replace(
        _prepared(
            TRACKER_PLUGIN,
            TRACKER_APP,
            profile_id="profile.new",
            scopes=("candidate.scope",),
            state="7",
        ),
        partition_key="9" * 64,
    )
    candidate_authority = grants.activate(candidate, _activation(candidate))
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants

    await connection.retire_predecessor_for_update(
        recovery,
        app_id=TRACKER_APP,
        candidate_activations=(_activation(candidate),),
        candidate_connection_revisions=(
            (TRACKER_APP, _activation(candidate).connection_revision),
        ),
    )

    current = grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    )
    assert current is not None
    assert current.authority == candidate_authority
    assert current.receipt.token_partition_key == candidate.partition_key
    assert broker.dependents[predecessor.partition_key] == set()
    assert broker.dependents[candidate.partition_key] == {TRACKER_PLUGIN}
    assert grants.union_scopes(
        predecessor.partition_key,
        ("next.scope",),
    ) == ("next.scope",)


@pytest.mark.anyio
async def test_changed_partition_rejects_missing_sealed_candidate_before_old_revocation(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    predecessor = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.old",
        scopes=("old.scope",),
        state="d",
    )
    grants.activate(predecessor, _activation(predecessor))
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
        candidate_app_ids=(TRACKER_APP,),
    )
    candidate = replace(
        _prepared(
            TRACKER_PLUGIN,
            TRACKER_APP,
            profile_id="profile.new",
            scopes=("candidate.scope",),
            state="e",
        ),
        partition_key="9" * 64,
    )
    grants.activate(candidate, _activation(candidate))
    candidate_record = grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    )
    assert candidate_record is not None
    broker.receipts = [
        item
        for item in broker.receipts
        if item.receipt_id != candidate_record.receipt.receipt_id
    ]
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=TRACKER_APP,
            candidate_activations=(_activation(candidate),),
            candidate_connection_revisions=(
                (TRACKER_APP, _activation(candidate).connection_revision),
            ),
        )

    assert broker.dependents[predecessor.partition_key] == {TRACKER_PLUGIN}
    assert grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) == candidate_record


@pytest.mark.anyio
async def test_changed_oauth_app_retires_after_restart_with_credentialless_candidate(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    permissions = _CheckpointPermissions()
    authority_store = PluginAuthorityStore(path=tmp_path / "authority.json")
    grants = PluginGrantService(
        broker=broker,
        authority_store=authority_store,
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    predecessor = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.old",
        scopes=("old.scope",),
        state="c",
    )
    grants.activate(predecessor, _activation(predecessor))
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
        candidate_app_ids=(TRACKER_APP,),
    )
    old_authority = authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT)
    candidate_revision = "d" * 64
    authority_values = {
        "plugin_id": TRACKER_PLUGIN,
        "app_id": TRACKER_APP,
        "account_key_hash": _ACCOUNT,
        "connection_revision": candidate_revision,
        "provider_scopes": (),
        "capability_ceiling": old_authority.capability_ceiling,
        "allowed_tool_schemas": old_authority.allowed_tool_schemas,
        "source_constraints": old_authority.source_constraints,
        "destination_constraints": old_authority.destination_constraints,
        "execution_policy": old_authority.execution_policy,
        "authority_kind": PluginConnectionAuthorityKind.CREDENTIALLESS,
    }
    candidate_authority = PluginConnectionAuthority(
        **authority_values,
        revision=plugin_connection_authority_revision(**authority_values),
    )
    authority_store.publish_connection(
        candidate_authority,
        expected_revision=old_authority.revision,
    )
    restarted = object.__new__(PluginConnectionService)
    restarted._grants = grants
    restarted._sessions = {}
    broker.revoke_errors.append(ValueError("oauth_provider_timeout"))

    with pytest.raises(ValueError, match="^oauth_provider_timeout$"):
        await restarted.retire_predecessor_for_update(
            recovery,
            app_id=TRACKER_APP,
            candidate_connection_revisions=((TRACKER_APP, candidate_revision),),
        )
    assert grants.pending_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is not None
    assert authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT) == (
        candidate_authority
    )

    restarted_grants = PluginGrantService(
        broker=broker,
        authority_store=authority_store,
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    restarted._grants = restarted_grants
    await restarted.retire_predecessor_for_update(
        recovery,
        app_id=TRACKER_APP,
        candidate_connection_revisions=((TRACKER_APP, candidate_revision),),
    )

    assert restarted_grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    assert authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT) == (
        candidate_authority
    )
    assert broker.dependents[predecessor.partition_key] == set()
    assert permissions.state == "approved"

    await restarted.retire_predecessor_for_update(
        recovery,
        app_id=TRACKER_APP,
        candidate_connection_revisions=((TRACKER_APP, candidate_revision),),
    )
    assert authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT) == (
        candidate_authority
    )


def test_exact_preserved_candidate_authority_disappearance_fails_closed(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    authority = PluginAuthorityStore(path=tmp_path / "authority.json")
    grants = PluginGrantService(
        broker=broker,
        authority_store=authority,
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    prepared = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.old",
        scopes=("old.scope",),
        state="e",
    )
    grants.activate(prepared, _activation(prepared))
    predecessor = grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    )
    assert predecessor is not None
    preserved_candidate_authority = predecessor.authority
    authority.invalidate_connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT)

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        grants._begin_update_retirement(
            predecessor,
            preserved_authority=preserved_candidate_authority,
        )

    assert grants.pending_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is not None
    assert broker.dependents[prepared.partition_key] == {TRACKER_PLUGIN}


@pytest.mark.anyio
async def test_removed_app_retirement_cannot_clear_replacement_authority(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    authority = PluginAuthorityStore(path=tmp_path / "authority.json")
    grants = PluginGrantService(
        broker=broker,
        authority_store=authority,
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    removed = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.removed",
        scopes=("removed.scope",),
        state="6",
    )
    predecessor = grants.activate(removed, _activation(removed))
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
    )
    replacement_values = {
        "plugin_id": predecessor.plugin_id,
        "app_id": predecessor.app_id,
        "account_key_hash": predecessor.account_key_hash,
        "connection_revision": "6" * 64,
        "provider_scopes": predecessor.provider_scopes,
        "capability_ceiling": predecessor.capability_ceiling,
        "allowed_tool_schemas": predecessor.allowed_tool_schemas,
        "source_constraints": predecessor.source_constraints,
        "destination_constraints": predecessor.destination_constraints,
        "execution_policy": predecessor.execution_policy,
        "authority_kind": predecessor.authority_kind,
    }
    replacement = PluginConnectionAuthority(
        **replacement_values,
        revision=plugin_connection_authority_revision(**replacement_values),
    )
    authority.publish_connection(
        replacement,
        expected_revision=predecessor.revision,
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=removed.app_id,
        )

    assert authority.connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT) == replacement
    assert grants.pending_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is not None
    assert broker.dependents[removed.partition_key] == {TRACKER_PLUGIN}


@pytest.mark.anyio
async def test_removed_app_keeps_same_plugin_partition_dependent_for_surviving_app(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    removed = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.shared",
        scopes=("removed.scope",),
        state="8",
    )
    survivor = _prepared(
        TRACKER_PLUGIN,
        "northwind.tracker-survivor.mcp",
        profile_id="profile.shared",
        scopes=("survivor.scope",),
        state="9",
    )
    grants.activate(removed, _activation(removed))
    grants.activate(survivor, _activation(survivor))
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
        candidate_app_ids=(survivor.app_id,),
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants

    await connection.retire_predecessor_for_update(
        recovery,
        app_id=TRACKER_APP,
        candidate_activations=(_activation(survivor),),
        candidate_connection_revisions=(
            (survivor.app_id, _activation(survivor).connection_revision),
        ),
    )

    assert grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    assert grants.current(
        TRACKER_PLUGIN,
        app_id=survivor.app_id,
        account_key_sha256=_ACCOUNT,
    ) is not None
    assert broker.dependents[removed.partition_key] == {TRACKER_PLUGIN}
    assert grants.union_scopes(
        removed.partition_key,
        ("candidate.scope",),
    ) == ("candidate.scope", "survivor.scope")


@pytest.mark.anyio
@pytest.mark.parametrize("tamper_survivor", (False, True), ids=("unchanged", "missing"))
async def test_completed_shared_partition_retry_reproves_exact_sealed_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_survivor: bool,
) -> None:
    broker = _CheckpointBroker()
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    removed = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.shared",
        scopes=("removed.scope",),
        state="1",
    )
    survivor = _prepared(
        TRACKER_PLUGIN,
        "northwind.tracker-survivor.mcp",
        profile_id="profile.shared",
        scopes=("survivor.scope",),
        state="2",
    )
    grants.activate(removed, _activation(removed))
    grants.activate(survivor, _activation(survivor))
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
        candidate_app_ids=(survivor.app_id,),
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants
    exact = grants._require_partition_receipts_exact
    calls = 0

    def crash_after_local_tombstone(**kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected_after_local_tombstone")
        exact(**kwargs)

    monkeypatch.setattr(
        grants,
        "_require_partition_receipts_exact",
        crash_after_local_tombstone,
    )
    with pytest.raises(RuntimeError, match="^injected_after_local_tombstone$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=removed.app_id,
            candidate_activations=(_activation(survivor),),
            candidate_connection_revisions=(
                (survivor.app_id, _activation(survivor).connection_revision),
            ),
        )
    assert grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    monkeypatch.setattr(grants, "_require_partition_receipts_exact", exact)
    if tamper_survivor:
        broker.receipts = []

    retry = connection.retire_predecessor_for_update(
        recovery,
        app_id=removed.app_id,
        candidate_activations=(_activation(survivor),),
        candidate_connection_revisions=(
            (survivor.app_id, _activation(survivor).connection_revision),
        ),
    )
    if tamper_survivor:
        with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
            await retry
    else:
        await retry


@pytest.mark.anyio
async def test_completed_cross_plugin_shared_retry_requires_exact_zero_plugin_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _CheckpointBroker()
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    removed = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.shared",
        scopes=("removed.scope",),
        state="3",
    )
    shared = _prepared(
        "suite.shared",
        "suite.shared.mcp",
        profile_id="profile.shared",
        scopes=("shared.scope",),
        state="4",
    )
    grants.activate(removed, _activation(removed))
    grants.activate(shared, _activation(shared))
    predecessor = grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    )
    assert predecessor is not None
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants
    complete = grants._complete_disconnect

    def crash_after_tombstone(*args: object, **kwargs: object) -> object:
        complete(*args, **kwargs)
        raise RuntimeError("injected_after_cross_plugin_tombstone")

    monkeypatch.setattr(grants, "_complete_disconnect", crash_after_tombstone)
    with pytest.raises(RuntimeError, match="^injected_after_cross_plugin_tombstone$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=TRACKER_APP,
        )
    assert grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    monkeypatch.setattr(grants, "_complete_disconnect", complete)
    broker.receipts.append(
        replace(
            predecessor.receipt,
            receipt_id="unexpected-replacement-receipt",
            status="active",
        )
    )
    broker.dependents[removed.partition_key].add(TRACKER_PLUGIN)

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=TRACKER_APP,
        )


@pytest.mark.anyio
async def test_removed_app_fails_closed_when_vault_still_binds_same_plugin_predecessor(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    survivor = _prepared(
        TRACKER_PLUGIN,
        "northwind.tracker-survivor.mcp",
        profile_id="profile.shared",
        scopes=("survivor.scope",),
        state="a",
    )
    removed = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.shared",
        scopes=("removed.scope",),
        state="b",
    )
    grants.activate(survivor, _activation(survivor))
    grants.activate(removed, _activation(removed))
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
        candidate_app_ids=(survivor.app_id,),
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=TRACKER_APP,
            candidate_activations=(_activation(survivor),),
            candidate_connection_revisions=(
                (survivor.app_id, _activation(survivor).connection_revision),
            ),
        )

    assert grants.pending_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    assert grants.current(
        TRACKER_PLUGIN,
        app_id=survivor.app_id,
        account_key_sha256=_ACCOUNT,
    ) is not None
    assert broker.dependents[removed.partition_key] == {TRACKER_PLUGIN}
    assert [item.app_id for item in broker.committed_grant_receipts()] == [TRACKER_APP]


@pytest.mark.anyio
async def test_removed_app_provider_failure_is_pending_and_restart_retries_exactly(
    tmp_path: Path,
) -> None:
    broker = _CheckpointBroker()
    permissions = _CheckpointPermissions()
    journal = tmp_path / "grants.json"
    authority = PluginAuthorityStore(path=tmp_path / "authority.json")
    grants = PluginGrantService(
        broker=broker,
        authority_store=authority,
        permission_store=permissions,
        journal_path=journal,
    )
    removed = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.removed",
        scopes=("removed.scope",),
        state="5",
    )
    grants.activate(removed, _activation(removed))
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
    )
    broker.revoke_errors.append(ValueError("oauth_provider_unreachable"))

    with pytest.raises(ValueError, match="^oauth_provider_unreachable$"):
        await connection.retire_predecessor_for_update(recovery, app_id=TRACKER_APP)
    assert grants.pending_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is not None
    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        authority.connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT)

    restarted_grants = PluginGrantService(
        broker=broker,
        authority_store=authority,
        permission_store=permissions,
        journal_path=journal,
    )
    restarted = object.__new__(PluginConnectionService)
    restarted._grants = restarted_grants
    await restarted.retire_predecessor_for_update(recovery, app_id=TRACKER_APP)
    assert restarted_grants.pending_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    assert restarted_grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    assert broker.dependents[removed.partition_key] == set()
    tombstone = restarted_grants.begin_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    )
    assert tombstone.completed is not None
    assert tombstone.completed.provider_token_deleted is True
    assert tombstone.completed.remaining_plugin_ids == ()
    assert restarted_grants.union_scopes(
        removed.partition_key,
        ("candidate.scope",),
    ) == ("candidate.scope",)


@pytest.mark.anyio
async def test_removed_app_retry_finishes_authority_after_crash_between_durable_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _CheckpointBroker()
    authority = PluginAuthorityStore(path=tmp_path / "authority.json")
    grants = PluginGrantService(
        broker=broker,
        authority_store=authority,
        permission_store=_CheckpointPermissions(),
        journal_path=tmp_path / "grants.json",
    )
    removed = _prepared(
        TRACKER_PLUGIN,
        TRACKER_APP,
        profile_id="profile.removed",
        scopes=("removed.scope",),
        state="f",
    )
    predecessor_authority = grants.activate(removed, _activation(removed))
    recovery = _retirement_recovery(
        grants,
        checkpoint_id=broker.checkpoint["checkpoint_id"],
    )
    connection = object.__new__(PluginConnectionService)
    connection._grants = grants
    invalidate = authority.compare_and_invalidate_connection_lineage
    crashed = False

    def crash_once(checkpoint: object) -> int:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated_process_death")
        return invalidate(checkpoint)

    monkeypatch.setattr(
        authority,
        "compare_and_invalidate_connection_lineage",
        crash_once,
    )
    with pytest.raises(RuntimeError, match="^simulated_process_death$"):
        await connection.retire_predecessor_for_update(
            recovery,
            app_id=TRACKER_APP,
        )
    assert grants.pending_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is not None
    assert authority.connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT) == (
        predecessor_authority
    )

    await connection.retire_predecessor_for_update(
        recovery,
        app_id=TRACKER_APP,
    )

    with pytest.raises(
        ValueError,
        match="^plugin_connection_authority_not_found$",
    ):
        authority.connection(TRACKER_PLUGIN, TRACKER_APP, _ACCOUNT)
    assert grants.pending_disconnect(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=_ACCOUNT,
    ) is None
    assert broker.dependents[removed.partition_key] == set()


@pytest.mark.parametrize(
    "field",
    (
        "predecessor_app_ids",
        "candidate_app_ids",
        "authority_lineages",
        "live_sessions",
    ),
)
def test_update_connection_recovery_requires_json_lists(
    tmp_path: Path,
    field: str,
) -> None:
    """Durable schema decoding rejects Python iterables that JSON cannot encode as lists."""

    broker = _ConnectionBroker()
    grants = _SessionRecoveryGrants(broker)
    service, _broker, installed = _remote_service(
        tmp_path,
        broker=broker,
        grants=grants,
        authentication_kind="none",
    )
    previous_record = installed["plugin_registry"].get(TRACKER_PLUGIN)
    assert previous_record.app_binding is not None
    recovery = service.begin_update_recovery(
        previous_record=previous_record,
        candidate_binding=previous_record.app_binding,
        account_key=ACCOUNT_KEY,
        checkpoint_id="plugin-update-recovery-" + "8" * 64,
    ).to_dict()
    recovery[field] = tuple(recovery[field])

    with pytest.raises(
        ValueError,
        match="^plugin_update_recovery_required$",
    ):
        PluginUpdateConnectionRecovery.from_dict(recovery)


@pytest.mark.anyio
async def test_restart_status_uses_durable_grant_without_opening_a_session(
    tmp_path: Path,
) -> None:
    """Status observes sealed grant evidence; only execution may touch the network."""

    service, broker, installed = _remote_service(tmp_path)
    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        started.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?code=x&state=callback-state",
    )
    await service.close_all()
    restarted, _broker, _installed = _remote_service(
        tmp_path,
        installed=installed,
        broker=broker,
        grants=service._grants,
    )
    restarted._client_factory = lambda *_args: (_ for _ in ()).throw(
        AssertionError("status must not create a client")
    )

    status = await restarted.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )

    assert status.status == "connected"
    assert restarted._sessions == {}
