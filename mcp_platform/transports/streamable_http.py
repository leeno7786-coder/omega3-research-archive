"""Validated official-SDK Streamable HTTP transport for current remote MCP clients."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
import weakref
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from mcp_platform.authorization import (
    _authorization_context_revision,
    _authorization_descriptor_binding,
    _authorization_redirect_binding,
    _authorization_request_binding,
    _authorization_response_binding,
    _authorization_transport_binding,
    _begin_broker_transport_operation,
    _broker_transport_response_context_entry,
    _broker_transport_response_registration,
    _consume_broker_transport_response_proof,
    _finish_broker_transport_operation,
    _mark_broker_transport_response,
    _snapshot_descriptor,
    is_mcp_authorization_config,
    McpBrokerAuthorizationConfig,
    McpHttpAuthorization,
)
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client import (
    SdkClientFactory,
    _issue_sdk_client_factory,
    _SDK_CLIENT_FACTORY_ISSUER,
)
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpConnectionDescriptor,
    McpTransport,
    _REMOTE_ORIGIN_PATH_RE,
)

_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z"
)
_MCP_PATH = "/mcp"
_LOOPBACK_ORIGIN = "http://127.0.0.1"
_REDIRECT_HOP_EXTENSION = "omega.streamable_http.hop"
_HTTP_CONFIG_ISSUER = object()
_AUTH_FLOW_RESPONSE_LIMIT = 16
_HTTP_LOG = logging.getLogger("omega.mcp.http")


_CAPTURE_ENV = "OMEGA_MCP_CAPTURE_FAILURES"
_MAX_CAPTURE_BYTES = 65_536


def _capture_failure_body(response: httpx2.Response, method: str) -> str | None:
    """Write one failing response body to a file the operator asked for.

    Off unless ``OMEGA_MCP_CAPTURE_FAILURES=1``. The log itself never carries
    provider-authored text -- that rule is what makes it safe to read and to
    ship -- but an operator diagnosing an opaque 403 needs to see what the
    provider actually said. This writes the body to its own file and returns
    only the path, so the operator opts in, chooses when to look, and deletes
    it afterwards. The request, its headers and any authorization are never
    written.
    """
    if os.environ.get(_CAPTURE_ENV, "").strip() != "1":
        return None
    try:
        from core.paths import OMEGA_ROOT

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        safe_method = method.replace("/", "-")
        target = OMEGA_ROOT / "guardian" / "logs" / f"mcp-failure-{stamp}-{safe_method}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        # The envelope matters as much as the body: a status that disagrees
        # with what the body says is itself the finding. Response headers are
        # provider-authored and belong in this opt-in file; the request, its
        # headers and any authorization are never written.
        body = response.content
        envelope = {
            "status": response.status_code,
            "method": method,
            "url": str(response.request.url),
            "response_headers": dict(response.headers),
            "body_bytes": len(body),
            "body_truncated": len(body) > _MAX_CAPTURE_BYTES,
            "body": body[:_MAX_CAPTURE_BYTES].decode("utf-8", "replace"),
        }
        target.write_text(json.dumps(envelope, indent=1), encoding="utf-8")
        return str(target)
    except Exception:  # noqa: BLE001 - a diagnostic must never break a request
        return None


def _log_resource_http_failure(response: httpx2.Response) -> None:
    """Keep operational causes without copying provider text or credential material."""
    # The server-initiated event stream is OPTIONAL in Streamable HTTP, and a
    # server that does not offer it answers the GET with 405. Slack does, and
    # every healthy connection to it therefore logged
    # "MCP resource request failed: method=unknown status=405
    # reason=unclassified" -- a warning about nothing, on a connection that had
    # just succeeded, which is worse than silence because it makes a working
    # system look broken. The request/response half carries every call Omega
    # makes; only server-pushed notifications are unavailable.
    if response.request.method == "GET" and response.status_code == 405:
        _HTTP_LOG.info(
            "MCP server does not offer the optional event stream; "
            "using request/response only"
        )
        return
    method = "unknown"
    try:
        request = json.loads(response.request.content)
    except (ValueError, httpx2.RequestNotRead):
        request = None
    request_method = request.get("method") if isinstance(request, dict) else None
    if isinstance(request_method, str) and request_method in {
        "initialize", "notifications/initialized", "server/discover", "tools/list",
        "tools/call", "prompts/list", "resources/list", "resources/templates/list",
    }:
        method = request_method
    reason = _challenge_reason(response) or "unclassified"
    if len(response.content) <= 65_536:
        try:
            document = response.json()
        except ValueError:
            document = None
        error = document.get("error") if isinstance(document, dict) else None
        details = error.get("details") if isinstance(error, dict) else None
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                candidate = detail.get("reason")
                if isinstance(candidate, str) and candidate in {
                    "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "ACCESS_TOKEN_EXPIRED",
                    "ACCESS_TOKEN_INVALID", "SERVICE_DISABLED", "CONSUMER_INVALID",
                    "BILLING_DISABLED", "IAM_PERMISSION_DENIED", "RATE_LIMIT_EXCEEDED",
                    "ORG_RESTRICTION_VIOLATION", "SECURITY_POLICY_VIOLATED",
                }:
                    reason = candidate
                    break
        if reason == "unclassified" and isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and "developer preview" in message.casefold():
                reason = "developer_preview_access"
        if reason == "unclassified" and isinstance(error, dict):
            # The envelope still names its canonical status when it ships no
            # details[].reason, and that status is a closed enum rather than
            # provider prose -- "unclassified" told an operator nothing at all.
            # Only the canonical set is admitted, for the same reason the
            # message and the details metadata are never logged: nothing
            # provider-authored reaches the log.
            status = error.get("status")
            if isinstance(status, str) and status in _CANONICAL_STATUS:
                reason = status
    captured = _capture_failure_body(response, method)
    _HTTP_LOG.warning(
        "MCP resource request failed: method=%s status=%d reason=%s%s",
        method,
        response.status_code,
        reason,
        f" body_captured={captured}" if captured else "",
    )


# The canonical status set (google.rpc.Code). A closed enum, never prose.
# RFC 6750 names the failure in the challenge header, and its error codes are a
# closed registry rather than provider prose. Google's MCP endpoints answer an
# auth failure with the challenge AND a successful-looking body, so a classifier
# that only reads the body can never see why a request was refused.
_BEARER_ERRORS = frozenset({"invalid_request", "invalid_token", "insufficient_scope"})
_BEARER_ERROR = re.compile(r'error\s*=\s*"?([a-z_]{1,64})"?')


def _challenge_reason(response: httpx2.Response) -> str | None:
    challenge = response.headers.get("www-authenticate")
    if not isinstance(challenge, str):
        return None
    found = _BEARER_ERROR.search(challenge)
    if found is None:
        return None
    error = found.group(1)
    return error if error in _BEARER_ERRORS else None


_CANONICAL_STATUS = frozenset(
    {
        "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED", "NOT_FOUND",
        "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "FAILED_PRECONDITION",
        "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED", "INTERNAL", "UNAVAILABLE", "DATA_LOSS",
        "UNAUTHENTICATED",
    }
)


def _fail(reason: McpClientErrorReason) -> None:
    raise McpClientError(reason)


def build_bounded_http_client(
    *,
    transport: httpx2.AsyncBaseTransport | None,
    connection_seconds: float,
    operation_seconds: float,
) -> httpx2.AsyncClient:
    """Construct a bounded client inside Omega's sole HTTP-client owner."""
    if (
        transport is not None
        and not isinstance(transport, httpx2.AsyncBaseTransport)
        or type(connection_seconds) is not float
        or connection_seconds <= 0
        or type(operation_seconds) is not float
        or operation_seconds <= 0
    ):
        raise ValueError("http_client_config_invalid")
    return httpx2.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=httpx2.Timeout(
            connect=connection_seconds,
            read=operation_seconds,
            write=operation_seconds,
            pool=connection_seconds,
        ),
        trust_env=False,
    )


