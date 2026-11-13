from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_broker import OAuthConnectionCommit, OAuthPreparedAuthorization
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
        self.claim_errors: dict[str, BaseException] = {}
        self.after_unclaimed_snapshot: object | None = None

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
        return receipt

    def unclaimed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        claimed = {receipt_id for receipt_id, _revision in self.claimed}
        snapshot = tuple(item for item in self.receipts if item.receipt_id not in claimed)
        hook = self.after_unclaimed_snapshot
        self.after_unclaimed_snapshot = None
        if callable(hook):
            hook()
        return snapshot

    def claim_grant_receipt(self, receipt_id: str, authority_revision: str) -> None:
        error = self.claim_errors.pop(receipt_id, None)
        if error is not None:
            raise error
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
            if (receipt.plugin_id, receipt.app_id, receipt.account_key_sha256)
            == (plugin_id, app_id, account_key_sha256)
        )
        return max(generations) if generations else None

    async def revoke(self, _partition_key: str, *, plugin_id: str) -> object:
        raise AssertionError(f"unexpected revoke for {plugin_id}")


def _prepared(plugin_id: str, *, state: str) -> OAuthPreparedAuthorization:
    return OAuthPreparedAuthorization(
        profile_id=f"profile.{plugin_id}",
        plugin_id=plugin_id,
        app_id=f"{plugin_id}.mcp",
        account_key_sha256=_ACCOUNT,
        state_sha256=state * 64,
        partition_key=_PARTITION,
        profile_revision=_PROFILE_REVISION,
        authorization_context_revision="d" * 64,
        scopes=("files.read",),
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


def _service(
    root: Path,
    *,
    broker: _Broker,
    authority_store: PluginAuthorityStore | None = None,
) -> PluginGrantService:
    return PluginGrantService(
        broker=broker,
        authority_store=authority_store or PluginAuthorityStore(path=root / "authority.json"),
        permission_store=_Permissions([]),
        journal_path=root / "grants.json",
    )


def _legacy_authority_revision(activation: PluginGrantActivation) -> str:
    """Reproduce the exact pre-authentication-kind authority revision."""

    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": activation.plugin_id,
                "app_id": activation.app_id,
                "account_key_hash": activation.account_key_sha256,
                "connection_revision": activation.connection_revision,
                "provider_scopes": list(activation.scopes),
                "capability_ceiling": list(activation.capability_ceiling),
                "allowed_tool_schemas": dict(activation.allowed_tool_schemas),
                "source_constraints": dict(activation.source_constraints),
                "destination_constraints": dict(activation.destination_constraints),
                "execution_policy": activation.execution_policy.value,
            }
        )
    )


def _schema_five_activation_document(
    activation: PluginGrantActivation,
    *,
    authority_revision: str | None = None,
) -> dict[str, object]:
    capability = activation.capability_ceiling[0].removeprefix("tool:")
    tool_digest = sha256_hex(canonical_bytes({"tool": capability}))
    tool_revision = sha256_hex(
        canonical_bytes({"tool_binding_digests": [tool_digest]})
    )
    assert activation.capability_binding_identities == (
        PluginCapabilityBindingIdentity("tool", tool_revision),
    )
    document = activation.to_dict()
    document.pop("capability_binding_identities")
    document.pop("capability_binding_revision")
    document["tool_binding_digests"] = [tool_digest]
    document["tool_binding_revision"] = tool_revision
    if authority_revision is not None:
        document["authority_revision"] = authority_revision
    return document


def _schema_four_receipt_binding_revision(
    activation: PluginGrantActivation,
    generation: int,
) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": activation.plugin_id,
                "app_id": activation.app_id,
                "account_key_sha256": activation.account_key_sha256,
                "profile_id": activation.profile_id,
                "profile_revision": activation.profile_revision,
                "token_partition_key": activation.token_partition_key,
                "connection_revision": activation.connection_revision,
                "capability_revision": activation.capability_revision,
                "authority_revision": _legacy_authority_revision(activation),
                "tool_binding_revision": activation.capability_binding_revision,
                "scopes": list(activation.scopes),
                "execution_policy": activation.execution_policy.value,
                "connection_generation": generation,
            }
        )
    )


