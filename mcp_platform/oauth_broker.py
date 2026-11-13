"""Durable provider-neutral OAuth composition boundary."""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
import secrets
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from mcp_platform.authorization import McpBrokerAuthorizationConfig
    from mcp_platform.plugin_execution_worker import (
        PluginExecutionAuthority,
        PluginExecutionWorker,
    )

from mcp_platform.authorization import parse_exact_callback
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client_contracts import McpClientError
from mcp_platform.oauth_profiles import (
    callback_landing_uri,
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthProfile,
    OAuthProfileMatchInput,
    OAuthProfileRegistry,
)
from mcp_platform.oauth_protocol import (
    OAuthAuthorizationParameters,
    OAuthDeviceAuthorization,
    OAuthDiscovery,
    HostedOAuthBridge,
    OAuthNetworkError,
    OAuthProviderError,
    OAuthProtocolClient,
    OAuthProtocolError,
    OAuthRevocationReceipt,
    OAuthTokenResponse,
    _requires_client_authentication,
)
from mcp_platform.oauth_vault import (
    OAuthClientCredential,
    OAuthClientRegistration,
    OAuthCommittedGrant,
    OAuthCredentialVault,
    OAuthGrantReceiptClaimOutcome,
    OAuthPendingTransaction,
    OAuthPreparedToken,
    OAuthTokenPartition,
)


_LOWER_HEX = frozenset("0123456789abcdef")
_SCOPE = re.compile(r"[A-Za-z0-9._~:/-]+\Z")
_CALLBACK_MAX_BYTES = 16_384
_CALLBACK_MAX_PARAMETERS = 16
#: How long one pending authorization attempt stays valid. Five minutes assumed
#: the operator only has to approve a consent screen; Grafana first asks which
#: instance to connect, which means finding a URL, and the attempt expired
#: underneath a sign-in that was still in progress (live 2026-09-07,
#: `oauth_transaction_expired`). Fifteen minutes is the ordinary lifetime for
#: OAuth state and costs nothing: the attempt is bound by PKCE, and the
#: provider's own authorization code expires far sooner than this does.
_ATTEMPT_TTL_SECONDS = 900.0
_AUTHORIZATION_CONTEXT_ATTEMPT_KEY = "omega_authorization_context_revision"
_LOGGER = logging.getLogger(__name__)


def _stable_text(value: object, *, reason: str, maximum: int = 2048) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(reason)
    return value


def _digest(value: object, *, reason: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        raise ValueError(reason)
    return value


def _clock_value(clock: Callable[[], float]) -> float:
    try:
        value = clock()
    except Exception as exc:
        raise ValueError("oauth_clock_invalid") from exc
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("oauth_clock_invalid")
    return float(value)


def _checked_scopes(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value or len(value) > 128:
        raise ValueError("oauth_scope_invalid")
    scopes = tuple(_stable_text(item, reason="oauth_scope_invalid", maximum=256) for item in value)
    if len(scopes) != len(set(scopes)) or any(_SCOPE.fullmatch(item) is None for item in scopes):
        raise ValueError("oauth_scope_invalid")
    return scopes


def _pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _authorization_request(url: str) -> tuple[object, ...]:
    """An authorization URL as the request it makes: parameter order and scope order
    carry no meaning; a repeated parameter, userinfo or fragment is never equal."""
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    return (
        parts.scheme,
        parts.netloc,
        parts.path,
        parts.fragment,
        tuple(sorted(
            (key, tuple(sorted(" ".join(values).split())) if key == "scope" else tuple(values))
            for key, values in query.items()
        )),
    )


def _token_is_fresh(token: OAuthPreparedToken, now: float) -> bool:
    return token.expires_at is None or token.expires_at > now


def _prepared_token(token: OAuthTokenResponse) -> OAuthPreparedToken:
    return OAuthPreparedToken(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        token_type=token.token_type,
        scopes=token.scopes,
        expires_at=token.expires_at,
    )


def _oauth_profile_document(profile: OAuthProfile) -> dict[str, object]:
    """Serialize one already-verified profile for an authenticated worker frame."""

    document: dict[str, object] = {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "catalog_signer": profile.catalog_signer,
        "allowed_plugin_publishers": list(profile.allowed_plugin_publishers),
        "app_publisher": profile.app_publisher,
        "provider_id": profile.provider_id,
        "authorization_issuer": profile.authorization_issuer,
        "resource_origins": list(profile.resource_origins),
        "client_mode": profile.client_mode.value,
        "authorization_flow": profile.authorization_flow.value,
        "account_partition": profile.account_partition,
        "revision": profile.revision,
    }
    if profile.client_id is not None:
        document["client_id"] = profile.client_id
    if profile.client_secret_ref is not None:
        document["client_secret_ref"] = profile.client_secret_ref
    if profile.client_metadata_document_url is not None:
        document["client_metadata_document_url"] = profile.client_metadata_document_url
    if profile.registration is not None:
        document["registration"] = profile.registration.to_dict()
    if profile.redirect_uri is not None:
        document["redirect_uri"] = profile.redirect_uri
    return document


_OAUTH_REFRESH_REQUEST_FIELDS = frozenset(
    {
        "plugin_id",
        "app_id",
        "account_key_sha256",
        "partition_key",
        "expected_revision",
        "profile_id",
        "profile_revision",
        "resource_origin",
        "client_id",
        "required_scopes",
        "connection_revision",
        "capability_revision",
        "authority_revision",
        "capability_binding_revision",
    }
)
_OAUTH_REFRESH_RESULT_FIELDS = frozenset(
    {
        "operation",
        "request_sha256",
        "partition_key",
        "expected_revision",
        "token",
    }
)
_OAUTH_REFRESH_FAILURE_FIELDS = frozenset(
    {
        "operation",
        "request_sha256",
        "partition_key",
        "expected_revision",
        "error_kind",
        "reason",
    }
)
_OAUTH_REFRESH_NETWORK_REASONS = frozenset(
    {"oauth_provider_timeout", "oauth_provider_unreachable"}
)


def _oauth_refresh_request_sha256(request: Mapping[str, object]) -> str:
    return sha256_hex(canonical_bytes(dict(request)))


def _checked_oauth_refresh_request(
    body: object,
    credential: object,
) -> tuple[
    OAuthProfile,
    dict[str, object],
    str,
    str | None,
]:
    """Rebind a refresh frame to its exact worker and token-partition authority."""

    from mcp_platform.plugin_execution_worker import PluginExecutionAuthority

    try:
        if (
            type(body) is not dict
            or set(body) != {"operation", "profile", "request", "worker_authority"}
            or body["operation"] != "oauth_refresh"
            or type(body["profile"]) is not dict
            or type(body["request"]) is not dict
            or set(body["request"]) != _OAUTH_REFRESH_REQUEST_FIELDS
            or type(credential) is not dict
            or set(credential) != {"refresh_token", "client_secret"}
        ):
            raise ValueError
        request = dict(body["request"])
        authority = PluginExecutionAuthority.from_dict(body["worker_authority"])
        profile_document = dict(body["profile"])
        catalog_signer = _stable_text(
            profile_document.get("catalog_signer"),
            reason="oauth_worker_refresh_invalid",
        )
        profile = OAuthProfile.from_verified_dict(
            profile_document,
            catalog_signer=catalog_signer,
        )
        plugin_id = _stable_text(
            request["plugin_id"], reason="oauth_worker_refresh_invalid"
        )
        app_id = _stable_text(request["app_id"], reason="oauth_worker_refresh_invalid")
        account_key_sha256 = _digest(
            request["account_key_sha256"], reason="oauth_worker_refresh_invalid"
        )
        partition_key = _digest(
            request["partition_key"], reason="oauth_worker_refresh_invalid"
        )
        for field_name in (
            "connection_revision",
            "capability_revision",
            "authority_revision",
            "capability_binding_revision",
        ):
            _digest(request[field_name], reason="oauth_worker_refresh_invalid")
        expected_revision = request["expected_revision"]
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError
        scopes_value = request["required_scopes"]
        if type(scopes_value) is not list:
            raise ValueError
        scopes = _checked_scopes(tuple(scopes_value))
        client_id = _stable_text(
            request["client_id"], reason="oauth_worker_refresh_invalid"
        )
        refresh_token = _stable_text(
            credential["refresh_token"],
            reason="oauth_worker_refresh_invalid",
            maximum=16_384,
        )
        client_secret_value = credential["client_secret"]
        client_secret = (
            None
            if client_secret_value is None
            else _stable_text(
                client_secret_value,
                reason="oauth_worker_refresh_invalid",
                maximum=1024,
            )
        )
        dynamic_client = profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
        expected_client = (
            client_id
            if dynamic_client
            else profile.client_id or profile.client_metadata_document_url
        )
        expected_partition = _partition_digest(
            account_partition=profile.account_partition,
            authorization_issuer=profile.authorization_issuer,
            resource_origin=request["resource_origin"],
            account_key_sha256=account_key_sha256,
            credential_identity=_credential_identity(profile, client_id),
        )
        if (
            authority.plugin_id != plugin_id
            or authority.app_id != app_id
            or authority.account_key_sha256 != account_key_sha256
            or authority.connection_revision != request["connection_revision"]
            or authority.capability_revision != request["capability_revision"]
            or authority.authority_revision != request["authority_revision"]
            or request["profile_id"] != profile.profile_id
            or request["profile_revision"] != profile.revision
            or request["resource_origin"] not in profile.resource_origins
            or client_id != expected_client
            or partition_key != expected_partition
            or (profile.client_secret_ref is None) != (client_secret is None)
            or dynamic_client and client_secret is not None
            or tuple(scopes_value) != scopes
        ):
            raise ValueError
        request["required_scopes"] = list(scopes)
        return profile, request, refresh_token, client_secret
    except (KeyError, TypeError, ValueError):
        raise ValueError("oauth_worker_refresh_invalid") from None


def _oauth_refresh_failure(
    request: Mapping[str, object],
    *,
    error_kind: str,
    reason: str,
) -> dict[str, object]:
    return {
        "operation": "oauth_refresh",
        "request_sha256": _oauth_refresh_request_sha256(request),
        "partition_key": request["partition_key"],
        "expected_revision": request["expected_revision"],
        "error_kind": error_kind,
        "reason": reason,
    }


def _execute_oauth_refresh_operation(
    body: dict[str, object],
    credential: dict[str, object],
) -> dict[str, object]:
    """Child-only OAuth refresh handler for the shared execution dispatcher."""

    profile, request, refresh_token, client_secret = _checked_oauth_refresh_request(
        body,
        credential,
    )
    protocol = OAuthProtocolClient()

    async def run() -> dict[str, object]:
        try:
            discovery = await protocol.discover(
                profile,
                resource_origin=str(request["resource_origin"]),
            )
            OAuthBroker._validate_discovery(
                profile,
                discovery,
                str(request["resource_origin"]),
            )
            token = await protocol.refresh(
                discovery,
                str(request["client_id"]),
                refresh_token,
                client_secret=client_secret,
                required_scopes=tuple(request["required_scopes"]),
            )
            return {
                "operation": "oauth_refresh",
                "request_sha256": _oauth_refresh_request_sha256(request),
                "partition_key": request["partition_key"],
                "expected_revision": request["expected_revision"],
                "token": {
                    "access_token": token.access_token,
                    "refresh_token": token.refresh_token,
                    "token_type": token.token_type,
                    "scopes": list(token.scopes),
                    "expires_at": token.expires_at,
                },
            }
        except OAuthNetworkError as exc:
            reason = (
                exc.reason
                if exc.reason in _OAUTH_REFRESH_NETWORK_REASONS
                else "oauth_provider_unreachable"
            )
            return _oauth_refresh_failure(
                request,
                error_kind="network",
                reason=reason,
            )
        except OAuthProtocolError as exc:
            reason = (
                exc.reason
                if isinstance(exc, OAuthProviderError)
                and exc.reason == "oauth_client_invalid"
                else "oauth_provider_rejected"
                if isinstance(exc, OAuthProviderError)
                else "oauth_refresh_invalid"
            )
            return _oauth_refresh_failure(
                request,
                error_kind="protocol",
                reason=reason,
            )
        finally:
            await protocol.aclose()

    return asyncio.run(run())


def _partition_digest(
    *,
    account_partition: str,
    authorization_issuer: str,
    resource_origin: str,
    account_key_sha256: str,
    credential_identity: str,
) -> str:
    material = {
        "account_key_sha256": account_key_sha256,
        "account_partition": account_partition,
        "authorization_issuer": authorization_issuer,
        "credential_identity": credential_identity,
    }
    if account_partition == "issuer_resource_account":
        material["resource_origin"] = resource_origin
    elif account_partition != "issuer_account":
        raise ValueError("oauth_profile_account_partition_invalid")
    return sha256_hex(canonical_bytes(material))


def _credential_identity(profile: OAuthProfile, client_id: str) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "client_id": client_id,
                "client_metadata_document_url": profile.client_metadata_document_url,
                "client_mode": profile.client_mode.value,
                "client_secret_ref": profile.client_secret_ref,
            }
        )
    )


