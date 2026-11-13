"""Portable typed custody for Omega OAuth credentials, transactions, and tokens."""

from __future__ import annotations

import base64
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from types import MappingProxyType

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import paths as core_paths
from mcp_platform.authorization import _StoredClientInfo, _StoredTokens
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_profiles import OAuthAuthorizationFlow, revision_digest
from mcp_platform.state_store import McpStateStore


_VERSION = 1
_INNER_SCHEMA_VERSION = 6
_AAD = b"omega-oauth-vault-v1"
_NONCE_BYTES = 12
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_ENTRIES = 512
_MAX_REPLAY_TOMBSTONES = _MAX_ENTRIES // 2
_REPLAY_DEFENSE_HORIZON_SECONDS = 86_400.0
_LOWER_HEX = frozenset("0123456789abcdef")
_PLUGIN_UPDATE_RECOVERY_PREFIX = "plugin-update-recovery-"
_DOCUMENT_MAPS = frozenset(
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
        "registrations",
    }
)
# The document shape from the oldest pre-wrapper format through inner schema 5 —
# every migration stage that runs *before* `_migrate_v5_document` adds
# "registrations" must gate on this, not on `_DOCUMENT_MAPS`, or a genuine
# historical document (which never had that key) is rejected before it ever
# reaches the code meant to add it. If a future record kind adds another
# top-level map, freeze a new `_DOCUMENT_MAPS_THROUGH_V<n>` the same way rather
# than editing this one.
_DOCUMENT_MAPS_THROUGH_V5 = frozenset(
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
_GENERATION_FLOOR_PREFIX = "connection-generation-floor:"


@dataclass(frozen=True, slots=True, repr=False)
class OAuthClientCredential:
    client_id: str
    client_secret: str | None

    def __repr__(self) -> str:
        return "OAuthClientCredential(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OAuthClientRegistration:
    authorization_issuer: str
    profile_id: str
    client_id: str
    registration_access_token: str | None
    registration_client_uri: str | None
    registered_at: float
    response_sha256: str
    #: Issued only by a server that registers CONFIDENTIAL clients, and only
    #: kept when that server's token endpoint requires client authentication.
    #: Sealed with the rest of the vault; never in ``__repr__``.
    client_secret: str | None = None
    #: RFC 7591 ``client_secret_expires_at`` as an absolute epoch time. None
    #: means the server issued no secret, or one that does not expire.
    client_secret_expires_at: float | None = None
    #: Whether this registration was made while looking for a secret and came
    #: back without one. It stops the upgrade being retried on every sign-in
    #: against a server that demands client authentication and issues nothing.
    secret_upgrade_attempted: bool = False

    def __repr__(self) -> str:
        return "OAuthClientRegistration(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OAuthPendingTransaction:
    profile_id: str
    plugin_id: str
    app_id: str
    account_key_sha256: str
    partition_key: str
    authorization_issuer: str
    resource_origin: str
    client_id: str
    state_sha256: str
    pkce_verifier: str | None
    redirect_uri: str | None
    scopes: tuple[str, ...]
    authorization_context_revision: str
    profile_revision: str
    issued_at: float
    expires_at: float
    attempt_revision: int
    attempt_key: str | None = None
    prepared_token: OAuthPreparedToken | None = None
    requested_scopes: tuple[str, ...] | None = None
    account_partition: str = "issuer_account"
    credential_identity: str | None = None
    attempt_context: Mapping[str, object] = field(default_factory=dict)
    authorization_flow: str = OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE.value
    device_code: str | None = None
    device_interval_seconds: int | None = None
    device_next_poll_at: float | None = None

    def __repr__(self) -> str:
        return (
            "OAuthPendingTransaction("
            f"profile_id={self.profile_id!r}, plugin_id={self.plugin_id!r}, "
            f"state_sha256={self.state_sha256!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OAuthPreparedToken:
    access_token: str
    refresh_token: str | None
    token_type: str
    scopes: tuple[str, ...]
    expires_at: float | None

    def __repr__(self) -> str:
        return "OAuthPreparedToken(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OAuthTokenPartition:
    partition_key: str
    profile_id: str
    profile_revision: str
    authorization_issuer: str
    resource_origin: str
    account_key_sha256: str
    client_id: str
    authorization_context_revision: str
    token: OAuthPreparedToken
    dependent_plugin_ids: tuple[str, ...]
    revision: int
    revocation_pending_plugin_id: str | None = None
    reauthorization_required: bool = False
    account_partition: str = "issuer_account"
    credential_identity: str | None = None
    migration_status: str | None = None

    def __repr__(self) -> str:
        return (
            "OAuthTokenPartition("
            f"partition_key={self.partition_key!r}, profile_id={self.profile_id!r}, "
            f"revision={self.revision})"
        )


@dataclass(frozen=True, slots=True)
class OAuthCommittedGrant:
    receipt_id: str
    plugin_id: str
    app_id: str
    account_key_sha256: str
    profile_id: str
    profile_revision: str
    token_partition_key: str
    connection_revision: str
    capability_revision: str
    authority_revision: str
    capability_binding_revision: str
    scopes: tuple[str, ...]
    execution_policy: str
    status: str = "active"
    claimed_authority_revision: str | None = None
    connection_generation: int = 0


class OAuthGrantReceiptClaimOutcome(StrEnum):
    ABSENT = "absent"
    CLAIMED = "claimed"
    IDEMPOTENTLY_CLAIMED = "idempotently_claimed"


def _default_key_provider() -> bytes:
    """Compatibility entry point for the default OAuth vault key.

    Sealed (passphrase, portable) custody when it exists or when a passphrase
    is configured; this machine's DPAPI custody otherwise.
    """

    from mcp_platform.sealed_secrets import PortableOAuthKeyProvider

    return PortableOAuthKeyProvider(
        core_paths.MCP_AUTHORIZATION_ROOT,
        core_paths.MCP_OAUTH_VAULT_PATH,
    )()


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("oauth_vault_record_invalid") from exc


def _json_loads(raw: bytes) -> object:
    def reject_constant(_value: str) -> object:
        raise ValueError

    return json.loads(raw.decode("utf-8"), parse_constant=reject_constant)


def _text(
    value: object,
    *,
    reason: str,
    maximum: int = 2048,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or len(value) > maximum:
        raise ValueError(reason)
    if not allow_empty and not value:
        raise ValueError(reason)
    if value.strip() != value or any(ord(character) < 32 for character in value):
        raise ValueError(reason)
    return value


def _optional_text(value: object, *, reason: str, maximum: int = 65_536) -> str | None:
    if value is None:
        return None
    return _text(value, reason=reason, maximum=maximum)


def _storage_key(value: object) -> str:
    return _text(value, reason="authorization_storage_key_invalid", maximum=128)


def _credential_ref(value: object) -> str:
    reference = _text(value, reason="oauth_credential_ref_invalid", maximum=256)
    if (
        not reference.startswith("oauth-client/")
        or reference.endswith("/")
        or "//" in reference
        or any(part in ("", ".", "..") for part in reference.split("/"))
    ):
        raise ValueError("oauth_credential_ref_invalid")
    return reference


def _stable_id(value: object, *, reason: str = "oauth_record_invalid") -> str:
    return _text(value, reason=reason, maximum=256)


def _sha256_digest(value: object, *, reason: str = "oauth_digest_invalid") -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        raise ValueError(reason)
    return value


def _revision(value: object, *, reason: str = "oauth_record_invalid") -> str:
    try:
        return revision_digest(value)
    except ValueError as exc:
        raise ValueError(reason) from exc


def _binding_revision(value: object, *, reason: str = "oauth_record_invalid") -> str:
    return _sha256_digest(value, reason=reason)


def _attempt_key_digest(plugin_id: str, app_id: str, account_key_sha256: str) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": _stable_id(plugin_id, reason="oauth_transaction_invalid"),
                "app_id": _stable_id(app_id, reason="oauth_transaction_invalid"),
                "account_key_sha256": _sha256_digest(
                    account_key_sha256,
                    reason="oauth_transaction_invalid",
                ),
            }
        )
    )


def _registration_key(authorization_issuer: str, profile_id: str) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "authorization_issuer": _text(
                    authorization_issuer,
                    reason="oauth_registration_invalid",
                ),
                "profile_id": _stable_id(profile_id, reason="oauth_registration_invalid"),
            }
        )
    )


def _connection_identity_digest(
    plugin_id: str,
    app_id: str,
    account_key_sha256: str,
) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": _stable_id(plugin_id, reason="oauth_grant_invalid"),
                "app_id": _stable_id(app_id, reason="oauth_grant_invalid"),
                "account_key_sha256": _sha256_digest(
                    account_key_sha256,
                    reason="oauth_grant_invalid",
                ),
            }
        )
    )


def _generation_floor_key(
    plugin_id: str,
    app_id: str,
    account_key_sha256: str,
) -> str:
    return _GENERATION_FLOOR_PREFIX + _connection_identity_digest(
        plugin_id,
        app_id,
        account_key_sha256,
    )


def _timestamp(value: object, *, reason: str = "oauth_record_invalid") -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(reason)
    return float(value)


def _scopes(value: object, *, reason: str = "oauth_record_invalid") -> tuple[str, ...]:
    if type(value) not in (tuple, list) or len(value) > 128:
        raise ValueError(reason)
    scopes = tuple(_text(item, reason=reason, maximum=512) for item in value)
    if len(set(scopes)) != len(scopes):
        raise ValueError(reason)
    return scopes


