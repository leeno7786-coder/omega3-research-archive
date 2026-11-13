"""Issuer-bound OAuth boundary for current remote MCP clients."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import math
import logging
import re
import sys
import secrets
import time
import weakref
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Protocol, TypeGuard, runtime_checkable
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from plugin_platform.contracts import AppRequirement, CapabilityKind

from mcp_platform.app_registry import InstalledMcpAppRecord
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
    _REMOTE_ORIGIN_PATH_RE,
)

_SCOPE = re.compile(r"[A-Za-z0-9._~:/-]+\Z")
_HEX_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z"
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_AUTHORIZATION_CONFIG_ISSUER = object()
_RESOURCE_METADATA = re.compile(r'(?:^|[,\s])resource_metadata="([^"]+)"')
_AUTHORIZATION_REQUEST_PROOF = "omega.mcp.authorization_request_proof"
_AUTHORIZATION_REDIRECT_PROOF = "omega.mcp.authorization_redirect_proof"
_AUTHORIZATION_REQUEST_ID_ATTEMPTS = 8
_GOOGLE_MCP_HOSTS = frozenset(
    {
        "calendarmcp.googleapis.com",
        "drivemcp.googleapis.com",
        "gmailmcp.googleapis.com",
    }
)
_GOOGLE_MCP_RESOURCE_PATH = "/mcp"
_GOOGLE_MCP_TRANSPORT_PATH = "/mcp/v1"


_LOGGER = logging.getLogger("omega.mcp.authorization")


def _fail(reason: McpClientErrorReason) -> None:
    """Refuse with a content-free reason, and record WHERE.

    This module raises `authorization_invalid` from more than twenty places
    covering unrelated conditions, and the operator sees one word. Miro's
    sign-in reached a completed MCP handshake and then refused with it, and the
    reason alone could not say which check that was.

    What is logged is Omega's own function and line -- never a token, never a
    value, never provider text -- which is the difference between reading this
    module and knowing.
    """
    caller = sys._getframe(1)
    _LOGGER.warning(
        "authorization refused: %s at %s:%d",
        reason.value,
        caller.f_code.co_name,
        caller.f_lineno,
    )
    raise McpClientError(reason)


@runtime_checkable
class McpAuthorizationClock(Protocol):
    """Host-injectable clock used for OAuth expiries and presentation leases."""

    def monotonic(self) -> float: ...

    def wall_time(self) -> float: ...


@runtime_checkable
class McpAuthorizationRandomness(Protocol):
    """Host-injectable cryptographically secure request-ID source."""

    def token_urlsafe(self, nbytes: int) -> str: ...


@dataclass(frozen=True, slots=True)
class _SystemAuthorizationClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_time(self) -> float:
        return time.time()


@dataclass(frozen=True, slots=True)
class _SystemAuthorizationRandomness:
    def token_urlsafe(self, nbytes: int) -> str:
        return secrets.token_urlsafe(nbytes)


_SYSTEM_AUTHORIZATION_CLOCK = _SystemAuthorizationClock()
_SYSTEM_AUTHORIZATION_RANDOMNESS = _SystemAuthorizationRandomness()


def _clock_value(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return float(value)


@dataclass(frozen=True, slots=True, repr=False)
class _StoredTokens:
    access_token: str
    token_type: str
    expires_at: float | None
    expires_in: int | None
    scope: str | None
    refresh_token: str | None
    authorization_endpoint: str
    token_endpoint: str

    def __repr__(self) -> str:
        return "McpStoredTokens(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _StoredClientInfo:
    client_id: str
    issuer: str
    redirect_uri: str
    token_endpoint_auth_method: str

    def __repr__(self) -> str:
        return "McpStoredClientInfo(<redacted>)"


@dataclass(frozen=True, slots=True)
class _BoundOAuthMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    scopes_supported: list[str] | None = None
    authorization_response_iss_parameter_supported: bool = True


@dataclass(frozen=True, slots=True)
class _BoundProtectedResourceMetadata:
    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str] | None = None


@runtime_checkable
class McpAuthorizationPresenter(Protocol):
    """Revocable host-side port for authorization presentation."""

    async def receive_authorization_request(self) -> McpAuthorizationRequest: ...

    async def submit_authorization_callback(
        self,
        request_id: str,
        callback_uri: str,
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class McpAuthorizationRequest:
    """Bounded URL presentation request carrying no OAuth provider authority."""

    request_id: str
    authorization_url: str
    expires_at: float

    def __repr__(self) -> str:
        return "McpAuthorizationRequest(<sealed>)"


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class McpAuthorizationChannel:
    """Sealed provider-side capability for one authorization configuration."""

    _proof: object

    def __repr__(self) -> str:
        return "McpAuthorizationChannel(<sealed>)"


@dataclass(slots=True)
class _PendingAuthorization:
    request: McpAuthorizationRequest
    future: asyncio.Future[AuthorizationCodeResult | object]
    expected_state: str
    generation: int


_CHANNEL_CLOSED = object()
_CHANNEL_FACTORY_ISSUER = object()


@dataclass(slots=True)
class _AuthorizationChannelState:
    proof: object
    queue: deque[_PendingAuthorization | object]
    queue_changed: asyncio.Event
    pending: dict[str, _PendingAuthorization]
    clock: McpAuthorizationClock = _SYSTEM_AUTHORIZATION_CLOCK
    randomness: McpAuthorizationRandomness = _SYSTEM_AUTHORIZATION_RANDOMNESS
    next_generation: int = 0
    revoked_generation: int = -1
    observed_revoked_generation: int = -1
    closed: bool = False
    bound: bool = False
    redirect_uri: str | None = None
    issuer: str | None = None
    loop: asyncio.AbstractEventLoop | None = None
    receiver_active: bool = False

    def bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = loop
        elif self.loop is not loop:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        return loop

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for pending in tuple(self.pending.values()):
            if not pending.future.done():
                pending.future.set_result(_CHANNEL_CLOSED)
        self.pending.clear()
        self.queue.clear()
        self.queue.append(_CHANNEL_CLOSED)
        self.queue_changed.set()


@dataclass(frozen=True, slots=True, repr=False)
class _IssuedAuthorizationChannel:
    channel_ref: weakref.ReferenceType[McpAuthorizationChannel]
    proof: object
    state: _AuthorizationChannelState


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class _McpAuthorizationPort:
    _proof: object

    async def receive_authorization_request(self) -> McpAuthorizationRequest:
        state = _authorization_port_state(self)
        state.bind_loop()
        if state.receiver_active:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        state.receiver_active = True
        try:
            while True:
                while state.queue:
                    value = state.queue.popleft()
                    if not state.queue:
                        state.queue_changed.clear()
                    if value is _CHANNEL_CLOSED:
                        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
                    if type(value) is not _PendingAuthorization:
                        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
                    pending = state.pending.get(value.request.request_id)
                    if pending is value and not value.future.done():
                        state.observed_revoked_generation = state.revoked_generation
                        return value.request
                    state.revoked_generation = max(
                        state.revoked_generation,
                        value.generation,
                    )
                if state.revoked_generation > state.observed_revoked_generation:
                    state.observed_revoked_generation = state.revoked_generation
                    state.queue_changed.clear()
                    _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
                if state.closed:
                    _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
                state.queue_changed.clear()
                if (
                    state.queue
                    or state.closed
                    or state.revoked_generation > state.observed_revoked_generation
                ):
                    continue
                await state.queue_changed.wait()
        finally:
            state.receiver_active = False

    async def submit_authorization_callback(
        self,
        request_id: str,
        callback_uri: str,
    ) -> None:
        state = _authorization_port_state(self)
        state.bind_loop()
        if (
            state.closed
            or type(request_id) is not str
            or type(callback_uri) is not str
            or state.redirect_uri is None
            or state.issuer is None
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        pending = state.pending.get(request_id)
        if pending is None or _clock_value(state.clock.monotonic()) > pending.request.expires_at:
            state.pending.pop(request_id, None)
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        result = _parse_callback_values(
            state.redirect_uri,
            state.issuer,
            callback_uri,
        )
        if not hmac.compare_digest(result.state, pending.expected_state):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        if state.pending.pop(request_id, None) is not pending or pending.future.done():
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        pending.future.set_result(result)

    async def close(self) -> None:
        _authorization_port_state(self).close()

    def __repr__(self) -> str:
        return "McpAuthorizationPresenter(<sealed>)"


@dataclass(frozen=True, slots=True, repr=False)
class _IssuedAuthorizationPort:
    port_ref: weakref.ReferenceType[_McpAuthorizationPort]
    proof: object
    state: _AuthorizationChannelState


_ISSUED_AUTHORIZATION_CHANNELS: weakref.WeakKeyDictionary[
    McpAuthorizationChannel,
    _IssuedAuthorizationChannel,
] = weakref.WeakKeyDictionary()
_ISSUED_AUTHORIZATION_PORTS: weakref.WeakKeyDictionary[
    _McpAuthorizationPort,
    _IssuedAuthorizationPort,
] = weakref.WeakKeyDictionary()


def _finalize_authorization_channel(state: _AuthorizationChannelState) -> None:
    loop = state.loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(state.close)
    else:
        state.close()


def create_mcp_authorization_channel(
    *,
    clock: McpAuthorizationClock | None = None,
    randomness: McpAuthorizationRandomness | None = None,
) -> tuple[
    McpAuthorizationChannel,
    McpAuthorizationPresenter,
]:
    """Issue a one-config provider capability and its revocable UI-side port."""
    selected_clock = _SYSTEM_AUTHORIZATION_CLOCK if clock is None else clock
    selected_randomness = _SYSTEM_AUTHORIZATION_RANDOMNESS if randomness is None else randomness
    if not isinstance(selected_clock, McpAuthorizationClock) or not isinstance(
        selected_randomness, McpAuthorizationRandomness
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return _issue_authorization_channel(
        _CHANNEL_FACTORY_ISSUER,
        clock=selected_clock,
        randomness=selected_randomness,
    )


def _issue_authorization_channel(
    issuer: object,
    *,
    clock: McpAuthorizationClock,
    randomness: McpAuthorizationRandomness,
) -> tuple[McpAuthorizationChannel, _McpAuthorizationPort]:
    if issuer is not _CHANNEL_FACTORY_ISSUER:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    proof = object()
    state = _AuthorizationChannelState(
        proof=proof,
        queue=deque(),
        queue_changed=asyncio.Event(),
        pending={},
        clock=clock,
        randomness=randomness,
    )
    channel = object.__new__(McpAuthorizationChannel)
    port = object.__new__(_McpAuthorizationPort)
    object.__setattr__(channel, "_proof", proof)
    object.__setattr__(port, "_proof", proof)
    _ISSUED_AUTHORIZATION_CHANNELS[channel] = _IssuedAuthorizationChannel(
        channel_ref=weakref.ref(channel),
        proof=proof,
        state=state,
    )
    _ISSUED_AUTHORIZATION_PORTS[port] = _IssuedAuthorizationPort(
        port_ref=weakref.ref(port),
        proof=proof,
        state=state,
    )
    weakref.finalize(channel, _finalize_authorization_channel, state)
    weakref.finalize(port, _finalize_authorization_channel, state)
    return channel, port


def _authorization_channel_record(channel: object) -> _IssuedAuthorizationChannel:
    if type(channel) is not McpAuthorizationChannel:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    record = _ISSUED_AUTHORIZATION_CHANNELS.get(channel)
    if (
        record is None
        or record.channel_ref() is not channel
        or channel._proof is not record.proof
        or record.state.proof is not record.proof
        or record.state.closed
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return record


def _authorization_port_state(port: object) -> _AuthorizationChannelState:
    if type(port) is not _McpAuthorizationPort:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    record = _ISSUED_AUTHORIZATION_PORTS.get(port)
    if (
        record is None
        or record.port_ref() is not port
        or port._proof is not record.proof
        or record.state.proof is not record.proof
        or record.state.closed
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return record.state


@runtime_checkable
class McpTokenStore(Protocol):
    """Opaque authorization persistence boundary."""

    async def load_tokens(self, storage_key: str) -> tuple[int, object] | None: ...

    async def store_tokens(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int | None: ...

    async def delete_tokens(
        self,
        storage_key: str,
        *,
        expected_revision: int,
    ) -> bool: ...

    async def load_client_info(self, storage_key: str) -> object | None: ...

    async def store_client_info(self, storage_key: str, client_info: object) -> None: ...

    async def claim_authorization_code(self, storage_key: str, code_sha256: str) -> bool: ...


class UnavailableMcpTokenStore:
    """Production placeholder until the encrypted MCP vault is connected."""

    @staticmethod
    def _unavailable() -> None:
        _fail(McpClientErrorReason.AUTHORIZATION_STORE_UNAVAILABLE)

    async def load_tokens(self, storage_key: str) -> tuple[int, object] | None:
        del storage_key
        self._unavailable()

    async def store_tokens(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int | None:
        del storage_key, tokens, expected_revision
        self._unavailable()

    async def delete_tokens(
        self,
        storage_key: str,
        *,
        expected_revision: int,
    ) -> bool:
        del storage_key, expected_revision
        self._unavailable()

    async def load_client_info(self, storage_key: str) -> object | None:
        del storage_key
        self._unavailable()

    async def store_client_info(self, storage_key: str, client_info: object) -> None:
        del storage_key, client_info
        self._unavailable()

    async def claim_authorization_code(self, storage_key: str, code_sha256: str) -> bool:
        del storage_key, code_sha256
        self._unavailable()

    def __repr__(self) -> str:
        return "UnavailableMcpTokenStore()"


@dataclass(slots=True)
class _AuthorizationOwner:
    lock: asyncio.Lock
    users: int = 0


_AUTHORIZATION_OWNER_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, _AuthorizationOwner],
] = weakref.WeakKeyDictionary()


@dataclass(slots=True)
class _AuthorizationOwnerLease:
    owners: dict[str, _AuthorizationOwner]
    storage_key: str
    owner: _AuthorizationOwner
    acquired: bool = False

    async def __aenter__(self) -> None:
        try:
            await self.owner.lock.acquire()
        except BaseException:
            self._release()
            raise
        self.acquired = True

    async def __aexit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> bool:
        self._release()
        return False

    def _release(self) -> None:
        if self.acquired:
            self.owner.lock.release()
            self.acquired = False
        self.owner.users -= 1
        if self.owner.users == 0 and self.owners.get(self.storage_key) is self.owner:
            self.owners.pop(self.storage_key, None)


def _authorization_owner(storage_key: str) -> _AuthorizationOwnerLease:
    loop = asyncio.get_running_loop()
    owners = _AUTHORIZATION_OWNER_LOCKS.setdefault(loop, {})
    owner = owners.setdefault(storage_key, _AuthorizationOwner(asyncio.Lock()))
    owner.users += 1
    return _AuthorizationOwnerLease(owners, storage_key, owner)


def _canonical_uri(
    value: object,
    *,
    metadata_document: bool = False,
    allow_origin: bool = False,
) -> str:
    """One canonical HTTPS URI, or a loopback redirect.

    ``allow_origin`` admits a URI that names no path. A RESOURCE identifier may
    be an origin -- RFC 8707 makes it a URI and Miro and Stripe both publish one
    -- while a redirect target and a metadata document always name something, so
    they keep the stricter reading.
    """
    invalid = False
    try:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        invalid = True
        parsed = urlsplit("")
        hostname = None
        port = None
    loopback = hostname is not None and hostname.casefold() in _LOOPBACK_HOSTS
    if loopback and not invalid:
        host_for_netloc = f"[{hostname}]" if hostname == "::1" else hostname
        expected_netloc = host_for_netloc if port is None else f"{host_for_netloc}:{port}"
        if (
            metadata_document
            or parsed.scheme != "http"
            or parsed.netloc != expected_netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.geturl() != value
            or port in {None, 0}
            or parsed.path == "/"
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        return value
    if (
        invalid
        or parsed.scheme != "https"
        or hostname is None
        or _HOST.fullmatch(hostname) is None
        or port in {0, 443}
        or parsed.netloc != (hostname if port is None else f"{hostname}:{port}")
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.path in {"", "/"} and not allow_origin)
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != value
        or metadata_document
        and not parsed.path.endswith(".json")
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return value


def _snapshot_descriptor(descriptor: object) -> McpConnectionDescriptor:
    if type(descriptor) is not McpConnectionDescriptor:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    invalid = False
    try:
        snapshot = McpConnectionDescriptor(
            app_id=descriptor.app_id,
            publisher_id=descriptor.publisher_id,
            deployment=descriptor.deployment,
            provider_id=descriptor.provider_id,
            protocol_version=descriptor.protocol_version,
            transport=descriptor.transport,
            verified_launch_sha256=descriptor.verified_launch_sha256,
            remote_origin=descriptor.remote_origin,
            authorization_issuer=descriptor.authorization_issuer,
            authentication=descriptor.authentication,
            declared_capabilities=dict(descriptor.declared_capabilities),
            extension_versions=dict(descriptor.extension_versions),
            capability_revision=descriptor.capability_revision,
            limits=McpClientLimits(**descriptor.limits.to_dict()),
            signed_capability_authority=descriptor.signed_capability_authority,
            runtime_compatibility=descriptor.runtime_compatibility,
        )
    except (AttributeError, TypeError, ValueError):
        invalid = True
        snapshot = None
    if invalid or snapshot is None:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return snapshot


def _requirement_from_record(record: InstalledMcpAppRecord) -> AppRequirement:
    return AppRequirement.from_dict(
        {
            "app_id": record.app_id,
            "publisher_id": record.publisher_id,
            "deployment": record.deployment.value,
            "provider_id": record.provider_id,
            "protocol_version": record.protocol_version,
            "transports": [transport.value for transport in record.transports],
            "remote_origin": record.remote_origin,
            "authorization_issuer": record.authorization_issuer,
            "authentication": record.authentication.to_dict(),
            "capabilities": [capability.to_dict() for capability in record.capabilities],
        }
    )


def _validated_signed_descriptor(
    descriptor: object,
    source: object,
) -> tuple[McpConnectionDescriptor, str]:
    snapshot = _snapshot_descriptor(descriptor)
    invalid = False
    try:
        record = InstalledMcpAppRecord.from_source(source)  # type: ignore[arg-type]
        requirement = _requirement_from_record(record)
        projection = project_declared_capabilities(requirement)
    except (AttributeError, TypeError, ValueError):
        invalid = True
        record = None
        projection = None
    if invalid or record is None or projection is None:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    declared_capabilities = {
        f"{capability.kind.value}:{capability.name}": capability.schema_sha256
        for capability in record.capabilities
        if capability.kind is not CapabilityKind.EXTENSION
    }
    extension_versions = {
        capability.name: capability.version
        for capability in record.capabilities
        if capability.kind is CapabilityKind.EXTENSION
    }
    descriptor_matches_record = declared_capabilities == dict(
        snapshot.declared_capabilities
    ) and extension_versions == dict(snapshot.extension_versions)
    projection_matches_record = (
        projection.snapshot.capability_revision == snapshot.capability_revision
    )
    if (
        type(source) is not TrustedRemoteAppSource
        or snapshot.deployment is not McpClientDeployment.REMOTE
        or snapshot.transport is not McpTransport.STREAMABLE_HTTP
        or record.deployment.value != snapshot.deployment.value
        or tuple(transport.value for transport in record.transports) != (snapshot.transport.value,)
        or record.app_id != snapshot.app_id
        or record.publisher_id != snapshot.publisher_id
        or record.provider_id != snapshot.provider_id
        or record.protocol_version != snapshot.protocol_version
        or record.remote_origin != snapshot.remote_origin
        or record.authorization_issuer != snapshot.authorization_issuer
        or record.authentication != snapshot.authentication
        or not descriptor_matches_record
        or not projection_matches_record
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return snapshot, sha256_hex(canonical_bytes(snapshot.to_dict()))


def _storage_key(
    *,
    descriptor_binding: str,
    authorization_context_revision: str,
    issuer: str,
    kind: str,
) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "authorization_context_revision": authorization_context_revision,
                "descriptor_binding": descriptor_binding,
                "issuer": issuer,
                "kind": kind,
            }
        )
    )


@dataclass(frozen=True, slots=True)
class _AuthorizationInputs:
    descriptor: McpConnectionDescriptor
    descriptor_binding: str
    authorization_context_revision: str
    resource_url: str
    issuer: str
    redirect_uri: str
    scopes: tuple[str, ...]
    authorization_channel: McpAuthorizationChannel
    channel_state: _AuthorizationChannelState
    token_store: McpTokenStore
    client_metadata_url: str | None
    client_id: str | None
    client_secret: str | None
    presentation_timeout_seconds: float


class _SdkTokenStorage(TokenStorage):
    def __init__(
        self,
        inputs: _AuthorizationInputs,
        guard: Callable[[], None],
    ) -> None:
        self._inputs = inputs
        self._guard = guard
        self._provider: OAuthClientProvider | None = None
        self._token_key = _storage_key(
            descriptor_binding=inputs.descriptor_binding,
            authorization_context_revision=inputs.authorization_context_revision,
            issuer=inputs.issuer,
            kind="tokens",
        )
        self._client_key = _storage_key(
            descriptor_binding=inputs.descriptor_binding,
            authorization_context_revision=inputs.authorization_context_revision,
            issuer=inputs.issuer,
            kind="client",
        )
        self._code_key = _storage_key(
            descriptor_binding=inputs.descriptor_binding,
            authorization_context_revision=inputs.authorization_context_revision,
            issuer=inputs.issuer,
            kind="authorization_code",
        )
        self.expected_refresh_token: str | None = None
        self.authorization_endpoint: str | None = None
        self.token_endpoint: str | None = None
        self.token_revision: int | None = None
        self.has_loaded_tokens = False

    def bind_provider(self, provider: OAuthClientProvider) -> None:
        self._provider = provider

    def bind_endpoints(self, authorization_endpoint: str, token_endpoint: str) -> None:
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint

    async def _load(self, method: str, key: str) -> object | None:
        failed = False
        try:
            return await getattr(self._inputs.token_store, method)(key)
        except Exception:
            failed = True
        if failed:
            _fail(McpClientErrorReason.AUTHORIZATION_STORE_UNAVAILABLE)

    async def _store(self, method: str, key: str, value: object) -> None:
        failed = False
        try:
            await getattr(self._inputs.token_store, method)(key, value)
            return
        except Exception:
            failed = True
        if failed:
            _fail(McpClientErrorReason.AUTHORIZATION_STORE_UNAVAILABLE)

    async def get_tokens(self) -> OAuthToken | None:
        self._guard()
        loaded = await self._load("load_tokens", self._token_key)
        self.has_loaded_tokens = True
        if loaded is None:
            self.token_revision = None
            self.expected_refresh_token = None
            return None
        if (
            type(loaded) is not tuple
            or len(loaded) != 2
            or type(loaded[0]) is not int
            or loaded[0] < 0
            or type(loaded[1]) is not _StoredTokens
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        revision, stored = loaded
        self.token_revision = revision
        invalid = False
        try:
            tokens = OAuthToken(
                access_token=stored.access_token,
                token_type=stored.token_type,
                expires_in=stored.expires_in,
                scope=stored.scope,
                refresh_token=stored.refresh_token,
            )
        except (TypeError, ValueError):
            invalid = True
            tokens = None
        if invalid or tokens is None:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        if self._provider is None:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        authorization_endpoint = _canonical_bound_endpoint(
            stored.authorization_endpoint,
            self._inputs.issuer,
        )
        token_endpoint = _canonical_bound_endpoint(
            stored.token_endpoint,
            self._inputs.issuer,
            allow_google_token_endpoint=_is_google_mcp_inputs(self._inputs),
        )
        self.bind_endpoints(authorization_endpoint, token_endpoint)
        self._provider.context.token_expiry_time = stored.expires_at
        self._provider.context.oauth_metadata = _BoundOAuthMetadata(
            issuer=self._inputs.issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
        )  # type: ignore[assignment]
        self._provider.context.protected_resource_metadata = _BoundProtectedResourceMetadata(
            resource=self._inputs.resource_url,
            authorization_servers=[self._inputs.issuer],
        )  # type: ignore[assignment]
        self.expected_refresh_token = stored.refresh_token
        return tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._guard()
        if type(tokens) is not OAuthToken:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        if self.authorization_endpoint is None or self.token_endpoint is None:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        expires_in = tokens.expires_in
        stored = _StoredTokens(
            access_token=tokens.access_token,
            token_type=tokens.token_type,
            expires_at=(
                None
                if expires_in is None
                else _clock_value(self._inputs.channel_state.clock.wall_time()) + expires_in
            ),
            expires_in=expires_in,
            scope=tokens.scope,
            refresh_token=tokens.refresh_token,
            authorization_endpoint=self.authorization_endpoint,
            token_endpoint=self.token_endpoint,
        )
        failed = False
        try:
            revision = await self._inputs.token_store.store_tokens(
                self._token_key,
                stored,
                expected_revision=self.token_revision,
            )
        except Exception:
            failed = True
            revision = None
        if failed:
            _fail(McpClientErrorReason.AUTHORIZATION_STORE_UNAVAILABLE)
        minimum_revision = 0 if self.token_revision is None else self.token_revision + 1
        if type(revision) is not int or revision < minimum_revision:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        self.token_revision = revision
        self.expected_refresh_token = tokens.refresh_token

    async def invalidate_tokens(self) -> None:
        self._guard()
        revision = self.token_revision
        if type(revision) is not int or revision < 0:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        failed = False
        try:
            deleted = await self._inputs.token_store.delete_tokens(
                self._token_key,
                expected_revision=revision,
            )
        except Exception:
            failed = True
            deleted = False
        if failed:
            _fail(McpClientErrorReason.AUTHORIZATION_STORE_UNAVAILABLE)
        if deleted is not True:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        self.token_revision = None
        self.expected_refresh_token = None

    async def synchronize_provider(self) -> None:
        self._guard()
        if self._provider is None:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        self._provider.context.current_tokens = await self.get_tokens()
        self._provider.context.client_info = await self.get_client_info()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        self._guard()
        stored = await self._load("load_client_info", self._client_key)
        expected_id = self._inputs.client_metadata_url or self._inputs.client_id
        expected_auth_method = (
            "client_secret_post" if self._inputs.client_secret is not None else "none"
        )
        if stored is None and self._inputs.client_id is not None:
            stored = _StoredClientInfo(
                client_id=self._inputs.client_id,
                issuer=self._inputs.issuer,
                redirect_uri=self._inputs.redirect_uri,
                token_endpoint_auth_method=expected_auth_method,
            )
            await self._store("store_client_info", self._client_key, stored)
        if stored is None:
            return None
        if (
            type(stored) is not _StoredClientInfo
            or stored.client_id != expected_id
            or stored.issuer != self._inputs.issuer
            or stored.redirect_uri != self._inputs.redirect_uri
            or stored.token_endpoint_auth_method != expected_auth_method
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        return OAuthClientInformationFull(
            client_id=stored.client_id,
            issuer=stored.issuer,
            redirect_uris=[stored.redirect_uri],
            client_secret=self._inputs.client_secret,
            token_endpoint_auth_method=expected_auth_method,
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._guard()
        expected_id = self._inputs.client_metadata_url or self._inputs.client_id
        expected_auth_method = (
            "client_secret_post" if self._inputs.client_secret is not None else "none"
        )
        if (
            type(client_info) is not OAuthClientInformationFull
            or client_info.client_id != expected_id
            or client_info.client_secret != self._inputs.client_secret
            or client_info.token_endpoint_auth_method != expected_auth_method
            or client_info.issuer != self._inputs.issuer
            or client_info.redirect_uris is None
            or [str(uri) for uri in client_info.redirect_uris] != [self._inputs.redirect_uri]
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        await self._store(
            "store_client_info",
            self._client_key,
            _StoredClientInfo(
                client_id=client_info.client_id,
                issuer=self._inputs.issuer,
                redirect_uri=self._inputs.redirect_uri,
                token_endpoint_auth_method=expected_auth_method,
            ),
        )

    async def claim_code(self, code: str) -> None:
        self._guard()
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        failed = False
        try:
            claimed = await self._inputs.token_store.claim_authorization_code(
                self._code_key,
                digest,
            )
        except Exception:
            failed = True
            claimed = False
        if failed:
            _fail(McpClientErrorReason.AUTHORIZATION_STORE_UNAVAILABLE)
        if claimed is not True:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)


def _resource_metadata_url(resource_url: str) -> str:
    parsed = urlsplit(resource_url)
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{parsed.path}"


def _google_mcp_host(origin: str | None) -> str | None:
    if type(origin) is not str:
        return None
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname not in _GOOGLE_MCP_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", _GOOGLE_MCP_TRANSPORT_PATH}
        or parsed.geturl() != origin
    ):
        return None
    return hostname


def _is_google_mcp_origin(origin: str | None) -> bool:
    return _google_mcp_host(origin) is not None


def _is_google_mcp_inputs(inputs: _AuthorizationInputs) -> bool:
    return _is_google_mcp_origin(inputs.descriptor.remote_origin)


def _is_google_mcp_transport_url(inputs: _AuthorizationInputs, url: str) -> bool:
    host = _google_mcp_host(inputs.descriptor.remote_origin)
    return host is not None and url == f"https://{host}{_GOOGLE_MCP_TRANSPORT_PATH}"


def _is_github_mcp_transport_url(inputs: _AuthorizationInputs, url: str) -> bool:
    origin = inputs.descriptor.remote_origin
    return origin == "https://api.githubcopilot.com/mcp/" and url == origin


def _is_resource_url(inputs: _AuthorizationInputs, url: str) -> bool:
    return (
        url == inputs.resource_url
        or _is_google_mcp_transport_url(inputs, url)
        or _is_github_mcp_transport_url(inputs, url)
    )


def _is_resource_metadata_url(inputs: _AuthorizationInputs, url: str) -> bool:
    if url == _resource_metadata_url(inputs.resource_url):
        return True
    host = _google_mcp_host(inputs.descriptor.remote_origin)
    if host is None:
        return False
    parsed = urlsplit(url)
    prefix = "/.well-known/oauth-protected-resource/"
    suffix = parsed.path.removeprefix(prefix)
    return (
        parsed.scheme == "https"
        and parsed.netloc == host
        and parsed.path.startswith(prefix)
        and bool(suffix)
        and "/" not in suffix
        and not parsed.query
        and not parsed.fragment
    )


def _authorization_metadata_url(issuer: str) -> str:
    parsed = urlsplit(issuer)
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{parsed.path}"


def _safe_json(response: httpx2.Response) -> dict[str, Any]:
    invalid = False
    try:
        value = json.loads(response.content)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        invalid = True
        value = None
    if invalid or type(value) is not dict:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return value


def _canonical_bound_endpoint(
    value: object,
    issuer: str,
    *,
    allow_google_token_endpoint: bool = False,
) -> str:
    invalid = False
    try:
        if type(value) is not str or value.strip() != value:
            raise ValueError
        parsed = urlsplit(value)
        issuer_parsed = urlsplit(issuer)
        port = parsed.port
    except (TypeError, ValueError):
        invalid = True
        parsed = urlsplit("")
        issuer_parsed = urlsplit("")
        port = None
    host_is_bound = parsed.netloc == issuer_parsed.netloc
    if allow_google_token_endpoint:
        host_is_bound = host_is_bound or (
            parsed.netloc == "oauth2.googleapis.com" and parsed.path == "/token"
        )
    if (
        invalid
        or parsed.scheme != "https"
        or parsed.scheme != issuer_parsed.scheme
        or not host_is_bound
        or (
            parsed.hostname != issuer_parsed.hostname
            and not (
                allow_google_token_endpoint
                and parsed.hostname == "oauth2.googleapis.com"
                and parsed.path == "/token"
            )
        )
        or port in {0, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != value
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return value


def _typed_unique_metadata_values(
    value: object,
    *,
    required: str,
) -> bool:
    return (
        type(value) is list
        and all(type(item) is str and item for item in value)
        and len(value) == len(set(value))
        and required in value
    )


def _single_query(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name)
    if values is None or len(values) != 1 or not values[0]:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return values[0]


def _root_normalised_resource(value: str) -> str:
    """Drop a lone root delimiter, which names no path at all.

    RFC 3986 s6.2.3: with an authority present, an empty path and "/" are
    equivalent. Miro publishes its resource as "https://mcp.miro.com/" and mints
    tokens whose audience is "https://mcp.miro.com"; those are one resource, and
    comparing the strings made them two. Nothing else is normalised -- a real
    path keeps its trailing slash, because "/mcp/" and "/mcp" are two paths and
    only the ROOT is a delimiter rather than a segment.
    """
    parsed = urlsplit(value)
    if parsed.path != "/" or parsed.query or parsed.fragment:
        return value
    return parsed._replace(path="").geturl()


def _audience_matches(value: object, resource_url: str) -> bool:
    expected = _root_normalised_resource(resource_url)
    if type(value) is str:
        return hmac.compare_digest(_root_normalised_resource(value), expected)
    return (
        type(value) is list
        and len(value) == 1
        and type(value[0]) is str
        and hmac.compare_digest(_root_normalised_resource(value[0]), expected)
    )


def _validate_token_audience(document: dict[str, Any], resource_url: str) -> None:
    audiences = [document[name] for name in ("audience", "resource", "aud") if name in document]
    if audiences and any(not _audience_matches(value, resource_url) for value in audiences):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    access_token = document.get("access_token")
    if audiences or type(access_token) is not str or access_token.count(".") != 2:
        return
    try:
        payload_segment = access_token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if type(payload) is not dict or "aud" not in payload:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    if not _audience_matches(payload["aud"], resource_url):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)


class _OAuthFlowValidator:
    def __init__(self, inputs: _AuthorizationInputs, storage: _SdkTokenStorage) -> None:
        self._inputs = inputs
        self._storage = storage
        self._resource_metadata_url = _resource_metadata_url(inputs.resource_url)
        self._authorization_metadata_url = _authorization_metadata_url(inputs.issuer)
        self._authorization_endpoint: str | None = None
        self._token_endpoint: str | None = None
        self._state: str | None = None
        self._code_challenge: str | None = None
        self._requested_scope: str | None = None

    def validate_authorization_url(self, authorization_url: object) -> None:
        self._restore_endpoints()
        invalid = False
        try:
            if type(authorization_url) is not str:
                raise ValueError
            parsed = urlsplit(authorization_url)
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except (TypeError, ValueError):
            invalid = True
            parsed = urlsplit("")
            query = {}
        if invalid:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        allowed = {
            "response_type",
            "client_id",
            "redirect_uri",
            "state",
            "code_challenge",
            "code_challenge_method",
            "resource",
            "scope",
        }
        if "prompt" in query:
            if _single_query(query, "prompt") != "consent":
                _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
            allowed.add("prompt")
        client_id = self._inputs.client_metadata_url or self._inputs.client_id
        state = _single_query(query, "state")
        challenge = _single_query(query, "code_challenge")
        scope = _single_query(query, "scope")
        scopes = scope.split(" ")
        if (
            self._authorization_endpoint is None
            or parsed._replace(query="").geturl() != self._authorization_endpoint
            or set(query) != allowed
            or _single_query(query, "response_type") != "code"
            or _single_query(query, "client_id") != client_id
            or _single_query(query, "redirect_uri") != self._inputs.redirect_uri
            or _single_query(query, "code_challenge_method") != "S256"
            or len(challenge) != 43
            or _single_query(query, "resource") != self._inputs.resource_url
            or len(scopes) != len(set(scopes))
            or set(self._inputs.scopes) != set(scopes)
            or any(_SCOPE.fullmatch(value) is None for value in scopes)
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        self._state = state
        self._code_challenge = challenge
        self._requested_scope = scope

    def validate_callback(self, result: AuthorizationCodeResult) -> None:
        if (
            self._state is None
            or result.state is None
            or not hmac.compare_digest(result.state, self._state)
            or result.iss != self._inputs.issuer
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    def validate_request(self, request: httpx2.Request) -> None:
        self._restore_endpoints()
        url = str(request.url)
        if _is_resource_url(self._inputs, url):
            return
        if _is_resource_metadata_url(self._inputs, url) or url == self._authorization_metadata_url:
            if request.method != "GET" or "Authorization" in request.headers:
                _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
            return
        if self._token_endpoint is not None and url == self._token_endpoint:
            self._validate_token_request(request)
            return
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    def _restore_endpoints(self) -> None:
        if self._authorization_endpoint is None:
            self._authorization_endpoint = self._storage.authorization_endpoint
        if self._token_endpoint is None:
            self._token_endpoint = self._storage.token_endpoint

    def _validate_token_request(self, request: httpx2.Request) -> None:
        invalid = False
        try:
            form = parse_qs(
                request.content.decode("ascii"),
                keep_blank_values=True,
                strict_parsing=True,
            )
        except (UnicodeDecodeError, ValueError):
            invalid = True
            form = {}
        if invalid:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        client_id = self._inputs.client_metadata_url or self._inputs.client_id
        grant_type = _single_query(form, "grant_type")
        common = {"grant_type", "client_id", "resource"}
        expected_secret = self._inputs.client_secret
        if expected_secret is None:
            if "client_secret" in form:
                _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        elif _single_query(form, "client_secret") != expected_secret:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        common_fields = common | ({"client_secret"} if expected_secret is not None else set())
        if (
            request.method != "POST"
            or request.headers.get("Content-Type") != "application/x-www-form-urlencoded"
            or "Authorization" in request.headers
            or _single_query(form, "client_id") != client_id
            or _single_query(form, "resource") != self._inputs.resource_url
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        if grant_type == "authorization_code":
            verifier = _single_query(form, "code_verifier")
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .decode("ascii")
                .rstrip("=")
            )
            if (
                set(form) != common_fields | {"code", "redirect_uri", "code_verifier"}
                or _single_query(form, "redirect_uri") != self._inputs.redirect_uri
                or self._code_challenge is None
                or not hmac.compare_digest(challenge, self._code_challenge)
            ):
                _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
            return
        if grant_type == "refresh_token":
            refresh_token = _single_query(form, "refresh_token")
            if (
                set(form) != common_fields | {"refresh_token"}
                or self._storage.expected_refresh_token is None
                or not hmac.compare_digest(
                    refresh_token,
                    self._storage.expected_refresh_token,
                )
            ):
                _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
            return
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    async def validate_response(
        self,
        request: httpx2.Request,
        response: httpx2.Response,
    ) -> httpx2.Response:
        url = str(request.url)
        if _is_resource_url(self._inputs, url):
            if response.status_code == 401:
                header = response.headers.get("WWW-Authenticate", "")
                matches = _RESOURCE_METADATA.findall(header)
                if _is_google_mcp_inputs(self._inputs):
                    if len(matches) != 1 or not _is_resource_metadata_url(self._inputs, matches[0]):
                        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
                elif matches != [self._resource_metadata_url]:
                    _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
            return response
        await response.aread()
        if _is_resource_metadata_url(self._inputs, url):
            return self._validate_resource_metadata(response)
        if url == self._authorization_metadata_url:
            self._validate_authorization_metadata(response)
            return response
        if url == self._token_endpoint:
            refresh = self._is_refresh_request(request)
            invalid_refresh = False
            try:
                self._validate_token_response(request, response)
            except McpClientError:
                if not refresh:
                    raise
                invalid_refresh = True
            if invalid_refresh:
                await self._storage.invalidate_tokens()
                return httpx2.Response(
                    400,
                    request=request,
                    content=b"",
                )
            return response
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    @staticmethod
    def _is_refresh_request(request: httpx2.Request) -> bool:
        invalid = False
        try:
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
        except (UnicodeDecodeError, ValueError):
            invalid = True
            form = {}
        if invalid:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        return form.get("grant_type") == ["refresh_token"]

    def _validate_resource_metadata(self, response: httpx2.Response) -> httpx2.Response:
        document = _safe_json(response)
        issuers = document.get("authorization_servers")
        normalized_issuers = (
            [value.rstrip("/") if type(value) is str else value for value in issuers]
            if type(issuers) is list
            else issuers
        )
        if (
            response.status_code != 200
            or document.get("resource") != self._inputs.resource_url
            or normalized_issuers != [self._inputs.issuer]
            or not self._supports_selected_scopes(document)
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        if _is_google_mcp_inputs(self._inputs) and normalized_issuers != issuers:
            document = dict(document)
            document["authorization_servers"] = normalized_issuers
            return httpx2.Response(
                response.status_code,
                headers=response.headers,
                json=document,
                request=response.request,
                extensions=response.extensions,
                history=response.history,
            )
        return response

    def _validate_authorization_metadata(self, response: httpx2.Response) -> None:
        document = _safe_json(response)
        authorization_endpoint = _canonical_bound_endpoint(
            document.get("authorization_endpoint"),
            self._inputs.issuer,
        )
        token_endpoint = _canonical_bound_endpoint(
            document.get("token_endpoint"),
            self._inputs.issuer,
            allow_google_token_endpoint=_is_google_mcp_inputs(self._inputs),
        )
        if (
            response.status_code != 200
            or document.get("issuer") != self._inputs.issuer
            or not _typed_unique_metadata_values(
                document.get("response_types_supported"),
                required="code",
            )
            or not _typed_unique_metadata_values(
                document.get("code_challenge_methods_supported"),
                required="S256",
            )
            or self._inputs.client_metadata_url is not None
            and document.get("client_id_metadata_document_supported") is not True
            or not self._supports_selected_scopes(document)
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        self._authorization_endpoint = authorization_endpoint
        self._token_endpoint = token_endpoint
        self._storage.bind_endpoints(authorization_endpoint, token_endpoint)

    def _supports_selected_scopes(self, document: Mapping[str, object]) -> bool:
        supported = document.get("scopes_supported")
        if supported is None:
            return True
        return (
            type(supported) is list
            and len(supported) == len(set(supported))
            and all(type(scope) is str and _SCOPE.fullmatch(scope) for scope in supported)
            and set(self._inputs.scopes).issubset(supported)
        )

    def _validate_token_response(
        self,
        request: httpx2.Request,
        response: httpx2.Response,
    ) -> None:
        document = _safe_json(response)
        invalid = False
        try:
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
            refresh = form.get("grant_type") == ["refresh_token"]
        except (UnicodeDecodeError, ValueError):
            invalid = True
            refresh = False
        if invalid:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        expected_statuses = {200} if refresh else {200, 201}
        access_token = document.get("access_token")
        expires_in = document.get("expires_in")
        scope = document.get("scope")
        required_scopes = set((self._requested_scope or " ".join(self._inputs.scopes)).split(" "))
        if (
            response.status_code not in expected_statuses
            or type(access_token) is not str
            or not access_token
            or document.get("token_type", "Bearer") != "Bearer"
            or expires_in is not None
            and (type(expires_in) is not int or expires_in <= 0)
            or scope is not None
            and (
                type(scope) is not str
                or any(_SCOPE.fullmatch(value) is None for value in scope.split(" "))
                or not required_scopes.issubset(scope.split(" "))
            )
            or document.get("refresh_token") is not None
            and type(document["refresh_token"]) is not str
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        _validate_token_audience(document, self._inputs.resource_url)


@dataclass(frozen=True, slots=True)
class _IssuedAuthorizationConfig:
    config_ref: weakref.ReferenceType[McpAuthorizationConfig]
    descriptor_binding: str
    provider: OAuthClientProvider
    inputs: _AuthorizationInputs
    proof: object
    fingerprint: tuple[object, ...]
    prototype: _ProviderBundle
    active_requests: weakref.WeakKeyDictionary[httpx2.Request, _ActiveAuthorizationRequest]
    active_proofs: dict[object, _ActiveAuthorizationRequest]
    cleanup_recovery_required: bool = False


_ISSUED_AUTHORIZATION_CONFIGS: weakref.WeakKeyDictionary[
    McpAuthorizationConfig,
    _IssuedAuthorizationConfig,
] = weakref.WeakKeyDictionary()


def _authorization_config_fingerprint(
    config: McpAuthorizationConfig,
    provider: OAuthClientProvider,
    inputs: _AuthorizationInputs,
    proof: object,
    prototype: _ProviderBundle,
) -> tuple[object, ...]:
    authority_binding = sha256_hex(
        canonical_bytes(
            {
                "authorization_context_revision": inputs.authorization_context_revision,
                "client_id": inputs.client_id,
                "client_secret_sha256": (
                    None
                    if inputs.client_secret is None
                    else sha256_hex(inputs.client_secret.encode("utf-8"))
                ),
                "client_metadata_url": inputs.client_metadata_url,
                "descriptor": inputs.descriptor.to_dict(),
                "descriptor_binding": inputs.descriptor_binding,
                "issuer": inputs.issuer,
                "presentation_timeout_seconds": inputs.presentation_timeout_seconds,
                "channel_identity": id(inputs.authorization_channel),
                "channel_proof_identity": id(inputs.authorization_channel._proof),
                "redirect_uri": inputs.redirect_uri,
                "resource_url": inputs.resource_url,
                "scopes": list(inputs.scopes),
                "store_identity": id(inputs.token_store),
            }
        )
    )
    return (
        id(config),
        id(provider),
        id(inputs),
        id(proof),
        authority_binding,
        _provider_prototype_fingerprint(prototype),
    )


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class McpAuthorizationConfig(httpx2.Auth):
    """Sealed OAuth adapter for one signed descriptor and account partition."""

    _provider: OAuthClientProvider
    _inputs: _AuthorizationInputs
    _proof: object

    @classmethod
    def for_client_metadata_document(
        cls,
        descriptor: McpConnectionDescriptor,
        trusted_source: TrustedRemoteAppSource,
        *,
        authorization_context_revision: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        presenter: McpAuthorizationChannel,
        client_metadata_url: str,
        token_store: McpTokenStore | None = None,
    ) -> McpAuthorizationConfig:
        return _issue_authorization_config(
            _AUTHORIZATION_CONFIG_ISSUER,
            descriptor,
            trusted_source,
            authorization_context_revision=authorization_context_revision,
            redirect_uri=redirect_uri,
            scopes=scopes,
            presenter=presenter,
            client_metadata_url=client_metadata_url,
            client_id=None,
            client_secret=None,
            token_store=token_store,
        )

    @classmethod
    def for_preregistered_client(
        cls,
        descriptor: McpConnectionDescriptor,
        trusted_source: TrustedRemoteAppSource,
        *,
        authorization_context_revision: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        presenter: McpAuthorizationChannel,
        client_id: str,
        client_secret: str | None = None,
        token_store: McpTokenStore | None = None,
    ) -> McpAuthorizationConfig:
        return _issue_authorization_config(
            _AUTHORIZATION_CONFIG_ISSUER,
            descriptor,
            trusted_source,
            authorization_context_revision=authorization_context_revision,
            redirect_uri=redirect_uri,
            scopes=scopes,
            presenter=presenter,
            client_metadata_url=None,
            client_id=client_id,
            client_secret=client_secret,
            token_store=token_store,
        )

    def __repr__(self) -> str:
        return "McpAuthorizationConfig(<sealed>)"

    async def async_auth_flow(
        self,
        request: httpx2.Request,
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        record = _authorization_config_record(self)
        async with _authorization_owner(record.prototype.storage._token_key):
            record = _authorization_config_record(self)
            bundle = _new_provider_bundle(
                record.inputs,
                lambda: _authorization_config_record(self),
            )
            bundle.storage._guard()
            provider_flow = bundle.provider.async_auth_flow(request)
            active_error: BaseException | None = None
            provider_completed = False
            owned_requests: list[httpx2.Request] = []
            try:
                provider_request = await anext(provider_flow)
                while True:
                    _bind_active_authorization_request(
                        record,
                        provider_request,
                        bundle,
                    )
                    owned_requests.append(provider_request)
                    try:
                        response = yield provider_request
                        bundle.storage._guard()
                        validated_response = await bundle.validator.validate_response(
                            provider_request,
                            response,
                        )
                    finally:
                        _unbind_active_authorization_request(record, provider_request)
                        owned_requests.remove(provider_request)
                    bundle.storage._guard()
                    provider_request = await provider_flow.asend(validated_response)
            except StopAsyncIteration:
                provider_completed = True
            except BaseException as error:
                active_error = error
            finally:
                for owned_request in owned_requests:
                    _unbind_active_authorization_request(record, owned_request)
                cleanup_error: BaseException | None = None
                try:
                    bundle.bridge.close()
                except BaseException as error:
                    cleanup_error = error
                flow_cleanup_error, flow_cleanup_complete = await _bounded_flow_close(
                    provider_flow,
                    record.inputs.presentation_timeout_seconds,
                )
                if cleanup_error is None:
                    cleanup_error = flow_cleanup_error
                if not flow_cleanup_complete or cleanup_error is not None:
                    object.__setattr__(record, "cleanup_recovery_required", True)
            if active_error is not None:
                raise active_error
            if cleanup_error is not None:
                if isinstance(cleanup_error, McpClientError):
                    raise cleanup_error
                if not isinstance(cleanup_error, Exception):
                    raise cleanup_error
                _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
            if provider_completed:
                return
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class McpBrokerAuthorizationConfig(httpx2.Auth):
    """Sealed token-only adapter for one current broker transport binding."""

    def __new__(cls) -> McpBrokerAuthorizationConfig:
        del cls
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    def __repr__(self) -> str:
        return "McpBrokerAuthorizationConfig(<sealed>)"

    def __copy__(self) -> McpBrokerAuthorizationConfig:
        del self
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    def __deepcopy__(self, memo: dict[int, object]) -> McpBrokerAuthorizationConfig:
        del self, memo
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    def __reduce__(self) -> object:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    def __getattribute__(self, name: str) -> object:
        if name == "__dict__":
            raise AttributeError("McpBrokerAuthorizationConfig has no instance dictionary")
        return super().__getattribute__(name)

    async def async_auth_flow(
        self,
        request: httpx2.Request,
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        record = _broker_authorization_record(self)
        owned_requests: list[httpx2.Request] = []
        try:
            _bind_broker_authorization_request(record, request, refresh_retry=False)
            owned_requests.append(request)
            token = await _broker_access_token(record, request)
            request.headers["authorization"] = f"Bearer {token}"
            del token
            response = yield request
            _unbind_broker_authorization_request(record, request)
            owned_requests.remove(request)
            if response.status_code != 401:
                return
            await _broker_refresh_after_rejection(record, request)
            retry = await _clone_without_sensitive_extensions(request)
            _bind_broker_authorization_request(record, retry, refresh_retry=True)
            owned_requests.append(retry)
            token = await _broker_access_token(record, retry)
            retry.headers["authorization"] = f"Bearer {token}"
            del token
            retry_response = yield retry
            if retry_response.status_code == 401:
                if _consume_marked_broker_transport_response(
                    record,
                    retry,
                    retry_response,
                ):
                    return
                _fail(McpClientErrorReason.AUTHORIZATION_REQUIRED)
        finally:
            for owned_request in owned_requests:
                _unbind_broker_authorization_request(record, owned_request)
            _strip_broker_request_secrets(request)


_WORKER_AUTHORIZATION_REQUEST_PROOF = "omega.worker.authorization_request_proof"


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class _McpWorkerAuthorizationConfig(httpx2.Auth):
    """One-process bearer capability received over the authenticated worker pipe."""

    def __new__(cls) -> _McpWorkerAuthorizationConfig:
        del cls
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    def __repr__(self) -> str:
        return "_McpWorkerAuthorizationConfig(<sealed>)"

    def __reduce__(self) -> object:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    async def async_auth_flow(
        self,
        request: httpx2.Request,
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        record = _worker_authorization_record(self)
        if str(request.url) != record.endpoint or "authorization" in request.headers:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        proof = object()
        request.extensions[_WORKER_AUTHORIZATION_REQUEST_PROOF] = proof
        record.active_requests[request] = proof
        request.headers["authorization"] = f"Bearer {record.access_token}"
        try:
            yield request
        finally:
            request.headers.pop("authorization", None)
            request.extensions.pop(_WORKER_AUTHORIZATION_REQUEST_PROOF, None)
            record.active_requests.pop(request, None)


@dataclass(slots=True, repr=False)
class _IssuedWorkerAuthorization:
    config_ref: weakref.ReferenceType[_McpWorkerAuthorizationConfig]
    descriptor_binding: str
    endpoint: str
    authorization_context_revision: str
    access_token: str
    proof: object
    fingerprint: tuple[object, ...]
    active_requests: weakref.WeakKeyDictionary[httpx2.Request, object]


_ISSUED_WORKER_AUTHORIZATIONS: weakref.WeakKeyDictionary[
    _McpWorkerAuthorizationConfig,
    _IssuedWorkerAuthorization,
] = weakref.WeakKeyDictionary()


def _worker_endpoint(descriptor: McpConnectionDescriptor) -> str:
    snapshot = _snapshot_descriptor(descriptor)
    if snapshot.deployment is not McpClientDeployment.REMOTE:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    origin = snapshot.remote_origin
    if type(origin) is not str:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    parsed = urlsplit(origin)
    # Same rule as the transport's own endpoint derivation: the origin IS the
    # endpoint. This once admitted three path literals -- which rejected
    # "/mcp", so Slack failed here after the operator had signed in -- and then
    # appended "/mcp" to a bare origin, which is how Miro and Stripe, the only
    # two providers serving at their root, answered the connect POST with 404.
    # Omega does not invent a path a provider did not publish.
    if parsed.path and _REMOTE_ORIGIN_PATH_RE.fullmatch(parsed.path) is None:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return origin


def _worker_authorization_fingerprint(
    config: _McpWorkerAuthorizationConfig,
    *,
    descriptor_binding: str,
    endpoint: str,
    authorization_context_revision: str,
    access_token: str,
    proof: object,
) -> tuple[object, ...]:
    return (
        id(config),
        descriptor_binding,
        endpoint,
        authorization_context_revision,
        sha256_hex(access_token.encode("utf-8")),
        id(proof),
    )


def _worker_authorization_rejection(
    access_token: object,
    authorization_context_revision: object,
) -> str | None:
    """Which check refuses this pair, or None if none does."""
    if type(access_token) is not str:
        return "access token is not a string"
    if not access_token:
        return "access token is empty"
    if len(access_token.encode("utf-8")) > 16_384:
        return f"access token is {len(access_token.encode('utf-8'))} bytes, over the 16384 bound"
    if any(ord(character) < 0x20 for character in access_token):
        return "access token carries a control character"
    if type(authorization_context_revision) is not str:
        return "authorization context revision is not a string"
    if _HEX_REVISION.fullmatch(authorization_context_revision) is None:
        return "authorization context revision is not a hex digest"
    return None


def _issue_worker_authorization_config(
    descriptor: McpConnectionDescriptor,
    *,
    access_token: object,
    authorization_context_revision: object,
) -> _McpWorkerAuthorizationConfig:
    snapshot = _snapshot_descriptor(descriptor)
    rejected = _worker_authorization_rejection(
        access_token,
        authorization_context_revision,
    )
    if rejected is not None:
        # The check, never a value: no token, no fragment of one, no provider
        # text. `authorization_invalid` covers six unrelated conditions and a
        # bare enum cannot say which of them, or whether the fault is Omega's
        # own context revision rather than anything the provider sent.
        _LOGGER.warning("worker authorization rejected: %s", rejected)
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    descriptor_binding = sha256_hex(canonical_bytes(snapshot.to_dict()))
    endpoint = _worker_endpoint(snapshot)
    config = object.__new__(_McpWorkerAuthorizationConfig)
    proof = object()
    fingerprint = _worker_authorization_fingerprint(
        config,
        descriptor_binding=descriptor_binding,
        endpoint=endpoint,
        authorization_context_revision=authorization_context_revision,
        access_token=access_token,
        proof=proof,
    )
    _ISSUED_WORKER_AUTHORIZATIONS[config] = _IssuedWorkerAuthorization(
        config_ref=weakref.ref(config),
        descriptor_binding=descriptor_binding,
        endpoint=endpoint,
        authorization_context_revision=authorization_context_revision,
        access_token=access_token,
        proof=proof,
        fingerprint=fingerprint,
        active_requests=weakref.WeakKeyDictionary(),
    )
    return config


def _worker_authorization_record(config: object) -> _IssuedWorkerAuthorization:
    if type(config) is not _McpWorkerAuthorizationConfig:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    record = _ISSUED_WORKER_AUTHORIZATIONS.get(config)
    if (
        record is None
        or record.config_ref() is not config
        or _worker_authorization_fingerprint(
            config,
            descriptor_binding=record.descriptor_binding,
            endpoint=record.endpoint,
            authorization_context_revision=record.authorization_context_revision,
            access_token=record.access_token,
            proof=record.proof,
        )
        != record.fingerprint
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return record


McpHttpAuthorization = (
    McpAuthorizationConfig | McpBrokerAuthorizationConfig | _McpWorkerAuthorizationConfig
)


def is_mcp_authorization_config(value: object) -> TypeGuard[McpHttpAuthorization]:
    """Return whether ``value`` is one of Omega's exact sealed auth types."""

    return type(value) in {
        McpAuthorizationConfig,
        McpBrokerAuthorizationConfig,
        _McpWorkerAuthorizationConfig,
    }


