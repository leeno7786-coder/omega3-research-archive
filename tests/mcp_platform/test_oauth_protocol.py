"""Provider-neutral OAuth protocol boundary tests."""

from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from mcp_platform.client_contracts import McpClientLimits
from mcp_platform.oauth_profiles import (
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthDynamicRegistrationTemplate,
    OAuthProfile,
    computed_profile_revision,
)
from mcp_platform.oauth_protocol import (
    OAuthAuthorizationParameters,
    OAuthDeviceAuthorization,
    OAuthNetworkError,
    OAuthProtocolClient,
    OAuthProtocolError,
    OAuthProviderError,
)
from mcp_platform.oauth_vault import OAuthClientCredential


_RESOURCE = "https://resource.example/mcp"
_ISSUER = "https://issuer.example"
_PROTECTED_METADATA = "https://resource.example/.well-known/oauth-protected-resource/mcp"
_AUTHORIZATION_METADATA = "https://issuer.example/.well-known/oauth-authorization-server"
_AUTHORIZATION_ENDPOINT = "https://login.example/authorize"
_TOKEN_ENDPOINT = "https://tokens.example/token"
_REVOCATION_ENDPOINT = "https://tokens.example/revoke"
_REGISTRATION_ENDPOINT = "https://issuer.example/register"
_REDIRECT = "http://127.0.0.1:13310/oauth/example.desktop/callback"


def _profile(
    mode: OAuthClientMode = OAuthClientMode.PREREGISTERED,
) -> OAuthProfile:
    profile = OAuthProfile(
        schema_version=1,
        profile_id="example.desktop",
        catalog_signer="omega.featured",
        allowed_plugin_publishers=("omega.first_party",),
        app_publisher="example",
        provider_id="example.official",
        authorization_issuer=_ISSUER,
        resource_origins=(_RESOURCE,),
        client_mode=mode,
        client_id=("omega-public-client" if mode is OAuthClientMode.PREREGISTERED else None),
        client_secret_ref=(
            "oauth-client/example.desktop/secret" if mode is OAuthClientMode.PREREGISTERED else None
        ),
        client_metadata_document_url=(
            "https://omega.example/oauth/example-client.json"
            if mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT
            else None
        ),
        redirect_uri=_REDIRECT,
        account_partition="issuer_account",
        revision="sha256:" + "0" * 64,
    )
    return replace(profile, revision=computed_profile_revision(profile))


def _device_profile() -> OAuthProfile:
    profile = replace(
        _profile(),
        authorization_issuer="https://github.com/login/oauth",
        resource_origins=("https://api.githubcopilot.com/mcp/",),
        client_secret_ref=None,
        redirect_uri=None,
        authorization_flow=OAuthAuthorizationFlow.DEVICE_CODE,
    )
    return replace(profile, revision=computed_profile_revision(profile))


def _dynamic_profile() -> OAuthProfile:
    profile = replace(
        _profile(OAuthClientMode.DYNAMIC_REGISTRATION),
        registration=OAuthDynamicRegistrationTemplate(
            client_name="Omega",
            redirect_uris=(_REDIRECT,),
            token_endpoint_auth_method="none",
            grant_types=("authorization_code", "refresh_token"),
            response_types=("code",),
            scope="repo read:user",
            software_id="org.omega2ai.desktop",
        ),
    )
    return replace(profile, revision=computed_profile_revision(profile))


class _CloseTrackingStream(httpx2.AsyncByteStream):
    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        wait_forever: bool = False,
    ) -> None:
        self._chunks = chunks
        self._wait_forever = wait_forever
        self.started = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        for chunk in self._chunks:
            yield chunk
        if self._wait_forever:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.close_calls += 1


Handler = Callable[
    [httpx2.Request],
    httpx2.Response | Awaitable[httpx2.Response],
]


class RecordingOAuthTransport(httpx2.AsyncBaseTransport):
    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx2.Request] = []
        self.close_calls = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self._handler(request)
        if inspect.isawaitable(response):
            response = await response
        return response

    async def aclose(self) -> None:
        self.close_calls += 1

    @property
    def secret_destinations(self) -> set[str]:
        destinations: set[str] = set()
        for request in self.requests:
            if b"desktop-secret" in request.content:
                destinations.add(str(request.url))
        return destinations


class _OAuthServer:
    def __init__(self) -> None:
        self.resource = _RESOURCE
        self.authorization_servers: object = [_ISSUER]
        self.metadata_issuer = _ISSUER
        self.authorization_endpoint = _AUTHORIZATION_ENDPOINT
        self.token_endpoint = _TOKEN_ENDPOINT
        self.revocation_endpoint: str | None = _REVOCATION_ENDPOINT
        self.registration_endpoint: object = _REGISTRATION_ENDPOINT
        self.registration_status = 201
        self.registration_document: object = {"client_id": "dynamic-client"}
        #: Unset by default: most providers state nothing, and an unstated list
        #: must not be read as "a secret is required".
        self.token_endpoint_auth_methods_supported: object = None
        self.authorization_response_iss_parameter_supported: object = True
        self.cimd_supported: object = True
        self.token_status = 200
        self.token_document: object = {
            "access_token": "opaque-access",
            "refresh_token": "opaque-refresh",
            "token_type": "Bearer",
            "expires_in": 60,
            "scope": "repo read:user",
            "resource": _RESOURCE,
        }

    def protected_document(self) -> dict[str, object]:
        return {
            "resource": self.resource,
            "authorization_servers": self.authorization_servers,
            # These unverified protected-resource fields must never become endpoints.
            "authorization_endpoint": "https://evil.example/authorize",
            "token_endpoint": "https://evil.example/token",
            "revocation_endpoint": "https://evil.example/revoke",
        }

    def authorization_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "issuer": self.metadata_issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["repo", "read:user"],
            "authorization_response_iss_parameter_supported": (
                self.authorization_response_iss_parameter_supported
            ),
            "client_id_metadata_document_supported": self.cimd_supported,
            "registration_endpoint": self.registration_endpoint,
        }
        if self.revocation_endpoint is not None:
            document["revocation_endpoint"] = self.revocation_endpoint
        if self.token_endpoint_auth_methods_supported is not None:
            document["token_endpoint_auth_methods_supported"] = (
                self.token_endpoint_auth_methods_supported
            )
        return document

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == _PROTECTED_METADATA:
            return httpx2.Response(200, json=self.protected_document())
        if url == _AUTHORIZATION_METADATA:
            return httpx2.Response(200, json=self.authorization_document())
        if url == self.token_endpoint:
            return httpx2.Response(self.token_status, json=self.token_document)
        if url == self.revocation_endpoint:
            return httpx2.Response(200, content=b"")
        if url == self.registration_endpoint:
            return httpx2.Response(
                self.registration_status,
                json=self.registration_document,
            )
        raise AssertionError(f"unexpected OAuth request: {request.method} {url}")