def _mapping(value: object, *, keys: set[str], reason: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(reason)
    return value


def _attempt_context(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError("oauth_transaction_invalid")
    forbidden = {
        "access_token",
        "authorization_code",
        "client_secret",
        "pkce_verifier",
        "refresh_token",
        "state",
    }

    def check(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if type(key) is not str or key.casefold() in forbidden:
                    raise ValueError("oauth_transaction_invalid")
                check(nested)
        elif isinstance(item, list):
            for nested in item:
                check(nested)

    try:
        payload = canonical_bytes(dict(value))
        if len(payload) > 65_536:
            raise ValueError
        document = json.loads(payload.decode("utf-8"))
        if type(document) is not dict:
            raise ValueError
        check(document)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError("oauth_transaction_invalid") from None
    return MappingProxyType(document)


def _credential(value: object) -> OAuthClientCredential:
    if type(value) is not OAuthClientCredential:
        raise ValueError("oauth_credential_record_invalid")
    return OAuthClientCredential(
        client_id=_text(
            value.client_id,
            reason="oauth_credential_record_invalid",
            maximum=2048,
        ),
        client_secret=_optional_text(
            value.client_secret,
            reason="oauth_credential_record_invalid",
        ),
    )


def _encode_credential(value: OAuthClientCredential) -> dict[str, Any]:
    checked = _credential(value)
    return {"client_id": checked.client_id, "client_secret": checked.client_secret}


def _decode_credential(value: object) -> OAuthClientCredential:
    record = _mapping(
        value,
        keys={"client_id", "client_secret"},
        reason="oauth_vault_unavailable",
    )
    try:
        return _credential(
            OAuthClientCredential(
                client_id=record["client_id"],
                client_secret=record["client_secret"],
            )
        )
    except ValueError as exc:
        raise ValueError("oauth_vault_unavailable") from exc


def _bool(value: object, *, reason: str) -> bool:
    if type(value) is not bool:
        raise ValueError(reason)
    return value


def _registration(value: object) -> OAuthClientRegistration:
    if type(value) is not OAuthClientRegistration:
        raise ValueError("oauth_registration_invalid")
    return OAuthClientRegistration(
        authorization_issuer=_text(
            value.authorization_issuer,
            reason="oauth_registration_invalid",
        ),
        profile_id=_stable_id(value.profile_id, reason="oauth_registration_invalid"),
        client_id=_text(
            value.client_id,
            reason="oauth_registration_invalid",
            maximum=2048,
        ),
        registration_access_token=_optional_text(
            value.registration_access_token,
            reason="oauth_registration_invalid",
        ),
        registration_client_uri=_optional_text(
            value.registration_client_uri,
            reason="oauth_registration_invalid",
        ),
        registered_at=_timestamp(value.registered_at, reason="oauth_registration_invalid"),
        response_sha256=_sha256_digest(
            value.response_sha256,
            reason="oauth_registration_invalid",
        ),
        client_secret=_optional_text(
            value.client_secret,
            reason="oauth_registration_invalid",
            maximum=1024,
        ),
        client_secret_expires_at=(
            None
            if value.client_secret_expires_at is None
            else _timestamp(
                value.client_secret_expires_at,
                reason="oauth_registration_invalid",
            )
        ),
        secret_upgrade_attempted=_bool(
            value.secret_upgrade_attempted,
            reason="oauth_registration_invalid",
        ),
    )


def _encode_registration(value: OAuthClientRegistration) -> dict[str, Any]:
    checked = _registration(value)
    return {
        "authorization_issuer": checked.authorization_issuer,
        "profile_id": checked.profile_id,
        "client_id": checked.client_id,
        "registration_access_token": checked.registration_access_token,
        "registration_client_uri": checked.registration_client_uri,
        "registered_at": checked.registered_at,
        "response_sha256": checked.response_sha256,
        "client_secret": checked.client_secret,
        "client_secret_expires_at": checked.client_secret_expires_at,
        "secret_upgrade_attempted": checked.secret_upgrade_attempted,
    }


#: The shape written before a registration could carry a secret. Every record
#: already on disk has exactly these keys, so both are accepted on read and the
#: newer shape is what gets written back.
_REGISTRATION_FIELDS_V1 = {
    "authorization_issuer",
    "profile_id",
    "client_id",
    "registration_access_token",
    "registration_client_uri",
    "registered_at",
    "response_sha256",
}
_REGISTRATION_FIELDS_V2 = _REGISTRATION_FIELDS_V1 | {
    "client_secret",
    "client_secret_expires_at",
}
_REGISTRATION_FIELDS = _REGISTRATION_FIELDS_V2 | {"secret_upgrade_attempted"}


def _decode_registration(value: object) -> OAuthClientRegistration:
    if type(value) is dict and set(value) == _REGISTRATION_FIELDS_V1:
        record: dict[str, Any] = {
            **value,
            "client_secret": None,
            "client_secret_expires_at": None,
            "secret_upgrade_attempted": False,
        }
    elif type(value) is dict and set(value) == _REGISTRATION_FIELDS_V2:
        record = {**value, "secret_upgrade_attempted": False}
    else:
        record = _mapping(value, keys=_REGISTRATION_FIELDS, reason="oauth_vault_unavailable")
    try:
        return _registration(
            OAuthClientRegistration(
                authorization_issuer=record["authorization_issuer"],
                profile_id=record["profile_id"],
                client_id=record["client_id"],
                registration_access_token=record["registration_access_token"],
                registration_client_uri=record["registration_client_uri"],
                registered_at=record["registered_at"],
                response_sha256=record["response_sha256"],
                client_secret=record["client_secret"],
                client_secret_expires_at=record["client_secret_expires_at"],
                secret_upgrade_attempted=record["secret_upgrade_attempted"],
            )
        )
    except ValueError as exc:
        raise ValueError("oauth_vault_unavailable") from exc


def _prepared_token(value: object) -> OAuthPreparedToken:
    if type(value) is not OAuthPreparedToken:
        raise ValueError("oauth_token_record_invalid")
    expires_at = (
        None
        if value.expires_at is None
        else _timestamp(
            value.expires_at,
            reason="oauth_token_record_invalid",
        )
    )
    return OAuthPreparedToken(
        access_token=_text(
            value.access_token,
            reason="oauth_token_record_invalid",
            maximum=65_536,
        ),
        refresh_token=_optional_text(
            value.refresh_token,
            reason="oauth_token_record_invalid",
        ),
        token_type=_text(
            value.token_type,
            reason="oauth_token_record_invalid",
            maximum=128,
        ),
        scopes=_scopes(value.scopes, reason="oauth_token_record_invalid"),
        expires_at=expires_at,
    )


def _encode_prepared_token(value: OAuthPreparedToken) -> dict[str, Any]:
    return asdict(_prepared_token(value))


def _decode_prepared_token(value: object) -> OAuthPreparedToken:
    record = _mapping(
        value,
        keys={"access_token", "refresh_token", "token_type", "scopes", "expires_at"},
        reason="oauth_vault_unavailable",
    )
    try:
        return _prepared_token(
            OAuthPreparedToken(
                access_token=record["access_token"],
                refresh_token=record["refresh_token"],
                token_type=record["token_type"],
                scopes=_scopes(record["scopes"]),
                expires_at=record["expires_at"],
            )
        )
    except ValueError as exc:
        raise ValueError("oauth_vault_unavailable") from exc


def _transaction(value: object) -> OAuthPendingTransaction:
    if type(value) is not OAuthPendingTransaction:
        raise ValueError("oauth_transaction_invalid")
    issued_at = _timestamp(value.issued_at, reason="oauth_transaction_invalid")
    expires_at = _timestamp(value.expires_at, reason="oauth_transaction_invalid")
    if expires_at <= issued_at:
        raise ValueError("oauth_transaction_invalid")
    if type(value.attempt_revision) is not int or value.attempt_revision < 0:
        raise ValueError("oauth_transaction_invalid")
    prepared = None if value.prepared_token is None else _prepared_token(value.prepared_token)
    client_id = _text(
        value.client_id,
        reason="oauth_transaction_invalid",
        maximum=2048,
    )
    requested_scopes = _scopes(
        value.scopes if value.requested_scopes is None else value.requested_scopes,
        reason="oauth_transaction_invalid",
    )
    scopes = _scopes(value.scopes, reason="oauth_transaction_invalid")
    if not set(requested_scopes).issubset(scopes):
        raise ValueError("oauth_transaction_invalid")
    account_partition = _stable_id(
        value.account_partition,
        reason="oauth_transaction_invalid",
    )
    if account_partition != "issuer_account":
        raise ValueError("oauth_transaction_invalid")
    credential_identity = (
        sha256_hex(canonical_bytes({"legacy_client_id": client_id}))
        if value.credential_identity is None
        else _sha256_digest(value.credential_identity, reason="oauth_transaction_invalid")
    )
    attempt_key = (
        _attempt_key_digest(value.plugin_id, value.app_id, value.account_key_sha256)
        if value.attempt_key is None
        else _sha256_digest(value.attempt_key, reason="oauth_transaction_invalid")
    )
    if attempt_key != _attempt_key_digest(
        value.plugin_id,
        value.app_id,
        value.account_key_sha256,
    ):
        raise ValueError("oauth_transaction_invalid")
    try:
        authorization_flow = OAuthAuthorizationFlow(value.authorization_flow)
    except (TypeError, ValueError):
        raise ValueError("oauth_transaction_invalid") from None
    if authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE:
        if (
            value.device_code is not None
            or value.device_interval_seconds is not None
            or value.device_next_poll_at is not None
        ):
            raise ValueError("oauth_transaction_invalid")
        pkce_verifier = _text(
            value.pkce_verifier,
            reason="oauth_transaction_invalid",
            maximum=2048,
        )
        redirect_uri = _text(value.redirect_uri, reason="oauth_transaction_invalid")
        device_code = None
        device_interval_seconds = None
        device_next_poll_at = None
    else:
        if value.pkce_verifier is not None or value.redirect_uri is not None:
            raise ValueError("oauth_transaction_invalid")
        device_code = _text(
            value.device_code,
            reason="oauth_transaction_invalid",
            maximum=65_536,
        )
        if (
            type(value.device_interval_seconds) is not int
            or value.device_interval_seconds <= 0
            or value.device_interval_seconds > 300
        ):
            raise ValueError("oauth_transaction_invalid")
        device_interval_seconds = value.device_interval_seconds
        device_next_poll_at = _timestamp(
            value.device_next_poll_at,
            reason="oauth_transaction_invalid",
        )
        if device_next_poll_at < issued_at or device_next_poll_at >= expires_at:
            raise ValueError("oauth_transaction_invalid")
        pkce_verifier = None
        redirect_uri = None
    return OAuthPendingTransaction(
        profile_id=_stable_id(value.profile_id, reason="oauth_transaction_invalid"),
        plugin_id=_stable_id(value.plugin_id, reason="oauth_transaction_invalid"),
        app_id=_stable_id(value.app_id, reason="oauth_transaction_invalid"),
        account_key_sha256=_sha256_digest(
            value.account_key_sha256,
            reason="oauth_transaction_invalid",
        ),
        partition_key=_sha256_digest(value.partition_key, reason="oauth_transaction_invalid"),
        authorization_issuer=_text(
            value.authorization_issuer,
            reason="oauth_transaction_invalid",
        ),
        resource_origin=_text(value.resource_origin, reason="oauth_transaction_invalid"),
        client_id=client_id,
        state_sha256=_sha256_digest(value.state_sha256, reason="oauth_transaction_invalid"),
        pkce_verifier=pkce_verifier,
        redirect_uri=redirect_uri,
        scopes=scopes,
        authorization_context_revision=_binding_revision(
            value.authorization_context_revision,
            reason="oauth_transaction_invalid",
        ),
        profile_revision=_revision(
            value.profile_revision,
            reason="oauth_transaction_invalid",
        ),
        issued_at=issued_at,
        expires_at=expires_at,
        attempt_revision=value.attempt_revision,
        attempt_key=attempt_key,
        prepared_token=prepared,
        requested_scopes=requested_scopes,
        account_partition=account_partition,
        credential_identity=credential_identity,
        attempt_context=_attempt_context(value.attempt_context),
        authorization_flow=authorization_flow.value,
        device_code=device_code,
        device_interval_seconds=device_interval_seconds,
        device_next_poll_at=device_next_poll_at,
    )


def _encode_transaction(value: OAuthPendingTransaction) -> dict[str, Any]:
    checked = _transaction(value)
    return {
        "account_key_sha256": checked.account_key_sha256,
        "account_partition": checked.account_partition,
        "app_id": checked.app_id,
        "attempt_context": dict(checked.attempt_context),
        "attempt_key": checked.attempt_key,
        "attempt_revision": checked.attempt_revision,
        "authorization_context_revision": checked.authorization_context_revision,
        "authorization_flow": checked.authorization_flow,
        "authorization_issuer": checked.authorization_issuer,
        "client_id": checked.client_id,
        "credential_identity": checked.credential_identity,
        "device_code": checked.device_code,
        "device_interval_seconds": checked.device_interval_seconds,
        "device_next_poll_at": checked.device_next_poll_at,
        "expires_at": checked.expires_at,
        "issued_at": checked.issued_at,
        "partition_key": checked.partition_key,
        "pkce_verifier": checked.pkce_verifier,
        "plugin_id": checked.plugin_id,
        "prepared_token": (
            None
            if checked.prepared_token is None
            else _encode_prepared_token(checked.prepared_token)
        ),
        "profile_id": checked.profile_id,
        "profile_revision": checked.profile_revision,
        "redirect_uri": checked.redirect_uri,
        "requested_scopes": list(checked.requested_scopes or ()),
        "resource_origin": checked.resource_origin,
        "scopes": list(checked.scopes),
        "state_sha256": checked.state_sha256,
    }


_TRANSACTION_FIELDS = {
    "account_key_sha256",
    "account_partition",
    "app_id",
    "attempt_context",
    "attempt_key",
    "attempt_revision",
    "authorization_context_revision",
    "authorization_flow",
    "authorization_issuer",
    "client_id",
    "credential_identity",
    "device_code",
    "device_interval_seconds",
    "device_next_poll_at",
    "expires_at",
    "issued_at",
    "partition_key",
    "pkce_verifier",
    "plugin_id",
    "prepared_token",
    "profile_id",
    "profile_revision",
    "redirect_uri",
    "requested_scopes",
    "resource_origin",
    "scopes",
    "state_sha256",
}


def _decode_transaction(value: object) -> OAuthPendingTransaction:
    record = _mapping(value, keys=_TRANSACTION_FIELDS, reason="oauth_vault_unavailable")
    try:
        prepared = (
            None
            if record["prepared_token"] is None
            else _decode_prepared_token(record["prepared_token"])
        )
        return _transaction(
            OAuthPendingTransaction(
                profile_id=record["profile_id"],
                plugin_id=record["plugin_id"],
                app_id=record["app_id"],
                account_key_sha256=record["account_key_sha256"],
                partition_key=record["partition_key"],
                authorization_issuer=record["authorization_issuer"],
                resource_origin=record["resource_origin"],
                client_id=record["client_id"],
                state_sha256=record["state_sha256"],
                pkce_verifier=record["pkce_verifier"],
                redirect_uri=record["redirect_uri"],
                scopes=_scopes(record["scopes"]),
                authorization_context_revision=record["authorization_context_revision"],
                authorization_flow=record["authorization_flow"],
                profile_revision=record["profile_revision"],
                issued_at=record["issued_at"],
                expires_at=record["expires_at"],
                attempt_revision=record["attempt_revision"],
                attempt_key=record["attempt_key"],
                prepared_token=prepared,
                requested_scopes=_scopes(record["requested_scopes"]),
                account_partition=record["account_partition"],
                credential_identity=record["credential_identity"],
                attempt_context=record["attempt_context"],
                device_code=record["device_code"],
                device_interval_seconds=record["device_interval_seconds"],
                device_next_poll_at=record["device_next_poll_at"],
            )
        )
    except ValueError as exc:
        raise ValueError("oauth_vault_unavailable") from exc


def _grant(value: object) -> OAuthCommittedGrant:
    if type(value) is not OAuthCommittedGrant:
        raise ValueError("oauth_grant_invalid")
    claimed = (
        None
        if value.claimed_authority_revision is None
        else _binding_revision(
            value.claimed_authority_revision,
            reason="oauth_grant_invalid",
        )
    )
    status = _stable_id(value.status, reason="oauth_grant_invalid")
    if type(value.connection_generation) is not int or value.connection_generation < 0:
        raise ValueError("oauth_grant_invalid")
    if status not in {
        "active",
        "reauthorization_required",
        "revocation_pending",
        "revoked",
    }:
        raise ValueError("oauth_grant_invalid")
    return OAuthCommittedGrant(
        receipt_id=_sha256_digest(value.receipt_id, reason="oauth_grant_invalid"),
        plugin_id=_stable_id(value.plugin_id, reason="oauth_grant_invalid"),
        app_id=_stable_id(value.app_id, reason="oauth_grant_invalid"),
        account_key_sha256=_sha256_digest(
            value.account_key_sha256,
            reason="oauth_grant_invalid",
        ),
        profile_id=_stable_id(value.profile_id, reason="oauth_grant_invalid"),
        profile_revision=_revision(value.profile_revision, reason="oauth_grant_invalid"),
        token_partition_key=_sha256_digest(
            value.token_partition_key,
            reason="oauth_grant_invalid",
        ),
        connection_revision=_binding_revision(
            value.connection_revision,
            reason="oauth_grant_invalid",
        ),
        capability_revision=_binding_revision(
            value.capability_revision,
            reason="oauth_grant_invalid",
        ),
        authority_revision=_binding_revision(
            value.authority_revision,
            reason="oauth_grant_invalid",
        ),
        capability_binding_revision=_binding_revision(
            value.capability_binding_revision,
            reason="oauth_grant_invalid",
        ),
        scopes=_scopes(value.scopes, reason="oauth_grant_invalid"),
        execution_policy=_stable_id(value.execution_policy, reason="oauth_grant_invalid"),
        status=status,
        claimed_authority_revision=claimed,
        connection_generation=value.connection_generation,
    )


def _encode_grant(value: OAuthCommittedGrant) -> dict[str, Any]:
    checked = _grant(value)
    payload = asdict(checked)
    payload["scopes"] = list(checked.scopes)
    return payload


_GRANT_FIELDS = {
    "receipt_id",
    "plugin_id",
    "app_id",
    "account_key_sha256",
    "profile_id",
    "profile_revision",
    "token_partition_key",
    "connection_revision",
    "capability_revision",
    "authority_revision",
    "capability_binding_revision",
    "scopes",
    "execution_policy",
    "status",
    "claimed_authority_revision",
    "connection_generation",
}
_SCHEMA_FOUR_GRANT_FIELDS = (
    _GRANT_FIELDS - {"capability_binding_revision"}
) | {"tool_binding_revision"}


def _decode_grant(value: object) -> OAuthCommittedGrant:
    record = _mapping(value, keys=_GRANT_FIELDS, reason="oauth_vault_unavailable")
    try:
        return _grant(
            OAuthCommittedGrant(
                receipt_id=record["receipt_id"],
                plugin_id=record["plugin_id"],
                app_id=record["app_id"],
                account_key_sha256=record["account_key_sha256"],
                profile_id=record["profile_id"],
                profile_revision=record["profile_revision"],
                token_partition_key=record["token_partition_key"],
                connection_revision=record["connection_revision"],
                capability_revision=record["capability_revision"],
                authority_revision=record["authority_revision"],
                capability_binding_revision=record["capability_binding_revision"],
                scopes=_scopes(record["scopes"]),
                execution_policy=record["execution_policy"],
                status=record["status"],
                claimed_authority_revision=record["claimed_authority_revision"],
                connection_generation=record["connection_generation"],
            )
        )
    except ValueError as exc:
        raise ValueError("oauth_vault_unavailable") from exc


def _partition(value: object) -> OAuthTokenPartition:
    if type(value) is not OAuthTokenPartition:
        raise ValueError("oauth_token_record_invalid")
    if type(value.revision) is not int or value.revision < 0:
        raise ValueError("oauth_token_record_invalid")
    dependents = tuple(
        _stable_id(item, reason="oauth_token_record_invalid") for item in value.dependent_plugin_ids
    )
    if not dependents or tuple(sorted(set(dependents))) != dependents:
        raise ValueError("oauth_token_record_invalid")
    pending = (
        None
        if value.revocation_pending_plugin_id is None
        else _stable_id(
            value.revocation_pending_plugin_id,
            reason="oauth_token_record_invalid",
        )
    )
    if pending is not None and (dependents != (pending,)):
        raise ValueError("oauth_token_record_invalid")
    if type(value.reauthorization_required) is not bool:
        raise ValueError("oauth_token_record_invalid")
    if pending is not None and value.reauthorization_required:
        raise ValueError("oauth_token_record_invalid")
    client_id = _text(
        value.client_id,
        reason="oauth_token_record_invalid",
        maximum=2048,
    )
    account_partition = _stable_id(
        value.account_partition,
        reason="oauth_token_record_invalid",
    )
    if account_partition != "issuer_account":
        raise ValueError("oauth_token_record_invalid")
    credential_identity = (
        sha256_hex(canonical_bytes({"legacy_client_id": client_id}))
        if value.credential_identity is None
        else _sha256_digest(
            value.credential_identity,
            reason="oauth_token_record_invalid",
        )
    )
    migration_status = (
        None
        if value.migration_status is None
        else _stable_id(value.migration_status, reason="oauth_token_record_invalid")
    )
    if migration_status not in {None, "legacy_v1"}:
        raise ValueError("oauth_token_record_invalid")
    return OAuthTokenPartition(
        partition_key=_sha256_digest(
            value.partition_key,
            reason="oauth_token_record_invalid",
        ),
        profile_id=_stable_id(value.profile_id, reason="oauth_token_record_invalid"),
        profile_revision=_revision(
            value.profile_revision,
            reason="oauth_token_record_invalid",
        ),
        authorization_issuer=_text(
            value.authorization_issuer,
            reason="oauth_token_record_invalid",
        ),
        resource_origin=_text(value.resource_origin, reason="oauth_token_record_invalid"),
        account_key_sha256=_sha256_digest(
            value.account_key_sha256,
            reason="oauth_token_record_invalid",
        ),
        client_id=client_id,
        authorization_context_revision=_binding_revision(
            value.authorization_context_revision,
            reason="oauth_token_record_invalid",
        ),
        token=_prepared_token(value.token),
        dependent_plugin_ids=dependents,
        revision=value.revision,
        revocation_pending_plugin_id=pending,
        reauthorization_required=value.reauthorization_required,
        account_partition=account_partition,
        credential_identity=credential_identity,
        migration_status=migration_status,
    )


def _encode_partition(value: OAuthTokenPartition) -> dict[str, Any]:
    checked = _partition(value)
    return {
        "account_key_sha256": checked.account_key_sha256,
        "account_partition": checked.account_partition,
        "authorization_issuer": checked.authorization_issuer,
        "authorization_context_revision": checked.authorization_context_revision,
        "client_id": checked.client_id,
        "credential_identity": checked.credential_identity,
        "dependent_plugin_ids": list(checked.dependent_plugin_ids),
        "partition_key": checked.partition_key,
        "profile_id": checked.profile_id,
        "profile_revision": checked.profile_revision,
        "resource_origin": checked.resource_origin,
        "revision": checked.revision,
        "migration_status": checked.migration_status,
        "revocation_pending_plugin_id": checked.revocation_pending_plugin_id,
        "reauthorization_required": checked.reauthorization_required,
        "token": _encode_prepared_token(checked.token),
    }


_PARTITION_FIELDS = {
    "account_key_sha256",
    "account_partition",
    "authorization_issuer",
    "authorization_context_revision",
    "client_id",
    "credential_identity",
    "dependent_plugin_ids",
    "partition_key",
    "profile_id",
    "profile_revision",
    "resource_origin",
    "revision",
    "migration_status",
    "revocation_pending_plugin_id",
    "reauthorization_required",
    "token",
}


def _decode_partition(value: object) -> OAuthTokenPartition:
    record = _mapping(value, keys=_PARTITION_FIELDS, reason="oauth_vault_unavailable")
    try:
        dependents = record["dependent_plugin_ids"]
        if type(dependents) is not list:
            raise ValueError
        return _partition(
            OAuthTokenPartition(
                partition_key=record["partition_key"],
                profile_id=record["profile_id"],
                profile_revision=record["profile_revision"],
                authorization_issuer=record["authorization_issuer"],
                resource_origin=record["resource_origin"],
                account_key_sha256=record["account_key_sha256"],
                client_id=record["client_id"],
                authorization_context_revision=record["authorization_context_revision"],
                token=_decode_prepared_token(record["token"]),
                dependent_plugin_ids=tuple(dependents),
                revision=record["revision"],
                revocation_pending_plugin_id=record["revocation_pending_plugin_id"],
                reauthorization_required=record["reauthorization_required"],
                account_partition=record["account_partition"],
                credential_identity=record["credential_identity"],
                migration_status=record["migration_status"],
            )
        )
    except ValueError as exc:
        raise ValueError("oauth_vault_unavailable") from exc


def _legacy_partition_identity_digest(partition: OAuthTokenPartition) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "account_key_sha256": partition.account_key_sha256,
                "authorization_context_revision": (partition.authorization_context_revision),
                "authorization_issuer": partition.authorization_issuer,
                "client_id": partition.client_id,
                "profile_id": partition.profile_id,
                "profile_revision": partition.profile_revision,
                "resource_origin": partition.resource_origin,
                "scopes": list(partition.token.scopes),
            }
        )
    )


