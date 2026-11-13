from __future__ import annotations

import asyncio
import base64
import contextvars
import copy
import json
import time
import weakref
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

import mcp_platform.authorization as authorization_module
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.authorization import (
    McpAuthorizationConfig,
    McpBrokerAuthorizationConfig,
    McpAuthorizationPresenter,
    create_mcp_authorization_channel,
    is_mcp_authorization_config,
)
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client import CurrentMcpClient
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.transports.streamable_http import (
    _BrokerFailureClient,
    _BrokerFailureSession,
    _BrokerFailureState,
    StreamableHttpClientConfig,
    _canonical_endpoint,
    _provider_request_headers,
    build_streamable_http_transport,
)
from tests.mcp_platform.test_broker_authorization import (
    _authorization as _trusted_broker_authorization,
    _binding as _trusted_broker_binding,
    _broker as _trusted_broker,
)
from plugin_platform.contracts import AppRequirement
from plugin_platform.package_verifier import verify_plugin_package
from tests.plugin_platform.support import build_signed_plugin, ephemeral_publisher_ring


_SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}
_TOOL_DIGEST = sha256_hex(canonical_bytes(_TOOL_SCHEMA))


def _descriptor(
    *,
    # A real provider publishes its path -- every one of the twenty-one shipped
    # packages does. Omega no longer appends "/mcp" to a bare origin, so a
    # fixture that wants that endpoint has to say so, exactly like a manifest.
    origin: str = "https://apps.example.com/mcp",
    with_tool: bool = False,
    limits: McpClientLimits | None = None,
) -> McpConnectionDescriptor:
    capabilities = (
        [
            {
                "kind": "tool",
                "name": "calendar.lookup",
                "schema_sha256": _TOOL_DIGEST,
                "version": None,
            }
        ]
        if with_tool
        else []
    )
    requirement = AppRequirement.from_dict(
        {
            "app_id": "com.example.calendar",
            "publisher_id": "example.publisher",
            "deployment": "remote",
            "provider_id": None,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": ["streamable_http"],
            "remote_origin": origin,
            "authorization_issuer": "https://auth.example.com/issuer",
            "authentication": {
                "kind": "oauth_authorization_code_pkce",
                "scopes": ["example.read"],
                "secret_fields": [],
            },
            "capabilities": capabilities,
        }
    )
    projection = project_declared_capabilities(requirement)
    return McpConnectionDescriptor(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=McpClientDeployment.REMOTE,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin=requirement.remote_origin,
        authorization_issuer=requirement.authorization_issuer,
        authentication=requirement.authentication,
        declared_capabilities={
            f"{capability['kind']}:{capability['name']}": capability["schema_sha256"]
            for capability in capabilities
        },
        extension_versions={},
        capability_revision=projection.snapshot.capability_revision,
        limits=limits or McpClientLimits(),
    )


def test_resource_http_failure_preserves_safe_provider_cause_without_secrets(caplog):
    from mcp_platform.transports.streamable_http import _HttpResponseBoundary

    descriptor = _descriptor()
    boundary = _HttpResponseBoundary(
        endpoint="https://apps.example.com/mcp",
        descriptor=descriptor,
        authorization=None,
        max_bytes=4096,
        max_duration_seconds=1.0,
        max_redirects=0,
    )
    request = httpx2.Request(
        "POST", boundary.endpoint,
        headers={"Authorization": "Bearer secret-header"},
        json={"method": "tools/list", "params": {"private": "secret-request"}},
    )
    body = {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "message": "secret-provider-message",
            "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                "metadata": {"private": "secret-metadata"},
            }],
        },
    }
    response = httpx2.Response(403, json=body, request=request)

    with caplog.at_level("WARNING", logger="omega.mcp.http"):
        asyncio.run(boundary(response))

    assert "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in caplog.text
    assert "tools/list" in caplog.text
    assert "403" in caplog.text
    assert "secret-" not in caplog.text
    assert response.json() == body
    with pytest.raises(httpx2.HTTPStatusError):
        response.raise_for_status()


@pytest.mark.parametrize("body", [b"not-json secret-body", b"[]", b'{"error":{"details":[{"reason":"secret-reason"}]}}'])
def test_resource_http_diagnostic_rejects_unrecognized_text_and_malformed_method(body, caplog):
    from mcp_platform.transports.streamable_http import _HttpResponseBoundary

    boundary = _HttpResponseBoundary(
        endpoint="https://apps.example.com/mcp", descriptor=_descriptor(),
        authorization=None, max_bytes=4096, max_duration_seconds=1.0, max_redirects=0,
    )
    response = httpx2.Response(
        403, content=body,
        request=httpx2.Request("POST", boundary.endpoint, json={"method": ["secret-method"]}),
    )
    with caplog.at_level("WARNING", logger="omega.mcp.http"):
        asyncio.run(boundary(response))
    assert "status=403" in caplog.text
    assert "unclassified" in caplog.text
    assert "secret-" not in caplog.text
    assert response.content == body


def test_resource_http_diagnostic_keeps_response_size_bound():
    from mcp_platform.transports.streamable_http import _HttpResponseBoundary

    boundary = _HttpResponseBoundary(
        endpoint="https://apps.example.com/mcp", descriptor=_descriptor(),
        authorization=None, max_bytes=10, max_duration_seconds=1.0, max_redirects=0,
    )
    response = httpx2.Response(
        403, stream=_OneChunkStream(b"x" * 11),
        request=httpx2.Request("POST", boundary.endpoint),
    )
    with pytest.raises(httpx2.StreamError, match="response_limit_exceeded"):
        asyncio.run(boundary(response))


def _trusted_source(tmp_path: Path, descriptor: McpConnectionDescriptor) -> TrustedRemoteAppSource:
    requirement = AppRequirement.from_dict(
        {
            "app_id": descriptor.app_id,
            "publisher_id": descriptor.publisher_id,
            "deployment": descriptor.deployment.value,
            "provider_id": descriptor.provider_id,
            "protocol_version": descriptor.protocol_version,
            "transports": [descriptor.transport.value],
            "remote_origin": descriptor.remote_origin,
            "authorization_issuer": descriptor.authorization_issuer,
            "authentication": descriptor.authentication.to_dict(),
            "capabilities": [],
        }
    )
    private, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private)
    document = json.loads((package / "plugin.json").read_bytes())
    document["kind"] = "mcp_backed"
    document["required_apps"] = [requirement.to_dict()]
    document["entry_points"][0]["required_app_ids"] = [requirement.app_id]
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(private.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    (package / "plugin.json").write_bytes(canonical_bytes(document))
    signed = verify_plugin_package(package, ring, "3.0.0").manifest.required_apps[0]
    return TrustedRemoteAppSource(requirement=signed)


class _OAuthPresenter:
    def __init__(self) -> None:
        self.authorization_url: str | None = None

    async def present_authorization(self, authorization_url: str) -> None:
        self.authorization_url = authorization_url

    async def wait_for_callback(self) -> str:
        assert self.authorization_url is not None
        state = parse_qs(urlsplit(self.authorization_url).query)["state"][0]
        return (
            "https://omega2ai.com/oauth/callback?code=stream-code"
            f"&state={state}&iss=https://auth.example.com/issuer"
        )


_OAUTH_DRIVERS: weakref.WeakKeyDictionary[
    McpAuthorizationConfig,
    tuple[McpAuthorizationPresenter, _OAuthPresenter],
] = weakref.WeakKeyDictionary()


def _oauth_authorization_config(
    tmp_path: Path,
    descriptor: McpConnectionDescriptor,
    *,
    presenter: _OAuthPresenter | None = None,
    token_store: _OAuthStore | None = None,
) -> McpAuthorizationConfig:
    channel, port = create_mcp_authorization_channel()
    authorization = McpAuthorizationConfig.for_client_metadata_document(
        descriptor,
        _trusted_source(tmp_path, descriptor),
        authorization_context_revision="a" * 64,
        redirect_uri="https://omega2ai.com/oauth/callback",
        scopes=("calendar.read",),
        presenter=channel,
        client_metadata_url="https://omega2ai.com/oauth/client-metadata.json",
        token_store=token_store or _OAuthStore(),
    )
    _OAUTH_DRIVERS[authorization] = (port, presenter or _OAuthPresenter())
    return authorization


def _broker_authorization_config(
    descriptor: McpConnectionDescriptor,
) -> McpBrokerAuthorizationConfig:
    binding = _trusted_broker_binding(
        app_id=descriptor.app_id,
        resource_url="https://apps.example.com/mcp",
        capability_revision=descriptor.capability_revision,
    )
    broker, _calls = _trusted_broker(binding)
    return _trusted_broker_authorization(binding, broker)


async def _drive_oauth_authorization(authorization: McpAuthorizationConfig) -> None:
    port, presenter = _OAUTH_DRIVERS[authorization]
    request = await port.receive_authorization_request()
    await presenter.present_authorization(request.authorization_url)
    callback_uri = await presenter.wait_for_callback()
    await port.submit_authorization_callback(request.request_id, callback_uri)


async def _start_with_oauth(
    client: CurrentMcpClient,
    authorization: McpAuthorizationConfig,
) -> object:
    driver = asyncio.create_task(_drive_oauth_authorization(authorization))
    start = asyncio.create_task(client.start())
    try:
        done, _pending = await asyncio.wait(
            {driver, start},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if driver in done:
            await driver
        return await start
    finally:
        for task in (driver, start):
            if not task.done():
                task.cancel()
            with suppress(BaseException):
                await task


class _OAuthStore:
    def __init__(self) -> None:
        self.tokens: dict[str, tuple[int, object]] = {}
        self.clients: dict[str, object] = {}
        self.codes: set[tuple[str, str]] = set()
        self._token_lock = asyncio.Lock()

    async def load_tokens(self, key: str) -> tuple[int, object] | None:
        return self.tokens.get(key)

    async def store_tokens(
        self,
        key: str,
        value: object,
        *,
        expected_revision: int | None,
    ) -> int | None:
        async with self._token_lock:
            current = self.tokens.get(key)
            current_revision = None if current is None else current[0]
            if current_revision != expected_revision:
                return None
            revision = 0 if current_revision is None else current_revision + 1
            self.tokens[key] = (revision, value)
            return revision

    async def delete_tokens(self, key: str, *, expected_revision: int) -> bool:
        async with self._token_lock:
            current = self.tokens.get(key)
            if current is None or current[0] != expected_revision:
                return False
            del self.tokens[key]
            return True

    async def load_client_info(self, key: str) -> object | None:
        return self.clients.get(key)

    async def store_client_info(self, key: str, value: object) -> None:
        self.clients[key] = value

    async def claim_authorization_code(self, key: str, digest: str) -> bool:
        identity = (key, digest)
        if identity in self.codes:
            return False
        self.codes.add(identity)
        return True


def _forge_descriptor(
    descriptor: McpConnectionDescriptor,
    **changes: object,
) -> McpConnectionDescriptor:
    forged = object.__new__(McpConnectionDescriptor)
    for field in fields(descriptor):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, getattr(descriptor, field.name)),
        )
    return forged


