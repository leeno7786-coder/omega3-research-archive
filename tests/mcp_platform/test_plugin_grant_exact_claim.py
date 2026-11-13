from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_broker import OAuthBroker
from mcp_platform.oauth_vault import (
    OAuthCommittedGrant,
    OAuthCredentialVault,
    OAuthPendingTransaction,
    OAuthPreparedToken,
)
from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_grants import PluginGrantService


_KEY = b"r" * 32
_ACCOUNT = "1" * 64
_PROFILE_REVISION = "sha256:" + "2" * 64


@dataclass
class _Permissions:
    invalidated: list[str]

    def invalidate_plugin(self, plugin_id: str) -> int:
        self.invalidated.append(plugin_id)
        return 1


def _broker(vault: OAuthCredentialVault) -> OAuthBroker:
    return OAuthBroker(
        registry=object(),  # type: ignore[arg-type]
        vault=vault,
        protocol=object(),  # type: ignore[arg-type]
        clock=lambda: 150.0,
    )


def _receipt(
    vault: OAuthCredentialVault,
    seed: str,
    *,
    plugin_id: str | None = None,
    client_seed: str | None = None,
) -> OAuthCommittedGrant:
    selected_plugin = plugin_id or f"plugin.{seed}"
    app_id = f"app.{selected_plugin}"
    client_id = f"desktop-client-{client_seed or seed}"
    credential_identity = sha256_hex(
        canonical_bytes({"legacy_client_id": client_id})
    )
    selected_partition = sha256_hex(
        canonical_bytes(
            {
                "account_key_sha256": _ACCOUNT,
                "account_partition": "issuer_account",
                "authorization_issuer": "https://issuer.example",
                "credential_identity": credential_identity,
            }
        )
    )
    state = sha256_hex(canonical_bytes({"state": seed}))
    scopes = (f"files.{seed}",)
    transaction = OAuthPendingTransaction(
        profile_id="profile.desktop",
        plugin_id=selected_plugin,
        app_id=app_id,
        account_key_sha256=_ACCOUNT,
        partition_key=selected_partition,
        authorization_issuer="https://issuer.example",
        resource_origin="https://resource.example/mcp",
        client_id=client_id,
        state_sha256=state,
        pkce_verifier=f"pkce-{seed}",
        redirect_uri="http://127.0.0.1:13310/oauth/profile.desktop/callback",
        scopes=scopes,
        authorization_context_revision="3" * 64,
        profile_revision=_PROFILE_REVISION,
        issued_at=100.0,
        expires_at=200.0,
        attempt_revision=0,
        requested_scopes=scopes,
    )
    grant = OAuthCommittedGrant(
        receipt_id=sha256_hex(canonical_bytes({"receipt": seed})),
        plugin_id=selected_plugin,
        app_id=app_id,
        account_key_sha256=_ACCOUNT,
        profile_id=transaction.profile_id,
        profile_revision=transaction.profile_revision,
        token_partition_key=selected_partition,
        connection_revision=sha256_hex(canonical_bytes({"connection": seed})),
        capability_revision=sha256_hex(canonical_bytes({"capability": seed})),
        authority_revision=sha256_hex(canonical_bytes({"authority": seed})),
        capability_binding_revision=sha256_hex(canonical_bytes({"binding": seed})),
        scopes=scopes,
        execution_policy="direct_turn",
        connection_generation=1,
    )
    vault.begin_transaction(transaction)
    vault.prepare_token(
        state,
        OAuthPreparedToken(
            access_token=f"access-{seed}",
            refresh_token=f"refresh-{seed}",
            token_type="Bearer",
            scopes=scopes,
            expires_at=3600.0,
        ),
    )
    vault.commit_authorization(state, grant)
    return grant


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


def _binding(receipt: OAuthCommittedGrant) -> str:
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


def _fact(receipt: OAuthCommittedGrant) -> dict[str, object]:
    return {
        "generation": receipt.connection_generation,
        "authority_revision": receipt.authority_revision,
        "receipt_binding_revision": _binding(receipt),
        "receipt": _receipt_document(receipt),
    }


def _identity(service: PluginGrantService, receipt: OAuthCommittedGrant) -> str:
    return service._key(
        receipt.plugin_id,
        receipt.app_id,
        receipt.account_key_sha256,
    )