def _partition_identity_digest(partition: OAuthTokenPartition) -> str:
    if partition.migration_status == "legacy_v1":
        return _legacy_partition_identity_digest(partition)
    return sha256_hex(
        canonical_bytes(
            {
                "account_key_sha256": partition.account_key_sha256,
                "account_partition": partition.account_partition,
                "authorization_issuer": partition.authorization_issuer,
                "credential_identity": partition.credential_identity,
            }
        )
    )


def _stored_tokens(value: object) -> dict[str, Any]:
    if type(value) is not _StoredTokens:
        raise ValueError("authorization_token_record_invalid")
    if type(value.expires_in) not in (int, type(None)) or (
        type(value.expires_in) is int and value.expires_in < 0
    ):
        raise ValueError("authorization_token_record_invalid")
    expires_at = (
        None
        if value.expires_at is None
        else _timestamp(
            value.expires_at,
            reason="authorization_token_record_invalid",
        )
    )
    checked = _StoredTokens(
        access_token=_text(
            value.access_token,
            reason="authorization_token_record_invalid",
            maximum=65_536,
        ),
        token_type=_text(
            value.token_type,
            reason="authorization_token_record_invalid",
            maximum=128,
        ),
        expires_at=expires_at,
        expires_in=value.expires_in,
        scope=_optional_text(
            value.scope,
            reason="authorization_token_record_invalid",
            maximum=16_384,
        ),
        refresh_token=_optional_text(
            value.refresh_token,
            reason="authorization_token_record_invalid",
        ),
        authorization_endpoint=_text(
            value.authorization_endpoint,
            reason="authorization_token_record_invalid",
        ),
        token_endpoint=_text(
            value.token_endpoint,
            reason="authorization_token_record_invalid",
        ),
    )
    return asdict(checked)


def _decode_stored_tokens(value: object) -> _StoredTokens:
    fields = {
        "access_token",
        "token_type",
        "expires_at",
        "expires_in",
        "scope",
        "refresh_token",
        "authorization_endpoint",
        "token_endpoint",
    }
    record = _mapping(value, keys=fields, reason="oauth_vault_unavailable")
    try:
        checked = _stored_tokens(_StoredTokens(**record))
        return _StoredTokens(**checked)
    except (TypeError, ValueError) as exc:
        raise ValueError("oauth_vault_unavailable") from exc


def _stored_client(value: object) -> dict[str, Any]:
    if type(value) is not _StoredClientInfo:
        raise ValueError("authorization_client_record_invalid")
    checked = _StoredClientInfo(
        client_id=_text(
            value.client_id,
            reason="authorization_client_record_invalid",
            maximum=2048,
        ),
        issuer=_text(value.issuer, reason="authorization_client_record_invalid"),
        redirect_uri=_text(
            value.redirect_uri,
            reason="authorization_client_record_invalid",
        ),
        token_endpoint_auth_method=_text(
            value.token_endpoint_auth_method,
            reason="authorization_client_record_invalid",
            maximum=128,
        ),
    )
    return asdict(checked)


def _decode_stored_client(value: object) -> _StoredClientInfo:
    fields = {"client_id", "issuer", "redirect_uri", "token_endpoint_auth_method"}
    record = _mapping(value, keys=fields, reason="oauth_vault_unavailable")
    try:
        checked = _stored_client(_StoredClientInfo(**record))
        return _StoredClientInfo(**checked)
    except (TypeError, ValueError) as exc:
        raise ValueError("oauth_vault_unavailable") from exc