def _loopback_descriptor(
    *,
    with_tool: bool = False,
    limits: McpClientLimits | None = None,
) -> McpConnectionDescriptor:
    return _descriptor(with_tool=with_tool, limits=limits)


def _metadata(
    descriptor: McpConnectionDescriptor,
    *,
    server_id: str = "example.calendar",
    server_version: str = "2.4.0",
) -> dict[str, object]:
    return {
        "appId": descriptor.app_id,
        "publisherId": descriptor.publisher_id,
        "deployment": descriptor.deployment.value,
        "providerId": descriptor.provider_id,
        "protocolVersion": descriptor.protocol_version,
        "transport": descriptor.transport.value,
        "remoteOrigin": descriptor.remote_origin,
        "authorizationIssuer": descriptor.authorization_issuer,
        _SERVER_INFO_META_KEY: {
            "name": server_id,
            "version": server_version,
        },
    }


def _response(
    request: httpx2.Request,
    result: Mapping[str, object],
    *,
    status_code: int = 200,
) -> httpx2.Response:
    body = json.loads(request.content)
    return httpx2.Response(
        status_code,
        headers={"content-type": "application/json"},
        json={"jsonrpc": "2.0", "id": body["id"], "result": dict(result)},
    )


def _discover_result(
    descriptor: McpConnectionDescriptor,
    *,
    tools: bool = False,
) -> dict[str, object]:
    return {
        "supportedVersions": [MCP_PROTOCOL_VERSION],
        "capabilities": {"tools": {}} if tools else {},
        "ttlMs": 0,
        "cacheScope": "private",
        "resultType": "complete",
        "_meta": _metadata(descriptor),
    }


class _BearerAuth(httpx2.Auth):
    def __init__(self, value: str) -> None:
        self.value = value

    def auth_flow(self, request: httpx2.Request) -> Any:
        request.headers["authorization"] = self.value
        yield request


class _NoOpAuth(httpx2.Auth):
    def auth_flow(self, request: httpx2.Request) -> Any:
        yield request


class _BlockingStream(httpx2.AsyncByteStream):
    def __init__(self, first_chunk: bytes | None = None) -> None:
        self.first_chunk = first_chunk
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        if self.first_chunk is not None:
            yield self.first_chunk
        await self.release.wait()

    async def aclose(self) -> None:
        self.close_calls += 1


