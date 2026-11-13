from __future__ import annotations

import ast
import base64
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
import json
import os
from threading import Barrier
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mcp_platform import oauth_vault as oauth_vault_module
from mcp_platform.authorization import _StoredClientInfo
from mcp_platform.oauth_vault import (
    _DOCUMENT_MAPS_THROUGH_V5,
    OAuthClientCredential,
    OAuthClientRegistration,
    OAuthCommittedGrant,
    OAuthCredentialVault,
    OAuthPendingTransaction,
    OAuthPreparedToken,
)
from mcp_platform.state_store import McpStateStore


_KEY = b"k" * 32
_AAD = b"omega-oauth-vault-v1"
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_DOCUMENT_MAPS = {
    "account_partitions",
    "claims",
    "clients",
    "committed_grants",
    "consumed_transactions",
    "credentials",
    "migrations",
    "registrations",
    "tokens",
    "transactions",
}


def _revision(character: str) -> str:
    return "sha256:" + character * 64


def _binding_revision(character: str) -> str:
    return character * 64


def _attempt_key(value: OAuthPendingTransaction) -> str:
    return sha256(
        json.dumps(
            {
                "account_key_sha256": value.account_key_sha256,
                "app_id": value.app_id,
                "plugin_id": value.plugin_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _credential_identity(*, client_id: str = "client-id") -> str:
    return sha256(
        json.dumps(
            {"legacy_client_id": client_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _partition_key(*, client_id: str = "client-id") -> str:
    payload = {
        "account_key_sha256": "1" * 64,
        "account_partition": "issuer_account",
        "authorization_issuer": "https://github.com",
        "credential_identity": _credential_identity(client_id=client_id),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class _MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def vault(tmp_path: Path) -> OAuthCredentialVault:
    return OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)


@pytest.fixture
def transaction() -> OAuthPendingTransaction:
    return OAuthPendingTransaction(
        profile_id="github.desktop",
        plugin_id="plugin.alpha",
        app_id="app.github",
        account_key_sha256="1" * 64,
        partition_key=_partition_key(),
        authorization_issuer="https://github.com",
        resource_origin="https://api.githubcopilot.com/mcp/",
        client_id="client-id",
        state_sha256="a" * 64,
        pkce_verifier="pkce-private-value",
        redirect_uri="http://127.0.0.1:13310/oauth/github.desktop/callback",
        scopes=("read:user", "repo"),
        authorization_context_revision=_binding_revision("4"),
        profile_revision=_revision("5"),
        issued_at=100.0,
        expires_at=200.0,
        attempt_revision=0,
        attempt_key="d2cc48c9d2eb4eecefcdffc857541b10570d7986f1c7f72b42f545fe3bdfc593",
        requested_scopes=("read:user", "repo"),
        account_partition="issuer_account",
        credential_identity=_credential_identity(),
    )


@pytest.fixture
def prepared_token() -> OAuthPreparedToken:
    return OAuthPreparedToken(
        access_token="access-private-value",
        refresh_token="refresh-private-value",
        token_type="Bearer",
        scopes=("read:user", "repo"),
        expires_at=3600.0,
    )


@pytest.fixture
def grant(transaction: OAuthPendingTransaction) -> OAuthCommittedGrant:
    return OAuthCommittedGrant(
        receipt_id="3" * 64,
        plugin_id=transaction.plugin_id,
        app_id=transaction.app_id,
        account_key_sha256=transaction.account_key_sha256,
        profile_id=transaction.profile_id,
        profile_revision=transaction.profile_revision,
        token_partition_key=transaction.partition_key,
        connection_revision=_binding_revision("6"),
        capability_revision=_binding_revision("7"),
        authority_revision=_binding_revision("8"),
        capability_binding_revision=_binding_revision("9"),
        scopes=transaction.scopes,
        execution_policy="direct_turn",
    )


def _decrypt_document(path: Path, key: bytes = _KEY) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    envelope = json.loads(raw.decode("utf-8"))
    plaintext = AESGCM(key).decrypt(
        base64.b64decode(envelope["nonce"], validate=True),
        base64.b64decode(envelope["ciphertext"], validate=True),
        _AAD,
    )
    document = json.loads(plaintext.decode("utf-8"))
    return document["records"], plaintext


def _encrypt_document(
    document: dict[str, object],
    *,
    nonce: bytes = b"n" * 12,
    legacy: bool = False,
    schema_version: int = 6,
) -> bytes:
    if not legacy:
        document = {"schema_version": schema_version, "records": document}
    plaintext = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = {
        "ciphertext": base64.b64encode(AESGCM(_KEY).encrypt(nonce, plaintext, _AAD)).decode(
            "ascii"
        ),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "version": 1,
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decrypt_payload(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    plaintext = AESGCM(_KEY).decrypt(
        base64.b64decode(raw["nonce"], validate=True),
        base64.b64decode(raw["ciphertext"], validate=True),
        _AAD,
    )
    return json.loads(plaintext.decode("utf-8"))


def write_legacy_v4_fixture_document(vault_path: Path) -> None:
    document: dict[str, object] = {name: {} for name in _DOCUMENT_MAPS_THROUGH_V5}
    vault_path.write_bytes(_encrypt_document(document, schema_version=4))


def _legacy_partition_key(transaction: OAuthPendingTransaction) -> str:
    payload = {
        "account_key_sha256": transaction.account_key_sha256,
        "authorization_context_revision": transaction.authorization_context_revision,
        "authorization_issuer": transaction.authorization_issuer,
        "client_id": transaction.client_id,
        "profile_id": transaction.profile_id,
        "profile_revision": transaction.profile_revision,
        "resource_origin": transaction.resource_origin,
        "scopes": list(transaction.scopes),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _legacy_transaction_value(
    transaction: OAuthPendingTransaction,
    token: OAuthPreparedToken | None,
) -> dict[str, object]:
    return {
        "account_key_sha256": transaction.account_key_sha256,
        "app_id": transaction.app_id,
        "attempt_revision": transaction.attempt_revision,
        "authorization_context_revision": transaction.authorization_context_revision,
        "authorization_issuer": transaction.authorization_issuer,
        "client_id": transaction.client_id,
        "expires_at": transaction.expires_at,
        "issued_at": transaction.issued_at,
        "partition_key": transaction.partition_key,
        "pkce_verifier": transaction.pkce_verifier,
        "plugin_id": transaction.plugin_id,
        "prepared_token": (
            None
            if token is None
            else {
                "access_token": token.access_token,
                "expires_at": token.expires_at,
                "refresh_token": token.refresh_token,
                "scopes": list(token.scopes),
                "token_type": token.token_type,
            }
        ),
        "profile_id": transaction.profile_id,
        "profile_revision": transaction.profile_revision,
        "redirect_uri": transaction.redirect_uri,
        "resource_origin": transaction.resource_origin,
        "scopes": list(transaction.scopes),
        "state_sha256": transaction.state_sha256,
    }


def _legacy_partition_value(
    transaction: OAuthPendingTransaction,
    token: OAuthPreparedToken,
    *,
    revocation_pending: bool,
) -> dict[str, object]:
    return {
        "account_key_sha256": transaction.account_key_sha256,
        "authorization_issuer": transaction.authorization_issuer,
        "authorization_context_revision": transaction.authorization_context_revision,
        "client_id": transaction.client_id,
        "dependent_plugin_ids": [transaction.plugin_id],
        "partition_key": transaction.partition_key,
        "profile_id": transaction.profile_id,
        "profile_revision": transaction.profile_revision,
        "resource_origin": transaction.resource_origin,
        "revision": 3,
        "revocation_pending_plugin_id": (transaction.plugin_id if revocation_pending else None),
        "reauthorization_required": False,
        "token": {
            "access_token": token.access_token,
            "expires_at": token.expires_at,
            "refresh_token": token.refresh_token,
            "scopes": list(token.scopes),
            "token_type": token.token_type,
        },
    }


def _legacy_grant_value(
    grant: OAuthCommittedGrant,
    *,
    claimed: bool,
    revocation_pending: bool,
) -> dict[str, object]:
    return {
        "account_key_sha256": grant.account_key_sha256,
        "app_id": grant.app_id,
        "authority_revision": grant.authority_revision,
        "capability_revision": grant.capability_revision,
        "claimed_authority_revision": grant.authority_revision if claimed else None,
        "connection_revision": grant.connection_revision,
        "execution_policy": grant.execution_policy,
        "plugin_id": grant.plugin_id,
        "profile_id": grant.profile_id,
        "profile_revision": grant.profile_revision,
        "receipt_id": grant.receipt_id,
        "scopes": list(grant.scopes),
        "status": "revocation_pending" if revocation_pending else "active",
        "token_partition_key": grant.token_partition_key,
        "tool_binding_revision": grant.capability_binding_revision,
    }


def _downgrade_grants_to_schema_four(document: dict[str, object]) -> None:
    grants = document["committed_grants"]
    assert isinstance(grants, dict)
    for raw in grants.values():
        assert isinstance(raw, dict)
        raw["tool_binding_revision"] = raw.pop("capability_binding_revision")
    migrations = document["migrations"]
    assert isinstance(migrations, dict)
    migrations.pop("oauth-vault-inner-v3", None)
    migrations.pop("oauth-vault-inner-v4", None)


def _legacy_v1_document(
    transaction: OAuthPendingTransaction,
    token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
    *,
    include_transaction: bool = False,
    claimed: bool = False,
    revocation_pending: bool = False,
) -> dict[str, object]:
    document: dict[str, object] = {name: {} for name in _DOCUMENT_MAPS_THROUGH_V5}
    document["migrations"] = {
        "legacy-sdk-import": {"revision": 2, "value": {"status": "completed"}}
    }
    if include_transaction:
        document["transactions"] = {
            transaction.state_sha256: {
                "revision": 4,
                "superseded": False,
                "value": _legacy_transaction_value(transaction, token),
            }
        }
        return document
    document["tokens"] = {
        transaction.partition_key: {
            "kind": "oauth",
            "value": _legacy_partition_value(
                transaction,
                token,
                revocation_pending=revocation_pending,
            ),
        }
    }
    document["account_partitions"] = {
        transaction.partition_key: {
            "account_key_sha256": transaction.account_key_sha256,
            "client_id": transaction.client_id,
            "profile_id": transaction.profile_id,
        }
    }
    document["committed_grants"] = {
        grant.receipt_id: _legacy_grant_value(
            grant,
            claimed=claimed,
            revocation_pending=revocation_pending,
        )
    }
    return document


def _malformed_document(case: str) -> dict[str, object]:
    document: dict[str, object] = {name: {} for name in _DOCUMENT_MAPS}
    if case == "credential-key":
        document["credentials"] = {
            "not-an-oauth-credential-reference": {
                "revision": 0,
                "value": {"client_id": "client-id", "client_secret": None},
            }
        }
    elif case == "credential-revision":
        document["credentials"] = {
            "oauth-client/provider/secret": {
                "revision": -1,
                "value": {"client_id": "client-id", "client_secret": None},
            }
        }
    elif case == "transaction-revision":
        document["transactions"] = {
            "a" * 64: {"revision": "zero", "superseded": False, "value": {}}
        }
    elif case == "sdk-token-kind":
        document["tokens"] = {
            "sdk:token-key": {
                "kind": "oauth",
                "revision": 0,
                "value": {
                    "access_token": "access-value",
                    "authorization_endpoint": "https://issuer.example/authorize",
                    "expires_at": None,
                    "expires_in": None,
                    "refresh_token": None,
                    "scope": None,
                    "token_endpoint": "https://issuer.example/token",
                    "token_type": "Bearer",
                },
            }
        }
    elif case == "sdk-client-key":
        document["clients"] = {
            "not-an-sdk-key": {
                "kind": "sdk",
                "value": {
                    "client_id": "client-id",
                    "issuer": "https://issuer.example",
                    "redirect_uri": "http://127.0.0.1:13310/callback",
                    "token_endpoint_auth_method": "none",
                },
            }
        }
    elif case == "nested-claim":
        document["claims"] = {"sdk:claim-key": {"not-a-sha256-digest": True}}
    elif case == "oauth-partition":
        document["tokens"] = {
            "2" * 64: {
                "kind": "oauth",
                "value": {
                    "account_key_sha256": "1" * 64,
                    "authorization_issuer": "https://issuer.example",
                    "client_id": "client-id",
                    "dependent_plugin_ids": ["plugin.alpha"],
                    "partition_key": "2" * 64,
                    "profile_id": "profile.alpha",
                    "profile_revision": _revision("5"),
                    "resource_origin": "https://resource.example/",
                    "revision": "zero",
                    "token": {
                        "access_token": "access-value",
                        "expires_at": None,
                        "refresh_token": None,
                        "scopes": ["read"],
                        "token_type": "Bearer",
                    },
                },
            }
        }
    elif case == "account-partition":
        document["account_partitions"] = {
            "2" * 64: {
                "account_key_sha256": "1" * 64,
                "account_partition": "issuer_account",
                "authorization_issuer": "https://issuer.example",
                "credential_identity": [],
            }
        }
    elif case == "committed-receipt":
        document["committed_grants"] = {
            "3" * 64: {
                "account_key_sha256": "1" * 64,
                "app_id": "app.alpha",
                "authority_revision": _binding_revision("8"),
                "capability_revision": "not-a-revision",
                "claimed_authority_revision": None,
                "connection_revision": _binding_revision("6"),
                "execution_policy": "direct_turn",
                "plugin_id": "plugin.alpha",
                "profile_id": "profile.alpha",
                "profile_revision": _revision("5"),
                "receipt_id": "3" * 64,
                "scopes": ["read"],
                "token_partition_key": "2" * 64,
                "tool_binding_revision": _binding_revision("9"),
            }
        }
    elif case == "migration-record":
        document["migrations"] = {"legacy-token-store-v1": {"revision": 0, "value": "completed"}}
    else:  # pragma: no cover - test fixture misuse
        raise AssertionError(case)
    return document


_MALFORMED_DOCUMENT_CASES = (
    "credential-key",
    "credential-revision",
    "transaction-revision",
    "sdk-token-kind",
    "sdk-client-key",
    "nested-claim",
    "oauth-partition",
    "account-partition",
    "committed-receipt",
    "migration-record",
)


def _commit(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    vault.begin_transaction(transaction)
    vault.prepare_token(transaction.state_sha256, token)
    vault.commit_authorization(transaction.state_sha256, grant)


def test_vault_round_trip_never_leaves_plaintext(tmp_path: Path) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY)
    credential = OAuthClientCredential(client_id="public-id", client_secret="private-value")

    revision = vault.store_credential(
        "oauth-client/google.desktop/secret",
        credential,
        expected_revision=None,
    )

    assert revision == 0
    assert vault.load_credential("oauth-client/google.desktop/secret") == (0, credential)
    raw = (tmp_path / "oauth-vault.json").read_bytes()
    assert b"private-value" not in raw
    assert "private-value" not in repr(credential)


@pytest.mark.skipif(os.name != "nt", reason="desktop custody uses Windows DPAPI")
def test_default_desktop_key_is_dpapi_wrapped_and_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMEGA_SEAL_PASSPHRASE", raising=False)
    monkeypatch.delenv("OMEGA_SEAL_PASSPHRASE_FILE", raising=False)
    vault = OAuthCredentialVault(tmp_path)
    credential = OAuthClientCredential(client_id="public-id", client_secret="private-value")

    vault.store_credential("oauth-client/provider/secret", credential, expected_revision=None)

    key_path = tmp_path / "oauth-vault-key.dpapi"
    assert key_path.is_file()
    assert b"private-value" not in key_path.read_bytes()
    assert OAuthCredentialVault(tmp_path).load_credential(
        "oauth-client/provider/secret"
    ) == (0, credential)


def test_existing_vault_without_any_unlock_material_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {name: {} for name in _DOCUMENT_MAPS}
    (tmp_path / "oauth-vault.json").write_bytes(_encrypt_document(document))
    monkeypatch.setattr("mcp_platform.desktop_secrets._legacy_key", lambda: None)
    monkeypatch.delenv("OMEGA_SEAL_PASSPHRASE", raising=False)
    monkeypatch.delenv("OMEGA_SEAL_PASSPHRASE_FILE", raising=False)

    with pytest.raises(ValueError, match="^oauth_vault_locked$"):
        OAuthCredentialVault(tmp_path).load_credential("oauth-client/provider/secret")


def test_vault_uses_exact_canonical_document_and_fresh_nonce(tmp_path: Path) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY)
    credential = OAuthClientCredential(client_id="public-id", client_secret=None)
    path = tmp_path / "oauth-vault.json"

    vault.store_credential(
        "oauth-client/google.desktop/secret",
        credential,
        expected_revision=None,
    )
    first_envelope = json.loads(path.read_text(encoding="utf-8"))
    vault.store_credential(
        "oauth-client/google.desktop/secret",
        credential,
        expected_revision=0,
    )
    second_envelope = json.loads(path.read_text(encoding="utf-8"))
    document, plaintext = _decrypt_document(path)

    assert set(first_envelope) == {"ciphertext", "nonce", "version"}
    assert first_envelope["version"] == 1
    assert first_envelope["nonce"] != second_envelope["nonce"]
    assert len(base64.b64decode(second_envelope["nonce"], validate=True)) == 12
    assert set(document) == _DOCUMENT_MAPS
    assert plaintext == json.dumps(
        {"schema_version": 6, "records": document},
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_legacy_v1_prepared_transaction_migrates_and_reencrypts_once(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    legacy_transaction = replace(
        transaction,
        partition_key=_legacy_partition_key(transaction),
    )
    legacy_grant = replace(grant, token_partition_key=legacy_transaction.partition_key)
    path = tmp_path / "oauth-vault.json"
    path.write_bytes(
        _encrypt_document(
            _legacy_v1_document(
                legacy_transaction,
                prepared_token,
                legacy_grant,
                include_transaction=True,
            ),
            legacy=True,
        )
    )
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)

    recovered = vault.recover_prepared_transactions()

    assert len(recovered) == 1
    assert recovered[0].prepared_token == prepared_token
    assert recovered[0].requested_scopes == ("read:user", "repo")
    assert recovered[0].attempt_key == (
        "d2cc48c9d2eb4eecefcdffc857541b10570d7986f1c7f72b42f545fe3bdfc593"
    )
    assert recovered[0].attempt_context == {
        "omega_authorization_context_revision": "4" * 64,
        "omega_migration_status": "reauthorization_required",
    }
    payload = _decrypt_payload(path)
    assert set(payload) == {"schema_version", "records"}
    assert payload["schema_version"] == 6
    assert payload["records"]["migrations"]["legacy-sdk-import"] == {
        "revision": 2,
        "value": {"status": "completed"},
    }


@pytest.mark.parametrize("claimed", [False, True], ids=["unclaimed", "claimed"])
def test_legacy_v1_partition_and_receipt_preserve_recovery_state(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
    claimed: bool,
) -> None:
    legacy_transaction = replace(
        transaction,
        partition_key=_legacy_partition_key(transaction),
    )
    legacy_grant = replace(grant, token_partition_key=legacy_transaction.partition_key)
    path = tmp_path / "oauth-vault.json"
    path.write_bytes(
        _encrypt_document(
            _legacy_v1_document(
                legacy_transaction,
                prepared_token,
                legacy_grant,
                claimed=claimed,
            ),
            legacy=True,
        )
    )
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)

    receipts = vault.claim_committed_grants()
    partition = vault.load_token_partition(legacy_transaction.partition_key)

    assert receipts == ()
    assert partition.token == prepared_token
    assert partition.dependent_plugin_ids == ("plugin.alpha",)
    assert partition.migration_status == "legacy_v1"
    assert partition.reauthorization_required is True
    assert (
        vault.maximum_connection_generation(
            "plugin.alpha",
            app_id="app.github",
            account_key_sha256="1" * 64,
        )
        == 0
    )
    records = _decrypt_payload(path)["records"]
    migrated = records["committed_grants"][legacy_grant.receipt_id]
    assert migrated["claimed_authority_revision"] == (
        legacy_grant.authority_revision if claimed else None
    )
    assert migrated["connection_generation"] == 0
    assert migrated["status"] == "reauthorization_required"


def test_connection_generation_floor_survives_revoke_prune_and_restart(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)
    committed = replace(grant, connection_generation=1)
    _commit(vault, transaction, prepared_token, committed)
    vault.mark_grant_claimed(
        committed.receipt_id,
        authority_revision=committed.authority_revision,
    )
    pending, remaining = vault.prepare_revocation(
        transaction.partition_key,
        plugin_id=transaction.plugin_id,
    )
    assert remaining == ()
    vault.finalize_revocation(
        transaction.partition_key,
        plugin_id=transaction.plugin_id,
        expected_revision=pending.revision,
    )
    vault.store_credential(
        "oauth-client/prune/secret",
        OAuthClientCredential("prune-client", None),
        expected_revision=None,
    )

    restarted = OAuthCredentialVault(
        tmp_path,
        key_provider=lambda: _KEY,
        clock=lambda: 150.0,
    )

    assert restarted.claim_committed_grants() == ()
    assert (
        restarted.maximum_connection_generation(
            transaction.plugin_id,
            app_id=transaction.app_id,
            account_key_sha256=transaction.account_key_sha256,
        )
        == 1
    )
    assert (
        restarted.maximum_connection_generation(
            "plugin.beta",
            app_id=transaction.app_id,
            account_key_sha256=transaction.account_key_sha256,
        )
        is None
    )


def test_schema_two_migration_seeds_nonprunable_generation_floor(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)
    committed = replace(grant, connection_generation=4)
    _commit(vault, transaction, prepared_token, committed)
    records, _plaintext = _decrypt_document(vault.path)
    records["migrations"] = {
        key: value
        for key, value in records["migrations"].items()
        if not key.startswith("connection-generation-floor:")
    }
    _downgrade_grants_to_schema_four(records)
    records.pop("registrations")  # a genuine schema-2 document never had this map
    vault.path.write_bytes(_encrypt_document(records, schema_version=2))

    migrated = OAuthCredentialVault(
        tmp_path,
        key_provider=lambda: _KEY,
        clock=lambda: 150.0,
    )
    assert (
        migrated.maximum_connection_generation(
            transaction.plugin_id,
            app_id=transaction.app_id,
            account_key_sha256=transaction.account_key_sha256,
        )
        == 4
    )
    assert _decrypt_payload(vault.path)["schema_version"] == 6


def test_schema_three_transaction_migrates_to_authorization_code_flow(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)
    vault.begin_transaction(transaction)
    records, _plaintext = _decrypt_document(vault.path)
    raw_transaction = records["transactions"][transaction.state_sha256]["value"]
    for key in (
        "authorization_flow",
        "device_code",
        "device_interval_seconds",
        "device_next_poll_at",
    ):
        raw_transaction.pop(key)
    records.pop("registrations")  # a genuine schema-3 document never had this map
    vault.path.write_bytes(_encrypt_document(records, schema_version=3))

    recovered = OAuthCredentialVault(
        tmp_path,
        key_provider=lambda: _KEY,
        clock=lambda: 150.0,
    ).load_transaction(transaction.state_sha256)

    assert recovered.authorization_flow == "authorization_code_pkce"
    assert recovered.device_code is None
    assert recovered.device_interval_seconds is None
    assert recovered.device_next_poll_at is None
    payload = _decrypt_payload(vault.path)
    assert payload["schema_version"] == 6
    assert payload["records"]["migrations"]["oauth-vault-inner-v3"] == {
        "revision": 0,
        "value": {"migration_status": "completed"},
    }


def test_new_exact_authorization_is_runtime_eligible_beside_legacy_cleanup_state(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    legacy_transaction = replace(
        transaction,
        partition_key=_legacy_partition_key(transaction),
    )
    legacy_grant = replace(grant, token_partition_key=legacy_transaction.partition_key)
    path = tmp_path / "oauth-vault.json"
    path.write_bytes(
        _encrypt_document(
            _legacy_v1_document(
                legacy_transaction,
                prepared_token,
                legacy_grant,
                claimed=True,
            ),
            legacy=True,
        )
    )
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)
    replacement = replace(
        transaction,
        state_sha256="b" * 64,
        attempt_revision=1,
    )

    vault.begin_transaction(replacement)
    vault.prepare_token(replacement.state_sha256, prepared_token)
    current = vault.commit_authorization(
        replacement.state_sha256,
        replace(
            grant,
            receipt_id="4" * 64,
            connection_generation=1,
        ),
    )

    assert current.partition_key == transaction.partition_key
    assert current.migration_status is None
    assert current.reauthorization_required is False
    legacy = vault.load_token_partition(legacy_transaction.partition_key)
    assert legacy.migration_status == "legacy_v1"
    assert legacy.reauthorization_required is True


def test_legacy_v1_revocation_pending_survives_migration_and_finalizes(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    legacy_transaction = replace(
        transaction,
        partition_key=_legacy_partition_key(transaction),
    )
    legacy_grant = replace(grant, token_partition_key=legacy_transaction.partition_key)
    path = tmp_path / "oauth-vault.json"
    path.write_bytes(
        _encrypt_document(
            _legacy_v1_document(
                legacy_transaction,
                prepared_token,
                legacy_grant,
                revocation_pending=True,
            ),
            legacy=True,
        )
    )
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)

    pending, remaining = vault.prepare_revocation(
        legacy_transaction.partition_key,
        plugin_id=legacy_transaction.plugin_id,
    )

    assert remaining == ()
    assert pending.token == prepared_token
    assert pending.revocation_pending_plugin_id == "plugin.alpha"
    assert pending.migration_status == "legacy_v1"
    vault.finalize_revocation(
        legacy_transaction.partition_key,
        plugin_id=legacy_transaction.plugin_id,
        expected_revision=pending.revision,
    )
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        vault.load_token_partition(legacy_transaction.partition_key)


def test_nonlegacy_partition_rejects_reauthorization_and_pending_combination(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)
    _commit(vault, transaction, prepared_token, grant)
    records, _plaintext = _decrypt_document(vault.path)
    partition = records["tokens"][transaction.partition_key]["value"]
    partition["reauthorization_required"] = True
    partition["revocation_pending_plugin_id"] = transaction.plugin_id
    vault.path.write_bytes(_encrypt_document(records))

    with pytest.raises(ValueError, match="^oauth_vault_unavailable$"):
        vault.load_token_partition(transaction.partition_key)


def test_unreconstructable_legacy_v1_record_has_explicit_migration_error(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    legacy_transaction = replace(
        transaction,
        partition_key=_legacy_partition_key(transaction),
    )
    document = _legacy_v1_document(
        legacy_transaction,
        prepared_token,
        replace(grant, token_partition_key=legacy_transaction.partition_key),
        include_transaction=True,
    )
    document["transactions"][legacy_transaction.state_sha256]["value"].pop("plugin_id")
    (tmp_path / "oauth-vault.json").write_bytes(_encrypt_document(document, legacy=True))
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 150.0)

    with pytest.raises(ValueError, match="^oauth_vault_migration_required$"):
        vault.recover_prepared_transactions()


def test_vault_requires_lock_for_every_document_read_and_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    original_read = McpStateStore.read_bytes
    original_write = McpStateStore.write_bytes

    def checked_read(store: McpStateStore) -> bytes | None:
        store._require_active_directory()
        observed.append("read")
        return original_read(store)

    def checked_write(store: McpStateStore, payload: bytes) -> None:
        store._require_active_directory()
        observed.append("write")
        original_write(store, payload)

    monkeypatch.setattr(McpStateStore, "read_bytes", checked_read)
    monkeypatch.setattr(McpStateStore, "write_bytes", checked_write)
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY)

    vault.store_credential(
        "oauth-client/google.desktop/secret",
        OAuthClientCredential(client_id="public-id", client_secret=None),
        expected_revision=None,
    )
    vault.load_credential("oauth-client/google.desktop/secret")

    assert observed == ["read", "write", "read"]


def test_vault_rejects_locked_wrong_key_corruption_and_wrong_document_shape(
    tmp_path: Path,
) -> None:
    locked = OAuthCredentialVault(
        tmp_path / "locked",
        key_provider=lambda: (_ for _ in ()).throw(ValueError("operator-locked")),
    )
    with pytest.raises(ValueError, match="oauth_vault_locked"):
        locked.store_credential(
            "oauth-client/google.desktop/secret",
            OAuthClientCredential(client_id="public-id", client_secret="private-value"),
            expected_revision=None,
        )
    assert not (tmp_path / "locked" / "oauth-vault.json").exists()

    healthy = OAuthCredentialVault(tmp_path / "state", key_provider=lambda: _KEY)
    healthy.store_credential(
        "oauth-client/google.desktop/secret",
        OAuthClientCredential(client_id="public-id", client_secret="private-value"),
        expected_revision=None,
    )
    wrong_key = OAuthCredentialVault(tmp_path / "state", key_provider=lambda: b"x" * 32)
    with pytest.raises(ValueError, match="oauth_vault_unavailable"):
        wrong_key.load_credential("oauth-client/google.desktop/secret")

    path = tmp_path / "state" / "oauth-vault.json"
    path.write_bytes(b"not-an-envelope")
    with pytest.raises(ValueError, match="oauth_vault_unavailable"):
        healthy.load_credential("oauth-client/google.desktop/secret")

    malformed = {name: {} for name in _DOCUMENT_MAPS}
    malformed["unexpected"] = {}
    path.write_bytes(_encrypt_document(malformed))
    with pytest.raises(ValueError, match="oauth_vault_unavailable"):
        healthy.load_credential("oauth-client/google.desktop/secret")


@pytest.mark.parametrize("case", _MALFORMED_DOCUMENT_CASES)
def test_corruption_in_any_namespace_blocks_unrelated_read(
    tmp_path: Path,
    case: str,
) -> None:
    path = tmp_path / "oauth-vault.json"
    path.write_bytes(_encrypt_document(_malformed_document(case)))
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY)

    with pytest.raises(ValueError, match="^oauth_vault_unavailable$"):
        vault.load_credential("oauth-client/unrelated/secret")


@pytest.mark.parametrize("case", _MALFORMED_DOCUMENT_CASES)
def test_corruption_in_any_namespace_blocks_unrelated_write(
    tmp_path: Path,
    case: str,
) -> None:
    path = tmp_path / "oauth-vault.json"
    path.write_bytes(_encrypt_document(_malformed_document(case)))
    before = path.read_bytes()
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY)

    with pytest.raises(ValueError, match="^oauth_vault_unavailable$"):
        vault.store_credential(
            "oauth-client/unrelated/secret",
            OAuthClientCredential(client_id="unrelated-client-id", client_secret=None),
            expected_revision=None,
        )

    assert path.read_bytes() == before


def test_vault_enforces_two_mib_document_limit(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "oauth-vault.json").write_bytes(b"x" * (_MAX_DOCUMENT_BYTES + 1))
    vault = OAuthCredentialVault(root, key_provider=lambda: _KEY)

    with pytest.raises(ValueError, match="oauth_vault_limit_exceeded"):
        vault.load_credential("oauth-client/google.desktop/secret")

    with pytest.raises(ValueError, match="oauth_vault_limit_invalid"):
        OAuthCredentialVault(
            tmp_path / "larger",
            key_provider=lambda: _KEY,
            max_document_bytes=_MAX_DOCUMENT_BYTES + 1,
        )


def test_vault_enforces_512_total_record_limit_without_changing_document(
    tmp_path: Path,
) -> None:
    document = {name: {} for name in _DOCUMENT_MAPS}
    document["credentials"] = {
        f"oauth-client/provider-{index}/secret": {
            "revision": 0,
            "value": {"client_id": "public-id", "client_secret": None},
        }
        for index in range(512)
    }
    (tmp_path / "oauth-vault.json").write_bytes(_encrypt_document(document))
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY)
    credential = OAuthClientCredential(client_id="public-id", client_secret=None)
    before = (tmp_path / "oauth-vault.json").read_bytes()

    with pytest.raises(ValueError, match="oauth_vault_entry_limit_exceeded"):
        vault.store_credential(
            "oauth-client/provider-overflow/secret",
            credential,
            expected_revision=None,
        )

    assert (tmp_path / "oauth-vault.json").read_bytes() == before


def test_credential_compare_and_swap_rejects_stale_revision(vault: OAuthCredentialVault) -> None:
    credential = OAuthClientCredential(client_id="public-id", client_secret=None)
    assert (
        vault.store_credential(
            "oauth-client/google.desktop/secret",
            credential,
            expected_revision=None,
        )
        == 0
    )

    with pytest.raises(ValueError, match="oauth_vault_revision_conflict"):
        vault.store_credential(
            "oauth-client/google.desktop/secret",
            credential,
            expected_revision=None,
        )

    assert (
        vault.store_credential(
            "oauth-client/google.desktop/secret",
            credential,
            expected_revision=0,
        )
        == 1
    )


def test_credential_cas_rejects_corrupt_existing_record_without_overwriting(
    tmp_path: Path,
) -> None:
    document = {name: {} for name in _DOCUMENT_MAPS}
    document["credentials"] = {
        "oauth-client/google.desktop/secret": {
            "value": {"client_id": "public-id", "client_secret": "private-value"},
        }
    }
    path = tmp_path / "oauth-vault.json"
    path.write_bytes(_encrypt_document(document))
    before = path.read_bytes()
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY)

    with pytest.raises(ValueError, match="oauth_vault_unavailable"):
        vault.store_credential(
            "oauth-client/google.desktop/secret",
            OAuthClientCredential(client_id="replacement", client_secret=None),
            expected_revision=None,
        )

    assert path.read_bytes() == before


def test_failed_atomic_write_preserves_previous_ciphertext_and_no_plaintext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY)
    vault.store_credential(
        "oauth-client/google.desktop/secret",
        OAuthClientCredential(client_id="public-id", client_secret="initial-private-value"),
        expected_revision=None,
    )
    path = tmp_path / "oauth-vault.json"
    before = path.read_bytes()
    attempted: list[bytes] = []

    def fail_write(_store: McpStateStore, payload: bytes) -> None:
        attempted.append(payload)
        raise OSError("simulated replace failure")

    monkeypatch.setattr(McpStateStore, "write_bytes", fail_write)
    with pytest.raises(ValueError, match="oauth_vault_unavailable"):
        vault.store_credential(
            "oauth-client/google.desktop/secret",
            OAuthClientCredential(client_id="public-id", client_secret="new-private-value"),
            expected_revision=0,
        )

    assert path.read_bytes() == before
    assert b"new-private-value" not in before
    assert attempted and b"new-private-value" not in attempted[0]
    assert not tuple(tmp_path.glob("*.part"))


def test_registration_round_trips_through_the_encrypted_document(tmp_path: Path) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: b"k" * 32)
    registration = OAuthClientRegistration(
        authorization_issuer="https://mcp.notion.com",
        profile_id="notion.desktop",
        client_id="dyn-client-123",
        registration_access_token="reg-token-abc",
        registration_client_uri="https://mcp.notion.com/register/dyn-client-123",
        registered_at=1_800_000_000.0,
        response_sha256="a" * 64,
    )
    vault.store_registration(registration)
    loaded = vault.load_registration("https://mcp.notion.com", "notion.desktop")
    assert loaded == registration
    assert vault.load_registration("https://mcp.notion.com", "linear.desktop") is None


def test_registration_deletion_removes_the_record_and_is_idempotent(tmp_path: Path) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: b"k" * 32)
    registration = OAuthClientRegistration(
        authorization_issuer="https://mcp.notion.com",
        profile_id="notion.desktop",
        client_id="dyn-client-123",
        registration_access_token=None,
        registration_client_uri=None,
        registered_at=1_800_000_000.0,
        response_sha256="a" * 64,
    )
    vault.store_registration(registration)
    deleted = vault.delete_registration("https://mcp.notion.com", "notion.desktop")
    assert deleted == registration
    assert vault.load_registration("https://mcp.notion.com", "notion.desktop") is None
    assert vault.delete_registration("https://mcp.notion.com", "notion.desktop") is None