def _consume_task_result(task: asyncio.Task[object]) -> None:
    try:
        task.exception()
    except BaseException:
        pass


async def _bounded_flow_close(
    provider_flow: AsyncGenerator[httpx2.Request, httpx2.Response],
    timeout_seconds: float,
) -> tuple[BaseException | None, bool]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    cleanup = asyncio.create_task(provider_flow.aclose())
    caller_cancellation: BaseException | None = None
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            cleanup.cancel()
            cleanup.add_done_callback(_consume_task_result)
            return caller_cancellation, False
        try:
            done, _pending = await asyncio.wait({cleanup}, timeout=remaining)
        except BaseException as error:
            if caller_cancellation is None:
                caller_cancellation = error
            continue
        if not done:
            cleanup.cancel()
            cleanup.add_done_callback(_consume_task_result)
            return caller_cancellation, False
        try:
            cleanup.result()
        except BaseException as error:
            if caller_cancellation is None:
                caller_cancellation = error
        return caller_cancellation, True


def _valid_public_client_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 256
        or value.strip() != value
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return value


def _valid_desktop_client_secret(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 512
        or value.strip() != value
        or any(ord(character) < 0x20 for character in value)
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return value


def _authorization_inputs(
    descriptor: McpConnectionDescriptor,
    trusted_source: TrustedRemoteAppSource,
    *,
    authorization_context_revision: object,
    redirect_uri: object,
    scopes: object,
    presenter: object,
    client_metadata_url: object,
    client_id: object,
    client_secret: object,
    token_store: object,
) -> _AuthorizationInputs:
    snapshot, descriptor_binding = _validated_signed_descriptor(descriptor, trusted_source)
    if (
        type(authorization_context_revision) is not str
        or _HEX_REVISION.fullmatch(authorization_context_revision) is None
        or type(scopes) is not tuple
        or not scopes
        or any(type(scope) is not str or _SCOPE.fullmatch(scope) is None for scope in scopes)
        or len(scopes) != len(set(scopes))
        or token_store is not None
        and not isinstance(token_store, McpTokenStore)
        or (client_metadata_url is None) == (client_id is None)
        or client_metadata_url is not None
        and client_secret is not None
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    redirect = _canonical_uri(redirect_uri)
    metadata_url = (
        None
        if client_metadata_url is None
        else _canonical_uri(client_metadata_url, metadata_document=True)
    )
    preregistered_id = None if client_id is None else _valid_public_client_id(client_id)
    preregistered_secret = (
        None if client_id is None else _valid_desktop_client_secret(client_secret)
    )
    assert snapshot.remote_origin is not None
    assert snapshot.authorization_issuer is not None
    google_mcp_host = _google_mcp_host(snapshot.remote_origin)
    if google_mcp_host is not None:
        resource_url = f"https://{google_mcp_host}{_GOOGLE_MCP_RESOURCE_PATH}"
    else:
        parsed_origin = urlsplit(snapshot.remote_origin)
        if parsed_origin.path in {_GOOGLE_MCP_RESOURCE_PATH, f"{_GOOGLE_MCP_RESOURCE_PATH}/"}:
            resource_url = (
                f"{parsed_origin.scheme}://{parsed_origin.netloc}{_GOOGLE_MCP_RESOURCE_PATH}"
            )
        else:
            resource_url = f"{snapshot.remote_origin}/mcp"
    channel_state = _bind_authorization_channel(
        presenter,
        redirect_uri=redirect,
        issuer=snapshot.authorization_issuer,
    )
    return _AuthorizationInputs(
        descriptor=snapshot,
        descriptor_binding=descriptor_binding,
        authorization_context_revision=authorization_context_revision,
        resource_url=resource_url,
        issuer=snapshot.authorization_issuer,
        redirect_uri=redirect,
        scopes=scopes,
        authorization_channel=presenter,
        channel_state=channel_state,
        token_store=(UnavailableMcpTokenStore() if token_store is None else token_store),
        client_metadata_url=metadata_url,
        client_id=preregistered_id,
        client_secret=preregistered_secret,
        presentation_timeout_seconds=snapshot.limits.max_connection_duration_ms / 1_000,
    )


def _parse_callback(inputs: _AuthorizationInputs, callback_uri: object) -> AuthorizationCodeResult:
    return _parse_callback_values(inputs.redirect_uri, inputs.issuer, callback_uri)


def _parse_callback_values(
    redirect_uri: str,
    issuer: str,
    callback_uri: object,
    *,
    issuer_parameter_required: bool = True,
) -> AuthorizationCodeResult:
    invalid = False
    try:
        if type(callback_uri) is not str or type(issuer_parameter_required) is not bool:
            raise ValueError
        parsed = urlsplit(callback_uri)
        expected = urlsplit(redirect_uri)
        if parsed._replace(query="").geturl() != expected.geturl() or parsed.fragment:
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if not {"code", "state"}.issubset(query) or any(
            len(values) != 1 for values in query.values()
        ):
            raise ValueError
        code = query["code"][0]
        state = query["state"][0]
        issuer_values = query.get("iss")
        issuer_value = None if issuer_values is None else issuer_values[0]
        if (
            not code
            or not state
            or issuer_parameter_required
            and issuer_value is None
            or issuer_value is not None
            and issuer_value != issuer
        ):
            raise ValueError
        result = AuthorizationCodeResult(code=code, state=state, iss=issuer_value)
    except (TypeError, ValueError):
        invalid = True
        result = None
    if invalid or result is None:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return result


def parse_exact_callback(
    callback_uri: object,
    *,
    redirect_uri: str,
    expected_issuer: str,
    issuer_parameter_required: bool,
) -> AuthorizationCodeResult:
    """Parse one exact callback using the verified RFC 9207 metadata decision."""
    return _parse_callback_values(
        redirect_uri,
        expected_issuer,
        callback_uri,
        issuer_parameter_required=issuer_parameter_required,
    )


def _bind_authorization_channel(
    channel: object,
    *,
    redirect_uri: str,
    issuer: str,
) -> _AuthorizationChannelState:
    record = _authorization_channel_record(channel)
    state = record.state
    if state.bound or state.redirect_uri is not None or state.issuer is not None:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    state.bound = True
    state.redirect_uri = redirect_uri
    state.issuer = issuer
    return state


def _publish_authorization_request(
    state: _AuthorizationChannelState,
    authorization_url: str,
    timeout_seconds: float,
) -> _PendingAuthorization:
    loop = state.bind_loop()
    if state.closed or not state.bound or state.pending:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    for _attempt in range(_AUTHORIZATION_REQUEST_ID_ATTEMPTS):
        request_id = state.randomness.token_urlsafe(32)
        if (
            type(request_id) is not str
            or not request_id
            or request_id.strip() != request_id
            or len(request_id.encode("utf-8")) > 256
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        if request_id not in state.pending:
            break
    else:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    request = McpAuthorizationRequest(
        request_id=request_id,
        authorization_url=authorization_url,
        expires_at=_clock_value(state.clock.monotonic()) + timeout_seconds,
    )
    expected_state = _single_query(
        parse_qs(urlsplit(authorization_url).query, strict_parsing=True),
        "state",
    )
    if expected_state is None:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    pending = _PendingAuthorization(
        request=request,
        future=loop.create_future(),
        expected_state=expected_state,
        generation=state.next_generation,
    )
    state.next_generation += 1
    state.pending[request_id] = pending
    state.queue.append(pending)
    state.queue_changed.set()
    return pending


def _revoke_pending_authorization(
    state: _AuthorizationChannelState,
    pending: _PendingAuthorization,
) -> None:
    if state.pending.get(pending.request.request_id) is pending:
        del state.pending[pending.request.request_id]
    for index, queued in enumerate(state.queue):
        if queued is pending:
            del state.queue[index]
            break
    state.revoked_generation = max(state.revoked_generation, pending.generation)
    state.queue_changed.set()
    if not pending.future.done():
        pending.future.cancel()


async def _await_authorization_callback(
    state: _AuthorizationChannelState,
    pending: _PendingAuthorization,
) -> AuthorizationCodeResult:
    state.bind_loop()
    remaining = max(
        0.0,
        pending.request.expires_at - _clock_value(state.clock.monotonic()),
    )
    try:
        done, _pending = await asyncio.wait({pending.future}, timeout=remaining)
    except asyncio.CancelledError:
        _revoke_pending_authorization(state, pending)
        raise
    if not done:
        _revoke_pending_authorization(state, pending)
        _fail(McpClientErrorReason.REQUEST_TIMEOUT)
    value = pending.future.result()
    if value is _CHANNEL_CLOSED or type(value) is not AuthorizationCodeResult:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return value


@dataclass(slots=True)
class _ProviderChannelBridge:
    state: _AuthorizationChannelState
    validator: _OAuthFlowValidator
    storage: _SdkTokenStorage
    guard: Callable[[], None]
    pending: _PendingAuthorization | None = None

    async def redirect_handler(self, authorization_url: str) -> None:
        self.guard()
        self.validator.validate_authorization_url(authorization_url)
        if self.pending is not None:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        self.pending = _publish_authorization_request(
            self.state,
            authorization_url,
            self.storage._inputs.presentation_timeout_seconds,
        )

    async def callback_handler(self) -> AuthorizationCodeResult:
        self.guard()
        pending = self.pending
        if pending is None:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        try:
            result = await _await_authorization_callback(self.state, pending)
        finally:
            self.pending = None
        self.guard()
        self.validator.validate_callback(result)
        await self.storage.claim_code(result.code)
        return result

    async def validate_resource_url(
        self,
        _server_url: str,
        resource_url: str | None,
    ) -> None:
        self.guard()
        if resource_url != self.storage._inputs.resource_url:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    def close(self) -> None:
        if self.pending is not None:
            _revoke_pending_authorization(self.state, self.pending)
            self.pending = None


@dataclass(slots=True)
class _ProviderBundle:
    provider: OAuthClientProvider
    storage: _SdkTokenStorage
    validator: _OAuthFlowValidator
    bridge: _ProviderChannelBridge
    static_fingerprint: tuple[object, ...]


def _provider_static_fingerprint(bundle: _ProviderBundle) -> tuple[object, ...]:
    provider = bundle.provider
    context = provider.context
    metadata = context.client_metadata
    return (
        id(provider),
        id(context),
        context.server_url,
        id(metadata),
        id(context.storage),
        id(context.redirect_handler),
        id(context.callback_handler),
        context.client_metadata_url,
        id(bundle.storage),
        id(bundle.validator),
        id(bundle.bridge),
        id(bundle.storage._guard),
        id(context.lock),
    )


def _provider_prototype_fingerprint(bundle: _ProviderBundle) -> tuple[object, ...]:
    context = bundle.provider.context
    try:
        metadata_binding = sha256_hex(
            canonical_bytes(context.client_metadata.model_dump(mode="json"))
        )
    except (AttributeError, TypeError, ValueError):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return _provider_static_fingerprint(bundle) + (
        metadata_binding,
        context.protected_resource_metadata,
        context.oauth_metadata,
        context.auth_server_url,
        context.protocol_version,
        context.client_info,
        context.current_tokens,
        context.token_expiry_time,
        bundle.storage.expected_refresh_token,
        bundle.storage.authorization_endpoint,
        bundle.storage.token_endpoint,
        bundle.storage.token_revision,
        bundle.storage.has_loaded_tokens,
    )


def _new_provider_bundle(
    inputs: _AuthorizationInputs,
    config_guard: Callable[[], None],
) -> _ProviderBundle:
    guard_holder: list[Callable[[], None]] = [config_guard]

    def guard() -> None:
        guard_holder[0]()

    storage = _SdkTokenStorage(inputs, guard)
    validator = _OAuthFlowValidator(inputs, storage)
    bridge = _ProviderChannelBridge(
        state=inputs.channel_state,
        validator=validator,
        storage=storage,
        guard=guard,
    )
    metadata = OAuthClientMetadata(
        client_name="Omega",
        redirect_uris=[inputs.redirect_uri],
        token_endpoint_auth_method=(
            "client_secret_post" if inputs.client_secret is not None else "none"
        ),
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=" ".join(inputs.scopes),
    )
    provider = OAuthClientProvider(
        server_url=inputs.resource_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=bridge.redirect_handler,
        callback_handler=bridge.callback_handler,
        client_metadata_url=inputs.client_metadata_url,
        validate_resource_url=bridge.validate_resource_url,
    )
    storage.bind_provider(provider)
    bundle = _ProviderBundle(
        provider=provider,
        storage=storage,
        validator=validator,
        bridge=bridge,
        static_fingerprint=(),
    )
    bundle.static_fingerprint = _provider_static_fingerprint(bundle)

    def full_guard() -> None:
        config_guard()
        if _provider_static_fingerprint(bundle) != bundle.static_fingerprint:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)

    guard_holder[0] = full_guard
    bundle.static_fingerprint = _provider_static_fingerprint(bundle)
    return bundle


@dataclass(frozen=True, slots=True, repr=False)
class _AuthorizationRedirectGrant:
    proof: object
    request_binding: str


@dataclass(slots=True, repr=False)
class _ActiveAuthorizationRequest:
    proof: object
    bundle: _ProviderBundle
    current_request: httpx2.Request
    redirects_remaining: int
    request_pending: bool = True
    response_pending: bool = False
    redirect_grant: _AuthorizationRedirectGrant | None = None


@dataclass(slots=True)
class _ActiveBrokerAuthorizationRequest:
    proof: object
    current_request: httpx2.Request
    refresh_retry: bool
    request_pending: bool = True
    response_pending: bool = False
    rejection_marker: _BrokerRetryResponseMarker | None = None


@dataclass(slots=True)
class _IssuedBrokerAuthorization:
    config_ref: weakref.ReferenceType[McpBrokerAuthorizationConfig]
    binding: object
    broker: object
    authority: object
    prepared: object | None
    proof: object
    fingerprint: tuple[object, ...]
    binding_revision: str
    descriptor_binding: str | None
    active_requests: weakref.WeakKeyDictionary[
        httpx2.Request,
        _ActiveBrokerAuthorizationRequest,
    ]


class _BrokerRetryResponseMarker:
    __slots__ = ()

    def __repr__(self) -> str:
        return "_BrokerRetryResponseMarker(<sealed>)"


@dataclass(slots=True, repr=False, eq=False)
class _BrokerTransportFailure:
    config_ref: weakref.ReferenceType[McpBrokerAuthorizationConfig]
    operation: _BrokerTransportOperation
    reason: McpClientErrorReason
    marker: _BrokerRetryResponseMarker
    request_ref: weakref.ReferenceType[httpx2.Request]
    response_ref: weakref.ReferenceType[httpx2.Response]
    response_consumed: bool = False
    stream_consumed: bool = False


@dataclass(slots=True, repr=False, eq=False)
class _BrokerTransportOperation:
    config_ref: weakref.ReferenceType[McpBrokerAuthorizationConfig]
    failure: _BrokerTransportFailure | None = None
    active: bool = True


_ISSUED_BROKER_AUTHORIZATIONS: weakref.WeakKeyDictionary[
    McpBrokerAuthorizationConfig,
    _IssuedBrokerAuthorization,
] = weakref.WeakKeyDictionary()
_CURRENT_BROKER_TRANSPORT_OPERATION: ContextVar[_BrokerTransportOperation | None] = ContextVar(
    "omega_broker_transport_operation", default=None
)
_CURRENT_BROKER_TRANSPORT_RESPONSE: ContextVar[_BrokerTransportFailure | None] = ContextVar(
    "omega_broker_transport_response", default=None
)


def _broker_binding_values(binding: object) -> dict[str, str]:
    from mcp_platform.oauth_broker import BrokerTransportBinding

    if type(binding) is not BrokerTransportBinding:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    values = {
        "plugin_id": binding.plugin_id,
        "app_id": binding.app_id,
        "profile_id": binding.profile_id,
        "profile_revision": binding.profile_revision,
        "partition_key": binding.partition_key,
        "resource_url": binding.resource_url,
        "connection_revision": binding.connection_revision,
        "capability_revision": binding.capability_revision,
        "authority_revision": binding.authority_revision,
        "capability_binding_revision": binding.capability_binding_revision,
    }
    plain_values = (
        values["plugin_id"],
        values["app_id"],
        values["profile_id"],
    )
    revisions = (
        values["partition_key"],
        values["connection_revision"],
        values["capability_revision"],
        values["authority_revision"],
        values["capability_binding_revision"],
    )
    if (
        any(
            type(value) is not str
            or not value
            or value.strip() != value
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
            for value in plain_values
        )
        or any(
            type(value) is not str or _HEX_REVISION.fullmatch(value) is None for value in revisions
        )
        or type(values["profile_revision"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", values["profile_revision"]) is None
        or _canonical_uri(values["resource_url"], allow_origin=True)
        != values["resource_url"]
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return values


def _broker_prepared_values(prepared: object) -> dict[str, object]:
    from mcp_platform.oauth_broker import OAuthPreparedAuthorization

    if type(prepared) is not OAuthPreparedAuthorization:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    values: dict[str, object] = {
        "profile_id": prepared.profile_id,
        "plugin_id": prepared.plugin_id,
        "app_id": prepared.app_id,
        "account_key_sha256": prepared.account_key_sha256,
        "state_sha256": prepared.state_sha256,
        "partition_key": prepared.partition_key,
        "profile_revision": prepared.profile_revision,
        "authorization_context_revision": prepared.authorization_context_revision,
        "scopes": list(prepared.scopes),
    }
    if (
        any(
            type(values[name]) is not str
            or not values[name]
            or str(values[name]).strip() != values[name]
            for name in ("profile_id", "plugin_id", "app_id")
        )
        or any(
            type(values[name]) is not str or _HEX_REVISION.fullmatch(str(values[name])) is None
            for name in (
                "account_key_sha256",
                "state_sha256",
                "partition_key",
                "authorization_context_revision",
            )
        )
        or type(values["profile_revision"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(values["profile_revision"])) is None
        or type(values["scopes"]) is not list
        or not values["scopes"]
        or any(
            type(scope) is not str or not scope or scope.strip() != scope
            for scope in values["scopes"]
        )
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return values


def _broker_authorization_fingerprint(
    config: McpBrokerAuthorizationConfig,
    *,
    binding: object,
    broker: object,
    authority: object,
    prepared: object | None,
    proof: object,
) -> tuple[object, ...]:
    from mcp_platform.oauth_broker import _oauth_broker_authorization_identity

    return (
        id(config),
        id(binding),
        id(broker),
        _oauth_broker_authorization_identity(broker),
        id(authority),
        id(prepared),
        id(proof),
        sha256_hex(canonical_bytes(_broker_binding_values(binding))),
        (
            None
            if prepared is None
            else sha256_hex(canonical_bytes(_broker_prepared_values(prepared)))
        ),
    )


def _issue_broker_authorization_from_broker(
    *,
    binding: object,
    broker: object,
    authority: object,
    prepared: object | None = None,
) -> McpBrokerAuthorizationConfig:
    from mcp_platform.oauth_broker import (
        _oauth_broker_authorization_identity,
        _validate_broker_transport_authority,
        OAuthBroker,
    )

    if type(broker) is not OAuthBroker:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    try:
        _oauth_broker_authorization_identity(broker)
        _validate_broker_transport_authority(broker, authority, binding)
    except ValueError:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    binding_values = _broker_binding_values(binding)
    if prepared is not None:
        _broker_prepared_values(prepared)
    config = object.__new__(McpBrokerAuthorizationConfig)
    proof = object()
    fingerprint = _broker_authorization_fingerprint(
        config,
        binding=binding,
        broker=broker,
        authority=authority,
        prepared=prepared,
        proof=proof,
    )
    _ISSUED_BROKER_AUTHORIZATIONS[config] = _IssuedBrokerAuthorization(
        config_ref=weakref.ref(config),
        binding=binding,
        broker=broker,
        authority=authority,
        prepared=prepared,
        proof=proof,
        fingerprint=fingerprint,
        binding_revision=sha256_hex(canonical_bytes(binding_values)),
        descriptor_binding=None,
        active_requests=weakref.WeakKeyDictionary(),
    )
    return config


def _broker_authorization_record(config: object) -> _IssuedBrokerAuthorization:
    if type(config) is not McpBrokerAuthorizationConfig:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    record = _ISSUED_BROKER_AUTHORIZATIONS.get(config)
    if record is None or record.config_ref() is not config:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    try:
        current_fingerprint = _broker_authorization_fingerprint(
            config,
            binding=record.binding,
            broker=record.broker,
            authority=record.authority,
            prepared=record.prepared,
            proof=record.proof,
        )
    except (AttributeError, TypeError, ValueError):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    if current_fingerprint != record.fingerprint:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return record


def _validate_current_broker_binding(record: _IssuedBrokerAuthorization) -> None:
    from mcp_platform.oauth_broker import _validate_broker_transport_authority

    try:
        _validate_broker_transport_authority(
            record.broker,
            record.authority,
            record.binding,
        )
    except ValueError:
        raise McpClientError(McpClientErrorReason.IDENTITY_MISMATCH) from None


def _begin_broker_transport_operation(
    config: object,
) -> tuple[
    _BrokerTransportOperation,
    Token[_BrokerTransportOperation | None],
]:
    record = _broker_authorization_record(config)
    operation = _BrokerTransportOperation(config_ref=record.config_ref)
    return operation, _CURRENT_BROKER_TRANSPORT_OPERATION.set(operation)


def _finish_broker_transport_operation(
    config: object,
    operation: object,
    context_token: Token[_BrokerTransportOperation | None],
) -> _BrokerTransportFailure | None:
    try:
        record = _broker_authorization_record(config)
        if (
            type(operation) is not _BrokerTransportOperation
            or operation.config_ref() is not record.config_ref()
            or not operation.active
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        failure = operation.failure
        operation.failure = None
        operation.active = False
        return failure
    finally:
        _CURRENT_BROKER_TRANSPORT_OPERATION.reset(context_token)


def _mark_broker_transport_response(
    config: object,
    descriptor: McpConnectionDescriptor,
    response: httpx2.Response,
) -> _BrokerTransportFailure | None:
    if type(response) is not httpx2.Response:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    record = _broker_authorization_record(_authorization_descriptor_binding(config, descriptor))
    _validate_broker_request_target(record, response.request)
    active = record.active_requests.get(response.request)
    if (
        active is None
        or response.request.extensions.get(_AUTHORIZATION_REQUEST_PROOF) is not active.proof
        or active.current_request is not response.request
        or active.request_pending
        or not active.response_pending
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    if response.status_code != 401 or not active.refresh_retry:
        return None
    operation = _CURRENT_BROKER_TRANSPORT_OPERATION.get()
    if (
        operation is None
        or not operation.active
        or operation.config_ref() is not record.config_ref()
    ):
        return None
    if operation.failure is not None or active.rejection_marker is not None:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    marker = _BrokerRetryResponseMarker()
    active.rejection_marker = marker
    failure = _BrokerTransportFailure(
        config_ref=record.config_ref,
        operation=operation,
        reason=McpClientErrorReason.AUTHORIZATION_REQUIRED,
        marker=marker,
        request_ref=weakref.ref(response.request),
        response_ref=weakref.ref(response),
    )
    operation.failure = failure
    _CURRENT_BROKER_TRANSPORT_RESPONSE.set(failure)
    return failure


def _broker_transport_response_registration(
    config: object,
    proof: object,
) -> tuple[object, httpx2.Request, httpx2.Response]:
    record = _broker_authorization_record(config)
    operation = _CURRENT_BROKER_TRANSPORT_OPERATION.get()
    if (
        type(proof) is not _BrokerTransportFailure
        or operation is None
        or proof.operation is not operation
        or proof.config_ref() is not record.config_ref()
        or operation.config_ref() is not record.config_ref()
        or operation.failure is not proof
        or not operation.active
        or proof.response_consumed
        or proof.stream_consumed
        or _CURRENT_BROKER_TRANSPORT_RESPONSE.get() is not proof
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    request = proof.request_ref()
    response = proof.response_ref()
    if (
        request is None
        or response is None
        or response.request is not request
        or response.status_code != 401
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return operation, request, response


def _broker_transport_response_context_entry(
    config: object,
    context: object,
) -> tuple[object, object] | None:
    try:
        proof = context.get(_CURRENT_BROKER_TRANSPORT_RESPONSE)
        operation = context.get(_CURRENT_BROKER_TRANSPORT_OPERATION)
    except (AttributeError, TypeError):
        return None
    if proof is None:
        return None
    record = _broker_authorization_record(config)
    if (
        type(proof) is not _BrokerTransportFailure
        or type(operation) is not _BrokerTransportOperation
        or proof.operation is not operation
        or proof.config_ref() is not record.config_ref()
        or operation.config_ref() is not record.config_ref()
        or operation.failure is not proof
        or not operation.active
        or not proof.response_consumed
        or proof.stream_consumed
    ):
        return None
    return operation, proof


def _consume_broker_transport_response_proof(
    config: object,
    operation: object,
    proof: object,
    request: httpx2.Request,
    response: httpx2.Response,
) -> object:
    record = _broker_authorization_record(config)
    if (
        type(operation) is not _BrokerTransportOperation
        or type(proof) is not _BrokerTransportFailure
        or proof.operation is not operation
        or proof.config_ref() is not record.config_ref()
        or operation.config_ref() is not record.config_ref()
        or operation.failure is not proof
        or not operation.active
        or not proof.response_consumed
        or proof.stream_consumed
        or type(request) is not httpx2.Request
        or type(response) is not httpx2.Response
        or proof.request_ref() is not request
        or proof.response_ref() is not response
        or response.request is not request
        or response.status_code != 401
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    proof.stream_consumed = True
    return proof.marker


def _consume_marked_broker_transport_response(
    record: _IssuedBrokerAuthorization,
    request: httpx2.Request,
    response: httpx2.Response,
) -> bool:
    operation = _CURRENT_BROKER_TRANSPORT_OPERATION.get()
    if (
        operation is None
        or not operation.active
        or operation.config_ref() is not record.config_ref()
    ):
        return False
    failure = operation.failure
    active = record.active_requests.get(request)
    if (
        failure is None
        or failure.operation is not operation
        or failure.config_ref() is not record.config_ref()
        or active is None
        or not active.refresh_retry
        or active.rejection_marker is not failure.marker
        or failure.request_ref() is not request
        or failure.response_ref() is not response
        or response.request is not request
        or response.status_code != 401
        or failure.response_consumed
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    failure.response_consumed = True
    active.rejection_marker = None
    return True


def _validate_broker_request_target(
    record: _IssuedBrokerAuthorization,
    request: httpx2.Request,
) -> None:
    _validate_current_broker_binding(record)
    try:
        resource_url = _broker_binding_values(record.binding)["resource_url"]
        target = str(request.url)
    except (AttributeError, TypeError, ValueError):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    if request.method != "POST" or target != resource_url:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)


def _broker_error_reason(error: Exception) -> McpClientErrorReason:
    from mcp_platform.oauth_protocol import (
        OAuthNetworkError,
        OAuthProtocolError,
        OAuthProviderError,
    )

    if isinstance(error, OAuthNetworkError) or str(error) == "oauth_provider_unreachable":
        return McpClientErrorReason.TRANSPORT_UNAVAILABLE
    if isinstance(error, OAuthProviderError) or str(error) in {
        "oauth_provider_rejected",
        "oauth_prepared_authorization_invalid",
        "oauth_reauthorization_required",
        "oauth_revocation_pending",
        "oauth_token_partition_missing",
    }:
        return McpClientErrorReason.AUTHORIZATION_REQUIRED
    if isinstance(error, OAuthProtocolError):
        return McpClientErrorReason.AUTHORIZATION_INVALID
    if str(error) in {
        "oauth_transport_binding_invalid",
        "oauth_token_partition_invalid",
    }:
        return McpClientErrorReason.IDENTITY_MISMATCH
    return McpClientErrorReason.AUTHORIZATION_INVALID


async def _broker_access_token(
    record: _IssuedBrokerAuthorization,
    request: httpx2.Request,
) -> str:
    _validate_broker_request_target(record, request)
    try:
        token = (
            await record.broker.current_access_token(record.binding)
            if record.prepared is None
            else await record.broker._current_prepared_access_token(
                record.prepared,
                record.binding,
            )
        )
    except Exception as error:
        raise McpClientError(_broker_error_reason(error)) from None
    if (
        type(token) is not str
        or not token
        or len(token.encode("utf-8")) > 16_384
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
    ):
        raise McpClientError(McpClientErrorReason.AUTHORIZATION_INVALID) from None
    try:
        _validate_broker_request_target(record, request)
    except BaseException:
        del token
        raise
    return token


async def _broker_refresh_after_rejection(
    record: _IssuedBrokerAuthorization,
    request: httpx2.Request,
) -> None:
    _validate_broker_request_target(record, request)
    if record.prepared is not None:
        raise McpClientError(McpClientErrorReason.AUTHORIZATION_REQUIRED)
    try:
        await record.broker.refresh(
            record.binding.partition_key,
            binding=record.binding,
            force=True,
        )
    except Exception as error:
        raise McpClientError(_broker_error_reason(error)) from None
    _validate_broker_request_target(record, request)


def _strip_broker_request_secrets(request: httpx2.Request) -> None:
    request.headers.pop("authorization", None)
    request.headers.pop("proxy-authorization", None)
    request.extensions.pop(_AUTHORIZATION_REQUEST_PROOF, None)
    request.extensions.pop(_AUTHORIZATION_REDIRECT_PROOF, None)


async def _clone_without_sensitive_extensions(request: httpx2.Request) -> httpx2.Request:
    try:
        content = await request.aread()
        headers = [
            (name, value)
            for name, value in request.headers.multi_items()
            if name.lower() not in {"authorization", "proxy-authorization"}
        ]
        extensions: dict[str, object] = {}
        timeout = request.extensions.get("timeout")
        if type(timeout) is dict:
            extensions["timeout"] = dict(timeout)
        return httpx2.Request(
            request.method,
            request.url,
            headers=headers,
            content=content,
            extensions=extensions,
        )
    except (AttributeError, TypeError, ValueError):
        raise McpClientError(McpClientErrorReason.AUTHORIZATION_INVALID) from None


def _bind_broker_authorization_request(
    record: _IssuedBrokerAuthorization,
    request: httpx2.Request,
    *,
    refresh_retry: bool,
) -> None:
    _validate_broker_request_target(record, request)
    if request in record.active_requests:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    proof = object()
    request.extensions[_AUTHORIZATION_REQUEST_PROOF] = proof
    record.active_requests[request] = _ActiveBrokerAuthorizationRequest(
        proof=proof,
        current_request=request,
        refresh_retry=refresh_retry,
    )


def _unbind_broker_authorization_request(
    record: _IssuedBrokerAuthorization,
    request: httpx2.Request,
) -> None:
    record.active_requests.pop(request, None)
    _strip_broker_request_secrets(request)


def _broker_authorization_descriptor_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
    *,
    endpoint: str | None,
) -> McpBrokerAuthorizationConfig:
    record = _broker_authorization_record(config)
    _validate_current_broker_binding(record)
    snapshot = _snapshot_descriptor(descriptor)
    values = _broker_binding_values(record.binding)
    descriptor_binding = sha256_hex(canonical_bytes(snapshot.to_dict()))
    if (
        snapshot.deployment is not McpClientDeployment.REMOTE
        or snapshot.transport is not McpTransport.STREAMABLE_HTTP
        or snapshot.app_id != values["app_id"]
        or snapshot.capability_revision != values["capability_revision"]
        or endpoint is not None
        and endpoint != values["resource_url"]
        or record.descriptor_binding is not None
        and descriptor_binding != record.descriptor_binding
        or record.descriptor_binding is None
        and endpoint is None
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    if record.descriptor_binding is None:
        record.descriptor_binding = descriptor_binding
    return config


def _broker_authorization_request_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
    request: httpx2.Request,
) -> None:
    record = _broker_authorization_record(_authorization_descriptor_binding(config, descriptor))
    _validate_broker_request_target(record, request)
    active = record.active_requests.get(request)
    if (
        active is None
        or request.extensions.get(_AUTHORIZATION_REQUEST_PROOF) is not active.proof
        or active.current_request is not request
        or not active.request_pending
        or active.response_pending
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    active.request_pending = False
    active.response_pending = True


def _broker_authorization_response_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
    request: httpx2.Request,
) -> None:
    try:
        record = _broker_authorization_record(_authorization_descriptor_binding(config, descriptor))
        _validate_broker_request_target(record, request)
        active = record.active_requests.get(request)
        if (
            active is None
            or request.extensions.get(_AUTHORIZATION_REQUEST_PROOF) is not active.proof
            or active.current_request is not request
            or active.request_pending
            or not active.response_pending
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        active.response_pending = False
    finally:
        _strip_broker_request_secrets(request)


def _broker_authorization_redirect_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
    response: httpx2.Response,
) -> None:
    record = _broker_authorization_record(_authorization_descriptor_binding(config, descriptor))
    _validate_broker_request_target(record, response.request)
    _strip_broker_request_secrets(response.request)
    _fail(McpClientErrorReason.IDENTITY_MISMATCH)


def _issue_authorization_config(
    issuer: object,
    descriptor: McpConnectionDescriptor,
    trusted_source: TrustedRemoteAppSource,
    *,
    authorization_context_revision: object,
    redirect_uri: object,
    scopes: object,
    presenter: object,
    client_metadata_url: object,
    client_id: object,
    client_secret: object,
    token_store: object,
) -> McpAuthorizationConfig:
    if issuer is not _AUTHORIZATION_CONFIG_ISSUER:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    inputs = _authorization_inputs(
        descriptor,
        trusted_source,
        authorization_context_revision=authorization_context_revision,
        redirect_uri=redirect_uri,
        scopes=scopes,
        presenter=presenter,
        client_metadata_url=client_metadata_url,
        client_id=client_id,
        client_secret=client_secret,
        token_store=token_store,
    )
    prototype = _new_provider_bundle(inputs, lambda: None)
    provider = prototype.provider
    config = object.__new__(McpAuthorizationConfig)
    proof = object()
    object.__setattr__(config, "_provider", provider)
    object.__setattr__(config, "_inputs", inputs)
    object.__setattr__(config, "_proof", proof)
    fingerprint = _authorization_config_fingerprint(
        config,
        provider,
        inputs,
        proof,
        prototype,
    )
    _ISSUED_AUTHORIZATION_CONFIGS[config] = _IssuedAuthorizationConfig(
        config_ref=weakref.ref(config),
        descriptor_binding=inputs.descriptor_binding,
        provider=provider,
        inputs=inputs,
        proof=proof,
        fingerprint=fingerprint,
        prototype=prototype,
        active_requests=weakref.WeakKeyDictionary(),
        active_proofs={},
    )
    return config


def _authorization_config_record(config: object) -> _IssuedAuthorizationConfig:
    if type(config) is not McpAuthorizationConfig:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    record = _ISSUED_AUTHORIZATION_CONFIGS.get(config)
    invalid = (
        record is None
        or record.config_ref() is not config
        or config._provider is not record.provider
        or config._inputs is not record.inputs
        or config._proof is not record.proof
    )
    if not invalid:
        try:
            invalid = (
                _authorization_config_fingerprint(
                    config,
                    config._provider,
                    config._inputs,
                    config._proof,
                    record.prototype,
                )
                != record.fingerprint
            )
        except (AttributeError, TypeError, ValueError):
            invalid = True
    if invalid:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    assert record is not None
    channel_record = _authorization_channel_record(record.inputs.authorization_channel)
    if (
        channel_record.state is not record.inputs.channel_state
        or not channel_record.state.bound
        or channel_record.state.redirect_uri != record.inputs.redirect_uri
        or channel_record.state.issuer != record.inputs.issuer
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return record


def _authorization_descriptor_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
    *,
    endpoint: str | None = None,
) -> McpHttpAuthorization:
    if type(config) is McpBrokerAuthorizationConfig:
        return _broker_authorization_descriptor_binding(
            config,
            descriptor,
            endpoint=endpoint,
        )
    if type(config) is _McpWorkerAuthorizationConfig:
        record = _worker_authorization_record(config)
        snapshot = _snapshot_descriptor(descriptor)
        binding = sha256_hex(canonical_bytes(snapshot.to_dict()))
        if (
            binding != record.descriptor_binding
            or endpoint is not None
            and endpoint != record.endpoint
        ):
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        return config
    record = _authorization_config_record(config)
    snapshot = _snapshot_descriptor(descriptor)
    binding = sha256_hex(canonical_bytes(snapshot.to_dict()))
    if binding != record.descriptor_binding:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return config


async def _authorization_request_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
    request: httpx2.Request,
) -> bool:
    if type(config) is McpBrokerAuthorizationConfig:
        _broker_authorization_request_binding(config, descriptor, request)
        return False
    if type(config) is _McpWorkerAuthorizationConfig:
        record = _worker_authorization_record(
            _authorization_descriptor_binding(config, descriptor)
        )
        proof = record.active_requests.get(request)
        if (
            proof is None
            or request.extensions.get(_WORKER_AUTHORIZATION_REQUEST_PROOF) is not proof
            or str(request.url) != record.endpoint
            or request.headers.get("authorization")
            != f"Bearer {record.access_token}"
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        return False
    record = _authorization_config_record(_authorization_descriptor_binding(config, descriptor))
    active = record.active_requests.get(request)
    proof = request.extensions.get(_AUTHORIZATION_REQUEST_PROOF)
    redirected = active is None
    if redirected:
        active = record.active_proofs.get(proof)
    if active is None or proof is not active.proof:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    active.bundle.storage._guard()
    active.bundle.validator.validate_request(request)
    if redirected:
        grant = active.redirect_grant
        if (
            grant is None
            or request.extensions.get(_AUTHORIZATION_REDIRECT_PROOF) is not grant.proof
            or await _authorization_request_lineage_binding(request) != grant.request_binding
            or active.request_pending
            or active.response_pending
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        active.redirect_grant = None
        active.current_request = request
        record.active_requests[request] = active
        request.extensions.pop(_AUTHORIZATION_REDIRECT_PROOF, None)
    elif (
        active.current_request is not request
        or not active.request_pending
        or active.response_pending
        or active.redirect_grant is not None
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    active.request_pending = False
    active.response_pending = True
    return redirected


def _authorization_response_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
    request: httpx2.Request,
) -> None:
    if type(config) is McpBrokerAuthorizationConfig:
        _broker_authorization_response_binding(config, descriptor, request)
        return
    if type(config) is _McpWorkerAuthorizationConfig:
        record = _worker_authorization_record(
            _authorization_descriptor_binding(config, descriptor)
        )
        proof = record.active_requests.get(request)
        if (
            proof is None
            or request.extensions.get(_WORKER_AUTHORIZATION_REQUEST_PROOF) is not proof
            or str(request.url) != record.endpoint
        ):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        return
    record = _authorization_config_record(_authorization_descriptor_binding(config, descriptor))
    active = record.active_requests.get(request)
    if (
        active is None
        or request.extensions.get(_AUTHORIZATION_REQUEST_PROOF) is not active.proof
        or active.current_request is not request
        or active.request_pending
        or not active.response_pending
        or active.redirect_grant is not None
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    active.bundle.storage._guard()
    active.bundle.validator.validate_request(request)
    active.response_pending = False


#: The Streamable HTTP session header, spelled once for the whole package.
#: Bound below so a redirect can neither add nor change one, and echoed by the
#: maintainer capture CLI, which hand-rolls a handshake outside the SDK and so
#: has to satisfy servers that issue a session and require it back. Neither use
#: reads it as authority -- that stays with the SDK session.
MCP_SESSION_HEADER = "mcp-session-id"


async def _authorization_request_lineage_binding(request: httpx2.Request) -> str:
    invalid = False
    try:
        content = await request.aread()
        if type(content) is not bytes:
            raise TypeError
        authority_headers = {
            name: sha256_hex(request.headers.get(name, "").encode("utf-8"))
            for name in (
                "accept",
                "authorization",
                "content-type",
                "mcp-protocol-version",
                MCP_SESSION_HEADER,
            )
        }
        binding = sha256_hex(
            canonical_bytes(
                {
                    "content_sha256": sha256_hex(content),
                    "headers": authority_headers,
                    "method": request.method,
                    "url": str(request.url),
                }
            )
        )
    except (AttributeError, TypeError, ValueError):
        invalid = True
        binding = ""
    if invalid:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    return binding


async def _authorization_redirect_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
    response: httpx2.Response,
) -> None:
    if type(config) is McpBrokerAuthorizationConfig:
        _broker_authorization_redirect_binding(config, descriptor, response)
        return
    if type(config) is _McpWorkerAuthorizationConfig:
        del descriptor, response
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    record = _authorization_config_record(_authorization_descriptor_binding(config, descriptor))
    request = response.request
    active = record.active_requests.get(request)
    location = response.headers.get("location")
    target = request.url.join(location) if location is not None else None
    if (
        active is None
        or request.extensions.get(_AUTHORIZATION_REQUEST_PROOF) is not active.proof
        or active.current_request is not request
        or active.request_pending
        or active.response_pending
        or active.redirect_grant is not None
        or active.redirects_remaining <= 0
        or response.status_code not in {307, 308}
        or request.method != "POST"
        or location not in {urlsplit(record.inputs.resource_url).path, record.inputs.resource_url}
        or target is None
        or str(target) != record.inputs.resource_url
    ):
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    active.bundle.storage._guard()
    active.bundle.validator.validate_request(request)
    redirect_proof = object()
    active.redirects_remaining -= 1
    active.redirect_grant = _AuthorizationRedirectGrant(
        proof=redirect_proof,
        request_binding=await _authorization_request_lineage_binding(request),
    )
    request.extensions[_AUTHORIZATION_REDIRECT_PROOF] = redirect_proof


def _bind_active_authorization_request(
    record: _IssuedAuthorizationConfig,
    request: httpx2.Request,
    bundle: _ProviderBundle,
) -> None:
    bundle.storage._guard()
    if request in record.active_requests:
        _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
    bundle.validator.validate_request(request)
    proof = object()
    request.extensions[_AUTHORIZATION_REQUEST_PROOF] = proof
    active = _ActiveAuthorizationRequest(
        proof=proof,
        bundle=bundle,
        current_request=request,
        redirects_remaining=record.inputs.descriptor.limits.max_http_redirects,
    )
    record.active_requests[request] = active
    record.active_proofs[proof] = active


def _unbind_active_authorization_request(
    record: _IssuedAuthorizationConfig,
    request: httpx2.Request,
) -> None:
    active = record.active_requests.get(request)
    if active is None:
        active = record.active_proofs.get(request.extensions.get(_AUTHORIZATION_REQUEST_PROOF))
    if active is None:
        return
    for owned_request, owned_active in tuple(record.active_requests.items()):
        if owned_active is active:
            record.active_requests.pop(owned_request, None)
            owned_request.extensions.pop(_AUTHORIZATION_REQUEST_PROOF, None)
            owned_request.extensions.pop(_AUTHORIZATION_REDIRECT_PROOF, None)
    if record.active_proofs.get(active.proof) is active:
        record.active_proofs.pop(active.proof, None)


def _authorization_transport_binding(
    config: object,
    descriptor: McpConnectionDescriptor,
) -> tuple[object, ...]:
    if type(config) is McpBrokerAuthorizationConfig:
        record = _broker_authorization_record(_authorization_descriptor_binding(config, descriptor))
        return record.fingerprint + (record.descriptor_binding,)
    if type(config) is _McpWorkerAuthorizationConfig:
        record = _worker_authorization_record(
            _authorization_descriptor_binding(config, descriptor)
        )
        return record.fingerprint
    record = _authorization_config_record(_authorization_descriptor_binding(config, descriptor))
    return record.fingerprint


def _authorization_context_revision(
    config: object,
    descriptor: McpConnectionDescriptor,
) -> str:
    if type(config) is McpBrokerAuthorizationConfig:
        record = _broker_authorization_record(_authorization_descriptor_binding(config, descriptor))
        return record.binding_revision
    if type(config) is _McpWorkerAuthorizationConfig:
        record = _worker_authorization_record(
            _authorization_descriptor_binding(config, descriptor)
        )
        return record.authorization_context_revision
    record = _authorization_config_record(_authorization_descriptor_binding(config, descriptor))
    return record.inputs.authorization_context_revision


__all__ = [
    "McpAuthorizationChannel",
    "McpAuthorizationClock",
    "McpAuthorizationConfig",
    "McpAuthorizationPresenter",
    "McpAuthorizationRandomness",
    "McpAuthorizationRequest",
    "McpBrokerAuthorizationConfig",
    "McpHttpAuthorization",
    "McpTokenStore",
    "UnavailableMcpTokenStore",
    "create_mcp_authorization_channel",
    "is_mcp_authorization_config",
    "parse_exact_callback",
]