def _attempt_key(plugin_id: str, app_id: str, account_key_sha256: str) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": plugin_id,
                "app_id": app_id,
                "account_key_sha256": account_key_sha256,
            }
        )
    )


def _legacy_revocation_partition_digest(partition: OAuthTokenPartition) -> str:
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


def _attempt_context(value: object) -> Mapping[str, object]:
    from mcp_platform.oauth_vault import _attempt_context as checked_attempt_context

    return MappingProxyType(dict(checked_attempt_context(value)))


@dataclass(frozen=True, slots=True)
class OAuthConnectionRequest:
    profile_match: OAuthProfileMatchInput
    plugin_id: str
    app_id: str
    account_key_sha256: str
    scopes: tuple[str, ...]
    authorization_context_revision: str
    expected_authorization_flow: OAuthAuthorizationFlow
    attempt_context: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, repr=False)
class OAuthAuthorizationStart:
    profile_id: str
    plugin_id: str
    authorization_url: str | None
    redirect_uri: str | None
    expires_at: float
    transaction_status: str = "authorizing"
    state_sha256: str = ""
    authorization_flow: OAuthAuthorizationFlow = OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
    user_code: str | None = None
    verification_uri: str | None = None
    poll_interval_seconds: int | None = None

    def __repr__(self) -> str:
        return (
            "OAuthAuthorizationStart("
            f"profile_id={self.profile_id!r}, plugin_id={self.plugin_id!r}, "
            f"expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OAuthAuthorizationAttempt:
    profile_id: str
    plugin_id: str
    app_id: str
    account_key_sha256: str
    state_sha256: str
    authorization_context_revision: str
    attempt_context: Mapping[str, object]

    def __repr__(self) -> str:
        return (
            "OAuthAuthorizationAttempt("
            f"profile_id={self.profile_id!r}, plugin_id={self.plugin_id!r}, "
            f"state_sha256={self.state_sha256!r})"
        )

    @classmethod
    def from_transaction(cls, transaction: OAuthPendingTransaction) -> OAuthAuthorizationAttempt:
        return cls(
            profile_id=transaction.profile_id,
            plugin_id=transaction.plugin_id,
            app_id=transaction.app_id,
            account_key_sha256=transaction.account_key_sha256,
            state_sha256=transaction.state_sha256,
            authorization_context_revision=transaction.authorization_context_revision,
            attempt_context=transaction.attempt_context,
        )


@dataclass(frozen=True, slots=True, repr=False)
class OAuthPreparedAuthorization:
    profile_id: str
    plugin_id: str
    app_id: str
    account_key_sha256: str
    state_sha256: str
    partition_key: str
    profile_revision: str
    authorization_context_revision: str
    scopes: tuple[str, ...]
    attempt_context: Mapping[str, object] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "OAuthPreparedAuthorization("
            f"profile_id={self.profile_id!r}, plugin_id={self.plugin_id!r}, "
            f"state_sha256={self.state_sha256!r})"
        )

    @classmethod
    def from_transaction(cls, transaction: OAuthPendingTransaction) -> OAuthPreparedAuthorization:
        if transaction.prepared_token is None:
            raise ValueError("oauth_token_not_prepared")
        return cls(
            profile_id=transaction.profile_id,
            plugin_id=transaction.plugin_id,
            app_id=transaction.app_id,
            account_key_sha256=transaction.account_key_sha256,
            state_sha256=transaction.state_sha256,
            partition_key=transaction.partition_key,
            profile_revision=transaction.profile_revision,
            authorization_context_revision=transaction.authorization_context_revision,
            scopes=transaction.requested_scopes or transaction.scopes,
            attempt_context=transaction.attempt_context,
        )


@dataclass(frozen=True, slots=True)
class OAuthConnectionCommit:
    connection_revision: str
    capability_revision: str
    authority_revision: str
    capability_binding_revision: str
    execution_policy: str
    connection_generation: int = 0


@dataclass(frozen=True, slots=True)
class BrokerTransportBinding:
    plugin_id: str
    app_id: str
    profile_id: str
    profile_revision: str
    partition_key: str
    resource_url: str
    connection_revision: str
    capability_revision: str
    authority_revision: str
    capability_binding_revision: str


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class BrokerTransportAuthority:
    """Sealed host capability that resolves one current transport binding."""

    def __new__(cls) -> BrokerTransportAuthority:
        del cls
        raise ValueError("oauth_transport_authority_invalid")

    def __repr__(self) -> str:
        return "BrokerTransportAuthority(<sealed>)"

    def __copy__(self) -> BrokerTransportAuthority:
        del self
        raise ValueError("oauth_transport_authority_invalid")

    def __deepcopy__(self, memo: dict[int, object]) -> BrokerTransportAuthority:
        del self, memo
        raise ValueError("oauth_transport_authority_invalid")

    def __reduce__(self) -> object:
        raise ValueError("oauth_transport_authority_invalid")


@dataclass(frozen=True, slots=True)
class OAuthRevocationResult:
    revoked_provider_token: bool
    dependents: tuple[str, ...]
    local_token_deleted: bool


@dataclass(frozen=True, slots=True)
class _CallbackQuery:
    callback_uri: str
    state: str
    code: str | None
    error: str | None
    issuer: str | None
    completion_handle: str | None


def _bounded_callback_query(callback_uri: object) -> _CallbackQuery:
    try:
        uri = _stable_text(
            callback_uri,
            reason="oauth_callback_invalid",
            maximum=_CALLBACK_MAX_BYTES,
        )
        parsed = urlsplit(uri)
        if parsed.fragment:
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if len(query) > _CALLBACK_MAX_PARAMETERS or any(
            len(values) != 1 for values in query.values()
        ):
            raise ValueError
        state = query.get("state", [""])[0]
        code = query.get("code", [None])[0]
        error = query.get("error", [None])[0]
        issuer = query.get("iss", [None])[0]
        completion_handle = query.get("completion_handle", [None])[0]
        if (
            not state
            or len(state.encode("utf-8")) > 1024
            or sum(value is not None for value in (code, error, completion_handle)) != 1
            or code is not None
            and (not code or len(code.encode("utf-8")) > 4096)
            or error is not None
            and (not error or len(error.encode("utf-8")) > 256)
            or completion_handle is not None
            and (not completion_handle or len(completion_handle.encode("utf-8")) > 1024)
        ):
            raise ValueError
        return _CallbackQuery(uri, state, code, error, issuer, completion_handle)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("oauth_callback_invalid") from None


def _callback_state_digest(callback: _CallbackQuery) -> str:
    if callback.completion_handle is not None:
        return _digest(callback.state, reason="oauth_callback_invalid")
    return sha256_hex(callback.state.encode("utf-8"))


class OAuthProfileUnavailable(ValueError):
    """The exact signed client profile has no usable local credential path."""

    def __init__(self) -> None:
        super().__init__("oauth_profile_unavailable")