def test_a_v4_document_migrates_forward_with_an_empty_registrations_map(tmp_path: Path) -> None:
    vault_path = tmp_path / "oauth-vault.json"
    write_legacy_v4_fixture_document(vault_path)  # helper seeds a schema-4 document, no key access
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: b"k" * 32)
    assert vault.load_registration("https://mcp.notion.com", "notion.desktop") is None


def test_a_real_unpadded_schema_five_document_migrates_forward_with_credentials_intact(
    tmp_path: Path,
) -> None:
    # This is the format every vault on disk is actually in today: a document
    # shaped by _DOCUMENT_MAPS_THROUGH_V5, with no "registrations" key at all
    # (not even an empty one) — unlike a fixture built from the padded,
    # current _DOCUMENT_MAPS.
    vault_path = tmp_path / "oauth-vault.json"
    document: dict[str, object] = {name: {} for name in _DOCUMENT_MAPS_THROUGH_V5}
    document["credentials"] = {
        "oauth-client/google.desktop/secret": {
            "revision": 0,
            "value": {"client_id": "public-id", "client_secret": None},
        }
    }
    vault_path.write_bytes(_encrypt_document(document, schema_version=5))
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: b"k" * 32)

    assert vault.load_registration("https://mcp.notion.com", "notion.desktop") is None
    assert vault.load_credential("oauth-client/google.desktop/secret") == (
        0,
        OAuthClientCredential(client_id="public-id", client_secret=None),
    )
    payload = _decrypt_payload(vault_path)
    assert payload["schema_version"] == 6
    assert payload["records"]["registrations"] == {}
    assert payload["records"]["migrations"]["oauth-vault-inner-v5"] == {
        "revision": 0,
        "value": {"migration_status": "completed"},
    }


