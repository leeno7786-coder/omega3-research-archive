from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_broker import (
    OAuthConnectionCommit,
    OAuthPreparedAuthorization,
    OAuthRevocationResult,
)
from mcp_platform.oauth_vault import (
    OAuthCommittedGrant,
    OAuthGrantReceiptClaimOutcome,
)
from mcp_platform.plugin_authority import (
    PluginAuthorityStore,
    PluginCapabilityBindingIdentity,
    PluginExecutionPolicy,
)
from mcp_platform.plugin_grants import PluginGrantActivation, PluginGrantService


_ACCOUNT = "a" * 64
_PARTITION = "b" * 64
_PROFILE_REVISION = "sha256:" + "c" * 64


@dataclass
class _Permissions:
    invalidated: list[str]

    def invalidate_plugin(self, plugin_id: str) -> int:
        self.invalidated.append(plugin_id)
        return 1


class _Broker:
    def __init__(self) -> None:
        self.receipts: list[OAuthCommittedGrant] = []
        self.claimed: list[tuple[str, str]] = []
        self.dependents: dict[str, set[str]] = {}
        self.revoke_errors: list[BaseException] = []

    def commit(
        self,
        prepared: OAuthPreparedAuthorization,
        commit: OAuthConnectionCommit,
    ) -> OAuthCommittedGrant:
        receipt = OAuthCommittedGrant(
            receipt_id=sha256_hex(
                canonical_bytes(
                    {
                        "connection_revision": commit.connection_revision,
                        "state_sha256": prepared.state_sha256,
                    }
                )
            ),
            plugin_id=prepared.plugin_id,
            app_id=prepared.app_id,
            account_key_sha256=prepared.account_key_sha256,
            profile_id=prepared.profile_id,
            profile_revision=prepared.profile_revision,
            token_partition_key=prepared.partition_key,
            connection_revision=commit.connection_revision,
            capability_revision=commit.capability_revision,
            authority_revision=commit.authority_revision,
            capability_binding_revision=commit.capability_binding_revision,
            scopes=prepared.scopes,
            execution_policy=commit.execution_policy,
            connection_generation=commit.connection_generation,
        )
        self.receipts.append(receipt)
        self.dependents.setdefault(prepared.partition_key, set()).add(prepared.plugin_id)
        return receipt

    def unclaimed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        claimed = {receipt_id for receipt_id, _revision in self.claimed}
        return tuple(item for item in self.receipts if item.receipt_id not in claimed)

    def claim_grant_receipt(self, receipt_id: str, authority_revision: str) -> None:
        self.claimed.append((receipt_id, authority_revision))

    def committed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        claimed = dict(self.claimed)
        return tuple(
            replace(
                item,
                claimed_authority_revision=claimed.get(
                    item.receipt_id,
                    item.claimed_authority_revision,
                ),
            )
            for item in self.receipts
        )

    def grant_receipt_state(self, expected: OAuthCommittedGrant) -> str:
        matches = tuple(
            item for item in self.committed_grant_receipts() if item.receipt_id == expected.receipt_id
        )
        if not matches:
            return "absent"
        if len(matches) != 1 or replace(
            matches[0],
            claimed_authority_revision=None,
        ) != replace(expected, claimed_authority_revision=None):
            raise ValueError("oauth_grant_receipt_conflict")
        return (
            "claimed"
            if matches[0].claimed_authority_revision is not None
            else "unclaimed"
        )

    def claim_grant_receipt_exact(
        self,
        expected: OAuthCommittedGrant,
        authority_revision: str,
    ) -> OAuthGrantReceiptClaimOutcome:
        if (
            expected.status != "active"
            or expected.claimed_authority_revision is not None
            or authority_revision != expected.authority_revision
        ):
            raise ValueError("oauth_grant_receipt_claim_conflict")
        matches = tuple(
            item for item in self.committed_grant_receipts() if item.receipt_id == expected.receipt_id
        )
        if not matches:
            return OAuthGrantReceiptClaimOutcome.ABSENT
        if len(matches) != 1 or replace(
            matches[0],
            status=expected.status,
            claimed_authority_revision=None,
        ) != expected:
            raise ValueError("oauth_grant_receipt_conflict")
        stored = matches[0]
        if stored.claimed_authority_revision is not None:
            if stored.claimed_authority_revision != authority_revision:
                raise ValueError("oauth_grant_receipt_claim_conflict")
            return OAuthGrantReceiptClaimOutcome.IDEMPOTENTLY_CLAIMED
        if stored.status != "active":
            raise ValueError("oauth_grant_receipt_conflict")
        self.claim_grant_receipt(expected.receipt_id, authority_revision)
        return OAuthGrantReceiptClaimOutcome.CLAIMED

    def maximum_connection_generation(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> int | None:
        generations = tuple(
            receipt.connection_generation
            for receipt in self.receipts
            if (
                receipt.plugin_id,
                receipt.app_id,
                receipt.account_key_sha256,
            )
            == (plugin_id, app_id, account_key_sha256)
        )
        return max(generations) if generations else None

    async def revoke(self, partition_key: str, *, plugin_id: str) -> OAuthRevocationResult:
        if self.revoke_errors:
            raise self.revoke_errors.pop(0)
        remaining = self.dependents[partition_key]
        remaining.discard(plugin_id)
        return OAuthRevocationResult(
            revoked_provider_token=not remaining,
            dependents=tuple(sorted(remaining)),
            local_token_deleted=not remaining,
        )


def _prepared(
    plugin_id: str,
    app_id: str,
    *,
    profile_id: str,
    scopes: tuple[str, ...],
    state: str,
) -> OAuthPreparedAuthorization:
    return OAuthPreparedAuthorization(
        profile_id=profile_id,
        plugin_id=plugin_id,
        app_id=app_id,
        account_key_sha256=_ACCOUNT,
        state_sha256=state * 64,
        partition_key=_PARTITION,
        profile_revision=_PROFILE_REVISION,
        authorization_context_revision="d" * 64,
        scopes=scopes,
    )


def _activation(prepared: OAuthPreparedAuthorization) -> PluginGrantActivation:
    capability = prepared.plugin_id.rsplit(".", 1)[-1]
    schema = sha256_hex(canonical_bytes({"type": "object"}))
    tool_digest = sha256_hex(canonical_bytes({"tool": capability}))
    return PluginGrantActivation(
        plugin_id=prepared.plugin_id,
        app_id=prepared.app_id,
        account_key_sha256=prepared.account_key_sha256,
        profile_id=prepared.profile_id,
        profile_revision=prepared.profile_revision,
        token_partition_key=prepared.partition_key,
        connection_revision=sha256_hex(
            canonical_bytes({"app_id": prepared.app_id, "profile": prepared.profile_id})
        ),
        capability_revision="e" * 64,
        scopes=prepared.scopes,
        capability_ceiling=(f"tool:{capability}",),
        allowed_tool_schemas={capability: schema},
        source_constraints={},
        destination_constraints={},
        capability_binding_identities=(
            PluginCapabilityBindingIdentity(
                capability_kind="tool",
                binding_sha256=sha256_hex(
                    canonical_bytes({"tool_binding_digests": [tool_digest]})
                ),
            ),
        ),
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
    )


@pytest.fixture
def grants(tmp_path: Path) -> tuple[PluginGrantService, _Broker, _Permissions]:
    broker = _Broker()
    permissions = _Permissions([])
    service = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    return service, broker, permissions


@pytest.mark.anyio
async def test_shared_token_survives_until_last_generic_dependent_leaves(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, _permissions = grants
    first = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="1",
    )
    second = _prepared(
        "suite.beta",
        "suite.beta.mcp",
        profile_id="profile.beta",
        scopes=("mail.read",),
        state="2",
    )
    service.activate(first, _activation(first))
    service.activate(second, _activation(second))

    left = await service.disconnect("suite.alpha", account_key_sha256=_ACCOUNT)
    assert left.provider_token_deleted is False
    assert left.remaining_plugin_ids == ("suite.beta",)
    final = await service.disconnect("suite.beta", account_key_sha256=_ACCOUNT)
    assert final.provider_token_deleted is True
    assert final.remaining_plugin_ids == ()


def test_scope_union_is_partition_based_not_profile_or_issuer_named(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, _permissions = grants
    first = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="3",
    )
    service.activate(first, _activation(first))

    assert service.union_scopes(_PARTITION, ("mail.read",)) == (
        "files.read",
        "mail.read",
    )


def test_reconcile_publishes_and_claims_receipt_after_commit_crash(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, broker, permissions = grants
    prepared = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="4",
    )
    activation = _activation(prepared)
    original = service.authority_store.publish_connection
    failures = 1

    def _crash_once(authority, *, expected_revision):
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("simulated_crash")
        return original(authority, expected_revision=expected_revision)

    monkeypatch.setattr(service.authority_store, "publish_connection", _crash_once)
    with pytest.raises(RuntimeError, match="simulated_crash"):
        service.activate(prepared, activation)
    assert broker.unclaimed_grant_receipts()

    recovered = PluginGrantService(
        broker=broker,
        authority_store=service.authority_store,
        permission_store=permissions,
        journal_path=service.journal_path,
    ).reconcile()

    assert len(recovered) == 1
    assert recovered[0].revision == activation.authority_revision
    assert broker.unclaimed_grant_receipts() == ()


def test_reconcile_claims_receipt_after_publication_crash(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, broker, permissions = grants
    prepared = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="b",
    )
    activation = _activation(prepared)
    original = broker.claim_grant_receipt
    failures = 1

    def _crash_once(receipt_id: str, authority_revision: str) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("simulated_claim_crash")
        original(receipt_id, authority_revision)

    monkeypatch.setattr(broker, "claim_grant_receipt", _crash_once)
    with pytest.raises(RuntimeError, match="simulated_claim_crash"):
        service.activate(prepared, activation)

    recovered = PluginGrantService(
        broker=broker,
        authority_store=service.authority_store,
        permission_store=permissions,
        journal_path=service.journal_path,
    ).reconcile()

    assert recovered == (activation.authority(),)
    assert broker.unclaimed_grant_receipts() == ()


def test_activation_preserves_per_plugin_execution_policy(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, _permissions = grants
    prepared = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="5",
    )
    activation = _activation(prepared)

    authority = service.activate(prepared, activation)

    assert authority.execution_policy is PluginExecutionPolicy.DIRECT_TURN
    assert authority.capability_ceiling == ("tool:alpha",)


def test_compatible_update_rebind_preserves_exact_customer_identity(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, _permissions = grants
    prepared = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="1",
    )
    activation = _activation(prepared)
    service.activate(prepared, activation)
    before = service.current(
        "suite.alpha",
        app_id="suite.alpha.mcp",
        account_key_sha256=_ACCOUNT,
    )
    assert before is not None

    rebound = service.rebind_compatible_update(
        "suite.alpha",
        app_id="suite.alpha.mcp",
        account_key_sha256=_ACCOUNT,
        package_revision="f" * 64,
    )
    assert rebound.activation.package_revision == "f" * 64
    assert rebound.activation.profile_id == before.activation.profile_id
    assert rebound.activation.profile_revision == before.activation.profile_revision
    assert rebound.activation.token_partition_key == before.activation.token_partition_key
    assert rebound.activation.account_key_sha256 == before.activation.account_key_sha256


@pytest.mark.anyio
async def test_compatible_rebind_retirement_keeps_the_live_receipt(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, broker, _permissions = grants
    prepared = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="c",
    )
    service.activate(prepared, _activation(prepared))
    predecessor = service.current(
        "suite.alpha", app_id="suite.alpha.mcp", account_key_sha256=_ACCOUNT
    )
    assert predecessor is not None
    candidate = service.rebind_compatible_update(
        "suite.alpha",
        app_id="suite.alpha.mcp",
        account_key_sha256=_ACCOUNT,
        package_revision="f" * 64,
    )
    recovery = {
        "plugin_id": "suite.alpha",
        "account_key_sha256": _ACCOUNT,
        "predecessor_grants": [predecessor.to_dict()],
    }
    monkeypatch.setattr(service, "_update_recovery", lambda _value: recovery)
    monkeypatch.setattr(
        service,
        "_candidate_records_for_update",
        lambda **_kwargs: (candidate,),
    )
    revoke_calls: list[tuple[str, str]] = []

    async def revoke(partition_key: str, *, plugin_id: str) -> OAuthRevocationResult:
        revoke_calls.append((partition_key, plugin_id))
        return OAuthRevocationResult(False, (plugin_id,))

    monkeypatch.setattr(broker, "revoke", revoke)
    result = await service.retire_update_predecessor(
        recovery,
        app_id="suite.alpha.mcp",
        candidate_survives=True,
        candidate_app_ids=("suite.alpha.mcp",),
        candidate_activations=(candidate.activation,),
    )
    assert result is True
    assert revoke_calls == []
    assert service.current(
        "suite.alpha", app_id="suite.alpha.mcp", account_key_sha256=_ACCOUNT
    ) == candidate


def test_reconcile_retires_an_older_crash_receipt_after_newer_activation(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, broker, permissions = grants
    older = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="6",
    )
    newer = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.write",),
        state="7",
    )
    original = service.authority_store.publish_connection
    failures = 1

    def _crash_once(authority, *, expected_revision):
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("simulated_old_publish_crash")
        return original(authority, expected_revision=expected_revision)

    monkeypatch.setattr(service.authority_store, "publish_connection", _crash_once)
    with pytest.raises(RuntimeError, match="simulated_old_publish_crash"):
        service.activate(older, _activation(older))
    newer_authority = service.activate(newer, _activation(newer))

    recovered = PluginGrantService(
        broker=broker,
        authority_store=service.authority_store,
        permission_store=permissions,
        journal_path=service.journal_path,
    ).reconcile()

    current = service.authority_store.connection("suite.alpha", "suite.alpha.mcp", _ACCOUNT)
    assert recovered == ()
    assert current == newer_authority
    assert current.provider_scopes == ("files.write",)
    assert broker.unclaimed_grant_receipts() == ()