class OAuthBroker:
    """The sole composition layer for one exact signed OAuth authorization."""

    def __init__(
        self,
        *,
        registry: OAuthProfileRegistry,
        vault: OAuthCredentialVault,
        protocol: OAuthProtocolClient,
        hosted_bridge: HostedOAuthBridge | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(clock):
            raise ValueError("oauth_broker_configuration_invalid")
        self._registry = registry
        self._vault = vault
        self._protocol = protocol
        self._hosted_bridge = hosted_bridge
        self._clock = clock
        self._completion_locks: dict[str, asyncio.Lock] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._registration_locks: dict[str, asyncio.Lock] = {}
        _register_oauth_broker(self)

    def __repr__(self) -> str:
        return "OAuthBroker(<sealed>)"

    @staticmethod
    def callback_state_sha256(callback_uri: str) -> str:
        callback = _bounded_callback_query(callback_uri)
        return _callback_state_digest(callback)

    def authorization_attempt(self, state_sha256: str) -> OAuthAuthorizationAttempt:
        state = _digest(state_sha256, reason="oauth_transaction_invalid")
        return OAuthAuthorizationAttempt.from_transaction(self._vault.load_transaction(state))

    def candidate_attempt_state_sha256(
        self,
        candidate_identity: object,
    ) -> tuple[str, ...]:
        return self._vault.candidate_attempt_state_sha256(candidate_identity)

    def __copy__(self) -> OAuthBroker:
        del self
        raise ValueError("oauth_broker_invalid")

    def __deepcopy__(self, memo: dict[int, object]) -> OAuthBroker:
        del self, memo
        raise ValueError("oauth_broker_invalid")

    def __reduce__(self) -> object:
        raise ValueError("oauth_broker_invalid")

    @staticmethod
    def _lock(locks: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    def _credential_for(self, profile: OAuthProfile) -> OAuthClientCredential | None:
        if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
            raise OAuthProfileUnavailable()
        if profile.client_mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT:
            if (
                profile.client_id is not None
                or profile.client_secret_ref is not None
                or profile.client_metadata_document_url is None
            ):
                raise OAuthProfileUnavailable()
            return None
        if profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
            if (
                self._hosted_bridge is None
                or profile.client_id is None
                or profile.client_secret_ref is not None
            ):
                raise OAuthProfileUnavailable()
            return OAuthClientCredential(profile.client_id, None)
        if profile.client_mode is not OAuthClientMode.PREREGISTERED or profile.client_id is None:
            raise OAuthProfileUnavailable()
        if profile.client_secret_ref is None:
            return OAuthClientCredential(profile.client_id, None)
        if profile.client_secret_ref != f"oauth-client/{profile.profile_id}/secret":
            raise OAuthProfileUnavailable()
        loaded = self._vault.load_credential(profile.client_secret_ref)
        if loaded is None:
            raise OAuthProfileUnavailable()
        _revision_number, credential = loaded
        if credential.client_id != profile.client_id or credential.client_secret is None:
            raise OAuthProfileUnavailable()
        return credential

    @staticmethod
    def _registration_lock_key(profile: OAuthProfile) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "authorization_issuer": profile.authorization_issuer,
                    "profile_id": profile.profile_id,
                }
            )
        )

    def _stored_dynamic_credential(self, profile: OAuthProfile) -> OAuthClientCredential:
        if profile.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION:
            raise ValueError("oauth_profile_unavailable")
        registration = self._vault.load_registration(
            profile.authorization_issuer,
            profile.profile_id,
        )
        if registration is None:
            raise ValueError("oauth_profile_unavailable")
        return OAuthClientCredential(
            registration.client_id,
            registration.client_secret,
        )

    def _registration_secret_is_live(self, registration: object) -> bool:
        """Has a stated secret expiry passed?

        A confidential client whose secret has expired cannot exchange anything,
        and nothing else in the flow would notice before the token endpoint
        refused it. `None` means the server issued no secret, or one that does
        not expire -- both are live.
        """
        expires_at = getattr(registration, "client_secret_expires_at", None)
        return expires_at is None or expires_at > _clock_value(self._clock)

    async def _resolve_dynamic_registration(
        self,
        profile: OAuthProfile,
        discovery: OAuthDiscovery,
    ) -> OAuthClientCredential:
        if profile.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION:
            raise ValueError("oauth_profile_unavailable")
        key = self._registration_lock_key(profile)
        async with self._lock(self._registration_locks, key):
            existing = self._vault.load_registration(
                profile.authorization_issuer,
                profile.profile_id,
            )
            # A registration stored before Omega could keep a secret has none,
            # and reusing it would send an unauthenticated exchange forever to a
            # server that requires client authentication. Registering again is
            # the only way out, and it is the same path a rejected client_id
            # already takes.
            # Worth one attempt: a registration stored before Omega could keep a
            # secret has none, and the server may issue one now. Not worth a
            # second -- `secret_upgrade_attempted` records that this exact
            # registration is already the server's answer, so a server that
            # demands client authentication and issues nothing (Miro does both)
            # is not re-registered on every sign-in forever.
            secret_missing = (
                existing is not None
                and existing.client_secret is None
                and not existing.secret_upgrade_attempted
                and _requires_client_authentication(discovery)
            )
            if (
                existing is not None
                and not secret_missing
                and self._registration_secret_is_live(existing)
            ):
                return OAuthClientCredential(
                    existing.client_id,
                    existing.client_secret,
                )
            if existing is not None:
                self._vault.delete_registration(
                    profile.authorization_issuer,
                    profile.profile_id,
                )
            response = await self._protocol.register(discovery, profile=profile)
            self._vault.store_registration(
                OAuthClientRegistration(
                    authorization_issuer=profile.authorization_issuer,
                    profile_id=profile.profile_id,
                    client_id=response.client_id,
                    registration_access_token=response.registration_access_token,
                    registration_client_uri=response.registration_client_uri,
                    registered_at=_clock_value(self._clock),
                    # The secret itself is deliberately absent from the digest:
                    # this identifies WHICH registration was stored, and a
                    # digest is not where a credential belongs.
                    response_sha256=sha256_hex(
                        canonical_bytes(
                            {
                                "client_id": response.client_id,
                                "registration_access_token": (
                                    response.registration_access_token
                                ),
                                "registration_client_uri": response.registration_client_uri,
                            }
                        )
                    ),
                    client_secret=response.client_secret,
                    client_secret_expires_at=response.client_secret_expires_at,
                    # Asked and answered: this server says it needs client
                    # authentication and issued nothing anyway (Miro does
                    # exactly that), so nothing asks again. A server that never
                    # required one was never asked, and stays open to the
                    # upgrade if it starts requiring it later.
                    secret_upgrade_attempted=(
                        response.client_secret is None
                        and _requires_client_authentication(discovery)
                    ),
                )
            )
            return OAuthClientCredential(response.client_id, response.client_secret)

    async def _replace_dynamic_registration(
        self,
        profile: OAuthProfile,
        discovery: OAuthDiscovery,
        *,
        rejected_client_id: str,
    ) -> None:
        key = self._registration_lock_key(profile)
        async with self._lock(self._registration_locks, key):
            current = self._vault.load_registration(
                profile.authorization_issuer,
                profile.profile_id,
            )
            if current is not None and current.client_id != rejected_client_id:
                return
            self._vault.delete_registration(
                profile.authorization_issuer,
                profile.profile_id,
            )
            try:
                response = await self._protocol.register(discovery, profile=profile)
            except OAuthProtocolError:
                raise ValueError("oauth_client_registration_rejected") from None
            self._vault.store_registration(
                OAuthClientRegistration(
                    authorization_issuer=profile.authorization_issuer,
                    profile_id=profile.profile_id,
                    client_id=response.client_id,
                    registration_access_token=response.registration_access_token,
                    registration_client_uri=response.registration_client_uri,
                    registered_at=_clock_value(self._clock),
                    response_sha256=sha256_hex(
                        canonical_bytes(
                            {
                                "client_id": response.client_id,
                                "registration_access_token": (
                                    response.registration_access_token
                                ),
                                "registration_client_uri": response.registration_client_uri,
                            }
                        )
                    ),
                    # A replacement registration issues a new secret too, and
                    # dropping it here would leave a confidential client that
                    # can register but never exchange.
                    client_secret=response.client_secret,
                    client_secret_expires_at=response.client_secret_expires_at,
                )
            )

    async def _invalidate_dynamic_registration(
        self,
        profile: OAuthProfile,
        *,
        rejected_client_id: str,
    ) -> None:
        """Forget only the rejected cached identity; Connect owns replacement."""

        key = self._registration_lock_key(profile)
        async with self._lock(self._registration_locks, key):
            current = self._vault.load_registration(
                profile.authorization_issuer,
                profile.profile_id,
            )
            if current is not None and current.client_id == rejected_client_id:
                self._vault.delete_registration(
                    profile.authorization_issuer,
                    profile.profile_id,
                )

    async def delete_registration(
        self,
        authorization_issuer: str,
        profile_id: str,
    ) -> None:
        issuer = _stable_text(authorization_issuer, reason="oauth_registration_invalid")
        selected_profile = _stable_text(profile_id, reason="oauth_registration_invalid")
        key = sha256_hex(
            canonical_bytes(
                {
                    "authorization_issuer": issuer,
                    "profile_id": selected_profile,
                }
            )
        )
        async with self._lock(self._registration_locks, key):
            registration = self._vault.load_registration(issuer, selected_profile)
            if registration is None or self._vault.registration_has_dependents(
                issuer,
                selected_profile,
            ):
                return
            if (
                registration.registration_client_uri is not None
                and registration.registration_access_token is not None
            ):
                issuer_url = urlsplit(issuer)
                management_url = urlsplit(registration.registration_client_uri)
                if (
                    management_url.scheme != issuer_url.scheme
                    or management_url.netloc != issuer_url.netloc
                ):
                    _LOGGER.warning(
                        "oauth_registration_deletion_failed:%s",
                        "oauth_registration_invalid",
                    )
                else:
                    try:
                        await self._protocol.deregister(
                            registration.registration_client_uri,
                            registration_access_token=(
                                registration.registration_access_token
                            ),
                        )
                    except (OAuthNetworkError, OAuthProviderError) as exc:
                        _LOGGER.warning(
                            "oauth_registration_deletion_failed:%s",
                            exc.reason,
                        )
            self._vault.delete_registration(issuer, selected_profile)

    @staticmethod
    def _profile_route(profile: OAuthProfile) -> None:
        if profile.authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
            if profile.redirect_uri is not None:
                raise ValueError("oauth_profile_unavailable")
            return
        parsed = urlsplit(profile.redirect_uri)
        if (
            parsed.path != f"/oauth/{profile.profile_id}/callback"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("oauth_profile_unavailable")

    @staticmethod
    def _request_values(
        request: OAuthConnectionRequest,
    ) -> tuple[str, str, str, tuple[str, ...], Mapping[str, object]]:
        if (
            type(request) is not OAuthConnectionRequest
            or type(request.profile_match) is not OAuthProfileMatchInput
            or type(request.expected_authorization_flow) is not OAuthAuthorizationFlow
        ):
            raise ValueError("oauth_connection_request_invalid")
        plugin_id = _stable_text(request.plugin_id, reason="oauth_connection_request_invalid")
        app_id = _stable_text(request.app_id, reason="oauth_connection_request_invalid")
        account = _digest(request.account_key_sha256, reason="oauth_connection_request_invalid")
        _digest(
            request.authorization_context_revision,
            reason="oauth_connection_request_invalid",
        )
        return (
            plugin_id,
            app_id,
            account,
            _checked_scopes(request.scopes + ()),
            _attempt_context(request.attempt_context),
        )

    async def begin(self, request: OAuthConnectionRequest) -> OAuthAuthorizationStart:
        plugin_id, app_id, account, requested_scopes, attempt_context = self._request_values(
            request
        )
        if _AUTHORIZATION_CONTEXT_ATTEMPT_KEY in attempt_context:
            raise ValueError("oauth_connection_request_invalid")
        persisted_attempt_context = _attempt_context(
            {
                **attempt_context,
                _AUTHORIZATION_CONTEXT_ATTEMPT_KEY: request.authorization_context_revision,
            }
        )
        normalized_match = request.profile_match.normalized()
        profile = self._registry.resolve(request.profile_match)
        if profile.authorization_flow is not request.expected_authorization_flow:
            raise ValueError("oauth_authorization_flow_mismatch")
        self._profile_route(profile)
        discovery = await self._protocol.discover(
            profile,
            resource_origin=normalized_match.resource_origin,
        )
        self._validate_discovery(profile, discovery, normalized_match.resource_origin)
        credential = (
            await self._resolve_dynamic_registration(profile, discovery)
            if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
            else self._credential_for(profile)
        )
        state = secrets.token_urlsafe(32)
        client_id = (
            profile.client_metadata_document_url if credential is None else credential.client_id
        )
        if client_id is None:
            raise ValueError("oauth_profile_unavailable")
        credential_identity = _credential_identity(profile, client_id)
        partition_key = _partition_digest(
            account_partition=profile.account_partition,
            authorization_issuer=profile.authorization_issuer,
            resource_origin=normalized_match.resource_origin,
            account_key_sha256=account,
            credential_identity=credential_identity,
        )
        scopes = self._vault.scope_union(
            partition_key,
            requested_scopes,
            replacing_plugin_id=plugin_id,
        )
        now = _clock_value(self._clock)
        verifier: str | None
        device: OAuthDeviceAuthorization | None
        if profile.authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE:
            verifier = secrets.token_urlsafe(64)
            challenge = _pkce_challenge(verifier)
            if profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
                if self._hosted_bridge is None:
                    raise ValueError("oauth_profile_unavailable")
                state_digest = sha256_hex(state.encode("utf-8"))
                authorization_url = await self._hosted_bridge.begin(
                    profile.profile_id, state_digest, challenge, scopes
                )
                # The bridge only relays a request the unit could build itself: the
                # discovered endpoint, the signed client and redirect, this attempt's
                # state and challenge, and exactly the consented scopes. Anything else
                # (another origin, wider scopes, an extra parameter) is refused, so a
                # compromised bridge cannot send the operator elsewhere.
                expected_url = self._protocol.build_authorization_url(
                    discovery,
                    OAuthAuthorizationParameters.for_profile(
                        profile,
                        credential=credential,
                        scopes=scopes,
                        state=state_digest,
                        code_challenge=challenge,
                        resource_origin=normalized_match.resource_origin,
                    ),
                )
                if _authorization_request(authorization_url) != _authorization_request(
                    expected_url
                ):
                    raise ValueError("oauth_profile_unavailable")
            else:
                parameters = OAuthAuthorizationParameters.for_profile(
                    profile,
                    credential=credential,
                    scopes=scopes,
                    state=state,
                    code_challenge=challenge,
                    resource_origin=normalized_match.resource_origin,
                )
                authorization_url = self._protocol.build_authorization_url(
                    discovery,
                    parameters,
                )
            expires_at = now + _ATTEMPT_TTL_SECONDS
            device = None
        else:
            verifier = None
            device = await self._protocol.begin_device_authorization(
                discovery,
                client_id=client_id,
                scopes=scopes,
            )
            authorization_url = device.verification_uri_complete or device.verification_uri
            expires_at = device.expires_at
        state_sha256 = sha256_hex(state.encode("utf-8"))
        attempt_key = _attempt_key(plugin_id, app_id, account)
        transaction = OAuthPendingTransaction(
            profile_id=profile.profile_id,
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256=account,
            partition_key=partition_key,
            authorization_issuer=profile.authorization_issuer,
            resource_origin=normalized_match.resource_origin,
            client_id=client_id,
            state_sha256=state_sha256,
            pkce_verifier=verifier,
            redirect_uri=profile.redirect_uri,
            scopes=scopes,
            authorization_context_revision=request.authorization_context_revision,
            profile_revision=profile.revision,
            issued_at=now,
            expires_at=expires_at,
            attempt_revision=self._vault.next_attempt_revision(attempt_key),
            attempt_key=attempt_key,
            requested_scopes=requested_scopes,
            account_partition=profile.account_partition,
            credential_identity=credential_identity,
            attempt_context=persisted_attempt_context,
            authorization_flow=profile.authorization_flow.value,
            device_code=None if device is None else device.device_code,
            device_interval_seconds=(None if device is None else device.interval_seconds),
            device_next_poll_at=None if device is None else now,
        )
        self._vault.begin_transaction(transaction)
        return OAuthAuthorizationStart(
            profile_id=profile.profile_id,
            plugin_id=plugin_id,
            authorization_url=authorization_url,
            redirect_uri=profile.redirect_uri,
            expires_at=expires_at,
            state_sha256=state_sha256,
            authorization_flow=profile.authorization_flow,
            user_code=None if device is None else device.user_code,
            verification_uri=None if device is None else device.verification_uri,
            poll_interval_seconds=(None if device is None else device.interval_seconds),
        )

    @staticmethod
    def _validate_discovery(
        profile: OAuthProfile,
        discovery: OAuthDiscovery,
        resource_origin: str,
    ) -> None:
        expected_client = (
            None
            if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
            else profile.client_id or profile.client_metadata_document_url
        )
        if (
            discovery.profile_id != profile.profile_id
            or discovery.profile_revision != profile.revision
            or discovery.client_mode is not profile.client_mode
            or discovery.client_identifier != expected_client
            or discovery.client_secret_advertised != (profile.client_secret_ref is not None)
            or discovery.redirect_uri != profile.redirect_uri
            or discovery.issuer != profile.authorization_issuer
            or discovery.resource_origin != resource_origin
            or discovery.authorization_flow is not profile.authorization_flow
        ):
            raise ValueError("oauth_discovery_binding_invalid")
        if profile.authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE:
            valid_flow = (
                profile.redirect_uri is not None
                and discovery.redirect_uri == profile.redirect_uri
                and discovery.authorization_endpoint is not None
                and "S256" in discovery.code_challenge_methods_supported
            )
        else:
            valid_flow = (
                profile.redirect_uri is None
                and discovery.redirect_uri is None
                and discovery.device_authorization_endpoint is not None
                and "urn:ietf:params:oauth:grant-type:device_code"
                in discovery.grant_types_supported
            )
        if not valid_flow:
            raise ValueError("oauth_discovery_binding_invalid")

    def _validate_transaction(
        self,
        profile: OAuthProfile,
        transaction: OAuthPendingTransaction,
    ) -> OAuthClientCredential | None:
        self._profile_route(profile)
        credential = (
            self._stored_dynamic_credential(profile)
            if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
            else self._credential_for(profile)
        )
        client_id = (
            profile.client_metadata_document_url if credential is None else credential.client_id
        )
        if client_id is None:
            raise ValueError("oauth_transaction_binding_drift")
        credential_identity = _credential_identity(profile, client_id)
        if transaction.attempt_context.get("omega_migration_status") is not None:
            raise ValueError("oauth_reauthorization_required")
        expected_partition = _partition_digest(
            account_partition=transaction.account_partition,
            authorization_issuer=transaction.authorization_issuer,
            resource_origin=transaction.resource_origin,
            account_key_sha256=transaction.account_key_sha256,
            credential_identity=transaction.credential_identity or "",
        )
        if (
            transaction.profile_id != profile.profile_id
            or transaction.profile_revision != profile.revision
            or transaction.authorization_flow != profile.authorization_flow.value
            or transaction.authorization_issuer != profile.authorization_issuer
            or transaction.resource_origin not in profile.resource_origins
            or transaction.redirect_uri != profile.redirect_uri
            or transaction.client_id != client_id
            or transaction.credential_identity != credential_identity
            or transaction.account_partition != profile.account_partition
            or transaction.partition_key != expected_partition
            or transaction.attempt_key
            != _attempt_key(
                transaction.plugin_id,
                transaction.app_id,
                transaction.account_key_sha256,
            )
            or transaction.attempt_context.get(_AUTHORIZATION_CONTEXT_ATTEMPT_KEY)
            != transaction.authorization_context_revision
        ):
            raise ValueError("oauth_transaction_binding_drift")
        return credential

    async def complete_device(self, request_id: str) -> OAuthPreparedAuthorization:
        """Poll one durable Device Flow attempt without exposing its device code."""

        state_sha256 = _digest(request_id, reason="oauth_transaction_invalid")
        async with self._lock(self._completion_locks, state_sha256):
            transaction = self._load_callback_transaction(state_sha256)
            if transaction.authorization_flow != OAuthAuthorizationFlow.DEVICE_CODE.value:
                raise ValueError("oauth_authorization_flow_mismatch")
            profile = self._registry.get_exact(
                transaction.profile_id,
                transaction.profile_revision,
            )
            credential = self._validate_transaction(profile, transaction)
            if credential is None or credential.client_secret is not None:
                raise ValueError("oauth_profile_unavailable")
            discovery = await self._protocol.discover(
                profile,
                resource_origin=transaction.resource_origin,
            )
            self._validate_discovery(profile, discovery, transaction.resource_origin)
            now = _clock_value(self._clock)
            if (
                transaction.device_code is None
                or transaction.device_interval_seconds is None
                or transaction.device_next_poll_at is None
                or now < transaction.device_next_poll_at
            ):
                raise ValueError("oauth_device_poll_too_soon")
            device = self._protocol.restore_device_authorization(
                discovery,
                client_id=transaction.client_id,
                scopes=transaction.scopes,
                device_code=transaction.device_code,
                expires_at=transaction.expires_at,
                interval_seconds=transaction.device_interval_seconds,
            )
            try:
                token = await self._protocol.poll_device_token(
                    discovery,
                    device,
                    client_id=transaction.client_id,
                    required_scopes=transaction.scopes,
                )
            except OAuthProtocolError as exc:
                if exc.reason in {
                    "oauth_device_authorization_pending",
                    "oauth_device_slow_down",
                }:
                    self._vault.record_device_poll(
                        state_sha256,
                        slow_down=exc.reason == "oauth_device_slow_down",
                    )
                raise
            self._vault.prepare_token(state_sha256, _prepared_token(token))
            return OAuthPreparedAuthorization.from_transaction(
                self._vault.load_transaction(state_sha256)
            )

    @staticmethod
    def _validate_denial_callback(
        callback: _CallbackQuery,
        transaction: OAuthPendingTransaction,
        discovery: OAuthDiscovery,
    ) -> None:
        parsed = urlsplit(callback.callback_uri)
        expected = urlsplit(callback_landing_uri(transaction.redirect_uri))
        issuer_required = discovery.authorization_response_iss_parameter_supported
        if (
            parsed._replace(query="").geturl() != expected.geturl()
            or issuer_required
            and callback.issuer is None
            or callback.issuer is not None
            and callback.issuer != transaction.authorization_issuer
        ):
            raise ValueError("oauth_callback_invalid")

    def _load_callback_transaction(self, state_sha256: str) -> OAuthPendingTransaction:
        try:
            return self._vault.load_transaction(state_sha256)
        except ValueError as exc:
            if str(exc) == "oauth_transaction_missing":
                if self._vault.is_transaction_consumed(state_sha256):
                    raise ValueError("oauth_callback_replayed") from None
                raise ValueError("oauth_callback_invalid") from None
            raise

    async def complete(
        self,
        *,
        profile_id: str,
        callback_uri: str,
    ) -> OAuthPreparedAuthorization:
        route_profile_id = _stable_text(profile_id, reason="oauth_callback_invalid")
        callback = _bounded_callback_query(callback_uri)
        state_sha256 = _callback_state_digest(callback)
        async with self._lock(self._completion_locks, state_sha256):
            transaction = self._load_callback_transaction(state_sha256)
            if transaction.authorization_flow != (
                OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE.value
            ):
                raise ValueError("oauth_authorization_flow_mismatch")
            if transaction.profile_id != route_profile_id:
                raise ValueError("oauth_callback_mismatch")
            profile = self._registry.get_exact(
                route_profile_id,
                transaction.profile_revision,
            )
            if (
                profile.client_mode is not OAuthClientMode.HOSTED_BRIDGE
                and callback.state == state_sha256
            ):
                # A direct PKCE callback must present the original random
                # state, not its publicly exposed transaction digest.
                raise ValueError("oauth_callback_invalid")
            credential = self._validate_transaction(profile, transaction)
            discovery = await self._protocol.discover(
                profile,
                resource_origin=transaction.resource_origin,
            )
            self._validate_discovery(profile, discovery, transaction.resource_origin)
            if callback.error is not None:
                self._validate_denial_callback(callback, transaction, discovery)
                self._vault.consume_transaction(state_sha256, reason="denied")
                reason = (
                    "oauth_authorization_denied"
                    if callback.error == "access_denied"
                    else "oauth_provider_rejected"
                )
                raise ValueError(reason)
            if profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
                if callback.completion_handle is None or callback.code is not None:
                    raise ValueError("oauth_callback_invalid")
                parsed_uri = urlsplit(callback.callback_uri)
                expected_uri = urlsplit(callback_landing_uri(transaction.redirect_uri))
                if (
                    (parsed_uri.scheme, parsed_uri.netloc, parsed_uri.path)
                    != (expected_uri.scheme, expected_uri.netloc, expected_uri.path)
                    or set(parse_qs(parsed_uri.query)) - {"state", "completion_handle", "iss"}
                    or callback.state != state_sha256
                    or callback.issuer != transaction.authorization_issuer
                ):
                    raise ValueError("oauth_callback_invalid")
                if transaction.prepared_token is not None:
                    raise ValueError("oauth_callback_replayed")
            else:
                if callback.completion_handle is not None:
                    raise ValueError("oauth_callback_invalid")
                try:
                    parsed = parse_exact_callback(
                        callback.callback_uri,
                        redirect_uri=callback_landing_uri(transaction.redirect_uri),
                        expected_issuer=transaction.authorization_issuer,
                        issuer_parameter_required=(
                            discovery.authorization_response_iss_parameter_supported
                        ),
                    )
                except McpClientError:
                    raise ValueError("oauth_callback_invalid") from None
                if sha256_hex(parsed.state.encode("utf-8")) != state_sha256:
                    raise ValueError("oauth_callback_invalid")
            if transaction.prepared_token is None:
                if transaction.pkce_verifier is None:
                    raise ValueError("oauth_transaction_binding_drift")
                try:
                    if profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
                        if self._hosted_bridge is None or callback.completion_handle is None:
                            raise ValueError("oauth_profile_unavailable")
                        token = await self._hosted_bridge.redeem(
                            profile.profile_id,
                            callback.completion_handle,
                            state_sha256,
                            transaction.pkce_verifier,
                            transaction.scopes,
                            transaction.resource_origin,
                        )
                    else:
                        parameters = OAuthAuthorizationParameters.for_profile(
                            profile,
                            credential=credential,
                            scopes=transaction.scopes,
                            state=state_sha256,
                            code_challenge=_pkce_challenge(transaction.pkce_verifier),
                            resource_origin=transaction.resource_origin,
                        )
                        token = await self._protocol.exchange(
                            discovery,
                            parameters,
                            code=parsed.code,
                            code_verifier=transaction.pkce_verifier,
                            client_secret=(
                                None if credential is None else credential.client_secret
                            ),
                        )
                except OAuthProviderError as exc:
                    if (
                        profile.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION
                        or exc.reason != "oauth_client_invalid"
                    ):
                        raise
                    self._vault.consume_transaction(
                        state_sha256,
                        reason="client_registration_rejected",
                    )
                    await self._replace_dynamic_registration(
                        profile,
                        discovery,
                        rejected_client_id=transaction.client_id,
                    )
                    raise ValueError("oauth_client_registration_rejected") from None
                self._vault.prepare_token(state_sha256, _prepared_token(token))
            return OAuthPreparedAuthorization.from_transaction(
                self._vault.load_transaction(state_sha256)
            )

    @staticmethod
    def _checked_commit(commit: OAuthConnectionCommit) -> OAuthConnectionCommit:
        if type(commit) is not OAuthConnectionCommit:
            raise ValueError("oauth_connection_commit_invalid")
        for value in (
            commit.connection_revision,
            commit.capability_revision,
            commit.authority_revision,
            commit.capability_binding_revision,
        ):
            _digest(value, reason="oauth_connection_commit_invalid")
        _stable_text(commit.execution_policy, reason="oauth_connection_commit_invalid")
        if type(commit.connection_generation) is not int or commit.connection_generation < 0:
            raise ValueError("oauth_connection_commit_invalid")
        return commit

    def commit(
        self,
        prepared: OAuthPreparedAuthorization,
        commit: OAuthConnectionCommit,
    ) -> OAuthCommittedGrant:
        if type(prepared) is not OAuthPreparedAuthorization:
            raise ValueError("oauth_prepared_authorization_invalid")
        checked = self._checked_commit(commit)
        grant = OAuthCommittedGrant(
            receipt_id=sha256_hex(
                canonical_bytes(
                    {
                        "connection_revision": checked.connection_revision,
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
            connection_revision=checked.connection_revision,
            capability_revision=checked.capability_revision,
            authority_revision=checked.authority_revision,
            capability_binding_revision=checked.capability_binding_revision,
            scopes=prepared.scopes,
            execution_policy=checked.execution_policy,
            connection_generation=checked.connection_generation,
        )
        try:
            self._vault.commit_authorization(prepared.state_sha256, grant)
        except ValueError as exc:
            if str(exc) == "oauth_transaction_missing" and self._vault.is_transaction_consumed(
                prepared.state_sha256
            ):
                raise ValueError("oauth_callback_replayed") from None
            raise
        return grant

    def recover(
        self,
    ) -> tuple[tuple[OAuthPreparedAuthorization, ...], tuple[OAuthCommittedGrant, ...]]:
        prepared = tuple(
            OAuthPreparedAuthorization.from_transaction(transaction)
            for transaction in self._vault.recover_prepared_transactions()
        )
        return prepared, self.unclaimed_grant_receipts()

    def is_transaction_consumed(self, state_sha256: str) -> bool:
        """Report whether an authorization attempt has reached a terminal state."""

        return self._vault.is_transaction_consumed(state_sha256)

    def discard_prepared(self, state_sha256: str) -> None:
        """Consume a terminal prepared attempt so restart recovery cannot replay it."""

        key = _digest(state_sha256, reason="oauth_transaction_invalid")
        try:
            transaction = self._vault.load_transaction(key)
        except ValueError as exc:
            if str(exc) == "oauth_transaction_missing":
                return
            raise
        if transaction.prepared_token is None:
            raise ValueError("oauth_prepared_authorization_invalid")
        self._vault.consume_transaction(key, reason="activation_failed")

    def discard_attempt(self, state_sha256: str) -> None:
        """Consume any rejected authorization attempt before it can be resumed."""

        key = _digest(state_sha256, reason="oauth_transaction_invalid")
        try:
            self._vault.load_transaction(key)
        except ValueError as exc:
            if str(exc) == "oauth_transaction_missing":
                return
            raise
        self._vault.consume_transaction(key, reason="authorization_rejected")

    def discard_connection_attempts(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> None:
        """Invalidate pending callbacks and prepared tokens at disconnect."""

        self._vault.consume_connection_transactions(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account_key_sha256,
        )

    def unclaimed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        return self._vault.claim_committed_grants()

    def committed_grant_receipts(self) -> tuple[OAuthCommittedGrant, ...]:
        return self._vault.committed_grant_receipts()

    def require_plugin_partition_receipts_exact(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_receipts: tuple[OAuthCommittedGrant, ...],
    ) -> None:
        self._vault.require_plugin_partition_receipts_exact(
            partition_key,
            plugin_id=plugin_id,
            expected_receipts=expected_receipts,
        )

    def require_plugin_partition_retirement_exact(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_remaining_plugin_ids: tuple[str, ...],
    ) -> None:
        self._vault.require_plugin_partition_retirement_exact(
            partition_key,
            plugin_id=plugin_id,
            expected_remaining_plugin_ids=expected_remaining_plugin_ids,
        )

    def grant_receipt_state(self, expected: OAuthCommittedGrant) -> str:
        return self._vault.grant_receipt_state(expected)

    def claim_grant_receipt_exact(
        self,
        expected: OAuthCommittedGrant,
        authority_revision: str,
    ) -> OAuthGrantReceiptClaimOutcome:
        return self._vault.claim_grant_receipt_exact(
            expected,
            authority_revision=authority_revision,
        )

    def claim_grant_receipt(self, receipt_id: str, authority_revision: str) -> None:
        self._vault.mark_grant_claimed(
            receipt_id,
            authority_revision=authority_revision,
        )

    def maximum_connection_generation(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> int | None:
        return self._vault.maximum_connection_generation(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account_key_sha256,
        )

    def begin_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        plugin_id: str,
        account_key_sha256: str,
        app_ids: tuple[str, ...],
    ) -> dict[str, str]:
        return self._vault.begin_plugin_update_recovery(
            checkpoint_id,
            plugin_id=plugin_id,
            account_key_sha256=account_key_sha256,
            app_ids=app_ids,
        )

    def restore_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        expected_revision: str,
    ) -> dict[str, str]:
        return self._vault.restore_plugin_update_recovery(
            checkpoint_id,
            expected_revision=expected_revision,
        )

    def discard_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        plugin_id: str,
        account_key_sha256: str,
    ) -> None:
        self._vault.discard_plugin_update_recovery(
            checkpoint_id,
            plugin_id=plugin_id,
            account_key_sha256=account_key_sha256,
        )

    def finalize_plugin_update_recovery(
        self,
        checkpoint_id: str,
        *,
        expected_revision: str,
    ) -> None:
        self._vault.finalize_plugin_update_recovery(
            checkpoint_id,
            expected_revision=expected_revision,
        )

    def _validate_partition(
        self,
        partition: OAuthTokenPartition,
        profile: OAuthProfile,
    ) -> OAuthClientCredential | None:
        credential = (
            OAuthClientCredential(partition.client_id, None)
            if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
            else self._credential_for(profile)
        )
        client_id = (
            profile.client_metadata_document_url if credential is None else credential.client_id
        )
        if client_id is None:
            raise ValueError("oauth_transport_binding_invalid")
        if partition.migration_status is not None:
            raise ValueError("oauth_reauthorization_required")
        credential_identity = _credential_identity(profile, client_id)
        expected_partition = _partition_digest(
            account_partition=partition.account_partition,
            authorization_issuer=partition.authorization_issuer,
            resource_origin=partition.resource_origin,
            account_key_sha256=partition.account_key_sha256,
            credential_identity=partition.credential_identity or "",
        )
        if (
            partition.authorization_issuer != profile.authorization_issuer
            or partition.client_id != client_id
            or partition.credential_identity != credential_identity
            or partition.account_partition != profile.account_partition
            or partition.partition_key != expected_partition
        ):
            raise ValueError("oauth_transport_binding_invalid")
        return credential

    def _validate_revocation_partition(
        self,
        partition: OAuthTokenPartition,
        profile: OAuthProfile,
    ) -> OAuthClientCredential | None:
        if partition.migration_status != "legacy_v1":
            return self._validate_partition(partition, profile)
        credential = (
            OAuthClientCredential(partition.client_id, None)
            if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
            else self._credential_for(profile)
        )
        client_id = (
            profile.client_metadata_document_url if credential is None else credential.client_id
        )
        if (
            client_id is None
            or partition.profile_id != profile.profile_id
            or partition.profile_revision != profile.revision
            or partition.authorization_issuer != profile.authorization_issuer
            or partition.resource_origin not in profile.resource_origins
            or partition.client_id != client_id
            or partition.account_partition != "issuer_account"
            or partition.partition_key != _legacy_revocation_partition_digest(partition)
        ):
            raise ValueError("oauth_transport_binding_invalid")
        return credential

    def _validate_binding_partition(
        self,
        binding: BrokerTransportBinding,
        partition: OAuthTokenPartition,
        grant: OAuthCommittedGrant,
    ) -> None:
        if (
            partition.partition_key != binding.partition_key
            or binding.plugin_id not in partition.dependent_plugin_ids
            or grant.plugin_id != binding.plugin_id
            or grant.app_id != binding.app_id
            or grant.account_key_sha256 != partition.account_key_sha256
            or grant.token_partition_key != partition.partition_key
            or not set(grant.scopes).issubset(partition.token.scopes)
            or grant.connection_revision != binding.connection_revision
            or grant.capability_revision != binding.capability_revision
            or grant.authority_revision != binding.authority_revision
            or grant.capability_binding_revision != binding.capability_binding_revision
        ):
            raise ValueError("oauth_transport_binding_invalid")
        if partition.reauthorization_required or grant.status == "reauthorization_required":
            raise ValueError("oauth_reauthorization_required")
        if partition.revocation_pending_plugin_id is not None:
            raise ValueError("oauth_revocation_pending")
        if grant.status == "revocation_pending":
            raise ValueError("oauth_revocation_pending")
        if grant.status != "active":
            raise ValueError("oauth_transport_binding_invalid")

    def _validate_transport_grant(
        self,
        binding: BrokerTransportBinding,
    ) -> OAuthCommittedGrant:
        if type(binding) is not BrokerTransportBinding:
            raise ValueError("oauth_transport_binding_invalid")
        grant = self._vault.validate_transport_grant(
            plugin_id=binding.plugin_id,
            app_id=binding.app_id,
            profile_id=binding.profile_id,
            profile_revision=binding.profile_revision,
            partition_key=binding.partition_key,
            connection_revision=binding.connection_revision,
            capability_revision=binding.capability_revision,
            authority_revision=binding.authority_revision,
            capability_binding_revision=binding.capability_binding_revision,
        )
        if (
            type(grant) is not OAuthCommittedGrant
            or grant.plugin_id != binding.plugin_id
            or grant.app_id != binding.app_id
            or grant.profile_id != binding.profile_id
            or grant.profile_revision != binding.profile_revision
            or grant.token_partition_key != binding.partition_key
            or grant.connection_revision != binding.connection_revision
            or grant.capability_revision != binding.capability_revision
            or grant.authority_revision != binding.authority_revision
            or grant.capability_binding_revision != binding.capability_binding_revision
            or grant.status != "active"
        ):
            raise ValueError("oauth_transport_binding_invalid")
        return grant

    def _load_transport_partition(
        self,
        binding: BrokerTransportBinding,
    ) -> OAuthTokenPartition:
        grant = self._validate_transport_grant(binding)
        partition = self._vault.load_token_partition(binding.partition_key)
        self._validate_binding_partition(binding, partition, grant)
        profile = self._registry.get_exact(grant.profile_id, grant.profile_revision)
        self._profile_route(profile)
        if binding.resource_url not in profile.resource_origins:
            raise ValueError("oauth_transport_binding_invalid")
        self._validate_partition(partition, profile)
        return partition

    def _validate_prepared_transport(
        self,
        prepared: OAuthPreparedAuthorization,
        binding: BrokerTransportBinding,
    ) -> OAuthPreparedToken:
        if (
            type(prepared) is not OAuthPreparedAuthorization
            or type(binding) is not BrokerTransportBinding
        ):
            raise ValueError("oauth_prepared_authorization_invalid")
        try:
            transaction = self._vault.load_transaction(prepared.state_sha256)
        except ValueError:
            raise ValueError("oauth_prepared_authorization_invalid") from None
        if (
            type(transaction) is not OAuthPendingTransaction
            or transaction.prepared_token is None
            or _clock_value(self._clock) >= transaction.expires_at
        ):
            raise ValueError("oauth_prepared_authorization_invalid")
        try:
            recovered = OAuthPreparedAuthorization.from_transaction(transaction)
        except ValueError:
            raise ValueError("oauth_prepared_authorization_invalid") from None
        if recovered != prepared:
            raise ValueError("oauth_prepared_authorization_invalid")
        if (
            binding.plugin_id != prepared.plugin_id
            or binding.app_id != prepared.app_id
            or binding.profile_id != prepared.profile_id
            or binding.profile_revision != prepared.profile_revision
            or binding.partition_key != prepared.partition_key
            or binding.resource_url != transaction.resource_origin
        ):
            raise ValueError("oauth_transport_binding_invalid")
        profile = self._registry.get_exact(prepared.profile_id, prepared.profile_revision)
        self._profile_route(profile)
        credential = (
            self._stored_dynamic_credential(profile)
            if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
            else self._credential_for(profile)
        )
        client_id = (
            profile.client_metadata_document_url if credential is None else credential.client_id
        )
        if client_id is None:
            raise ValueError("oauth_prepared_authorization_invalid")
        credential_identity = _credential_identity(profile, client_id)
        if transaction.attempt_context.get("omega_migration_status") is not None:
            raise ValueError("oauth_reauthorization_required")
        expected_partition = _partition_digest(
            account_partition=transaction.account_partition,
            authorization_issuer=transaction.authorization_issuer,
            resource_origin=transaction.resource_origin,
            account_key_sha256=transaction.account_key_sha256,
            credential_identity=transaction.credential_identity or "",
        )
        if (
            transaction.authorization_issuer != profile.authorization_issuer
            or transaction.resource_origin not in profile.resource_origins
            or transaction.client_id != client_id
            or transaction.credential_identity != credential_identity
            or transaction.account_partition != profile.account_partition
            or transaction.redirect_uri != profile.redirect_uri
            or transaction.partition_key != expected_partition
            or transaction.prepared_token.scopes != transaction.scopes
            or transaction.attempt_context.get(_AUTHORIZATION_CONTEXT_ATTEMPT_KEY)
            != transaction.authorization_context_revision
        ):
            raise ValueError("oauth_prepared_authorization_invalid")
        return transaction.prepared_token

    def authorize_transport(
        self,
        binding: BrokerTransportBinding,
        authority: BrokerTransportAuthority,
    ) -> McpBrokerAuthorizationConfig:
        """Issue token-only transport auth from broker and host authority."""

        _oauth_broker_record(self)
        _validate_broker_transport_authority(self, authority, binding)
        grant = self._validate_transport_grant(binding)
        del grant
        from mcp_platform.authorization import _issue_broker_authorization_from_broker

        return _issue_broker_authorization_from_broker(
            broker=self,
            binding=binding,
            authority=authority,
        )

    def authorize_prepared_transport(
        self,
        prepared: OAuthPreparedAuthorization,
        binding: BrokerTransportBinding,
        authority: BrokerTransportAuthority,
    ) -> McpBrokerAuthorizationConfig:
        """Issue the exact sealed transport config for one live prepared probe."""

        _oauth_broker_record(self)
        _validate_broker_transport_authority(self, authority, binding)
        token = self._validate_prepared_transport(prepared, binding)
        del token
        from mcp_platform.authorization import _issue_broker_authorization_from_broker

        return _issue_broker_authorization_from_broker(
            broker=self,
            binding=binding,
            authority=authority,
            prepared=prepared,
        )

    async def current_access_token(self, binding: BrokerTransportBinding) -> str:
        partition = self._load_transport_partition(binding)
        if not _token_is_fresh(partition.token, _clock_value(self._clock)):
            await self.refresh(binding.partition_key, binding=binding)
            partition = self._load_transport_partition(binding)
        return partition.token.access_token

    async def _issue_execution_worker_access_token(
        self,
        binding: BrokerTransportBinding,
        authority: BrokerTransportAuthority,
        *,
        execution_worker: PluginExecutionWorker | None,
        worker_authority: PluginExecutionAuthority,
        timeout_seconds: float,
    ) -> str:
        """Issue one bearer value, refreshing only in the shared fenced worker."""

        from mcp_platform.plugin_execution_worker import (
            PluginExecutionAuthority,
            PluginExecutionWorker,
        )

        _oauth_broker_record(self)
        _validate_broker_transport_authority(self, authority, binding)
        if (
            type(worker_authority) is not PluginExecutionAuthority
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("oauth_worker_refresh_invalid")
        key = binding.partition_key
        async with self._lock(self._refresh_locks, key):
            current = self._load_transport_partition(binding)
            if (
                worker_authority.plugin_id != binding.plugin_id
                or worker_authority.app_id != binding.app_id
                or worker_authority.account_key_sha256 != current.account_key_sha256
                or worker_authority.connection_revision != binding.connection_revision
                or worker_authority.capability_revision != binding.capability_revision
                or worker_authority.authority_revision != binding.authority_revision
            ):
                raise ValueError("oauth_worker_refresh_invalid")
            if _token_is_fresh(current.token, _clock_value(self._clock)):
                return current.token.access_token
            if type(execution_worker) is not PluginExecutionWorker:
                raise ValueError("oauth_worker_refresh_invalid")
            if current.token.refresh_token is None:
                self._vault.mark_partition_reauthorization_required(key)
                raise ValueError("oauth_reauthorization_required")
            profile = self._registry.get_exact(current.profile_id, current.profile_revision)
            credential = self._validate_partition(current, profile)
            request: dict[str, object] = {
                "plugin_id": binding.plugin_id,
                "app_id": binding.app_id,
                "account_key_sha256": current.account_key_sha256,
                "partition_key": current.partition_key,
                "expected_revision": current.revision,
                "profile_id": current.profile_id,
                "profile_revision": current.profile_revision,
                "resource_origin": current.resource_origin,
                "client_id": current.client_id,
                "required_scopes": list(current.token.scopes),
                "connection_revision": binding.connection_revision,
                "capability_revision": binding.capability_revision,
                "authority_revision": binding.authority_revision,
                "capability_binding_revision": binding.capability_binding_revision,
            }
            result = await execution_worker.run_async(
                authority=worker_authority,
                body={
                    "operation": "oauth_refresh",
                    "profile": _oauth_profile_document(profile),
                    "request": request,
                },
                credential={
                    "refresh_token": current.token.refresh_token,
                    "client_secret": (
                        None if credential is None else credential.client_secret
                    ),
                },
                timeout_seconds=float(timeout_seconds),
            )
            _validate_broker_transport_authority(self, authority, binding)
            if not isinstance(result, Mapping):
                raise ValueError("oauth_worker_refresh_invalid")
            result_document = dict(result)
            expected_request_sha256 = _oauth_refresh_request_sha256(request)
            common_valid = (
                result_document.get("operation") == "oauth_refresh"
                and result_document.get("request_sha256") == expected_request_sha256
                and result_document.get("partition_key") == current.partition_key
                and result_document.get("expected_revision") == current.revision
            )
            if set(result_document) == _OAUTH_REFRESH_FAILURE_FIELDS:
                if not common_valid:
                    raise ValueError("oauth_worker_refresh_invalid")
                error_kind = result_document["error_kind"]
                reason = result_document["reason"]
                if error_kind == "network" and reason in _OAUTH_REFRESH_NETWORK_REASONS:
                    raise OAuthNetworkError(reason)
                if error_kind == "protocol" and reason == "oauth_provider_rejected":
                    raise OAuthProviderError(reason)
                if error_kind == "protocol" and reason == "oauth_client_invalid":
                    self._vault.mark_partition_reauthorization_required(key)
                    if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
                        await self._invalidate_dynamic_registration(
                            profile,
                            rejected_client_id=current.client_id,
                        )
                    raise ValueError("oauth_reauthorization_required")
                if error_kind == "protocol" and reason == "oauth_refresh_invalid":
                    raise OAuthProtocolError(reason)
                raise ValueError("oauth_worker_refresh_invalid")
            if set(result_document) != _OAUTH_REFRESH_RESULT_FIELDS or not common_valid:
                raise ValueError("oauth_worker_refresh_invalid")
            token_document = result_document["token"]
            try:
                if type(token_document) is not dict or set(token_document) != {
                    "access_token",
                    "refresh_token",
                    "token_type",
                    "scopes",
                    "expires_at",
                }:
                    raise ValueError
                scopes_value = token_document["scopes"]
                expires_value = token_document["expires_at"]
                if type(scopes_value) is not list or (
                    expires_value is not None
                    and type(expires_value) not in {int, float}
                ):
                    raise ValueError
                token = OAuthPreparedToken(
                    access_token=_stable_text(
                        token_document["access_token"],
                        reason="oauth_worker_refresh_invalid",
                        maximum=65_536,
                    ),
                    refresh_token=(
                        None
                        if token_document["refresh_token"] is None
                        else _stable_text(
                            token_document["refresh_token"],
                            reason="oauth_worker_refresh_invalid",
                            maximum=65_536,
                        )
                    ),
                    token_type=_stable_text(
                        token_document["token_type"],
                        reason="oauth_worker_refresh_invalid",
                        maximum=128,
                    ),
                    scopes=_checked_scopes(tuple(scopes_value)),
                    expires_at=(
                        None
                        if expires_value is None
                        else float(expires_value)
                    ),
                )
                if (
                    token.refresh_token is None
                    or token.token_type != "Bearer"
                    or token.scopes != current.token.scopes
                    or token.expires_at is not None
                    and (
                        not math.isfinite(token.expires_at)
                        or token.expires_at <= _clock_value(self._clock)
                    )
                ):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("oauth_worker_refresh_invalid") from None
            latest_before_replace = self._load_transport_partition(binding)
            if latest_before_replace.revision != current.revision:
                if _token_is_fresh(latest_before_replace.token, _clock_value(self._clock)):
                    return latest_before_replace.token.access_token
                raise ValueError("oauth_token_revision_conflict")
            try:
                self._vault.replace_token_partition(
                    key,
                    token,
                    expected_revision=current.revision,
                )
            except ValueError as exc:
                if str(exc) != "oauth_token_revision_conflict":
                    raise
                latest = self._load_transport_partition(binding)
                if _token_is_fresh(latest.token, _clock_value(self._clock)):
                    return latest.token.access_token
                raise
            return self._load_transport_partition(binding).token.access_token

    async def _current_prepared_access_token(
        self,
        prepared: OAuthPreparedAuthorization,
        binding: BrokerTransportBinding,
    ) -> str:
        """Read a prepared token only while its exact transaction is probe-eligible."""

        return self._validate_prepared_transport(prepared, binding).access_token

    async def refresh(
        self,
        partition_key: str,
        *,
        binding: BrokerTransportBinding | None = None,
        force: bool = False,
    ) -> OAuthTokenPartition:
        key = _digest(partition_key, reason="oauth_token_partition_invalid")
        if type(force) is not bool or (
            binding is not None
            and (type(binding) is not BrokerTransportBinding or binding.partition_key != key)
        ):
            raise ValueError("oauth_transport_binding_invalid")
        async with self._lock(self._refresh_locks, key):
            current = (
                self._vault.load_token_partition(key)
                if binding is None
                else self._load_transport_partition(binding)
            )
            if current.revocation_pending_plugin_id is not None:
                raise ValueError("oauth_revocation_pending")
            if current.reauthorization_required or current.migration_status is not None:
                raise ValueError("oauth_reauthorization_required")
            if not force and _token_is_fresh(current.token, _clock_value(self._clock)):
                return current
            if current.token.refresh_token is None:
                # Marking is durable and says "this connection is dead until the
                # operator signs in again". A token that has not expired is not
                # dead just because it cannot be refreshed -- a credential with
                # no expiry never can be -- and only `force` reaches here with a
                # live one. Condemn only a credential actually past its life;
                # refuse the refresh either way.
                #
                # Live cost (2026-09-08): a maintainer sweep forced a refresh
                # across every installed plugin to capture their surfaces, and
                # marked Slack's working non-expiring connection. Supabase's
                # mark then blocked its own update rollback, which stranded the
                # lifecycle and stopped the HUD from starting at all.
                if not _token_is_fresh(current.token, _clock_value(self._clock)):
                    self._vault.mark_partition_reauthorization_required(key)
                raise ValueError("oauth_reauthorization_required")
            profile = self._registry.get_exact(current.profile_id, current.profile_revision)
            credential = self._validate_partition(current, profile)
            try:
                if profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
                    if self._hosted_bridge is None:
                        raise OAuthProfileUnavailable()
                    # Refresh has no live authorization attempt. The exact
                    # vault partition key is the durable connection digest.
                    token = await self._hosted_bridge.refresh(
                        profile.profile_id,
                        current.token.refresh_token,
                        key,
                        current.token.scopes,
                        current.resource_origin,
                    )
                else:
                    discovery = await self._protocol.discover(
                        profile,
                        resource_origin=current.resource_origin,
                    )
                    self._validate_discovery(profile, discovery, current.resource_origin)
                    token = await self._protocol.refresh(
                        discovery,
                        current.client_id,
                        current.token.refresh_token,
                        client_secret=(None if credential is None else credential.client_secret),
                        required_scopes=current.token.scopes,
                    )
            except OAuthProviderError as exc:
                self._vault.mark_partition_reauthorization_required(key)
                if (
                    profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
                    and exc.reason == "oauth_client_invalid"
                ):
                    await self._invalidate_dynamic_registration(
                        profile,
                        rejected_client_id=current.client_id,
                    )
                    raise ValueError("oauth_reauthorization_required") from None
                raise
            except OAuthNetworkError:
                raise
            except OAuthProtocolError:
                self._vault.mark_partition_reauthorization_required(key)
                raise
            try:
                replaced = self._vault.replace_token_partition(
                    key,
                    _prepared_token(token),
                    expected_revision=current.revision,
                )
                return replaced if binding is None else self._load_transport_partition(binding)
            except ValueError as exc:
                if str(exc) != "oauth_token_revision_conflict":
                    raise
                latest = (
                    self._vault.load_token_partition(key)
                    if binding is None
                    else self._load_transport_partition(binding)
                )
                if _token_is_fresh(latest.token, _clock_value(self._clock)):
                    return latest
                raise

    async def revoke(
        self,
        partition_key: str,
        *,
        plugin_id: str,
        expected_receipts: tuple[OAuthCommittedGrant, ...] | None = None,
        tolerate_provider_refusal: bool = False,
    ) -> OAuthRevocationResult:
        """Revoke one partition, and say what the provider was told.

        ``tolerate_provider_refusal`` is the difference between the two callers.
        A DISCONNECT is an instruction to let go: the local credential must be
        destroyed whatever the provider says, because refusing leaves the plugin
        installed and the token held. A predecessor RETIREMENT during an update
        is not -- the plugin is staying, the old token should really be revoked,
        and a refusal is worth retrying after a restart.

        A provider that advertises no revocation endpoint is not a refusal and
        never blocks either caller: there is no attempt that could ever succeed.
        """
        key = _digest(partition_key, reason="oauth_token_partition_invalid")
        dependent = _stable_text(plugin_id, reason="oauth_token_partition_invalid")
        async with self._lock(self._refresh_locks, key):
            prepared, remaining = self._vault.prepare_revocation(
                key,
                plugin_id=dependent,
                expected_receipts=expected_receipts,
            )
            if remaining:
                return OAuthRevocationResult(
                    revoked_provider_token=False,
                    dependents=remaining,
                    local_token_deleted=False,
                )
            profile = self._registry.get_exact(
                prepared.profile_id,
                prepared.profile_revision,
            )
            credential = self._validate_revocation_partition(prepared, profile)
            try:
                if profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
                    if self._hosted_bridge is None:
                        raise OAuthProfileUnavailable()
                    if prepared.token.refresh_token is None:
                        receipt = OAuthRevocationReceipt(
                            endpoint_advertised=False, provider_request_sent=False
                        )
                    else:
                        # The same partition digest binds this bridge call to
                        # the customer-owned connection being deleted.
                        # Its receipt is the record of what the provider was sent;
                        # the broker never assumes a request it did not see.
                        receipt = await self._hosted_bridge.revoke(
                            profile.profile_id, prepared.token.refresh_token, key
                        )
                        if type(receipt) is not OAuthRevocationReceipt:
                            raise OAuthProtocolError("oauth_revocation_unsupported")
                else:
                    discovery = await self._protocol.discover(
                        profile,
                        resource_origin=prepared.resource_origin,
                    )
                    self._validate_discovery(profile, discovery, prepared.resource_origin)
                    receipt = await self._protocol.revoke(
                        discovery,
                        client_id=prepared.client_id,
                        token=prepared.token.refresh_token or prepared.token.access_token,
                        client_secret=(None if credential is None else credential.client_secret),
                    )
            except OAuthProviderError:
                if not tolerate_provider_refusal:
                    # A retirement wants this retried, not absorbed.
                    raise
                # The provider refused the revocation. RFC 7009 s2.2 says a
                # server answers 200 even for a token it does not recognise, so
                # the common cause is a token that already expired -- Google
                # answers 400 for one, and that is what blocked uninstalling
                # Calendar and Drive while Gmail, whose token was still live,
                # went through. Either way the instruction was to disconnect,
                # and keeping Omega's own copy because the provider would not
                # confirm is the wrong half to preserve.
                _LOGGER.warning(
                    "oauth_revocation_rejected: %s disconnected and its local "
                    "credential destroyed; the provider refused the revocation, "
                    "so a token it still holds must be revoked there",
                    dependent,
                )
                self._vault.finalize_revocation(
                    key,
                    plugin_id=dependent,
                    expected_revision=prepared.revision,
                    expected_receipts=expected_receipts,
                )
                return OAuthRevocationResult(
                    revoked_provider_token=False,
                    dependents=(),
                    local_token_deleted=True,
                )
            if receipt.endpoint_advertised and not receipt.provider_request_sent:
                # The provider HAS a revocation endpoint and nothing was sent to
                # it. That is an internal contradiction, not a provider lacking
                # the feature, and it is the only case still refused.
                raise ValueError("oauth_revocation_unsupported")
            if not receipt.endpoint_advertised:
                # RFC 7009 revocation is OPTIONAL, and GitHub, Slack, Google,
                # Hugging Face and Netlify all publish no `revocation_endpoint`
                # at the issuer Omega resolves. Refusing here raised BEFORE
                # `finalize_revocation`, so uninstall could never complete for
                # any of them AND Omega kept the credential it had just been
                # told to destroy -- the worse of both outcomes, and one no
                # later attempt could ever satisfy. The local copy is destroyed
                # below; `revoked_provider_token` reports honestly that the
                # provider was not told.
                _LOGGER.warning(
                    "oauth_revocation_unsupported: %s disconnected and its local "
                    "credential destroyed; the provider publishes no revocation "
                    "endpoint, so a token it still holds must be revoked there",
                    dependent,
                )
            self._vault.finalize_revocation(
                key,
                plugin_id=dependent,
                expected_revision=prepared.revision,
                expected_receipts=expected_receipts,
            )
            if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
                await self.delete_registration(
                    profile.authorization_issuer,
                    profile.profile_id,
                )
            return OAuthRevocationResult(
                revoked_provider_token=receipt.provider_request_sent,
                dependents=(),
                local_token_deleted=True,
            )


_OAUTH_BROKER_INSTANCE_FIELDS = frozenset(
    {
        "_registry",
        "_vault",
        "_protocol",
        "_hosted_bridge",
        "_clock",
        "_completion_locks",
        "_refresh_locks",
        "_registration_locks",
    }
)
_OAUTH_BROKER_AUTHORITY_METHODS = (
    "_credential_for",
    "_validate_discovery",
    "_validate_partition",
    "_validate_revocation_partition",
    "_validate_binding_partition",
    "_validate_transport_grant",
    "_load_transport_partition",
    "_validate_prepared_transport",
    "authorize_transport",
    "authorize_prepared_transport",
    "current_access_token",
    "_issue_execution_worker_access_token",
    "_current_prepared_access_token",
    "refresh",
)
_ORIGINAL_OAUTH_BROKER_AUTHORITY_METHODS = tuple(
    (name, OAuthBroker.__dict__[name]) for name in _OAUTH_BROKER_AUTHORITY_METHODS
)


@dataclass(slots=True, repr=False)
class _IssuedOAuthBroker:
    broker_ref: weakref.ReferenceType[OAuthBroker]
    proof: object
    fingerprint: tuple[object, ...]


@dataclass(slots=True, repr=False)
class _IssuedBrokerTransportAuthority:
    authority_ref: weakref.ReferenceType[BrokerTransportAuthority]
    broker_ref: weakref.ReferenceType[OAuthBroker]
    current_binding: Callable[[BrokerTransportBinding], BrokerTransportBinding]
    proof: object
    fingerprint: tuple[object, ...]


_ISSUED_OAUTH_BROKERS: weakref.WeakKeyDictionary[OAuthBroker, _IssuedOAuthBroker] = (
    weakref.WeakKeyDictionary()
)
_ISSUED_BROKER_TRANSPORT_AUTHORITIES: weakref.WeakKeyDictionary[
    BrokerTransportAuthority,
    _IssuedBrokerTransportAuthority,
] = weakref.WeakKeyDictionary()


def _oauth_broker_fingerprint(
    broker: OAuthBroker,
    proof: object,
    original_methods: tuple[tuple[str, object], ...] = _ORIGINAL_OAUTH_BROKER_AUTHORITY_METHODS,
) -> tuple[object, ...]:
    if any(OAuthBroker.__dict__.get(name) is not method for name, method in original_methods):
        raise ValueError("oauth_broker_invalid")
    return (
        id(broker),
        id(broker._registry),
        id(broker._vault),
        id(broker._protocol),
        id(broker._hosted_bridge),
        id(broker._clock),
        id(broker._completion_locks),
        id(broker._refresh_locks),
        id(proof),
        tuple(id(method) for _name, method in original_methods),
    )


def _register_oauth_broker(broker: OAuthBroker) -> None:
    if (
        type(broker) is not OAuthBroker
        or broker in _ISSUED_OAUTH_BROKERS
        or set(vars(broker)) != _OAUTH_BROKER_INSTANCE_FIELDS
    ):
        raise ValueError("oauth_broker_invalid")
    proof = object()
    _ISSUED_OAUTH_BROKERS[broker] = _IssuedOAuthBroker(
        broker_ref=weakref.ref(broker),
        proof=proof,
        fingerprint=_oauth_broker_fingerprint(broker, proof),
    )


def _oauth_broker_record(broker: object) -> _IssuedOAuthBroker:
    if type(broker) is not OAuthBroker:
        raise ValueError("oauth_broker_invalid")
    record = _ISSUED_OAUTH_BROKERS.get(broker)
    if (
        record is None
        or record.broker_ref() is not broker
        or set(vars(broker)) != _OAUTH_BROKER_INSTANCE_FIELDS
        or _oauth_broker_fingerprint(broker, record.proof) != record.fingerprint
    ):
        raise ValueError("oauth_broker_invalid")
    return record


def _oauth_broker_authorization_identity(broker: object) -> tuple[object, ...]:
    record = _oauth_broker_record(broker)
    return record.fingerprint


def _broker_transport_authority_fingerprint(
    authority: BrokerTransportAuthority,
    *,
    broker: OAuthBroker,
    current_binding: Callable[[BrokerTransportBinding], BrokerTransportBinding],
    proof: object,
) -> tuple[object, ...]:
    return (
        id(authority),
        id(broker),
        _oauth_broker_authorization_identity(broker),
        id(current_binding),
        id(proof),
    )


def _issue_broker_transport_authority(
    broker: OAuthBroker,
    *,
    current_binding: Callable[[BrokerTransportBinding], BrokerTransportBinding],
) -> BrokerTransportAuthority:
    _oauth_broker_record(broker)
    if not callable(current_binding):
        raise ValueError("oauth_transport_authority_invalid")
    authority = object.__new__(BrokerTransportAuthority)
    proof = object()
    _ISSUED_BROKER_TRANSPORT_AUTHORITIES[authority] = _IssuedBrokerTransportAuthority(
        authority_ref=weakref.ref(authority),
        broker_ref=weakref.ref(broker),
        current_binding=current_binding,
        proof=proof,
        fingerprint=_broker_transport_authority_fingerprint(
            authority,
            broker=broker,
            current_binding=current_binding,
            proof=proof,
        ),
    )
    return authority


def _broker_transport_authority_record(
    broker: OAuthBroker,
    authority: object,
) -> _IssuedBrokerTransportAuthority:
    _oauth_broker_record(broker)
    if type(authority) is not BrokerTransportAuthority:
        raise ValueError("oauth_transport_authority_invalid")
    record = _ISSUED_BROKER_TRANSPORT_AUTHORITIES.get(authority)
    if (
        record is None
        or record.authority_ref() is not authority
        or record.broker_ref() is not broker
        or _broker_transport_authority_fingerprint(
            authority,
            broker=broker,
            current_binding=record.current_binding,
            proof=record.proof,
        )
        != record.fingerprint
    ):
        raise ValueError("oauth_transport_authority_invalid")
    return record


def _validate_broker_transport_authority(
    broker: OAuthBroker,
    authority: object,
    binding: object,
) -> None:
    record = _broker_transport_authority_record(broker, authority)
    if type(binding) is not BrokerTransportBinding:
        raise ValueError("oauth_transport_authority_invalid")
    try:
        current = record.current_binding(binding)
    except Exception:
        raise ValueError("oauth_transport_authority_invalid") from None
    if type(current) is not BrokerTransportBinding or current != binding:
        raise ValueError("oauth_transport_authority_invalid")


__all__ = [
    "BrokerTransportAuthority",
    "BrokerTransportBinding",
    "OAuthAuthorizationStart",
    "OAuthBroker",
    "OAuthConnectionCommit",
    "OAuthConnectionRequest",
    "OAuthPreparedAuthorization",
    "OAuthProfileUnavailable",
    "OAuthRevocationResult",
]