def test_a_real_unpadded_schema_four_document_migrates_forward_with_credentials_intact(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "oauth-vault.json"
    document: dict[str, object] = {name: {} for name in _DOCUMENT_MAPS_THROUGH_V5}
    document["credentials"] = {
        "oauth-client/google.desktop/secret": {
            "revision": 0,
            "value": {"client_id": "public-id", "client_secret": None},
        }
    }
    vault_path.write_bytes(_encrypt_document(document, schema_version=4))
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: b"k" * 32)

    assert vault.load_registration("https://mcp.notion.com", "notion.desktop") is None
    assert vault.load_credential("oauth-client/google.desktop/secret") == (
        0,
        OAuthClientCredential(client_id="public-id", client_secret=None),
    )
    payload = _decrypt_payload(vault_path)
    assert payload["schema_version"] == 6
    assert payload["records"]["registrations"] == {}
    assert payload["records"]["migrations"]["oauth-vault-inner-v4"] == {
        "revision": 0,
        "value": {"migration_status": "completed"},
    }
    assert payload["records"]["migrations"]["oauth-vault-inner-v5"] == {
        "revision": 0,
        "value": {"migration_status": "completed"},
    }


def test_historical_document_shape_does_not_follow_future_record_kinds() -> None:
    historical_names = frozenset(
        {
            "credentials",
            "transactions",
            "tokens",
            "clients",
            "claims",
            "account_partitions",
            "committed_grants",
            "consumed_transactions",
            "migrations",
        }
    )
    assert _DOCUMENT_MAPS_THROUGH_V5 == historical_names
    assert oauth_vault_module._DOCUMENT_MAPS - _DOCUMENT_MAPS_THROUGH_V5 == {"registrations"}

    # Re-evaluate only the historical declaration with an extended current
    # schema. Existing encrypted documents must keep their original shape.
    source = Path(oauth_vault_module.__file__).read_text(encoding="utf-8")
    declaration = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_DOCUMENT_MAPS_THROUGH_V5"
            for target in node.targets
        )
    )
    expression = ast.Expression(body=declaration.value)
    extended_schema = oauth_vault_module._DOCUMENT_MAPS | {"future_record_kind"}
    actual = eval(
        compile(expression, "<historical-vault-shape>", "eval"),
        {"__builtins__": {"frozenset": frozenset}, "_DOCUMENT_MAPS": extended_schema},
    )
    assert actual == historical_names