@pytest.mark.anyio
async def test_revocation_pending_survives_restart_and_retries_provider_failure(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, broker, permissions = grants
    prepared = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="8",
    )
    service.activate(prepared, _activation(prepared))
    broker.revoke_errors.append(ValueError("oauth_provider_unreachable"))

    with pytest.raises(ValueError, match="^oauth_provider_unreachable$"):
        await service.disconnect("suite.alpha", account_key_sha256=_ACCOUNT)
    assert (
        service.current("suite.alpha", app_id="suite.alpha.mcp", account_key_sha256=_ACCOUNT)
        is None
    )

    restarted = PluginGrantService(
        broker=broker,
        authority_store=service.authority_store,
        permission_store=permissions,
        journal_path=service.journal_path,
    )
    pending = restarted.pending_disconnect(
        "suite.alpha",
        app_id="suite.alpha.mcp",
        account_key_sha256=_ACCOUNT,
    )
    assert pending is not None
    result = await restarted.finish_disconnect(pending)
    repeated = await restarted.disconnect("suite.alpha", account_key_sha256=_ACCOUNT)

    assert result.provider_token_deleted is True
    assert result.remaining_plugin_ids == ()
    assert repeated == result


def test_fresh_grant_is_not_reported_as_a_pending_disconnect(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, _permissions = grants
    prepared = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="a",
    )
    service.activate(prepared, _activation(prepared))

    assert (
        service.pending_disconnect(
            "suite.alpha",
            app_id="suite.alpha.mcp",
            account_key_sha256=_ACCOUNT,
        )
        is None
    )


