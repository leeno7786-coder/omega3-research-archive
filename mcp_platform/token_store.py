"""MCP SDK token-store adapter backed by Omega's single OAuth vault document."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import paths as core_paths
from mcp_platform.authorization import _StoredClientInfo, _StoredTokens
from mcp_platform.oauth_vault import OAuthCredentialVault, _default_key_provider
from mcp_platform.state_store import McpStateStore


_LEGACY_VERSION = 1
_LEGACY_AAD = b"omega-mcp-authorization-store-v1"
_LEGACY_NONCE_BYTES = 12
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_ENTRIES = 512


class LegacyMcpAuthorizationStoreReader:
    """Migration-only reader for Task 7; runtime SDK operations never consult it."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        key_provider: Callable[[], bytes] = _default_key_provider,
    ) -> None:
        self.root = Path(root) if root is not None else core_paths.MCP_AUTHORIZATION_ROOT
        self.path = (
            self.root / "tokens.json"
            if root is not None
            else core_paths.MCP_AUTHORIZATION_STORE_PATH
        )
        self._key_provider = key_provider
        self._state = McpStateStore(
            state_root=self.root,
            document_path=self.path,
            invalid_path_reason="authorization_store_unavailable",
            max_document_bytes=_MAX_DOCUMENT_BYTES,
        )

    def _key_bytes(self) -> bytes:
        try:
            raw = self._key_provider()
        except Exception as exc:
            raise ValueError("authorization_store_unavailable") from exc
        if type(raw) is not bytes or len(raw) != 32:
            raise ValueError("authorization_store_unavailable")
        return raw

    def read_document(self) -> dict[str, Any] | None:
        """Return old records only when explicitly invoked by migration code."""

        with self._state.locked():
            raw = self._state.read_bytes()
            if raw is None:
                return None
            try:
                envelope = json.loads(raw.decode("utf-8"))
                if (
                    type(envelope) is not dict
                    or set(envelope) != {"version", "nonce", "ciphertext"}
                    or envelope.get("version") != _LEGACY_VERSION
                ):
                    raise ValueError
                nonce = base64.b64decode(envelope["nonce"], validate=True)
                ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
                if len(nonce) != _LEGACY_NONCE_BYTES or not ciphertext:
                    raise ValueError
                plaintext = AESGCM(self._key_bytes()).decrypt(
                    nonce,
                    ciphertext,
                    _LEGACY_AAD,
                )
                document = json.loads(plaintext.decode("utf-8"))
                if (
                    type(document) is not dict
                    or set(document) != {"version", "tokens", "clients", "claims"}
                    or document.get("version") != _LEGACY_VERSION
                    or any(
                        type(document[name]) is not dict
                        for name in ("tokens", "clients", "claims")
                    )
                ):
                    raise ValueError
                entries = len(document["tokens"]) + len(document["clients"])
                for claims in document["claims"].values():
                    if type(claims) is not dict:
                        raise ValueError
                    entries += len(claims)
                if entries > _MAX_ENTRIES:
                    raise ValueError
                return document
            except Exception as exc:
                raise ValueError("authorization_store_unavailable") from exc


class OmegaMcpTokenStore:
    """Keep the existing MCP SDK protocol on the shared typed OAuth vault."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        vault: OAuthCredentialVault | None = None,
        key_provider: Callable[[], bytes] = _default_key_provider,
        max_document_bytes: int = _MAX_DOCUMENT_BYTES,
    ) -> None:
        self._vault = vault or OAuthCredentialVault(
            root,
            key_provider=key_provider,
            max_document_bytes=max_document_bytes,
        )
        self.root = self._vault.root
        self.path = self._vault.path

    @staticmethod
    def _translate(error: ValueError) -> ValueError:
        reason = str(error)
        if reason == "oauth_vault_revision_conflict":
            return ValueError("authorization_revision_conflict")
        if reason in {
            "oauth_vault_entry_limit_exceeded",
            "oauth_vault_limit_exceeded",
        }:
            return ValueError("authorization_store_limit_exceeded")
        if reason.startswith("oauth_"):
            return ValueError("authorization_store_unavailable")
        return error

    def _call(self, method: Callable[..., Any], *args: object, **kwargs: object) -> Any:
        try:
            return method(*args, **kwargs)
        except ValueError as exc:
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc

    def load_tokens_sync(self, storage_key: str) -> tuple[int, _StoredTokens] | None:
        return self._call(self._vault.load_sdk_tokens, storage_key)

    async def load_tokens(self, storage_key: str) -> tuple[int, _StoredTokens] | None:
        return await asyncio.to_thread(self.load_tokens_sync, storage_key)

    def store_tokens_sync(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int:
        return self._call(
            self._vault.store_sdk_tokens,
            storage_key,
            tokens,
            expected_revision=expected_revision,
        )

    async def store_tokens(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int:
        return await asyncio.to_thread(
            self.store_tokens_sync,
            storage_key,
            tokens,
            expected_revision=expected_revision,
        )

    def delete_tokens_sync(self, storage_key: str, *, expected_revision: int) -> bool:
        return self._call(
            self._vault.delete_sdk_tokens,
            storage_key,
            expected_revision=expected_revision,
        )

    async def delete_tokens(self, storage_key: str, *, expected_revision: int) -> bool:
        return await asyncio.to_thread(
            self.delete_tokens_sync,
            storage_key,
            expected_revision=expected_revision,
        )

    def load_client_info_sync(self, storage_key: str) -> _StoredClientInfo | None:
        return self._call(self._vault.load_sdk_client_info, storage_key)

    async def load_client_info(self, storage_key: str) -> _StoredClientInfo | None:
        return await asyncio.to_thread(self.load_client_info_sync, storage_key)

    def store_client_info_sync(self, storage_key: str, client_info: object) -> None:
        self._call(self._vault.store_sdk_client_info, storage_key, client_info)

    async def store_client_info(self, storage_key: str, client_info: object) -> None:
        await asyncio.to_thread(
            self.store_client_info_sync,
            storage_key,
            client_info,
        )

    def claim_authorization_code_sync(self, storage_key: str, code_sha256: str) -> bool:
        return self._call(
            self._vault.claim_sdk_authorization_code,
            storage_key,
            code_sha256,
        )

    async def claim_authorization_code(self, storage_key: str, code_sha256: str) -> bool:
        return await asyncio.to_thread(
            self.claim_authorization_code_sync,
            storage_key,
            code_sha256,
        )


__all__ = ["LegacyMcpAuthorizationStoreReader", "OmegaMcpTokenStore"]
