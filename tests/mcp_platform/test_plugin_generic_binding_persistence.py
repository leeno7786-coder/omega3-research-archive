from __future__ import annotations

import base64
from dataclasses import asdict
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_broker import OAuthPreparedAuthorization
from mcp_platform.oauth_vault import (
    OAuthCommittedGrant,
    OAuthCredentialVault,
    OAuthGrantReceiptClaimOutcome,
    OAuthPendingTransaction,
    OAuthPreparedToken,
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
_VAULT_KEY = b"k" * 32
_VAULT_AAD = b"omega-oauth-vault-v1"


class _Permissions:
    def invalidate_plugin(self, _plugin_id: str) -> int:
        return 0


class _Broker:
    """Constructor-complete broker double; migration reads need no provider I/O."""

    def commit(self, *_args: object, **_kwargs: object) -> OAuthCommittedGrant:
        raise AssertionError("unexpected commit")

    def unclaimed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        return ()

    def committed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        return ()

    def grant_receipt_state(self, _expected: OAuthCommittedGrant) -> str:
        return "absent"

    def claim_grant_receipt_exact(
        self,
        _expected: OAuthCommittedGrant,
        _authority_revision: str,
    ) -> OAuthGrantReceiptClaimOutcome:
        return OAuthGrantReceiptClaimOutcome.ABSENT

    def claim_grant_receipt(self, _receipt_id: str, _authority_revision: str) -> None:
        raise AssertionError("unexpected claim")

    def maximum_connection_generation(
        self,
        _plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> int | None:
        del app_id, account_key_sha256
        return None

    async def revoke(self, _partition_key: str, *, plugin_id: str) -> object:
        raise AssertionError(f"unexpected revoke for {plugin_id}")


def _prepared() -> OAuthPreparedAuthorization:
    return OAuthPreparedAuthorization(
        profile_id="profile.generic",
        plugin_id="suite.generic",
        app_id="suite.generic.mcp",
        account_key_sha256=_ACCOUNT,
        state_sha256="d" * 64,
        partition_key=_PARTITION,
        profile_revision=_PROFILE_REVISION,
        authorization_context_revision="e" * 64,
        scopes=("files.read",),
    )


def _activation(
    prepared: OAuthPreparedAuthorization,
) -> tuple[PluginGrantActivation, tuple[str, ...], str]:
    tool_digests = (
        sha256_hex(canonical_bytes({"tool": "files.list"})),
        sha256_hex(canonical_bytes({"tool": "files.read"})),
    )
    legacy_tool_revision = sha256_hex(
        canonical_bytes({"tool_binding_digests": list(tool_digests)})
    )
    activation = PluginGrantActivation(
        plugin_id=prepared.plugin_id,
        app_id=prepared.app_id,
        account_key_sha256=prepared.account_key_sha256,
        profile_id=prepared.profile_id,
        profile_revision=prepared.profile_revision,
        token_partition_key=prepared.partition_key,
        connection_revision="1" * 64,
        capability_revision="2" * 64,
        scopes=prepared.scopes,
        capability_ceiling=("tool:files.list", "tool:files.read"),
        allowed_tool_schemas={
            "files.list": sha256_hex(canonical_bytes({"type": "object"})),
            "files.read": sha256_hex(
                canonical_bytes(
                    {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    }
                )
            ),
        },
        source_constraints={},
        destination_constraints={},
        capability_binding_identities=(
            PluginCapabilityBindingIdentity("tool", legacy_tool_revision),
        ),
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
    )
    return activation, tool_digests, legacy_tool_revision


def _legacy_activation_document(
    activation: PluginGrantActivation,
    tool_digests: tuple[str, ...],
    legacy_tool_revision: str,
) -> dict[str, object]:
    document = activation.to_dict()
    document.pop("capability_binding_identities")
    document.pop("capability_binding_revision")
    document["tool_binding_digests"] = list(tool_digests)
    document["tool_binding_revision"] = legacy_tool_revision
    return document


def _legacy_receipt_document(
    activation: PluginGrantActivation,
    legacy_tool_revision: str,
) -> dict[str, object]:
    receipt = OAuthCommittedGrant(
        receipt_id="3" * 64,
        plugin_id=activation.plugin_id,
        app_id=activation.app_id,
        account_key_sha256=activation.account_key_sha256,
        profile_id=activation.profile_id,
        profile_revision=activation.profile_revision,
        token_partition_key=activation.token_partition_key,
        connection_revision=activation.connection_revision,
        capability_revision=activation.capability_revision,
        authority_revision=activation.authority_revision,
        capability_binding_revision=activation.capability_binding_revision,
        scopes=activation.scopes,
        execution_policy=activation.execution_policy.value,
        connection_generation=1,
    )
    document = asdict(receipt)
    document["scopes"] = list(receipt.scopes)
    document.pop("capability_binding_revision")
    document["tool_binding_revision"] = legacy_tool_revision
    return document


def _legacy_grant_service(
    root: Path,
) -> tuple[PluginGrantService, PluginGrantActivation, str]:
    prepared = _prepared()
    activation, tool_digests, legacy_tool_revision = _activation(prepared)
    authority_store = PluginAuthorityStore(path=root / "authority.json")
    authority_store.publish_connection(activation.authority(), expected_revision="")
    service = PluginGrantService(
        broker=_Broker(),
        authority_store=authority_store,
        permission_store=_Permissions(),
        journal_path=root / "grants.json",
    )
    identity = service._key(
        activation.plugin_id,
        activation.app_id,
        activation.account_key_sha256,
    )
    service.journal_path.write_bytes(
        canonical_bytes(
            {
                "schema_version": 5,
                "staged": {},
                "active": {
                    identity: {
                        "generation": 1,
                        "receipt": _legacy_receipt_document(
                            activation,
                            legacy_tool_revision,
                        ),
                        "activation": _legacy_activation_document(
                            activation,
                            tool_digests,
                            legacy_tool_revision,
                        ),
                        "eligible": True,
                        "revocation_pending": False,
                    }
                },
                "generations": {identity: 1},
                "disconnected": {},
                "retired_legacy": {},
            }
        )
    )
    return service, activation, identity


def test_schema_five_tool_grant_migrates_once_to_one_generic_identity(
    tmp_path: Path,
) -> None:
    service, activation, identity = _legacy_grant_service(tmp_path)

    migrated = service.current(
        activation.plugin_id,
        app_id=activation.app_id,
        account_key_sha256=activation.account_key_sha256,
    )

    assert migrated is not None
    assert migrated.activation.capability_binding_identities == (
        PluginCapabilityBindingIdentity(
            "tool",
            activation.capability_binding_identities[0].binding_sha256,
        ),
    )
    assert (
        migrated.receipt.capability_binding_revision
        == migrated.activation.capability_binding_revision
    )
    payload = json.loads(service.journal_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 6
    assert payload["active"][identity]["activation"][
        "capability_binding_identities"
    ] == [
        {
            "capability_kind": "tool",
            "binding_sha256": activation.capability_binding_identities[0].binding_sha256,
        }
    ]
    assert "tool_binding_digests" not in payload["active"][identity]["activation"]
    assert "tool_binding_revision" not in payload["active"][identity]["receipt"]
    first_migrated_bytes = service.journal_path.read_bytes()

    restarted = PluginGrantService(
        broker=_Broker(),
        authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        permission_store=_Permissions(),
        journal_path=tmp_path / "grants.json",
    )
    assert restarted.current(
        activation.plugin_id,
        app_id=activation.app_id,
        account_key_sha256=activation.account_key_sha256,
    ) == migrated
    assert service.journal_path.read_bytes() == first_migrated_bytes


@pytest.mark.parametrize("tamper", ("tool_aggregate", "receipt_linkage"))
def test_schema_five_tool_grant_rejects_binding_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    service, activation, identity = _legacy_grant_service(tmp_path)
    document = json.loads(service.journal_path.read_text(encoding="utf-8"))
    active = document["active"][identity]
    if tamper == "tool_aggregate":
        active["activation"]["tool_binding_revision"] = "f" * 64
    else:
        active["receipt"]["tool_binding_revision"] = "f" * 64
    service.journal_path.write_bytes(canonical_bytes(document))

    with pytest.raises(ValueError, match="^plugin_grant_store_unavailable$"):
        service.current(
            activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
        )


def _credential_identity() -> str:
    return sha256_hex(canonical_bytes({"legacy_client_id": "client-id"}))


def _partition_key() -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "account_key_sha256": "1" * 64,
                "account_partition": "issuer_account",
                "authorization_issuer": "https://github.com",
                "credential_identity": _credential_identity(),
            }
        )
    )