def test_multi_app_disconnect_selects_the_exact_bound_app(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, _broker, _permissions = grants
    mail = _prepared(
        "suite.office",
        "suite.office.mail",
        profile_id="profile.mail",
        scopes=("mail.read",),
        state="d",
    )
    calendar = _prepared(
        "suite.office",
        "suite.office.calendar",
        profile_id="profile.calendar",
        scopes=("calendar.read",),
        state="e",
    )
    service.activate(mail, _activation(mail))
    service.activate(calendar, _activation(calendar))

    pending = service.begin_disconnect(
        "suite.office",
        app_id="suite.office.mail",
        account_key_sha256=_ACCOUNT,
    )

    assert pending.app_id == "suite.office.mail"
    assert (
        service.current(
            "suite.office",
            app_id="suite.office.calendar",
            account_key_sha256=_ACCOUNT,
        )
        is not None
    )


def test_schema_one_crash_receipt_normalizes_before_reconciliation(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, broker, permissions = grants
    prepared = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="9",
    )
    activation = _activation(prepared)
    legacy_activation = activation.to_dict()
    for field in (
        "authorization_context_revision",
        "descriptor_revision",
        "package_revision",
        "attempt_state_sha256",
    ):
        legacy_activation.pop(field)
    receipt = OAuthCommittedGrant(
        receipt_id="9" * 64,
        plugin_id=prepared.plugin_id,
        app_id=prepared.app_id,
        account_key_sha256=prepared.account_key_sha256,
        profile_id=prepared.profile_id,
        profile_revision=prepared.profile_revision,
        token_partition_key=prepared.partition_key,
        connection_revision=activation.connection_revision,
        capability_revision=activation.capability_revision,
        authority_revision=activation.authority_revision,
        capability_binding_revision=activation.capability_binding_revision,
        scopes=activation.scopes,
        execution_policy=activation.execution_policy.value,
    )
    broker.receipts.append(receipt)
    service.journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "staged": {prepared.state_sha256: legacy_activation},
                "active": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    recovered = PluginGrantService(
        broker=broker,
        authority_store=service.authority_store,
        permission_store=permissions,
        journal_path=service.journal_path,
    ).reconcile()

    assert recovered == (activation.authority(),)
    migrated = json.loads(service.journal_path.read_text(encoding="utf-8"))
    stored = migrated["active"][service._key(prepared.plugin_id, prepared.app_id, _ACCOUNT)]
    assert stored["activation"] == activation.to_dict()
    assert stored["receipt"]["connection_generation"] == 0
    assert stored["receipt"]["status"] == "active"
    assert broker.unclaimed_grant_receipts() == ()


def test_missing_grant_journal_uses_broker_generation_floor(
    grants: tuple[PluginGrantService, _Broker, _Permissions],
) -> None:
    service, broker, permissions = grants
    first = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.read",),
        state="a",
    )
    service.activate(first, _activation(first))
    first_generation = broker.receipts[-1].connection_generation
    service.journal_path.unlink()
    second = _prepared(
        "suite.alpha",
        "suite.alpha.mcp",
        profile_id="profile.alpha",
        scopes=("files.write",),
        state="c",
    )
    restarted = PluginGrantService(
        broker=broker,
        authority_store=service.authority_store,
        permission_store=permissions,
        journal_path=service.journal_path,
    )

    restarted.activate(second, _activation(second))

    assert first_generation == 1
    assert broker.receipts[-1].connection_generation == 2