def test_document_shape_through_schema_five_survives_the_full_migration_chain(
    tmp_path: Path,
) -> None:
    # Guard: a document built from exactly _DOCUMENT_MAPS_THROUGH_V5 — the
    # frozen historical shape, with no "registrations" key — must still
    # migrate forward cleanly. If a future record kind extends
    # _DOCUMENT_MAPS without also freezing a new historical constant and
    # wiring the migration gates to it (the exact mistake this constant
    # exists to prevent), a document built this way stops migrating and
    # this test fails here instead of on a real user's vault.
    vault_path = tmp_path / "oauth-vault.json"
    document: dict[str, object] = {name: {} for name in _DOCUMENT_MAPS_THROUGH_V5}
    assert "registrations" not in document
    vault_path.write_bytes(_encrypt_document(document, schema_version=5))
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: b"k" * 32)

    assert vault.load_registration("https://mcp.notion.com", "notion.desktop") is None
    payload = _decrypt_payload(vault_path)
    assert set(payload["records"]) == _DOCUMENT_MAPS
    assert payload["records"]["registrations"] == {}


def test_begin_transaction_supersedes_partition_and_state_is_single_use(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
) -> None:
    vault.begin_transaction(transaction)
    newer = replace(transaction, state_sha256="b" * 64, attempt_revision=1)

    vault.begin_transaction(newer)

    with pytest.raises(ValueError, match="oauth_transaction_superseded"):
        vault.load_transaction(transaction.state_sha256)
    assert vault.load_transaction(newer.state_sha256) == newer