class _OneChunkStream(httpx2.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.content

    async def aclose(self) -> None:
        return


class _DelayedOversizedStream(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.started = False
        self.close_calls = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started = True
        await asyncio.sleep(0.25)
        yield b"x" * 2_048

    async def aclose(self) -> None:
        self.close_calls += 1


class _StreamBaseException(BaseException):
    pass


class _FailingIteratorAndCloseStream(httpx2.AsyncByteStream):
    def __init__(self, active_error: BaseException) -> None:
        self.active_error = active_error
        self.close_calls = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"entered"
        raise self.active_error

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("cleanup-sentinel")
        self.closed = True


class _FailingIteratorAndResistantCloseStream(httpx2.AsyncByteStream):
    def __init__(self, active_error: BaseException) -> None:
        self.active_error = active_error
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"entered"
        raise self.active_error

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        while not self.release_close.is_set():
            try:
                await self.release_close.wait()
            except asyncio.CancelledError:
                continue
        self.closed = True


class _GatedCloseStream(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if False:
            yield b""

    async def aclose(self) -> None:
        self.close_calls += 1
        self.entered.set()
        await self.release.wait()
        self.closed = True


class _FailThenGatedRetryStream(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.first_entered = asyncio.Event()
        self.first_release = asyncio.Event()
        self.retry_entered = asyncio.Event()
        self.retry_release = asyncio.Event()
        self.close_calls = 0
        self.active_closes = 0
        self.max_active_closes = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if False:
            yield b""

    async def aclose(self) -> None:
        self.close_calls += 1
        call = self.close_calls
        self.active_closes += 1
        self.max_active_closes = max(self.max_active_closes, self.active_closes)
        try:
            if call == 1:
                self.first_entered.set()
                await self.first_release.wait()
                raise RuntimeError("first-close-failed")
            self.retry_entered.set()
            await self.retry_release.wait()
            self.closed = True
        finally:
            self.active_closes -= 1


def _https_test_config(
    transport: httpx2.AsyncBaseTransport,
    *,
    auth: httpx2.Auth | None = None,
) -> StreamableHttpClientConfig:
    return StreamableHttpClientConfig._for_https_test(transport, auth=auth)


def _loopback_test_config(
    transport: httpx2.AsyncBaseTransport,
    *,
    auth: httpx2.Auth | None = None,
) -> StreamableHttpClientConfig:
    return StreamableHttpClientConfig._for_loopback_test(transport, auth=auth)


def _direct_http_sdk_client(
    descriptor: McpConnectionDescriptor,
    config: StreamableHttpClientConfig,
) -> Any:
    factory = build_streamable_http_transport(descriptor, config)
    return factory._builder(
        mode=MCP_PROTOCOL_VERSION,
        client_info=types.Implementation(name="omega-mcp-host", version="3.0.0"),
        extensions=(),
        cache=None,
    )


def _exception_messages(error: BaseException) -> tuple[str, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(
            message for nested in error.exceptions for message in _exception_messages(nested)
        )
    return (str(error),)


def _contains_client_reason(
    error: BaseException,
    reason: McpClientErrorReason,
) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_client_reason(nested, reason) for nested in error.exceptions)
    return isinstance(error, McpClientError) and error.reason is reason


def test_streamable_http_factory_boundary_exists() -> None:
    assert callable(build_streamable_http_transport)


@pytest.mark.parametrize(
    "origin",
    [
        "http://apps.example.com",
        "http://localhost",
        "https://user@apps.example.com",
        "https://apps.example.com#fragment",
        # A PATH is not an alias and no longer belongs here. Providers publish
        # their endpoint at one: Slack's is https://mcp.slack.com/mcp, and
        # refusing it made a signed, authorized plugin fail at connect
        # (2026-09-07). The acceptance side is pinned in
        # tests/mcp_platform/test_provider_interop.py, which also checks the
        # transport and the worker agree on what a stated path means.
        "https://apps.example.com:0",
        "https://apps.example.com:443",
        "https://Apps.example.com",
        "https://%61pps.example.com",
        "https://app\N{LATIN SMALL LETTER S WITH CIRCUMFLEX}.example.com",
        "https://apps.example.com\r",
    ],
)
def test_origin_validation_rejects_aliases_before_http_client_construction(origin: str) -> None:
    descriptor = _forge_descriptor(_descriptor(), remote_origin=origin)
    config = StreamableHttpClientConfig.unauthenticated()

    with pytest.raises(McpClientError) as raised:
        build_streamable_http_transport(descriptor, config)

    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH


def test_https_test_transport_reports_live_authenticated() -> None:
    async def app(scope: Mapping[str, object], receive: object, send: object) -> None:
        del scope, receive, send

    factory = build_streamable_http_transport(
        _descriptor(),
        StreamableHttpClientConfig._for_https_test(httpx2.ASGITransport(app=app)),
    )
    assert factory.discovery_source is DiscoverySource.LIVE_AUTHENTICATED
    public_auth = build_streamable_http_transport(
        _descriptor(),
        StreamableHttpClientConfig._for_https_test(
            httpx2.ASGITransport(app=app),
            auth=_NoOpAuth(),
        ),
    )
    assert public_auth.discovery_source is DiscoverySource.LIVE_OBSERVED
    unauthenticated = build_streamable_http_transport(
        _descriptor(),
        StreamableHttpClientConfig.unauthenticated(),
    )
    assert unauthenticated.discovery_source is DiscoverySource.LIVE_OBSERVED


def test_https_origin_builds_only_the_fixed_canonical_mcp_path() -> None:
    factory = build_streamable_http_transport(
        _descriptor(),
        StreamableHttpClientConfig.unauthenticated(),
    )

    assert factory.deployment is McpClientDeployment.REMOTE
    assert factory.transport is McpTransport.STREAMABLE_HTTP
    assert "/mcp" not in repr(factory)


def test_signed_nondefault_https_port_is_preserved_in_exact_endpoint() -> None:
    factory = build_streamable_http_transport(
        _descriptor(origin="https://apps.example.com:8443/mcp"),
        StreamableHttpClientConfig.unauthenticated(),
    )

    assert factory._builder.endpoint == "https://apps.example.com:8443/mcp"


def test_a_bare_https_origin_is_the_endpoint() -> None:
    """Omega does not invent a path a provider did not publish.

    "/mcp" used to be appended here. Miro and Stripe serve their MCP endpoint at
    their root and are the only two shipped providers with a bare origin, so the
    guess was wrong every time it applied -- both answered the connect POST with
    404 after a successful sign-in and token exchange.
    """
    for origin in (
        "https://mcp.miro.com",
        "https://mcp.stripe.com",
        "https://drivemcp.googleapis.com",
    ):
        descriptor = _descriptor(origin=origin)

        assert _canonical_endpoint(descriptor, loopback_test=False) == origin


def test_signed_versioned_endpoint_is_not_appended_again() -> None:
    descriptor = _descriptor(origin="https://drivemcp.googleapis.com/mcp/v1")

    assert (
        _canonical_endpoint(descriptor, loopback_test=False)
        == "https://drivemcp.googleapis.com/mcp/v1"
    )


def test_github_copilot_origin_with_mcp_slash_is_not_rejected() -> None:
    descriptor = _descriptor(origin="https://api.githubcopilot.com/mcp/")

    assert (
        _canonical_endpoint(descriptor, loopback_test=False) == "https://api.githubcopilot.com/mcp/"
    )


def test_authorization_revision_is_sealed_into_factory_client_and_cache_partition(
    tmp_path: Path,
) -> None:
    import mcp_platform.client as client_module

    descriptor = _descriptor()
    authorization = _oauth_authorization_config(tmp_path, descriptor)
    factory = build_streamable_http_transport(
        descriptor,
        _https_test_config(
            httpx2.MockTransport(lambda _request: httpx2.Response(500)),
            auth=authorization,
        ),
    )
    record = client_module._ISSUED_SDK_CLIENT_FACTORIES[factory]
    client = CurrentMcpClient(descriptor, factory)

    assert record.authorization_context_revision == "a" * 64
    assert client.authorization_context_revision == record.authorization_context_revision
    with pytest.raises(TypeError):
        CurrentMcpClient(
            descriptor,
            factory,
            authorization_context_revision="b" * 64,
        )


def test_response_close_has_an_absolute_deadline_when_cleanup_resists_cancellation() -> None:
    from mcp_platform.transports.streamable_http import _BoundedResponseStream

    class _CancellationResistantClose(httpx2.AsyncByteStream):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            if False:
                yield b""

        async def aclose(self) -> None:
            self.calls += 1
            self.entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue

    async def exercise() -> None:
        raw = _CancellationResistantClose()
        request = httpx2.Request("POST", "https://apps.example.com:8443/mcp")
        bounded = _BoundedResponseStream(
            raw,
            request=request,
            limit=32,
            duration_seconds=1,
            shutdown_seconds=0.02,
        )
        close_task = asyncio.create_task(bounded.aclose())
        try:
            started = asyncio.get_running_loop().time()
            done, _pending = await asyncio.wait({close_task}, timeout=0.25)
            assert close_task in done, "response close exceeded its absolute deadline"
            with pytest.raises(McpClientError) as raised:
                await close_task
            assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert asyncio.get_running_loop().time() - started < 0.25
            assert raw.calls == 1
            owner = bounded._close_task
            assert isinstance(owner, asyncio.Task)
            assert not owner.done()

            with pytest.raises(McpClientError) as retried:
                await bounded.aclose()
            assert retried.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert bounded._close_task is owner
            assert raw.calls == 1

            raw.release.set()
            await owner
            await bounded.aclose()
            assert raw.calls == 1
        finally:
            raw.release.set()
            if not close_task.done():
                await close_task
            await asyncio.sleep(0.02)

    asyncio.run(exercise())


def test_response_close_preserves_cancellation_identity_and_recovery_owner() -> None:
    from mcp_platform.transports.streamable_http import _BoundedResponseStream

    class _CancellationResistantClose(httpx2.AsyncByteStream):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            if False:
                yield b""

        async def aclose(self) -> None:
            self.calls += 1
            self.entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue

    async def exercise() -> None:
        raw = _CancellationResistantClose()
        bounded = _BoundedResponseStream(
            raw,
            request=httpx2.Request("POST", "https://apps.example.com:8443/mcp"),
            limit=32,
            duration_seconds=1,
            shutdown_seconds=0.02,
        )
        propagated: list[asyncio.CancelledError] = []

        async def close_and_capture() -> None:
            try:
                await bounded.aclose()
            except asyncio.CancelledError as error:
                propagated.append(error)
                raise

        waiter = asyncio.create_task(close_and_capture())
        try:
            await raw.entered.wait()
            owner = bounded._close_task
            assert isinstance(owner, asyncio.Task)
            waiter.cancel("http-close-cancel-sentinel")
            with pytest.raises(asyncio.CancelledError) as raised:
                await waiter
            assert len(propagated) == 1
            assert raised.value is propagated[0]
            assert raised.value.args == ("http-close-cancel-sentinel",)
            assert bounded._close_task is owner
            assert not owner.done()

            with pytest.raises(McpClientError) as retried:
                await bounded.aclose()
            assert retried.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert bounded._close_task is owner
            assert raw.calls == 1

            raw.release.set()
            await owner
            await bounded.aclose()
        finally:
            raw.release.set()
            owner = bounded._close_task
            if owner is not None:
                await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("mismatch", ["app_id", "origin", "revision"])
def test_http_factory_is_bound_to_the_exact_descriptor_before_any_request(
    mismatch: str,
) -> None:
    issued_descriptor = _descriptor()
    if mismatch == "app_id":
        presented_descriptor = _forge_descriptor(
            issued_descriptor,
            app_id="com.example.unrelated",
        )
    elif mismatch == "origin":
        presented_descriptor = _descriptor(origin="https://other.example.com")
    else:
        presented_descriptor = _forge_descriptor(
            issued_descriptor,
            capability_revision="f" * 64,
        )
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(request, _discover_result(presented_descriptor))

    config = _https_test_config(httpx2.MockTransport(handler), auth=_NoOpAuth())
    factory = build_streamable_http_transport(issued_descriptor, config)
    client = CurrentMcpClient(presented_descriptor, factory)

    async def exercise() -> None:
        try:
            with pytest.raises(McpClientError) as raised:
                await client.start()
            assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
        finally:
            await client.close()

    asyncio.run(exercise())
    assert requests == []


def test_equal_descriptor_copy_keeps_binding_but_public_auth_remains_observed() -> None:
    issued_descriptor = _descriptor()
    equal_descriptor = _descriptor()
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(request, _discover_result(equal_descriptor))

    config = _https_test_config(httpx2.MockTransport(handler), auth=_NoOpAuth())
    client = CurrentMcpClient(
        equal_descriptor,
        build_streamable_http_transport(issued_descriptor, config),
    )

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
        await client.close()

    asyncio.run(exercise())
    assert requests == []


def test_descriptor_mutation_after_factory_issuance_is_rejected_before_request() -> None:
    descriptor = _descriptor()
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(request, _discover_result(descriptor))

    config = _https_test_config(httpx2.MockTransport(handler), auth=_NoOpAuth())
    factory = build_streamable_http_transport(descriptor, config)
    object.__setattr__(descriptor, "remote_origin", "https://mutated.example.com")
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
        await client.close()

    asyncio.run(exercise())
    assert requests == []


def test_authenticated_boundary_accepts_only_the_two_omega_sealed_auth_types(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    legacy = _oauth_authorization_config(tmp_path, descriptor)
    brokered = _broker_authorization_config(descriptor)

    assert is_mcp_authorization_config(legacy) is True
    assert is_mcp_authorization_config(brokered) is True
    assert is_mcp_authorization_config(_NoOpAuth()) is False
    assert isinstance(StreamableHttpClientConfig.authenticated(legacy), StreamableHttpClientConfig)
    assert isinstance(
        StreamableHttpClientConfig.authenticated(brokered),
        StreamableHttpClientConfig,
    )
    with pytest.raises(McpClientError) as raised:
        StreamableHttpClientConfig.authenticated(_NoOpAuth())  # type: ignore[arg-type]
    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID


class _BrokerOperationProbe:
    def __init__(
        self,
        descriptor: McpConnectionDescriptor,
        authorization: McpBrokerAuthorizationConfig,
        state: _BrokerFailureState,
    ) -> None:
        self.descriptor = descriptor
        self.authorization = authorization
        self.state = state
        self.requests: list[httpx2.Request] = []
        self.last_marker: object | None = None
        self._request_id = 0

    def _sdk_error_item(self) -> SessionMessage:
        return SessionMessage(
            types.JSONRPCError(
                jsonrpc="2.0",
                id=self._request_id,
                error=types.ErrorData(
                    code=-32603,
                    message="Server returned an error response",
                ),
            )
        )

    async def _complete_second_401(
        self,
    ) -> tuple[object, contextvars.Context, httpx2.Response]:
        self._request_id += 1
        request = httpx2.Request(
            "POST",
            "https://apps.example.com/mcp",
            content=(f'{{"jsonrpc":"2.0","id":{self._request_id},"method":"tools/call"}}').encode(),
        )
        flow = self.authorization.async_auth_flow(request)
        first = await anext(flow)
        self.requests.append(first)
        await authorization_module._authorization_request_binding(
            self.authorization,
            self.descriptor,
            first,
        )
        first_response = httpx2.Response(401, request=first)
        authorization_module._authorization_response_binding(
            self.authorization,
            self.descriptor,
            first,
        )
        retry = await flow.asend(first_response)
        self.requests.append(retry)
        await authorization_module._authorization_request_binding(
            self.authorization,
            self.descriptor,
            retry,
        )
        retry_response = httpx2.Response(401, request=retry)
        proof = authorization_module._mark_broker_transport_response(
            self.authorization,
            self.descriptor,
            retry_response,
        )
        assert proof is not None
        self.state.bind_response(proof)
        sender_context = contextvars.copy_context()
        authorization_module._authorization_response_binding(
            self.authorization,
            self.descriptor,
            retry,
        )
        try:
            await flow.asend(retry_response)
        except StopAsyncIteration:
            pass
        return proof, sender_context, retry_response

    async def _correlated_second_401_marker(self) -> object:
        _proof, sender_context, _response = await self._complete_second_401()
        correlated = self.state.correlate(self._sdk_error_item(), sender_context)
        assert type(correlated) is SessionMessage
        assert type(correlated.message) is types.JSONRPCError
        marker = correlated.message.error.data
        assert marker is not None
        self.last_marker = marker
        return marker

    async def rejected(self) -> None:
        marker = await self._correlated_second_401_marker()
        raise MCPError(
            code=-32603,
            message="Server returned an error response",
            data=marker,
        )

    async def rejected_then_unrelated_error(self) -> None:
        await self._complete_second_401()
        raise RuntimeError("later-operation-error")

    async def rejected_then_same_shaped_sdk_error(self) -> None:
        await self._complete_second_401()
        raise MCPError(
            code=-32603,
            message="Server returned an error response",
        )

    async def marked_succeed(self) -> str:
        await self._complete_second_401()
        return "marked-operation-succeeded"

    async def marked_wait_for_cancellation(self, started: asyncio.Event) -> None:
        await self._complete_second_401()
        started.set()
        await asyncio.Event().wait()

    async def wrong_response_with_same_id(self) -> tuple[object, object, object]:
        _proof, sender_context, _response = await self._complete_second_401()
        item = self._sdk_error_item()
        wrong = self.state.correlate(item, contextvars.Context())
        correlated = self.state.correlate(item, sender_context)
        replayed = self.state.correlate(item, sender_context)
        return wrong, correlated, replayed

    async def replay_last_marker(self) -> None:
        assert self.last_marker is not None
        raise MCPError(
            code=-32603,
            message="Server returned an error response",
            data=self.last_marker,
        )

    async def wait_for_cancellation(self, started: asyncio.Event) -> None:
        started.set()
        await asyncio.Event().wait()

    async def succeed(self) -> str:
        return "operation-succeeded"

    async def fail_without_authorization_error(self) -> None:
        raise RuntimeError("operation-failure-sentinel")


class _CleanupFailureClient:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.elicitation_callback: object | None = None

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        raise self.failure


def _broker_operation_probe() -> tuple[
    _BrokerFailureSession,
    _BrokerOperationProbe,
    object,
    _BrokerFailureState,
]:
    descriptor = _descriptor(with_tool=True)
    binding = _trusted_broker_binding(
        app_id=descriptor.app_id,
        resource_url="https://apps.example.com/mcp",
        capability_revision=descriptor.capability_revision,
    )
    broker, calls = _trusted_broker(binding)
    authorization = _trusted_broker_authorization(binding, broker)
    assert (
        authorization_module._authorization_descriptor_binding(
            authorization,
            descriptor,
            endpoint="https://apps.example.com/mcp",
        )
        is authorization
    )
    state = _BrokerFailureState(authorization)
    probe = _BrokerOperationProbe(descriptor, authorization, state)
    return _BrokerFailureSession(probe, authorization, state), probe, calls, state


def test_broker_auth_failure_does_not_replace_later_operation_cancellation() -> None:
    wrapped, probe, calls, _state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as rejected:
            await wrapped.rejected()
        assert rejected.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED

        started = asyncio.Event()
        operation = asyncio.create_task(wrapped.wait_for_cancellation(started))
        await started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(exercise())
    assert len(probe.requests) == 2
    assert len(calls.refresh) == 1
    assert all(request.headers.get("authorization") is None for request in probe.requests)
    assert all(
        "authorization" not in extension
        for request in probe.requests
        for extension in request.extensions
    )


def test_broker_auth_failure_does_not_replace_later_cleanup_cancellation() -> None:
    wrapped, probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as rejected:
            await wrapped.rejected()
        cancellation = asyncio.CancelledError("cleanup-cancellation-sentinel")
        client = _BrokerFailureClient(
            _CleanupFailureClient(cancellation),  # type: ignore[arg-type]
            probe.authorization,
            state,
        )

        with pytest.raises(asyncio.CancelledError) as raised:
            await client.__aexit__(
                McpClientError,
                rejected.value,
                rejected.value.__traceback__,
            )
        assert raised.value is cancellation

    asyncio.run(exercise())


def test_marked_second_401_does_not_replace_later_operation_error() -> None:
    wrapped, probe, calls, _state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="^later-operation-error$"):
            await wrapped.rejected_then_unrelated_error()

    asyncio.run(exercise())
    assert len(probe.requests) == 2
    assert len(calls.refresh) == 1
    assert all(request.headers.get("authorization") is None for request in probe.requests)


def test_marked_second_401_does_not_replace_later_same_shaped_sdk_error() -> None:
    wrapped, probe, calls, _state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(MCPError) as raised:
            await wrapped.rejected_then_same_shaped_sdk_error()
        assert type(raised.value) is MCPError
        assert raised.value.code == -32603
        assert raised.value.message == "Server returned an error response"
        assert raised.value.data is None

    asyncio.run(exercise())
    assert len(probe.requests) == 2
    assert len(calls.refresh) == 1
    assert all(request.headers.get("authorization") is None for request in probe.requests)


def test_consumed_second_401_marker_cannot_be_replayed_in_a_later_operation() -> None:
    wrapped, probe, _calls, _state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as rejected:
            await wrapped.rejected()
        assert rejected.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED

        with pytest.raises(MCPError) as replayed:
            await wrapped.replay_last_marker()
        assert replayed.value.data is probe.last_marker

    asyncio.run(exercise())


def test_broker_response_marker_is_not_attached_to_wrong_response_with_same_id() -> None:
    wrapped, _probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        wrong, correlated, replayed = await wrapped.wrong_response_with_same_id()
        assert type(wrong) is SessionMessage
        assert type(wrong.message) is types.JSONRPCError
        assert wrong.message.error.data is None
        assert type(correlated) is SessionMessage
        assert type(correlated.message) is types.JSONRPCError
        assert correlated.message.error.data is not None
        assert replayed is wrong
        assert len(state._response_markers) == 0

    asyncio.run(exercise())


def test_broker_response_marker_is_cleared_after_marked_success() -> None:
    wrapped, _probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        assert await wrapped.marked_succeed() == "marked-operation-succeeded"
        assert len(state._response_markers) == 0

    asyncio.run(exercise())


def test_broker_response_marker_is_cleared_after_cancellation() -> None:
    wrapped, _probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        started = asyncio.Event()
        operation = asyncio.create_task(wrapped.marked_wait_for_cancellation(started))
        await started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert len(state._response_markers) == 0

    asyncio.run(exercise())


def test_broker_response_marker_is_cleared_after_unrelated_error() -> None:
    wrapped, _probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="^later-operation-error$"):
            await wrapped.rejected_then_unrelated_error()
        assert len(state._response_markers) == 0

    asyncio.run(exercise())


def test_broker_response_marker_is_cleared_after_translated_failure() -> None:
    wrapped, _probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await wrapped.rejected()
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
        assert len(state._response_markers) == 0

    asyncio.run(exercise())


def test_broker_response_markers_are_cleared_per_concurrent_operation() -> None:
    wrapped, _probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        started = asyncio.Event()
        waiting = asyncio.create_task(wrapped.marked_wait_for_cancellation(started))
        await started.wait()
        assert len(state._response_markers) == 1

        assert await wrapped.marked_succeed() == "marked-operation-succeeded"
        assert len(state._response_markers) == 1

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert len(state._response_markers) == 0

    asyncio.run(exercise())


def test_marked_second_401_does_not_replace_later_cleanup_error() -> None:
    wrapped, probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as rejected:
            await wrapped.rejected()
        cleanup = RuntimeError("cleanup-error-sentinel")
        client = _BrokerFailureClient(
            _CleanupFailureClient(cleanup),  # type: ignore[arg-type]
            probe.authorization,
            state,
        )

        with pytest.raises(RuntimeError) as raised:
            await client.__aexit__(
                McpClientError,
                rejected.value,
                rejected.value.__traceback__,
            )
        assert raised.value is cleanup

    asyncio.run(exercise())


def test_marked_second_401_preserves_mixed_cleanup_base_exception_group() -> None:
    wrapped, probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as rejected:
            await wrapped.rejected()
        cancellation = asyncio.CancelledError("cleanup-cancellation-sentinel")
        runtime = RuntimeError("unrelated-operation-error")
        validation = ValueError("cleanup-validation-error")
        cleanup = BaseExceptionGroup(
            "mixed-cleanup-errors",
            [cancellation, rejected.value, runtime, validation],
        )
        client = _BrokerFailureClient(
            _CleanupFailureClient(cleanup),  # type: ignore[arg-type]
            probe.authorization,
            state,
        )

        with pytest.raises(BaseExceptionGroup) as raised:
            await client.__aexit__(
                McpClientError,
                rejected.value,
                rejected.value.__traceback__,
            )
        assert raised.value is cleanup
        assert raised.value.exceptions == (
            cancellation,
            rejected.value,
            runtime,
            validation,
        )

    asyncio.run(exercise())


def test_marked_second_401_maps_auth_only_cleanup_group() -> None:
    wrapped, probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as rejected:
            await wrapped.rejected()
        cleanup = ExceptionGroup("auth-only-cleanup", [rejected.value])
        client = _BrokerFailureClient(
            _CleanupFailureClient(cleanup),  # type: ignore[arg-type]
            probe.authorization,
            state,
        )

        with pytest.raises(McpClientError) as raised:
            await client.__aexit__(
                McpClientError,
                rejected.value,
                rejected.value.__traceback__,
            )
        assert raised.value is rejected.value

    asyncio.run(exercise())


def test_marked_second_401_preserves_unrelated_cleanup_exception_group() -> None:
    wrapped, probe, _calls, state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as rejected:
            await wrapped.rejected()
        cleanup = ExceptionGroup(
            "cleanup-errors",
            [
                rejected.value,
                RuntimeError("unrelated-operation-error"),
                ValueError("cleanup-validation-error"),
            ],
        )
        client = _BrokerFailureClient(
            _CleanupFailureClient(cleanup),  # type: ignore[arg-type]
            probe.authorization,
            state,
        )

        with pytest.raises(ExceptionGroup) as raised:
            await client.__aexit__(
                McpClientError,
                rejected.value,
                rejected.value.__traceback__,
            )
        assert raised.value is cleanup
        assert raised.value.exceptions[0] is rejected.value
        assert [str(error) for error in raised.value.exceptions[1:]] == [
            "unrelated-operation-error",
            "cleanup-validation-error",
        ]

    asyncio.run(exercise())


def test_broker_auth_failure_is_isolated_from_concurrent_operation_cancellation() -> None:
    wrapped, _probe, _calls, _state = _broker_operation_probe()

    async def exercise() -> None:
        started = asyncio.Event()
        waiting = asyncio.create_task(wrapped.wait_for_cancellation(started))
        await started.wait()
        rejected = asyncio.create_task(wrapped.rejected())
        with pytest.raises(McpClientError) as raised:
            await rejected
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    asyncio.run(exercise())


def test_broker_auth_failure_is_not_sticky_after_success() -> None:
    wrapped, _probe, _calls, _state = _broker_operation_probe()

    async def exercise() -> None:
        with pytest.raises(McpClientError) as rejected:
            await wrapped.rejected()
        assert rejected.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
        assert await wrapped.succeed() == "operation-succeeded"

        with pytest.raises(RuntimeError, match="^operation-failure-sentinel$"):
            await wrapped.fail_without_authorization_error()

    asyncio.run(exercise())


def test_broker_auth_uses_one_refresh_retry_through_the_current_transport() -> None:
    descriptor = _descriptor()
    observed_authorization: list[str | None] = []
    binding = _trusted_broker_binding(
        app_id=descriptor.app_id,
        resource_url="https://apps.example.com/mcp",
        capability_revision=descriptor.capability_revision,
    )
    broker, calls = _trusted_broker(binding)
    authorization = _trusted_broker_authorization(binding, broker)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        observed_authorization.append(request.headers.get("authorization"))
        if len(observed_authorization) == 1:
            return httpx2.Response(401)
        assert len(observed_authorization) == 2
        return _response(request, _discover_result(descriptor))

    factory = build_streamable_http_transport(
        descriptor,
        _https_test_config(httpx2.MockTransport(handler), auth=authorization),
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        receipt = await client.start()
        assert receipt.source is DiscoverySource.LIVE_AUTHENTICATED
        await client.close()

    asyncio.run(exercise())
    assert observed_authorization == [
        "Bearer opaque-access",
        "Bearer opaque-refreshed-access",
    ]
    assert calls.current == [binding] * 5
    assert len(calls.refresh) == 1


def test_broker_auth_second_401_maps_to_authorization_required_without_another_retry() -> None:
    descriptor = _descriptor()
    binding = _trusted_broker_binding(
        app_id=descriptor.app_id,
        resource_url="https://apps.example.com/mcp",
        capability_revision=descriptor.capability_revision,
    )
    broker, calls = _trusted_broker(binding)
    authorization = _trusted_broker_authorization(binding, broker)
    requests: list[httpx2.Request] = []
    errors: list[McpClientError] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(401)

    client = CurrentMcpClient(
        descriptor,
        build_streamable_http_transport(
            descriptor,
            _https_test_config(httpx2.MockTransport(handler), auth=authorization),
        ),
    )

    async def exercise() -> None:
        try:
            with pytest.raises(McpClientError) as raised:
                await client.start()
            errors.append(raised.value)
        finally:
            await client.close()

    asyncio.run(exercise())
    assert len(requests) == 2
    assert len(calls.refresh) == 1
    assert [request.headers.get("authorization") for request in requests] == [None, None]
    assert all("authorization" not in name for request in requests for name in request.extensions)
    assert [error.reason for error in errors] == [McpClientErrorReason.AUTHORIZATION_REQUIRED]
    assert "opaque-access" not in repr(errors[0])
    assert "opaque-refreshed-access" not in repr(errors[0])


def test_broker_auth_second_401_maps_through_sdk_convenience_methods() -> None:
    descriptor = _descriptor(with_tool=True)
    binding = _trusted_broker_binding(
        app_id=descriptor.app_id,
        resource_url="https://apps.example.com/mcp",
        capability_revision=descriptor.capability_revision,
    )
    broker, calls = _trusted_broker(binding)
    authorization = _trusted_broker_authorization(binding, broker)
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 1:
            return _response(request, _discover_result(descriptor, tools=True))
        return httpx2.Response(401)

    config = _https_test_config(httpx2.MockTransport(handler), auth=authorization)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
                await connected.session.call_tool("calendar.lookup", {"query": "today"})
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED

    asyncio.run(exercise())
    assert len(requests) == 3
    assert len(calls.refresh) == 1
    assert all(request.headers.get("authorization") is None for request in requests)


@pytest.mark.parametrize(
    "location",
    [
        "/mcp",
        "https://attacker.invalid/mcp",
    ],
)
def test_broker_auth_denies_redirect_without_retry_or_token_leak(location: str) -> None:
    descriptor = _descriptor(limits=replace(McpClientLimits(), max_http_redirects=1))
    authorization = _broker_authorization_config(descriptor)
    observed_authorization: list[str | None] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        observed_authorization.append(request.headers.get("authorization"))
        return httpx2.Response(307, headers={"location": location})

    client = _direct_http_sdk_client(
        descriptor,
        _https_test_config(httpx2.MockTransport(handler), auth=authorization),
    )
    errors: list[BaseException] = []

    async def exercise() -> None:
        try:
            async with client as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
        except BaseException as error:
            errors.append(error)

    asyncio.run(exercise())
    assert observed_authorization == ["Bearer opaque-access"]
    assert len(errors) == 1
    assert _contains_client_reason(errors[0], McpClientErrorReason.IDENTITY_MISMATCH)
    assert "opaque-access" not in repr(errors[0])
    assert location not in repr(errors[0])


def test_broker_auth_rejects_descriptor_substitution_before_token_load() -> None:
    descriptor = _descriptor()
    authorization = _broker_authorization_config(descriptor)
    substituted = _forge_descriptor(descriptor, capability_revision="f" * 64)

    with pytest.raises(McpClientError) as raised:
        build_streamable_http_transport(
            substituted,
            _https_test_config(
                httpx2.MockTransport(lambda _request: httpx2.Response(500)),
                auth=authorization,
            ),
        )

    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH


def test_sealed_oauth_proof_upgrades_only_its_exact_signed_descriptor_to_authenticated(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    presenter = _OAuthPresenter()
    store = _OAuthStore()
    authorization = _oauth_authorization_config(
        tmp_path,
        descriptor,
        presenter=presenter,
        token_store=store,
    )
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        url = str(request.url)
        if url == "https://apps.example.com/mcp":
            if request.headers.get("Authorization") != "Bearer stream-access":
                return httpx2.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://apps.example.com/'
                            '.well-known/oauth-protected-resource/mcp", '
                            'scope="calendar.read"'
                        )
                    },
                )
            return _response(request, _discover_result(descriptor))
        if url == "https://apps.example.com/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={
                    "resource": "https://apps.example.com/mcp",
                    "authorization_servers": ["https://auth.example.com/issuer"],
                    "scopes_supported": ["calendar.read"],
                },
            )
        if url == "https://auth.example.com/.well-known/oauth-authorization-server/issuer":
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://auth.example.com/issuer",
                    "authorization_endpoint": "https://auth.example.com/issuer/authorize",
                    "token_endpoint": "https://auth.example.com/issuer/token",
                    "client_id_metadata_document_supported": True,
                    "code_challenge_methods_supported": ["S256"],
                    "response_types_supported": ["code"],
                    "scopes_supported": ["calendar.read"],
                },
            )
        if url == "https://auth.example.com/issuer/token":
            return httpx2.Response(
                200,
                json={
                    "access_token": "stream-access",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "refresh_token": "stream-refresh",
                    "scope": "calendar.read",
                    "audience": "https://apps.example.com/mcp",
                },
            )
        raise AssertionError(request.method)

    config = _https_test_config(httpx2.MockTransport(handler), auth=authorization)
    factory = build_streamable_http_transport(descriptor, config)
    assert factory.discovery_source is DiscoverySource.LIVE_AUTHENTICATED
    client = CurrentMcpClient(descriptor, factory)
    receipts: list[object] = []

    async def exercise() -> None:
        receipt = await _start_with_oauth(client, authorization)
        receipts.append(receipt)
        assert receipt.source is DiscoverySource.LIVE_AUTHENTICATED
        await client.close()

    asyncio.run(exercise())

    assert [request.method for request in requests] == ["POST", "GET", "GET", "POST", "POST"]
    exposed = " ".join(
        [repr(authorization), repr(config), repr(factory), repr(receipts)]
        + [repr(value) for value in store.tokens.values()]
        + [repr(value) for value in store.clients.values()]
    )
    assert all(
        marker not in exposed for marker in ("stream-code", "stream-access", "stream-refresh")
    )