def build_bounded_sync_http_client(
    *,
    transport: httpx2.BaseTransport | None,
    connection_seconds: float,
    operation_seconds: float,
) -> httpx2.Client:
    """Construct a bounded synchronous client inside Omega's sole HTTP-client owner."""
    if (
        transport is not None
        and not isinstance(transport, httpx2.BaseTransport)
        or type(connection_seconds) is not float
        or connection_seconds <= 0
        or type(operation_seconds) is not float
        or operation_seconds <= 0
    ):
        raise ValueError("http_client_config_invalid")
    return httpx2.Client(
        transport=transport,
        follow_redirects=False,
        timeout=httpx2.Timeout(
            connect=connection_seconds,
            read=operation_seconds,
            write=operation_seconds,
            pool=connection_seconds,
        ),
        trust_env=False,
    )


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class StreamableHttpClientConfig:
    """Sealed construction inputs for the adapter-owned ``httpx2.AsyncClient``."""

    _auth: httpx2.Auth | None
    _transport: httpx2.AsyncBaseTransport | None
    _loopback_test: bool
    _proof: object

    @classmethod
    def unauthenticated(cls) -> StreamableHttpClientConfig:
        return _issue_http_config(
            _HTTP_CONFIG_ISSUER,
            auth=None,
            transport=None,
            loopback_test=False,
        )

    @classmethod
    def authenticated(cls, auth: McpHttpAuthorization) -> StreamableHttpClientConfig:
        if not is_mcp_authorization_config(auth):
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        return _issue_http_config(
            _HTTP_CONFIG_ISSUER,
            auth=auth,
            transport=None,
            loopback_test=False,
        )

    @classmethod
    def _for_loopback_test(
        cls,
        transport: httpx2.AsyncBaseTransport,
        *,
        auth: httpx2.Auth | None = None,
    ) -> StreamableHttpClientConfig:
        if type(transport) not in {httpx2.MockTransport, httpx2.ASGITransport} or (
            auth is not None and not isinstance(auth, httpx2.Auth)
        ):
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        return _issue_http_config(
            _HTTP_CONFIG_ISSUER,
            auth=auth,
            transport=transport,
            loopback_test=True,
        )

    @classmethod
    def _for_https_test(
        cls,
        transport: httpx2.AsyncBaseTransport,
        *,
        auth: httpx2.Auth | None = None,
    ) -> StreamableHttpClientConfig:
        if type(transport) not in {httpx2.MockTransport, httpx2.ASGITransport} or (
            auth is not None and not isinstance(auth, httpx2.Auth)
        ):
            _fail(McpClientErrorReason.IDENTITY_MISMATCH)
        return _issue_http_config(
            _HTTP_CONFIG_ISSUER,
            auth=auth,
            transport=transport,
            loopback_test=False,
        )

    def __repr__(self) -> str:
        mode = "loopback_test" if self._loopback_test else "remote"
        return f"StreamableHttpClientConfig(mode={mode!r})"