def _parameters(
    profile: OAuthProfile,
    *,
    scopes: tuple[str, ...] = ("repo", "read:user"),
) -> OAuthAuthorizationParameters:
    client_id = (
        profile.client_id
        if profile.client_mode is OAuthClientMode.PREREGISTERED
        else profile.client_metadata_document_url
    )
    assert client_id is not None
    return OAuthAuthorizationParameters(
        client_id=client_id,
        redirect_uri=profile.redirect_uri,
        scopes=scopes,
        state="opaque-state",
        code_challenge="challenge",
        resource_origin=profile.resource_origins[0],
    )


@pytest.mark.anyio
async def test_protocol_discovers_builds_pkce_and_exchanges_only_at_bound_endpoint() -> None:
    profile = _profile()
    server = _OAuthServer()
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport, clock=lambda: 100.0)

    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)
    parameters = _parameters(profile)
    url = protocol.build_authorization_url(discovery, parameters)
    query = parse_qs(urlsplit(url).query, strict_parsing=True)
    token = await protocol.exchange(
        discovery,
        parameters,
        code="opaque-code",
        code_verifier="verifier",
        client_secret="desktop-secret",
    )

    assert urlsplit(url)._replace(query="").geturl() == _AUTHORIZATION_ENDPOINT
    assert query == {
        "client_id": ["omega-public-client"],
        "code_challenge": ["challenge"],
        "code_challenge_method": ["S256"],
        "redirect_uri": [_REDIRECT],
        "resource": [_RESOURCE],
        "response_type": ["code"],
        "scope": ["repo read:user"],
        "state": ["opaque-state"],
    }
    assert token.access_token == "opaque-access"
    assert token.refresh_token == "opaque-refresh"
    assert token.expires_at == 160.0
    assert "opaque-access" not in repr(token)
    assert "opaque-refresh" not in repr(token)
    assert transport.secret_destinations == {discovery.token_endpoint}
    assert all("evil.example" not in str(request.url) for request in transport.requests)


@pytest.mark.anyio
async def test_register_posts_the_signed_public_client_document_and_returns_redacted_result() -> None:
    profile = _dynamic_profile()
    server = _OAuthServer()
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)

    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)
    response = await protocol.register(discovery, profile=profile)

    request = transport.requests[-1]
    assert request.method == "POST"
    assert str(request.url) == _REGISTRATION_ENDPOINT
    assert json.loads(request.content) == {
        "client_name": "Omega",
        "redirect_uris": [_REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "repo read:user",
        "software_id": "org.omega2ai.desktop",
    }
    assert request.headers["Content-Type"] == "application/json"
    assert response.client_id == "dynamic-client"
    assert response.registration_access_token is None
    assert response.registration_client_uri is None
    assert "dynamic-client" not in repr(response)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "registration_document",
    (
        {},
        {"client_id": "dynamic-client", "token_endpoint_auth_method": "client_secret_post"},
        # A management token with nothing to spend it on is malformed.
        {"client_id": "dynamic-client", "registration_access_token": "management-token"},
        {
            "client_id": "dynamic-client",
            "registration_access_token": "management-token",
            "registration_client_uri": "https://evil.example/register/dynamic-client",
        },
        # A relative reference must not become a way past the same-origin check.
        {
            "client_id": "dynamic-client",
            "registration_access_token": "management-token",
            "registration_client_uri": "//evil.example/register/dynamic-client",
        },
    ),
)
async def test_register_rejects_invalid_public_client_responses(
    registration_document: dict[str, object],
) -> None:
    server = _OAuthServer()
    server.registration_document = registration_document
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)

    with pytest.raises(OAuthProtocolError, match="^oauth_registration_invalid$"):
        await protocol.register(discovery, profile=profile)


@pytest.mark.anyio
@pytest.mark.parametrize("secret", ("provider-secret", "", [], {}, None))
async def test_a_volunteered_client_secret_is_read_past_and_never_carried(
    secret: object,
) -> None:
    """Omega asked for a public client; a secret beside ``none`` is not its business.

    Refusing the registration over it was the rule, and it made Neon -- which
    answers ``token_endpoint_auth_method: none`` AND a 32-character secret --
    impossible to register at all. The response type has nowhere to put a
    secret, so reading past it is what "never stored" already meant.
    """
    server = _OAuthServer()
    server.registration_document = {
        "client_id": "dynamic-client",
        "client_secret": secret,
        "token_endpoint_auth_method": "none",
    }
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)

    response = await protocol.register(discovery, profile=profile)

    assert response.client_id == "dynamic-client"
    # The field exists now -- a confidential server's secret lands in it -- but
    # a server that accepts ``none`` never has one read, whatever it sends.
    assert response.client_secret is None
    assert "provider-secret" not in repr(response)


@pytest.mark.anyio
async def test_a_management_uri_without_its_token_is_dropped_not_refused() -> None:
    """RFC 7591 pairs the URI with the token; six providers send the URI alone.

    ``deregister`` needs both, so a lone URI names a registration that cannot be
    managed remotely. That is a capability Omega does not get, not a reason to
    throw away the client_id the provider just issued.
    """
    server = _OAuthServer()
    server.registration_document = {
        "client_id": "dynamic-client",
        "registration_client_uri": f"{_REGISTRATION_ENDPOINT}/dynamic-client",
    }
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)

    response = await protocol.register(discovery, profile=profile)

    assert response.client_id == "dynamic-client"
    assert response.registration_client_uri is None
    assert response.registration_access_token is None