def _legacy_receipt(
    prepared: OAuthPreparedAuthorization,
    activation: PluginGrantActivation,
    generation: int,
) -> OAuthCommittedGrant:
    return OAuthCommittedGrant(
        receipt_id=sha256_hex(
            canonical_bytes(
                {
                    "connection_revision": activation.connection_revision,
                    "state_sha256": prepared.state_sha256,
                }
            )
        ),
        plugin_id=activation.plugin_id,
        app_id=activation.app_id,
        account_key_sha256=activation.account_key_sha256,
        profile_id=activation.profile_id,
        profile_revision=activation.profile_revision,
        token_partition_key=activation.token_partition_key,
        connection_revision=activation.connection_revision,
        capability_revision=activation.capability_revision,
        authority_revision=_legacy_authority_revision(activation),
        capability_binding_revision=activation.capability_binding_revision,
        scopes=activation.scopes,
        execution_policy=activation.execution_policy.value,
        connection_generation=generation,
    )


def _receipt_document(receipt: OAuthCommittedGrant) -> dict[str, object]:
    return {
        "receipt_id": receipt.receipt_id,
        "plugin_id": receipt.plugin_id,
        "app_id": receipt.app_id,
        "account_key_sha256": receipt.account_key_sha256,
        "profile_id": receipt.profile_id,
        "profile_revision": receipt.profile_revision,
        "token_partition_key": receipt.token_partition_key,
        "connection_revision": receipt.connection_revision,
        "capability_revision": receipt.capability_revision,
        "authority_revision": receipt.authority_revision,
        "capability_binding_revision": receipt.capability_binding_revision,
        "scopes": list(receipt.scopes),
        "execution_policy": receipt.execution_policy,
        "status": receipt.status,
        "claimed_authority_revision": None,
        "connection_generation": receipt.connection_generation,
    }


def _exact_receipt_binding_revision(receipt: OAuthCommittedGrant) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "receipt_id": receipt.receipt_id,
                "plugin_id": receipt.plugin_id,
                "app_id": receipt.app_id,
                "account_key_sha256": receipt.account_key_sha256,
                "profile_id": receipt.profile_id,
                "profile_revision": receipt.profile_revision,
                "token_partition_key": receipt.token_partition_key,
                "connection_revision": receipt.connection_revision,
                "capability_revision": receipt.capability_revision,
                "authority_revision": receipt.authority_revision,
                "capability_binding_revision": receipt.capability_binding_revision,
                "scopes": list(receipt.scopes),
                "execution_policy": receipt.execution_policy,
                "connection_generation": receipt.connection_generation,
            }
        )
    )


def _retired_fact(receipt: OAuthCommittedGrant) -> dict[str, object]:
    return {
        "generation": receipt.connection_generation,
        "authority_revision": receipt.authority_revision,
        "receipt_binding_revision": _exact_receipt_binding_revision(receipt),
        "receipt": _receipt_document(receipt),
    }


def _rewrite_active_as_legacy(
    service: PluginGrantService,
    activation: PluginGrantActivation,
) -> bytes:
    document = json.loads(service.journal_path.read_text(encoding="utf-8"))
    key = service._key(activation.plugin_id, activation.app_id, activation.account_key_sha256)
    legacy_revision = _legacy_authority_revision(activation)
    document["schema_version"] = 2
    document.pop("retired_legacy", None)
    document["active"][key]["activation"] = _schema_five_activation_document(
        activation,
        authority_revision=legacy_revision,
    )
    document["active"][key]["receipt"]["authority_revision"] = legacy_revision
    payload = canonical_bytes(document)
    service.journal_path.write_bytes(payload)
    return payload