def _vault_transaction() -> OAuthPendingTransaction:
    return OAuthPendingTransaction(
        profile_id="github.desktop",
        plugin_id="plugin.generic",
        app_id="app.github",
        account_key_sha256="1" * 64,
        partition_key=_partition_key(),
        authorization_issuer="https://github.com",
        resource_origin="https://api.githubcopilot.com/mcp/",
        client_id="client-id",
        state_sha256="4" * 64,
        pkce_verifier="pkce-private-value",
        redirect_uri="http://127.0.0.1:13310/oauth/github.desktop/callback",
        scopes=("read:user", "repo"),
        authorization_context_revision="5" * 64,
        profile_revision="sha256:" + "6" * 64,
        issued_at=100.0,
        expires_at=200.0,
        attempt_revision=0,
        attempt_key=sha256_hex(
            canonical_bytes(
                {
                    "account_key_sha256": "1" * 64,
                    "app_id": "app.github",
                    "plugin_id": "plugin.generic",
                }
            )
        ),
        requested_scopes=("read:user", "repo"),
        account_partition="issuer_account",
        credential_identity=_credential_identity(),
    )


def _vault_grant(
    transaction: OAuthPendingTransaction,
) -> tuple[OAuthCommittedGrant, str]:
    legacy_tool_revision = "9" * 64
    generic_revision = sha256_hex(
        canonical_bytes(
            {
                "capability_binding_identities": [
                    {
                        "capability_kind": "tool",
                        "binding_sha256": legacy_tool_revision,
                    }
                ]
            }
        )
    )
    return (
        OAuthCommittedGrant(
            receipt_id="3" * 64,
            plugin_id=transaction.plugin_id,
            app_id=transaction.app_id,
            account_key_sha256=transaction.account_key_sha256,
            profile_id=transaction.profile_id,
            profile_revision=transaction.profile_revision,
            token_partition_key=transaction.partition_key,
            connection_revision="7" * 64,
            capability_revision="8" * 64,
            authority_revision="a" * 64,
            capability_binding_revision=generic_revision,
            scopes=transaction.scopes,
            execution_policy="direct_turn",
        ),
        legacy_tool_revision,
    )