@pytest.mark.anyio
async def test_a_relative_management_uri_resolves_against_the_registration_endpoint() -> None:
    """Providers on the common worker OAuth stack answer with a path.

    Resolving it widens nothing: the same-origin check against the issuer still
    runs on the result, which is what the refusal cases above pin.
    """
    server = _OAuthServer()
    server.registration_document = {
        "client_id": "dynamic-client",
        "registration_access_token": "management-token",
        "registration_client_uri": "/register/dynamic-client",
    }
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)

    response = await protocol.register(discovery, profile=profile)

    assert response.registration_client_uri == f"{_REGISTRATION_ENDPOINT}/dynamic-client"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "registration_endpoint",
    (
        None,
        "https://evil.example/register",
        "https://user@issuer.example/register",
        "https://issuer.example/register#fragment",
    ),
)
async def test_registration_requires_an_exact_issuer_origin_endpoint(
    registration_endpoint: str | None,
) -> None:
    server = _OAuthServer()
    server.registration_endpoint = registration_endpoint
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()

    if registration_endpoint is None:
        discovery = await protocol.discover(profile, resource_origin=_RESOURCE)
        with pytest.raises(OAuthProtocolError, match="^oauth_registration_unsupported$"):
            await protocol.register(discovery, profile=profile)
    else:
        with pytest.raises(OAuthProtocolError, match="^oauth_metadata_invalid$"):
            await protocol.discover(profile, resource_origin=_RESOURCE)


@pytest.mark.anyio
async def test_deregister_sends_bearer_delete_only_to_the_exact_issuer_origin() -> None:
    registration_uri = "https://issuer.example/register/dynamic-client"
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(204)

    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(handler))

    await protocol.deregister(
        registration_uri,
        registration_access_token="management-token",
    )

    assert requests[0].method == "DELETE"
    assert str(requests[0].url) == registration_uri
    assert requests[0].headers["Authorization"] == "Bearer management-token"


@pytest.mark.anyio
async def test_deregister_rejects_an_invalid_management_uri_before_network() -> None:
    transport = RecordingOAuthTransport(lambda _request: httpx2.Response(204))
    protocol = OAuthProtocolClient(transport=transport)

    with pytest.raises(OAuthProtocolError, match="^oauth_registration_invalid$"):
        await protocol.deregister(
            "http://issuer.example/register/dynamic-client",
            registration_access_token="management-token",
        )

    assert transport.requests == []


@pytest.mark.anyio
async def test_device_flow_uses_only_discovered_endpoints_and_never_a_client_secret() -> None:
    profile = _device_profile()
    resource_metadata = "https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"
    authorization_metadata = "https://github.com/.well-known/oauth-authorization-server/login/oauth"
    device_endpoint = "https://github.com/login/device/code"
    token_endpoint = "https://github.com/login/oauth/access_token"
    polls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal polls
        url = str(request.url)
        if url == resource_metadata:
            return httpx2.Response(
                200,
                json={
                    "resource": profile.resource_origins[0],
                    "authorization_servers": [profile.authorization_issuer],
                },
            )
        if url == authorization_metadata:
            return httpx2.Response(
                200,
                json={
                    "issuer": profile.authorization_issuer,
                    "token_endpoint": token_endpoint,
                    "device_authorization_endpoint": device_endpoint,
                    "grant_types_supported": ["urn:ietf:params:oauth:grant-type:device_code"],
                    # GitHub currently advertises only offline_access here even
                    # though its OAuth endpoints accept the signed app scopes.
                    "scopes_supported": ["offline_access"],
                },
            )
        if url == device_endpoint:
            assert b"client_secret" not in request.content
            return httpx2.Response(
                200,
                json={
                    "device_code": "opaque-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "verification_uri_complete": (
                        "https://github.com/login/device?user_code=ABCD-EFGH"
                    ),
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if url == token_endpoint:
            polls += 1
            assert b"client_secret" not in request.content
            if polls == 1:
                return httpx2.Response(200, json={"error": "authorization_pending"})
            return httpx2.Response(
                200,
                json={
                    "access_token": "opaque-access",
                    "token_type": "bearer",
                    "scope": "repo read:user",
                },
            )
        raise AssertionError(f"unexpected OAuth request: {request.method} {url}")

    transport = RecordingOAuthTransport(handler)
    protocol = OAuthProtocolClient(transport=transport, clock=lambda: 100.0)
    discovery = await protocol.discover(profile, resource_origin=profile.resource_origins[0])
    authorization = await protocol.begin_device_authorization(
        discovery,
        client_id=profile.client_id,
        scopes=("repo", "read:user"),
    )

    assert isinstance(authorization, OAuthDeviceAuthorization)
    assert authorization.user_code == "ABCD-EFGH"
    assert authorization.verification_uri == "https://github.com/login/device"
    assert authorization.expires_at == 1000.0
    assert authorization.interval_seconds == 5
    assert "opaque-device-code" not in repr(authorization)
    with pytest.raises(OAuthProtocolError, match="^oauth_device_authorization_pending$"):
        await protocol.poll_device_token(
            discovery,
            authorization,
            client_id=profile.client_id,
            required_scopes=("repo", "read:user"),
        )
    token = await protocol.poll_device_token(
        discovery,
        authorization,
        client_id=profile.client_id,
        required_scopes=("repo", "read:user"),
    )

    assert token.access_token == "opaque-access"
    assert transport.secret_destinations == set()


@pytest.mark.anyio
async def test_device_flow_disabled_is_an_actionable_content_free_provider_error() -> None:
    profile = _device_profile()
    resource_metadata = "https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"
    authorization_metadata = "https://github.com/.well-known/oauth-authorization-server/login/oauth"
    device_endpoint = "https://github.com/login/device/code"

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == resource_metadata:
            return httpx2.Response(
                200,
                json={
                    "resource": profile.resource_origins[0],
                    "authorization_servers": [profile.authorization_issuer],
                },
            )
        if url == authorization_metadata:
            return httpx2.Response(
                200,
                json={
                    "issuer": profile.authorization_issuer,
                    "token_endpoint": "https://github.com/login/oauth/access_token",
                    "device_authorization_endpoint": device_endpoint,
                    "grant_types_supported": ["urn:ietf:params:oauth:grant-type:device_code"],
                    "scopes_supported": ["repo", "read:user"],
                },
            )
        if url == device_endpoint:
            return httpx2.Response(
                400,
                json={
                    "error": "device_flow_disabled",
                    "error_description": "private provider detail must not escape",
                },
            )
        raise AssertionError(f"unexpected OAuth request: {request.method} {url}")

    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(handler))
    discovery = await protocol.discover(profile, resource_origin=profile.resource_origins[0])

    with pytest.raises(OAuthProviderError, match="^oauth_device_flow_disabled$") as raised:
        await protocol.begin_device_authorization(
            discovery,
            client_id=profile.client_id,
            scopes=("repo", "read:user"),
        )

    assert "private provider detail" not in str(raised.value)
    assert "private provider detail" not in repr(raised.value)