def _rewrite_staged_as_legacy(
    service: PluginGrantService,
    prepared: OAuthPreparedAuthorization,
    activation: PluginGrantActivation,
) -> bytes:
    service._stage(prepared, activation)
    document = json.loads(service.journal_path.read_text(encoding="utf-8"))
    document["schema_version"] = 2
    document.pop("retired_legacy", None)
    document["staged"][prepared.state_sha256]["activation"] = (
        _schema_five_activation_document(
            activation,
            authority_revision=_legacy_authority_revision(activation),
        )
    )
    payload = canonical_bytes(document)
    service.journal_path.write_bytes(payload)
    return payload


def _make_broker_receipt_legacy(
    broker: _Broker,
    activation: PluginGrantActivation,
) -> OAuthCommittedGrant:
    legacy = replace(
        broker.receipts[-1],
        authority_revision=_legacy_authority_revision(activation),
        claimed_authority_revision=None,
    )
    broker.receipts[-1] = legacy
    broker.claimed.clear()
    return legacy


def _write_same_identity_legacy_history(
    service: PluginGrantService,
    *,
    active_prepared: OAuthPreparedAuthorization,
    staged_prepared: OAuthPreparedAuthorization,
    active_activation: PluginGrantActivation,
    staged_activation: PluginGrantActivation,
    active_generation: int,
    staged_generation: int,
) -> str:
    service.activate(active_prepared, active_activation)
    service._stage(staged_prepared, staged_activation)
    document = json.loads(service.journal_path.read_text(encoding="utf-8"))
    identity = service._key(
        active_activation.plugin_id,
        active_activation.app_id,
        active_activation.account_key_sha256,
    )
    active = document["active"][identity]
    active["generation"] = active_generation
    active["receipt"]["connection_generation"] = active_generation
    active["receipt"]["authority_revision"] = _legacy_authority_revision(
        active_activation
    )
    active["activation"] = _schema_five_activation_document(
        active_activation,
        authority_revision=_legacy_authority_revision(active_activation),
    )
    staged = document["staged"][staged_prepared.state_sha256]
    staged["generation"] = staged_generation
    staged["activation"] = _schema_five_activation_document(
        staged_activation,
        authority_revision=_legacy_authority_revision(staged_activation),
    )
    document["generations"][identity] = max(active_generation, staged_generation)
    document["schema_version"] = 2
    document.pop("retired_legacy", None)
    service.journal_path.write_bytes(canonical_bytes(document))
    return identity