class OAuthCredentialVault:
    """One versioned AES-256-GCM document for every sensitive OAuth namespace."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        key_provider: Callable[[], bytes] | None = None,
        clock: Callable[[], float] = time.time,
        max_document_bytes: int = _MAX_DOCUMENT_BYTES,
    ) -> None:
        if (
            type(max_document_bytes) is not int
            or max_document_bytes <= 0
            or max_document_bytes > _MAX_DOCUMENT_BYTES
        ):
            raise ValueError("oauth_vault_limit_invalid")
        if (key_provider is not None and not callable(key_provider)) or not callable(clock):
            raise ValueError("oauth_vault_configuration_invalid")
        self.root = Path(root) if root is not None else core_paths.MCP_AUTHORIZATION_ROOT
        self.path = (
            self.root / "oauth-vault.json" if root is not None else core_paths.MCP_OAUTH_VAULT_PATH
        )
        if key_provider is None:
            from mcp_platform.sealed_secrets import PortableOAuthKeyProvider

            key_provider = PortableOAuthKeyProvider(self.root, self.path)
        self._key_provider = key_provider
        self._clock = clock
        self._max_document_bytes = max_document_bytes
        self._state = McpStateStore(
            state_root=self.root,
            document_path=self.path,
            invalid_path_reason="oauth_vault_unavailable",
            max_document_bytes=max_document_bytes,
        )

    @staticmethod
    def _empty() -> dict[str, dict[str, Any]]:
        return {name: {} for name in sorted(_DOCUMENT_MAPS)}

    @classmethod
    def _migrate_v1_document(cls, value: object) -> dict[str, dict[str, Any]]:
        v3_transaction_fields = _TRANSACTION_FIELDS - {
            "authorization_flow",
            "device_code",
            "device_interval_seconds",
            "device_next_poll_at",
        }
        legacy_transaction_fields = v3_transaction_fields - {
            "account_partition",
            "attempt_context",
            "attempt_key",
            "credential_identity",
            "requested_scopes",
        }
        legacy_partition_fields = _PARTITION_FIELDS - {
            "account_partition",
            "credential_identity",
            "migration_status",
        }
        legacy_grant_fields = _SCHEMA_FOUR_GRANT_FIELDS - {"connection_generation"}
        try:
            if type(value) is not dict or set(value) != _DOCUMENT_MAPS_THROUGH_V5:
                raise ValueError
            if any(type(value[name]) is not dict for name in _DOCUMENT_MAPS_THROUGH_V5):
                raise ValueError
            document = _json_loads(_json_bytes(value))
            if type(document) is not dict:
                raise ValueError
            for state_sha256, raw in document["transactions"].items():
                _sha256_digest(state_sha256, reason="oauth_vault_migration_required")
                if (
                    type(raw) is not dict
                    or set(raw) != {"revision", "superseded", "value"}
                    or type(raw["revision"]) is not int
                    or raw["revision"] < 0
                    or type(raw["superseded"]) is not bool
                    or type(raw["value"]) is not dict
                ):
                    raise ValueError
                transaction = raw["value"]
                fields = set(transaction)
                if fields == v3_transaction_fields:
                    continue
                lane = _attempt_key_digest(
                    transaction["plugin_id"],
                    transaction["app_id"],
                    transaction["account_key_sha256"],
                )
                if fields == v3_transaction_fields - {"attempt_key"}:
                    transaction["attempt_key"] = lane
                    continue
                if fields != legacy_transaction_fields:
                    raise ValueError
                client_id = _text(
                    transaction["client_id"],
                    reason="oauth_vault_migration_required",
                    maximum=2048,
                )
                transaction.update(
                    {
                        "account_partition": "issuer_account",
                        "attempt_context": {
                            "omega_authorization_context_revision": transaction[
                                "authorization_context_revision"
                            ],
                            "omega_migration_status": "reauthorization_required",
                        },
                        "attempt_key": lane,
                        "credential_identity": sha256_hex(
                            canonical_bytes({"legacy_client_id": client_id})
                        ),
                        "requested_scopes": list(transaction["scopes"]),
                    }
                )
            migrated_partitions: list[str] = []
            legacy_pending: dict[str, str | None] = {}
            rebuilt_accounts: dict[str, object] = {}
            for partition_key, raw in document["tokens"].items():
                if type(partition_key) is str and partition_key.startswith("sdk:"):
                    continue
                _sha256_digest(partition_key, reason="oauth_vault_migration_required")
                if (
                    type(raw) is not dict
                    or set(raw) != {"kind", "value"}
                    or raw["kind"] != "oauth"
                    or type(raw["value"]) is not dict
                ):
                    raise ValueError
                partition = raw["value"]
                fields = set(partition)
                if fields == _PARTITION_FIELDS:
                    pass
                elif fields == _PARTITION_FIELDS - {"migration_status"}:
                    partition["migration_status"] = None
                elif fields == legacy_partition_fields:
                    client_id = _text(
                        partition["client_id"],
                        reason="oauth_vault_migration_required",
                        maximum=2048,
                    )
                    partition.update(
                        {
                            "account_partition": "issuer_account",
                            "credential_identity": sha256_hex(
                                canonical_bytes({"legacy_client_id": client_id})
                            ),
                            "migration_status": "legacy_v1",
                            "reauthorization_required": (
                                partition["revocation_pending_plugin_id"] is None
                            ),
                        }
                    )
                    migrated_partitions.append(partition_key)
                else:
                    raise ValueError
                if partition["migration_status"] == "legacy_v1":
                    partition["reauthorization_required"] = (
                        partition["revocation_pending_plugin_id"] is None
                    )
                    if partition_key not in migrated_partitions:
                        migrated_partitions.append(partition_key)
                    legacy_pending[partition_key] = partition["revocation_pending_plugin_id"]
                rebuilt_accounts[partition_key] = {
                    "account_key_sha256": partition["account_key_sha256"],
                    "account_partition": partition["account_partition"],
                    "authorization_issuer": partition["authorization_issuer"],
                    "credential_identity": partition["credential_identity"],
                }
            document["account_partitions"] = rebuilt_accounts
            for receipt_id, grant in document["committed_grants"].items():
                _sha256_digest(receipt_id, reason="oauth_vault_migration_required")
                if type(grant) is not dict:
                    raise ValueError
                fields = set(grant)
                if fields == _SCHEMA_FOUR_GRANT_FIELDS:
                    continue
                if fields != legacy_grant_fields:
                    raise ValueError
                grant["connection_generation"] = 0
            for grant in document["committed_grants"].values():
                partition_key = grant["token_partition_key"]
                if partition_key not in legacy_pending:
                    continue
                grant["status"] = (
                    "revocation_pending"
                    if legacy_pending[partition_key] == grant["plugin_id"]
                    else "reauthorization_required"
                )
            migration_key = "oauth-vault-inner-v1"
            if migration_key in document["migrations"]:
                raise ValueError
            document["migrations"][migration_key] = {
                "revision": 0,
                "value": {
                    "migration_status": "completed",
                    "legacy_partition_keys": sorted(migrated_partitions),
                },
            }
            return cls._migrate_v3_document(document)
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise ValueError("oauth_vault_migration_required") from None

    @classmethod
    def _migrate_v2_document(cls, value: object) -> dict[str, dict[str, Any]]:
        try:
            document = _json_loads(_json_bytes(value))
            legacy_pending: dict[str, str | None] = {}
            for partition_key, raw in document["tokens"].items():
                if type(raw) is not dict or raw.get("kind") != "oauth":
                    continue
                partition = raw.get("value")
                if type(partition) is not dict or partition.get("migration_status") != "legacy_v1":
                    continue
                partition["reauthorization_required"] = (
                    partition["revocation_pending_plugin_id"] is None
                )
                legacy_pending[partition_key] = partition["revocation_pending_plugin_id"]
            for grant in document["committed_grants"].values():
                partition_key = grant["token_partition_key"]
                if partition_key in legacy_pending:
                    grant["status"] = (
                        "revocation_pending"
                        if legacy_pending[partition_key] == grant["plugin_id"]
                        else "reauthorization_required"
                    )
            document = cls._migrate_v3_document(document)
            migration_key = "oauth-vault-inner-v2"
            if migration_key in document["migrations"]:
                raise ValueError
            document["migrations"][migration_key] = {
                "revision": 0,
                "value": {"migration_status": "completed"},
            }
            return cls._validate_document(document)
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise ValueError("oauth_vault_migration_required") from None

    @classmethod
    def _migrate_v3_document(cls, value: object) -> dict[str, dict[str, Any]]:
        document = _json_loads(_json_bytes(value))
        if type(document) is not dict or set(document) != _DOCUMENT_MAPS_THROUGH_V5:
            raise ValueError("oauth_vault_unavailable")
        additions = {
            "authorization_flow": OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE.value,
            "device_code": None,
            "device_interval_seconds": None,
            "device_next_poll_at": None,
        }
        for raw in document["transactions"].values():
            if (
                type(raw) is not dict
                or set(raw) != {"revision", "superseded", "value"}
                or type(raw["value"]) is not dict
                or any(key in raw["value"] for key in additions)
            ):
                raise ValueError("oauth_vault_unavailable")
            raw["value"].update(additions)
        migration_key = "oauth-vault-inner-v3"
        if migration_key in document["migrations"]:
            raise ValueError("oauth_vault_unavailable")
        document["migrations"][migration_key] = {
            "revision": 0,
            "value": {"migration_status": "completed"},
        }
        return cls._migrate_v4_document(document)

    @classmethod
    def _migrate_v4_document(cls, value: object) -> dict[str, dict[str, Any]]:
        """Bind every legacy tool receipt to the generic capability schema."""

        try:
            document = _json_loads(_json_bytes(value))
            if type(document) is not dict or set(document) != _DOCUMENT_MAPS_THROUGH_V5:
                raise ValueError
            for receipt_id, raw in document["committed_grants"].items():
                _sha256_digest(receipt_id, reason="oauth_vault_unavailable")
                if type(raw) is not dict or set(raw) != _SCHEMA_FOUR_GRANT_FIELDS:
                    raise ValueError
                tool_revision = _binding_revision(
                    raw.pop("tool_binding_revision"),
                    reason="oauth_vault_unavailable",
                )
                raw["capability_binding_revision"] = sha256_hex(
                    canonical_bytes(
                        {
                            "capability_binding_identities": [
                                {
                                    "capability_kind": "tool",
                                    "binding_sha256": tool_revision,
                                }
                            ]
                        }
                    )
                )
            migration_key = "oauth-vault-inner-v4"
            if migration_key in document["migrations"]:
                raise ValueError
            document["migrations"][migration_key] = {
                "revision": 0,
                "value": {"migration_status": "completed"},
            }
            cls._seed_generation_floors(document)
            return cls._migrate_v5_document(document)
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise ValueError("oauth_vault_migration_required") from None

    @classmethod
    def _migrate_v5_document(cls, value: object) -> dict[str, dict[str, Any]]:
        """Add the encrypted vault's dynamic-client-registration record map."""

        try:
            document = _json_loads(_json_bytes(value))
            if type(document) is not dict or set(document) != _DOCUMENT_MAPS_THROUGH_V5:
                raise ValueError
            document["registrations"] = {}
            migration_key = "oauth-vault-inner-v5"
            if migration_key in document["migrations"]:
                raise ValueError
            document["migrations"][migration_key] = {
                "revision": 0,
                "value": {"migration_status": "completed"},
            }
            return cls._validate_document(document)
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise ValueError("oauth_vault_migration_required") from None

    def _key_bytes(self) -> bytes:
        try:
            raw = self._key_provider()
        except Exception as exc:
            raise ValueError("oauth_vault_locked") from exc
        if type(raw) is not bytes or len(raw) != 32:
            raise ValueError("oauth_vault_locked")
        return raw

    def _clock_value(self) -> float:
        try:
            return _timestamp(self._clock(), reason="oauth_vault_unavailable")
        except Exception as exc:
            raise ValueError("oauth_vault_unavailable") from exc

    @staticmethod
    def _entry_count(document: dict[str, dict[str, Any]]) -> int:
        count = sum(len(document[name]) for name in _DOCUMENT_MAPS)
        for claims in document["claims"].values():
            if type(claims) is not dict:
                raise ValueError("oauth_vault_unavailable")
            count += len(claims)
        return count

    @staticmethod
    def _validate_sdk_key(value: object) -> str:
        key = _text(
            value,
            reason="oauth_vault_unavailable",
            maximum=132,
        )
        if not key.startswith("sdk:"):
            raise ValueError("oauth_vault_unavailable")
        _text(
            key.removeprefix("sdk:"),
            reason="oauth_vault_unavailable",
            maximum=128,
        )
        return key

    @staticmethod
    def _validate_record_revision(value: object) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("oauth_vault_unavailable")
        return value

    @staticmethod
    def _validate_credential_records(document: dict[str, dict[str, Any]]) -> None:
        for key, value in document["credentials"].items():
            _credential_ref(key)
            record = _mapping(
                value,
                keys={"revision", "value"},
                reason="oauth_vault_unavailable",
            )
            OAuthCredentialVault._validate_record_revision(record["revision"])
            _decode_credential(record["value"])

    @staticmethod
    def _validate_registration_records(document: dict[str, dict[str, Any]]) -> None:
        for key, value in document["registrations"].items():
            registration_key = _sha256_digest(key, reason="oauth_vault_unavailable")
            registration = _decode_registration(value)
            if (
                _registration_key(
                    registration.authorization_issuer,
                    registration.profile_id,
                )
                != registration_key
            ):
                raise ValueError("oauth_vault_unavailable")

    @classmethod
    def _validate_transaction_records(cls, document: dict[str, dict[str, Any]]) -> None:
        for key, value in document["transactions"].items():
            state_sha256 = _sha256_digest(key, reason="oauth_vault_unavailable")
            _revision_number, _superseded, transaction = cls._decode_transaction_record(value)
            if transaction.state_sha256 != state_sha256:
                raise ValueError("oauth_vault_unavailable")

    @classmethod
    def _validate_token_records(
        cls,
        document: dict[str, dict[str, Any]],
    ) -> dict[str, OAuthTokenPartition]:
        partitions: dict[str, OAuthTokenPartition] = {}
        for key, value in document["tokens"].items():
            if type(key) is str and key.startswith("sdk:"):
                cls._validate_sdk_key(key)
                record = _mapping(
                    value,
                    keys={"kind", "revision", "value"},
                    reason="oauth_vault_unavailable",
                )
                if record["kind"] != "sdk":
                    raise ValueError("oauth_vault_unavailable")
                cls._validate_record_revision(record["revision"])
                _decode_stored_tokens(record["value"])
                continue
            partition_key = _sha256_digest(key, reason="oauth_vault_unavailable")
            partition = cls._decode_token_record(value)
            if (
                partition.partition_key != partition_key
                or _partition_identity_digest(partition) != partition_key
            ):
                raise ValueError("oauth_vault_unavailable")
            partitions[partition_key] = partition
        return partitions

    @classmethod
    def _validate_client_records(cls, document: dict[str, dict[str, Any]]) -> None:
        for key, value in document["clients"].items():
            cls._validate_sdk_key(key)
            record = _mapping(
                value,
                keys={"kind", "value"},
                reason="oauth_vault_unavailable",
            )
            if record["kind"] != "sdk":
                raise ValueError("oauth_vault_unavailable")
            _decode_stored_client(record["value"])

    @classmethod
    def _validate_claim_records(cls, document: dict[str, dict[str, Any]]) -> None:
        for key, value in document["claims"].items():
            cls._validate_sdk_key(key)
            if type(value) is not dict:
                raise ValueError("oauth_vault_unavailable")
            for code_sha256, claimed in value.items():
                _sha256_digest(code_sha256, reason="oauth_vault_unavailable")
                if claimed is not True:
                    _timestamp(claimed, reason="oauth_vault_unavailable")

    @staticmethod
    def _grant_partition_identity_matches(
        grant: OAuthCommittedGrant,
        partition: OAuthTokenPartition,
    ) -> bool:
        return (
            grant.account_key_sha256 == partition.account_key_sha256
            and grant.token_partition_key == partition.partition_key
            and set(grant.scopes).issubset(partition.token.scopes)
        )

    @classmethod
    def _validate_grant_partition_relationships(
        cls,
        partitions: dict[str, OAuthTokenPartition],
        grants: dict[str, OAuthCommittedGrant],
    ) -> None:
        eligible_plugins: dict[str, set[str]] = {key: set() for key in partitions}
        live_owners: set[tuple[str, str]] = set()
        for grant in grants.values():
            if (
                grant.claimed_authority_revision is not None
                and grant.claimed_authority_revision != grant.authority_revision
            ):
                raise ValueError("oauth_vault_unavailable")
            partition = partitions.get(grant.token_partition_key)
            if grant.status == "revoked":
                if partition is not None and not cls._grant_partition_identity_matches(
                    grant,
                    partition,
                ):
                    raise ValueError("oauth_vault_unavailable")
                continue
            if (
                partition is None
                or not cls._grant_partition_identity_matches(grant, partition)
                or grant.plugin_id not in partition.dependent_plugin_ids
            ):
                raise ValueError("oauth_vault_unavailable")
            if partition.reauthorization_required:
                expected_status = "reauthorization_required"
            elif partition.revocation_pending_plugin_id == grant.plugin_id:
                expected_status = "revocation_pending"
            else:
                expected_status = "active"
            if grant.status != expected_status:
                raise ValueError("oauth_vault_unavailable")
            owner = (partition.partition_key, grant.plugin_id)
            if owner in live_owners:
                raise ValueError("oauth_vault_unavailable")
            live_owners.add(owner)
            eligible_plugins[partition.partition_key].add(grant.plugin_id)
        for partition_key, partition in partitions.items():
            if eligible_plugins[partition_key] != set(partition.dependent_plugin_ids):
                raise ValueError("oauth_vault_unavailable")

    @staticmethod
    def _validate_account_partition_record(
        key: object,
        value: object,
    ) -> tuple[str, str, str, str, str]:
        partition_key = _sha256_digest(key, reason="oauth_vault_unavailable")
        record = _mapping(
            value,
            keys={
                "account_key_sha256",
                "account_partition",
                "authorization_issuer",
                "credential_identity",
            },
            reason="oauth_vault_unavailable",
        )
        return (
            partition_key,
            _sha256_digest(
                record["account_key_sha256"],
                reason="oauth_vault_unavailable",
            ),
            _stable_id(record["account_partition"], reason="oauth_vault_unavailable"),
            _text(record["authorization_issuer"], reason="oauth_vault_unavailable"),
            _sha256_digest(record["credential_identity"], reason="oauth_vault_unavailable"),
        )

    @classmethod
    def _validate_account_partition_records(
        cls,
        document: dict[str, dict[str, Any]],
        partitions: dict[str, OAuthTokenPartition],
    ) -> None:
        accounts: dict[str, tuple[str, str, str, str]] = {}
        for key, value in document["account_partitions"].items():
            partition_key, account_key, account_partition, issuer, credential_identity = (
                cls._validate_account_partition_record(key, value)
            )
            accounts[partition_key] = (
                account_key,
                account_partition,
                issuer,
                credential_identity,
            )
        if set(accounts) != set(partitions):
            raise ValueError("oauth_vault_unavailable")
        for partition_key, partition in partitions.items():
            if accounts[partition_key] != (
                partition.account_key_sha256,
                partition.account_partition,
                partition.authorization_issuer,
                partition.credential_identity,
            ):
                raise ValueError("oauth_vault_unavailable")

    @staticmethod
    def _validate_grant_records(
        document: dict[str, dict[str, Any]],
    ) -> dict[str, OAuthCommittedGrant]:
        grants: dict[str, OAuthCommittedGrant] = {}
        for key, value in document["committed_grants"].items():
            receipt_id = _sha256_digest(key, reason="oauth_vault_unavailable")
            grant = _decode_grant(value)
            if grant.receipt_id != receipt_id:
                raise ValueError("oauth_vault_unavailable")
            grants[receipt_id] = grant
        return grants

    @staticmethod
    def _validate_consumed_transaction_records(
        document: dict[str, dict[str, Any]],
    ) -> None:
        for key, value in document["consumed_transactions"].items():
            _sha256_digest(key, reason="oauth_vault_unavailable")
            record = _mapping(
                value,
                keys={"consumed_at", "reason"},
                reason="oauth_vault_unavailable",
            )
            _timestamp(record["consumed_at"], reason="oauth_vault_unavailable")
            _stable_id(record["reason"], reason="oauth_vault_unavailable")

    @staticmethod
    def _decode_generation_floor_record(
        key: str,
        value: object,
    ) -> tuple[str, str, str, int]:
        record = _mapping(
            value,
            keys={"revision", "value"},
            reason="oauth_vault_unavailable",
        )
        OAuthCredentialVault._validate_record_revision(record["revision"])
        floor = _mapping(
            record["value"],
            keys={
                "plugin_id",
                "app_id",
                "account_key_sha256",
                "maximum_connection_generation",
            },
            reason="oauth_vault_unavailable",
        )
        plugin_id = _stable_id(floor["plugin_id"], reason="oauth_vault_unavailable")
        app_id = _stable_id(floor["app_id"], reason="oauth_vault_unavailable")
        account = _sha256_digest(
            floor["account_key_sha256"],
            reason="oauth_vault_unavailable",
        )
        generation = floor["maximum_connection_generation"]
        if type(generation) is not int or generation < 0:
            raise ValueError("oauth_vault_unavailable")
        if key != _generation_floor_key(plugin_id, app_id, account):
            raise ValueError("oauth_vault_unavailable")
        return plugin_id, app_id, account, generation

    @classmethod
    def _raise_generation_floor(
        cls,
        document: dict[str, dict[str, Any]],
        grant: OAuthCommittedGrant,
    ) -> None:
        key = _generation_floor_key(
            grant.plugin_id,
            grant.app_id,
            grant.account_key_sha256,
        )
        current = document["migrations"].get(key)
        if current is None:
            revision = 0
            generation = grant.connection_generation
        else:
            _plugin_id, _app_id, _account, previous = cls._decode_generation_floor_record(
                key,
                current,
            )
            if grant.connection_generation <= previous:
                return
            revision = current["revision"] + 1
            generation = grant.connection_generation
        document["migrations"][key] = {
            "revision": revision,
            "value": {
                "plugin_id": grant.plugin_id,
                "app_id": grant.app_id,
                "account_key_sha256": grant.account_key_sha256,
                "maximum_connection_generation": generation,
            },
        }

    @classmethod
    def _seed_generation_floors(cls, document: dict[str, dict[str, Any]]) -> None:
        for record in document["committed_grants"].values():
            cls._raise_generation_floor(document, _decode_grant(record))

    @classmethod
    def _validate_migration_value(cls, value: object, *, depth: int = 0) -> None:
        if depth > 8:
            raise ValueError("oauth_vault_unavailable")
        if value is None or type(value) in (bool, int):
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("oauth_vault_unavailable")
            return
        if type(value) is str:
            if len(value) > 65_536:
                raise ValueError("oauth_vault_unavailable")
            return
        if type(value) is list:
            if len(value) > _MAX_ENTRIES:
                raise ValueError("oauth_vault_unavailable")
            for item in value:
                cls._validate_migration_value(item, depth=depth + 1)
            return
        if type(value) is dict:
            if len(value) > _MAX_ENTRIES:
                raise ValueError("oauth_vault_unavailable")
            for key, item in value.items():
                _stable_id(key, reason="oauth_vault_unavailable")
                cls._validate_migration_value(item, depth=depth + 1)
            return
        raise ValueError("oauth_vault_unavailable")

    @classmethod
    def _validate_migration_records(cls, document: dict[str, dict[str, Any]]) -> None:
        for key, value in document["migrations"].items():
            _stable_id(key, reason="oauth_vault_unavailable")
            if key.startswith(_GENERATION_FLOOR_PREFIX):
                cls._decode_generation_floor_record(key, value)
                continue
            record = _mapping(
                value,
                keys={"revision", "value"},
                reason="oauth_vault_unavailable",
            )
            cls._validate_record_revision(record["revision"])
            if type(record["value"]) is not dict:
                raise ValueError("oauth_vault_unavailable")
            cls._validate_migration_value(record["value"])

    @staticmethod
    def _broker_migration_receipt(
        migration_id: str,
        value: object,
    ) -> dict[str, object]:
        receipt = _mapping(
            value,
            keys={
                "migration_id",
                "preserved_token_keys",
                "deleted_token_keys",
                "disconnected_plugin_ids",
                "source_revisions",
            },
            reason="oauth_vault_unavailable",
        )
        if receipt["migration_id"] != migration_id:
            raise ValueError("oauth_vault_unavailable")

        def exact_tuple(name: str, *, digests: bool) -> tuple[str, ...]:
            raw = receipt[name]
            if type(raw) is not list:
                raise ValueError("oauth_vault_unavailable")
            values = tuple(
                (
                    _sha256_digest(item, reason="oauth_vault_unavailable")
                    if digests
                    else _stable_id(item, reason="oauth_vault_unavailable")
                )
                for item in raw
            )
            if tuple(sorted(set(values))) != values:
                raise ValueError("oauth_vault_unavailable")
            return values

        preserved = exact_tuple("preserved_token_keys", digests=True)
        deleted = exact_tuple("deleted_token_keys", digests=True)
        disconnected = exact_tuple("disconnected_plugin_ids", digests=False)
        sources = exact_tuple("source_revisions", digests=False)
        if set(preserved) & set(deleted):
            raise ValueError("oauth_vault_unavailable")
        return {
            "migration_id": migration_id,
            "preserved_token_keys": list(preserved),
            "deleted_token_keys": list(deleted),
            "disconnected_plugin_ids": list(disconnected),
            "source_revisions": list(sources),
        }

    @classmethod
    def _validate_document(
        cls,
        value: object,
        *,
        enforce_entry_limit: bool = True,
    ) -> dict[str, dict[str, Any]]:
        if type(value) is not dict or set(value) != _DOCUMENT_MAPS:
            raise ValueError("oauth_vault_unavailable")
        if any(type(value[name]) is not dict for name in _DOCUMENT_MAPS):
            raise ValueError("oauth_vault_unavailable")
        document = value
        if enforce_entry_limit and cls._entry_count(document) > _MAX_ENTRIES:
            raise ValueError("oauth_vault_entry_limit_exceeded")
        try:
            cls._validate_credential_records(document)
            cls._validate_registration_records(document)
            cls._validate_transaction_records(document)
            partitions = cls._validate_token_records(document)
            cls._validate_client_records(document)
            cls._validate_claim_records(document)
            cls._validate_account_partition_records(document, partitions)
            grants = cls._validate_grant_records(document)
            cls._validate_grant_partition_relationships(partitions, grants)
            cls._validate_consumed_transaction_records(document)
            cls._validate_migration_records(document)
        except ValueError as exc:
            raise ValueError("oauth_vault_unavailable") from exc
        return document

    def _prune_document(
        self,
        document: dict[str, dict[str, Any]],
        *,
        now: float,
        include_expired_transactions: bool,
        retained_expired_state_sha256: str | None = None,
    ) -> None:
        latest_superseded: dict[str, tuple[int, str]] = {}
        decoded_transactions: dict[str, tuple[bool, OAuthPendingTransaction]] = {}
        for state_sha256 in sorted(tuple(document["transactions"])):
            _revision_number, superseded, transaction = self._decode_transaction_record(
                document["transactions"][state_sha256]
            )
            decoded_transactions[state_sha256] = (superseded, transaction)
            if superseded:
                candidate = (transaction.attempt_revision, state_sha256)
                assert transaction.attempt_key is not None
                if candidate > latest_superseded.get(transaction.attempt_key, (-1, "")):
                    latest_superseded[transaction.attempt_key] = candidate
        for state_sha256, (superseded, transaction) in decoded_transactions.items():
            assert transaction.attempt_key is not None
            retained_superseded = latest_superseded.get(transaction.attempt_key)
            if (
                include_expired_transactions
                and transaction.expires_at <= now
                and transaction.prepared_token is None
                and state_sha256 != retained_expired_state_sha256
            ):
                document["transactions"].pop(state_sha256)
            elif superseded and retained_superseded != (
                transaction.attempt_revision,
                state_sha256,
            ):
                document["transactions"].pop(state_sha256)
        for state_sha256 in sorted(tuple(document["consumed_transactions"])):
            record = document["consumed_transactions"][state_sha256]
            consumed_at = _timestamp(record["consumed_at"], reason="oauth_vault_unavailable")
            if consumed_at + _REPLAY_DEFENSE_HORIZON_SECONDS <= now:
                document["consumed_transactions"].pop(state_sha256)
        for storage_key in sorted(tuple(document["claims"])):
            claims = document["claims"][storage_key]
            for code_sha256 in sorted(tuple(claims)):
                claimed_at = claims[code_sha256]
                if claimed_at is True:
                    claims[code_sha256] = now
                elif (
                    _timestamp(claimed_at, reason="oauth_vault_unavailable")
                    + _REPLAY_DEFENSE_HORIZON_SECONDS
                    <= now
                ):
                    claims.pop(code_sha256)
            if not claims:
                document["claims"].pop(storage_key)
        for receipt_id in sorted(tuple(document["committed_grants"])):
            grant = _decode_grant(document["committed_grants"][receipt_id])
            if grant.status == "revoked":
                document["committed_grants"].pop(receipt_id)
        consumed = document["consumed_transactions"]
        other_entry_count = self._entry_count(document) - len(consumed)
        retained_tombstones = min(
            _MAX_REPLAY_TOMBSTONES,
            max(0, _MAX_ENTRIES - other_entry_count),
        )
        ordered_tombstones = sorted(
            consumed,
            key=lambda state_sha256: (
                _timestamp(
                    consumed[state_sha256]["consumed_at"],
                    reason="oauth_vault_unavailable",
                ),
                state_sha256,
            ),
        )
        eviction_count = max(0, len(ordered_tombstones) - retained_tombstones)
        for state_sha256 in ordered_tombstones[:eviction_count]:
            consumed.pop(state_sha256)

    def _read(
        self,
        *,
        retained_expired_state_sha256: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        try:
            if self.path.exists() and self.path.stat().st_size > self._max_document_bytes:
                raise ValueError("oauth_vault_limit_exceeded")
        except OSError as exc:
            raise ValueError("oauth_vault_unavailable") from exc
        raw = self._state.read_bytes()
        if raw is None:
            return self._empty()
        try:
            envelope = _json_loads(raw)
            if (
                type(envelope) is not dict
                or set(envelope) != {"version", "nonce", "ciphertext"}
                or envelope.get("version") != _VERSION
            ):
                raise ValueError
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            if len(nonce) != _NONCE_BYTES or not ciphertext:
                raise ValueError
            plaintext = AESGCM(self._key_bytes()).decrypt(nonce, ciphertext, _AAD)
            if len(plaintext) > self._max_document_bytes:
                raise ValueError("oauth_vault_limit_exceeded")
            decoded = _json_loads(plaintext)
            migrated = type(decoded) is dict and set(decoded) == _DOCUMENT_MAPS_THROUGH_V5
            if migrated:
                document = self._migrate_v1_document(decoded)
            else:
                if type(decoded) is not dict or set(decoded) != {"schema_version", "records"}:
                    raise ValueError("oauth_vault_migration_required")
                if decoded["schema_version"] == 2:
                    document = self._migrate_v2_document(decoded["records"])
                    migrated = True
                elif decoded["schema_version"] == 3:
                    document = self._migrate_v3_document(decoded["records"])
                    migrated = True
                elif decoded["schema_version"] == 4:
                    document = self._migrate_v4_document(decoded["records"])
                    migrated = True
                elif decoded["schema_version"] == 5:
                    document = self._migrate_v5_document(decoded["records"])
                    migrated = True
                elif decoded["schema_version"] == _INNER_SCHEMA_VERSION:
                    document = self._validate_document(
                        decoded["records"],
                        enforce_entry_limit=False,
                    )
                else:
                    raise ValueError("oauth_vault_migration_required")
            self._prune_document(
                document,
                now=self._clock_value(),
                include_expired_transactions=True,
                retained_expired_state_sha256=retained_expired_state_sha256,
            )
            checked = self._validate_document(document)
            if migrated:
                self._write(checked)
            return checked
        except ValueError as exc:
            if str(exc) in {
                "oauth_vault_entry_limit_exceeded",
                "oauth_vault_limit_exceeded",
                "oauth_vault_locked",
                "oauth_vault_migration_required",
            }:
                raise
            raise ValueError("oauth_vault_unavailable") from exc
        except Exception as exc:
            raise ValueError("oauth_vault_unavailable") from exc

    def _read_for_update(self) -> dict[str, dict[str, Any]]:
        document = self._read()
        self._prune_document(
            document,
            now=self._clock_value(),
            include_expired_transactions=True,
        )
        return document

    def _write(self, document: dict[str, dict[str, Any]]) -> None:
        self._prune_document(
            document,
            now=self._clock_value(),
            include_expired_transactions=False,
        )
        self._validate_document(document)
        plaintext = _json_bytes(
            {
                "records": document,
                "schema_version": _INNER_SCHEMA_VERSION,
            }
        )
        if len(plaintext) > self._max_document_bytes:
            raise ValueError("oauth_vault_limit_exceeded")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._key_bytes()).encrypt(nonce, plaintext, _AAD)
        payload = _json_bytes(
            {
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "version": _VERSION,
            }
        )
        if len(payload) > self._max_document_bytes:
            raise ValueError("oauth_vault_limit_exceeded")
        try:
            self._state.write_bytes(payload)
        except Exception as exc:
            if isinstance(exc, ValueError) and str(exc) in {
                "oauth_vault_entry_limit_exceeded",
                "oauth_vault_limit_exceeded",
                "oauth_vault_locked",
            }:
                raise
            raise ValueError("oauth_vault_unavailable") from exc

    def _cas_put(
        self,
        namespace: str,
        key: str,
        value: dict[str, Any],
        *,
        expected_revision: int | None,
        kind: str | None = None,
    ) -> int:
        if type(expected_revision) not in (int, type(None)) or (
            type(expected_revision) is int and expected_revision < 0
        ):
            raise ValueError("oauth_vault_revision_invalid")
        with self._state.locked():
            document = self._read_for_update()
            current = document[namespace].get(key)
            if current is not None:
                expected_fields = (
                    {"revision", "value"} if kind is None else {"kind", "revision", "value"}
                )
                if (
                    type(current) is not dict
                    or set(current) != expected_fields
                    or (kind is not None and current["kind"] != kind)
                ):
                    raise ValueError("oauth_vault_unavailable")
            current_revision = None if current is None else current.get("revision")
            if current_revision is not None and (
                type(current_revision) is not int or current_revision < 0
            ):
                raise ValueError("oauth_vault_unavailable")
            if current_revision != expected_revision:
                raise ValueError("oauth_vault_revision_conflict")
            revision = 0 if current_revision is None else current_revision + 1
            record: dict[str, Any] = {"revision": revision, "value": value}
            if kind is not None:
                record["kind"] = kind
            document[namespace][key] = record
            self._write(document)
            return revision

    def load_credential(
        self,
        credential_ref: str,
    ) -> tuple[int, OAuthClientCredential] | None:
        key = _credential_ref(credential_ref)
        with self._state.locked():
            record = self._read()["credentials"].get(key)
            if record is None:
                return None
            if (
                type(record) is not dict
                or set(record) != {"revision", "value"}
                or type(record["revision"]) is not int
                or record["revision"] < 0
            ):
                raise ValueError("oauth_vault_unavailable")
            return record["revision"], _decode_credential(record["value"])

    def store_credential(
        self,
        credential_ref: str,
        value: OAuthClientCredential,
        *,
        expected_revision: int | None,
    ) -> int:
        return self._cas_put(
            "credentials",
            _credential_ref(credential_ref),
            _encode_credential(value),
            expected_revision=expected_revision,
        )

    def load_registration(
        self,
        authorization_issuer: str,
        profile_id: str,
    ) -> OAuthClientRegistration | None:
        key = _registration_key(authorization_issuer, profile_id)
        with self._state.locked():
            record = self._read()["registrations"].get(key)
            if record is None:
                return None
            return _decode_registration(record)

    def store_registration(self, registration: OAuthClientRegistration) -> None:
        checked = _registration(registration)
        key = _registration_key(checked.authorization_issuer, checked.profile_id)
        with self._state.locked():
            document = self._read_for_update()
            document["registrations"][key] = _encode_registration(checked)
            self._write(document)

    def delete_registration(
        self,
        authorization_issuer: str,
        profile_id: str,
    ) -> OAuthClientRegistration | None:
        key = _registration_key(authorization_issuer, profile_id)
        with self._state.locked():
            document = self._read_for_update()
            record = document["registrations"].pop(key, None)
            if record is None:
                return None
            registration = _decode_registration(record)
            self._write(document)
            return registration

    def registration_has_dependents(
        self,
        authorization_issuer: str,
        profile_id: str,
    ) -> bool:
        """Return whether an exact registration still owns live OAuth work."""

        issuer = _text(authorization_issuer, reason="oauth_registration_invalid")
        selected_profile = _stable_id(profile_id, reason="oauth_registration_invalid")
        with self._state.locked():
            document = self._read()
            for record in document["transactions"].values():
                _revision, superseded, transaction = self._decode_transaction_record(record)
                if (
                    not superseded
                    and transaction.authorization_issuer == issuer
                    and transaction.profile_id == selected_profile
                ):
                    return True
            for record in document["committed_grants"].values():
                grant = _decode_grant(record)
                if grant.status == "revoked" or grant.profile_id != selected_profile:
                    continue
                partition_record = document["tokens"].get(grant.token_partition_key)
                if partition_record is None:
                    raise ValueError("oauth_vault_unavailable")
                partition = self._decode_token_record(partition_record)
                if partition.authorization_issuer == issuer:
                    return True
            return False

    def migration_receipt(self, migration_id: str) -> dict[str, object] | None:
        """Load one secret-free, durable migration marker from the encrypted vault."""

        key = _stable_id(migration_id, reason="oauth_migration_invalid")
        with self._state.locked():
            record = self._read()["migrations"].get(key)
            if record is None:
                return None
            wrapped = _mapping(
                record,
                keys={"revision", "value"},
                reason="oauth_vault_unavailable",
            )
            self._validate_record_revision(wrapped["revision"])
            receipt = self._broker_migration_receipt(key, wrapped["value"])
        return _json_loads(_json_bytes(receipt))  # type: ignore[return-value]

    def commit_migration(
        self,
        migration_id: str,
        *,
        preserve: tuple[tuple[OAuthTokenPartition, OAuthCommittedGrant], ...],
        deleted_token_keys: tuple[str, ...],
        disconnected_plugin_ids: tuple[str, ...],
        source_revisions: tuple[str, ...],
    ) -> dict[str, object]:
        """Atomically import exact token/grant pairs and their one-time marker."""

        key = _stable_id(migration_id, reason="oauth_migration_invalid")
        if type(preserve) is not tuple:
            raise ValueError("oauth_migration_invalid")
        checked_pairs = tuple(
            (_partition(partition), _grant(grant)) for partition, grant in preserve
        )
        preserved_token_keys = tuple(sorted({item[0].partition_key for item in checked_pairs}))
        receipt = self._broker_migration_receipt(
            key,
            {
                "migration_id": key,
                "preserved_token_keys": list(preserved_token_keys),
                "deleted_token_keys": list(deleted_token_keys),
                "disconnected_plugin_ids": list(disconnected_plugin_ids),
                "source_revisions": list(source_revisions),
            },
        )
        with self._state.locked():
            document = self._read_for_update()
            current = document["migrations"].get(key)
            if current is not None:
                wrapped = _mapping(
                    current,
                    keys={"revision", "value"},
                    reason="oauth_vault_unavailable",
                )
                self._validate_record_revision(wrapped["revision"])
                return self._broker_migration_receipt(key, wrapped["value"])
            for partition, grant in checked_pairs:
                if (
                    partition.partition_key != grant.token_partition_key
                    or partition.profile_id != grant.profile_id
                    or partition.profile_revision != grant.profile_revision
                    or partition.account_key_sha256 != grant.account_key_sha256
                    or grant.plugin_id not in partition.dependent_plugin_ids
                    or not set(grant.scopes).issubset(partition.token.scopes)
                ):
                    raise ValueError("oauth_migration_invalid")
                existing_partition = document["tokens"].get(partition.partition_key)
                if existing_partition is not None:
                    if self._decode_token_record(existing_partition) != partition:
                        raise ValueError("oauth_migration_conflict")
                else:
                    document["tokens"][partition.partition_key] = {
                        "kind": "oauth",
                        "value": _encode_partition(partition),
                    }
                    document["account_partitions"][partition.partition_key] = {
                        "account_key_sha256": partition.account_key_sha256,
                        "account_partition": partition.account_partition,
                        "authorization_issuer": partition.authorization_issuer,
                        "credential_identity": partition.credential_identity,
                    }
                if grant.receipt_id in document["committed_grants"]:
                    raise ValueError("oauth_migration_conflict")
                document["committed_grants"][grant.receipt_id] = _encode_grant(grant)
                self._raise_generation_floor(document, grant)
            document["migrations"][key] = {"revision": 0, "value": receipt}
            self._write(document)
        return _json_loads(_json_bytes(receipt))  # type: ignore[return-value]

    @classmethod
    def _plugin_update_recovery_value(
        cls,
        checkpoint_id: str,
        value: object,
    ) -> dict[str, object]:
        checkpoint = _stable_id(
            checkpoint_id,
            reason="oauth_plugin_update_recovery_invalid",
        )
        if not checkpoint.startswith(_PLUGIN_UPDATE_RECOVERY_PREFIX):
            raise ValueError("oauth_plugin_update_recovery_invalid")
        record = _mapping(
            value,
            keys={
                "checkpoint_id",
                "plugin_id",
                "account_key_sha256",
                "app_ids",
                "tokens",
                "account_partitions",
                "committed_grants",
                "revision",
                "restored",
            },
            reason="oauth_plugin_update_recovery_invalid",
        )
        plugin_id = _stable_id(
            record["plugin_id"],
            reason="oauth_plugin_update_recovery_invalid",
        )
        account = _sha256_digest(
            record["account_key_sha256"],
            reason="oauth_plugin_update_recovery_invalid",
        )
        raw_apps = record["app_ids"]
        if type(raw_apps) is not list:
            raise ValueError("oauth_plugin_update_recovery_invalid")
        app_ids = tuple(
            _stable_id(app_id, reason="oauth_plugin_update_recovery_invalid")
            for app_id in raw_apps
        )
        if app_ids != tuple(sorted(app_ids)) or len(app_ids) != len(set(app_ids)):
            raise ValueError("oauth_plugin_update_recovery_invalid")
        tokens = record["tokens"]
        accounts = record["account_partitions"]
        grants = record["committed_grants"]
        if any(type(item) is not dict for item in (tokens, accounts, grants)):
            raise ValueError("oauth_plugin_update_recovery_invalid")
        checked_partitions: dict[str, OAuthTokenPartition] = {}
        for key, raw in tokens.items():
            partition_key = _sha256_digest(
                key,
                reason="oauth_plugin_update_recovery_invalid",
            )
            partition = cls._decode_token_record(raw)
            if (
                partition.partition_key != partition_key
                or partition.account_key_sha256 != account
                or plugin_id not in partition.dependent_plugin_ids
            ):
                raise ValueError("oauth_plugin_update_recovery_invalid")
            checked_partitions[partition_key] = partition
        if set(accounts) != set(checked_partitions):
            raise ValueError("oauth_plugin_update_recovery_invalid")
        for key, raw in accounts.items():
            partition_key, account_key, account_partition, issuer, credential = (
                cls._validate_account_partition_record(key, raw)
            )
            partition = checked_partitions.get(partition_key)
            if partition is None or (
                account_key,
                account_partition,
                issuer,
                credential,
            ) != (
                partition.account_key_sha256,
                partition.account_partition,
                partition.authorization_issuer,
                partition.credential_identity,
            ):
                raise ValueError("oauth_plugin_update_recovery_invalid")
        for receipt_id, raw in grants.items():
            receipt = _decode_grant(raw)
            if (
                _sha256_digest(
                    receipt_id,
                    reason="oauth_plugin_update_recovery_invalid",
                )
                != receipt.receipt_id
                or receipt.plugin_id != plugin_id
                or receipt.account_key_sha256 != account
                or receipt.app_id not in app_ids
                or receipt.token_partition_key not in checked_partitions
            ):
                raise ValueError("oauth_plugin_update_recovery_invalid")
        material = {
            "checkpoint_id": checkpoint,
            "plugin_id": plugin_id,
            "account_key_sha256": account,
            "app_ids": list(app_ids),
            "tokens": tokens,
            "account_partitions": accounts,
            "committed_grants": grants,
        }
        revision = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.oauth-plugin-update-recovery.v1",
                    "material": material,
                }
            )
        )
        if (
            record["checkpoint_id"] != checkpoint
            or record["revision"] != revision
            or type(record["restored"]) is not bool
        ):
            raise ValueError("oauth_plugin_update_recovery_invalid")
        return {
            **material,
            "revision": revision,
            "restored": record["restored"],
        }

    def begin_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        plugin_id: str,
        account_key_sha256: str,
        app_ids: tuple[str, ...],
    ) -> dict[str, str]:
        checkpoint = _stable_id(
            checkpoint_id,
            reason="oauth_plugin_update_recovery_invalid",
        )
        plugin = _stable_id(
            plugin_id,
            reason="oauth_plugin_update_recovery_invalid",
        )
        account = _sha256_digest(
            account_key_sha256,
            reason="oauth_plugin_update_recovery_invalid",
        )
        if (
            not checkpoint.startswith(_PLUGIN_UPDATE_RECOVERY_PREFIX)
            or type(app_ids) is not tuple
        ):
            raise ValueError("oauth_plugin_update_recovery_invalid")
        apps = tuple(
            sorted(
                _stable_id(app_id, reason="oauth_plugin_update_recovery_invalid")
                for app_id in app_ids
            )
        )
        if len(apps) != len(set(apps)):
            raise ValueError("oauth_plugin_update_recovery_invalid")
        with self._state.locked():
            document = self._read_for_update()
            current = document["migrations"].get(checkpoint)
            if current is not None:
                wrapped = _mapping(
                    current,
                    keys={"revision", "value"},
                    reason="oauth_plugin_update_recovery_invalid",
                )
                value = self._plugin_update_recovery_value(checkpoint, wrapped["value"])
                if (
                    value["plugin_id"] != plugin
                    or value["account_key_sha256"] != account
                    or tuple(value["app_ids"]) != apps
                ):
                    raise ValueError("oauth_plugin_update_recovery_conflict")
                return {"checkpoint_id": checkpoint, "revision": str(value["revision"])}
            grants = {
                receipt_id: raw
                for receipt_id, raw in document["committed_grants"].items()
                for receipt in (_decode_grant(raw),)
                if receipt.plugin_id == plugin
                and receipt.account_key_sha256 == account
                and receipt.app_id in apps
                and receipt.status != "revoked"
            }
            partition_keys = {
                _decode_grant(raw).token_partition_key for raw in grants.values()
            }
            tokens = {
                key: document["tokens"][key]
                for key in sorted(partition_keys)
                if key in document["tokens"]
            }
            accounts = {
                key: document["account_partitions"][key]
                for key in sorted(partition_keys)
                if key in document["account_partitions"]
            }
            if set(tokens) != partition_keys or set(accounts) != partition_keys:
                raise ValueError("oauth_plugin_update_recovery_invalid")
            material = {
                "checkpoint_id": checkpoint,
                "plugin_id": plugin,
                "account_key_sha256": account,
                "app_ids": list(apps),
                "tokens": _json_loads(_json_bytes(tokens)),
                "account_partitions": _json_loads(_json_bytes(accounts)),
                "committed_grants": _json_loads(_json_bytes(grants)),
            }
            revision = sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.oauth-plugin-update-recovery.v1",
                        "material": material,
                    }
                )
            )
            value = {**material, "revision": revision, "restored": False}
            self._plugin_update_recovery_value(checkpoint, value)
            document["migrations"][checkpoint] = {"revision": 0, "value": value}
            self._write(document)
        return {"checkpoint_id": checkpoint, "revision": revision}

    def discard_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        plugin_id: str,
        account_key_sha256: str,
    ) -> None:
        """Discard one exact unconsumed checkpoint after an unsealed update crash."""

        checkpoint = _stable_id(
            checkpoint_id,
            reason="oauth_plugin_update_recovery_invalid",
        )
        plugin = _stable_id(
            plugin_id,
            reason="oauth_plugin_update_recovery_invalid",
        )
        account = _sha256_digest(
            account_key_sha256,
            reason="oauth_plugin_update_recovery_invalid",
        )
        if not checkpoint.startswith(_PLUGIN_UPDATE_RECOVERY_PREFIX):
            raise ValueError("oauth_plugin_update_recovery_invalid")
        with self._state.locked():
            document = self._read_for_update()
            wrapped = document["migrations"].get(checkpoint)
            if wrapped is None:
                return
            wrapped = _mapping(
                wrapped,
                keys={"revision", "value"},
                reason="oauth_plugin_update_recovery_invalid",
            )
            value = self._plugin_update_recovery_value(checkpoint, wrapped["value"])
            if (
                value["plugin_id"] != plugin
                or value["account_key_sha256"] != account
                or value["restored"] is not False
            ):
                raise ValueError("oauth_plugin_update_recovery_conflict")
            document["migrations"].pop(checkpoint)
            self._write(document)

    def restore_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        expected_revision: str,
    ) -> dict[str, str]:
        checkpoint = _stable_id(
            checkpoint_id,
            reason="oauth_plugin_update_recovery_invalid",
        )
        expected = _sha256_digest(
            expected_revision,
            reason="oauth_plugin_update_recovery_invalid",
        )
        with self._state.locked():
            document = self._read_for_update()
            wrapped = document["migrations"].get(checkpoint)
            if wrapped is None:
                raise ValueError("oauth_plugin_update_recovery_missing")
            wrapped = _mapping(
                wrapped,
                keys={"revision", "value"},
                reason="oauth_plugin_update_recovery_invalid",
            )
            value = self._plugin_update_recovery_value(checkpoint, wrapped["value"])
            if value["revision"] != expected:
                raise ValueError("oauth_plugin_update_recovery_conflict")
            plugin = str(value["plugin_id"])
            account = str(value["account_key_sha256"])
            app_ids = set(value["app_ids"])
            predecessor_tokens = value["tokens"]
            predecessor_accounts = value["account_partitions"]
            predecessor_grants = value["committed_grants"]
            assert type(predecessor_tokens) is dict
            assert type(predecessor_accounts) is dict
            assert type(predecessor_grants) is dict
            affected_partitions = set(predecessor_tokens)
            for receipt_id, raw in tuple(document["committed_grants"].items()):
                receipt = _decode_grant(raw)
                if (
                    receipt.plugin_id == plugin
                    and receipt.account_key_sha256 == account
                    and receipt.app_id in app_ids
                ):
                    affected_partitions.add(receipt.token_partition_key)
                    document["committed_grants"].pop(receipt_id)
            for receipt_id, raw in predecessor_grants.items():
                current = document["committed_grants"].get(receipt_id)
                if current is not None and current != raw:
                    raise ValueError("oauth_plugin_update_recovery_conflict")
                document["committed_grants"][receipt_id] = _json_loads(
                    _json_bytes(raw)
                )

            for partition_key in sorted(affected_partitions):
                partition_grants = tuple(
                    grant
                    for raw in document["committed_grants"].values()
                    for grant in (_decode_grant(raw),)
                    if grant.token_partition_key == partition_key
                    and grant.status != "revoked"
                )
                if not partition_grants:
                    document["tokens"].pop(partition_key, None)
                    document["account_partitions"].pop(partition_key, None)
                    continue
                dependents = tuple(
                    sorted({grant.plugin_id for grant in partition_grants})
                )
                if len(dependents) != len(partition_grants):
                    raise ValueError("oauth_plugin_update_recovery_conflict")
                current_raw = document["tokens"].get(partition_key)
                snapshot_raw = predecessor_tokens.get(partition_key)
                current_partition = (
                    None
                    if current_raw is None
                    else self._decode_token_record(current_raw)
                )
                snapshot_partition = (
                    None
                    if snapshot_raw is None
                    else self._decode_token_record(snapshot_raw)
                )
                if current_partition is not None and snapshot_partition is not None:
                    if _partition_identity_digest(current_partition) != (
                        _partition_identity_digest(snapshot_partition)
                    ):
                        raise ValueError("oauth_plugin_update_recovery_conflict")
                    current_account = self._validate_account_partition_record(
                        partition_key,
                        document["account_partitions"].get(partition_key),
                    )
                    snapshot_account = self._validate_account_partition_record(
                        partition_key,
                        predecessor_accounts.get(partition_key),
                    )
                    if current_account != snapshot_account:
                        raise ValueError("oauth_plugin_update_recovery_conflict")
                current_is_usable = (
                    current_partition is not None
                    and current_partition.revocation_pending_plugin_id is None
                    and not current_partition.reauthorization_required
                    and current_partition.migration_status is None
                    and all(
                        set(grant.scopes).issubset(current_partition.token.scopes)
                        and grant.status == "active"
                        for grant in partition_grants
                    )
                )
                if current_is_usable:
                    assert current_partition is not None
                    metadata_changed = (
                        current_partition.dependent_plugin_ids != dependents
                    )
                    restored_partition = replace(
                        current_partition,
                        dependent_plugin_ids=dependents,
                        revision=current_partition.revision + int(metadata_changed),
                    )
                else:
                    has_non_target_grant = any(
                        grant.plugin_id != plugin
                        or grant.account_key_sha256 != account
                        or grant.app_id not in app_ids
                        for grant in partition_grants
                    )
                    # A structural conflict: the predecessor snapshot does not
                    # describe this partition, so no rollback may proceed on it.
                    if snapshot_partition is None or has_non_target_grant or any(
                        not set(grant.scopes).issubset(snapshot_partition.token.scopes)
                        for grant in partition_grants
                    ):
                        raise ValueError("oauth_plugin_update_recovery_conflict")
                    # A grant awaiting reauthorization is NOT that. It says the
                    # credential is unusable and a sign-in resolves it -- and
                    # refusing the whole rollback for it stranded the update
                    # permanently, taking the lifecycle and the HUD with it.
                    # Finish instead, restoring nothing and leaving the
                    # partition explicitly needing authorization.
                    if any(grant.status != "active" for grant in partition_grants):
                        if current_partition is None:
                            document["tokens"].pop(partition_key, None)
                            document["account_partitions"].pop(partition_key, None)
                            continue
                        restored_partition = replace(
                            current_partition,
                            dependent_plugin_ids=dependents,
                            revision=current_partition.revision + 1,
                            reauthorization_required=True,
                        )
                    else:
                        restored_partition = replace(
                            snapshot_partition,
                            dependent_plugin_ids=dependents,
                            revision=max(
                                snapshot_partition.revision,
                                0
                                if current_partition is None
                                else current_partition.revision + 1,
                            ),
                            revocation_pending_plugin_id=None,
                            reauthorization_required=False,
                            migration_status=None,
                        )
                        document["account_partitions"][partition_key] = (
                            _json_loads(
                                _json_bytes(predecessor_accounts[partition_key])
                            )
                        )
                document["tokens"][partition_key] = {
                    "kind": "oauth",
                    "value": _encode_partition(restored_partition),
                }
            value["restored"] = True
            document["migrations"][checkpoint] = {
                "revision": int(wrapped["revision"]) + 1,
                "value": value,
            }
            self._write(document)
        return {"checkpoint_id": checkpoint, "revision": expected}

    def finalize_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        expected_revision: str,
    ) -> None:
        checkpoint = _stable_id(
            checkpoint_id,
            reason="oauth_plugin_update_recovery_invalid",
        )
        expected = _sha256_digest(
            expected_revision,
            reason="oauth_plugin_update_recovery_invalid",
        )
        with self._state.locked():
            document = self._read_for_update()
            wrapped = document["migrations"].get(checkpoint)
            if wrapped is None:
                return
            wrapped = _mapping(
                wrapped,
                keys={"revision", "value"},
                reason="oauth_plugin_update_recovery_invalid",
            )
            value = self._plugin_update_recovery_value(checkpoint, wrapped["value"])
            if value["revision"] != expected:
                raise ValueError("oauth_plugin_update_recovery_conflict")
            document["migrations"].pop(checkpoint)
            self._write(document)

    @staticmethod
    def _decode_transaction_record(record: object) -> tuple[int, bool, OAuthPendingTransaction]:
        if (
            type(record) is not dict
            or set(record) != {"revision", "superseded", "value"}
            or type(record["revision"]) is not int
            or record["revision"] < 0
            or type(record["superseded"]) is not bool
        ):
            raise ValueError("oauth_vault_unavailable")
        return record["revision"], record["superseded"], _decode_transaction(record["value"])

    def _current_transaction_record(
        self,
        document: dict[str, dict[str, Any]],
        state_sha256: str,
    ) -> tuple[dict[str, Any], OAuthPendingTransaction]:
        record = document["transactions"].get(state_sha256)
        if record is None:
            raise ValueError("oauth_transaction_missing")
        _revision_number, superseded, current = self._decode_transaction_record(record)
        if superseded:
            raise ValueError("oauth_transaction_superseded")
        if self._clock_value() >= current.expires_at and current.prepared_token is None:
            raise ValueError("oauth_transaction_expired")
        return record, current

    def _replace_active_transaction(
        self,
        attempt_key: str,
        value: OAuthPendingTransaction,
    ) -> None:
        checked = _transaction(value)
        if checked.attempt_key != _sha256_digest(
            attempt_key,
            reason="oauth_transaction_invalid",
        ):
            raise ValueError("oauth_transaction_invalid")
        state_key = checked.state_sha256
        with self._state.locked():
            document = self._read_for_update()
            if state_key in document["transactions"]:
                raise ValueError("oauth_transaction_state_conflict")
            for key, record in tuple(document["transactions"].items()):
                _revision, superseded, current = self._decode_transaction_record(record)
                if current.attempt_key == checked.attempt_key and superseded:
                    document["transactions"].pop(key)
            superseded_keys: list[str] = []
            for key, record in document["transactions"].items():
                revision, superseded, current = self._decode_transaction_record(record)
                if current.attempt_key != checked.attempt_key or superseded:
                    continue
                if checked.attempt_revision <= current.attempt_revision:
                    raise ValueError("oauth_transaction_revision_conflict")
                document["transactions"][key] = {
                    "revision": revision + 1,
                    "superseded": True,
                    "value": _encode_transaction(current),
                }
                superseded_keys.append(key)
            if len(superseded_keys) > 1:
                raise ValueError("oauth_vault_unavailable")
            document["transactions"][state_key] = {
                "revision": 0,
                "superseded": False,
                "value": _encode_transaction(checked),
            }
            self._write(document)

    def begin_transaction(self, value: OAuthPendingTransaction) -> None:
        checked = _transaction(value)
        assert checked.attempt_key is not None
        self._replace_active_transaction(checked.attempt_key, checked)

    def next_attempt_revision(self, attempt_key: str) -> int:
        key = _sha256_digest(attempt_key, reason="oauth_transaction_invalid")
        with self._state.locked():
            revisions = tuple(
                transaction.attempt_revision
                for record in self._read()["transactions"].values()
                for _record_revision, _superseded, transaction in (
                    self._decode_transaction_record(record),
                )
                if transaction.attempt_key == key
            )
            return max(revisions, default=-1) + 1

    def _current_transaction(self, state_sha256: str) -> OAuthPendingTransaction:
        with self._state.locked():
            _record, current = self._current_transaction_record(
                self._read(retained_expired_state_sha256=state_sha256),
                state_sha256,
            )
            return current

    def load_transaction(self, state_sha256: str) -> OAuthPendingTransaction:
        key = _sha256_digest(state_sha256, reason="oauth_transaction_invalid")
        return self._current_transaction(key)

    def recover_prepared_transactions(self) -> tuple[OAuthPendingTransaction, ...]:
        with self._state.locked():
            found: list[OAuthPendingTransaction] = []
            records = self._read()["transactions"]
            for key in sorted(records):
                record = records[key]
                _revision_number, superseded, transaction = self._decode_transaction_record(record)
                if not superseded and transaction.prepared_token is not None:
                    found.append(transaction)
            return tuple(found)

    def candidate_attempt_state_sha256(
        self,
        candidate_identity: object,
    ) -> tuple[str, ...]:
        """Enumerate active attempts bound to one server-held candidate identity."""

        checked = _attempt_context(
            {
                "schema_version": 2,
                "candidate_identity": candidate_identity,
            }
        )["candidate_identity"]
        with self._state.locked():
            found: list[str] = []
            for record in self._read()["transactions"].values():
                _revision, superseded, transaction = self._decode_transaction_record(record)
                if (
                    not superseded
                    and transaction.attempt_context.get("schema_version") == 2
                    and transaction.attempt_context.get("candidate_identity") == checked
                ):
                    found.append(transaction.state_sha256)
            return tuple(sorted(found))

    @staticmethod
    def _record_consumed_transaction(
        document: dict[str, dict[str, Any]],
        state_sha256: str,
        *,
        consumed_at: float,
        reason: str,
    ) -> None:
        if state_sha256 in document["consumed_transactions"]:
            raise ValueError("oauth_callback_replayed")
        document["consumed_transactions"][state_sha256] = {
            "consumed_at": consumed_at,
            "reason": _stable_id(reason, reason="oauth_transaction_invalid"),
        }

    def consume_transaction(self, state_sha256: str, *, reason: str) -> None:
        key = _sha256_digest(state_sha256, reason="oauth_transaction_invalid")
        with self._state.locked():
            document = self._read()
            self._current_transaction_record(document, key)
            self._record_consumed_transaction(
                document,
                key,
                consumed_at=self._clock_value(),
                reason=reason,
            )
            document["transactions"].pop(key)
            self._write(document)

    def consume_connection_transactions(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> None:
        """Cancel unfinished sign-ins for exactly one connection, never its grants."""

        identity = (
            _stable_id(plugin_id, reason="oauth_transaction_invalid"),
            _stable_id(app_id, reason="oauth_transaction_invalid"),
            _sha256_digest(account_key_sha256, reason="oauth_transaction_invalid"),
        )
        with self._state.locked():
            document = self._read_for_update()
            removed = False
            for key, record in tuple(document["transactions"].items()):
                _revision, _superseded, transaction = self._decode_transaction_record(record)
                if (
                    transaction.plugin_id,
                    transaction.app_id,
                    transaction.account_key_sha256,
                ) != identity:
                    continue
                self._record_consumed_transaction(
                    document,
                    key,
                    consumed_at=self._clock_value(),
                    reason="connection_cancelled",
                )
                document["transactions"].pop(key)
                removed = True
            if removed:
                self._write(document)

    def is_transaction_consumed(self, state_sha256: str) -> bool:
        key = _sha256_digest(state_sha256, reason="oauth_transaction_invalid")
        with self._state.locked():
            return key in self._read()["consumed_transactions"]

    def _mutate_transaction(
        self,
        state_sha256: str,
        mutation: Callable[[OAuthPendingTransaction], OAuthPendingTransaction],
    ) -> None:
        with self._state.locked():
            document = self._read()
            record, current = self._current_transaction_record(document, state_sha256)
            expected_revision = record["revision"]
            changed = _transaction(mutation(current))
            if changed.state_sha256 != current.state_sha256:
                raise ValueError("oauth_transaction_invalid")
            latest = document["transactions"].get(state_sha256)
            if type(latest) is not dict or latest.get("revision") != expected_revision:
                raise ValueError("oauth_transaction_revision_conflict")
            document["transactions"][state_sha256] = {
                "revision": expected_revision + 1,
                "superseded": False,
                "value": _encode_transaction(changed),
            }
            self._write(document)

    def prepare_token(self, state_sha256: str, token: OAuthPreparedToken) -> None:
        key = _sha256_digest(state_sha256, reason="oauth_transaction_invalid")
        checked = _prepared_token(token)

        def attach(current: OAuthPendingTransaction) -> OAuthPendingTransaction:
            if current.prepared_token is not None:
                raise ValueError("oauth_transaction_already_prepared")
            return replace(current, prepared_token=checked)

        self._mutate_transaction(key, attach)

    def record_device_poll(self, state_sha256: str, *, slow_down: bool) -> None:
        """Persist the provider-mandated next poll boundary for one device attempt."""

        key = _sha256_digest(state_sha256, reason="oauth_transaction_invalid")
        if type(slow_down) is not bool:
            raise ValueError("oauth_transaction_invalid")
        now = self._clock_value()

        def advance(current: OAuthPendingTransaction) -> OAuthPendingTransaction:
            if (
                current.authorization_flow != OAuthAuthorizationFlow.DEVICE_CODE.value
                or current.prepared_token is not None
                or current.device_interval_seconds is None
            ):
                raise ValueError("oauth_transaction_invalid")
            interval = min(
                300,
                current.device_interval_seconds + (5 if slow_down else 0),
            )
            return replace(
                current,
                device_interval_seconds=interval,
                device_next_poll_at=now + interval,
            )

        self._mutate_transaction(key, advance)

    @staticmethod
    def _decode_token_record(record: object) -> OAuthTokenPartition:
        if type(record) is not dict or set(record) != {"kind", "value"}:
            raise ValueError("oauth_vault_unavailable")
        if record["kind"] != "oauth":
            raise ValueError("oauth_vault_unavailable")
        return _decode_partition(record["value"])

    @staticmethod
    def _grant_matches_transaction(
        grant: OAuthCommittedGrant,
        transaction: OAuthPendingTransaction,
    ) -> bool:
        return (
            grant.plugin_id == transaction.plugin_id
            and grant.app_id == transaction.app_id
            and grant.account_key_sha256 == transaction.account_key_sha256
            and grant.profile_id == transaction.profile_id
            and grant.profile_revision == transaction.profile_revision
            and grant.token_partition_key == transaction.partition_key
            and grant.scopes == transaction.requested_scopes
            and grant.status == "active"
            and grant.claimed_authority_revision is None
        )

    def _commit_prepared_transaction(
        self,
        state_sha256: str,
        grant: OAuthCommittedGrant,
    ) -> OAuthTokenPartition:
        state_key = _sha256_digest(state_sha256, reason="oauth_transaction_invalid")
        checked_grant = _grant(grant)
        with self._state.locked():
            document = self._read()
            _record, transaction = self._current_transaction_record(document, state_key)
            if transaction.prepared_token is None:
                raise ValueError("oauth_transaction_unprepared")
            if (
                not self._grant_matches_transaction(checked_grant, transaction)
                or transaction.prepared_token.scopes != transaction.scopes
            ):
                raise ValueError("oauth_grant_mismatch")
            if checked_grant.receipt_id in document["committed_grants"]:
                raise ValueError("oauth_grant_receipt_conflict")
            existing_record = document["tokens"].get(transaction.partition_key)
            if existing_record is None:
                dependents = (transaction.plugin_id,)
                token_revision = 0
            else:
                existing = self._decode_token_record(existing_record)
                identity_matches = (
                    existing.partition_key == transaction.partition_key
                    and existing.account_partition == transaction.account_partition
                    and existing.authorization_issuer == transaction.authorization_issuer
                    and existing.account_key_sha256 == transaction.account_key_sha256
                    and existing.credential_identity == transaction.credential_identity
                )
                if not identity_matches or existing.revocation_pending_plugin_id is not None:
                    raise ValueError("oauth_token_partition_conflict")
                existing_plugin_grants = tuple(
                    grant
                    for record in document["committed_grants"].values()
                    for grant in (_decode_grant(record),)
                    if grant.token_partition_key == transaction.partition_key
                    and grant.plugin_id == transaction.plugin_id
                    and grant.status != "revoked"
                )
                if existing.reauthorization_required:
                    if (
                        transaction.plugin_id not in existing.dependent_plugin_ids
                        or not existing_plugin_grants
                        or any(
                            grant.app_id != transaction.app_id for grant in existing_plugin_grants
                        )
                    ):
                        raise ValueError("oauth_token_partition_conflict")
                    for receipt_id, record in tuple(document["committed_grants"].items()):
                        grant_record = _decode_grant(record)
                        if grant_record.token_partition_key != transaction.partition_key:
                            continue
                        if grant_record.plugin_id == transaction.plugin_id:
                            document["committed_grants"].pop(receipt_id)
                        elif grant_record.status == "reauthorization_required":
                            document["committed_grants"][receipt_id] = _encode_grant(
                                replace(grant_record, status="active")
                            )
                    dependents = existing.dependent_plugin_ids
                else:
                    for receipt_id, record in tuple(document["committed_grants"].items()):
                        grant_record = _decode_grant(record)
                        if (
                            grant_record.token_partition_key == transaction.partition_key
                            and grant_record.plugin_id == transaction.plugin_id
                        ):
                            document["committed_grants"].pop(receipt_id)
                    dependents = tuple(
                        sorted({*existing.dependent_plugin_ids, transaction.plugin_id})
                    )
                token_revision = existing.revision + 1
            partition = OAuthTokenPartition(
                partition_key=transaction.partition_key,
                profile_id=transaction.profile_id,
                profile_revision=transaction.profile_revision,
                authorization_issuer=transaction.authorization_issuer,
                resource_origin=transaction.resource_origin,
                account_key_sha256=transaction.account_key_sha256,
                client_id=transaction.client_id,
                authorization_context_revision=(transaction.authorization_context_revision),
                token=transaction.prepared_token,
                dependent_plugin_ids=dependents,
                revision=token_revision,
                revocation_pending_plugin_id=None,
                reauthorization_required=False,
                account_partition=transaction.account_partition,
                credential_identity=transaction.credential_identity,
            )
            document["tokens"][transaction.partition_key] = {
                "kind": "oauth",
                "value": _encode_partition(partition),
            }
            document["account_partitions"][transaction.partition_key] = {
                "account_key_sha256": transaction.account_key_sha256,
                "account_partition": transaction.account_partition,
                "authorization_issuer": transaction.authorization_issuer,
                "credential_identity": transaction.credential_identity,
            }
            document["committed_grants"][checked_grant.receipt_id] = _encode_grant(checked_grant)
            self._raise_generation_floor(document, checked_grant)
            self._record_consumed_transaction(
                document,
                state_key,
                consumed_at=self._clock_value(),
                reason="committed",
            )
            document["transactions"].pop(state_key)
            self._write(document)
            return partition

    def commit_authorization(
        self,
        state_sha256: str,
        grant: OAuthCommittedGrant,
    ) -> OAuthTokenPartition:
        return self._commit_prepared_transaction(
            _sha256_digest(state_sha256, reason="oauth_transaction_invalid"),
            grant,
        )

    def _unclaimed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        with self._state.locked():
            records = self._read()["committed_grants"]
            grants = tuple(_decode_grant(records[key]) for key in sorted(records))
            return tuple(
                grant
                for grant in grants
                if grant.claimed_authority_revision is None and grant.status == "active"
            )

    def claim_committed_grants(self) -> tuple[OAuthCommittedGrant, ...]:
        return self._unclaimed_grant_receipts()

    def committed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        with self._state.locked():
            records = self._read()["committed_grants"]
            return tuple(_decode_grant(records[key]) for key in sorted(records))

    def grant_receipt_state(self, expected: OAuthCommittedGrant) -> str:
        checked = _grant(expected)
        with self._state.locked():
            record = self._read()["committed_grants"].get(checked.receipt_id)
            if record is None:
                return "absent"
            stored = _decode_grant(record)
        if replace(stored, claimed_authority_revision=None) != replace(
            checked,
            claimed_authority_revision=None,
        ):
            raise ValueError("oauth_grant_receipt_conflict")
        return "claimed" if stored.claimed_authority_revision is not None else "unclaimed"

    def claim_grant_receipt_exact(
        self,
        expected: OAuthCommittedGrant,
        *,
        authority_revision: str,
    ) -> OAuthGrantReceiptClaimOutcome:
        checked = _grant(expected)
        claim_revision = _binding_revision(
            authority_revision,
            reason="oauth_grant_invalid",
        )
        if (
            checked.status != "active"
            or checked.claimed_authority_revision is not None
            or claim_revision != checked.authority_revision
        ):
            raise ValueError("oauth_grant_receipt_claim_conflict")
        with self._state.locked():
            document = self._read_for_update()
            record = document["committed_grants"].get(checked.receipt_id)
            if record is None:
                return OAuthGrantReceiptClaimOutcome.ABSENT
            stored = _decode_grant(record)
            if replace(
                stored,
                status=checked.status,
                claimed_authority_revision=None,
            ) != checked:
                raise ValueError("oauth_grant_receipt_conflict")
            if stored.status != "active":
                raise ValueError("oauth_grant_receipt_conflict")
            if stored.claimed_authority_revision is not None:
                if stored.claimed_authority_revision != claim_revision:
                    raise ValueError("oauth_grant_receipt_claim_conflict")
                return OAuthGrantReceiptClaimOutcome.IDEMPOTENTLY_CLAIMED
            document["committed_grants"][checked.receipt_id] = _encode_grant(
                replace(stored, claimed_authority_revision=claim_revision)
            )
            self._write(document)
            return OAuthGrantReceiptClaimOutcome.CLAIMED

    def maximum_connection_generation(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> int | None:
        identity = (
            _stable_id(plugin_id, reason="oauth_grant_invalid"),
            _stable_id(app_id, reason="oauth_grant_invalid"),
            _sha256_digest(account_key_sha256, reason="oauth_grant_invalid"),
        )
        with self._state.locked():
            document = self._read()
            generations = [
                grant.connection_generation
                for record in document["committed_grants"].values()
                for grant in (_decode_grant(record),)
                if (grant.plugin_id, grant.app_id, grant.account_key_sha256) == identity
            ]
            floor_key = _generation_floor_key(*identity)
            floor_record = document["migrations"].get(floor_key)
            if floor_record is not None:
                floor_identity = self._decode_generation_floor_record(
                    floor_key,
                    floor_record,
                )
                if floor_identity[:3] != identity:
                    raise ValueError("oauth_vault_unavailable")
                generations.append(floor_identity[3])
        return max(generations) if generations else None

    def _claim_grant_receipt(
        self,
        receipt_id: str,
        authority_revision: str,
    ) -> None:
        with self._state.locked():
            document = self._read_for_update()
            record = document["committed_grants"].get(receipt_id)
            if record is None:
                raise ValueError("oauth_grant_receipt_missing")
            grant = _decode_grant(record)
            if grant.claimed_authority_revision == authority_revision:
                return
            if grant.claimed_authority_revision is not None:
                raise ValueError("oauth_grant_receipt_claim_conflict")
            if authority_revision != grant.authority_revision:
                raise ValueError("oauth_grant_receipt_claim_conflict")
            document["committed_grants"][receipt_id] = _encode_grant(
                replace(grant, claimed_authority_revision=authority_revision)
            )
            self._write(document)

    def mark_grant_claimed(self, receipt_id: str, *, authority_revision: str) -> None:
        self._claim_grant_receipt(
            _sha256_digest(receipt_id, reason="oauth_grant_invalid"),
            _binding_revision(authority_revision, reason="oauth_grant_invalid"),
        )

    @staticmethod
    def _set_grant_status(
        document: dict[str, dict[str, Any]],
        *,
        partition_key: str,
        plugin_id: str,
        status: str,
    ) -> None:
        changed = False
        for receipt_id, record in tuple(document["committed_grants"].items()):
            grant = _decode_grant(record)
            if (
                grant.token_partition_key == partition_key
                and grant.plugin_id == plugin_id
                and grant.status != "revoked"
            ):
                document["committed_grants"][receipt_id] = _encode_grant(
                    replace(grant, status=status)
                )
                changed = True
        if not changed:
            raise ValueError("oauth_transport_binding_invalid")

    def validate_transport_grant(
        self,
        *,
        plugin_id: str,
        app_id: str,
        profile_id: str,
        profile_revision: str,
        partition_key: str,
        connection_revision: str,
        capability_revision: str,
        authority_revision: str,
        capability_binding_revision: str,
    ) -> OAuthCommittedGrant:
        expected = {
            "plugin_id": _stable_id(plugin_id, reason="oauth_transport_binding_invalid"),
            "app_id": _stable_id(app_id, reason="oauth_transport_binding_invalid"),
            "profile_id": _stable_id(profile_id, reason="oauth_transport_binding_invalid"),
            "profile_revision": _revision(
                profile_revision,
                reason="oauth_transport_binding_invalid",
            ),
            "token_partition_key": _sha256_digest(
                partition_key,
                reason="oauth_transport_binding_invalid",
            ),
            "connection_revision": _binding_revision(
                connection_revision,
                reason="oauth_transport_binding_invalid",
            ),
            "capability_revision": _binding_revision(
                capability_revision,
                reason="oauth_transport_binding_invalid",
            ),
            "authority_revision": _binding_revision(
                authority_revision,
                reason="oauth_transport_binding_invalid",
            ),
            "capability_binding_revision": _binding_revision(
                capability_binding_revision,
                reason="oauth_transport_binding_invalid",
            ),
        }
        with self._state.locked():
            grants = tuple(
                grant
                for record in self._read()["committed_grants"].values()
                for grant in (_decode_grant(record),)
                if all(getattr(grant, field) == value for field, value in expected.items())
            )
        if len(grants) != 1:
            raise ValueError("oauth_transport_binding_invalid")
        grant = grants[0]
        if grant.status == "reauthorization_required":
            raise ValueError("oauth_reauthorization_required")
        if grant.status == "revocation_pending":
            raise ValueError("oauth_revocation_pending")
        if grant.status != "active":
            raise ValueError("oauth_transport_binding_invalid")
        return grant

    def mark_partition_reauthorization_required(self, partition_key: str) -> None:
        key = _sha256_digest(partition_key, reason="oauth_token_partition_invalid")
        with self._state.locked():
            document = self._read_for_update()
            record = document["tokens"].get(key)
            if record is None:
                raise ValueError("oauth_token_partition_missing")
            current = self._decode_token_record(record)
            if current.revocation_pending_plugin_id is not None:
                raise ValueError("oauth_revocation_pending")
            for plugin_id in current.dependent_plugin_ids:
                self._set_grant_status(
                    document,
                    partition_key=key,
                    plugin_id=plugin_id,
                    status="reauthorization_required",
                )
            changed = replace(
                current,
                revision=current.revision + 1,
                reauthorization_required=True,
            )
            document["tokens"][key] = {
                "kind": "oauth",
                "value": _encode_partition(changed),
            }
            self._write(document)

    def clear_partition_reauthorization_required(
        self,
        partition_key: str,
        *,
        now: float,
    ) -> None:
        """Undo a reauthorization mark that the credential does not justify.

        Refuses unless the stored token is still within its life: clearing the
        flag asserts the credential is usable, and an expired one is not made
        usable by dropping a flag. `oauth_reauthorization_required` is raised
        when it cannot honestly be cleared, so a caller that must have a working
        credential still learns it needs the sign-in.
        """

        key = _sha256_digest(partition_key, reason="oauth_token_partition_invalid")
        with self._state.locked():
            document = self._read_for_update()
            record = document["tokens"].get(key)
            if record is None:
                raise ValueError("oauth_token_partition_missing")
            current = self._decode_token_record(record)
            if current.revocation_pending_plugin_id is not None:
                raise ValueError("oauth_revocation_pending")
            if current.migration_status is not None:
                raise ValueError("oauth_reauthorization_required")
            expires_at = current.token.expires_at
            if expires_at is not None and expires_at <= now:
                raise ValueError("oauth_reauthorization_required")
            if not current.reauthorization_required:
                return
            for receipt_id, raw in tuple(document["committed_grants"].items()):
                grant = _decode_grant(raw)
                if (
                    grant.token_partition_key == key
                    and grant.status == "reauthorization_required"
                ):
                    document["committed_grants"][receipt_id] = _encode_grant(
                        replace(grant, status="active")
                    )
            document["tokens"][key] = {
                "kind": "oauth",
                "value": _encode_partition(
                    replace(
                        current,
                        revision=current.revision + 1,
                        reauthorization_required=False,
                    )
                ),
            }
            self._write(document)

    def _current_token_partition(self, partition_key: str) -> OAuthTokenPartition:
        with self._state.locked():
            record = self._read()["tokens"].get(partition_key)
            if record is None:
                raise ValueError("oauth_token_partition_missing")
            return self._decode_token_record(record)

    def load_token_partition(self, partition_key: str) -> OAuthTokenPartition:
        return self._current_token_partition(
            _sha256_digest(partition_key, reason="oauth_token_partition_invalid")
        )

    def scope_union(
        self,
        partition_key: str,
        requested_scopes: tuple[str, ...],
        *,
        replacing_plugin_id: str,
    ) -> tuple[str, ...]:
        key = _sha256_digest(partition_key, reason="oauth_token_partition_invalid")
        requested = set(_scopes(requested_scopes, reason="oauth_token_partition_invalid"))
        plugin_id = _stable_id(
            replacing_plugin_id,
            reason="oauth_token_partition_invalid",
        )
        with self._state.locked():
            document = self._read()
            if key not in document["tokens"]:
                return tuple(sorted(requested))
            for record in document["committed_grants"].values():
                grant = _decode_grant(record)
                if (
                    grant.token_partition_key == key
                    and grant.plugin_id != plugin_id
                    and grant.status in {"active", "reauthorization_required"}
                ):
                    requested.update(grant.scopes)
        return tuple(sorted(requested))

    def _replace_partition_token(
        self,
        partition_key: str,
        token: OAuthPreparedToken,
        *,
        expected_revision: int,
    ) -> OAuthTokenPartition:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("oauth_token_revision_invalid")
        checked_token = _prepared_token(token)
        with self._state.locked():
            document = self._read_for_update()
            record = document["tokens"].get(partition_key)
            if record is None:
                raise ValueError("oauth_token_partition_missing")
            current = self._decode_token_record(record)
            if current.revision != expected_revision:
                raise ValueError("oauth_token_revision_conflict")
            if current.revocation_pending_plugin_id is not None:
                raise ValueError("oauth_revocation_pending")
            if current.reauthorization_required or current.migration_status is not None:
                raise ValueError("oauth_reauthorization_required")
            if checked_token.scopes != current.token.scopes:
                raise ValueError("oauth_token_scope_conflict")
            changed = replace(
                current,
                token=checked_token,
                revision=current.revision + 1,
            )
            document["tokens"][partition_key] = {
                "kind": "oauth",
                "value": _encode_partition(changed),
            }
            self._write(document)
            return changed

    def replace_token_partition(
        self,
        partition_key: str,
        token: OAuthPreparedToken,
        *,
        expected_revision: int,
    ) -> OAuthTokenPartition:
        return self._replace_partition_token(
            _sha256_digest(partition_key, reason="oauth_token_partition_invalid"),
            token,
            expected_revision=expected_revision,
        )

    def prepare_revocation(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_receipts: tuple[OAuthCommittedGrant, ...] | None = None,
    ) -> tuple[OAuthTokenPartition, tuple[str, ...]]:
        key = _sha256_digest(partition_key, reason="oauth_token_partition_invalid")
        dependent = _stable_id(plugin_id, reason="oauth_token_partition_invalid")
        expected = (
            None
            if expected_receipts is None
            else tuple(_grant(item) for item in expected_receipts)
        )
        with self._state.locked():
            document = self._read_for_update()
            record = document["tokens"].get(key)
            if record is None:
                raise ValueError("oauth_token_partition_missing")
            current = self._decode_token_record(record)
            if expected is not None:
                self._require_plugin_partition_receipts_locked(
                    document,
                    partition_key=key,
                    plugin_id=dependent,
                    expected=expected,
                    allow_revocation_pending=(
                        current.revocation_pending_plugin_id == dependent
                    ),
                    allow_completed_revocation=(
                        current.revocation_pending_plugin_id is None
                        and dependent not in current.dependent_plugin_ids
                    ),
                )
            if current.revocation_pending_plugin_id is not None:
                if current.revocation_pending_plugin_id != dependent:
                    raise ValueError("oauth_revocation_conflict")
                return current, ()
            if dependent not in current.dependent_plugin_ids:
                return current, current.dependent_plugin_ids
            remaining = tuple(item for item in current.dependent_plugin_ids if item != dependent)
            status = "revoked" if remaining else "revocation_pending"
            self._set_grant_status(
                document,
                partition_key=key,
                plugin_id=dependent,
                status=status,
            )
            changed = replace(
                current,
                dependent_plugin_ids=(remaining or current.dependent_plugin_ids),
                revision=current.revision + 1,
                revocation_pending_plugin_id=(None if remaining else dependent),
                reauthorization_required=(
                    False
                    if not remaining
                    else current.reauthorization_required
                ),
            )
            document["tokens"][key] = {
                "kind": "oauth",
                "value": _encode_partition(changed),
            }
            self._write(document)
            return changed, remaining

    def finalize_revocation(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_revision: int,
        expected_receipts: tuple[OAuthCommittedGrant, ...] | None = None,
    ) -> None:
        key = _sha256_digest(partition_key, reason="oauth_token_partition_invalid")
        dependent = _stable_id(plugin_id, reason="oauth_token_partition_invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("oauth_token_revision_invalid")
        expected = (
            None
            if expected_receipts is None
            else tuple(_grant(item) for item in expected_receipts)
        )
        with self._state.locked():
            document = self._read_for_update()
            record = document["tokens"].get(key)
            if record is None:
                raise ValueError("oauth_token_partition_missing")
            current = self._decode_token_record(record)
            if current.revision != expected_revision:
                raise ValueError("oauth_token_revision_conflict")
            if current.revocation_pending_plugin_id != dependent:
                raise ValueError("oauth_revocation_conflict")
            if expected is not None:
                self._require_plugin_partition_receipts_locked(
                    document,
                    partition_key=key,
                    plugin_id=dependent,
                    expected=expected,
                    allow_revocation_pending=True,
                )
            self._set_grant_status(
                document,
                partition_key=key,
                plugin_id=dependent,
                status="revoked",
            )
            document["tokens"].pop(key)
            document["account_partitions"].pop(key, None)
            self._write(document)

    @staticmethod
    def _require_plugin_partition_receipts_locked(
        document: dict[str, dict[str, Any]],
        *,
        partition_key: str,
        plugin_id: str,
        expected: tuple[OAuthCommittedGrant, ...],
        allow_revocation_pending: bool,
        allow_completed_revocation: bool = False,
    ) -> None:
        if len({item.receipt_id for item in expected}) != len(expected):
            raise ValueError("oauth_grant_receipt_conflict")
        if any(
            item.plugin_id != plugin_id
            or item.token_partition_key != partition_key
            or item.status != "active"
            for item in expected
        ):
            raise ValueError("oauth_grant_receipt_conflict")
        current = tuple(
            grant
            for record in document["committed_grants"].values()
            for grant in (_decode_grant(record),)
            if grant.plugin_id == plugin_id
            and grant.token_partition_key == partition_key
            and grant.status != "revoked"
        )
        if allow_completed_revocation:
            # Shared-partition revocation is committed before the caller can
            # write its local tombstone.  Revoked receipts are deliberately
            # compacted, so an exact retry is represented by no remaining
            # receipt for this plugin on the still-live partition.  Any live
            # replacement receipt keeps this fail-closed.
            if current:
                raise ValueError("oauth_grant_receipt_conflict")
            return
        if any(
            item.claimed_authority_revision
            not in {None, item.authority_revision}
            for item in current
        ):
            raise ValueError("oauth_grant_receipt_conflict")
        allowed_statuses = {"active", "reauthorization_required"}
        if allow_revocation_pending:
            allowed_statuses.add("revocation_pending")
        normalized_current = tuple(
            sorted(
                (
                    replace(item, status="active", claimed_authority_revision=None)
                    for item in current
                    if item.status in allowed_statuses
                ),
                key=lambda item: item.receipt_id,
            )
        )
        normalized_expected = tuple(
            sorted(
                (
                    replace(item, status="active", claimed_authority_revision=None)
                    for item in expected
                ),
                key=lambda item: item.receipt_id,
            )
        )
        if len(normalized_current) != len(current) or normalized_current != normalized_expected:
            raise ValueError("oauth_grant_receipt_conflict")

    def require_plugin_partition_receipts_exact(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_receipts: tuple[OAuthCommittedGrant, ...],
    ) -> None:
        key = _sha256_digest(partition_key, reason="oauth_token_partition_invalid")
        dependent = _stable_id(plugin_id, reason="oauth_token_partition_invalid")
        expected = tuple(_grant(item) for item in expected_receipts)
        with self._state.locked():
            document = self._read()
            if not expected:
                record = document["tokens"].get(key)
                if record is None:
                    raise ValueError("oauth_grant_receipt_conflict")
                partition = self._decode_token_record(record)
                if (
                    dependent in partition.dependent_plugin_ids
                    or partition.revocation_pending_plugin_id == dependent
                ):
                    raise ValueError("oauth_grant_receipt_conflict")
            self._require_plugin_partition_receipts_locked(
                document,
                partition_key=key,
                plugin_id=dependent,
                expected=expected,
                allow_revocation_pending=False,
            )

    def require_plugin_partition_retirement_exact(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_remaining_plugin_ids: tuple[str, ...],
    ) -> None:
        """Prove one completed revocation's exact terminal partition state."""

        key = _sha256_digest(partition_key, reason="oauth_token_partition_invalid")
        dependent = _stable_id(plugin_id, reason="oauth_token_partition_invalid")
        if type(expected_remaining_plugin_ids) is not tuple:
            raise ValueError("oauth_grant_receipt_conflict")
        remaining = tuple(
            _stable_id(item, reason="oauth_token_partition_invalid")
            for item in expected_remaining_plugin_ids
        )
        if (
            remaining != tuple(sorted(remaining))
            or len(remaining) != len(set(remaining))
            or dependent in remaining
        ):
            raise ValueError("oauth_grant_receipt_conflict")
        with self._state.locked():
            document = self._read()
            self._require_plugin_partition_receipts_locked(
                document,
                partition_key=key,
                plugin_id=dependent,
                expected=(),
                allow_revocation_pending=False,
            )
            record = document["tokens"].get(key)
            if not remaining:
                if record is not None:
                    raise ValueError("oauth_grant_receipt_conflict")
                return
            if record is None:
                raise ValueError("oauth_grant_receipt_conflict")
            partition = self._decode_token_record(record)
            if (
                partition.dependent_plugin_ids != remaining
                or partition.revocation_pending_plugin_id is not None
            ):
                raise ValueError("oauth_grant_receipt_conflict")

    def remove_dependent(
        self,
        partition_key: str,
        *,
        plugin_id: str,
    ) -> tuple[str, ...]:
        _partition, remaining = self.prepare_revocation(
            partition_key,
            plugin_id=plugin_id,
        )
        return remaining

    @staticmethod
    def _sdk_key(storage_key: str) -> str:
        return f"sdk:{_storage_key(storage_key)}"

    def load_sdk_tokens(self, storage_key: str) -> tuple[int, _StoredTokens] | None:
        key = self._sdk_key(storage_key)
        with self._state.locked():
            record = self._read()["tokens"].get(key)
            if record is None:
                return None
            if (
                type(record) is not dict
                or set(record) != {"kind", "revision", "value"}
                or record["kind"] != "sdk"
                or type(record["revision"]) is not int
                or record["revision"] < 0
            ):
                raise ValueError("oauth_vault_unavailable")
            return record["revision"], _decode_stored_tokens(record["value"])

    def store_sdk_tokens(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int:
        return self._cas_put(
            "tokens",
            self._sdk_key(storage_key),
            _stored_tokens(tokens),
            expected_revision=expected_revision,
            kind="sdk",
        )

    def delete_sdk_tokens(self, storage_key: str, *, expected_revision: int) -> bool:
        key = self._sdk_key(storage_key)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("authorization_revision_invalid")
        with self._state.locked():
            document = self._read_for_update()
            record = document["tokens"].get(key)
            if record is None:
                return False
            if (
                type(record) is not dict
                or record.get("kind") != "sdk"
                or type(record.get("revision")) is not int
            ):
                raise ValueError("oauth_vault_unavailable")
            if record["revision"] != expected_revision:
                return False
            document["tokens"].pop(key)
            self._write(document)
            return True

    def load_sdk_client_info(self, storage_key: str) -> _StoredClientInfo | None:
        key = self._sdk_key(storage_key)
        with self._state.locked():
            record = self._read()["clients"].get(key)
            if record is None:
                return None
            if type(record) is not dict or set(record) != {"kind", "value"}:
                raise ValueError("oauth_vault_unavailable")
            if record["kind"] != "sdk":
                raise ValueError("oauth_vault_unavailable")
            return _decode_stored_client(record["value"])

    def store_sdk_client_info(self, storage_key: str, client_info: object) -> None:
        key = self._sdk_key(storage_key)
        value = _stored_client(client_info)
        with self._state.locked():
            document = self._read_for_update()
            document["clients"][key] = {"kind": "sdk", "value": value}
            self._write(document)

    def claim_sdk_authorization_code(self, storage_key: str, code_sha256: str) -> bool:
        key = self._sdk_key(storage_key)
        digest = _sha256_digest(code_sha256, reason="authorization_code_invalid")
        with self._state.locked():
            document = self._read_for_update()
            claims = document["claims"].setdefault(key, {})
            if type(claims) is not dict:
                raise ValueError("oauth_vault_unavailable")
            if digest in claims:
                return False
            claims[digest] = self._clock_value()
            self._write(document)
            return True


__all__ = [
    "OAuthClientCredential",
    "OAuthClientRegistration",
    "OAuthCommittedGrant",
    "OAuthCredentialVault",
    "OAuthPendingTransaction",
    "OAuthPreparedToken",
    "OAuthTokenPartition",
]