@pytest.mark.anyio
async def test_exchange_requests_json_and_normalizes_bearer_and_comma_scopes() -> None:
    server = _OAuthServer()
    server.token_document = {
        "access_token": "exchange-access",
        "refresh_token": "exchange-refresh",
        "token_type": "bearer",
        "expires_in": 60,
        "scope": "repo,read:user",
        "resource": _RESOURCE,
    }
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    token = await protocol.exchange(
        discovery,
        _parameters(_profile()),
        code="opaque-code",
        code_verifier="verifier",
        client_secret=None,
    )

    assert token.token_type == "Bearer"
    assert token.scopes == ("repo", "read:user")
    assert token.access_token == "exchange-access"
    assert [request.headers["Accept"] for request in transport.requests] == [
        "application/json",
        "application/json",
        "application/json",
    ]
    assert [request.headers["Accept-Encoding"] for request in transport.requests] == [
        "identity",
        "identity",
        "identity",
    ]


@pytest.mark.anyio
async def test_dotted_opaque_access_token_is_never_inferred_to_be_a_jwt() -> None:
    server = _OAuthServer()
    server.token_document = {
        "access_token": "alpha.beta.gamma",
        "token_type": "Bearer",
        "expires_in": 60,
        "scope": "repo read:user",
    }
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    token = await protocol.exchange(
        discovery,
        _parameters(_profile()),
        code="opaque-code",
        code_verifier="verifier",
        client_secret=None,
    )

    assert token.access_token == "alpha.beta.gamma"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("resource", "expected_url"),
    [
        (
            "https://resource.example/mcp",
            "https://resource.example/.well-known/oauth-protected-resource/mcp",
        ),
        (
            "https://resource.example/mcp/v1/",
            "https://resource.example/.well-known/oauth-protected-resource/mcp/v1/",
        ),
    ],
)
async def test_discovery_constructs_the_exact_protected_resource_metadata_path(
    resource: str,
    expected_url: str,
) -> None:
    profile = replace(_profile(), resource_origins=(resource,))
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx2.Response(
                200,
                json={"resource": resource, "authorization_servers": [_ISSUER]},
            )
        return httpx2.Response(200, json=_OAuthServer().authorization_document())

    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(handler))

    await protocol.discover(profile, resource_origin=resource)

    assert seen == [expected_url, _AUTHORIZATION_METADATA]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("resource", "oauth_resource_mismatch"),
        ("protected_issuer", "oauth_issuer_mismatch"),
        ("metadata_issuer", "oauth_issuer_mismatch"),
    ],
)
async def test_discovery_rejects_resource_and_issuer_mismatch_before_endpoint_use(
    mutation: str,
    reason: str,
) -> None:
    server = _OAuthServer()
    if mutation == "resource":
        server.resource = "https://other.example/mcp"
    elif mutation == "protected_issuer":
        server.authorization_servers = ["https://evil.example"]
    else:
        server.metadata_issuer = "https://evil.example"
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)

    with pytest.raises(OAuthProtocolError, match=f"^{reason}$"):
        await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert all("evil.example" not in str(request.url) for request in transport.requests)


@pytest.mark.anyio
async def test_discovery_accepts_root_slash_reference_but_keeps_pinned_issuer() -> None:
    """Google PRM references accounts.google.com/; its AS issuer has no slash."""
    server = _OAuthServer()
    server.authorization_servers = [_ISSUER + "/"]
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    try:
        discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)
        assert discovery.issuer == "https://issuer.example"
        assert discovery.authorization_endpoint == _AUTHORIZATION_ENDPOINT
        assert [str(request.url) for request in transport.requests] == [
            _PROTECTED_METADATA, _AUTHORIZATION_METADATA,
        ]
    finally:
        await protocol.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("reference", [
    "https://issuer.example//", "https://issuer.example/tenant/",
    "https://issuer.example/?next=other", "https://issuer.example/#fragment",
    "http://issuer.example/", "https://issuer.example:443/",
    "https://user@issuer.example/", "https://issuer.example.evil.test/",
])
async def test_discovery_root_reference_does_not_relax_other_url_components(reference) -> None:
    server = _OAuthServer()
    server.authorization_servers = [reference]
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    try:
        with pytest.raises(OAuthProtocolError, match="^oauth_issuer_mismatch$"):
            await protocol.discover(_profile(), resource_origin=_RESOURCE)
        assert len(transport.requests) == 1
    finally:
        await protocol.aclose()


@pytest.mark.anyio
async def test_discovery_root_reference_still_requires_exact_metadata_issuer() -> None:
    server = _OAuthServer()
    server.authorization_servers = [_ISSUER + "/"]
    server.metadata_issuer = _ISSUER + "/"
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    try:
        with pytest.raises(OAuthProtocolError, match="^oauth_issuer_mismatch$"):
            await protocol.discover(_profile(), resource_origin=_RESOURCE)
        assert len(transport.requests) == 2
    finally:
        await protocol.aclose()


@pytest.mark.anyio
async def test_discovery_never_trims_a_path_scoped_issuer_reference() -> None:
    server = _OAuthServer()
    server.authorization_servers = ["https://issuer.example/tenant/"]
    profile = replace(_profile(), authorization_issuer="https://issuer.example/tenant")
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    try:
        with pytest.raises(OAuthProtocolError, match="^oauth_issuer_mismatch$"):
            await protocol.discover(profile, resource_origin=_RESOURCE)
        assert len(transport.requests) == 1
    finally:
        await protocol.aclose()


@pytest.mark.anyio
async def test_verified_metadata_may_publish_endpoints_on_another_https_origin() -> None:
    server = _OAuthServer()
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))

    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert discovery.authorization_endpoint == _AUTHORIZATION_ENDPOINT
    assert discovery.token_endpoint == _TOKEN_ENDPOINT
    assert discovery.revocation_endpoint == _REVOCATION_ENDPOINT


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization_endpoint", "http://login.example/authorize"),
        ("token_endpoint", "https://user@tokens.example/token"),
        ("revocation_endpoint", "https://tokens.example/revoke?next=evil"),
    ],
)
async def test_discovery_rejects_structurally_unsafe_verified_metadata_endpoints(
    field: str,
    value: str,
) -> None:
    server = _OAuthServer()
    setattr(server, field, value)
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)

    with pytest.raises(OAuthProtocolError, match="^oauth_metadata_invalid$"):
        await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert all(str(request.url) != value for request in transport.requests)