def test_schema_two_legacy_oauth_activation_retires_to_reconnectable_state(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="1")
    activation = _activation(prepared)
    service.activate(prepared, activation)
    legacy_receipt = _make_broker_receipt_legacy(broker, activation)
    _rewrite_active_as_legacy(service, activation)

    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )

    assert (
        restarted.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    migrated = json.loads(service.journal_path.read_text(encoding="utf-8"))
    key = service._key(activation.plugin_id, activation.app_id, activation.account_key_sha256)
    assert migrated["schema_version"] == 6
    assert key not in migrated["active"]
    assert migrated["generations"][key] == 1
    assert len(migrated["retired_legacy"][key]) == 1
    retired = migrated["retired_legacy"][key][0]
    assert retired == _retired_fact(legacy_receipt)

    reconnect = _prepared("suite.legacy", state="7")
    reconnected = _activation(reconnect)
    assert restarted.activate(reconnect, reconnected) == reconnected.authority()
    rewritten = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert rewritten["generations"][key] == 2
    assert key in rewritten["retired_legacy"]
    assert restarted.reconcile() == ()
    cleaned = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert key not in cleaned["retired_legacy"]


def test_schema_two_mixed_journal_preserves_current_oauth_activation(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    legacy_prepared = _prepared("suite.legacy", state="2")
    current_prepared = _prepared("suite.current", state="3")
    legacy = _activation(legacy_prepared)
    current = _activation(current_prepared)
    service.activate(legacy_prepared, legacy)
    service.activate(current_prepared, current)
    _rewrite_active_as_legacy(service, legacy)

    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )

    assert (
        restarted.current(
            legacy.plugin_id,
            app_id=legacy.app_id,
            account_key_sha256=legacy.account_key_sha256,
        )
        is None
    )
    current_record = restarted.current(
        current.plugin_id,
        app_id=current.app_id,
        account_key_sha256=current.account_key_sha256,
    )
    assert current_record is not None
    assert current_record.activation == current


@pytest.mark.parametrize(
    ("active_generation", "staged_generation"),
    [(1, 2), (2, 1)],
)
def test_schema_two_same_identity_legacy_history_retires_every_generation(
    tmp_path: Path,
    active_generation: int,
    staged_generation: int,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    active_prepared = _prepared("suite.history", state="9")
    staged_prepared = _prepared("suite.history", state="a")
    active_activation = _activation(active_prepared)
    staged_activation = replace(
        _activation(staged_prepared),
        source_constraints={"generation": "staged"},
    )
    identity = _write_same_identity_legacy_history(
        service,
        active_prepared=active_prepared,
        staged_prepared=staged_prepared,
        active_activation=active_activation,
        staged_activation=staged_activation,
        active_generation=active_generation,
        staged_generation=staged_generation,
    )
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )

    assert (
        restarted.current(
            active_activation.plugin_id,
            app_id=active_activation.app_id,
            account_key_sha256=active_activation.account_key_sha256,
        )
        is None
    )
    migrated = json.loads(service.journal_path.read_text(encoding="utf-8"))
    facts = migrated["retired_legacy"][identity]
    assert [item["generation"] for item in facts] == [1, 2]
    assert len({item["receipt_binding_revision"] for item in facts}) == 2
    assert migrated["generations"][identity] == 2
    assert migrated["active"] == {}
    assert migrated["staged"] == {}


@pytest.mark.parametrize("conflicting_payload", [False, True])
def test_schema_two_same_generation_duplicate_legacy_history_is_rejected(
    tmp_path: Path,
    conflicting_payload: bool,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    active_prepared = _prepared("suite.duplicate", state="b")
    staged_prepared = _prepared("suite.duplicate", state="c")
    active_activation = _activation(active_prepared)
    staged_activation = _activation(staged_prepared)
    if conflicting_payload:
        staged_activation = replace(
            staged_activation,
            destination_constraints={"different": True},
        )
    _write_same_identity_legacy_history(
        service,
        active_prepared=active_prepared,
        staged_prepared=staged_prepared,
        active_activation=active_activation,
        staged_activation=staged_activation,
        active_generation=1,
        staged_generation=1,
    )
    before = service.journal_path.read_bytes()
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )

    with pytest.raises(ValueError, match="^plugin_grant_store_unavailable$"):
        restarted.current(
            active_activation.plugin_id,
            app_id=active_activation.app_id,
            account_key_sha256=active_activation.account_key_sha256,
        )

    assert service.journal_path.read_bytes() == before


def test_schema_four_retirement_history_has_exact_global_bound(tmp_path: Path) -> None:
    def _write_history(root: Path, count: int) -> PluginGrantService:
        root.mkdir()
        broker = _Broker()
        service = _service(root, broker=broker)
        identity = service._key("suite.bound", "suite.bound.mcp", _ACCOUNT)
        facts = [
            {
                "generation": generation,
                "authority_revision": sha256_hex(
                    canonical_bytes({"authority_generation": generation})
                ),
                "receipt_binding_revision": sha256_hex(
                    canonical_bytes({"receipt_generation": generation})
                ),
            }
            for generation in range(1, count + 1)
        ]
        service.journal_path.write_bytes(
            canonical_bytes(
                {
                    "schema_version": 4,
                    "staged": {},
                    "active": {},
                    "generations": {identity: count},
                    "disconnected": {},
                    "retired_legacy": {identity: facts},
                }
            )
        )
        return service

    maximum = _write_history(tmp_path / "maximum", 512)
    assert (
        maximum.current(
            "suite.bound",
            app_id="suite.bound.mcp",
            account_key_sha256=_ACCOUNT,
        )
        is None
    )
    oversized = _write_history(tmp_path / "oversized", 513)
    with pytest.raises(ValueError, match="^plugin_grant_store_unavailable$"):
        oversized.current(
            "suite.bound",
            app_id="suite.bound.mcp",
            account_key_sha256=_ACCOUNT,
        )


def test_schema_five_exact_receipt_history_has_exact_global_bound(tmp_path: Path) -> None:
    def _write_history(root: Path, count: int) -> PluginGrantService:
        root.mkdir()
        broker = _Broker()
        service = _service(root, broker=broker)
        prepared = _prepared("suite.bound", state="b")
        activation = _activation(prepared)
        identity = service._key(
            activation.plugin_id,
            activation.app_id,
            activation.account_key_sha256,
        )
        facts = [
            _retired_fact(
                replace(
                    _legacy_receipt(prepared, activation, generation),
                    receipt_id=sha256_hex(
                        canonical_bytes({"receipt_generation": generation})
                    ),
                )
            )
            for generation in range(1, count + 1)
        ]
        service.journal_path.write_bytes(
            canonical_bytes(
                {
                    "schema_version": 5,
                    "staged": {},
                    "active": {},
                    "generations": {identity: count},
                    "disconnected": {},
                    "retired_legacy": {identity: facts},
                }
            )
        )
        return service

    maximum = _write_history(tmp_path / "maximum-v5", 512)
    assert maximum.current(
        "suite.bound",
        app_id="suite.bound.mcp",
        account_key_sha256=_ACCOUNT,
    ) is None
    oversized = _write_history(tmp_path / "oversized-v5", 513)
    with pytest.raises(ValueError, match="^plugin_grant_store_unavailable$"):
        oversized.current(
            "suite.bound",
            app_id="suite.bound.mcp",
            account_key_sha256=_ACCOUNT,
        )


@pytest.mark.parametrize("tamper", ["authority_revision", "payload"])
def test_schema_two_legacy_migration_rejects_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="4")
    activation = _activation(prepared)
    _ = service.activate(prepared, activation)
    _rewrite_active_as_legacy(service, activation)
    document = json.loads(service.journal_path.read_text(encoding="utf-8"))
    key = service._key(activation.plugin_id, activation.app_id, activation.account_key_sha256)
    if tamper == "authority_revision":
        document["active"][key]["activation"]["authority_revision"] = "f" * 64
        document["active"][key]["receipt"]["authority_revision"] = "f" * 64
    else:
        document["active"][key]["activation"]["source_constraints"] = {
            "tampered": True
        }
    before = canonical_bytes(document)
    service.journal_path.write_bytes(before)
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )

    with pytest.raises(ValueError, match="^plugin_grant_store_unavailable$"):
        restarted.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )

    assert service.journal_path.read_bytes() == before