def _decrypt_vault(path: Path) -> dict[str, object]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    plaintext = AESGCM(_VAULT_KEY).decrypt(
        base64.b64decode(envelope["nonce"], validate=True),
        base64.b64decode(envelope["ciphertext"], validate=True),
        _VAULT_AAD,
    )
    return json.loads(plaintext.decode("utf-8"))


def _encrypt_vault(payload: dict[str, object]) -> bytes:
    plaintext = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    nonce = b"n" * 12
    envelope = {
        "ciphertext": base64.b64encode(
            AESGCM(_VAULT_KEY).encrypt(nonce, plaintext, _VAULT_AAD)
        ).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "version": 1,
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _legacy_schema_four_vault(
    root: Path,
) -> tuple[OAuthCredentialVault, OAuthCommittedGrant, str]:
    transaction = _vault_transaction()
    grant, legacy_tool_revision = _vault_grant(transaction)
    vault = OAuthCredentialVault(
        root,
        key_provider=lambda: _VAULT_KEY,
        clock=lambda: 150.0,
    )
    vault.begin_transaction(transaction)
    vault.prepare_token(
        transaction.state_sha256,
        OAuthPreparedToken(
            access_token="access-private-value",
            refresh_token="refresh-private-value",
            token_type="Bearer",
            scopes=transaction.scopes,
            expires_at=3600.0,
        ),
    )
    vault.commit_authorization(transaction.state_sha256, grant)
    payload = _decrypt_vault(vault.path)
    raw = payload["records"]["committed_grants"][grant.receipt_id]
    raw.pop("capability_binding_revision")
    raw["tool_binding_revision"] = legacy_tool_revision
    payload["schema_version"] = 4
    # A genuine schema-4 document predates the registrations map entirely — a
    # live vault write always includes it (empty), so strip it here rather
    # than leave the fixture unrealistically padded.
    payload["records"].pop("registrations")
    vault.path.write_bytes(_encrypt_vault(payload))
    return vault, grant, legacy_tool_revision


def test_schema_four_vault_tool_grant_migrates_once_to_generic_revision(
    tmp_path: Path,
) -> None:
    vault, grant, legacy_tool_revision = _legacy_schema_four_vault(tmp_path)
    expected = sha256_hex(
        canonical_bytes(
            {
                "capability_binding_identities": [
                    {
                        "capability_kind": "tool",
                        "binding_sha256": legacy_tool_revision,
                    }
                ]
            }
        )
    )

    migrated = OAuthCredentialVault(
        tmp_path,
        key_provider=lambda: _VAULT_KEY,
        clock=lambda: 150.0,
    ).committed_grant_receipts()

    assert len(migrated) == 1
    assert migrated[0].capability_binding_revision == expected
    payload = _decrypt_vault(vault.path)
    assert payload["schema_version"] == 6
    raw = payload["records"]["committed_grants"][grant.receipt_id]
    assert raw["capability_binding_revision"] == expected
    assert "tool_binding_revision" not in raw
    assert payload["records"]["migrations"]["oauth-vault-inner-v4"] == {
        "revision": 0,
        "value": {"migration_status": "completed"},
    }
    first_migrated_bytes = vault.path.read_bytes()

    assert OAuthCredentialVault(
        tmp_path,
        key_provider=lambda: _VAULT_KEY,
        clock=lambda: 150.0,
    ).committed_grant_receipts() == migrated
    assert vault.path.read_bytes() == first_migrated_bytes


@pytest.mark.parametrize("tamper", ("malformed_binding", "mixed_schema_alias"))
def test_schema_four_vault_rejects_tampered_tool_grant(
    tmp_path: Path,
    tamper: str,
) -> None:
    vault, grant, _legacy_tool_revision = _legacy_schema_four_vault(tmp_path)
    payload = _decrypt_vault(vault.path)
    raw = payload["records"]["committed_grants"][grant.receipt_id]
    if tamper == "malformed_binding":
        raw["tool_binding_revision"] = "not-a-digest"
    else:
        raw["capability_binding_revision"] = "f" * 64
    vault.path.write_bytes(_encrypt_vault(payload))

    with pytest.raises(ValueError, match="^oauth_vault_migration_required$"):
        OAuthCredentialVault(
            tmp_path,
            key_provider=lambda: _VAULT_KEY,
            clock=lambda: 150.0,
        ).committed_grant_receipts()