@pytest.mark.anyio
async def test_redirect_is_not_followed_and_response_stream_is_closed() -> None:
    stream = _CloseTrackingStream((b"redirect-body",))
    transport = RecordingOAuthTransport(
        lambda _request: httpx2.Response(
            302,
            headers={"Location": "https://evil.example/metadata"},
            stream=stream,
        )
    )
    protocol = OAuthProtocolClient(transport=transport)

    with pytest.raises(OAuthProtocolError, match="^oauth_redirect_rejected$"):
        await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert len(transport.requests) == 1
    assert stream.close_calls == 1


@pytest.mark.anyio
async def test_malformed_json_is_content_free_and_stream_is_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "malformed-private-marker"
    stream = _CloseTrackingStream((f'{{"marker":"{marker}"'.encode(),))
    transport = RecordingOAuthTransport(lambda _request: httpx2.Response(200, stream=stream))
    protocol = OAuthProtocolClient(transport=transport)

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(OAuthProtocolError, match="^oauth_response_invalid$") as raised,
    ):
        await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert marker not in str(raised.value)
    assert marker not in repr(raised.value)
    assert marker not in caplog.text
    assert raised.value.__cause__ is None
    assert stream.close_calls == 1


@pytest.mark.anyio
async def test_deep_json_is_rejected_with_a_content_free_stable_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "deep-private-marker"
    body = (
        b'{"resource":"https://resource.example/mcp",'
        b'"authorization_servers":["https://issuer.example"],"extra":'
        + b"[" * 80
        + f'"{marker}"'.encode()
        + b"]" * 80
        + b"}"
    )
    stream = _CloseTrackingStream((body,))
    server = _OAuthServer()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == _PROTECTED_METADATA:
            return httpx2.Response(200, stream=stream)
        return server(request)

    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(handler))

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(OAuthProtocolError, match="^oauth_response_invalid$") as raised,
    ):
        await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert marker not in str(raised.value)
    assert marker not in repr(raised.value)
    assert marker not in caplog.text
    assert raised.value.__cause__ is None
    assert stream.close_calls == 1


@pytest.mark.anyio
async def test_huge_json_integer_is_rejected_with_a_stable_protocol_error() -> None:
    server = _OAuthServer()
    body = (
        b'{"access_token":"opaque-access","token_type":"Bearer","expires_in":'
        + b"9" * 5_000
        + b',"scope":"repo read:user"}'
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == _TOKEN_ENDPOINT:
            return httpx2.Response(200, content=body)
        return server(request)

    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(handler))
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    with pytest.raises(OAuthProtocolError, match="^oauth_response_invalid$") as raised:
        await protocol.exchange(
            discovery,
            _parameters(_profile()),
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )

    assert raised.value.__cause__ is None


@pytest.mark.anyio
async def test_encoded_response_is_rejected_before_decompression_or_iteration() -> None:
    server = _OAuthServer()
    protected_body = (
        b'{"resource":"https://resource.example/mcp",'
        b'"authorization_servers":["https://issuer.example"]}'
    )
    stream = _CloseTrackingStream((gzip.compress(protected_body),))

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == _PROTECTED_METADATA:
            return httpx2.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=stream,
            )
        return server(request)

    transport = RecordingOAuthTransport(handler)
    protocol = OAuthProtocolClient(transport=transport)

    with pytest.raises(
        OAuthProtocolError,
        match="^oauth_response_encoding_unsupported$",
    ):
        await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert transport.requests[0].headers["Accept-Encoding"] == "identity"
    assert not stream.started.is_set()
    assert stream.close_calls == 1


@pytest.mark.anyio
async def test_descriptor_response_limit_is_enforced_and_stream_is_closed() -> None:
    stream = _CloseTrackingStream((b"x" * 33,))
    transport = RecordingOAuthTransport(lambda _request: httpx2.Response(200, stream=stream))
    protocol = OAuthProtocolClient(
        transport=transport,
        limits=McpClientLimits(max_discovery_document_bytes=32),
    )

    with pytest.raises(OAuthProtocolError, match="^oauth_response_too_large$"):
        await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert stream.close_calls == 1


@pytest.mark.anyio
async def test_descriptor_timeout_is_enforced_and_stream_is_closed() -> None:
    stream = _CloseTrackingStream((), wait_forever=True)
    transport = RecordingOAuthTransport(lambda _request: httpx2.Response(200, stream=stream))
    protocol = OAuthProtocolClient(
        transport=transport,
        limits=McpClientLimits(max_operation_duration_ms=10),
    )

    with pytest.raises(OAuthNetworkError, match="^oauth_provider_timeout$"):
        await protocol.discover(_profile(), resource_origin=_RESOURCE)

    assert stream.started.is_set()
    assert stream.close_calls == 1


@pytest.mark.anyio
async def test_provider_denial_and_network_failure_remain_distinct() -> None:
    server = _OAuthServer()
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)
    server.token_status = 400
    server.token_document = {
        "error": "access_denied",
        "error_description": "private provider payload",
    }

    with pytest.raises(OAuthProviderError, match="^oauth_provider_rejected$"):
        await protocol.exchange(
            discovery,
            _parameters(_profile()),
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )

    async def network_failure(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == _TOKEN_ENDPOINT:
            raise httpx2.ConnectError("private network payload", request=request)
        return server(request)

    second = OAuthProtocolClient(transport=RecordingOAuthTransport(network_failure))
    second_discovery = await second.discover(_profile(), resource_origin=_RESOURCE)
    with pytest.raises(OAuthNetworkError, match="^oauth_provider_unreachable$"):
        await second.exchange(
            second_discovery,
            _parameters(_profile()),
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        # Missing one of the requested scopes: the package asked for something
        # the operator did not approve, and that still refuses. A SUPERSET no
        # longer does -- see the coverage tests below.
        ("scope", "repo", "oauth_scope_mismatch"),
        ("token_type", "DPoP", "oauth_token_invalid"),
        ("resource", "https://other.example/mcp", "oauth_resource_mismatch"),
    ],
)
async def test_token_response_requires_exact_scope_bearer_and_resource_binding(
    field: str,
    value: object,
    reason: str,
) -> None:
    server = _OAuthServer()
    assert isinstance(server.token_document, dict)
    server.token_document[field] = value
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    with pytest.raises(OAuthProtocolError, match=f"^{reason}$"):
        await protocol.exchange(
            discovery,
            _parameters(_profile()),
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", "opaque-access\nfield-private-marker"),
        ("refresh_token", "x" * 16_385 + "field-private-marker"),
    ],
    ids=["malformed-access-token", "oversized-refresh-token"],
)
async def test_malformed_or_oversized_token_fields_are_rejected_without_content(
    field: str,
    value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _OAuthServer()
    assert isinstance(server.token_document, dict)
    server.token_document[field] = value
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(OAuthProtocolError, match="^oauth_token_invalid$") as raised,
    ):
        await protocol.exchange(
            discovery,
            _parameters(_profile()),
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )

    assert "field-private-marker" not in str(raised.value)
    assert "field-private-marker" not in repr(raised.value)
    assert "field-private-marker" not in caplog.text
    assert raised.value.__cause__ is None