@pytest.mark.parametrize("redirect_status", [307, 308])
def test_authenticated_same_endpoint_redirect_preserves_only_its_exact_request_lineage(
    tmp_path: Path,
    redirect_status: int,
) -> None:
    import mcp_platform.authorization as authorization_module

    descriptor = _descriptor(limits=replace(McpClientLimits(), max_http_redirects=1))
    authorization = _oauth_authorization_config(tmp_path, descriptor)
    requests: list[httpx2.Request] = []
    authorized_requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        url = str(request.url)
        if url == "https://apps.example.com/mcp":
            if request.headers.get("Authorization") != "Bearer redirect-access":
                return httpx2.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://apps.example.com/'
                            '.well-known/oauth-protected-resource/mcp", '
                            'scope="calendar.read"'
                        )
                    },
                )
            authorized_requests.append(request)
            if len(authorized_requests) == 1:
                return httpx2.Response(
                    redirect_status,
                    headers={"location": "/mcp"},
                )
            assert len(authorized_requests) == 2
            with pytest.raises(McpClientError) as replayed:
                await authorization_module._authorization_request_binding(
                    authorization,
                    descriptor,
                    request,
                )
            assert replayed.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
            copied_request = copy.copy(request)
            with pytest.raises(McpClientError) as copied:
                await authorization_module._authorization_request_binding(
                    authorization,
                    descriptor,
                    copied_request,
                )
            assert copied.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
            return _response(request, _discover_result(descriptor))
        if url == "https://apps.example.com/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={
                    "resource": "https://apps.example.com/mcp",
                    "authorization_servers": ["https://auth.example.com/issuer"],
                    "scopes_supported": ["calendar.read"],
                },
            )
        if url == "https://auth.example.com/.well-known/oauth-authorization-server/issuer":
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://auth.example.com/issuer",
                    "authorization_endpoint": "https://auth.example.com/issuer/authorize",
                    "token_endpoint": "https://auth.example.com/issuer/token",
                    "client_id_metadata_document_supported": True,
                    "code_challenge_methods_supported": ["S256"],
                    "response_types_supported": ["code"],
                    "scopes_supported": ["calendar.read"],
                },
            )
        if url == "https://auth.example.com/issuer/token":
            return httpx2.Response(
                200,
                json={
                    "access_token": "redirect-access",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "refresh_token": "redirect-refresh",
                    "scope": "calendar.read",
                    "audience": "https://apps.example.com/mcp",
                },
            )
        raise AssertionError(request.method)

    config = _https_test_config(httpx2.MockTransport(handler), auth=authorization)
    factory = build_streamable_http_transport(descriptor, config)
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        receipt = await _start_with_oauth(client, authorization)
        assert receipt.source is DiscoverySource.LIVE_AUTHENTICATED
        await client.close()

    asyncio.run(exercise())

    assert [request.method for request in requests] == [
        "POST",
        "GET",
        "GET",
        "POST",
        "POST",
        "POST",
    ]
    assert len(authorized_requests) == 2
    assert authorized_requests[0].content == authorized_requests[1].content
    assert all(
        request.headers["authorization"] == "Bearer redirect-access"
        for request in authorized_requests
    )