@dataclass(frozen=True, slots=True)
class _IssuedHttpConfig:
    config_ref: weakref.ReferenceType[StreamableHttpClientConfig]
    auth: httpx2.Auth | None
    transport: httpx2.AsyncBaseTransport | None
    loopback_test: bool
    proof: object
    fingerprint: tuple[int, int, int, int, bool]


_ISSUED_HTTP_CONFIGS: weakref.WeakKeyDictionary[
    StreamableHttpClientConfig,
    _IssuedHttpConfig,
] = weakref.WeakKeyDictionary()


def _http_config_fingerprint(
    config: StreamableHttpClientConfig,
    *,
    auth: httpx2.Auth | None,
    transport: httpx2.AsyncBaseTransport | None,
    loopback_test: bool,
    proof: object,
) -> tuple[int, int, int, int, bool]:
    return (id(config), id(auth), id(transport), id(proof), loopback_test)


def _issue_http_config(
    issuer: object,
    *,
    auth: httpx2.Auth | None,
    transport: httpx2.AsyncBaseTransport | None,
    loopback_test: bool,
) -> StreamableHttpClientConfig:
    if (
        issuer is not _HTTP_CONFIG_ISSUER
        or type(loopback_test) is not bool
        or auth is not None
        and not isinstance(auth, httpx2.Auth)
        or loopback_test
        and type(transport) not in {httpx2.MockTransport, httpx2.ASGITransport}
        or transport is not None
        and type(transport) not in {httpx2.MockTransport, httpx2.ASGITransport}
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    config = object.__new__(StreamableHttpClientConfig)
    proof = object()
    object.__setattr__(config, "_auth", auth)
    object.__setattr__(config, "_transport", transport)
    object.__setattr__(config, "_loopback_test", loopback_test)
    object.__setattr__(config, "_proof", proof)
    _ISSUED_HTTP_CONFIGS[config] = _IssuedHttpConfig(
        config_ref=weakref.ref(config),
        auth=auth,
        transport=transport,
        loopback_test=loopback_test,
        proof=proof,
        fingerprint=_http_config_fingerprint(
            config,
            auth=auth,
            transport=transport,
            loopback_test=loopback_test,
            proof=proof,
        ),
    )
    return config


def _http_config_record(config: object) -> _IssuedHttpConfig:
    if type(config) is not StreamableHttpClientConfig:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    record = _ISSUED_HTTP_CONFIGS.get(config)
    if record is None or record.config_ref() is not config:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    if (
        config._auth is not record.auth
        or config._transport is not record.transport
        or config._loopback_test is not record.loopback_test
        or config._proof is not record.proof
        or _http_config_fingerprint(
            config,
            auth=config._auth,
            transport=config._transport,
            loopback_test=config._loopback_test,
            proof=config._proof,
        )
        != record.fingerprint
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return record


def _canonical_endpoint(descriptor: McpConnectionDescriptor, *, loopback_test: bool) -> str:
    if (
        type(descriptor) is not McpConnectionDescriptor
        or descriptor.deployment is not McpClientDeployment.REMOTE
        or descriptor.transport is not McpTransport.STREAMABLE_HTTP
        or descriptor.verified_launch_sha256 is not None
        or type(descriptor.remote_origin) is not str
        or type(descriptor.authorization_issuer) is not str
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    origin = descriptor.remote_origin
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except (TypeError, ValueError):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    hostname = parsed.hostname
    authority = hostname if port is None else f"{hostname}:{port}"
    if (
        parsed.scheme != "https"
        or hostname is None
        or _HOST.fullmatch(hostname) is None
        or port in {0, 443}
        or parsed.netloc != authority
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != origin
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    if loopback_test:
        return f"{_LOOPBACK_ORIGIN}{_MCP_PATH}"
    # The origin IS the endpoint. This used to admit three path literals, which
    # rejected "/mcp" while accepting "/mcp/", so Slack's published endpoint
    # failed at connect with identity_mismatch; and it then APPENDED "/mcp" to
    # a bare origin, which is how Miro and Stripe -- the only two providers
    # serving at their root -- answered the connect POST with 404 (live
    # 2026-09-07). Omega does not invent a path a provider did not publish.
    # The origin reached here through ``McpConnectionDescriptor``, which
    # validates the path against this same grammar; re-checking keeps this a
    # boundary rather than trusting the caller.
    if parsed.path and _REMOTE_ORIGIN_PATH_RE.fullmatch(parsed.path) is None:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return origin


def _provider_request_headers(descriptor: McpConnectionDescriptor) -> dict[str, str]:
    """Return provider-standard discovery filters derived from signed authority."""

    try:
        hostname = urlsplit(str(descriptor.remote_origin)).hostname
    except (TypeError, ValueError):
        return {}
    if hostname != "api.githubcopilot.com":
        return {}
    authority = descriptor.signed_capability_authority
    tools = authority.get("tools") if isinstance(authority, Mapping) else None
    if not isinstance(tools, Mapping) or not tools:
        return {}
    names = tuple(sorted(tools))
    if any(type(name) is not str or not name or "," in name for name in names):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return {"X-MCP-Tools": ",".join(names)}


class _BoundedResponseStream(httpx2.AsyncByteStream):
    def __init__(
        self,
        stream: httpx2.AsyncByteStream,
        *,
        request: httpx2.Request,
        limit: int,
        duration_seconds: float,
        shutdown_seconds: float | None = None,
    ) -> None:
        self._stream = stream
        self._request = request
        self._limit = limit
        self._duration_seconds = duration_seconds
        self._shutdown_seconds = duration_seconds if shutdown_seconds is None else shutdown_seconds
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._iterator_to_close: AsyncIterator[bytes] | None = None
        self._iterator_reading = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        deadline = asyncio.get_running_loop().time() + self._duration_seconds
        iterator = self._stream.__aiter__()
        self._iterator_to_close = iterator
        active_error: BaseException | None = None
        try:
            while not self._closed:
                try:
                    self._iterator_reading = True
                    try:
                        async with asyncio.timeout_at(deadline):
                            chunk = await anext(iterator)
                    finally:
                        self._iterator_reading = False
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    raise httpx2.ReadTimeout(
                        "response_timeout",
                        request=self._request,
                    ) from None
                if type(chunk) is not bytes:
                    raise httpx2.StreamError("response_invalid")
                size += len(chunk)
                if size > self._limit:
                    raise httpx2.StreamError("response_limit_exceeded")
                yield chunk
        except BaseException as error:
            active_error = error
        cleanup_error: BaseException | None = None
        try:
            await self.aclose()
        except BaseException as error:
            cleanup_error = error
        if active_error is not None and (
            isinstance(active_error, asyncio.CancelledError)
            or not isinstance(active_error, Exception)
        ):
            raise active_error
        if (
            type(cleanup_error) is McpClientError
            and cleanup_error.reason is McpClientErrorReason.CLEANUP_REQUIRED
        ):
            raise cleanup_error
        if active_error is not None:
            raise active_error
        if cleanup_error is not None:
            raise cleanup_error

    async def _close_underlying(self) -> None:
        try:
            if not self._closed:
                await self._stream.aclose()
                self._closed = True
        finally:
            # Closing a stream object does not close its __aiter__ generator.
            # Own that finalization within the same bounded cleanup task rather
            # than leaving nested HTTP generators to race at loop shutdown.
            # A running anext must unwind after stream abortion before closure;
            # a generator paused at yield is already safe to finalize here.
            iterator = self._iterator_to_close
            if iterator is not None and not self._iterator_reading:
                close_iterator = getattr(iterator, "aclose", None)
                if callable(close_iterator):
                    await close_iterator()
                self._iterator_to_close = None

    async def aclose(self) -> None:
        if self._closed and self._iterator_to_close is None:
            return
        close_task = self._close_task
        if close_task is None or close_task.done():
            close_task = asyncio.create_task(self._close_underlying())
            self._close_task = close_task
            close_task.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
        active_cancel: asyncio.CancelledError | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_seconds
        while loop.time() < deadline and not close_task.done():
            try:
                remaining = max(0.0, deadline - loop.time())
                done, _pending = await asyncio.wait({close_task}, timeout=remaining)
            except asyncio.CancelledError as error:
                current = asyncio.current_task()
                if current is not None and current.cancelling() and active_cancel is None:
                    active_cancel = error
                continue
            if close_task in done:
                break
        if close_task.done():
            try:
                close_task.result()
            except BaseException:
                if self._close_task is close_task:
                    self._close_task = None
                if active_cancel is not None:
                    raise active_cancel
                raise
        else:
            if active_cancel is not None:
                raise active_cancel
            _fail(McpClientErrorReason.CLEANUP_REQUIRED)
        if active_cancel is not None:
            raise active_cancel


class _EmptyResponseStream(httpx2.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        if False:
            yield b""

    async def aclose(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class _HttpRequestBoundary:
    endpoint: str
    descriptor: McpConnectionDescriptor
    authorization: McpHttpAuthorization | None

    async def __call__(self, request: httpx2.Request) -> None:
        redirected = False
        if self.authorization is not None:
            redirected = await _authorization_request_binding(
                self.authorization,
                self.descriptor,
                request,
            )
        if str(request.url) != self.endpoint:
            return
        hop = (
            request.extensions.get(_REDIRECT_HOP_EXTENSION, 0)
            if redirected or self.authorization is None
            else 0
        )
        request.extensions[_REDIRECT_HOP_EXTENSION] = hop + 1


@dataclass(frozen=True, slots=True)
class _HttpResponseBoundary:
    endpoint: str
    descriptor: McpConnectionDescriptor
    authorization: McpHttpAuthorization | None
    max_bytes: int
    max_duration_seconds: float
    max_redirects: int
    broker_state: _BrokerFailureState | None = None

    def _bind_stream(self, response: httpx2.Response) -> None:
        response.stream = _BoundedResponseStream(
            response.stream,
            request=response.request,
            limit=self.max_bytes,
            duration_seconds=self.max_duration_seconds,
            shutdown_seconds=(self.descriptor.limits.max_shutdown_duration_ms / 1_000),
        )

    async def _reject_redirect(self, response: httpx2.Response) -> None:
        if not isinstance(response.stream, _BoundedResponseStream):
            self._bind_stream(response)
        await response.aclose()
        response.status_code = 400
        response.headers.clear()
        response.stream = _EmptyResponseStream()

    async def __call__(self, response: httpx2.Response) -> None:
        resource_request = str(response.request.url) == self.endpoint
        broker_proof: object | None = None
        if self.authorization is not None:
            try:
                if type(self.authorization) is McpBrokerAuthorizationConfig:
                    broker_proof = _mark_broker_transport_response(
                        self.authorization,
                        self.descriptor,
                        response,
                    )
                _authorization_response_binding(
                    self.authorization,
                    self.descriptor,
                    response.request,
                )
            except McpClientError:
                await response.aclose()
                raise
        if broker_proof is not None:
            if self.broker_state is None:
                await response.aclose()
                _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
            self.broker_state.bind_response(broker_proof)
        if not resource_request:
            if self.authorization is None:
                await response.aclose()
                raise httpx2.ProtocolError("authorization_request_rejected")
        if response.is_redirect:
            if type(self.authorization) is McpBrokerAuthorizationConfig:
                try:
                    await _authorization_redirect_binding(
                        self.authorization,
                        self.descriptor,
                        response,
                    )
                except McpClientError:
                    await response.aclose()
                    raise
            if not resource_request:
                await self._reject_redirect(response)
                return
            location = response.headers.get("location")
            hop = response.request.extensions.get(_REDIRECT_HOP_EXTENSION, 1)
            endpoint_path = urlsplit(self.endpoint).path
            if location not in {endpoint_path, self.endpoint} or hop > self.max_redirects:
                await self._reject_redirect(response)
                return
            target = response.request.url.join(location)
            if str(target) != self.endpoint:
                await self._reject_redirect(response)
                return
            self._bind_stream(response)
            try:
                await response.aread()
            except (httpx2.StreamError, httpx2.TimeoutException):
                await self._reject_redirect(response)
                return
            if self.authorization is not None:
                try:
                    await _authorization_redirect_binding(
                        self.authorization,
                        self.descriptor,
                        response,
                    )
                except McpClientError:
                    await response.aclose()
                    raise
            return
        self._bind_stream(response)
        if resource_request and response.is_error:
            await response.aread()
            _log_resource_http_failure(response)


@asynccontextmanager
async def _owned_streamable_http(
    endpoint: str,
    http_client: httpx2.AsyncClient,
    broker_state: _BrokerFailureState | None,
) -> AsyncIterator[Any]:
    async with http_client:
        async with streamable_http_client(
            endpoint,
            http_client=http_client,
            terminate_on_close=False,
        ) as streams:
            if broker_state is None:
                yield streams
            else:
                yield _BrokerMarkedReadStream(streams[0], broker_state), streams[1]


class _BrokerFailureState:
    __slots__ = ("_authorization", "_response_markers", "_translated")

    def __init__(self, authorization: McpBrokerAuthorizationConfig) -> None:
        self._authorization = authorization
        self._response_markers: dict[object, _BrokerResponseEntry] = {}
        self._translated: list[McpClientError] = []

    def bind_response(self, proof: object) -> None:
        operation, request, response = _broker_transport_response_registration(
            self._authorization,
            proof,
        )
        if operation in self._response_markers:
            _fail(McpClientErrorReason.AUTHORIZATION_INVALID)
        self._response_markers[operation] = _BrokerResponseEntry(
            proof=proof,
            request=request,
            response=response,
        )

    def correlate(
        self,
        item: SessionMessage | Exception,
        sender_context: object,
    ) -> SessionMessage | Exception:
        if type(item) is not SessionMessage or type(item.message) is not types.JSONRPCError:
            return item
        entry = _broker_transport_response_context_entry(
            self._authorization,
            sender_context,
        )
        if entry is None:
            return item
        operation, proof = entry
        bound = self._response_markers.get(operation)
        if bound is None or bound.proof is not proof:
            return item
        del self._response_markers[operation]
        marker = _consume_broker_transport_response_proof(
            self._authorization,
            operation,
            proof,
            bound.request,
            bound.response,
        )
        error = types.ErrorData(
            code=types.INTERNAL_ERROR,
            message="Broker authorization rejected",
            data=marker,
        )
        return SessionMessage(
            types.JSONRPCError(
                jsonrpc="2.0",
                id=item.message.id,
                error=error,
            ),
            item.metadata,
        )

    def clear_operation(self, operation: object) -> None:
        self._response_markers.pop(operation, None)

    def mark(self, error: McpClientError) -> None:
        self._translated.append(error)

    def consume(self, error: BaseException | None) -> bool:
        for index, translated in enumerate(self._translated):
            if translated is error:
                del self._translated[index]
                return True
        return False

    def clear(self) -> None:
        self._response_markers.clear()
        self._translated.clear()


@dataclass(slots=True, repr=False, eq=False)
class _BrokerResponseEntry:
    proof: object
    request: httpx2.Request
    response: httpx2.Response


class _BrokerMarkedReadStream:
    __slots__ = ("_state", "_stream")

    def __init__(self, stream: Any, state: _BrokerFailureState) -> None:
        self._stream = stream
        self._state = state

    @property
    def last_context(self) -> Any:
        return getattr(self._stream, "last_context", None)

    async def receive(self) -> SessionMessage | Exception:
        item = await self._stream.receive()
        return self._state.correlate(item, self.last_context)

    def __aiter__(self) -> _BrokerMarkedReadStream:
        return self

    async def __anext__(self) -> SessionMessage | Exception:
        item = await anext(self._stream)
        return self._state.correlate(item, self.last_context)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def __aenter__(self) -> _BrokerMarkedReadStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> bool | None:
        return await self._stream.__aexit__(exc_type, exc_value, traceback)


class _BrokerFailureSession:
    __slots__ = ("_authorization", "_session", "_state")

    def __init__(
        self,
        session: Any,
        authorization: McpBrokerAuthorizationConfig,
        state: _BrokerFailureState,
    ) -> None:
        self._session = session
        self._authorization = authorization
        self._state = state

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._session, name)
        if not callable(value):
            return value

        def invoke(*args: Any, **kwargs: Any) -> Any:
            outcome = value(*args, **kwargs)
            if inspect.isawaitable(outcome):
                return self._await_transport(outcome)
            return outcome

        return invoke

    async def _await_transport(self, outcome: Any) -> Any:
        operation, context_token = _begin_broker_transport_operation(self._authorization)
        finished = False
        try:
            return await outcome
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = _finish_broker_transport_operation(
                self._authorization,
                operation,
                context_token,
            )
            finished = True
            if _is_marked_second_401_failure(error, failure):
                translated = McpClientError(failure.reason)
                self._state.mark(translated)
                raise translated from None
            raise
        finally:
            try:
                if not finished:
                    _finish_broker_transport_operation(
                        self._authorization,
                        operation,
                        context_token,
                    )
            finally:
                self._state.clear_operation(operation)


class _BrokerFailureConnected:
    __slots__ = ("_connected", "_session")

    def __init__(
        self,
        connected: Client,
        authorization: McpBrokerAuthorizationConfig,
        state: _BrokerFailureState,
    ) -> None:
        self._connected = connected
        self._session = _BrokerFailureSession(connected.session, authorization, state)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connected, name)

    @property
    def session(self) -> _BrokerFailureSession:
        return self._session


class _BrokerFailureClient:
    __slots__ = ("_authorization", "_client", "_state")

    def __init__(
        self,
        client: Client,
        authorization: McpBrokerAuthorizationConfig,
        state: _BrokerFailureState | None = None,
    ) -> None:
        self._client = client
        self._authorization = authorization
        self._state = state if state is not None else _BrokerFailureState(authorization)

    @property
    def elicitation_callback(self) -> Any:
        return self._client.elicitation_callback

    @elicitation_callback.setter
    def elicitation_callback(self, value: Any) -> None:
        self._client.elicitation_callback = value

    async def __aenter__(self) -> _BrokerFailureConnected:
        connected = await self._client.__aenter__()
        return _BrokerFailureConnected(connected, self._authorization, self._state)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        translated = self._state.consume(exc_value)
        try:
            await self._client.__aexit__(exc_type, exc_value, traceback)
        except asyncio.CancelledError:
            raise
        except BaseExceptionGroup as error:
            if translated and _contains_only_exception_identity(error, exc_value):
                assert exc_value is not None
                raise exc_value
            raise
        finally:
            self._state.clear()


def _is_marked_second_401_failure(error: Exception, failure: Any) -> bool:
    return bool(
        failure is not None
        and failure.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
        and failure.response_consumed
        and failure.stream_consumed
        and type(error) is MCPError
        and error.data is failure.marker
    )


def _contains_only_exception_identity(
    error: BaseException,
    target: BaseException | None,
) -> bool:
    if error is target:
        return True
    return isinstance(error, BaseExceptionGroup) and all(
        _contains_only_exception_identity(nested, target) for nested in error.exceptions
    )


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _HttpSdkClientBuilder:
    endpoint: str
    descriptor: McpConnectionDescriptor
    authorization: McpHttpAuthorization | None
    config: StreamableHttpClientConfig
    connection_seconds: float
    operation_seconds: float
    max_bytes: int
    max_redirects: int

    def __call__(
        self,
        *,
        mode: str,
        client_info: types.Implementation,
        extensions: tuple[object, ...],
        cache: None,
    ) -> Client:
        record = _http_builder_record(self)
        broker_state = (
            _BrokerFailureState(record.authorization)
            if type(record.authorization) is McpBrokerAuthorizationConfig
            else None
        )
        boundary = _HttpResponseBoundary(
            endpoint=record.endpoint,
            descriptor=record.descriptor,
            authorization=record.authorization,
            max_bytes=record.max_bytes,
            max_duration_seconds=record.operation_seconds,
            max_redirects=record.max_redirects,
            broker_state=broker_state,
        )
        http_client = httpx2.AsyncClient(
            auth=record.auth,
            headers=_provider_request_headers(record.descriptor),
            verify=True,
            http1=True,
            http2=False,
            timeout=httpx2.Timeout(
                connect=record.connection_seconds,
                read=record.operation_seconds,
                write=record.operation_seconds,
                pool=record.connection_seconds,
            ),
            follow_redirects=True,
            max_redirects=(
                max(record.max_redirects, _AUTH_FLOW_RESPONSE_LIMIT)
                if record.authorization is not None
                else record.max_redirects
            ),
            event_hooks={
                "request": [
                    _HttpRequestBoundary(
                        record.endpoint,
                        record.descriptor,
                        record.authorization,
                    )
                ],
                "response": [boundary],
            },
            transport=record.transport,
            trust_env=False,
        )
        client = Client(
            _owned_streamable_http(record.endpoint, http_client, broker_state),
            mode=mode,
            client_info=client_info,
            extensions=extensions,
            cache=cache,
            read_timeout_seconds=record.operation_seconds,
        )
        if type(record.authorization) is McpBrokerAuthorizationConfig:
            assert broker_state is not None
            return _BrokerFailureClient(client, record.authorization, broker_state)
        return client


@dataclass(frozen=True, slots=True, repr=False)
class _IssuedHttpBuilder:
    builder_ref: weakref.ReferenceType[_HttpSdkClientBuilder]
    endpoint: str
    descriptor: McpConnectionDescriptor
    authorization: McpHttpAuthorization | None
    config: StreamableHttpClientConfig
    auth: httpx2.Auth | None
    transport: httpx2.AsyncBaseTransport | None
    connection_seconds: float
    operation_seconds: float
    max_bytes: int
    max_redirects: int
    config_fingerprint: tuple[int, int, int, int, bool]
    authorization_binding: tuple[object, ...] | None
    fingerprint: tuple[object, ...]


_ISSUED_HTTP_BUILDERS: weakref.WeakKeyDictionary[
    _HttpSdkClientBuilder,
    _IssuedHttpBuilder,
] = weakref.WeakKeyDictionary()


def _http_builder_fingerprint(
    builder: _HttpSdkClientBuilder,
    *,
    config_fingerprint: tuple[int, int, int, int, bool],
    authorization_binding: tuple[object, ...] | None,
) -> tuple[object, ...]:
    return (
        id(builder),
        builder.endpoint,
        sha256_hex(canonical_bytes(builder.descriptor.to_dict())),
        id(builder.authorization),
        id(builder.config),
        builder.connection_seconds,
        builder.operation_seconds,
        builder.max_bytes,
        builder.max_redirects,
        config_fingerprint,
        authorization_binding,
    )


def _issue_http_builder(
    *,
    endpoint: str,
    descriptor: McpConnectionDescriptor,
    authorization: McpHttpAuthorization | None,
    config: StreamableHttpClientConfig,
    config_record: _IssuedHttpConfig,
) -> _HttpSdkClientBuilder:
    authorization_binding = (
        None
        if authorization is None
        else _authorization_transport_binding(authorization, descriptor)
    )
    builder = _HttpSdkClientBuilder(
        endpoint=endpoint,
        descriptor=descriptor,
        authorization=authorization,
        config=config,
        connection_seconds=descriptor.limits.max_connection_duration_ms / 1_000,
        operation_seconds=descriptor.limits.max_operation_duration_ms / 1_000,
        max_bytes=min(
            descriptor.limits.max_discovery_document_bytes,
            descriptor.limits.max_result_document_bytes,
        ),
        max_redirects=descriptor.limits.max_http_redirects,
    )
    fingerprint = _http_builder_fingerprint(
        builder,
        config_fingerprint=config_record.fingerprint,
        authorization_binding=authorization_binding,
    )
    _ISSUED_HTTP_BUILDERS[builder] = _IssuedHttpBuilder(
        builder_ref=weakref.ref(builder),
        endpoint=endpoint,
        descriptor=descriptor,
        authorization=authorization,
        config=config,
        auth=config_record.auth,
        transport=config_record.transport,
        connection_seconds=builder.connection_seconds,
        operation_seconds=builder.operation_seconds,
        max_bytes=builder.max_bytes,
        max_redirects=builder.max_redirects,
        config_fingerprint=config_record.fingerprint,
        authorization_binding=authorization_binding,
        fingerprint=fingerprint,
    )
    return builder


def _http_builder_record(builder: object) -> _IssuedHttpBuilder:
    if type(builder) is not _HttpSdkClientBuilder:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    record = _ISSUED_HTTP_BUILDERS.get(builder)
    if record is None or record.builder_ref() is not builder:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    config_record = _http_config_record(record.config)
    authorization_binding = (
        None
        if record.authorization is None
        else _authorization_transport_binding(record.authorization, record.descriptor)
    )
    if (
        builder.endpoint != record.endpoint
        or builder.descriptor is not record.descriptor
        or builder.authorization is not record.authorization
        or builder.config is not record.config
        or builder.connection_seconds != record.connection_seconds
        or builder.operation_seconds != record.operation_seconds
        or builder.max_bytes != record.max_bytes
        or builder.max_redirects != record.max_redirects
        or config_record.fingerprint != record.config_fingerprint
        or authorization_binding != record.authorization_binding
        or _http_builder_fingerprint(
            builder,
            config_fingerprint=config_record.fingerprint,
            authorization_binding=authorization_binding,
        )
        != record.fingerprint
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return record


def build_streamable_http_transport(
    descriptor: McpConnectionDescriptor,
    http_client: StreamableHttpClientConfig,
) -> SdkClientFactory:
    """Issue one descriptor-bound SDK factory for validated Streamable HTTP."""
    record = _http_config_record(http_client)
    snapshot = _snapshot_descriptor(descriptor)
    endpoint = _canonical_endpoint(snapshot, loopback_test=record.loopback_test)
    authorization: McpHttpAuthorization | None = None
    authorization_revision: str | None = None
    discovery_source = DiscoverySource.LIVE_OBSERVED
    if not record.loopback_test:
        if is_mcp_authorization_config(record.auth):
            authorization = _authorization_descriptor_binding(
                record.auth,
                snapshot,
                endpoint=endpoint,
            )
            authorization_revision = _authorization_context_revision(authorization, snapshot)
            discovery_source = DiscoverySource.LIVE_AUTHENTICATED
        elif record.transport is not None and record.auth is None:
            discovery_source = DiscoverySource.LIVE_AUTHENTICATED
    builder = _issue_http_builder(
        endpoint=endpoint,
        descriptor=snapshot,
        authorization=authorization,
        config=http_client,
        config_record=record,
    )
    return _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER,
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        builder=builder,
        builder_identity=http_client,
        discovery_source=discovery_source,
        descriptor=snapshot,
        authorization_context_revision=authorization_revision,
    )


__all__ = [
    "StreamableHttpClientConfig",
    "build_bounded_http_client",
    "build_bounded_sync_http_client",
    "build_streamable_http_transport",
]
