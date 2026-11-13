from __future__ import annotations

from pathlib import Path

import pytest

from mcp_platform.authorization import _StoredClientInfo, _StoredTokens
from mcp_platform.oauth_vault import OAuthClientCredential, OAuthCredentialVault
from mcp_platform.token_store import OmegaMcpTokenStore


def _tokens() -> _StoredTokens:
    return _StoredTokens(
        access_token="access-value",
        token_type="Bearer",
        expires_at=1234.0,
        expires_in=3600,
        scope="drive.file drive.readonly",
        refresh_token="refresh-value",
        authorization_endpoint="https://accounts.google.com/o/oauth2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
    )


def _client() -> _StoredClientInfo:
    return _StoredClientInfo(
        client_id="client-id",
        issuer="https://accounts.google.com",
        redirect_uri="https://omega2ai.com/oauth/callback",
        token_endpoint_auth_method="none",
    )


def test_token_store_persists_encrypted_tokens_and_client_info(tmp_path: Path) -> None:
    store = OmegaMcpTokenStore(tmp_path / "authorization", key_provider=lambda: b"k" * 32)

    revision = store.store_tokens_sync("token-key", _tokens(), expected_revision=None)
    store.store_client_info_sync("client-key", _client())

    assert revision == 0
    assert store.load_tokens_sync("token-key") == (0, _tokens())
    assert store.load_client_info_sync("client-key") == _client()
    raw = (tmp_path / "authorization" / "oauth-vault.json").read_bytes()
    assert b"access-value" not in raw
    assert b"refresh-value" not in raw


def test_token_store_compare_and_swap_rejects_stale_revision(tmp_path: Path) -> None:
    store = OmegaMcpTokenStore(tmp_path / "authorization", key_provider=lambda: b"k" * 32)
    assert store.store_tokens_sync("token-key", _tokens(), expected_revision=None) == 0

    with pytest.raises(ValueError, match="authorization_revision_conflict"):
        store.store_tokens_sync("token-key", _tokens(), expected_revision=None)

    assert store.store_tokens_sync("token-key", _tokens(), expected_revision=0) == 1
    assert store.delete_tokens_sync("token-key", expected_revision=1) is True
    assert store.load_tokens_sync("token-key") is None


def test_token_store_rejects_wrong_key_and_oversized_documents(tmp_path: Path) -> None:
    store = OmegaMcpTokenStore(tmp_path / "authorization", key_provider=lambda: b"k" * 32)
    store.store_tokens_sync("token-key", _tokens(), expected_revision=None)

    wrong = OmegaMcpTokenStore(tmp_path / "authorization", key_provider=lambda: b"x" * 32)
    with pytest.raises(ValueError, match="authorization_store_unavailable"):
        wrong.load_tokens_sync("token-key")

    oversized = OmegaMcpTokenStore(
        tmp_path / "authorization",
        key_provider=lambda: b"k" * 32,
        max_document_bytes=16,
    )
    with pytest.raises(ValueError, match="authorization_store_limit_exceeded"):
        oversized.load_tokens_sync("token-key")


@pytest.mark.anyio
async def test_token_store_async_protocol_round_trip(tmp_path: Path) -> None:
    store = OmegaMcpTokenStore(tmp_path / "authorization", key_provider=lambda: b"k" * 32)

    revision = await store.store_tokens("token-key", _tokens(), expected_revision=None)

    assert revision == 0
    assert await store.load_tokens("token-key") == (0, _tokens())
    assert await store.delete_tokens("token-key", expected_revision=0) is True


def test_token_store_adapter_and_broker_share_one_vault_document(tmp_path: Path) -> None:
    vault = OAuthCredentialVault(tmp_path / "authorization", key_provider=lambda: b"k" * 32)
    store = OmegaMcpTokenStore(vault=vault)

    store.store_tokens_sync("token-key", _tokens(), expected_revision=None)
    vault.store_credential(
        "oauth-client/google.desktop/secret",
        OAuthClientCredential(client_id="client-id", client_secret="client-private-value"),
        expected_revision=None,
    )

    assert store.load_tokens_sync("token-key") == (0, _tokens())
    assert vault.load_credential("oauth-client/google.desktop/secret") is not None
    assert vault.path == tmp_path / "authorization" / "oauth-vault.json"
    assert not (tmp_path / "authorization" / "tokens.json").exists()
    assert b"client-private-value" not in vault.path.read_bytes()


def test_token_store_runtime_never_reads_or_deletes_legacy_document(tmp_path: Path) -> None:
    root = tmp_path / "authorization"
    root.mkdir()
    legacy_path = root / "tokens.json"
    legacy_bytes = b"legacy-migration-only-record"
    legacy_path.write_bytes(legacy_bytes)
    store = OmegaMcpTokenStore(root, key_provider=lambda: b"k" * 32)

    assert store.load_tokens_sync("token-key") is None
    assert legacy_path.read_bytes() == legacy_bytes


def test_token_store_code_claim_is_single_use(tmp_path: Path) -> None:
    store = OmegaMcpTokenStore(tmp_path / "authorization", key_provider=lambda: b"k" * 32)
    digest = "a" * 64

    assert store.claim_authorization_code_sync("code-key", digest) is True
    assert store.claim_authorization_code_sync("code-key", digest) is False