@pytest.mark.parametrize(
    ("redirect_status", "location"),
    [
        (302, "/mcp"),
        (307, "https://other.example/mcp"),
        (307, "http://apps.example.com/mcp"),
        (307, "https://apps.example.com/other"),
    ],
)
def test_authenticated_redirect_rejects_method_or_target_mutation_without_forwarding(
    tmp_path: Path,
    redirect_status: int,
    location: str,
) -> None:
    descriptor = _descriptor(limits=replace(McpClientLimits(), max_http_redirects=1))
    authorization = _oauth_authorization_config(tmp_path, descriptor)
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        url = str(request.url)
        if url == "https://apps.example.com/mcp":
            if request.headers.get("Authorization") != "Bearer rejected-access":
                return httpx2.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://apps.example.com/'
                            '.well-known/oauth-protected-resource/mcp", '
                            'scope="calendar.read"'
                        )
                    },
                )
            return httpx2.Response(
                redirect_status,
                headers={"location": location},
            )
        if url == "https://apps.example.com/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={
                    "resource": "https://apps.example.com/mcp",
                    "authorization_servers": ["https://auth.example.com/issuer"],
                    "scopes_supported": ["calendar.read"],
                },
            )
        if url == "https://auth.example.com/.well-known/oauth-authorization-server/issuer":
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://auth.example.com/issuer",
                    "authorization_endpoint": "https://auth.example.com/issuer/authorize",
                    "token_endpoint": "https://auth.example.com/issuer/token",
                    "client_id_metadata_document_supported": True,
                    "code_challenge_methods_supported": ["S256"],
                    "response_types_supported": ["code"],
                    "scopes_supported": ["calendar.read"],
                },
            )
        if url == "https://auth.example.com/issuer/token":
            return httpx2.Response(
                200,
                json={
                    "access_token": "rejected-access",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "refresh_token": "rejected-refresh",
                    "scope": "calendar.read",
                    "audience": "https://apps.example.com/mcp",
                },
            )
        raise AssertionError("redirect_target_reached")

    config = _https_test_config(httpx2.MockTransport(handler), auth=authorization)
    client = CurrentMcpClient(
        descriptor,
        build_streamable_http_transport(descriptor, config),
    )
    errors: list[BaseException] = []

    async def exercise() -> None:
        try:
            await _start_with_oauth(client, authorization)
        except BaseException as error:
            errors.append(error)
        finally:
            await client.close()

    asyncio.run(exercise())

    assert len(errors) == 1
    assert len(requests) == 5
    assert requests[-1].method == "POST"
    assert str(requests[-1].url) == "https://apps.example.com/mcp"
    assert requests[-1].headers["authorization"] == "Bearer rejected-access"
    assert all(
        marker not in repr(errors[0])
        for marker in ("rejected-access", "rejected-refresh", location)
    )