def _service(
    root: Path,
    broker: OAuthBroker,
    receipts: tuple[OAuthCommittedGrant, ...],
) -> PluginGrantService:
    service = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=root / "authority.json"),
        permission_store=_Permissions([]),
        journal_path=root / "grants.json",
    )
    service.journal_path.write_bytes(
        canonical_bytes(
            {
                "schema_version": 6,
                "staged": {},
                "active": {},
                "generations": {
                    _identity(service, receipt): receipt.connection_generation
                    for receipt in receipts
                },
                "disconnected": {},
                "retired_legacy": {
                    _identity(service, receipt): [_fact(receipt)] for receipt in receipts
                },
            }
        )
    )
    return service


def _install_race_hooks(
    broker: OAuthBroker,
    vault: OAuthCredentialVault,
    raced: OAuthCommittedGrant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = broker.grant_receipt_state
    raced_once = False

    def observe_then_revoke(expected: OAuthCommittedGrant) -> str:
        nonlocal raced_once
        result = state(expected)
        if expected.receipt_id == raced.receipt_id and result == "unclaimed" and not raced_once:
            raced_once = True
            vault.prepare_revocation(
                raced.token_partition_key,
                plugin_id=raced.plugin_id,
            )
        return result

    exact_once = False

    def revoke_then_claim(
        expected: OAuthCommittedGrant,
        authority_revision: str,
    ) -> object:
        nonlocal exact_once
        if expected.receipt_id == raced.receipt_id and not exact_once:
            exact_once = True
            vault.prepare_revocation(
                raced.token_partition_key,
                plugin_id=raced.plugin_id,
            )
        return vault.claim_grant_receipt_exact(
            expected,
            authority_revision=authority_revision,
        )

    monkeypatch.setattr(broker, "grant_receipt_state", observe_then_revoke)
    monkeypatch.setattr(broker, "claim_grant_receipt_exact", revoke_then_claim, raising=False)


def test_reconcile_never_claims_a_receipt_raced_to_revocation_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = OAuthCredentialVault(
        tmp_path / "vault",
        key_provider=lambda: _KEY,
        clock=lambda: 150.0,
    )
    broker = _broker(vault)
    receipt = _receipt(vault, "race")
    _install_race_hooks(broker, vault, receipt, monkeypatch)
    service = _service(tmp_path, broker, (receipt,))
    before = service.journal_path.read_bytes()

    with pytest.raises(ValueError, match="^plugin_grant_recovery_invalid$"):
        service.reconcile()

    assert vault.committed_grant_receipts() == (
        replace(receipt, status="revocation_pending"),
    )
    assert service.journal_path.read_bytes() == before


def test_claimed_receipt_raced_to_revocation_pending_retains_retirement_fact(
    tmp_path: Path,
) -> None:
    vault = OAuthCredentialVault(
        tmp_path / "vault",
        key_provider=lambda: _KEY,
        clock=lambda: 150.0,
    )
    broker = _broker(vault)
    receipt = _receipt(vault, "claimed-revocation-race")
    assert broker.claim_grant_receipt_exact(
        receipt,
        receipt.authority_revision,
    ) == "claimed"
    vault.prepare_revocation(
        receipt.token_partition_key,
        plugin_id=receipt.plugin_id,
    )
    raced = replace(
        receipt,
        status="revocation_pending",
        claimed_authority_revision=receipt.authority_revision,
    )

    with pytest.raises(ValueError, match="^oauth_grant_receipt_conflict$"):
        broker.claim_grant_receipt_exact(
            receipt,
            receipt.authority_revision,
        )

    assert vault.committed_grant_receipts() == (raced,)
    service = _service(tmp_path, broker, (receipt,))
    retirement_fact = service.journal_path.read_bytes()
    with pytest.raises(ValueError, match="^plugin_grant_recovery_invalid$"):
        service.reconcile()
    assert service.journal_path.read_bytes() == retirement_fact
    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        service.authority_store.connection(
            receipt.plugin_id,
            receipt.app_id,
            receipt.account_key_sha256,
        )


def test_partial_exact_claim_race_cleans_only_claimed_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = OAuthCredentialVault(
        tmp_path / "vault",
        key_provider=lambda: _KEY,
        clock=lambda: 150.0,
    )
    broker = _broker(vault)
    claimed = _receipt(vault, "a-claimed")
    raced = _receipt(vault, "b-raced")
    absent = replace(
        raced,
        receipt_id=sha256_hex(canonical_bytes({"receipt": "z-absent"})),
        plugin_id="plugin.z-absent",
        app_id="app.plugin.z-absent",
        token_partition_key=sha256_hex(canonical_bytes({"partition": "z-absent"})),
        connection_revision=sha256_hex(canonical_bytes({"connection": "z-absent"})),
        capability_revision=sha256_hex(canonical_bytes({"capability": "z-absent"})),
        authority_revision=sha256_hex(canonical_bytes({"authority": "z-absent"})),
        capability_binding_revision=sha256_hex(canonical_bytes({"binding": "z-absent"})),
        scopes=("files.z-absent",),
    )
    _install_race_hooks(broker, vault, raced, monkeypatch)
    service = _service(tmp_path, broker, (claimed, raced, absent))

    with pytest.raises(ValueError, match="^plugin_grant_recovery_invalid$"):
        service.reconcile()

    document = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert _identity(service, claimed) not in document["retired_legacy"]
    assert set(document["retired_legacy"]) == {
        _identity(service, raced),
        _identity(service, absent),
    }
    stored = {item.receipt_id: item for item in vault.committed_grant_receipts()}
    assert stored[claimed.receipt_id].claimed_authority_revision == claimed.authority_revision
    assert stored[raced.receipt_id] == replace(raced, status="revocation_pending")

    pending = vault.load_token_partition(raced.token_partition_key)
    vault.finalize_revocation(
        raced.token_partition_key,
        plugin_id=raced.plugin_id,
        expected_revision=pending.revision,
    )
    assert service.reconcile() == ()
    after_terminal = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert set(after_terminal["retired_legacy"]) == {
        _identity(service, raced),
        _identity(service, absent),
    }


def test_exact_claim_cleanup_crash_restarts_idempotently_with_real_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = OAuthCredentialVault(
        tmp_path / "vault",
        key_provider=lambda: _KEY,
        clock=lambda: 150.0,
    )
    broker = _broker(vault)
    receipt = _receipt(vault, "cleanup")
    service = _service(tmp_path, broker, (receipt,))

    def fail_cleanup(_document: dict[str, object]) -> None:
        raise RuntimeError("simulated_cleanup_crash")

    monkeypatch.setattr(service, "_write_locked", fail_cleanup)
    with pytest.raises(RuntimeError, match="^simulated_cleanup_crash$"):
        service.reconcile()
    assert vault.grant_receipt_state(receipt) == "claimed"
    crashed = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert _identity(service, receipt) in crashed["retired_legacy"]

    restarted = PluginGrantService(
        broker=broker,
        authority_store=service.authority_store,
        permission_store=_Permissions([]),
        journal_path=service.journal_path,
    )
    assert restarted.reconcile() == ()
    assert restarted.reconcile() == ()
    cleaned = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert cleaned["retired_legacy"] == {}


def test_revoked_or_removed_exact_receipt_is_inert_retained_and_non_poisoning(
    tmp_path: Path,
) -> None:
    vault = OAuthCredentialVault(
        tmp_path / "vault",
        key_provider=lambda: _KEY,
        clock=lambda: 150.0,
    )
    broker = _broker(vault)
    revoked = _receipt(vault, "revoked")
    removed = _receipt(vault, "removed")
    pending, remaining = vault.prepare_revocation(
        revoked.token_partition_key,
        plugin_id=revoked.plugin_id,
    )
    assert remaining == ()
    vault.finalize_revocation(
        revoked.token_partition_key,
        plugin_id=revoked.plugin_id,
        expected_revision=pending.revision,
    )
    replacement = _receipt(
        vault,
        "replacement",
        plugin_id=removed.plugin_id,
        client_seed="removed",
    )
    assert broker.claim_grant_receipt_exact(
        replacement,
        replacement.authority_revision,
    ) == "claimed"
    assert removed.receipt_id not in {
        item.receipt_id for item in vault.committed_grant_receipts()
    }
    service = _service(tmp_path, broker, (revoked, removed))

    assert service.reconcile() == ()
    assert service.reconcile() == ()

    retained = json.loads(service.journal_path.read_text(encoding="utf-8"))[
        "retired_legacy"
    ]
    assert set(retained) == {
        _identity(service, revoked),
        _identity(service, removed),
    }
    current = {
        item.receipt_id: item for item in vault.committed_grant_receipts()
    }
    assert revoked.receipt_id not in current
    assert (
        current[replacement.receipt_id].claimed_authority_revision
        == replacement.authority_revision
    )
