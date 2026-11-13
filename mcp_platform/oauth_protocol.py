"""Bounded, provider-neutral OAuth discovery and token protocol client."""

from __future__ import annotations

import asyncio
import logging
import json
import math
import re
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin, urlsplit

import httpx2

from mcp_platform.client_contracts import McpClientLimits
from mcp_platform.canonical import canonical_bytes
from mcp_platform.oauth_profiles import (
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthProfile,
)
from mcp_platform.oauth_vault import OAuthClientCredential
from mcp_platform.transports.streamable_http import build_bounded_http_client


_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z"
)
_LOG = logging.getLogger("omega.mcp.oauth")
_SCOPE = re.compile(r"[A-Za-z0-9._~:/-]+\Z")
_MAX_JSON_DEPTH = 64
_MAX_SCOPE_COUNT = 128
#: An absolute expiry far enough out to be a real timestamp and no further.
_MAX_EXPIRES_AT_SECONDS = 4_102_444_800  # 2100-01-01T00:00:00Z
_MAX_SCOPE_SERIALIZATION_BYTES = 32_768
_MAX_TOKEN_BYTES = 16_384
_MAX_TOKEN_TYPE_BYTES = 64
_MAX_EXPIRES_IN_SECONDS = 2_147_483_647
_DISCOVERY_RESPONSE_FIELDS = (
    "profile_id",
    "profile_revision",
    "client_mode",
    "client_identifier",
    "client_secret_advertised",
    "redirect_uri",
    "issuer",
    "resource_origin",
    "authorization_endpoint",
    "token_endpoint",
    "revocation_endpoint",
    "registration_endpoint",
    "code_challenge_methods_supported",
    "authorization_response_iss_parameter_supported",
    "scopes_supported",
    "authorization_flow",
    "device_authorization_endpoint",
    "grant_types_supported",
)
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthProtocolError(ValueError):
    """A content-free OAuth protocol contract failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.reason!r})"


class OAuthNetworkError(OAuthProtocolError):
    """The provider could not be reached within the signed descriptor limits."""


class OAuthProviderError(OAuthProtocolError):
    """A reachable provider rejected the protocol operation."""


def _fail(reason: str) -> None:
    raise OAuthProtocolError(reason)


def _stable_text(value: object, *, reason: str, maximum: int = 4096) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        _fail(reason)
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise OAuthProtocolError(reason) from None
    if encoded_length > maximum:
        _fail(reason)
    return value


def _scopes(value: object, *, reason: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value or len(value) > _MAX_SCOPE_COUNT:
        _fail(reason)
    checked = tuple(_stable_text(item, reason=reason, maximum=256) for item in value)
    if (
        sum(len(scope.encode("utf-8")) for scope in checked) > _MAX_SCOPE_SERIALIZATION_BYTES
        or len(checked) != len(set(checked))
        or any(_SCOPE.fullmatch(scope) is None for scope in checked)
    ):
        _fail(reason)
    return checked


def _json_depth_is_bounded(body: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return False
        elif byte in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                return False
    return True


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _json_object(body: bytes) -> dict[str, Any]:
    if not _json_depth_is_bounded(body):
        _fail("oauth_response_invalid")
    try:
        value = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        OverflowError,
    ):
        raise OAuthProtocolError("oauth_response_invalid") from None
    if type(value) is not dict:
        _fail("oauth_response_invalid")
    return value


def _clock_value(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _fail("oauth_clock_invalid")
    return float(value)


def _required_token_text(value: object) -> str:
    try:
        return _stable_text(
            value,
            reason="oauth_token_invalid",
            maximum=_MAX_TOKEN_BYTES,
        )
    except OAuthProtocolError:
        raise OAuthProtocolError("oauth_token_invalid") from None


def _optional_token_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_token_text(value)


def _bearer_token_type(value: object) -> str:
    try:
        token_type = _stable_text(
            value,
            reason="oauth_token_invalid",
            maximum=_MAX_TOKEN_TYPE_BYTES,
        )
    except OAuthProtocolError:
        raise OAuthProtocolError("oauth_token_invalid") from None
    if token_type.casefold() != "bearer":
        _fail("oauth_token_invalid")
    return "Bearer"


#: The token-endpoint authentication methods Omega can actually perform. The
#: form-encoded secret is what `_token_request` sends; assertion-based methods
#: (``private_key_jwt`` and friends) need a key Omega was never issued.
_CONFIDENTIAL_AUTH_METHODS = frozenset({"client_secret_post", "client_secret_basic"})


def _requires_client_authentication(discovery: OAuthDiscovery) -> bool:
    """Does this server's token endpoint refuse an unauthenticated client?

    Only a STATED list counts. An absent one is not a claim that a secret is
    required, and reading it as one would start sending secrets to every
    provider that simply says nothing -- most of them.
    """
    methods = discovery.token_endpoint_auth_methods_supported
    return bool(methods) and "none" not in methods


def _optional_client_secret(value: object) -> str | None:
    if value is None or value == "":
        return None
    return _stable_text(value, reason="oauth_registration_invalid", maximum=1024)


def _optional_secret_expiry(value: object) -> float | None:
    """RFC 7591 states this in seconds since the epoch; 0 means it never expires."""
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > _MAX_EXPIRES_AT_SECONDS:
        _fail("oauth_registration_invalid")
    return None if value == 0 else float(value)


def _returned_scopes(
    value: object,
    required_scopes: tuple[str, ...],
) -> tuple[str, ...]:
    if value is None or (type(value) is str and not value.strip()):
        # RFC 6749 s5.1 makes `scope` OPTIONAL and requires it only when the
        # granted scope differs from the requested one. A provider that sends an
        # empty string has stated nothing, which is what omitting it means -- and
        # refusing it instead is a mismatch invented out of an absence.
        return required_scopes
    try:
        serialized = _stable_text(
            value,
            reason="oauth_scope_mismatch",
            maximum=_MAX_SCOPE_SERIALIZATION_BYTES,
        )
    except OAuthProtocolError:
        _LOG.warning(
            "oauth_scope_mismatch: the returned scope is unreadable "
            "(%d bytes, requested=%d)",
            len(str(value)),
            len(required_scopes),
        )
        raise OAuthProtocolError("oauth_scope_mismatch") from None
    # Evidence, not a record: split loosely, check coverage, discard. RFC 6749
    # s3.3 separates scopes with spaces and some servers use commas, and neither
    # spelling has to satisfy Omega's own scope grammar because not one of these
    # strings is kept.
    granted = {part for part in serialized.replace(",", " ").split() if part}
    missing = set(required_scopes) - granted
    if missing:
        # Counts only -- the runtime log never repeats a scope the provider
        # chose. How many are missing is what says whether the package asks for
        # something the operator did not approve.
        _LOG.warning(
            "oauth_scope_mismatch: requested=%d granted=%d missing=%d",
            len(required_scopes),
            len(granted),
            len(missing),
        )
        _fail("oauth_scope_mismatch")
    # What Omega records is what the signed package declared and asked for. A
    # provider that grants MORE -- Cloudflare grants 384 for a request of one --
    # has made its own decision on its own consent screen, and Omega does not
    # take on authority its package never declared.
    return required_scopes


def _token_expiry(expires_in: object, *, now: object) -> float | None:
    if expires_in is None:
        return None
    if type(expires_in) is not int or expires_in <= 0 or expires_in > _MAX_EXPIRES_IN_SECONDS:
        _fail("oauth_token_invalid")
    clock_value = _clock_value(now)
    try:
        expires_at = clock_value + expires_in
    except (OverflowError, ValueError, TypeError):
        raise OAuthProtocolError("oauth_token_invalid") from None
    if not math.isfinite(expires_at):
        _fail("oauth_token_invalid")
    return expires_at


def _metadata_url(value: str, well_known_path: str) -> str:
    parsed = urlsplit(value)
    # RFC 8414 s3.1 and RFC 9728 s3.1 insert the well-known path between the
    # host and the identifier's PATH COMPONENT. A bare origin spelled with its
    # root delimiter -- "https://mcp.miro.com/" -- has no path component, and
    # keeping the "/" asked for ".../oauth-authorization-server/", which
    # providers answer with a 404 or a redirect (and Omega refuses redirects).
    # Miro, Netlify and Vercel were unreachable for exactly that reason.
    #
    # The strip stops there. A trailing slash inside a real path is part of the
    # identifier: GitHub's resource is "https://api.githubcopilot.com/mcp/",
    # its metadata lives at ".../oauth-protected-resource/mcp/", and the
    # document names the resource with the slash -- so trimming it fetches a
    # different document that then fails the exact resource comparison.
    suffix = "" if parsed.path == "/" else parsed.path
    return f"{parsed.scheme}://{parsed.netloc}{well_known_path}{suffix}"


def protected_resource_metadata_url(resource_origin: str) -> str:
    """Return the RFC 9728 metadata URL for one exact signed resource."""
    return _metadata_url(
        resource_origin,
        "/.well-known/oauth-protected-resource",
    )


def authorization_server_metadata_url(issuer: str) -> str:
    """Return the RFC 8414 metadata URL for one exact signed issuer."""
    return _metadata_url(
        issuer,
        "/.well-known/oauth-authorization-server",
    )


def _https_endpoint(value: object, *, reason: str = "oauth_metadata_invalid") -> str:
    invalid = False
    try:
        endpoint = _stable_text(value, reason=reason)
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except (OAuthProtocolError, TypeError, ValueError):
        invalid = True
        endpoint = ""
        parsed = urlsplit("")
        hostname = None
        port = None
    expected_netloc = hostname if port is None else f"{hostname}:{port}"
    if (
        invalid
        or parsed.scheme != "https"
        or hostname is None
        or hostname != hostname.casefold()
        or _HOST.fullmatch(hostname) is None
        or parsed.netloc != expected_netloc
        or parsed.username is not None
        or parsed.password is not None
        or port in {0, 443}
        or parsed.path in {"", "/"}
        or "\\" in parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != endpoint
    ):
        _fail(reason)
    return endpoint


def _resolved_reference(value: object, base: str) -> object:
    """Resolve a relative URI reference against the endpoint that answered it.

    RFC 7591 calls ``registration_client_uri`` a URI, and providers built on the
    common worker OAuth stack answer with a path -- ``/register/<id>``. This
    changes nothing about what is ACCEPTED: the same-origin check against the
    issuer still runs on the result. It only stops a relative reference from
    reading as no origin at all.
    """
    if type(value) is not str or type(base) is not str:
        return value
    return urljoin(base, value)


def _same_origin_endpoint(value: object, issuer: str, *, reason: str) -> str:
    endpoint = _https_endpoint(value, reason=reason)
    parsed = urlsplit(endpoint)
    issuer_url = urlsplit(issuer)
    if (parsed.scheme, parsed.netloc) != (issuer_url.scheme, issuer_url.netloc):
        _fail(reason)
    return endpoint


def _https_browser_url(value: object, *, reason: str) -> str:
    try:
        url = _stable_text(value, reason=reason, maximum=4096)
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (OAuthProtocolError, TypeError, ValueError):
        raise OAuthProtocolError(reason) from None
    expected_netloc = hostname if port is None else f"{hostname}:{port}"
    if (
        parsed.scheme != "https"
        or hostname is None
        or hostname != hostname.casefold()
        or _HOST.fullmatch(hostname) is None
        or parsed.netloc != expected_netloc
        or parsed.username is not None
        or parsed.password is not None
        or port in {0, 443}
        or parsed.path in {"", "/"}
        or "\\" in parsed.path
        or parsed.fragment
        or parsed.geturl() != url
    ):
        _fail(reason)
    return url


def _typed_unique_strings(value: object, *, required: str) -> tuple[str, ...]:
    if (
        type(value) is not list
        or not value
        or any(type(item) is not str or not item for item in value)
        or len(value) != len(set(value))
        or required not in value
    ):
        _fail("oauth_metadata_invalid")
    return tuple(value)


def _optional_supported_scopes(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if type(value) is not list:
        _fail("oauth_metadata_invalid")
    try:
        scopes = _scopes(tuple(value), reason="oauth_metadata_invalid")
    except OAuthProtocolError:
        raise OAuthProtocolError("oauth_metadata_invalid") from None
    return scopes


@dataclass(
    frozen=True,
    slots=True,
    eq=False,
    weakref_slot=True,
    repr=False,
)
class OAuthDiscovery:
    """One metadata result issued by the protocol client that verified it."""

    profile_id: str
    profile_revision: str
    client_mode: OAuthClientMode
    client_identifier: str | None
    client_secret_advertised: bool
    redirect_uri: str | None
    issuer: str
    resource_origin: str
    authorization_endpoint: str | None
    token_endpoint: str
    revocation_endpoint: str | None
    code_challenge_methods_supported: tuple[str, ...]
    authorization_response_iss_parameter_supported: bool
    scopes_supported: tuple[str, ...] | None
    registration_endpoint: str | None = None
    authorization_flow: OAuthAuthorizationFlow = OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
    device_authorization_endpoint: str | None = None
    grant_types_supported: tuple[str, ...] = ()
    resource_identifier: str | None = None
    #: RFC 8414 ``token_endpoint_auth_methods_supported``. Empty means the
    #: server stated nothing, which RFC 6749 says to read as ``client_secret_basic``
    #: -- but Omega only uses this to decide whether a dynamically registered
    #: client is ALLOWED to present a secret, and an unstated list is not a
    #: statement that one is required.
    token_endpoint_auth_methods_supported: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "OAuthDiscovery("
            f"profile_id={self.profile_id!r}, profile_revision={self.profile_revision!r})"
        )

    @property
    def resource_parameter(self) -> str:
        """The RFC 8707 ``resource`` value: the identifier, else the endpoint."""
        return self.resource_identifier or self.resource_origin


@dataclass(frozen=True, slots=True, repr=False)
class OAuthAuthorizationParameters:
    """Redacted parameters for one PKCE authorization request."""

    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    state: str
    code_challenge: str
    resource_origin: str

    def __post_init__(self) -> None:
        _stable_text(self.client_id, reason="oauth_authorization_invalid", maximum=2048)
        _stable_text(self.redirect_uri, reason="oauth_authorization_invalid", maximum=2048)
        _scopes(self.scopes, reason="oauth_authorization_invalid")
        _stable_text(self.state, reason="oauth_authorization_invalid", maximum=1024)
        _stable_text(
            self.code_challenge,
            reason="oauth_authorization_invalid",
            maximum=256,
        )
        _stable_text(
            self.resource_origin,
            reason="oauth_authorization_invalid",
            maximum=2048,
        )

    def __repr__(self) -> str:
        return "OAuthAuthorizationParameters(<sealed>)"

    @classmethod
    def for_profile(
        cls,
        profile: OAuthProfile,
        *,
        credential: OAuthClientCredential | None,
        scopes: tuple[str, ...],
        state: str,
        code_challenge: str,
        resource_origin: str,
    ) -> OAuthAuthorizationParameters:
        if type(profile) is not OAuthProfile:
            _fail("oauth_client_mode_mismatch")
        if (
            profile.authorization_flow is not OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
            or profile.redirect_uri is None
        ):
            _fail("oauth_authorization_flow_mismatch")
        if profile.client_mode is OAuthClientMode.PREREGISTERED:
            if (
                type(credential) is not OAuthClientCredential
                or credential.client_id != profile.client_id
                or profile.client_id is None
                or profile.client_secret_ref is None
                and credential.client_secret is not None
            ):
                _fail("oauth_client_mode_mismatch")
            client_id = credential.client_id
        elif profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
            # The unit never builds this request for the browser: the bridge does. It is
            # built here only so the unit can hold the bridge's answer to exactly it.
            if (
                type(credential) is not OAuthClientCredential
                or profile.client_id is None
                or credential.client_id != profile.client_id
                or credential.client_secret is not None
            ):
                _fail("oauth_client_mode_mismatch")
            client_id = credential.client_id
        elif profile.client_mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT:
            if credential is not None or profile.client_metadata_document_url is None:
                _fail("oauth_client_mode_mismatch")
            client_id = profile.client_metadata_document_url
        else:
            # A dynamically registered client MAY hold a secret now: Supabase
            # and monday.com issue one at registration and require it at the
            # token endpoint. It is not refused here because it is not used
            # here -- an authorization request is a browser redirect, and the
            # parameters this builds carry a client_id, redirect_uri, scopes,
            # state, challenge and resource, and no place to put a secret.
            if (
                type(credential) is not OAuthClientCredential
                or profile.registration is None
            ):
                _fail("oauth_client_mode_mismatch")
            client_id = _stable_text(
                credential.client_id,
                reason="oauth_client_mode_mismatch",
                maximum=2048,
            )
        if resource_origin not in profile.resource_origins:
            _fail("oauth_resource_mismatch")
        return cls(
            client_id=client_id,
            redirect_uri=profile.redirect_uri,
            scopes=scopes,
            state=state,
            code_challenge=code_challenge,
            resource_origin=resource_origin,
        )


@dataclass(
    frozen=True,
    slots=True,
    eq=False,
    weakref_slot=True,
    repr=False,
)
class OAuthDeviceAuthorization:
    """One bounded device authorization response issued by this client."""

    profile_id: str
    profile_revision: str
    client_id: str
    scopes: tuple[str, ...]
    resource_origin: str
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_at: float
    interval_seconds: int

    def __repr__(self) -> str:
        return "OAuthDeviceAuthorization(<redacted>)"


def _device_fingerprint(value: OAuthDeviceAuthorization) -> tuple[object, ...]:
    return (
        value.profile_id,
        value.profile_revision,
        value.client_id,
        value.scopes,
        value.resource_origin,
        value.device_code,
        value.user_code,
        value.verification_uri,
        value.verification_uri_complete,
        value.expires_at,
        value.interval_seconds,
    )


@dataclass(frozen=True, slots=True, repr=False)
class OAuthTokenResponse:
    """A redacted, exact-scope Bearer token response."""

    access_token: str
    refresh_token: str | None
    token_type: str
    scopes: tuple[str, ...]
    expires_at: float | None

    def __repr__(self) -> str:
        return "OAuthTokenResponse(<redacted>)"


class HostedOAuthBridge(Protocol):
    """Optional confidential-client seam; never a durable token store."""

    async def begin(
        self, profile_id: str, state_digest: str, pkce_challenge: str, scopes: tuple[str, ...]
    ) -> str: ...

    async def redeem(
        self,
        profile_id: str,
        completion_handle: str,
        state_digest: str,
        pkce_verifier: str,
        scopes: tuple[str, ...],
        resource_origin: str,
    ) -> OAuthTokenResponse: ...

    async def refresh(
        self,
        profile_id: str,
        refresh_token: str,
        state_digest: str,
        scopes: tuple[str, ...],
        resource_origin: str,
    ) -> OAuthTokenResponse: ...

    async def revoke(
        self, profile_id: str, refresh_token: str, state_digest: str
    ) -> OAuthRevocationReceipt: ...


@dataclass(frozen=True, slots=True, repr=False)
class OAuthClientRegistrationResponse:
    """A redacted dynamic-client-registration response."""

    client_id: str
    registration_access_token: str | None
    registration_client_uri: str | None
    #: Issued only by a server that registers CONFIDENTIAL clients. It never
    #: appears in ``__repr__`` and is written straight into the sealed vault by
    #: the broker; nothing else may hold it.
    client_secret: str | None = None
    #: RFC 7591 ``client_secret_expires_at``: seconds since the epoch, or 0 for
    #: a secret that does not expire.
    client_secret_expires_at: float | None = None

    def __repr__(self) -> str:
        return "OAuthClientRegistrationResponse(<redacted>)"


@dataclass(frozen=True, slots=True)
class OAuthRevocationReceipt:
    """Content-free evidence of whether revocation was advertised and attempted."""

    endpoint_advertised: bool
    provider_request_sent: bool


def _discovery_fingerprint(discovery: OAuthDiscovery) -> tuple[object, ...]:
    return tuple(getattr(discovery, field) for field in _DISCOVERY_RESPONSE_FIELDS)


#: RFC 6749 s5.2 closes the token-endpoint error code to this set, and RFC 8628
#: s3.5 adds the device-flow members. A value drawn from it is one of Omega's own
#: literals, which is what makes it safe to log; anything else is not printed.
_REGISTERED_TOKEN_ERRORS = frozenset({
    "invalid_request",
    "invalid_client",
    "invalid_grant",
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
    "authorization_pending",
    "slow_down",
    "access_denied",
    "expired_token",
})


def _registered_token_error(value: object) -> str:
    """The provider's error code when it is a registered one, else a placeholder.

    Never returns provider-authored text: a value outside the registered set is
    reported by its shape alone, so a server that puts a sentence or a token in
    `error` cannot write into Omega's log.
    """
    if value is None:
        return "absent"
    if type(value) is not str:
        return "not a string"
    if value in _REGISTERED_TOKEN_ERRORS:
        return value
    return "unregistered"


class OAuthProtocolClient:
    """Generic OAuth discovery, authorization, token, refresh, and revoke client."""

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        limits: McpClientLimits | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        selected_limits = McpClientLimits() if limits is None else limits
        if type(selected_limits) is not McpClientLimits:
            _fail("oauth_limits_invalid")
        self._limits = selected_limits
        self._clock = clock or time.time
        connection_seconds = selected_limits.max_connection_duration_ms / 1_000
        operation_seconds = selected_limits.max_operation_duration_ms / 1_000
        self._operation_seconds = operation_seconds
        self._client = build_bounded_http_client(
            transport=transport,
            connection_seconds=connection_seconds,
            operation_seconds=operation_seconds,
        )
        self._issued: weakref.WeakKeyDictionary[
            OAuthDiscovery,
            tuple[object, ...],
        ] = weakref.WeakKeyDictionary()
        self._issued_devices: weakref.WeakKeyDictionary[
            OAuthDeviceAuthorization,
            tuple[object, ...],
        ] = weakref.WeakKeyDictionary()

    def __repr__(self) -> str:
        return "OAuthProtocolClient(<sealed>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | None,
        max_bytes: int,
        json_document: Mapping[str, object] | None = None,
        bearer_token: str | None = None,
    ) -> tuple[int, bytes]:
        if form is not None and json_document is not None:
            _fail("oauth_request_invalid")
        body = (
            canonical_bytes(dict(json_document))
            if json_document is not None
            else None if form is None else urlencode(dict(form)).encode("ascii")
        )
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if body is not None:
            headers["Content-Type"] = (
                "application/json"
                if json_document is not None
                else "application/x-www-form-urlencoded"
            )
        if bearer_token is not None:
            headers["Authorization"] = (
                "Bearer "
                + _stable_text(
                    bearer_token,
                    reason="oauth_registration_invalid",
                    maximum=_MAX_TOKEN_BYTES,
                )
            )
        try:
            async with asyncio.timeout(self._operation_seconds):
                async with self._client.stream(
                    method,
                    url,
                    content=body,
                    headers=headers,
                ) as response:
                    if response.is_redirect:
                        raise OAuthProtocolError("oauth_redirect_rejected")
                    content_encoding = response.headers.get("Content-Encoding", "")
                    if content_encoding.strip().casefold() not in {"", "identity"}:
                        raise OAuthProtocolError("oauth_response_encoding_unsupported")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise OAuthProtocolError("oauth_response_too_large")
                        chunks.append(bytes(chunk))
                    return response.status_code, b"".join(chunks)
        except TimeoutError:
            raise OAuthNetworkError("oauth_provider_timeout") from None
        except httpx2.TimeoutException:
            raise OAuthNetworkError("oauth_provider_timeout") from None
        except httpx2.RequestError:
            raise OAuthNetworkError("oauth_provider_unreachable") from None

    async def _get_json(self, url: str) -> dict[str, Any]:
        status, body = await self._request_bytes(
            "GET",
            url,
            form=None,
            max_bytes=self._limits.max_discovery_document_bytes,
        )
        if status != 200:
            # Which document, because discovery reads several and a bare reason
            # cannot say whether it was the protected-resource metadata, the
            # authorization-server metadata, or a JWKS that was refused.
            _LOG.warning(
                "discovery document refused: status=%d path=%s",
                status,
                urlsplit(url).path or "/",
            )
            raise OAuthProviderError("oauth_provider_rejected")
        return _json_object(body)

    async def discover(
        self,
        profile: OAuthProfile,
        *,
        resource_origin: str,
    ) -> OAuthDiscovery:
        if (
            type(profile) is not OAuthProfile
            or type(resource_origin) is not str
            or resource_origin not in profile.resource_origins
        ):
            _fail("oauth_resource_mismatch")
        # RFC 9728: the metadata document is addressed by the resource
        # *identifier*, which a signed profile may state separately when the
        # provider does not serve it under the endpoint's own suffixed path.
        resource_identifier = profile.identifier_for(resource_origin)
        protected = await self._get_json(
            protected_resource_metadata_url(resource_identifier)
        )
        if protected.get("resource") != resource_identifier:
            _fail("oauth_resource_mismatch")
        issuer = profile.authorization_issuer
        references = protected.get("authorization_servers")
        # Some PRMs spell a root URL with '/', while the AS publishes its
        # issuer without it. Accept only that reference spelling: still fetch
        # the signed issuer's URL and require its exact issuer below (and in
        # callbacks). Never trim path-scoped issuers or follow another host.
        root_reference = (
            urlsplit(issuer).path == "" and references == [issuer + "/"]
        )
        if references != [issuer] and not root_reference:
            _fail("oauth_issuer_mismatch")
        metadata = await self._get_json(
            authorization_server_metadata_url(profile.authorization_issuer)
        )
        if metadata.get("issuer") != profile.authorization_issuer:
            _fail("oauth_issuer_mismatch")
        grant_types_value = metadata.get("grant_types_supported", [])
        if (
            type(grant_types_value) is not list
            or any(type(item) is not str or not item for item in grant_types_value)
            or len(grant_types_value) != len(set(grant_types_value))
        ):
            _fail("oauth_metadata_invalid")
        grant_types = tuple(grant_types_value)
        auth_methods_value = metadata.get("token_endpoint_auth_methods_supported", [])
        if (
            type(auth_methods_value) is not list
            or any(type(item) is not str or not item for item in auth_methods_value)
            or len(auth_methods_value) != len(set(auth_methods_value))
            or len(auth_methods_value) > _MAX_SCOPE_COUNT
        ):
            _fail("oauth_metadata_invalid")
        token_endpoint_auth_methods = tuple(auth_methods_value)
        if profile.authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE:
            _typed_unique_strings(
                metadata.get("response_types_supported"),
                required="code",
            )
            challenge_methods = _typed_unique_strings(
                metadata.get("code_challenge_methods_supported"),
                required="S256",
            )
            authorization_endpoint = _https_endpoint(metadata.get("authorization_endpoint"))
            device_authorization_endpoint = None
        else:
            if _DEVICE_GRANT not in grant_types:
                _fail("oauth_authorization_flow_mismatch")
            challenge_methods = ()
            authorization_endpoint = None
            device_authorization_endpoint = _https_endpoint(
                metadata.get("device_authorization_endpoint")
            )
        issuer_parameter = metadata.get(
            "authorization_response_iss_parameter_supported",
            False,
        )
        if type(issuer_parameter) is not bool:
            _fail("oauth_metadata_invalid")
        if (
            profile.client_mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT
            and metadata.get("client_id_metadata_document_supported") is not True
        ):
            _fail("oauth_client_mode_mismatch")
        if profile.client_mode in (OAuthClientMode.PREREGISTERED, OAuthClientMode.HOSTED_BRIDGE):
            client_identifier = profile.client_id
        elif profile.client_mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT:
            client_identifier = profile.client_metadata_document_url
        else:
            client_identifier = None
        if (
            profile.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION
            and client_identifier is None
        ):
            _fail("oauth_client_mode_mismatch")
        revocation_value = metadata.get("revocation_endpoint")
        if revocation_value is not None and type(revocation_value) is not str:
            _fail("oauth_metadata_invalid")
        registration_value = metadata.get("registration_endpoint")
        if registration_value is not None and type(registration_value) is not str:
            _fail("oauth_metadata_invalid")
        registration_endpoint = (
            None
            if registration_value is None
            else _same_origin_endpoint(
                registration_value,
                profile.authorization_issuer,
                reason="oauth_metadata_invalid",
            )
        )
        discovery = OAuthDiscovery(
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            client_mode=profile.client_mode,
            client_identifier=client_identifier,
            client_secret_advertised=profile.client_secret_ref is not None,
            redirect_uri=profile.redirect_uri,
            issuer=profile.authorization_issuer,
            resource_origin=resource_origin,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=_https_endpoint(metadata.get("token_endpoint")),
            revocation_endpoint=(
                None if revocation_value is None else _https_endpoint(revocation_value)
            ),
            registration_endpoint=registration_endpoint,
            code_challenge_methods_supported=challenge_methods,
            authorization_response_iss_parameter_supported=issuer_parameter,
            scopes_supported=_optional_supported_scopes(metadata.get("scopes_supported")),
            authorization_flow=profile.authorization_flow,
            device_authorization_endpoint=device_authorization_endpoint,
            grant_types_supported=grant_types,
            resource_identifier=profile.resource_identifier,
            token_endpoint_auth_methods_supported=token_endpoint_auth_methods,
        )
        self._issued[discovery] = _discovery_fingerprint(discovery)
        return discovery

    async def register(
        self,
        discovery: OAuthDiscovery,
        *,
        profile: OAuthProfile,
    ) -> OAuthClientRegistrationResponse:
        checked = self._checked_discovery(discovery)
        if (
            type(profile) is not OAuthProfile
            or profile.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION
            or checked.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION
            or checked.profile_id != profile.profile_id
            or checked.profile_revision != profile.revision
            or checked.issuer != profile.authorization_issuer
            or profile.registration is None
        ):
            _fail("oauth_client_mode_mismatch")
        if checked.registration_endpoint is None:
            _fail("oauth_registration_unsupported")
        status, body = await self._request_bytes(
            "POST",
            checked.registration_endpoint,
            form=None,
            json_document=profile.registration.to_dict(),
            max_bytes=self._limits.max_result_document_bytes,
        )
        if status not in {200, 201}:
            raise OAuthProviderError("oauth_registration_invalid")
        try:
            document = _json_object(body)
            client_id = _stable_text(
                document.get("client_id"),
                reason="oauth_registration_invalid",
                maximum=2048,
            )
            # A server that registers CONFIDENTIAL clients issues a secret;
            # Supabase and monday.com both require one at the token endpoint.
            # The secret is carried only as far as the sealed vault, and only
            # when the AUTHORIZATION SERVER said its token endpoint needs client
            # authentication -- a secret volunteered by a server that also
            # accepts ``none`` (Neon does this) is read past, exactly as before.
            client_secret: str | None = None
            client_secret_expires_at: float | None = None
            if _requires_client_authentication(checked):
                client_secret = _optional_client_secret(document.get("client_secret"))
                client_secret_expires_at = _optional_secret_expiry(
                    document.get("client_secret_expires_at")
                )
            response_auth_method = document.get("token_endpoint_auth_method", "none")
            if response_auth_method != "none" and (
                client_secret is None
                or response_auth_method not in _CONFIDENTIAL_AUTH_METHODS
            ):
                # Either a method Omega cannot perform, or a confidential method
                # with nothing to perform it with.
                _fail("oauth_registration_invalid")
            access_token_value = document.get("registration_access_token")
            client_uri_value = document.get("registration_client_uri")
            if access_token_value is not None and client_uri_value is None:
                _fail("oauth_registration_invalid")
            if client_uri_value is not None and access_token_value is None:
                # RFC 7591 pairs the management URI with the token authorising
                # it, and ``deregister`` needs both. Six providers answer with
                # the URI alone: that registration cannot be managed remotely,
                # which is no reason to refuse the client_id just issued.
                client_uri_value = None
            registration_access_token = (
                None
                if access_token_value is None
                else _stable_text(
                    access_token_value,
                    reason="oauth_registration_invalid",
                    maximum=_MAX_TOKEN_BYTES,
                )
            )
            registration_client_uri = (
                None
                if client_uri_value is None
                else _same_origin_endpoint(
                    _resolved_reference(client_uri_value, checked.registration_endpoint),
                    checked.issuer,
                    reason="oauth_registration_invalid",
                )
            )
        except OAuthProtocolError:
            raise OAuthProtocolError("oauth_registration_invalid") from None
        return OAuthClientRegistrationResponse(
            client_id=client_id,
            registration_access_token=registration_access_token,
            registration_client_uri=registration_client_uri,
            client_secret=client_secret,
            client_secret_expires_at=client_secret_expires_at,
        )

    async def deregister(
        self,
        registration_client_uri: str,
        *,
        registration_access_token: str,
    ) -> None:
        uri = _https_endpoint(
            registration_client_uri,
            reason="oauth_registration_invalid",
        )
        status, _body = await self._request_bytes(
            "DELETE",
            uri,
            form=None,
            bearer_token=registration_access_token,
            max_bytes=self._limits.max_result_document_bytes,
        )
        if status < 200 or status >= 300:
            raise OAuthProviderError("oauth_registration_deletion_failed")

    def _checked_discovery(self, discovery: object) -> OAuthDiscovery:
        if type(discovery) is not OAuthDiscovery:
            _fail("oauth_discovery_invalid")
        expected = self._issued.get(discovery)
        try:
            current = _discovery_fingerprint(discovery)
        except (AttributeError, TypeError, ValueError):
            current = None
        if expected is None or current != expected:
            _fail("oauth_discovery_invalid")
        return discovery

    def _bind_parameters(
        self,
        discovery: object,
        parameters: object,
    ) -> tuple[OAuthDiscovery, OAuthAuthorizationParameters]:
        checked = self._checked_discovery(discovery)
        if (
            type(parameters) is not OAuthAuthorizationParameters
            or checked.authorization_flow is not OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
            or checked.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION
            and parameters.client_id != checked.client_identifier
            or parameters.redirect_uri != checked.redirect_uri
            or parameters.resource_origin != checked.resource_origin
        ):
            _fail("oauth_authorization_invalid")
        if checked.scopes_supported is not None and not set(parameters.scopes).issubset(
            checked.scopes_supported
        ):
            _fail("oauth_scope_mismatch")
        return checked, parameters

    def build_authorization_url(
        self,
        discovery: OAuthDiscovery,
        parameters: OAuthAuthorizationParameters,
    ) -> str:
        checked, bound = self._bind_parameters(discovery, parameters)
        if checked.authorization_endpoint is None:
            _fail("oauth_authorization_flow_mismatch")
        query = {
            "client_id": bound.client_id,
            "code_challenge": bound.code_challenge,
            "code_challenge_method": "S256",
            "redirect_uri": bound.redirect_uri,
            "resource": checked.resource_parameter,
            "response_type": "code",
            "scope": " ".join(bound.scopes),
            "state": bound.state,
        }
        return f"{checked.authorization_endpoint}?{urlencode(query)}"

    async def begin_device_authorization(
        self,
        discovery: OAuthDiscovery,
        *,
        client_id: str | None,
        scopes: tuple[str, ...],
    ) -> OAuthDeviceAuthorization:
        checked = self._checked_discovery(discovery)
        selected_client = _stable_text(
            client_id,
            reason="oauth_client_mode_mismatch",
            maximum=2048,
        )
        selected_scopes = _scopes(scopes, reason="oauth_scope_mismatch")
        if (
            checked.authorization_flow is not OAuthAuthorizationFlow.DEVICE_CODE
            or checked.device_authorization_endpoint is None
            or checked.client_secret_advertised
            or selected_client != checked.client_identifier
        ):
            _fail("oauth_authorization_flow_mismatch")
        status, body = await self._request_bytes(
            "POST",
            checked.device_authorization_endpoint,
            form={"client_id": selected_client, "scope": " ".join(selected_scopes)},
            max_bytes=self._limits.max_result_document_bytes,
        )
        if status != 200:
            try:
                provider_error = _json_object(body).get("error")
            except OAuthProtocolError:
                provider_error = None
            if provider_error == "device_flow_disabled":
                raise OAuthProviderError("oauth_device_flow_disabled")
            raise OAuthProviderError("oauth_provider_rejected")
        document = _json_object(body)
        device_code = _stable_text(
            document.get("device_code"),
            reason="oauth_device_authorization_invalid",
            maximum=_MAX_TOKEN_BYTES,
        )
        user_code = _stable_text(
            document.get("user_code"),
            reason="oauth_device_authorization_invalid",
            maximum=256,
        )
        verification_uri = _https_browser_url(
            document.get("verification_uri"),
            reason="oauth_device_authorization_invalid",
        )
        complete_value = document.get("verification_uri_complete")
        verification_uri_complete = (
            None
            if complete_value is None
            else _https_browser_url(
                complete_value,
                reason="oauth_device_authorization_invalid",
            )
        )
        expires_in = document.get("expires_in")
        interval = document.get("interval", 5)
        if (
            type(expires_in) is not int
            or expires_in <= 0
            or expires_in > _MAX_EXPIRES_IN_SECONDS
            or type(interval) is not int
            or interval <= 0
            or interval > 300
        ):
            _fail("oauth_device_authorization_invalid")
        authorization = OAuthDeviceAuthorization(
            profile_id=checked.profile_id,
            profile_revision=checked.profile_revision,
            client_id=selected_client,
            scopes=selected_scopes,
            resource_origin=checked.resource_origin,
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=verification_uri_complete,
            expires_at=_clock_value(self._clock()) + expires_in,
            interval_seconds=interval,
        )
        self._issued_devices[authorization] = _device_fingerprint(authorization)
        return authorization

    def _checked_device_authorization(
        self,
        value: object,
    ) -> OAuthDeviceAuthorization:
        if type(value) is not OAuthDeviceAuthorization:
            _fail("oauth_device_authorization_invalid")
        expected = self._issued_devices.get(value)
        try:
            current = _device_fingerprint(value)
        except (AttributeError, TypeError, ValueError):
            current = None
        if expected is None or current != expected:
            _fail("oauth_device_authorization_invalid")
        return value

    def restore_device_authorization(
        self,
        discovery: OAuthDiscovery,
        *,
        client_id: str,
        scopes: tuple[str, ...],
        device_code: str,
        expires_at: float,
        interval_seconds: int,
    ) -> OAuthDeviceAuthorization:
        """Rebind encrypted pending state to freshly verified discovery authority."""

        checked = self._checked_discovery(discovery)
        selected_client = _stable_text(
            client_id,
            reason="oauth_device_authorization_invalid",
            maximum=2048,
        )
        selected_scopes = _scopes(scopes, reason="oauth_device_authorization_invalid")
        selected_code = _stable_text(
            device_code,
            reason="oauth_device_authorization_invalid",
            maximum=_MAX_TOKEN_BYTES,
        )
        selected_expiry = _clock_value(expires_at)
        if (
            checked.authorization_flow is not OAuthAuthorizationFlow.DEVICE_CODE
            or checked.device_authorization_endpoint is None
            or selected_client != checked.client_identifier
            or type(interval_seconds) is not int
            or interval_seconds <= 0
            or interval_seconds > 300
        ):
            _fail("oauth_device_authorization_invalid")
        authorization = OAuthDeviceAuthorization(
            profile_id=checked.profile_id,
            profile_revision=checked.profile_revision,
            client_id=selected_client,
            scopes=selected_scopes,
            resource_origin=checked.resource_origin,
            device_code=selected_code,
            user_code="withheld-after-start",
            verification_uri=checked.device_authorization_endpoint,
            verification_uri_complete=None,
            expires_at=selected_expiry,
            interval_seconds=interval_seconds,
        )
        self._issued_devices[authorization] = _device_fingerprint(authorization)
        return authorization

    async def poll_device_token(
        self,
        discovery: OAuthDiscovery,
        authorization: OAuthDeviceAuthorization,
        *,
        client_id: str | None,
        required_scopes: tuple[str, ...],
    ) -> OAuthTokenResponse:
        checked = self._checked_discovery(discovery)
        device = self._checked_device_authorization(authorization)
        selected_client = _stable_text(
            client_id,
            reason="oauth_client_mode_mismatch",
            maximum=2048,
        )
        scopes = _scopes(required_scopes, reason="oauth_scope_mismatch")
        if (
            checked.authorization_flow is not OAuthAuthorizationFlow.DEVICE_CODE
            or selected_client != checked.client_identifier
            or device.profile_id != checked.profile_id
            or device.profile_revision != checked.profile_revision
            or device.client_id != selected_client
            or device.scopes != scopes
            or device.resource_origin != checked.resource_origin
        ):
            _fail("oauth_device_authorization_invalid")
        status, body = await self._request_bytes(
            "POST",
            checked.token_endpoint,
            form={
                "client_id": selected_client,
                "device_code": device.device_code,
                "grant_type": _DEVICE_GRANT,
            },
            max_bytes=self._limits.max_result_document_bytes,
        )
        document = _json_object(body)
        error = document.get("error")
        if error is not None:
            reasons = {
                "authorization_pending": "oauth_device_authorization_pending",
                "slow_down": "oauth_device_slow_down",
                "access_denied": "oauth_authorization_denied",
                "expired_token": "oauth_device_authorization_expired",
            }
            reason = reasons.get(error) if type(error) is str else None
            if reason is None:
                raise OAuthProviderError("oauth_provider_rejected")
            raise OAuthProtocolError(reason)
        if status != 200:
            raise OAuthProviderError("oauth_provider_rejected")
        return self._token_response(
            checked,
            document,
            required_scopes=scopes,
            existing_refresh_token=None,
        )

    def _client_secret(
        self,
        discovery: OAuthDiscovery,
        value: object,
    ) -> str | None:
        if value is None:
            return None
        secret = _stable_text(value, reason="oauth_client_mode_mismatch", maximum=1024)
        preregistered = (
            discovery.client_mode is OAuthClientMode.PREREGISTERED
            and discovery.client_secret_advertised
        )
        # A dynamically registered client has no signed ``client_secret_ref`` to
        # advertise -- its secret does not exist until the server issues one --
        # so the question is whether the SERVER requires client authentication.
        # Supabase and monday.com both do; Neon does not, and a secret it
        # volunteers is never presented.
        dynamic_confidential = (
            discovery.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
            and _requires_client_authentication(discovery)
        )
        if not preregistered and not dynamic_confidential:
            _fail("oauth_client_mode_mismatch")
        return secret

    async def _token_request(
        self,
        discovery: OAuthDiscovery,
        *,
        form: Mapping[str, str],
        required_scopes: tuple[str, ...],
        existing_refresh_token: str | None,
    ) -> OAuthTokenResponse:
        status, body = await self._request_bytes(
            "POST",
            discovery.token_endpoint,
            form=form,
            max_bytes=self._limits.max_result_document_bytes,
        )
        if status != 200:
            try:
                provider_error = _json_object(body).get("error")
            except OAuthProtocolError:
                provider_error = None
            # The status, the registered error code and what Omega itself sent.
            # `oauth_provider_rejected` is raised from six places and on its own
            # separates none of them; `invalid_client` and `invalid_grant` mean
            # completely different repairs.
            _LOG.warning(
                "token endpoint refused: status=%d error=%s grant=%s client_authenticated=%s",
                status,
                _registered_token_error(provider_error),
                form.get("grant_type", "absent"),
                "client_secret" in form,
            )
            # `invalid_client` is what RFC 6749 s5.2 reserves for this, and a
            # server that demands client authentication then refuses a request
            # that carried none has said so whichever code it chose. monday.com
            # says `invalid_request`; the operator should still read "this
            # client cannot authenticate here", not "the provider said no".
            unauthenticated = (
                "client_secret" not in form
                and _requires_client_authentication(discovery)
            )
            if provider_error == "invalid_client" or unauthenticated:
                raise OAuthProviderError("oauth_client_invalid")
            raise OAuthProviderError("oauth_provider_rejected")
        document = _json_object(body)
        return self._token_response(
            discovery,
            document,
            required_scopes=required_scopes,
            existing_refresh_token=existing_refresh_token,
        )

    def _token_response(
        self,
        discovery: OAuthDiscovery,
        document: Mapping[str, object],
        *,
        required_scopes: tuple[str, ...],
        existing_refresh_token: str | None,
    ) -> OAuthTokenResponse:
        access_token = _required_token_text(document.get("access_token"))
        refresh_token = _optional_token_text(document.get("refresh_token", existing_refresh_token))
        token_type = _bearer_token_type(document.get("token_type"))
        _returned_scopes(document.get("scope"), required_scopes)
        self._validate_token_resource(document, discovery.resource_origin)
        expires_at = _token_expiry(document.get("expires_in"), now=self._clock())
        return OAuthTokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            scopes=required_scopes,
            expires_at=expires_at,
        )

    @staticmethod
    def _validate_token_resource(
        document: Mapping[str, object],
        resource_origin: str,
    ) -> None:
        audiences = [document[name] for name in ("audience", "resource", "aud") if name in document]

        def matches(value: object) -> bool:
            return value == resource_origin or value == [resource_origin]

        if audiences and any(not matches(value) for value in audiences):
            _fail("oauth_resource_mismatch")

    async def exchange(
        self,
        discovery: OAuthDiscovery,
        parameters: OAuthAuthorizationParameters,
        *,
        code: str,
        code_verifier: str,
        client_secret: str | None,
    ) -> OAuthTokenResponse:
        checked, bound = self._bind_parameters(discovery, parameters)
        secret = self._client_secret(checked, client_secret)
        form = {
            "client_id": bound.client_id,
            "code": _stable_text(code, reason="oauth_exchange_invalid", maximum=4096),
            "code_verifier": _stable_text(
                code_verifier,
                reason="oauth_exchange_invalid",
                maximum=1024,
            ),
            "grant_type": "authorization_code",
            "redirect_uri": bound.redirect_uri,
            "resource": checked.resource_parameter,
        }
        if secret is not None:
            form["client_secret"] = secret
        return await self._token_request(
            checked,
            form=form,
            required_scopes=bound.scopes,
            existing_refresh_token=None,
        )

    async def refresh(
        self,
        discovery: OAuthDiscovery,
        client_id: str,
        refresh_token: str,
        *,
        client_secret: str | None,
        required_scopes: tuple[str, ...],
    ) -> OAuthTokenResponse:
        checked = self._checked_discovery(discovery)
        if (
            checked.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION
            and client_id != checked.client_identifier
        ):
            _fail("oauth_client_mode_mismatch")
        _stable_text(client_id, reason="oauth_client_mode_mismatch", maximum=2048)
        scopes = _scopes(required_scopes, reason="oauth_scope_mismatch")
        secret = self._client_secret(checked, client_secret)
        current_refresh = _stable_text(
            refresh_token,
            reason="oauth_refresh_invalid",
            maximum=16_384,
        )
        form = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": current_refresh,
            "resource": checked.resource_parameter,
        }
        if secret is not None:
            form["client_secret"] = secret
        return await self._token_request(
            checked,
            form=form,
            required_scopes=scopes,
            existing_refresh_token=current_refresh,
        )

    async def revoke(
        self,
        discovery: OAuthDiscovery,
        *,
        client_id: str,
        token: str,
        client_secret: str | None,
    ) -> OAuthRevocationReceipt:
        checked = self._checked_discovery(discovery)
        if (
            checked.client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION
            and client_id != checked.client_identifier
        ):
            _fail("oauth_client_mode_mismatch")
        _stable_text(client_id, reason="oauth_client_mode_mismatch", maximum=2048)
        if checked.revocation_endpoint is None:
            return OAuthRevocationReceipt(
                endpoint_advertised=False,
                provider_request_sent=False,
            )
        secret = self._client_secret(checked, client_secret)
        form = {
            "client_id": client_id,
            "token": _stable_text(token, reason="oauth_revoke_invalid", maximum=16_384),
        }
        if secret is not None:
            form["client_secret"] = secret
        status, _body = await self._request_bytes(
            "POST",
            checked.revocation_endpoint,
            form=form,
            max_bytes=self._limits.max_result_document_bytes,
        )
        if status < 200 or status >= 300:
            raise OAuthProviderError("oauth_provider_rejected")
        return OAuthRevocationReceipt(
            endpoint_advertised=True,
            provider_request_sent=True,
        )


class HttpHostedOAuthBridge:
    """The unit side of the hosted bridge (docs/plugin-hosted-oauth-bridge.md).

    The bridge base is the origin of the profile's signed ``redirect_uri``; nothing about
    where the bridge lives is configured on the unit. A token the bridge relays is validated
    exactly as a direct token response is, against the scopes the operator consented to.
    """

    def __init__(
        self,
        profile_for: Callable[[str], OAuthProfile | None],
        *,
        protocol: OAuthProtocolClient,
    ) -> None:
        # The caller's protocol client, so the one close the caller already owns
        # (PluginLifecycleService.close) also releases the bridge's connections.
        self._profile_for = profile_for
        self._http = protocol

    def _endpoint(self, profile_id: str, operation: str) -> str:
        profile = self._profile_for(profile_id)
        if type(profile) is not OAuthProfile or profile.client_mode is not OAuthClientMode.HOSTED_BRIDGE:
            _fail("oauth_profile_unavailable")
        origin = urlsplit(profile.redirect_uri)
        if origin.scheme != "https" or not origin.hostname or origin.username or origin.password:
            _fail("oauth_profile_unavailable")
        return f"https://{origin.netloc}/bridge/v1/{profile.profile_id}/{operation}"

    async def _post(self, profile_id: str, operation: str, document: Mapping[str, object]) -> dict[str, Any]:
        status, body = await self._http._request_bytes(
            "POST",
            self._endpoint(profile_id, operation),
            form=None,
            json_document=document,
            max_bytes=self._http._limits.max_result_document_bytes,
        )
        if status == 200:
            return _json_object(body)
        try:
            error = _json_object(body).get("error")
        except OAuthProtocolError:
            error = None
        _LOG.warning(
            "hosted bridge refused: operation=%s status=%d error=%s",
            operation, status, _registered_token_error(error),
        )
        if status == 502 and error == "provider_unavailable":
            # The bridge ran but could not reach the provider: the direct path's network failure.
            raise OAuthNetworkError("oauth_provider_unreachable")
        if status >= 500 or status == 404:
            # The bridge itself cannot serve this profile (not deployed, unconfigured or
            # down): the provider has said nothing, so this is never reported as its refusal.
            _fail("oauth_profile_unavailable")
        if error == "invalid_client":
            raise OAuthProviderError("oauth_client_invalid")
        raise OAuthProviderError("oauth_provider_rejected")

    def _token(
        self,
        document: Mapping[str, object],
        scopes: tuple[str, ...],
        existing_refresh_token: str | None,
        resource_origin: str,
    ) -> OAuthTokenResponse:
        required = _scopes(scopes, reason="oauth_scope_mismatch")
        _returned_scopes(document.get("scope"), required)
        OAuthProtocolClient._validate_token_resource(document, resource_origin)
        return OAuthTokenResponse(
            access_token=_required_token_text(document.get("access_token")),
            refresh_token=_optional_token_text(document.get("refresh_token", existing_refresh_token)),
            token_type=_bearer_token_type(document.get("token_type")),
            scopes=required,
            expires_at=_token_expiry(document.get("expires_in"), now=self._http._clock()),
        )

    async def begin(
        self, profile_id: str, state_digest: str, pkce_challenge: str, scopes: tuple[str, ...]
    ) -> str:
        requested = tuple(sorted(_scopes(scopes, reason="oauth_scope_mismatch")))
        document = await self._post(profile_id, "begin", {
            "state_digest": state_digest, "pkce_challenge": pkce_challenge, "scopes": list(requested),
        })
        return _stable_text(document.get("authorization_url"), reason="oauth_profile_unavailable", maximum=8192)

    async def redeem(
        self,
        profile_id: str,
        completion_handle: str,
        state_digest: str,
        pkce_verifier: str,
        scopes: tuple[str, ...],
        resource_origin: str,
    ) -> OAuthTokenResponse:
        document = await self._post(profile_id, "redeem", {
            "completion_handle": completion_handle, "state_digest": state_digest,
            "pkce_verifier": pkce_verifier,
        })
        return self._token(document, scopes, None, resource_origin)

    async def refresh(
        self,
        profile_id: str,
        refresh_token: str,
        state_digest: str,
        scopes: tuple[str, ...],
        resource_origin: str,
    ) -> OAuthTokenResponse:
        document = await self._post(profile_id, "refresh", {
            "refresh_token": refresh_token, "state_digest": state_digest,
        })
        return self._token(document, scopes, refresh_token, resource_origin)

    async def revoke(
        self, profile_id: str, refresh_token: str, state_digest: str
    ) -> OAuthRevocationReceipt:
        document = await self._post(profile_id, "revoke", {
            "refresh_token": refresh_token, "state_digest": state_digest,
        })
        advertised, sent = document.get("endpoint_advertised"), document.get("provider_request_sent")
        if type(advertised) is not bool or type(sent) is not bool or (sent and not advertised):
            _fail("oauth_revocation_unsupported")
        return OAuthRevocationReceipt(endpoint_advertised=advertised, provider_request_sent=sent)


__all__ = [
    "HttpHostedOAuthBridge",
    "OAuthAuthorizationParameters",
    "OAuthClientRegistrationResponse",
    "OAuthDeviceAuthorization",
    "OAuthDiscovery",
    "OAuthNetworkError",
    "OAuthProtocolClient",
    "OAuthProtocolError",
    "OAuthProviderError",
    "OAuthRevocationReceipt",
    "OAuthTokenResponse",
    "authorization_server_metadata_url",
    "protected_resource_metadata_url",
]