@pytest.mark.anyio
async def test_malformed_unicode_token_field_is_a_content_free_protocol_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "unicode-private-marker"
    server = _OAuthServer()
    body = (
        b'{"access_token":"\\ud800unicode-private-marker",'
        b'"token_type":"Bearer","scope":"repo read:user"}'
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == _TOKEN_ENDPOINT:
            return httpx2.Response(200, content=body)
        return server(request)

    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(handler))
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(OAuthProtocolError, match="^oauth_token_invalid$") as raised,
    ):
        await protocol.exchange(
            discovery,
            _parameters(_profile()),
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )

    assert marker not in str(raised.value)
    assert marker not in repr(raised.value)
    assert marker not in caplog.text
    assert raised.value.__cause__ is None


@pytest.mark.anyio
async def test_extreme_expiry_is_rejected_as_a_stable_token_error() -> None:
    server = _OAuthServer()
    assert isinstance(server.token_document, dict)
    server.token_document["expires_in"] = 10**400
    protocol = OAuthProtocolClient(
        transport=RecordingOAuthTransport(server),
        clock=lambda: 1.0,
    )
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    with pytest.raises(OAuthProtocolError, match="^oauth_token_invalid$") as raised:
        await protocol.exchange(
            discovery,
            _parameters(_profile()),
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )

    assert raised.value.__cause__ is None


@pytest.mark.anyio
async def test_refresh_preserves_omitted_refresh_token_and_omits_absent_secret() -> None:
    server = _OAuthServer()
    server.token_document = {
        "access_token": "refreshed-access",
        "token_type": "bearer",
        "expires_in": 30,
        "scope": "read:user,repo",
        "resource": _RESOURCE,
    }
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport, clock=lambda: 25.0)
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    token = await protocol.refresh(
        discovery,
        "omega-public-client",
        "existing-refresh",
        client_secret=None,
        required_scopes=("repo", "read:user"),
    )

    request = transport.requests[-1]
    form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
    assert form == {
        "client_id": ["omega-public-client"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["existing-refresh"],
        "resource": [_RESOURCE],
    }
    assert token.refresh_token == "existing-refresh"
    assert token.token_type == "Bearer"
    assert token.scopes == ("repo", "read:user")
    assert token.expires_at == 55.0
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Accept-Encoding"] == "identity"


@pytest.mark.anyio
async def test_refresh_preserves_exact_invalid_client_provider_cause() -> None:
    server = _OAuthServer()
    server.token_status = 401
    server.token_document = {"error": "invalid_client"}
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    discovery = await protocol.discover(_dynamic_profile(), resource_origin=_RESOURCE)

    with pytest.raises(OAuthProviderError, match="^oauth_client_invalid$"):
        await protocol.refresh(
            discovery,
            "dynamic-client",
            "existing-refresh",
            client_secret=None,
            required_scopes=("repo", "read:user"),
        )


@pytest.mark.anyio
async def test_concurrent_refreshes_are_not_hidden_by_a_protocol_global_lock() -> None:
    """The later broker partition lock, not this protocol client, owns serialization."""
    server = _OAuthServer()
    entered = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal entered
        if str(request.url) == _TOKEN_ENDPOINT:
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()
        return server(request)

    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(handler))
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)
    calls = [
        asyncio.create_task(
            protocol.refresh(
                discovery,
                "omega-public-client",
                f"refresh-{index}",
                client_secret=None,
                required_scopes=("repo", "read:user"),
            )
        )
        for index in range(2)
    ]
    try:
        await asyncio.wait_for(both_entered.wait(), timeout=1)
    finally:
        release.set()
    tokens = await asyncio.gather(*calls)

    assert entered == 2
    assert [token.access_token for token in tokens] == ["opaque-access", "opaque-access"]


@pytest.mark.anyio
async def test_revoke_returns_noop_only_when_verified_metadata_omits_endpoint() -> None:
    server = _OAuthServer()
    server.revocation_endpoint = None
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)
    request_count = len(transport.requests)

    receipt = await protocol.revoke(
        discovery,
        client_id="omega-public-client",
        token="opaque-refresh",
        client_secret="desktop-secret",
    )

    assert receipt.endpoint_advertised is False
    assert receipt.provider_request_sent is False
    assert len(transport.requests) == request_count


@pytest.mark.anyio
async def test_revoke_uses_only_the_discovery_bound_endpoint_and_redacts_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _OAuthServer()
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)

    with caplog.at_level(logging.DEBUG):
        receipt = await protocol.revoke(
            discovery,
            client_id="omega-public-client",
            token="opaque-refresh",
            client_secret="desktop-secret",
        )

    assert receipt.endpoint_advertised is True
    assert receipt.provider_request_sent is True
    assert transport.secret_destinations == {_REVOCATION_ENDPOINT}
    exposed = " ".join(
        [repr(protocol), repr(discovery), repr(receipt), caplog.text]
        + [repr(request) for request in transport.requests]
    )
    assert "desktop-secret" not in exposed
    assert "opaque-refresh" not in exposed


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["network", "provider"])
async def test_revoke_network_and_provider_errors_are_not_noop_receipts(
    failure: str,
) -> None:
    server = _OAuthServer()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == _REVOCATION_ENDPOINT:
            if failure == "network":
                raise httpx2.ConnectError("offline", request=request)
            return httpx2.Response(503, content=b"private provider response")
        return server(request)

    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(handler))
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)
    expected = OAuthNetworkError if failure == "network" else OAuthProviderError

    with pytest.raises(expected):
        await protocol.revoke(
            discovery,
            client_id="omega-public-client",
            token="opaque-refresh",
            client_secret=None,
        )