def test_schema_two_legacy_migration_is_idempotent_after_restart(tmp_path: Path) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="5")
    activation = _activation(prepared)
    service.activate(prepared, activation)
    _rewrite_active_as_legacy(service, activation)
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        restarted.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    migrated = service.journal_path.read_bytes()

    second_restart = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        second_restart.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    assert service.journal_path.read_bytes() == migrated


def test_schema_three_single_retirement_marker_upgrades_to_ordered_history(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="3")
    activation = _activation(prepared)
    identity = service._key(
        activation.plugin_id,
        activation.app_id,
        activation.account_key_sha256,
    )
    service.journal_path.write_bytes(
        canonical_bytes(
            {
                "schema_version": 3,
                "staged": {},
                "active": {},
                "generations": {identity: 1},
                "disconnected": {},
                "retired_legacy": {
                    identity: {
                        "generation": 1,
                        "authority_revision": _legacy_authority_revision(activation),
                        "receipt_binding_revision": _schema_four_receipt_binding_revision(
                            activation,
                            1,
                        ),
                    }
                },
            }
        )
    )

    assert (
        service.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    upgraded = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == 6
    assert upgraded["retired_legacy"] == {}


def test_schema_four_retirement_upgrades_only_with_one_exact_stored_receipt(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="4")
    activation = _activation(prepared)
    receipt = _legacy_receipt(prepared, activation, 1)
    broker.receipts.append(receipt)
    identity = service._key(
        activation.plugin_id,
        activation.app_id,
        activation.account_key_sha256,
    )
    service.journal_path.write_bytes(
        canonical_bytes(
            {
                "schema_version": 4,
                "staged": {},
                "active": {},
                "generations": {identity: 1},
                "disconnected": {},
                "retired_legacy": {
                    identity: [
                        {
                            "generation": 1,
                            "authority_revision": receipt.authority_revision,
                            "receipt_binding_revision": (
                                _schema_four_receipt_binding_revision(activation, 1)
                            ),
                        }
                    ]
                },
            }
        )
    )

    assert (
        service.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    upgraded = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == 6
    assert upgraded["retired_legacy"][identity] == [_retired_fact(receipt)]


def test_unprovable_schema_four_retirement_does_not_poison_current_grants(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    current_prepared = _prepared("suite.current", state="5")
    current = _activation(current_prepared)
    service.activate(current_prepared, current)
    missing_prepared = _prepared("suite.missing", state="6")
    missing = _activation(missing_prepared)
    missing_identity = service._key(
        missing.plugin_id,
        missing.app_id,
        missing.account_key_sha256,
    )
    document = json.loads(service.journal_path.read_text(encoding="utf-8"))
    document["schema_version"] = 4
    document["generations"][missing_identity] = 1
    document["retired_legacy"] = {
        missing_identity: [
            {
                "generation": 1,
                "authority_revision": _legacy_authority_revision(missing),
                "receipt_binding_revision": _schema_four_receipt_binding_revision(
                    missing,
                    1,
                ),
            }
        ]
    }
    service.journal_path.write_bytes(canonical_bytes(document))

    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        restarted.current(
            current.plugin_id,
            app_id=current.app_id,
            account_key_sha256=current.account_key_sha256,
        )
        is not None
    )
    upgraded = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == 6
    assert upgraded["generations"][missing_identity] == 1
    assert upgraded["retired_legacy"] == {}


def test_delayed_legacy_receipt_visibility_preserves_then_claims_exact_fact(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.delayed", state="7")
    activation = _activation(prepared)
    _rewrite_staged_as_legacy(service, prepared, activation)
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        restarted.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    receipt = _legacy_receipt(prepared, activation, 1)
    identity = service._key(
        activation.plugin_id,
        activation.app_id,
        activation.account_key_sha256,
    )
    broker.after_unclaimed_snapshot = lambda: broker.receipts.append(receipt)

    assert restarted.reconcile() == ()
    delayed = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert delayed["retired_legacy"][identity] == [_retired_fact(receipt)]
    assert broker.claimed == []

    assert restarted.reconcile() == ()
    assert broker.claimed == [(receipt.receipt_id, receipt.authority_revision)]
    cleaned = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert cleaned["retired_legacy"] == {}
    assert restarted.reconcile() == ()


def test_partial_legacy_recovery_cleans_only_exact_confirmed_receipts(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = tuple(
        _prepared(f"suite.partial{index}", state=str(index))
        for index in range(1, 4)
    )
    activations = tuple(_activation(item) for item in prepared)
    for item, activation in zip(prepared, activations, strict=True):
        service._stage(item, activation)
    document = json.loads(service.journal_path.read_text(encoding="utf-8"))
    document["schema_version"] = 2
    document.pop("retired_legacy", None)
    for item, activation in zip(prepared, activations, strict=True):
        document["staged"][item.state_sha256]["activation"] = (
            _schema_five_activation_document(
                activation,
                authority_revision=_legacy_authority_revision(activation),
            )
        )
    service.journal_path.write_bytes(canonical_bytes(document))
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert restarted.current(
        activations[0].plugin_id,
        app_id=activations[0].app_id,
        account_key_sha256=activations[0].account_key_sha256,
    ) is None
    receipts = tuple(
        _legacy_receipt(item, activation, 1)
        for item, activation in zip(prepared, activations, strict=True)
    )
    broker.receipts.extend((receipts[0], receipts[2]))
    broker.claim_errors[receipts[2].receipt_id] = RuntimeError("claim_failed")

    with pytest.raises(RuntimeError, match="^claim_failed$"):
        restarted.reconcile()

    migrated = json.loads(service.journal_path.read_text(encoding="utf-8"))
    identities = tuple(
        service._key(item.plugin_id, item.app_id, item.account_key_sha256)
        for item in activations
    )
    assert identities[0] not in migrated["retired_legacy"]
    assert identities[1] in migrated["retired_legacy"]
    assert identities[2] in migrated["retired_legacy"]
    assert broker.claimed == [
        (receipts[0].receipt_id, receipts[0].authority_revision)
    ]

    assert restarted.reconcile() == ()
    after_retry = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert set(after_retry["retired_legacy"]) == {identities[1]}


@pytest.mark.parametrize(
    "substitution",
    [
        "different_id",
        "same_id_other_app",
        "duplicate_id_other_fields",
        "cross_identity",
    ],
)
def test_legacy_retirement_rejects_every_receipt_identity_substitution(
    tmp_path: Path,
    substitution: str,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.identity", state="8")
    activation = _activation(prepared)
    _rewrite_staged_as_legacy(service, prepared, activation)
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert restarted.current(
        activation.plugin_id,
        app_id=activation.app_id,
        account_key_sha256=activation.account_key_sha256,
    ) is None
    exact = _legacy_receipt(prepared, activation, 1)
    changed = {
        "different_id": replace(exact, receipt_id="f" * 64),
        "same_id_other_app": replace(exact, app_id="suite.other.mcp"),
        "duplicate_id_other_fields": replace(exact, app_id="suite.other.mcp"),
        "cross_identity": replace(
            exact,
            receipt_id="e" * 64,
            account_key_sha256="9" * 64,
        ),
    }[substitution]
    broker.receipts.append(changed)
    if substitution == "duplicate_id_other_fields":
        broker.receipts.insert(0, exact)
    before = service.journal_path.read_bytes()

    with pytest.raises(ValueError, match="^(oauth_grant_receipt_conflict|plugin_grant_recovery_invalid)$"):
        restarted.reconcile()

    assert broker.claimed == []
    assert service.journal_path.read_bytes() == before


def test_schema_two_unclaimed_legacy_receipt_is_retired_without_republication(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="8")
    activation = _activation(prepared)
    service.activate(prepared, activation)
    legacy_revision = _legacy_authority_revision(activation)
    _make_broker_receipt_legacy(broker, activation)
    _rewrite_active_as_legacy(service, activation)
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )

    assert restarted.reconcile() == ()
    assert broker.unclaimed_grant_receipts() == ()
    assert broker.claimed == [(broker.receipts[-1].receipt_id, legacy_revision)]
    assert (
        restarted.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    migrated = json.loads(service.journal_path.read_text(encoding="utf-8"))
    key = service._key(activation.plugin_id, activation.app_id, activation.account_key_sha256)
    assert migrated["generations"][key] == 1
    assert key not in migrated["retired_legacy"]
    assert restarted.reconcile() == ()


def test_reconnect_stage_crash_preserves_legacy_receipt_recovery(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    old_prepared = _prepared("suite.legacy", state="d")
    old_activation = _activation(old_prepared)
    service.activate(old_prepared, old_activation)
    legacy_receipt = _make_broker_receipt_legacy(broker, old_activation)
    _rewrite_active_as_legacy(service, old_activation)
    migrated = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        migrated.current(
            old_activation.plugin_id,
            app_id=old_activation.app_id,
            account_key_sha256=old_activation.account_key_sha256,
        )
        is None
    )
    reconnect = _prepared("suite.legacy", state="e")
    reconnect_activation = _activation(reconnect)

    assert migrated._stage(reconnect, reconnect_activation) == 2
    staged = json.loads(service.journal_path.read_text(encoding="utf-8"))
    identity = service._key(
        old_activation.plugin_id,
        old_activation.app_id,
        old_activation.account_key_sha256,
    )
    assert identity in staged["retired_legacy"]

    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert restarted.reconcile() == ()
    assert broker.claimed == [
        (legacy_receipt.receipt_id, legacy_receipt.authority_revision)
    ]
    recovered = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert recovered["generations"][identity] == 2
    assert identity not in recovered["retired_legacy"]
    assert reconnect.state_sha256 in recovered["staged"]
    assert restarted.activate(reconnect, reconnect_activation) == (
        reconnect_activation.authority()
    )


@pytest.mark.parametrize("substitution", ["current", "other_app", "other_account"])
def test_legacy_retirement_never_matches_a_substituted_receipt(
    tmp_path: Path,
    substitution: str,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="f")
    activation = _activation(prepared)
    service.activate(prepared, activation)
    _make_broker_receipt_legacy(broker, activation)
    _rewrite_active_as_legacy(service, activation)
    migrated = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        migrated.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    changes = {
        "current": {"authority_revision": activation.authority_revision},
        "other_app": {"app_id": "suite.other.mcp"},
        "other_account": {"account_key_sha256": "9" * 64},
    }[substitution]
    broker.receipts[-1] = replace(broker.receipts[-1], **changes)
    before = service.journal_path.read_bytes()

    with pytest.raises(ValueError, match="^plugin_grant_recovery_invalid$"):
        migrated.reconcile()

    assert broker.unclaimed_grant_receipts() == (broker.receipts[-1],)
    assert service.journal_path.read_bytes() == before


def test_legacy_receipt_claim_failure_preserves_history_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="1")
    activation = _activation(prepared)
    service.activate(prepared, activation)
    _make_broker_receipt_legacy(broker, activation)
    _rewrite_active_as_legacy(service, activation)
    migrated = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        migrated.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
    before = service.journal_path.read_bytes()
    original_claim = broker.claim_grant_receipt

    def _fail_claim(_receipt_id: str, _authority_revision: str) -> None:
        raise RuntimeError("simulated_claim_crash")

    monkeypatch.setattr(broker, "claim_grant_receipt", _fail_claim)
    with pytest.raises(RuntimeError, match="^simulated_claim_crash$"):
        migrated.reconcile()
    assert service.journal_path.read_bytes() == before
    monkeypatch.setattr(broker, "claim_grant_receipt", original_claim)

    assert migrated.reconcile() == ()
    cleaned = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert cleaned["retired_legacy"] == {}


def test_legacy_history_cleanup_crash_is_idempotent_without_generation_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="2")
    activation = _activation(prepared)
    service.activate(prepared, activation)
    _make_broker_receipt_legacy(broker, activation)
    _rewrite_active_as_legacy(service, activation)
    migrated = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        migrated.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )

    def _fail_cleanup(_document: dict[str, object]) -> None:
        raise RuntimeError("simulated_cleanup_crash")

    monkeypatch.setattr(migrated, "_write_locked", _fail_cleanup)
    with pytest.raises(RuntimeError, match="^simulated_cleanup_crash$"):
        migrated.reconcile()
    assert broker.unclaimed_grant_receipts() == ()
    crashed = json.loads(service.journal_path.read_text(encoding="utf-8"))
    identity = service._key(
        activation.plugin_id,
        activation.app_id,
        activation.account_key_sha256,
    )
    assert identity in crashed["retired_legacy"]

    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert restarted.reconcile() == ()
    assert restarted.reconcile() == ()
    cleaned = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert cleaned["generations"][identity] == 1
    assert cleaned["retired_legacy"] == {}


def test_schema_two_legacy_migration_rewrite_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _Broker()
    service = _service(tmp_path, broker=broker)
    prepared = _prepared("suite.legacy", state="6")
    activation = _activation(prepared)
    service.activate(prepared, activation)
    before = _rewrite_active_as_legacy(service, activation)
    restarted = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )

    def _fail_write(_payload: bytes) -> None:
        raise RuntimeError("simulated_migration_crash")

    monkeypatch.setattr(restarted._state, "write_bytes", _fail_write)
    with pytest.raises(RuntimeError, match="^simulated_migration_crash$"):
        restarted.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
    assert service.journal_path.read_bytes() == before

    recovered = _service(
        tmp_path,
        broker=broker,
        authority_store=service.authority_store,
    )
    assert (
        recovered.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )
        is None
    )