def test_begin_transaction_rejects_nonincreasing_attempt_revision(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
) -> None:
    vault.begin_transaction(transaction)

    with pytest.raises(ValueError, match="oauth_transaction_revision_conflict"):
        vault.begin_transaction(replace(transaction, state_sha256="b" * 64))

    assert vault.load_transaction(transaction.state_sha256) == transaction


def test_transaction_expiry_fails_closed(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
) -> None:
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=lambda: 201.0)
    vault.begin_transaction(transaction)

    with pytest.raises(ValueError, match="oauth_transaction_expired"):
        vault.load_transaction(transaction.state_sha256)


def test_prepared_token_and_containing_records_redact_repr(
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
) -> None:
    prepared = replace(transaction, prepared_token=prepared_token)
    for value in (prepared_token, prepared):
        rendered = repr(value)
        assert "access-private-value" not in rendered
        assert "refresh-private-value" not in rendered
        assert "pkce-private-value" not in rendered


def test_commit_moves_prepared_token_atomically_and_consumes_state(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)

    partition = vault.load_token_partition(transaction.partition_key)
    assert partition.partition_key == transaction.partition_key
    assert partition.profile_id == transaction.profile_id
    assert partition.client_id == "client-id"
    assert partition.token == prepared_token
    assert partition.dependent_plugin_ids == (transaction.plugin_id,)
    assert partition.revision == 0
    with pytest.raises(ValueError, match="oauth_transaction_missing"):
        vault.load_transaction(transaction.state_sha256)
    with pytest.raises(ValueError, match="oauth_transaction_missing"):
        vault.commit_authorization(transaction.state_sha256, grant)
    raw = vault.path.read_bytes()
    for secret in (
        b"pkce-private-value",
        b"access-private-value",
        b"refresh-private-value",
        b"client-private-value",
    ):
        assert secret not in raw
    assert "access-private-value" not in repr(partition)