@pytest.mark.parametrize(
    ("mode", "credential", "expected_client_id"),
    [
        (
            OAuthClientMode.PREREGISTERED,
            OAuthClientCredential("omega-public-client", "desktop-secret"),
            "omega-public-client",
        ),
        (
            OAuthClientMode.CLIENT_METADATA_DOCUMENT,
            None,
            "https://omega.example/oauth/example-client.json",
        ),
    ],
)
def test_signed_client_mode_selects_only_its_exact_identity_without_fallback(
    mode: OAuthClientMode,
    credential: OAuthClientCredential | None,
    expected_client_id: str,
) -> None:
    profile = _profile(mode)

    parameters = OAuthAuthorizationParameters.for_profile(
        profile,
        credential=credential,
        scopes=("repo", "read:user"),
        state="opaque-state",
        code_challenge="challenge",
        resource_origin=_RESOURCE,
    )

    assert parameters.client_id == expected_client_id


@pytest.mark.parametrize(
    ("mode", "credential"),
    [
        (OAuthClientMode.PREREGISTERED, None),
        (
            OAuthClientMode.PREREGISTERED,
            OAuthClientCredential("other-client", "desktop-secret"),
        ),
        (
            OAuthClientMode.CLIENT_METADATA_DOCUMENT,
            OAuthClientCredential("omega-public-client", None),
        ),
    ],
)
def test_signed_client_mode_rejects_cross_mode_identity_and_dcr_fallback(
    mode: OAuthClientMode,
    credential: OAuthClientCredential | None,
) -> None:
    with pytest.raises(OAuthProtocolError, match="^oauth_client_mode_mismatch$"):
        OAuthAuthorizationParameters.for_profile(
            _profile(mode),
            credential=credential,
            scopes=("repo", "read:user"),
            state="opaque-state",
            code_challenge="challenge",
            resource_origin=_RESOURCE,
        )


@pytest.mark.anyio
async def test_discovery_objects_cannot_be_forged_or_mutated_into_endpoint_authority() -> None:
    server = _OAuthServer()
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    discovery = await protocol.discover(_profile(), resource_origin=_RESOURCE)
    object.__setattr__(discovery, "token_endpoint", "https://evil.example/token")

    with pytest.raises(OAuthProtocolError, match="^oauth_discovery_invalid$"):
        await protocol.exchange(
            discovery,
            _parameters(_profile()),
            code="opaque-code",
            code_verifier="verifier",
            client_secret="desktop-secret",
        )

    assert all("evil.example" not in str(request.url) for request in transport.requests)


def test_authorization_parameters_are_redacted() -> None:
    profile = _profile()
    parameters = _parameters(profile)

    assert "opaque-state" not in repr(parameters)
    assert "challenge" not in repr(parameters)


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_an_empty_returned_scope_states_nothing(value: str) -> None:
    """RFC 6749 s5.1 makes `scope` optional and requires it only when the
    granted set DIFFERS. A provider sending an empty string has said nothing,
    which is what omitting it means -- reading that as a mismatch invents one
    out of an absence, and it is what refused Cloudflare's sign-in.
    """
    from mcp_platform.oauth_protocol import _returned_scopes

    assert _returned_scopes(value, ("account:read",)) == ("account:read",)


def test_a_granted_scope_only_has_to_cover_what_was_asked() -> None:
    """Cloudflare answers a request for one scope with 384 of them.

    That is its own consent screen's decision, made in front of the operator,
    and demanding set equality refused a token that plainly covered everything
    the package declared. What Omega records is still what it asked for: the
    extra grants are not authority the package declared, so it does not take
    them on.
    """
    from mcp_platform.oauth_protocol import _returned_scopes

    granted = " ".join(f"scope{index}" for index in range(384))
    assert _returned_scopes(f"{granted} account:read", ("account:read",)) == (
        "account:read",
    )
    # Commas are the other spelling some servers use, and a repeat is not a
    # disagreement about anything.
    assert _returned_scopes("a, b, a", ("a", "b")) == ("a", "b")


def test_a_returned_scope_missing_what_was_asked_is_refused() -> None:
    from mcp_platform.oauth_protocol import OAuthProtocolError, _returned_scopes

    with pytest.raises(OAuthProtocolError, match="^oauth_scope_mismatch$"):
        _returned_scopes("b c", ("a", "b"))


def test_an_unreadable_returned_scope_is_still_refused() -> None:
    """Bounded and plain, because it is read at all."""
    from mcp_platform.oauth_protocol import OAuthProtocolError, _returned_scopes

    for unreadable in ("account:read \x01", "x" * 40_000):
        with pytest.raises(OAuthProtocolError, match="^oauth_scope_mismatch$"):
            _returned_scopes(unreadable, ("account:read",))


# --- a dynamically registered CONFIDENTIAL client ----------------------------
#
# Supabase and monday.com advertise ``client_secret_basic``/``client_secret_post``
# with no ``none``: their token endpoints refuse an unauthenticated client.
# Supabase's registration issues a real secret, Omega discarded it, and the
# exchange answered 422 -- the plugin could register and never connect.


@pytest.mark.parametrize(
    ("methods", "required"),
    [
        # Stated and lacking "none": the server refuses a public client.
        (["client_secret_basic", "client_secret_post"], True),
        (["client_secret_post"], True),
        # "none" present: a public client is welcome, so no secret is presented
        # even if the server volunteers one. Neon, Notion, Linear and Wix all
        # look like this, and all four connect as public clients today.
        (["client_secret_post", "client_secret_basic", "none"], False),
        (["none"], False),
        # Unstated is not a claim that a secret is required -- and most
        # providers state nothing at all.
        ([], False),
    ],
)
def test_only_a_stated_list_without_none_requires_client_authentication(
    methods: list[str], required: bool
) -> None:
    from mcp_platform.oauth_protocol import _requires_client_authentication

    class _Discovery:
        token_endpoint_auth_methods_supported = tuple(methods)

    assert _requires_client_authentication(_Discovery()) is required


@pytest.mark.anyio
async def test_a_confidential_registration_keeps_its_secret() -> None:
    server = _OAuthServer()
    server.token_endpoint_auth_methods_supported = [
        "client_secret_basic",
        "client_secret_post",
    ]
    server.registration_document = {
        "client_id": "dynamic-client",
        "client_secret": "issued-secret",
        "client_secret_expires_at": 0,
    }
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)

    response = await protocol.register(discovery, profile=profile)

    assert response.client_secret == "issued-secret"
    # 0 means "does not expire", not "expired at the epoch".
    assert response.client_secret_expires_at is None
    assert "issued-secret" not in repr(response)