def test_sealed_oauth_provenance_rejects_descriptor_substitution_before_request(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    authorization = _oauth_authorization_config(tmp_path, descriptor)
    requests: list[httpx2.Request] = []
    config = _https_test_config(
        httpx2.MockTransport(lambda request: requests.append(request) or httpx2.Response(200)),
        auth=authorization,
    )
    substituted = _forge_descriptor(
        descriptor,
        capability_revision="f" * 64,
    )

    with pytest.raises(McpClientError) as raised:
        build_streamable_http_transport(substituted, config)

    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    assert requests == []


@pytest.mark.parametrize("mutation", ["config", "authorization", "endpoint"])
def test_authenticated_factory_rejects_builder_authority_mutation_before_client_or_network(
    tmp_path: Path,
    mutation: str,
) -> None:
    descriptor = _descriptor()
    authorization = _oauth_authorization_config(tmp_path, descriptor)
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(request, _discover_result(descriptor))

    config = _https_test_config(httpx2.MockTransport(handler), auth=authorization)
    factory = build_streamable_http_transport(descriptor, config)
    builder = factory._builder
    if mutation == "config":
        replacement = _https_test_config(httpx2.MockTransport(handler))
        object.__setattr__(builder, "config", replacement)
    elif mutation == "authorization":
        object.__setattr__(builder, "authorization", None)
    else:
        object.__setattr__(builder, "endpoint", "https://other.example.com/mcp")
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
        await client.close()

    asyncio.run(exercise())
    assert requests == []


@pytest.mark.parametrize("construction", ["copy", "object_new", "proof_reuse"])
def test_authenticated_factory_rejects_substituted_config_lifecycle_proof(
    tmp_path: Path,
    construction: str,
) -> None:
    descriptor = _descriptor()
    authorization = _oauth_authorization_config(tmp_path, descriptor)
    requests: list[httpx2.Request] = []
    config = _https_test_config(
        httpx2.MockTransport(lambda request: requests.append(request) or httpx2.Response(500)),
        auth=authorization,
    )
    factory = build_streamable_http_transport(descriptor, config)
    if construction == "copy":
        substitute = copy.copy(config)
    else:
        substitute = object.__new__(StreamableHttpClientConfig)
        object.__setattr__(substitute, "_auth", config._auth)
        object.__setattr__(substitute, "_transport", config._transport)
        object.__setattr__(substitute, "_loopback_test", config._loopback_test)
        object.__setattr__(
            substitute,
            "_proof",
            config._proof if construction == "proof_reuse" else object(),
        )
    object.__setattr__(factory._builder, "config", substitute)

    with pytest.raises(McpClientError) as raised:
        _ = factory._builder(
            mode=MCP_PROTOCOL_VERSION,
            client_info=types.Implementation(name="omega-mcp-host", version="3.0.0"),
            extensions=(),
            cache=None,
        )

    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    assert requests == []


def test_authenticated_factory_revalidates_nested_authorization_proof_before_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_platform.transports.streamable_http as http_module

    descriptor = _descriptor()
    authorization = _oauth_authorization_config(tmp_path, descriptor)
    config = _https_test_config(
        httpx2.MockTransport(lambda _request: httpx2.Response(500)), auth=authorization
    )
    factory = build_streamable_http_transport(descriptor, config)
    object.__setattr__(authorization, "_proof", object())
    constructor_calls = 0

    def forbidden_constructor(**_options: object) -> httpx2.AsyncClient:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("http-client-construction-sentinel")

    monkeypatch.setattr(http_module.httpx2, "AsyncClient", forbidden_constructor)
    client = CurrentMcpClient(descriptor, factory)

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
        await client.close()

    asyncio.run(exercise())
    assert constructor_calls == 0


@pytest.mark.parametrize("construction", ["copy", "object_new"])
def test_authenticated_factory_rejects_copied_or_object_new_builder(
    tmp_path: Path,
    construction: str,
) -> None:
    descriptor = _descriptor()
    authorization = _oauth_authorization_config(tmp_path, descriptor)
    config = _https_test_config(
        httpx2.MockTransport(lambda _request: httpx2.Response(500)),
        auth=authorization,
    )
    original = build_streamable_http_transport(descriptor, config)._builder
    if construction == "copy":
        substitute = copy.copy(original)
    else:
        substitute = object.__new__(type(original))
        for field in fields(original):
            object.__setattr__(substitute, field.name, getattr(original, field.name))

    with pytest.raises(McpClientError) as raised:
        substitute(
            mode=MCP_PROTOCOL_VERSION,
            client_info=types.Implementation(name="omega-mcp-host", version="3.0.0"),
            extensions=(),
            cache=None,
        )

    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH


def test_resource_request_hook_rebinds_authorization_before_next_oauth_network_call(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    holder: list[McpAuthorizationConfig] = []

    class MutatingPresenter(_OAuthPresenter):
        async def present_authorization(self, authorization_url: str) -> None:
            await super().present_authorization(authorization_url)
            object.__setattr__(holder[0], "_proof", object())

    authorization = _oauth_authorization_config(
        tmp_path,
        descriptor,
        presenter=MutatingPresenter(),
    )
    holder.append(authorization)
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        url = str(request.url)
        if url == "https://apps.example.com/mcp":
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata="https://apps.example.com/'
                        '.well-known/oauth-protected-resource/mcp", scope="calendar.read"'
                    )
                },
            )
        if url.endswith("/.well-known/oauth-protected-resource/mcp"):
            return httpx2.Response(
                200,
                json={
                    "resource": "https://apps.example.com/mcp",
                    "authorization_servers": ["https://auth.example.com/issuer"],
                },
            )
        if ".well-known/oauth-authorization-server" in url:
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://auth.example.com/issuer",
                    "authorization_endpoint": "https://auth.example.com/issuer/authorize",
                    "token_endpoint": "https://auth.example.com/issuer/token",
                    "client_id_metadata_document_supported": True,
                    "code_challenge_methods_supported": ["S256"],
                    "response_types_supported": ["code"],
                },
            )
        raise AssertionError("token endpoint must not be contacted")

    client = _direct_http_sdk_client(
        descriptor,
        _https_test_config(httpx2.MockTransport(handler), auth=authorization),
    )

    async def exercise() -> None:
        driver = asyncio.create_task(_drive_oauth_authorization(authorization))
        try:
            with pytest.raises(BaseExceptionGroup) as raised:
                async with client as connected:
                    await connected.session.send_request(
                        types.DiscoverRequest(),
                        types.DiscoverResult,
                    )
        finally:
            if not driver.done():
                driver.cancel()
            with suppress(BaseException):
                await driver
        assert _contains_client_reason(
            raised.value,
            McpClientErrorReason.AUTHORIZATION_INVALID,
        )

    asyncio.run(exercise())
    assert [request.method for request in requests] == ["POST", "GET", "GET"]