def test_commit_uses_exact_client_binding_with_multiple_registrations(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    vault.store_credential(
        "oauth-client/google.desktop/secret",
        OAuthClientCredential(client_id="google-client-id", client_secret=None),
        expected_revision=None,
    )
    vault.store_credential(
        "oauth-client/github.desktop/secret",
        OAuthClientCredential(client_id="github-client-id", client_secret=None),
        expected_revision=None,
    )
    bound = replace(
        transaction,
        client_id="github-client-id",
        partition_key=_partition_key(client_id="github-client-id"),
        credential_identity=_credential_identity(client_id="github-client-id"),
    )

    _commit(
        vault,
        bound,
        prepared_token,
        replace(grant, token_partition_key=bound.partition_key),
    )

    assert vault.load_token_partition(bound.partition_key).client_id == "github-client-id"


def test_commit_does_not_inherit_unrelated_same_issuer_sdk_client(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    vault.store_sdk_client_info(
        "unrelated-registration",
        _StoredClientInfo(
            client_id="wrong-client-id",
            issuer=transaction.authorization_issuer,
            redirect_uri="http://127.0.0.1:9999/callback",
            token_endpoint_auth_method="none",
        ),
    )
    bound = replace(
        transaction,
        client_id="exact-preregistered-client-id",
        partition_key=_partition_key(client_id="exact-preregistered-client-id"),
        credential_identity=_credential_identity(client_id="exact-preregistered-client-id"),
    )

    _commit(
        vault,
        bound,
        prepared_token,
        replace(grant, token_partition_key=bound.partition_key),
    )

    assert (
        vault.load_token_partition(bound.partition_key).client_id == "exact-preregistered-client-id"
    )


def test_commit_preserves_cimd_client_binding_without_preregistered_fallback(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    vault.store_sdk_client_info(
        "unrelated-registration",
        _StoredClientInfo(
            client_id="wrong-client-id",
            issuer=transaction.authorization_issuer,
            redirect_uri="http://127.0.0.1:9999/callback",
            token_endpoint_auth_method="none",
        ),
    )
    client_metadata_url = "https://omega2ai.com/oauth/client-metadata.json"
    bound = replace(
        transaction,
        client_id=client_metadata_url,
        partition_key=_partition_key(client_id=client_metadata_url),
        credential_identity=_credential_identity(client_id=client_metadata_url),
    )

    _commit(
        vault,
        bound,
        prepared_token,
        replace(grant, token_partition_key=bound.partition_key),
    )

    assert vault.load_token_partition(bound.partition_key).client_id == client_metadata_url


def test_shared_partition_rejects_a_different_client_registration_binding(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    cross_bound = replace(
        transaction,
        plugin_id="plugin.beta",
        app_id="app.beta",
        client_id="https://omega2ai.com/oauth/client-metadata.json",
        credential_identity=_credential_identity(
            client_id="https://omega2ai.com/oauth/client-metadata.json"
        ),
        state_sha256="b" * 64,
        attempt_revision=1,
    )
    cross_bound = replace(cross_bound, attempt_key=_attempt_key(cross_bound))
    cross_bound_grant = replace(
        grant,
        receipt_id="4" * 64,
        plugin_id=cross_bound.plugin_id,
        app_id=cross_bound.app_id,
    )
    vault.begin_transaction(cross_bound)
    vault.prepare_token(cross_bound.state_sha256, prepared_token)

    with pytest.raises(ValueError, match="^oauth_token_partition_conflict$"):
        vault.commit_authorization(cross_bound.state_sha256, cross_bound_grant)

    assert vault.load_token_partition(transaction.partition_key).client_id == "client-id"


def test_commit_rejects_unprepared_or_mismatched_grant(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    grant: OAuthCommittedGrant,
) -> None:
    vault.begin_transaction(transaction)
    with pytest.raises(ValueError, match="oauth_transaction_unprepared"):
        vault.commit_authorization(transaction.state_sha256, grant)

    vault.prepare_token(
        transaction.state_sha256,
        OAuthPreparedToken(
            access_token="access-private-value",
            refresh_token=None,
            token_type="Bearer",
            scopes=transaction.scopes,
            expires_at=None,
        ),
    )
    with pytest.raises(ValueError, match="oauth_grant_mismatch"):
        vault.commit_authorization(
            transaction.state_sha256,
            replace(grant, plugin_id="plugin.forged"),
        )
    assert vault.load_transaction(transaction.state_sha256).prepared_token is not None


def test_refresh_compare_and_swap_rejects_stale_revision(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    refreshed = replace(
        prepared_token,
        access_token="new-access-private-value",
        refresh_token="new-refresh-private-value",
    )

    current = vault.replace_token_partition(
        transaction.partition_key,
        refreshed,
        expected_revision=0,
    )

    assert current.revision == 1
    assert current.token == refreshed
    with pytest.raises(ValueError, match="oauth_token_revision_conflict"):
        vault.replace_token_partition(
            transaction.partition_key,
            prepared_token,
            expected_revision=0,
        )
    assert vault.load_token_partition(transaction.partition_key) == current


def test_plugin_update_checkpoint_restores_exact_predecessor_token_and_grant(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    """A narrower candidate token cannot destroy the predecessor on rollback."""

    _commit(vault, transaction, prepared_token, grant)
    vault.mark_grant_claimed(grant.receipt_id, authority_revision=grant.authority_revision)
    predecessor_partition = vault.load_token_partition(transaction.partition_key)
    predecessor_receipts = vault.committed_grant_receipts()
    checkpoint = vault.begin_plugin_update_recovery(
        "plugin-update-recovery-" + "a" * 64,
        plugin_id=transaction.plugin_id,
        account_key_sha256=transaction.account_key_sha256,
        app_ids=(transaction.app_id,),
    )
    with pytest.raises(
        ValueError,
        match="^oauth_plugin_update_recovery_conflict$",
    ):
        vault.begin_plugin_update_recovery(
            checkpoint["checkpoint_id"],
            plugin_id="different.plugin",
            account_key_sha256=transaction.account_key_sha256,
            app_ids=(transaction.app_id,),
        )

    candidate = replace(
        transaction,
        state_sha256="b" * 64,
        attempt_revision=1,
        scopes=("read:user",),
        requested_scopes=("read:user",),
    )
    candidate = replace(candidate, attempt_key=_attempt_key(candidate))
    candidate_token = replace(
        prepared_token,
        access_token="candidate-access-private-value",
        refresh_token="candidate-refresh-private-value",
        scopes=("read:user",),
    )
    candidate_grant = replace(
        grant,
        receipt_id="4" * 64,
        connection_revision="a" * 64,
        authority_revision="b" * 64,
        capability_binding_revision="c" * 64,
        scopes=("read:user",),
        claimed_authority_revision=None,
        connection_generation=1,
    )
    _commit(vault, candidate, candidate_token, candidate_grant)
    candidate_partition = vault.load_token_partition(transaction.partition_key)
    assert candidate_partition.token == candidate_token

    restored = vault.restore_plugin_update_recovery(
        checkpoint["checkpoint_id"],
        expected_revision=checkpoint["revision"],
    )

    assert restored == checkpoint
    restored_partition = vault.load_token_partition(transaction.partition_key)
    assert replace(restored_partition, revision=predecessor_partition.revision) == (
        predecessor_partition
    )
    assert restored_partition.revision > candidate_partition.revision
    assert vault.committed_grant_receipts() == predecessor_receipts
    vault.finalize_plugin_update_recovery(
        checkpoint["checkpoint_id"],
        expected_revision=checkpoint["revision"],
    )


def test_plugin_update_restore_preserves_a_newer_compatible_token_revision(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    checkpoint = vault.begin_plugin_update_recovery(
        "plugin-update-recovery-" + "c" * 64,
        plugin_id=transaction.plugin_id,
        account_key_sha256=transaction.account_key_sha256,
        app_ids=(transaction.app_id,),
    )
    current = vault.load_token_partition(transaction.partition_key)
    refreshed_token = replace(
        prepared_token,
        access_token="refreshed-access-private-value",
        refresh_token="refreshed-refresh-private-value",
    )
    refreshed = vault.replace_token_partition(
        transaction.partition_key,
        refreshed_token,
        expected_revision=current.revision,
    )

    vault.restore_plugin_update_recovery(
        checkpoint["checkpoint_id"],
        expected_revision=checkpoint["revision"],
    )

    restored = vault.load_token_partition(transaction.partition_key)
    assert restored.token == refreshed_token
    assert restored.revision >= refreshed.revision
    assert vault.committed_grant_receipts() == (grant,)


def test_plugin_update_checkpoint_discard_is_exact_and_idempotent(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    """An unsealed lifecycle update can discard only its own durable checkpoint."""

    _commit(vault, transaction, prepared_token, grant)
    checkpoint = vault.begin_plugin_update_recovery(
        "plugin-update-recovery-" + "9" * 64,
        plugin_id=transaction.plugin_id,
        account_key_sha256=transaction.account_key_sha256,
        app_ids=(transaction.app_id,),
    )

    with pytest.raises(
        ValueError,
        match="^oauth_plugin_update_recovery_conflict$",
    ):
        vault.discard_plugin_update_recovery(
            checkpoint["checkpoint_id"],
            plugin_id="different.plugin",
            account_key_sha256=transaction.account_key_sha256,
        )

    vault.discard_plugin_update_recovery(
        checkpoint["checkpoint_id"],
        plugin_id=transaction.plugin_id,
        account_key_sha256=transaction.account_key_sha256,
    )
    vault.discard_plugin_update_recovery(
        checkpoint["checkpoint_id"],
        plugin_id=transaction.plugin_id,
        account_key_sha256=transaction.account_key_sha256,
    )
    with pytest.raises(
        ValueError,
        match="^oauth_plugin_update_recovery_missing$",
    ):
        vault.restore_plugin_update_recovery(
            checkpoint["checkpoint_id"],
            expected_revision=checkpoint["revision"],
        )
    assert vault.load_token_partition(transaction.partition_key).token == prepared_token
    assert vault.committed_grant_receipts() == (grant,)


def test_plugin_update_restore_merges_a_later_shared_partition_dependent(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    checkpoint = vault.begin_plugin_update_recovery(
        "plugin-update-recovery-" + "d" * 64,
        plugin_id=transaction.plugin_id,
        account_key_sha256=transaction.account_key_sha256,
        app_ids=(transaction.app_id,),
    )
    beta_transaction = replace(
        transaction,
        plugin_id="plugin.beta",
        app_id="app.beta",
        state_sha256="e" * 64,
        attempt_revision=1,
    )
    beta_transaction = replace(
        beta_transaction,
        attempt_key=_attempt_key(beta_transaction),
    )
    beta_grant = replace(
        grant,
        receipt_id="f" * 64,
        plugin_id=beta_transaction.plugin_id,
        app_id=beta_transaction.app_id,
        connection_revision="a" * 64,
        authority_revision="b" * 64,
        capability_binding_revision="c" * 64,
    )
    _commit(vault, beta_transaction, prepared_token, beta_grant)

    vault.restore_plugin_update_recovery(
        checkpoint["checkpoint_id"],
        expected_revision=checkpoint["revision"],
    )

    assert vault.committed_grant_receipts() == tuple(
        sorted((grant, beta_grant), key=lambda item: item.receipt_id)
    )
    restored = vault.load_token_partition(transaction.partition_key)
    assert restored.dependent_plugin_ids == ("plugin.alpha", "plugin.beta")


def test_candidate_attempt_can_be_enumerated_before_lifecycle_request_journal(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
) -> None:
    identity = {
        "plugin_id": transaction.plugin_id,
        "target_version": "2.0.0",
        "target_signing_key_id": "publisher.key",
        "target_manifest_sha256": "1" * 64,
        "target_package_sha256": "2" * 64,
        "candidate_binding_revision": "3" * 64,
    }
    candidate = replace(
        transaction,
        attempt_context={
            "schema_version": 2,
            "candidate_identity": identity,
        },
    )
    vault.begin_transaction(candidate)

    assert vault.candidate_attempt_state_sha256(identity) == (
        transaction.state_sha256,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"account_key_sha256": "f" * 64},
        {"authorization_issuer": "https://forged.example"},
        {"credential_identity": "f" * 64},
        {"plugin_id": "plugin.new", "app_id": "app.new"},
    ],
    ids=["account", "issuer", "credential", "new-dependent"],
)
def test_reauthorization_rejects_partition_identity_mismatch_or_new_dependent(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
    changes: dict[str, str],
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    vault.mark_grant_claimed(grant.receipt_id, authority_revision=grant.authority_revision)
    vault.mark_partition_reauthorization_required(transaction.partition_key)
    candidate = replace(
        transaction,
        state_sha256="b" * 64,
        attempt_revision=1,
        **changes,
    )
    candidate = replace(candidate, attempt_key=_attempt_key(candidate))
    candidate_grant = replace(
        grant,
        receipt_id="4" * 64,
        plugin_id=candidate.plugin_id,
        app_id=candidate.app_id,
        account_key_sha256=candidate.account_key_sha256,
        profile_id=candidate.profile_id,
        profile_revision=candidate.profile_revision,
        claimed_authority_revision=None,
    )
    vault.begin_transaction(candidate)
    vault.prepare_token(candidate.state_sha256, prepared_token)

    with pytest.raises(ValueError, match="^oauth_token_partition_conflict$"):
        vault.commit_authorization(candidate.state_sha256, candidate_grant)

    retained = vault.load_token_partition(transaction.partition_key)
    assert retained.reauthorization_required is True
    assert retained.token == prepared_token


def test_shared_token_survives_until_last_generic_dependent_is_removed(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    second = replace(
        transaction,
        plugin_id="plugin.beta",
        app_id="app.second",
        state_sha256="b" * 64,
        attempt_revision=1,
    )
    second = replace(second, attempt_key=_attempt_key(second))
    second_grant = replace(
        grant,
        receipt_id="4" * 64,
        plugin_id=second.plugin_id,
        app_id=second.app_id,
    )
    vault.begin_transaction(second)
    vault.prepare_token(second.state_sha256, prepared_token)
    joined = vault.commit_authorization(second.state_sha256, second_grant)

    assert joined.dependent_plugin_ids == ("plugin.alpha", "plugin.beta")
    assert vault.remove_dependent(transaction.partition_key, plugin_id="plugin.alpha") == (
        "plugin.beta",
    )
    assert vault.load_token_partition(transaction.partition_key).dependent_plugin_ids == (
        "plugin.beta",
    )
    assert vault.remove_dependent(transaction.partition_key, plugin_id="plugin.beta") == ()

    pending = vault.load_token_partition(transaction.partition_key)
    assert pending.dependent_plugin_ids == ("plugin.beta",)
    assert pending.revocation_pending_plugin_id == "plugin.beta"
    vault.finalize_revocation(
        transaction.partition_key,
        plugin_id="plugin.beta",
        expected_revision=pending.revision,
    )
    with pytest.raises(ValueError, match="oauth_token_partition_missing"):
        vault.load_token_partition(transaction.partition_key)


def test_committed_grant_receipt_is_replayed_until_claimed_once(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)

    assert vault.claim_committed_grants() == (grant,)
    assert vault.claim_committed_grants() == (grant,)
    vault.mark_grant_claimed(grant.receipt_id, authority_revision=grant.authority_revision)

    assert vault.claim_committed_grants() == ()
    vault.mark_grant_claimed(grant.receipt_id, authority_revision=grant.authority_revision)
    with pytest.raises(ValueError, match="oauth_grant_receipt_claim_conflict"):
        vault.mark_grant_claimed(
            grant.receipt_id,
            authority_revision=_binding_revision("f"),
        )


def test_receipt_state_is_exact_content_bound_and_distinguishes_absent_claimed(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    absent = replace(grant, receipt_id="f" * 64)
    assert vault.grant_receipt_state(absent) == "absent"
    _commit(vault, transaction, prepared_token, grant)

    assert vault.committed_grant_receipts() == (grant,)
    assert vault.grant_receipt_state(grant) == "unclaimed"
    with pytest.raises(ValueError, match="^oauth_grant_receipt_conflict$"):
        vault.grant_receipt_state(replace(grant, app_id="app.other"))

    vault.mark_grant_claimed(grant.receipt_id, authority_revision=grant.authority_revision)
    assert vault.grant_receipt_state(grant) == "claimed"
    assert vault.committed_grant_receipts() == (
        replace(grant, claimed_authority_revision=grant.authority_revision),
    )


def test_exact_receipt_claim_is_atomic_typed_and_idempotent(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    absent = replace(grant, receipt_id="f" * 64)
    assert vault.claim_grant_receipt_exact(
        absent,
        authority_revision=absent.authority_revision,
    ) == "absent"
    _commit(vault, transaction, prepared_token, grant)

    claimed = vault.claim_grant_receipt_exact(
        grant,
        authority_revision=grant.authority_revision,
    )
    assert isinstance(claimed, StrEnum)
    assert claimed == "claimed"
    assert vault.claim_grant_receipt_exact(
        grant,
        authority_revision=grant.authority_revision,
    ) == "idempotently_claimed"
    with pytest.raises(ValueError, match="^oauth_grant_receipt_claim_conflict$"):
        vault.claim_grant_receipt_exact(
            grant,
            authority_revision=_binding_revision("f"),
        )
    assert vault.grant_receipt_state(grant) == "claimed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugin_id", "plugin.changed"),
        ("app_id", "app.changed"),
        ("account_key_sha256", "f" * 64),
        ("profile_id", "profile.changed"),
        ("profile_revision", _revision("f")),
        ("token_partition_key", "e" * 64),
        ("connection_revision", _binding_revision("d")),
        ("capability_revision", _binding_revision("c")),
        ("authority_revision", _binding_revision("b")),
        ("capability_binding_revision", _binding_revision("a")),
        ("scopes", ("admin",)),
        ("execution_policy", "standing"),
        ("status", "revocation_pending"),
        ("connection_generation", 7),
    ],
)
def test_exact_receipt_claim_rejects_every_same_id_content_substitution(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
    field: str,
    value: object,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    changed = replace(grant, **{field: value})

    with pytest.raises(ValueError, match="^(oauth_grant_receipt_conflict|oauth_grant_receipt_claim_conflict)$"):
        vault.claim_grant_receipt_exact(
            changed,
            authority_revision=grant.authority_revision,
        )

    assert vault.committed_grant_receipts() == (grant,)


def test_exact_receipt_claim_cas_rejects_a_revocation_race_without_mutation(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    assert vault.grant_receipt_state(grant) == "unclaimed"
    vault.prepare_revocation(grant.token_partition_key, plugin_id=grant.plugin_id)

    with pytest.raises(ValueError, match="^oauth_grant_receipt_conflict$"):
        vault.claim_grant_receipt_exact(
            grant,
            authority_revision=grant.authority_revision,
        )

    stored = vault.committed_grant_receipts()
    assert stored == (replace(grant, status="revocation_pending"),)


def test_exact_receipt_claim_treats_completed_revocation_removal_as_absent(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    pending, remaining = vault.prepare_revocation(
        grant.token_partition_key,
        plugin_id=grant.plugin_id,
    )
    assert remaining == ()
    vault.finalize_revocation(
        grant.token_partition_key,
        plugin_id=grant.plugin_id,
        expected_revision=pending.revision,
    )

    assert vault.claim_grant_receipt_exact(
        grant,
        authority_revision=grant.authority_revision,
    ) == "absent"
    assert vault.committed_grant_receipts() == ()


def test_exact_receipt_claim_and_revocation_are_lock_bounded(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    barrier = Barrier(2)

    def claim() -> object:
        barrier.wait(timeout=2)
        try:
            return vault.claim_grant_receipt_exact(
                grant,
                authority_revision=grant.authority_revision,
            )
        except ValueError as exc:
            return str(exc)

    def revoke() -> object:
        barrier.wait(timeout=2)
        return vault.prepare_revocation(
            grant.token_partition_key,
            plugin_id=grant.plugin_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        revoke_future = executor.submit(revoke)
        claim_result = claim_future.result(timeout=3)
        _pending, remaining = revoke_future.result(timeout=3)

    assert remaining == ()
    assert claim_result in {"claimed", "oauth_grant_receipt_conflict"}
    stored = vault.committed_grant_receipts()[0]
    assert stored.status == "revocation_pending"
    assert stored.claimed_authority_revision in {None, grant.authority_revision}


def test_recovery_returns_prepared_transactions_and_commit_journals_consumed_state(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    vault.begin_transaction(transaction)
    assert vault.recover_prepared_transactions() == ()
    vault.prepare_token(transaction.state_sha256, prepared_token)

    prepared = vault.recover_prepared_transactions()

    assert prepared == (replace(transaction, prepared_token=prepared_token),)
    assert vault.is_transaction_consumed(transaction.state_sha256) is False
    vault.commit_authorization(transaction.state_sha256, grant)
    assert vault.recover_prepared_transactions() == ()
    assert vault.is_transaction_consumed(transaction.state_sha256) is True


@pytest.mark.parametrize(
    ("record", "field", "value"),
    [
        ("grant", "account_key_sha256", "f" * 64),
        ("grant", "scopes", ["write:admin"]),
        ("grant", "status", "reauthorization_required"),
        ("partition", "dependent_plugin_ids", ["plugin.alpha", "plugin.forged"]),
        ("partition", "reauthorization_required", True),
        ("partition-identity", "credential_identity", "f" * 64),
    ],
)
def test_whole_document_rejects_shape_valid_cross_record_forgery(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
    record: str,
    field: str,
    value: object,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    document, _plaintext = _decrypt_document(vault.path)
    if record == "grant":
        document["committed_grants"][grant.receipt_id][field] = value
    else:
        document["tokens"][transaction.partition_key]["value"][field] = value
        if record == "partition-identity":
            document["account_partitions"][transaction.partition_key][field] = value
    vault.path.write_bytes(_encrypt_document(document))

    with pytest.raises(ValueError, match="^oauth_vault_unavailable$"):
        vault.load_token_partition(transaction.partition_key)


@pytest.mark.parametrize(
    "forgery",
    ["claimed-authority-mismatch", "duplicate-live-dependent"],
)
def test_whole_document_rejects_conflicting_grant_receipt_ownership(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
    forgery: str,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    document, _plaintext = _decrypt_document(vault.path)
    stored_grant = document["committed_grants"][grant.receipt_id]
    if forgery == "claimed-authority-mismatch":
        stored_grant["claimed_authority_revision"] = _binding_revision("f")
    else:
        duplicate = json.loads(json.dumps(stored_grant))
        duplicate["receipt_id"] = _binding_revision("4")
        document["committed_grants"][duplicate["receipt_id"]] = duplicate
    vault.path.write_bytes(_encrypt_document(document))

    with pytest.raises(ValueError, match="^oauth_vault_unavailable$"):
        vault.claim_committed_grants()


def test_direct_load_reclaims_more_than_global_limit_expired_transactions(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
) -> None:
    clock = _MutableClock(150.0)
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=clock)
    vault.begin_transaction(transaction)
    document, _plaintext = _decrypt_document(vault.path)
    transactions = document["transactions"]
    template = transactions.pop(transaction.state_sha256)
    states = tuple(sha256(f"expired-{index}".encode()).hexdigest() for index in range(520))
    for index, state_sha256 in enumerate(states):
        record = json.loads(json.dumps(template))
        record["value"]["partition_key"] = sha256(f"expired-partition-{index}".encode()).hexdigest()
        record["value"]["state_sha256"] = state_sha256
        transactions[state_sha256] = record
    vault.path.write_bytes(_encrypt_document(document))
    clock.value = transaction.expires_at

    with pytest.raises(ValueError, match="^oauth_transaction_expired$"):
        vault.load_transaction(states[-1])
    assert vault.load_credential("oauth-client/prune/secret") is None
    vault.store_credential(
        "oauth-client/prune/secret",
        OAuthClientCredential("prune-client", None),
        expected_revision=None,
    )
    pruned, _plaintext = _decrypt_document(vault.path)
    assert pruned["transactions"] == {}


def test_pruning_never_evicts_prepared_transactions_at_entry_limit(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
) -> None:
    clock = _MutableClock(150.0)
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=clock)
    vault.begin_transaction(transaction)
    document, _plaintext = _decrypt_document(vault.path)
    transactions = document["transactions"]
    template = transactions.pop(transaction.state_sha256)
    encoded_token = {
        "access_token": prepared_token.access_token,
        "expires_at": prepared_token.expires_at,
        "refresh_token": prepared_token.refresh_token,
        "scopes": list(prepared_token.scopes),
        "token_type": prepared_token.token_type,
    }
    for index in range(513):
        state_sha256 = sha256(f"prepared-{index}".encode()).hexdigest()
        record = json.loads(json.dumps(template))
        record["value"]["partition_key"] = sha256(
            f"prepared-partition-{index}".encode()
        ).hexdigest()
        record["value"]["prepared_token"] = encoded_token
        record["value"]["state_sha256"] = state_sha256
        transactions[state_sha256] = record
    vault.path.write_bytes(_encrypt_document(document))
    before = vault.path.read_bytes()
    clock.value = transaction.expires_at

    with pytest.raises(ValueError, match="^oauth_vault_entry_limit_exceeded$"):
        vault.load_credential("oauth-client/prune/secret")

    assert vault.path.read_bytes() == before
    retained, _plaintext = _decrypt_document(vault.path)
    assert len(retained["transactions"]) == 513


def test_recent_consumed_burst_evicts_oldest_before_new_mutation_and_keeps_replay(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
) -> None:
    clock = _MutableClock(10_000.0)
    document: dict[str, object] = {name: {} for name in _DOCUMENT_MAPS}
    consumed = document["consumed_transactions"]
    assert isinstance(consumed, dict)
    states = tuple(sha256(f"recent-{index}".encode()).hexdigest() for index in range(512))
    for index, state_sha256 in enumerate(states):
        consumed[state_sha256] = {
            "consumed_at": clock.value - len(states) + index,
            "reason": "denied",
        }
    (tmp_path / "oauth-vault.json").write_bytes(_encrypt_document(document))
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=clock)

    vault.store_credential(
        "oauth-client/prune/secret",
        OAuthClientCredential("prune-client", None),
        expected_revision=None,
    )

    pruned, _plaintext = _decrypt_document(vault.path)
    assert tuple(pruned["consumed_transactions"]) == tuple(sorted(states[-256:]))
    assert vault.is_transaction_consumed(states[0]) is False
    assert vault.is_transaction_consumed(states[-1]) is True
    replay = replace(
        transaction,
        state_sha256=states[-1],
        issued_at=clock.value,
        expires_at=clock.value + 300.0,
    )
    vault.begin_transaction(replay)
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        vault.consume_transaction(replay.state_sha256, reason="denied")


def test_pruning_allows_more_than_global_limit_callback_cycles_and_keeps_recent_replay(
    tmp_path: Path,
) -> None:
    clock = _MutableClock(800_000.0)
    document: dict[str, object] = {name: {} for name in _DOCUMENT_MAPS}
    consumed = document["consumed_transactions"]
    assert isinstance(consumed, dict)
    states = tuple(sha256(f"callback-{index}".encode()).hexdigest() for index in range(520))
    for index, state_sha256 in enumerate(states):
        consumed[state_sha256] = {
            "consumed_at": clock.value if index == len(states) - 1 else float(index),
            "reason": "denied",
        }
    (tmp_path / "oauth-vault.json").write_bytes(_encrypt_document(document))
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=clock)

    vault.store_credential(
        "oauth-client/prune/secret",
        OAuthClientCredential("prune-client", None),
        expected_revision=None,
    )

    assert vault.is_transaction_consumed(states[0]) is False
    assert vault.is_transaction_consumed(states[-1]) is True
    pruned, _plaintext = _decrypt_document(vault.path)
    assert set(pruned["consumed_transactions"]) == {states[-1]}


def test_pruning_collapses_more_than_global_limit_superseded_retries(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
) -> None:
    clock = _MutableClock(150.0)
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=clock)
    vault.begin_transaction(transaction)
    document, _plaintext = _decrypt_document(vault.path)
    transactions = document["transactions"]
    template = transactions.pop(transaction.state_sha256)
    states = tuple(sha256(f"retry-{index}".encode()).hexdigest() for index in range(520))
    for index, state_sha256 in enumerate(states):
        record = json.loads(json.dumps(template))
        record["revision"] = 1
        record["superseded"] = True
        record["value"]["attempt_revision"] = index
        record["value"]["state_sha256"] = state_sha256
        transactions[state_sha256] = record
    vault.path.write_bytes(_encrypt_document(document))

    vault.store_credential(
        "oauth-client/prune/secret",
        OAuthClientCredential("prune-client", None),
        expected_revision=None,
    )

    pruned, _plaintext = _decrypt_document(vault.path)
    assert set(pruned["transactions"]) == {states[-1]}
    with pytest.raises(ValueError, match="^oauth_transaction_superseded$"):
        vault.load_transaction(states[-1])


def test_pruning_reclaims_expired_transaction_on_next_vault_mutation(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
) -> None:
    clock = _MutableClock(150.0)
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=clock)
    vault.begin_transaction(transaction)
    clock.value = transaction.expires_at

    vault.store_credential(
        "oauth-client/prune/secret",
        OAuthClientCredential("prune-client", None),
        expected_revision=None,
    )

    with pytest.raises(ValueError, match="^oauth_transaction_missing$"):
        vault.load_transaction(transaction.state_sha256)


def test_pruning_is_bounded_and_preserves_live_prepared_and_unclaimed_recovery(
    tmp_path: Path,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    clock = _MutableClock(100.0)
    vault = OAuthCredentialVault(tmp_path, key_provider=lambda: _KEY, clock=clock)
    _commit(vault, transaction, prepared_token, grant)
    pending = replace(
        transaction,
        plugin_id="plugin.beta",
        app_id="app.beta",
        partition_key="b" * 64,
        state_sha256="c" * 64,
        issued_at=100.0,
        expires_at=1_000_000.0,
        attempt_revision=1,
    )
    pending = replace(pending, attempt_key=_attempt_key(pending))
    vault.begin_transaction(pending)
    vault.prepare_token(pending.state_sha256, prepared_token)
    code_sha256 = "d" * 64
    assert vault.claim_sdk_authorization_code("sdk-connection", code_sha256) is True
    assert vault.claim_sdk_authorization_code("sdk-connection", code_sha256) is False

    clock.value = 800_000.0
    vault.store_credential(
        "oauth-client/prune/secret",
        OAuthClientCredential("prune-client", None),
        expected_revision=None,
    )

    assert vault.recover_prepared_transactions() == (
        replace(pending, prepared_token=prepared_token),
    )
    assert vault.claim_committed_grants() == (grant,)
    assert vault.claim_sdk_authorization_code("sdk-connection", code_sha256) is True


def test_denied_transaction_is_consumed_without_a_code_or_token(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
) -> None:
    vault.begin_transaction(transaction)

    vault.consume_transaction(transaction.state_sha256, reason="denied")

    assert vault.is_transaction_consumed(transaction.state_sha256) is True
    with pytest.raises(ValueError, match="oauth_transaction_missing"):
        vault.load_transaction(transaction.state_sha256)
    _document, plaintext = _decrypt_document(vault.path)
    assert b"authorization-code" not in plaintext


def test_transport_grant_status_blocks_reauthorization_and_pending_revocation(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)
    binding = {
        "plugin_id": grant.plugin_id,
        "app_id": grant.app_id,
        "profile_id": grant.profile_id,
        "profile_revision": grant.profile_revision,
        "partition_key": grant.token_partition_key,
        "connection_revision": grant.connection_revision,
        "capability_revision": grant.capability_revision,
        "authority_revision": grant.authority_revision,
        "capability_binding_revision": grant.capability_binding_revision,
    }
    assert vault.validate_transport_grant(**binding) == grant

    vault.mark_partition_reauthorization_required(transaction.partition_key)
    assert vault.load_token_partition(transaction.partition_key).reauthorization_required is True
    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        vault.validate_transport_grant(**binding)


def test_last_dependent_revocation_is_two_phase_and_compare_and_swap_protected(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    _commit(vault, transaction, prepared_token, grant)

    pending, remaining = vault.prepare_revocation(
        transaction.partition_key,
        plugin_id=transaction.plugin_id,
    )

    assert remaining == ()
    assert pending.revocation_pending_plugin_id == transaction.plugin_id
    assert pending.token == prepared_token
    assert vault.load_token_partition(transaction.partition_key) == pending
    with pytest.raises(ValueError, match="^oauth_revocation_pending$"):
        vault.validate_transport_grant(
            plugin_id=grant.plugin_id,
            app_id=grant.app_id,
            profile_id=grant.profile_id,
            profile_revision=grant.profile_revision,
            partition_key=grant.token_partition_key,
            connection_revision=grant.connection_revision,
            capability_revision=grant.capability_revision,
            authority_revision=grant.authority_revision,
            capability_binding_revision=grant.capability_binding_revision,
        )
    with pytest.raises(ValueError, match="^oauth_token_revision_conflict$"):
        vault.finalize_revocation(
            transaction.partition_key,
            plugin_id=transaction.plugin_id,
            expected_revision=pending.revision - 1,
        )
    assert vault.load_token_partition(transaction.partition_key) == pending

    vault.finalize_revocation(
        transaction.partition_key,
        plugin_id=transaction.plugin_id,
        expected_revision=pending.revision,
    )
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        vault.load_token_partition(transaction.partition_key)


def test_a_reauthorization_mark_can_be_undone_while_the_credential_still_lives(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    """A mark made in error must be undoable without a full sign-in.

    Marking is durable and was one-way: only committing a fresh grant cleared
    it. That stranded a working connection when the mark was wrong -- a forced
    refresh against a token that simply cannot be refreshed used to make exactly
    that mistake.
    """

    _commit(vault, transaction, prepared_token, grant)
    vault.mark_partition_reauthorization_required(transaction.partition_key)
    marked = vault.load_token_partition(transaction.partition_key)
    assert marked.reauthorization_required is True
    assert [
        receipt.status
        for receipt in vault.committed_grant_receipts()
        if receipt.plugin_id == transaction.plugin_id
    ] == ["reauthorization_required"]

    vault.clear_partition_reauthorization_required(
        transaction.partition_key,
        now=prepared_token.expires_at - 1.0,
    )

    cleared = vault.load_token_partition(transaction.partition_key)
    assert cleared.reauthorization_required is False
    assert cleared.revision > marked.revision
    # The grants the same mark moved come back with it.
    assert [
        receipt.status
        for receipt in vault.committed_grant_receipts()
        if receipt.plugin_id == transaction.plugin_id
    ] == ["active"]


def test_a_reauthorization_mark_cannot_be_undone_once_the_token_has_expired(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    """Clearing asserts the credential is usable. An expired one is not.

    Dropping the flag would claim a working connection where there is none, so
    the honest answer stays `oauth_reauthorization_required` -- that case really
    does need the operator to sign in again.
    """

    _commit(vault, transaction, prepared_token, grant)
    vault.mark_partition_reauthorization_required(transaction.partition_key)

    with pytest.raises(ValueError) as raised:
        vault.clear_partition_reauthorization_required(
            transaction.partition_key,
            now=prepared_token.expires_at + 1.0,
        )

    assert str(raised.value) == "oauth_reauthorization_required"
    assert vault.load_token_partition(transaction.partition_key).reauthorization_required


def test_clearing_an_unmarked_partition_changes_nothing(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    """Idempotent: no revision churn for a partition that was never marked."""

    _commit(vault, transaction, prepared_token, grant)
    before = vault.load_token_partition(transaction.partition_key)

    vault.clear_partition_reauthorization_required(
        transaction.partition_key,
        now=prepared_token.expires_at - 1.0,
    )

    assert vault.load_token_partition(transaction.partition_key) == before


def test_a_rollback_finishes_by_asking_for_a_sign_in_when_the_grant_needs_one(
    vault: OAuthCredentialVault,
    transaction: OAuthPendingTransaction,
    prepared_token: OAuthPreparedToken,
    grant: OAuthCommittedGrant,
) -> None:
    """Rolling back must not need a credential it can never get.

    The restore refused whenever any grant on the partition was not `active`,
    including the one status an operator can actually resolve. That was a dead
    end (2026-09-08, Supabase): the rollback could not complete, so the
    interrupted update could never be discarded, so the lifecycle could not
    compose, so the HUD refused to start at all.

    A grant awaiting reauthorization is not a conflict -- it says the credential
    is unusable and a sign-in fixes it, which is exactly the state a rollback
    should be free to leave behind.
    """

    _commit(vault, transaction, prepared_token, grant)
    # The credential became unusable BEFORE the update began -- the live shape
    # was a forced refresh against a token with no refresh token. So the
    # checkpoint's own snapshot records a grant that is not `active`, and
    # restoring it puts that status straight back.
    vault.mark_partition_reauthorization_required(transaction.partition_key)
    marked = vault.load_token_partition(transaction.partition_key)
    assert marked.reauthorization_required is True
    checkpoint = vault.begin_plugin_update_recovery(
        "plugin-update-recovery-" + "b" * 64,
        plugin_id=transaction.plugin_id,
        account_key_sha256=transaction.account_key_sha256,
        app_ids=(transaction.app_id,),
    )

    restored = vault.restore_plugin_update_recovery(
        checkpoint["checkpoint_id"],
        expected_revision=checkpoint["revision"],
    )

    assert restored == checkpoint
    partition = vault.load_token_partition(transaction.partition_key)
    # Nothing was restored and nothing was fabricated: the partition is left
    # explicitly needing the sign-in, which is the honest answer.
    assert partition.reauthorization_required is True
    assert partition.token == marked.token
    assert partition.revision > marked.revision
    vault.finalize_plugin_update_recovery(
        checkpoint["checkpoint_id"],
        expected_revision=checkpoint["revision"],
    )