@pytest.mark.anyio
async def test_a_public_server_that_volunteers_a_secret_is_still_read_past() -> None:
    """Neon returns a secret alongside ``none``. It takes no part in anything."""
    server = _OAuthServer()
    server.token_endpoint_auth_methods_supported = ["client_secret_post", "none"]
    server.registration_document = {
        "client_id": "dynamic-client",
        "client_secret": "volunteered",
    }
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)

    response = await protocol.register(discovery, profile=profile)

    assert response.client_secret is None


@pytest.mark.anyio
async def test_a_confidential_method_without_a_secret_is_refused() -> None:
    """A response naming a method it gave Omega nothing to perform."""
    server = _OAuthServer()
    server.token_endpoint_auth_methods_supported = ["client_secret_post"]
    server.registration_document = {
        "client_id": "dynamic-client",
        "token_endpoint_auth_method": "client_secret_post",
    }
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)

    with pytest.raises(OAuthProtocolError, match="^oauth_registration_invalid$"):
        await protocol.register(discovery, profile=profile)


@pytest.mark.anyio
async def test_the_exchange_presents_a_dynamically_issued_secret() -> None:
    """The whole point: it has to reach the token endpoint.

    A dynamic client has no signed ``client_secret_ref`` to advertise -- its
    secret does not exist until the server issues one -- so `_client_secret`
    admitted nothing for it and the form went out unauthenticated.
    """
    server = _OAuthServer()
    server.token_endpoint_auth_methods_supported = [
        "client_secret_basic",
        "client_secret_post",
    ]
    transport = RecordingOAuthTransport(server)
    protocol = OAuthProtocolClient(transport=transport)
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)
    parameters = OAuthAuthorizationParameters(
        client_id="dynamic-client",
        redirect_uri=profile.redirect_uri,
        scopes=("repo", "read:user"),
        state="opaque-state",
        code_challenge="challenge",
        resource_origin=profile.resource_origins[0],
    )

    await protocol.exchange(
        discovery,
        parameters,
        code="opaque-code",
        code_verifier="verifier",
        client_secret="issued-secret",
    )

    posted = [r for r in transport.requests if str(r.url) == server.token_endpoint]
    assert posted
    assert b"client_secret=issued-secret" in posted[-1].content


@pytest.mark.anyio
async def test_a_public_dynamic_client_still_may_not_present_a_secret() -> None:
    server = _OAuthServer()
    server.token_endpoint_auth_methods_supported = ["client_secret_post", "none"]
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)
    parameters = OAuthAuthorizationParameters(
        client_id="dynamic-client",
        redirect_uri=profile.redirect_uri,
        scopes=("repo", "read:user"),
        state="opaque-state",
        code_challenge="challenge",
        resource_origin=profile.resource_origins[0],
    )

    with pytest.raises(OAuthProtocolError, match="^oauth_client_mode_mismatch$"):
        await protocol.exchange(
            discovery,
            parameters,
            code="opaque-code",
            code_verifier="verifier",
            client_secret="not-mine-to-send",
        )


@pytest.mark.anyio
async def test_a_refused_unauthenticated_exchange_reads_as_a_client_fault() -> None:
    """monday.com's shape, proven at the wire on 2026-09-07.

    Its metadata offers only `client_secret_post` and `client_secret_basic`, its
    dynamic registration answers `already_registered: true` with no
    `client_secret`, and it then refuses the exchange with `invalid_request`
    rather than the `invalid_client` RFC 6749 s5.2 reserves for it. The operator
    read `oauth_provider_rejected` -- "the provider said no" -- for what is
    actually "this client cannot authenticate to this server at all", and went
    looking for a bug in Omega three times.
    """
    server = _OAuthServer()
    server.token_endpoint_auth_methods_supported = [
        "client_secret_post",
        "client_secret_basic",
    ]
    server.token_status = 400
    server.token_document = {"error": "invalid_request"}
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)
    parameters = OAuthAuthorizationParameters(
        client_id="dynamic-client",
        redirect_uri=profile.redirect_uri,
        scopes=("repo", "read:user"),
        state="opaque-state",
        code_challenge="challenge",
        resource_origin=profile.resource_origins[0],
    )

    with pytest.raises(OAuthProviderError, match="^oauth_client_invalid$"):
        await protocol.exchange(
            discovery,
            parameters,
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )


@pytest.mark.anyio
async def test_an_authenticated_exchange_keeps_the_providers_own_refusal() -> None:
    """The promotion must not hide a malformed request Omega really did send.

    It needs BOTH that the server demands client authentication and that this
    request carried none. A request that authenticated and was still refused is
    the provider's answer, and stays reported as one.
    """
    server = _OAuthServer()
    server.token_endpoint_auth_methods_supported = ["client_secret_post"]
    server.token_status = 400
    server.token_document = {"error": "invalid_request"}
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)
    parameters = OAuthAuthorizationParameters(
        client_id="dynamic-client",
        redirect_uri=profile.redirect_uri,
        scopes=("repo", "read:user"),
        state="opaque-state",
        code_challenge="challenge",
        resource_origin=profile.resource_origins[0],
    )

    with pytest.raises(OAuthProviderError, match="^oauth_provider_rejected$"):
        await protocol.exchange(
            discovery,
            parameters,
            code="opaque-code",
            code_verifier="verifier",
            client_secret="issued-secret",
        )


@pytest.mark.anyio
async def test_a_public_server_refusal_is_still_the_providers_own() -> None:
    """Miro's shape must keep working, which is why this is not a pre-check.

    A server that admits `none` and refuses is refusing the request, not the
    client. The state before the request -- demands authentication, has no
    secret -- does NOT predict the answer: Miro is in exactly that state and its
    token endpoint answers 200. Only the answer decides, so only the answer is
    read.
    """
    server = _OAuthServer()
    server.token_endpoint_auth_methods_supported = ["client_secret_post", "none"]
    server.token_status = 400
    server.token_document = {"error": "invalid_request"}
    protocol = OAuthProtocolClient(transport=RecordingOAuthTransport(server))
    profile = _dynamic_profile()
    discovery = await protocol.discover(profile, resource_origin=_RESOURCE)
    parameters = OAuthAuthorizationParameters(
        client_id="dynamic-client",
        redirect_uri=profile.redirect_uri,
        scopes=("repo", "read:user"),
        state="opaque-state",
        code_challenge="challenge",
        resource_origin=profile.resource_origins[0],
    )

    with pytest.raises(OAuthProviderError, match="^oauth_provider_rejected$"):
        await protocol.exchange(
            discovery,
            parameters,
            code="opaque-code",
            code_verifier="verifier",
            client_secret=None,
        )