def test_loopback_wire_authority_is_private_and_cannot_publish_authenticated() -> None:
    descriptor = _loopback_descriptor()
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(request, _discover_result(descriptor))

    config = _loopback_test_config(httpx2.MockTransport(handler))
    client = CurrentMcpClient(
        descriptor,
        build_streamable_http_transport(descriptor, config),
    )

    async def exercise() -> None:
        with pytest.raises(McpClientError) as raised:
            await client.start()
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
        await client.close()

    asyncio.run(exercise())
    assert requests == []
    assert "for_loopback_test" not in vars(StreamableHttpClientConfig)
    assert "for_https_test" not in vars(StreamableHttpClientConfig)


def test_mock_transport_observes_exact_current_wire_headers_body_and_path() -> None:
    descriptor = _loopback_descriptor()
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(request, _discover_result(descriptor))

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        async with _direct_http_sdk_client(descriptor, config) as connected:
            discovered = await connected.session.send_request(
                types.DiscoverRequest(),
                types.DiscoverResult,
            )
            assert discovered.meta == _metadata(descriptor)

    asyncio.run(exercise())

    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.content)
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1/mcp"
    assert request.url.query == b""
    assert request.headers["mcp-protocol-version"] == MCP_PROTOCOL_VERSION
    assert request.headers["mcp-method"] == body["method"] == "server/discover"
    assert "mcp-name" not in request.headers
    assert "mcp-session-id" not in request.headers
    assert body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == (
        MCP_PROTOCOL_VERSION
    )
    assert body["params"]["_meta"]["io.modelcontextprotocol/clientInfo"] == {
        "name": "omega-mcp-host",
        "version": "3.0.0",
    }


def test_github_transport_filters_discovery_to_signed_tool_authority() -> None:
    descriptor = _forge_descriptor(
        _descriptor(origin="https://api.githubcopilot.com/mcp/"),
        signed_capability_authority={
            "tools": {
                "search_repositories": {},
                "get_file_contents": {},
                "issue_read": {},
            },
            "resources": {},
            "resource_templates": {},
        },
    )

    assert _provider_request_headers(descriptor) == {
        "X-MCP-Tools": "get_file_contents,issue_read,search_repositories"
    }
    assert _provider_request_headers(_descriptor()) == {}


def test_sdk_owned_method_and_name_headers_match_each_operation_body() -> None:
    descriptor = _loopback_descriptor(with_tool=True)
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        body = json.loads(request.content)
        method = body["method"]
        if method == "server/discover":
            result = _discover_result(descriptor, tools=True)
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "calendar.lookup",
                        "description": "Calendar lookup.",
                        "inputSchema": _TOOL_SCHEMA,
                        "_meta": _metadata(descriptor),
                    }
                ],
                "ttlMs": 0,
                "cacheScope": "private",
                "resultType": "complete",
                "_meta": _metadata(descriptor),
            }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": "one match"}],
                "isError": False,
                "resultType": "complete",
                "_meta": _metadata(descriptor),
            }
        else:
            raise AssertionError(method)
        return _response(request, result)

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        async with _direct_http_sdk_client(descriptor, config) as connected:
            await connected.session.send_request(
                types.DiscoverRequest(),
                types.DiscoverResult,
            )
            await connected.session.list_tools()
            await connected.session.list_tools()
            result = await connected.session.call_tool(
                "calendar.lookup",
                {"query": "today"},
            )
            assert type(result) is types.CallToolResult

    asyncio.run(exercise())

    assert [json.loads(request.content)["method"] for request in requests] == [
        "server/discover",
        "tools/list",
        "tools/list",
        "tools/call",
    ]
    for request in requests:
        body = json.loads(request.content)
        assert request.headers["mcp-protocol-version"] == MCP_PROTOCOL_VERSION
        assert request.headers["mcp-method"] == body["method"]
    tool_call = requests[-1]
    tool_body = json.loads(tool_call.content)
    assert tool_call.headers["mcp-name"] == tool_body["params"]["name"]


def test_response_identity_metadata_is_required_on_every_http_call() -> None:
    from mcp_platform.client import _normalize_metadata

    descriptor = _loopback_descriptor(with_tool=True)
    calls = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(request, _discover_result(descriptor, tools=True))
        return _response(
            request,
            {
                "tools": [],
                "ttlMs": 0,
                "cacheScope": "private",
                "resultType": "complete",
                "_meta": _metadata(descriptor, server_id="changed.server"),
            },
        )

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        async with _direct_http_sdk_client(descriptor, config) as connected:
            discovered = await connected.session.send_request(
                types.DiscoverRequest(),
                types.DiscoverResult,
            )
            _, server_identity = _normalize_metadata(
                descriptor,
                discovered.meta,
                expected_server=None,
            )
            listed = await connected.session.list_tools()
            with pytest.raises(McpClientError) as raised:
                _normalize_metadata(
                    descriptor,
                    listed.meta,
                    expected_server=server_identity,
                )
            assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH

    asyncio.run(exercise())
    assert calls == 2


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/mcp",
        "http://apps.example.com/mcp",
        "https://apps.example.com/other",
    ],
)
def test_cross_origin_downgrade_and_path_redirects_are_rejected_without_forwarding_credentials(
    location: str,
) -> None:
    limits = replace(
        McpClientLimits(),
        max_http_redirects=2,
        max_operation_duration_ms=150,
    )
    descriptor = _descriptor(limits=limits)
    requests: list[httpx2.Request] = []
    credential = "Bearer redirect-sentinel"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(307, headers={"location": location})

    config = _https_test_config(
        httpx2.MockTransport(handler),
        auth=_BearerAuth(credential),
    )

    async def exercise() -> None:
        with pytest.raises(Exception) as raised:
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
        assert credential not in repr(raised.value)

    asyncio.run(exercise())

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == credential


def test_rejected_redirect_body_is_never_read_and_its_stream_is_closed() -> None:
    limits = replace(
        McpClientLimits(),
        max_result_document_bytes=512,
        max_operation_duration_ms=100,
    )
    descriptor = _loopback_descriptor(limits=limits)
    stream = _DelayedOversizedStream()
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            307,
            headers={
                "location": "https://hostile.example/mcp",
                "content-type": "application/json",
            },
            stream=stream,
        )

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        started = time.monotonic()
        with pytest.raises(Exception) as raised:
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
        assert time.monotonic() - started < 0.2
        assert "xxxx" not in str(raised.value)
        assert "xxxx" not in repr(raised.value)

    asyncio.run(exercise())
    assert len(requests) == 1
    assert stream.started is False
    assert stream.close_calls == 1


def test_same_endpoint_redirects_stop_at_the_descriptor_bound() -> None:
    limits = replace(
        McpClientLimits(),
        max_http_redirects=1,
        max_operation_duration_ms=150,
    )
    descriptor = _loopback_descriptor(limits=limits)
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(307, headers={"location": "/mcp"})

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        with pytest.raises(Exception):
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )

    asyncio.run(exercise())
    assert len(requests) == 2


def test_redirect_without_location_fails_closed_without_a_second_request() -> None:
    descriptor = _loopback_descriptor(
        limits=replace(McpClientLimits(), max_operation_duration_ms=150)
    )
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(307)

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        with pytest.raises(BaseExceptionGroup):
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )

    asyncio.run(exercise())
    assert len(requests) == 1


def test_same_endpoint_redirect_body_is_bounded_before_following() -> None:
    limits = replace(
        McpClientLimits(),
        max_http_redirects=2,
        max_result_document_bytes=512,
        max_operation_duration_ms=200,
    )
    descriptor = _loopback_descriptor(limits=limits)
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(
                307,
                headers={"location": "/mcp"},
                stream=_OneChunkStream(b"x" * 2_048),
            )
        return _response(request, _discover_result(descriptor))

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        with pytest.raises(BaseExceptionGroup) as raised:
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
        assert "xxxx" not in str(raised.value)

    asyncio.run(exercise())
    assert len(requests) == 1


def test_async_client_is_constructed_with_exact_timeout_redirect_tls_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_platform.transports.streamable_http as http_module

    limits = replace(
        McpClientLimits(),
        max_http_redirects=2,
        max_connection_duration_ms=350,
        max_operation_duration_ms=175,
    )
    descriptor = _loopback_descriptor(limits=limits)
    auth = _BearerAuth("Bearer settings-sentinel")

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return _response(request, _discover_result(descriptor))

    config = _loopback_test_config(
        httpx2.MockTransport(handler),
        auth=auth,
    )
    real_async_client = httpx2.AsyncClient
    captured: list[dict[str, object]] = []

    def capture(**options: object) -> httpx2.AsyncClient:
        captured.append(options)
        return real_async_client(**options)

    monkeypatch.setattr(http_module.httpx2, "AsyncClient", capture)

    async def exercise() -> None:
        async with _direct_http_sdk_client(descriptor, config) as connected:
            await connected.session.send_request(
                types.DiscoverRequest(),
                types.DiscoverResult,
            )

    asyncio.run(exercise())

    assert len(captured) == 1
    options = captured[0]
    timeout = options["timeout"]
    assert isinstance(timeout, httpx2.Timeout)
    assert timeout.connect == 0.35
    assert timeout.pool == 0.35
    assert timeout.read == 0.175
    assert timeout.write == 0.175
    assert options["follow_redirects"] is True
    assert options["max_redirects"] == 2
    assert options["verify"] is True
    assert options["trust_env"] is False
    assert options["auth"] is auth
    assert options["http1"] is True
    assert options["http2"] is False


def test_response_bytes_are_bounded_before_sdk_json_parsing() -> None:
    limits = replace(
        McpClientLimits(),
        max_result_document_bytes=512,
        max_operation_duration_ms=200,
    )
    descriptor = _loopback_descriptor(limits=limits)

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_OneChunkStream(b"{" + b"x" * 2_048),
        )

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        started = time.monotonic()
        with pytest.raises(BaseExceptionGroup) as raised:
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
        assert time.monotonic() - started < 1
        messages = _exception_messages(raised.value)
        assert any("response_limit_exceeded" in message for message in messages)
        assert all("xxxx" not in message for message in messages)

    asyncio.run(exercise())


def test_event_stream_has_an_absolute_response_deadline() -> None:
    limits = replace(
        McpClientLimits(),
        max_result_document_bytes=1_024,
        max_operation_duration_ms=100,
    )
    descriptor = _loopback_descriptor(limits=limits)
    stream = _BlockingStream()

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        started = time.monotonic()
        with pytest.raises(BaseExceptionGroup):
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
        assert time.monotonic() - started < 1
        assert stream.close_calls == 1

    asyncio.run(exercise())


def test_cancellation_aborts_exactly_one_in_flight_response_stream() -> None:
    limits = replace(McpClientLimits(), max_operation_duration_ms=5_000)
    descriptor = _loopback_descriptor(limits=limits)
    stream = _BlockingStream()

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
        )

    config = _loopback_test_config(httpx2.MockTransport(handler))

    async def exercise() -> None:
        async def send_request() -> None:
            async with _direct_http_sdk_client(descriptor, config) as connected:
                await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )

        start_task = asyncio.create_task(send_request())
        await stream.started.wait()
        start_task.cancel("cancel-http-stream")
        with pytest.raises(asyncio.CancelledError) as raised:
            await start_task
        assert raised.value.args == ("cancel-http-stream",)
        assert stream.close_calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "active_error",
    [
        pytest.param(asyncio.CancelledError("active-cancel"), id="cancelled"),
        pytest.param(KeyboardInterrupt("active-keyboard"), id="keyboard"),
        pytest.param(_StreamBaseException("active-base"), id="base-exception"),
    ],
)
def test_response_stream_preserves_active_base_exception_and_retries_failed_cleanup(
    active_error: BaseException,
) -> None:
    from mcp_platform.transports.streamable_http import _BoundedResponseStream

    request = httpx2.Request("POST", "https://apps.example.com/mcp")
    raw_stream = _FailingIteratorAndCloseStream(active_error)
    bounded = _BoundedResponseStream(
        raw_stream,
        request=request,
        limit=512,
        duration_seconds=1,
    )

    async def exercise() -> None:
        iterator = bounded.__aiter__()
        assert await anext(iterator) == b"entered"
        with pytest.raises(BaseException) as raised:
            await anext(iterator)
        assert raised.value is active_error
        assert raw_stream.close_calls == 1
        assert raw_stream.closed is False

        await bounded.aclose()
        assert raw_stream.close_calls == 2
        assert raw_stream.closed is True

    asyncio.run(exercise())


def test_response_stream_reports_cleanup_required_while_read_failure_close_owner_lives() -> None:
    from mcp_platform.transports.streamable_http import _BoundedResponseStream

    request = httpx2.Request("POST", "https://apps.example.com:8443/mcp")
    active_error = httpx2.ReadTimeout("read-sentinel", request=request)
    raw_stream = _FailingIteratorAndResistantCloseStream(active_error)
    bounded = _BoundedResponseStream(
        raw_stream,
        request=request,
        limit=512,
        duration_seconds=1,
        shutdown_seconds=0.02,
    )

    async def exercise() -> None:
        try:
            iterator = bounded.__aiter__()
            assert await anext(iterator) == b"entered"
            with pytest.raises(McpClientError) as raised:
                await anext(iterator)
            assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            owner = bounded._close_task
            assert isinstance(owner, asyncio.Task)
            assert not owner.done()
            assert raw_stream.close_calls == 1

            with pytest.raises(McpClientError) as retried:
                await bounded.aclose()
            assert retried.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert bounded._close_task is owner
            assert raw_stream.close_calls == 1

            raw_stream.release_close.set()
            await owner
            await bounded.aclose()
            assert raw_stream.closed is True
        finally:
            raw_stream.release_close.set()
            owner = bounded._close_task
            if owner is not None:
                await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "active_error",
    [
        pytest.param(asyncio.CancelledError("active-cancel"), id="cancelled"),
        pytest.param(KeyboardInterrupt("active-keyboard"), id="keyboard"),
        pytest.param(_StreamBaseException("active-base"), id="base-exception"),
    ],
)
def test_response_stream_preserves_nonordinary_error_identity_with_live_close_owner(
    active_error: BaseException,
) -> None:
    from mcp_platform.transports.streamable_http import _BoundedResponseStream

    raw_stream = _FailingIteratorAndResistantCloseStream(active_error)
    bounded = _BoundedResponseStream(
        raw_stream,
        request=httpx2.Request("POST", "https://apps.example.com:8443/mcp"),
        limit=512,
        duration_seconds=1,
        shutdown_seconds=0.02,
    )

    async def exercise() -> None:
        try:
            iterator = bounded.__aiter__()
            assert await anext(iterator) == b"entered"
            with pytest.raises(BaseException) as raised:
                await anext(iterator)
            assert raised.value is active_error
            owner = bounded._close_task
            assert isinstance(owner, asyncio.Task)
            assert not owner.done()
            assert raw_stream.close_calls == 1

            with pytest.raises(McpClientError) as retried:
                await bounded.aclose()
            assert retried.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert bounded._close_task is owner

            raw_stream.release_close.set()
            await owner
            await bounded.aclose()
        finally:
            raw_stream.release_close.set()
            owner = bounded._close_task
            if owner is not None:
                await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(exercise())


def test_response_stream_drains_one_close_through_repeated_cancellation() -> None:
    from mcp_platform.transports.streamable_http import _BoundedResponseStream

    request = httpx2.Request("POST", "https://apps.example.com/mcp")
    raw_stream = _GatedCloseStream()
    bounded = _BoundedResponseStream(
        raw_stream,
        request=request,
        limit=512,
        duration_seconds=1,
    )

    async def exercise() -> None:
        close_task = asyncio.create_task(bounded.aclose())
        await raw_stream.entered.wait()
        close_task.cancel("first-cancel")
        await asyncio.sleep(0)
        close_task.cancel("second-cancel")
        await asyncio.sleep(0)
        assert not close_task.done()
        raw_stream.release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await close_task
        assert raised.value.args == ("first-cancel",)
        assert raw_stream.close_calls == 1
        assert raw_stream.closed is True

        await bounded.aclose()
        assert raw_stream.close_calls == 1

    asyncio.run(exercise())


def test_stale_close_waiter_cannot_erase_a_newer_retry_owner() -> None:
    from mcp_platform.transports.streamable_http import _BoundedResponseStream

    request = httpx2.Request("POST", "https://apps.example.com/mcp")
    raw_stream = _FailThenGatedRetryStream()
    bounded = _BoundedResponseStream(
        raw_stream,
        request=request,
        limit=512,
        duration_seconds=1,
    )

    async def exercise() -> None:
        first_waiter = asyncio.create_task(bounded.aclose())
        await raw_stream.first_entered.wait()
        first_owner = bounded._close_task
        assert isinstance(first_owner, asyncio.Task)
        retry_waiters: list[asyncio.Task[None]] = []

        async def retry_after_first_waiter() -> None:
            try:
                await first_waiter
            except BaseException:
                pass
            await bounded.aclose()

        def schedule_retry(_completed: asyncio.Task[None]) -> None:
            retry_waiters.append(asyncio.create_task(retry_after_first_waiter()))

        first_owner.add_done_callback(schedule_retry)
        stale_waiter = asyncio.create_task(bounded.aclose())
        await asyncio.sleep(0)
        assert bounded._close_task is first_owner
        raw_stream.first_release.set()
        await raw_stream.retry_entered.wait()
        failures = await asyncio.gather(
            first_waiter,
            stale_waiter,
            return_exceptions=True,
        )
        assert [type(error) for error in failures] == [RuntimeError, RuntimeError]
        assert len(retry_waiters) == 1

        final_waiter = asyncio.create_task(bounded.aclose())
        await asyncio.sleep(0)
        assert raw_stream.close_calls == 2
        assert raw_stream.max_active_closes == 1
        raw_stream.retry_release.set()
        await retry_waiters[0]
        await final_waiter
        assert raw_stream.closed is True
        assert raw_stream.close_calls == 2

        await bounded.aclose()
        assert raw_stream.close_calls == 2

    asyncio.run(exercise())


def test_loopback_asgi_current_server_uses_the_same_public_sdk_transport_path() -> None:
    descriptor = _loopback_descriptor()
    observed: list[tuple[str, str, str | None]] = []
    metadata = _metadata(descriptor)

    async def discover(_ctx: Any, _params: types.RequestParams) -> types.DiscoverResult:
        return types.DiscoverResult(
            supportedVersions=[MCP_PROTOCOL_VERSION],
            capabilities=types.ServerCapabilities(),
            ttlMs=0,
            cacheScope="private",
            resultType="complete",
            _meta=metadata,
        )

    server = Server("example.calendar", version="2.4.0")
    server.add_request_handler("server/discover", types.RequestParams, discover)
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)

    async def observed_app(scope: Any, receive: Any, send: Any) -> None:
        headers = {key.decode(): value.decode() for key, value in scope["headers"]}
        observed.append((scope["method"], scope["path"], headers.get("mcp-session-id")))
        await manager.handle_request(scope, receive, send)

    config = _loopback_test_config(httpx2.ASGITransport(app=observed_app))

    async def exercise() -> None:
        async with manager.run():
            async with _direct_http_sdk_client(descriptor, config) as connected:
                discovered = await connected.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
                assert discovered.meta == {
                    key: value for key, value in metadata.items() if value is not None
                }

    asyncio.run(exercise())
    assert observed == [("POST", "/mcp", None)]
