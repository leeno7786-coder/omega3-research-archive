from __future__ import annotations

import base64
import asyncio
import copy
import gc
import json
import logging
import weakref
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest

import mcp_platform.authorization as authorization_module
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.authorization import (
    McpAuthorizationChannel,
    McpAuthorizationConfig,
    McpAuthorizationPresenter,
    McpAuthorizationRequest,
    McpTokenStore,
    create_mcp_authorization_channel,
)
from mcp_platform.canonical import canonical_bytes
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from plugin_platform.contracts import AppRequirement
from plugin_platform.package_verifier import verify_plugin_package
from tests.plugin_platform.support import build_signed_plugin, ephemeral_publisher_ring


_REDIRECT_URI = "https://omega2ai.com/oauth/callback"
_CLIENT_METADATA_URL = "https://omega2ai.com/oauth/client-metadata.json"
_AUTHORIZATION_CONTEXT_REVISION = "a" * 64
_TEST_AUTHORIZATION_DRIVERS: weakref.WeakKeyDictionary[
    McpAuthorizationChannel,
    tuple[McpAuthorizationPresenter, object],
] = weakref.WeakKeyDictionary()


def _requirement(
    *,
    origin: str = "https://apps.example.com",
    issuer: str = "https://auth.example.com/issuer",
) -> AppRequirement:
    return AppRequirement.from_dict(
        {
            "app_id": "com.example.calendar",
            "publisher_id": "example.publisher",
            "deployment": "remote",
            "provider_id": None,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": ["streamable_http"],
            "remote_origin": origin,
            "authorization_issuer": issuer,
            "authentication": {
                "kind": "oauth_authorization_code_pkce",
                "scopes": ["calendar.read"],
                "secret_fields": [],
            },
            "capabilities": [],
        }
    )


def _write_verified_requirement(root: Path, requirement: AppRequirement) -> AppRequirement:
    private, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(root, private)
    document = json.loads((package / "plugin.json").read_bytes())
    document["kind"] = "mcp_backed"
    document["required_apps"] = [requirement.to_dict()]
    document["entry_points"][0]["required_app_ids"] = [requirement.app_id]
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(private.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    (package / "plugin.json").write_bytes(canonical_bytes(document))
    return verify_plugin_package(package, ring, "3.0.0").manifest.required_apps[0]


def _trusted_source(
    tmp_path: Path, requirement: AppRequirement | None = None
) -> TrustedRemoteAppSource:
    signed = _write_verified_requirement(tmp_path, requirement or _requirement())
    return TrustedRemoteAppSource(requirement=signed)


def _descriptor(requirement: AppRequirement | None = None) -> McpConnectionDescriptor:
    requirement = requirement or _requirement()
    projection = project_declared_capabilities(requirement)
    return McpConnectionDescriptor(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=McpClientDeployment.REMOTE,
        provider_id=requirement.provider_id,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin=requirement.remote_origin,
        authorization_issuer=requirement.authorization_issuer,
        authentication=requirement.authentication,
        declared_capabilities={},
        extension_versions={},
        capability_revision=projection.snapshot.capability_revision,
        limits=McpClientLimits(),
    )


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


class _Presenter:
    async def present_authorization(self, authorization_url: str) -> None:
        del authorization_url

    async def wait_for_callback(self) -> str:
        raise AssertionError("callback was not expected")


class _FlowPresenter:
    def __init__(
        self,
        *,
        callback_base: str = _REDIRECT_URI,
        code: str = "one-use-code",
        state: str | None = None,
        issuer: str = "https://auth.example.com/issuer",
    ) -> None:
        self.authorization_url: str | None = None
        self.callback_base = callback_base
        self.code = code
        self.state = state
        self.issuer = issuer

    async def present_authorization(self, authorization_url: str) -> None:
        self.authorization_url = authorization_url

    async def wait_for_callback(self) -> str:
        assert self.authorization_url is not None
        state = parse_qs(urlsplit(self.authorization_url).query)["state"][0]
        return (
            f"{self.callback_base}?code={self.code}&state={self.state or state}&iss={self.issuer}"
        )


class _SequencedPresenter(_FlowPresenter):
    def __init__(self) -> None:
        super().__init__()
        self.authorization_urls: list[str] = []
        self.callback_count = 0

    async def present_authorization(self, authorization_url: str) -> None:
        await super().present_authorization(authorization_url)
        self.authorization_urls.append(authorization_url)

    async def wait_for_callback(self) -> str:
        assert self.authorization_url is not None
        self.callback_count += 1
        state = parse_qs(urlsplit(self.authorization_url).query)["state"][0]
        return (
            f"{_REDIRECT_URI}?code=code-{self.callback_count}&state={state}"
            "&iss=https://auth.example.com/issuer"
        )


class _MemoryStore:
    def __init__(self) -> None:
        self.tokens: dict[str, tuple[int, object]] = {}
        self.clients: dict[str, object] = {}
        self.codes: set[tuple[str, str]] = set()
        self._token_lock = asyncio.Lock()
        self._token_revisions: dict[str, int] = {}

    async def load_tokens(self, storage_key: str) -> tuple[int, object] | None:
        return self.tokens.get(storage_key)

    async def store_tokens(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int | None:
        async with self._token_lock:
            current = self.tokens.get(storage_key)
            current_revision = None if current is None else current[0]
            if current_revision != expected_revision:
                return None
            revision = self._token_revisions.get(storage_key, -1) + 1
            self.tokens[storage_key] = (revision, tokens)
            self._token_revisions[storage_key] = revision
            return revision

    async def delete_tokens(
        self,
        storage_key: str,
        *,
        expected_revision: int,
    ) -> bool:
        async with self._token_lock:
            current = self.tokens.get(storage_key)
            if current is None or current[0] != expected_revision:
                return False
            del self.tokens[storage_key]
            return True

    async def load_client_info(self, storage_key: str) -> object | None:
        return self.clients.get(storage_key)

    async def store_client_info(self, storage_key: str, client_info: object) -> None:
        self.clients[storage_key] = client_info

    async def claim_authorization_code(self, storage_key: str, code_sha256: str) -> bool:
        identity = (storage_key, code_sha256)
        if identity in self.codes:
            return False
        self.codes.add(identity)
        return True


class _CancellingStore(_MemoryStore):
    def __init__(self, seam: str, cancellation: asyncio.CancelledError) -> None:
        super().__init__()
        self.seam = seam
        self.cancellation = cancellation

    async def load_tokens(self, storage_key: str) -> tuple[int, object] | None:
        if self.seam == "storage_load":
            raise self.cancellation
        return await super().load_tokens(storage_key)

    async def store_client_info(self, storage_key: str, client_info: object) -> None:
        if self.seam == "storage_client_write":
            raise self.cancellation
        await super().store_client_info(storage_key, client_info)

    async def store_tokens(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int | None:
        if self.seam == "storage_token_write":
            raise self.cancellation
        return await super().store_tokens(
            storage_key,
            tokens,
            expected_revision=expected_revision,
        )


class _RecordingStore(_MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.loaded_token_keys: list[str] = []
        self.loaded_client_keys: list[str] = []

    async def load_tokens(self, storage_key: str) -> tuple[int, object] | None:
        self.loaded_token_keys.append(storage_key)
        return await super().load_tokens(storage_key)

    async def load_client_info(self, storage_key: str) -> object | None:
        self.loaded_client_keys.append(storage_key)
        return await super().load_client_info(storage_key)


class _FailingDeleteStore(_MemoryStore):
    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self.failure = failure
        self.delete_calls = 0

    async def delete_tokens(self, storage_key: str, *, expected_revision: int) -> bool:
        del storage_key, expected_revision
        self.delete_calls += 1
        raise self.failure


class _RefreshWriteCancellingStore(_MemoryStore):
    def __init__(self, cancellation: asyncio.CancelledError) -> None:
        super().__init__()
        self.cancellation = cancellation
        self.cancel_next_write = False

    async def store_tokens(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int | None:
        if self.cancel_next_write:
            self.cancel_next_write = False
            raise self.cancellation
        return await super().store_tokens(
            storage_key,
            tokens,
            expected_revision=expected_revision,
        )


class _ExternalWinnerStore(_MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.inject_external_winner = False

    async def store_tokens(
        self,
        storage_key: str,
        tokens: object,
        *,
        expected_revision: int | None,
    ) -> int | None:
        if self.inject_external_winner and expected_revision is not None:
            self.inject_external_winner = False
            current = self.tokens[storage_key]
            revision = current[0] + 1
            self.tokens[storage_key] = (
                revision,
                replace(
                    tokens,
                    access_token="external-winner-access",
                    refresh_token="external-winner-refresh",
                ),
            )
            self._token_revisions[storage_key] = revision
            return None
        return await super().store_tokens(
            storage_key,
            tokens,
            expected_revision=expected_revision,
        )


class _CollisionMap(dict[str, object]):
    def __bool__(self) -> bool:
        return False

    def __contains__(self, key: object) -> bool:
        del key
        return True


def _error_chain_text(error: BaseException) -> str:
    values: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        values.extend((str(current), repr(current), repr(current.args)))
        current = current.__cause__ or current.__context__
    return " ".join(values)


def _sensitive_client_error(marker: str) -> McpClientError:
    try:
        raise RuntimeError(marker)
    except RuntimeError:
        try:
            raise McpClientError(McpClientErrorReason.AUTHORIZATION_INVALID)
        except McpClientError as error:
            return error


class _MockOAuthServers:
    def __init__(self) -> None:
        self.resource_metadata_url = (
            "https://apps.example.com/.well-known/oauth-protected-resource/mcp"
        )
        self.prm_resource = "https://apps.example.com/mcp"
        self.prm_issuers = ["https://auth.example.com/issuer"]
        self.metadata_issuer = "https://auth.example.com/issuer"
        self.authorization_endpoint = "https://auth.example.com/issuer/authorize"
        self.token_endpoint = "https://auth.example.com/issuer/token"
        self.cimd_supported = True
        self.response_types_supported: object = ["code"]
        self.code_challenge_methods_supported: object = ["S256"]
        self.registration_endpoint: str | None = None
        self.token_audience: str | list[str] = "https://apps.example.com/mcp"
        self.token_status = 200
        self.token_body: dict[str, object] = {
            "access_token": "access-value",
            "token_type": "Bearer",
            "expires_in": 60,
            "refresh_token": "refresh-value",
            "scope": "calendar.read",
        }
        self.requests: list[httpx2.Request] = []
        self.accepted_access_tokens = {"access-value"}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == "https://apps.example.com/mcp":
            authorization = request.headers.get("Authorization", "")
            if authorization.removeprefix("Bearer ") in self.accepted_access_tokens:
                return httpx2.Response(200, json={"ok": True})
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{self.resource_metadata_url}", '
                        'scope="calendar.read"'
                    )
                },
            )
        if url == self.resource_metadata_url:
            return httpx2.Response(
                200,
                json={
                    "resource": self.prm_resource,
                    "authorization_servers": self.prm_issuers,
                    "scopes_supported": ["calendar.read"],
                },
            )
        if ".well-known/oauth-authorization-server" in url:
            document: dict[str, object] = {
                "issuer": self.metadata_issuer,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "client_id_metadata_document_supported": self.cimd_supported,
                "code_challenge_methods_supported": self.code_challenge_methods_supported,
                "scopes_supported": ["calendar.read"],
                "response_types_supported": self.response_types_supported,
            }
            if self.registration_endpoint is not None:
                document["registration_endpoint"] = self.registration_endpoint
            return httpx2.Response(200, json=document)
        if url == self.token_endpoint:
            body = dict(self.token_body)
            body["audience"] = self.token_audience
            return httpx2.Response(self.token_status, json=body)
        if url == self.registration_endpoint:
            return httpx2.Response(
                201,
                json={"client_id": "dynamically-issued", "client_secret": "secret-value"},
            )
        raise AssertionError(f"unexpected request: {request.method}")


def _channel_for(presenter: object) -> McpAuthorizationChannel:
    channel, port = create_mcp_authorization_channel()
    _TEST_AUTHORIZATION_DRIVERS[channel] = (port, presenter)
    return channel


def _callback_for(request: McpAuthorizationRequest, code: str) -> str:
    state = parse_qs(urlsplit(request.authorization_url).query, strict_parsing=True)["state"][0]
    return f"{_REDIRECT_URI}?code={code}&state={state}&iss=https://auth.example.com/issuer"


async def _drive_test_authorization(channel: McpAuthorizationChannel) -> None:
    port, presenter = _TEST_AUTHORIZATION_DRIVERS[channel]
    request = await port.receive_authorization_request()
    await presenter.present_authorization(request.authorization_url)  # type: ignore[attr-defined]
    callback_uri = await presenter.wait_for_callback()  # type: ignore[attr-defined]
    await port.submit_authorization_callback(request.request_id, callback_uri)


def _observe_authorization_publications(
    monkeypatch: pytest.MonkeyPatch,
) -> asyncio.Queue[McpAuthorizationRequest]:
    observed: asyncio.Queue[McpAuthorizationRequest] = asyncio.Queue()
    publish = authorization_module._publish_authorization_request

    def capture(*args: object, **kwargs: object) -> object:
        pending = publish(*args, **kwargs)
        observed.put_nowait(pending.request)
        return pending

    monkeypatch.setattr(authorization_module, "_publish_authorization_request", capture)
    return observed


def _queued_authorization_requests(port: McpAuthorizationPresenter) -> tuple[object, ...]:
    state = authorization_module._authorization_port_state(port)
    return tuple(getattr(entry, "request", entry) for entry in state.queue)


async def _request_without_ui(
    config: McpAuthorizationConfig,
    server: object,
) -> httpx2.Response:
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(server),  # type: ignore[arg-type]
        auth=config,
    ) as client:
        return await client.get("https://apps.example.com/mcp")


async def _client_get_with_test_authorization(
    client: httpx2.AsyncClient,
    config: McpAuthorizationConfig,
) -> httpx2.Response:
    channel = config._inputs.authorization_channel
    driver = asyncio.create_task(_drive_test_authorization(channel))
    request_task = asyncio.create_task(client.get("https://apps.example.com/mcp"))
    try:
        done, _pending = await asyncio.wait(
            {driver, request_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if driver in done:
            await driver
        return await request_task
    finally:
        for task in (driver, request_task):
            if not task.done():
                task.cancel()
            with suppress(BaseException):
                await task


async def _send(config: McpAuthorizationConfig, server: object) -> httpx2.Response:
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(server),  # type: ignore[arg-type]
        auth=config,
    ) as client:
        return await _client_get_with_test_authorization(client, config)


def _cimd_config(
    descriptor: McpConnectionDescriptor,
    source: TrustedRemoteAppSource,
    *,
    presenter: McpAuthorizationPresenter | None = None,
    token_store: McpTokenStore | None = None,
) -> McpAuthorizationConfig:
    options: dict[str, Any] = {}
    if token_store is not None:
        options["token_store"] = token_store
    return McpAuthorizationConfig.for_client_metadata_document(
        descriptor,
        source,
        authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
        redirect_uri=_REDIRECT_URI,
        scopes=("calendar.read",),
        presenter=_channel_for(presenter or _Presenter()),
        client_metadata_url=_CLIENT_METADATA_URL,
        **options,
    )


def _cimd_channel_config(
    descriptor: McpConnectionDescriptor,
    source: TrustedRemoteAppSource,
    *,
    token_store: McpTokenStore | None = None,
    authorization_context_revision: str = _AUTHORIZATION_CONTEXT_REVISION,
) -> tuple[McpAuthorizationConfig, McpAuthorizationPresenter]:
    channel, port = create_mcp_authorization_channel()
    options: dict[str, Any] = {}
    if token_store is not None:
        options["token_store"] = token_store
    config = McpAuthorizationConfig.for_client_metadata_document(
        descriptor,
        source,
        authorization_context_revision=authorization_context_revision,
        redirect_uri=_REDIRECT_URI,
        scopes=("calendar.read",),
        presenter=channel,
        client_metadata_url=_CLIENT_METADATA_URL,
        **options,
    )
    return config, port


def test_authorization_boundary_exists() -> None:
    assert McpAuthorizationChannel is not None
    assert McpAuthorizationConfig is not None
    assert McpAuthorizationPresenter is not None
    assert McpAuthorizationRequest is not None
    assert McpTokenStore is not None
    assert create_mcp_authorization_channel is not None


def test_signed_source_and_exact_descriptor_are_validated_before_sdk_auth_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirement = _requirement()
    source = _trusted_source(tmp_path, requirement)
    descriptor = _descriptor(requirement)
    real_provider = authorization_module.OAuthClientProvider
    constructor_calls = 0

    def capture(*args: object, **kwargs: object) -> object:
        nonlocal constructor_calls
        constructor_calls += 1
        return real_provider(*args, **kwargs)

    monkeypatch.setattr(authorization_module, "OAuthClientProvider", capture)

    config = _cimd_config(descriptor, source, token_store=_MemoryStore())

    assert isinstance(config, httpx2.Auth)
    assert constructor_calls == 1
    assert _CLIENT_METADATA_URL not in repr(config)
    assert _REDIRECT_URI not in repr(config)

    forged_source = object.__new__(TrustedRemoteAppSource)
    object.__setattr__(forged_source, "requirement", requirement)
    with pytest.raises(McpClientError) as unsigned:
        _cimd_config(descriptor, forged_source, token_store=_MemoryStore())
    assert unsigned.value.reason is McpClientErrorReason.IDENTITY_MISMATCH

    with pytest.raises(McpClientError) as mismatched:
        _cimd_config(
            _forge_descriptor(descriptor, remote_origin="https://other.example.com"),
            source,
            token_store=_MemoryStore(),
        )
    assert mismatched.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    assert constructor_calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization_context_revision", "not-a-revision"),
        ("redirect_uri", "https://omega2ai.com/oauth/callback?alias=1"),
        ("redirect_uri", "http://omega2ai.com/oauth/callback"),
        ("scopes", ()),
        ("scopes", ("calendar.read", "calendar.read")),
        ("client_metadata_url", "https://omega2ai.com"),
        ("client_metadata_url", "https://omega2ai.com:0/client.json"),
    ],
)
def test_authorization_config_rejects_ambiguous_public_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    requirement = _requirement()
    options: dict[str, object] = {
        "authorization_context_revision": _AUTHORIZATION_CONTEXT_REVISION,
        "redirect_uri": _REDIRECT_URI,
        "scopes": ("calendar.read",),
        "presenter": _channel_for(_Presenter()),
        "client_metadata_url": _CLIENT_METADATA_URL,
        "token_store": _MemoryStore(),
    }
    options[field] = value

    with pytest.raises(McpClientError) as raised:
        McpAuthorizationConfig.for_client_metadata_document(
            _descriptor(requirement),
            _trusted_source(tmp_path, requirement),
            **options,
        )

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID


def test_preregistered_identity_is_exact_public_client_without_secret(tmp_path: Path) -> None:
    requirement = _requirement()
    config = McpAuthorizationConfig.for_preregistered_client(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
        redirect_uri=_REDIRECT_URI,
        scopes=("calendar.read",),
        presenter=_channel_for(_Presenter()),
        client_id="omega-public-client",
        token_store=_MemoryStore(),
    )

    assert isinstance(config, httpx2.Auth)
    assert "omega-public-client" not in repr(config)


def test_desktop_loopback_redirect_is_accepted(tmp_path: Path) -> None:
    requirement = _requirement()
    config = McpAuthorizationConfig.for_preregistered_client(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
        redirect_uri="http://127.0.0.1:13310/oauth/profile.desktop/callback",
        scopes=("calendar.read",),
        presenter=_channel_for(_Presenter()),
        client_id="omega-desktop-client",
        token_store=_MemoryStore(),
    )

    assert config._inputs.redirect_uri == (
        "http://127.0.0.1:13310/oauth/profile.desktop/callback"
    )


def test_preregistered_desktop_secret_is_bound_without_being_repr_or_stored(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    store = _MemoryStore()
    config = McpAuthorizationConfig.for_preregistered_client(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
        redirect_uri=_REDIRECT_URI,
        scopes=("calendar.read",),
        presenter=_channel_for(_Presenter()),
        client_id="omega-desktop-client",
        client_secret="desktop-client-secret",
        token_store=store,
    )

    assert config._inputs.client_secret == "desktop-client-secret"
    assert "desktop-client-secret" not in repr(config)
    assert all("desktop-client-secret" not in repr(value) for value in store.clients.values())


@pytest.mark.parametrize("construction", ["copy", "object_new", "proof_reuse"])
@pytest.mark.anyio
async def test_authorization_config_rejects_copied_or_reused_lifecycle_proof_before_network(
    tmp_path: Path,
    construction: str,
) -> None:
    requirement = _requirement()
    original = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    if construction == "copy":
        substitute = copy.copy(original)
    else:
        substitute = object.__new__(McpAuthorizationConfig)
        object.__setattr__(substitute, "_provider", original._provider)
        object.__setattr__(substitute, "_inputs", original._inputs)
        object.__setattr__(
            substitute,
            "_proof",
            original._proof if construction == "proof_reuse" else object(),
        )
    server = _MockOAuthServers()

    with pytest.raises(McpClientError) as raised:
        await _send(substitute, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert server.requests == []


@pytest.mark.anyio
async def test_authorization_config_rejects_nested_account_mutation_before_network(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    object.__setattr__(config._inputs, "authorization_context_revision", "b" * 64)
    server = _MockOAuthServers()

    with pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert server.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        "callback_handler",
        "redirect_handler",
        "storage",
        "client_metadata_url",
        "client_metadata_scope",
        "server_url",
    ],
)
async def test_authorization_config_rejects_nested_sdk_provider_authority_before_network(
    tmp_path: Path,
    mutation: str,
) -> None:
    class PublicStorage:
        def __init__(self) -> None:
            self.tokens: list[object] = []
            self.clients: list[object] = []

        async def get_tokens(self) -> None:
            return None

        async def set_tokens(self, tokens: object) -> None:
            self.tokens.append(tokens)

        async def get_client_info(self) -> None:
            return None

        async def set_client_info(self, client_info: object) -> None:
            self.clients.append(client_info)

    presenter = _FlowPresenter(code="nested-provider-bypass-code")
    store = _MemoryStore()
    requirement = _requirement()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=presenter,
        token_store=store,
    )
    context = config._provider.context
    public_storage = PublicStorage()

    async def bypass_callback() -> object:
        assert presenter.authorization_url is not None
        state = parse_qs(urlsplit(presenter.authorization_url).query)["state"][0]
        return authorization_module.AuthorizationCodeResult(
            code="nested-provider-bypass-code",
            state=state,
            iss="https://auth.example.com/issuer",
        )

    async def bypass_redirect(_authorization_url: str) -> None:
        return None

    if mutation == "callback_handler":
        context.callback_handler = bypass_callback
    elif mutation == "redirect_handler":
        context.redirect_handler = bypass_redirect
    elif mutation == "storage":
        context.storage = public_storage  # type: ignore[assignment]
    elif mutation == "client_metadata_url":
        context.client_metadata_url = "https://evil.example/client.json"
    elif mutation == "client_metadata_scope":
        context.client_metadata.scope = "admin.write"
    else:
        context.server_url = "https://evil.example/mcp"
    server = _MockOAuthServers()

    with pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert server.requests == []
    assert store.tokens == {}
    assert store.codes == set()
    assert public_storage.tokens == []
    assert public_storage.clients == []


@pytest.mark.anyio
async def test_cimd_flow_binds_exact_metadata_pkce_callback_and_token_exchange(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    presenter = _FlowPresenter()
    store = _MemoryStore()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=presenter,
        token_store=store,
    )
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        url = str(request.url)
        if url == "https://apps.example.com/mcp":
            if request.headers.get("Authorization") == "Bearer access-value":
                return httpx2.Response(200, json={"ok": True})
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
                    "scopes_supported": ["calendar.read"],
                    "response_types_supported": ["code"],
                },
            )
        if url == "https://auth.example.com/issuer/token":
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
            assert form["grant_type"] == ["authorization_code"]
            assert form["client_id"] == [_CLIENT_METADATA_URL]
            assert form["redirect_uri"] == [_REDIRECT_URI]
            assert form["resource"] == ["https://apps.example.com/mcp"]
            assert form["code"] == ["one-use-code"]
            verifier = form["code_verifier"][0]
            assert 43 <= len(verifier) <= 128
            return httpx2.Response(
                200,
                json={
                    "access_token": "access-value",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "refresh_token": "refresh-value",
                    "scope": "calendar.read",
                    "audience": "https://apps.example.com/mcp",
                },
            )
        raise AssertionError(f"unexpected request: {request.method}")

    response = await _send(config, handler)  # type: ignore[arg-type]

    assert response.status_code == 200
    assert presenter.authorization_url is not None
    authorization = urlsplit(presenter.authorization_url)
    query = parse_qs(authorization.query, strict_parsing=True)
    assert authorization._replace(query="").geturl() == (
        "https://auth.example.com/issuer/authorize"
    )
    assert query["client_id"] == [_CLIENT_METADATA_URL]
    assert query["redirect_uri"] == [_REDIRECT_URI]
    assert query["resource"] == ["https://apps.example.com/mcp"]
    assert query["scope"] == ["calendar.read"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert len(store.tokens) == 1
    assert len(store.clients) == 1
    assert len(store.codes) == 1
    assert all("one-use-code" not in key for key, _value in store.codes)
    assert all(type(value).__name__ != "OAuthToken" for value in store.tokens.values())
    assert all(
        type(value).__name__ != "OAuthClientInformationFull" for value in store.clients.values()
    )
    assert [request.method for request in seen] == ["GET", "GET", "GET", "POST", "GET"]


@pytest.mark.anyio
async def test_adapter_rejects_provider_token_exchange_when_pkce_verifier_is_not_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_provider = authorization_module.OAuthClientProvider

    class _MalformedPkceProvider(real_provider):
        def __init__(self, **options: object) -> None:
            super().__init__(**options)  # type: ignore[arg-type]

        async def async_auth_flow(
            self,
            request: httpx2.Request,
        ) -> Any:
            yield request
            yield httpx2.Request(
                "GET",
                "https://apps.example.com/.well-known/oauth-protected-resource/mcp",
            )
            yield httpx2.Request(
                "GET",
                "https://auth.example.com/.well-known/oauth-authorization-server/issuer",
            )
            authorization_url = "https://auth.example.com/issuer/authorize?" + urlencode(
                {
                    "response_type": "code",
                    "client_id": _CLIENT_METADATA_URL,
                    "redirect_uri": _REDIRECT_URI,
                    "state": "provider-state",
                    "code_challenge": "x" * 43,
                    "code_challenge_method": "S256",
                    "resource": "https://apps.example.com/mcp",
                    "scope": "calendar.read",
                }
            )
            assert self.context.redirect_handler is not None
            assert self.context.callback_handler is not None
            await self.context.redirect_handler(authorization_url)
            result = await self.context.callback_handler()
            yield httpx2.Request(
                "POST",
                "https://auth.example.com/issuer/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": _CLIENT_METADATA_URL,
                    "redirect_uri": _REDIRECT_URI,
                    "resource": "https://apps.example.com/mcp",
                    "code": result.code,
                    "code_verifier": "v" * 43,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    monkeypatch.setattr(authorization_module, "OAuthClientProvider", _MalformedPkceProvider)
    requirement = _requirement()
    server = _MockOAuthServers()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )

    with pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert all(request.method != "POST" for request in server.requests)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "resource_metadata_url",
            "https://evil.example/.well-known/oauth-protected-resource/mcp",
        ),
        ("prm_resource", "https://other.example.com/mcp"),
        ("prm_issuers", ["https://evil.example/issuer"]),
        ("metadata_issuer", "https://evil.example/issuer"),
        ("authorization_endpoint", "https://evil.example/authorize"),
        ("token_endpoint", "https://evil.example/token"),
        ("token_audience", "https://other.example.com/mcp"),
    ],
)
async def test_flow_rejects_mismatched_resource_issuer_endpoint_or_audience_before_use(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    setattr(server, field, value)
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )

    with pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert "evil.example" not in str(raised.value)
    assert all(
        request.url.host != "evil.example"
        for request in server.requests
        if request.url.host is not None
    )


@pytest.mark.anyio
async def test_cimd_refusal_never_falls_back_to_dynamic_client_registration(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    server.cimd_supported = False
    server.registration_endpoint = "https://auth.example.com/issuer/register"
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )

    with pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert all(request.method != "POST" for request in server.requests)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "presenter",
    [
        _FlowPresenter(state="wrong-state"),
        _FlowPresenter(issuer="https://evil.example/issuer"),
        _FlowPresenter(callback_base="https://omega2ai.com/oauth/other-callback"),
        _FlowPresenter(callback_base=f"{_REDIRECT_URI}#fragment"),
    ],
)
async def test_callback_requires_exact_uri_state_and_response_issuer(
    tmp_path: Path,
    presenter: _FlowPresenter,
) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=presenter,
        token_store=_MemoryStore(),
    )

    with pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert all(request.method != "POST" for request in server.requests)


def test_google_callback_accepts_authorized_extra_query_parameters() -> None:
    callback = (
        "https://omega2ai.com/oauth/callback?"
        "code=google-code&state=google-state&iss=https%3A%2F%2Faccounts.google.com&"
        "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.readonly&"
        "authuser=0&prompt=consent&hd=example.com"
    )

    result = authorization_module._parse_callback_values(
        _REDIRECT_URI,
        "https://accounts.google.com",
        callback,
    )

    assert result.code == "google-code"
    assert result.state == "google-state"
    assert result.iss == "https://accounts.google.com"


@pytest.mark.parametrize(
    ("issuer_parameter_required", "issuer_query", "expected_issuer"),
    [
        (True, "&iss=https%3A%2F%2Fauth.example.com%2Fissuer", "https://auth.example.com/issuer"),
        (False, "", None),
        (False, "&iss=https%3A%2F%2Fauth.example.com%2Fissuer", "https://auth.example.com/issuer"),
    ],
)
def test_exact_callback_obeys_verified_response_issuer_parameter_support(
    issuer_parameter_required: bool,
    issuer_query: str,
    expected_issuer: str | None,
) -> None:
    callback = f"{_REDIRECT_URI}?code=opaque-code&state=opaque-state{issuer_query}"

    result = authorization_module.parse_exact_callback(
        callback,
        redirect_uri=_REDIRECT_URI,
        expected_issuer="https://auth.example.com/issuer",
        issuer_parameter_required=issuer_parameter_required,
    )

    assert result.code == "opaque-code"
    assert result.state == "opaque-state"
    assert result.iss == expected_issuer


@pytest.mark.parametrize(
    ("issuer_parameter_required", "issuer_query"),
    [
        (True, ""),
        (True, "&iss=https%3A%2F%2Fevil.example"),
        (False, "&iss=https%3A%2F%2Fevil.example"),
        (
            False,
            "&iss=https%3A%2F%2Fauth.example.com%2Fissuer"
            "&iss=https%3A%2F%2Fauth.example.com%2Fissuer",
        ),
    ],
)
def test_exact_callback_rejects_missing_required_or_any_mismatched_issuer(
    issuer_parameter_required: bool,
    issuer_query: str,
) -> None:
    callback = f"{_REDIRECT_URI}?code=opaque-code&state=opaque-state{issuer_query}"

    with pytest.raises(McpClientError) as raised:
        authorization_module.parse_exact_callback(
            callback,
            redirect_uri=_REDIRECT_URI,
            expected_issuer="https://auth.example.com/issuer",
            issuer_parameter_required=issuer_parameter_required,
        )

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID


@pytest.mark.anyio
async def test_state_mismatch_is_rejected_before_sdk_logging_can_expose_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_marker = "state-marker-must-not-leak"
    requirement = _requirement()
    server = _MockOAuthServers()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(state=state_marker),
        token_store=_MemoryStore(),
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert state_marker not in str(raised.value)
    assert state_marker not in repr(raised.value)
    assert state_marker not in caplog.text


@pytest.mark.anyio
async def test_metadata_issuer_mismatch_is_rejected_before_sdk_logging_can_expose_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issuer_marker = "https://issuer-marker.example/issuer"
    requirement = _requirement()
    server = _MockOAuthServers()
    server.metadata_issuer = issuer_marker
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert issuer_marker not in str(raised.value)
    assert issuer_marker not in repr(raised.value)
    assert issuer_marker not in caplog.text


@pytest.mark.anyio
async def test_authorization_code_is_single_use_for_bound_issuer_account_and_descriptor(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    source = _trusted_source(tmp_path, requirement)
    descriptor = _descriptor(requirement)
    store = _MemoryStore()

    first = _cimd_config(
        descriptor,
        source,
        presenter=_FlowPresenter(),
        token_store=store,
    )
    assert (await _send(first, _MockOAuthServers())).status_code == 200
    store.tokens.clear()

    replay = _cimd_config(
        descriptor,
        source,
        presenter=_FlowPresenter(),
        token_store=store,
    )
    second_server = _MockOAuthServers()
    with pytest.raises(McpClientError) as raised:
        await _send(replay, second_server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert all(request.method != "POST" for request in second_server.requests)


@pytest.mark.anyio
async def test_preregistered_flow_uses_exact_public_id_and_never_registers(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    server.cimd_supported = False
    server.registration_endpoint = "https://auth.example.com/issuer/register"
    config = McpAuthorizationConfig.for_preregistered_client(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
        redirect_uri=_REDIRECT_URI,
        scopes=("calendar.read",),
        presenter=_channel_for(_FlowPresenter()),
        client_id="omega-public-client",
        token_store=_MemoryStore(),
    )

    response = await _send(config, server)

    assert response.status_code == 200
    token_request = next(
        request for request in server.requests if str(request.url) == server.token_endpoint
    )
    form = parse_qs(token_request.content.decode("ascii"), strict_parsing=True)
    assert form["client_id"] == ["omega-public-client"]
    assert all(str(request.url) != server.registration_endpoint for request in server.requests)


@pytest.mark.anyio
async def test_preregistered_desktop_secret_is_sent_only_to_token_endpoint(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    server.cimd_supported = False
    server.registration_endpoint = "https://auth.example.com/issuer/register"
    config = McpAuthorizationConfig.for_preregistered_client(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
        redirect_uri=_REDIRECT_URI,
        scopes=("calendar.read",),
        presenter=_channel_for(_FlowPresenter()),
        client_id="omega-desktop-client",
        client_secret="desktop-client-secret",
        token_store=_MemoryStore(),
    )

    assert (await _send(config, server)).status_code == 200
    token_request = next(
        request for request in server.requests if str(request.url) == server.token_endpoint
    )
    form = parse_qs(token_request.content.decode("ascii"), strict_parsing=True)
    assert form["client_secret"] == ["desktop-client-secret"]
    assert all(
        "desktop-client-secret" not in request.content.decode("ascii")
        for request in server.requests
        if str(request.url) != server.token_endpoint
    )


@pytest.mark.anyio
async def test_default_store_fails_closed_before_network(tmp_path: Path) -> None:
    requirement = _requirement()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
    )
    server = _MockOAuthServers()

    with pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_STORE_UNAVAILABLE
    assert server.requests == []


@pytest.mark.anyio
async def test_failed_exchange_is_content_free_and_redacted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_markers = (
        "one-use-code",
        "leaked-access-token",
        "leaked-refresh-token",
        "leaked-client-secret",
    )
    requirement = _requirement()
    server = _MockOAuthServers()
    server.token_status = 400
    server.token_body = {
        "error": "invalid_grant",
        "error_description": " ".join(secret_markers),
    }
    store = _MemoryStore()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=store,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    exposed = " ".join(
        [str(raised.value), repr(raised.value), repr(config), caplog.text]
        + [repr(value) for value in store.tokens.values()]
        + [repr(value) for value in store.clients.values()]
    )
    assert all(marker not in exposed for marker in secret_markers)


@pytest.mark.anyio
async def test_malformed_remote_body_is_absent_from_recursive_error_context_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "malformed-body-capability-marker"
    requirement = _requirement()
    server = _MockOAuthServers()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == server.token_endpoint:
            return httpx2.Response(400, content=f'{{"error":"{marker}"'.encode())
        return server(request)

    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(McpClientError) as raised:
        await _send(config, handler)  # type: ignore[arg-type]

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert marker not in _error_chain_text(raised.value)
    assert marker not in caplog.text


@pytest.mark.anyio
async def test_authorization_port_rejects_sensitive_callback_before_sdk_logging(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "callback-capability-marker"
    requirement = _requirement()
    config, port = _cimd_channel_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    request = await port.receive_authorization_request()
    with caplog.at_level(logging.DEBUG), pytest.raises(McpClientError) as raised:
        await port.submit_authorization_callback(
            request.request_id,
            f"{_REDIRECT_URI}?code=code&state={marker}&iss=https://auth.example.com/issuer",
        )

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert marker not in _error_chain_text(raised.value)
    assert marker not in caplog.text
    await port.close()
    with pytest.raises(McpClientError):
        await request_task


@pytest.mark.anyio
@pytest.mark.parametrize("seam", ["redirect", "callback"])
async def test_structural_sync_presenter_is_rejected_before_provider_or_network(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    marker = f"sync-presenter-capability-marker-{seam}"
    sensitive = _sensitive_client_error(marker)
    provider_calls = 0
    real_provider = authorization_module.OAuthClientProvider

    class Presenter:
        def present_authorization(self, authorization_url: str) -> object:
            del authorization_url
            if seam == "redirect":
                raise sensitive

            async def complete() -> None:
                return None

            return complete()

        def wait_for_callback(self) -> str:
            raise sensitive

    def capture_provider(*args: object, **kwargs: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return real_provider(*args, **kwargs)

    monkeypatch.setattr(authorization_module, "OAuthClientProvider", capture_provider)
    requirement = _requirement()
    server = _MockOAuthServers()
    with caplog.at_level(logging.DEBUG), pytest.raises(McpClientError) as raised:
        config = McpAuthorizationConfig.for_client_metadata_document(
            _descriptor(requirement),
            _trusted_source(tmp_path, requirement),
            authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
            redirect_uri=_REDIRECT_URI,
            scopes=("calendar.read",),
            presenter=Presenter(),  # type: ignore[arg-type]
            client_metadata_url=_CLIENT_METADATA_URL,
            token_store=_MemoryStore(),
        )
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert marker not in _error_chain_text(raised.value)
    assert marker not in caplog.text
    assert provider_calls == 0
    assert server.requests == []


@pytest.mark.anyio
async def test_authorization_channel_wait_is_connection_bounded_and_releases_owner(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    descriptor = _forge_descriptor(
        _descriptor(requirement),
        limits=McpClientLimits(max_connection_duration_ms=20),
    )
    source = _trusted_source(tmp_path, requirement)
    store = _MemoryStore()
    stalled, port = _cimd_channel_config(
        descriptor,
        source,
        token_store=store,
    )
    request_task = asyncio.create_task(_request_without_ui(stalled, _MockOAuthServers()))
    request = await port.receive_authorization_request()

    with pytest.raises(McpClientError) as raised:
        await request_task

    assert raised.value.reason is McpClientErrorReason.REQUEST_TIMEOUT
    with pytest.raises(McpClientError) as late:
        await port.submit_authorization_callback(
            request.request_id,
            f"{_REDIRECT_URI}?code=late&state=late&iss=https://auth.example.com/issuer",
        )
    assert late.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID

    recovered = _cimd_config(
        descriptor,
        source,
        presenter=_FlowPresenter(code="after-timeout"),
        token_store=store,
    )
    assert (await _send(recovered, _MockOAuthServers())).status_code == 200


@pytest.mark.anyio
async def test_authorization_timeout_spawns_no_presenter_task_or_survivor(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    descriptor = _forge_descriptor(
        _descriptor(requirement),
        limits=McpClientLimits(max_connection_duration_ms=10),
    )
    config, port = _cimd_channel_config(
        descriptor,
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    before = set(asyncio.all_tasks())
    request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    await port.receive_authorization_request()

    with pytest.raises(McpClientError) as raised:
        await request_task

    assert raised.value.reason is McpClientErrorReason.REQUEST_TIMEOUT
    assert set(asyncio.all_tasks()) <= before | {asyncio.current_task()}


@pytest.mark.anyio
async def test_authorization_timeout_before_ui_receive_leaves_a_bounded_empty_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _observe_authorization_publications(monkeypatch)
    requirement = _requirement()
    descriptor = _forge_descriptor(
        _descriptor(requirement),
        limits=McpClientLimits(max_connection_duration_ms=10),
    )
    config, port = _cimd_channel_config(
        descriptor,
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    stale_request = await asyncio.wait_for(observed.get(), timeout=1)

    with pytest.raises(McpClientError) as timed_out:
        await request_task
    assert timed_out.value.reason is McpClientErrorReason.REQUEST_TIMEOUT
    assert all(queued is not stale_request for queued in _queued_authorization_requests(port))
    with pytest.raises(McpClientError) as late:
        await port.submit_authorization_callback(
            stale_request.request_id,
            _callback_for(stale_request, "late-timeout-code"),
        )
    assert late.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID

    with pytest.raises(McpClientError) as empty:
        await asyncio.wait_for(port.receive_authorization_request(), timeout=1)
    assert empty.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID

    recovered_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    current_request = await asyncio.wait_for(observed.get(), timeout=1)
    received = await asyncio.wait_for(port.receive_authorization_request(), timeout=1)
    assert received is current_request
    await port.submit_authorization_callback(
        received.request_id,
        _callback_for(received, "after-timeout-before-receive"),
    )
    assert (await recovered_task).status_code == 200


@pytest.mark.anyio
async def test_authorization_cancellation_before_ui_receive_retires_stale_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _observe_authorization_publications(monkeypatch)
    requirement = _requirement()
    config, port = _cimd_channel_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    stale_requests: list[McpAuthorizationRequest] = []

    for index in range(2):
        request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
        stale_requests.append(await asyncio.wait_for(observed.get(), timeout=1))
        request_task.cancel(f"first-before-receive-{index}")
        request_task.cancel(f"second-before-receive-{index}")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await request_task
        assert cancelled.value.args == (f"first-before-receive-{index}",)

    queued = _queued_authorization_requests(port)
    assert all(stale is not queued_request for stale in stale_requests for queued_request in queued)

    for index, stale_request in enumerate(stale_requests):
        with pytest.raises(McpClientError) as late:
            await port.submit_authorization_callback(
                stale_request.request_id,
                _callback_for(stale_request, f"late-cancel-code-{index}"),
            )
        assert late.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID

    with pytest.raises(McpClientError) as empty:
        await asyncio.wait_for(port.receive_authorization_request(), timeout=1)
    assert empty.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID

    recovered_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    current_request = await asyncio.wait_for(observed.get(), timeout=1)
    received = await asyncio.wait_for(port.receive_authorization_request(), timeout=1)
    assert received is current_request
    await port.submit_authorization_callback(
        received.request_id,
        _callback_for(received, "after-cancel-before-receive"),
    )
    assert (await recovered_task).status_code == 200


@pytest.mark.anyio
async def test_repeated_authorization_wait_cancellation_preserves_first_identity_and_storage(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    store = _MemoryStore()
    config, port = _cimd_channel_config(
        descriptor,
        source,
        token_store=store,
    )
    request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    request = await port.receive_authorization_request()
    request_task.cancel("first-authorization-cancel")
    request_task.cancel("second-authorization-cancel")

    with pytest.raises(asyncio.CancelledError) as raised:
        await request_task

    assert raised.value.args == ("first-authorization-cancel",)
    assert store.tokens == {}
    with pytest.raises(McpClientError):
        await port.submit_authorization_callback(
            request.request_id,
            f"{_REDIRECT_URI}?code=late&state=late&iss=https://auth.example.com/issuer",
        )
    recovered = _cimd_config(
        descriptor,
        source,
        presenter=_FlowPresenter(code="after-repeated-cancel"),
        token_store=store,
    )
    assert (await _send(recovered, _MockOAuthServers())).status_code == 200


@pytest.mark.anyio
async def test_authorization_port_rejects_wrong_late_and_duplicate_request_ids(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    config, port = _cimd_channel_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    request = await port.receive_authorization_request()

    with pytest.raises(McpClientError) as wrong:
        await port.submit_authorization_callback(
            "wrong-request-id",
            _callback_for(request, "wrong-id-code"),
        )
    assert wrong.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    await port.submit_authorization_callback(
        request.request_id,
        _callback_for(request, "channel-success-code"),
    )
    assert (await request_task).status_code == 200
    with pytest.raises(McpClientError) as duplicate:
        await port.submit_authorization_callback(
            request.request_id,
            _callback_for(request, "duplicate-code"),
        )
    assert duplicate.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID


@pytest.mark.anyio
async def test_authorization_request_id_collision_exhaustion_is_bounded_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "collision-private-request-id"
    calls = 0

    def collide(_bytes: int) -> str:
        nonlocal calls
        calls += 1
        if calls > 8:
            raise AssertionError("request id generation was not bounded")
        return marker

    monkeypatch.setattr(authorization_module.secrets, "token_urlsafe", collide)
    state = authorization_module._AuthorizationChannelState(
        proof=object(),
        queue=deque(),
        queue_changed=asyncio.Event(),
        pending=_CollisionMap(),  # type: ignore[arg-type]
        bound=True,
        redirect_uri=_REDIRECT_URI,
        issuer="https://auth.example.com/issuer",
    )

    with pytest.raises(McpClientError) as raised:
        authorization_module._publish_authorization_request(
            state,
            "https://auth.example.com/issuer/authorize?state=bound-state",
            1.0,
        )

    assert calls == 8
    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert marker not in _error_chain_text(raised.value)


@pytest.mark.anyio
async def test_authorization_port_close_revokes_pending_request(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    config, port = _cimd_channel_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    request = await port.receive_authorization_request()
    await port.close()

    with pytest.raises(McpClientError) as raised:
        await request_task
    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    with pytest.raises(McpClientError):
        await port.submit_authorization_callback(
            request.request_id,
            _callback_for(request, "closed-port-code"),
        )


@pytest.mark.anyio
async def test_authorization_port_gc_revokes_pending_request(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    config, port = _cimd_channel_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    await port.receive_authorization_request()
    port_ref = weakref.ref(port)  # type: ignore[arg-type]
    del port
    gc.collect()

    assert port_ref() is None
    with pytest.raises(McpClientError) as raised:
        await asyncio.wait_for(request_task, timeout=1)
    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID


@pytest.mark.anyio
@pytest.mark.parametrize("termination", ["close", "timeout", "cancel"])
async def test_concurrent_authorization_receivers_are_bounded_without_survivors(
    tmp_path: Path,
    termination: str,
) -> None:
    requirement = _requirement()
    descriptor = _forge_descriptor(
        _descriptor(requirement),
        limits=McpClientLimits(max_connection_duration_ms=20),
    )
    config, port = _cimd_channel_config(
        descriptor,
        _trusted_source(tmp_path, requirement),
        token_store=_MemoryStore(),
    )
    receivers = [asyncio.create_task(port.receive_authorization_request()) for _ in range(8)]
    request_task: asyncio.Task[httpx2.Response] | None = None
    request: McpAuthorizationRequest | None = None
    try:
        await asyncio.sleep(0)
        if termination == "close":
            await port.close()
        else:
            request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
            while request is None:
                await asyncio.sleep(0)
                for receiver in receivers:
                    if receiver.done() and not receiver.cancelled():
                        with suppress(McpClientError):
                            request = receiver.result()
            if termination == "cancel":
                request_task.cancel("first-channel-cancel")
                request_task.cancel("second-channel-cancel")
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await request_task
                assert cancelled.value.args == ("first-channel-cancel",)
            else:
                with pytest.raises(McpClientError) as timed_out:
                    await request_task
                assert timed_out.value.reason is McpClientErrorReason.REQUEST_TIMEOUT

        done, pending = await asyncio.wait(receivers, timeout=0.05)
        assert pending == set()
        results = await asyncio.gather(*done, return_exceptions=True)
        requests = [result for result in results if type(result) is McpAuthorizationRequest]
        failures = [result for result in results if isinstance(result, McpClientError)]
        assert len(requests) == (0 if termination == "close" else 1)
        assert len(failures) == 8 - len(requests)
        assert all(
            failure.reason is McpClientErrorReason.AUTHORIZATION_INVALID
            and failure.__context__ is None
            and failure.__cause__ is None
            for failure in failures
        )
        if request is not None:
            with pytest.raises(McpClientError) as late:
                await port.submit_authorization_callback(
                    request.request_id,
                    _callback_for(request, "late-concurrent-code"),
                )
            assert late.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    finally:
        for task in receivers:
            if not task.done():
                task.cancel()
            with suppress(BaseException):
                await task
        if request_task is not None and not request_task.done():
            request_task.cancel()
            with suppress(BaseException):
                await request_task
        with suppress(McpClientError):
            await port.close()


@pytest.mark.anyio
async def test_distinct_authorization_channels_isolate_concurrent_requests(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    first, first_port = _cimd_channel_config(
        descriptor,
        source,
        token_store=_MemoryStore(),
    )
    second, second_port = _cimd_channel_config(
        descriptor,
        source,
        token_store=_MemoryStore(),
        authorization_context_revision="b" * 64,
    )
    first_task = asyncio.create_task(_request_without_ui(first, _MockOAuthServers()))
    second_task = asyncio.create_task(_request_without_ui(second, _MockOAuthServers()))
    first_request, second_request = await asyncio.gather(
        first_port.receive_authorization_request(),
        second_port.receive_authorization_request(),
    )

    assert first_request.request_id != second_request.request_id
    with pytest.raises(McpClientError):
        await first_port.submit_authorization_callback(
            second_request.request_id,
            _callback_for(second_request, "cross-channel-code"),
        )
    await asyncio.gather(
        first_port.submit_authorization_callback(
            first_request.request_id,
            _callback_for(first_request, "first-channel-code"),
        ),
        second_port.submit_authorization_callback(
            second_request.request_id,
            _callback_for(second_request, "second-channel-code"),
        ),
    )
    first_response, second_response = await asyncio.gather(first_task, second_task)
    assert first_response.status_code == second_response.status_code == 200


@pytest.mark.anyio
async def test_authorization_channel_and_port_are_redacted_and_unforgeable(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    channel, port = create_mcp_authorization_channel()
    forged = copy.copy(channel)
    with pytest.raises(McpClientError) as rejected:
        McpAuthorizationConfig.for_client_metadata_document(
            _descriptor(requirement),
            _trusted_source(tmp_path, requirement),
            authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
            redirect_uri=_REDIRECT_URI,
            scopes=("calendar.read",),
            presenter=forged,
            client_metadata_url=_CLIENT_METADATA_URL,
            token_store=_MemoryStore(),
        )
    assert rejected.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID

    config = McpAuthorizationConfig.for_client_metadata_document(
        _descriptor(requirement),
        _trusted_source(tmp_path / "valid", requirement),
        authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
        redirect_uri=_REDIRECT_URI,
        scopes=("calendar.read",),
        presenter=channel,
        client_metadata_url=_CLIENT_METADATA_URL,
        token_store=_MemoryStore(),
    )
    request_task = asyncio.create_task(_request_without_ui(config, _MockOAuthServers()))
    request = await port.receive_authorization_request()
    exposed = " ".join((repr(channel), repr(port), repr(request)))
    assert request.authorization_url not in exposed
    assert request.request_id not in exposed
    assert all(marker not in exposed for marker in ("state=", "code_challenge="))
    await port.close()
    with pytest.raises(McpClientError):
        await request_task


@pytest.mark.anyio
async def test_token_scope_must_cover_the_requested_scope(tmp_path: Path) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    server.token_body["scope"] = "unrelated.read"
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )

    with pytest.raises(McpClientError) as raised:
        await _send(config, server)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert len([request for request in server.requests if request.method == "POST"]) == 1


@pytest.mark.anyio
async def test_discovered_provider_scope_cannot_expand_or_omit_selected_scope(
    tmp_path: Path,
) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    original = server.__call__

    def handler(request: httpx2.Request) -> httpx2.Response:
        response = original(request)
        if str(request.url) == server.resource_metadata_url:
            return httpx2.Response(
                200,
                json={
                    "resource": server.prm_resource,
                    "authorization_servers": server.prm_issuers,
                    "scopes_supported": ["calendar.write"],
                },
                request=request,
            )
        return response

    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )

    with pytest.raises(McpClientError) as raised:
        await _send(config, handler)

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID


@pytest.mark.anyio
@pytest.mark.parametrize(
    "seam",
    [
        "exchange",
        "storage_load",
        "storage_client_write",
        "storage_token_write",
    ],
)
async def test_cancellation_identity_is_preserved_at_authorization_seams(
    tmp_path: Path,
    seam: str,
) -> None:
    cancellation = asyncio.CancelledError(f"cancel-{seam}")
    requirement = _requirement()
    token_store: McpTokenStore = (
        _CancellingStore(seam, cancellation) if seam.startswith("storage_") else _MemoryStore()
    )
    server = _MockOAuthServers()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seam == "exchange" and str(request.url) == server.token_endpoint:
            raise cancellation
        return server(request)

    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=token_store,
    )
    with pytest.raises(asyncio.CancelledError) as raised:
        await _send(config, handler)  # type: ignore[arg-type]

    assert raised.value is cancellation
    assert authorization_module._AUTHORIZATION_OWNER_LOCKS.get(asyncio.get_running_loop(), {}) == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response_types", "challenge_methods", "accepted"),
    [
        (["token", "code"], ["plain", "S256"], True),
        (["code", "code"], ["S256"], False),
        (["code"], ["S256", "S256"], False),
        (["token"], ["S256"], False),
        (["code"], ["plain"], False),
        ("code", ["S256"], False),
        (["code", 1], ["S256"], False),
    ],
)
async def test_oauth_metadata_accepts_only_typed_unique_required_supersets(
    tmp_path: Path,
    response_types: object,
    challenge_methods: object,
    accepted: bool,
) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    server.response_types_supported = response_types
    server.code_challenge_methods_supported = challenge_methods
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )

    if accepted:
        assert (await _send(config, server)).status_code == 200
    else:
        with pytest.raises(McpClientError) as raised:
            await _send(config, server)
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID


@pytest.mark.anyio
async def test_injected_authorization_clock_and_secure_randomness_drive_expiry_and_request_id(
    tmp_path: Path,
) -> None:
    class _Clock:
        def monotonic(self) -> float:
            return 10.0

        def wall_time(self) -> float:
            return 500.0

    class _Randomness:
        def token_urlsafe(self, nbytes: int) -> str:
            assert nbytes == 32
            return "deterministic-request-id"

    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    store = _MemoryStore()
    channel, port = create_mcp_authorization_channel(
        clock=_Clock(),
        randomness=_Randomness(),
    )
    presenter = _FlowPresenter()
    _TEST_AUTHORIZATION_DRIVERS[channel] = (port, presenter)
    config = McpAuthorizationConfig.for_client_metadata_document(
        descriptor,
        source,
        authorization_context_revision=_AUTHORIZATION_CONTEXT_REVISION,
        redirect_uri=_REDIRECT_URI,
        scopes=("calendar.read",),
        presenter=channel,
        client_metadata_url=_CLIENT_METADATA_URL,
        token_store=store,
    )
    server = _MockOAuthServers()
    with pytest.MonkeyPatch.context() as patch:
        observed = _observe_authorization_publications(patch)
        assert (await _send(config, server)).status_code == 200
        request = observed.get_nowait()
    assert request.request_id == "deterministic-request-id"
    assert request.expires_at == 130.0
    stored = next(iter(store.tokens.values()))[1]
    assert stored.expires_at == 560.0


@pytest.mark.anyio
async def test_authorization_cleanup_cannot_replace_exact_active_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = asyncio.CancelledError("active-authorization-cancel")
    cleanup_error = RuntimeError("cleanup-must-not-replace")
    requirement = _requirement()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_Presenter(),
        token_store=_MemoryStore(),
    )

    class _Flow:
        def __aiter__(self) -> _Flow:
            return self

        async def __anext__(self) -> httpx2.Request:
            raise cancellation

        async def asend(self, _value: object) -> httpx2.Request:
            raise AssertionError

        async def aclose(self) -> None:
            raise cleanup_error

    class _Storage:
        def _guard(self) -> None:
            return None

    class _Provider:
        def async_auth_flow(self, _request: httpx2.Request) -> _Flow:
            return _Flow()

    class _Bridge:
        def close(self) -> None:
            return None

    bundle = type(
        "Bundle",
        (),
        {"storage": _Storage(), "provider": _Provider(), "bridge": _Bridge()},
    )()
    monkeypatch.setattr(authorization_module, "_new_provider_bundle", lambda *_args: bundle)
    flow = config.async_auth_flow(httpx2.Request("GET", "https://apps.example.com/mcp"))
    with pytest.raises(asyncio.CancelledError) as raised:
        await anext(flow)
    assert raised.value is cancellation
    assert authorization_module._AUTHORIZATION_OWNER_LOCKS.get(asyncio.get_running_loop(), {}) == {}


def test_signed_nondefault_https_ports_bind_oauth_resource_and_issuer(
    tmp_path: Path,
) -> None:
    requirement = _requirement(
        origin="https://apps.example.com:8443",
        issuer="https://auth.example.com:9443/issuer",
    )
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_Presenter(),
    )

    assert config._inputs.resource_url == "https://apps.example.com:8443/mcp"
    assert config._inputs.issuer == "https://auth.example.com:9443/issuer"
    assert (
        authorization_module._canonical_bound_endpoint(
            "https://auth.example.com:9443/issuer/token",
            config._inputs.issuer,
        )
        == "https://auth.example.com:9443/issuer/token"
    )


@pytest.mark.anyio
async def test_client_and_token_storage_keys_bind_issuer_account_and_descriptor(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()

    async def initialize(config: McpAuthorizationConfig) -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _request: httpx2.Response(200, json={"ok": True})
            ),
            auth=config,
        ) as client:
            assert (await _client_get_with_test_authorization(client, config)).status_code == 200

    first_requirement = _requirement()
    first_descriptor = _descriptor(first_requirement)
    await initialize(
        _cimd_config(
            first_descriptor,
            _trusted_source(tmp_path / "first", first_requirement),
            token_store=store,
        )
    )
    await initialize(
        McpAuthorizationConfig.for_client_metadata_document(
            first_descriptor,
            _trusted_source(tmp_path / "second", first_requirement),
            authorization_context_revision="b" * 64,
            redirect_uri=_REDIRECT_URI,
            scopes=("calendar.read",),
            presenter=_channel_for(_Presenter()),
            client_metadata_url=_CLIENT_METADATA_URL,
            token_store=store,
        )
    )
    second_requirement = _requirement(issuer="https://auth.example.com/other-issuer")
    await initialize(
        _cimd_config(
            _descriptor(second_requirement),
            _trusted_source(tmp_path / "third", second_requirement),
            token_store=store,
        )
    )

    assert len(store.loaded_token_keys) == 3
    assert len(store.loaded_client_keys) == 3
    assert len(set(store.loaded_token_keys)) == 3
    assert len(set(store.loaded_client_keys)) == 3
    assert set(store.loaded_token_keys).isdisjoint(store.loaded_client_keys)
    assert all(len(key) == 64 for key in store.loaded_token_keys + store.loaded_client_keys)
    assert all(
        marker not in key
        for key in store.loaded_token_keys + store.loaded_client_keys
        for marker in ("auth.example.com", "calendar", "omega2ai")
    )


@pytest.mark.anyio
async def test_expiry_refresh_rotation_and_omitted_rotation_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(authorization_module.time, "time", lambda: now[0])
    requirement = _requirement()
    server = _MockOAuthServers()
    server.token_body["expires_in"] = 10
    store = _MemoryStore()
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=store,
    )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(server),
        auth=config,
    ) as client:
        assert (await _client_get_with_test_authorization(client, config)).status_code == 200

        now[0] += 11
        server.token_body = {
            "access_token": "access-two",
            "token_type": "Bearer",
            "expires_in": 10,
            "refresh_token": "refresh-two",
            "scope": "calendar.read",
        }
        server.accepted_access_tokens.add("access-two")
        assert (await _client_get_with_test_authorization(client, config)).status_code == 200

        now[0] += 11
        server.token_body = {
            "access_token": "access-three",
            "token_type": "Bearer",
            "expires_in": 10,
            "scope": "calendar.read",
        }
        server.accepted_access_tokens.add("access-three")
        assert (await _client_get_with_test_authorization(client, config)).status_code == 200

        now[0] += 11
        server.token_body = {
            "access_token": "access-four",
            "token_type": "Bearer",
            "expires_in": 10,
            "refresh_token": "refresh-four",
            "scope": "calendar.read",
        }
        server.accepted_access_tokens.add("access-four")
        assert (await _client_get_with_test_authorization(client, config)).status_code == 200

    token_requests = [
        request for request in server.requests if str(request.url) == server.token_endpoint
    ]
    grants = [
        parse_qs(request.content.decode("ascii"), strict_parsing=True) for request in token_requests
    ]
    assert grants[0]["grant_type"] == ["authorization_code"]
    assert grants[1]["refresh_token"] == ["refresh-value"]
    assert grants[2]["refresh_token"] == ["refresh-two"]
    assert grants[3]["refresh_token"] == ["refresh-two"]
    assert all("refresh" not in repr(value) for value in store.tokens.values())


@pytest.mark.anyio
async def test_scope_step_up_cannot_expand_the_selected_scope_ceiling(tmp_path: Path) -> None:
    requirement = _requirement()
    server = _MockOAuthServers()
    presenter = _SequencedPresenter()
    store = _MemoryStore()
    token_count = 0
    access_one_count = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal token_count, access_one_count
        if str(request.url) == server.token_endpoint:
            token_count += 1
            if token_count == 1:
                server.token_body = {
                    "access_token": "access-one",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "refresh_token": "refresh-one",
                    "scope": "calendar.read",
                }
                server.accepted_access_tokens.add("access-one")
            else:
                server.token_body = {
                    "access_token": "access-two",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "refresh_token": "refresh-two",
                    "scope": "calendar.read calendar.write",
                }
                server.accepted_access_tokens.add("access-two")
        if (
            str(request.url) == "https://apps.example.com/mcp"
            and request.headers.get("Authorization") == "Bearer access-one"
        ):
            access_one_count += 1
            if access_one_count == 2:
                server.requests.append(request)
                return httpx2.Response(
                    403,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer error="insufficient_scope", scope="calendar.write"'
                        )
                    },
                )
        return server(request)

    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=presenter,
        token_store=store,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        auth=config,
    ) as client:
        assert (await _client_get_with_test_authorization(client, config)).status_code == 200
        with pytest.raises(McpClientError) as exc_info:
            await _client_get_with_test_authorization(client, config)

    assert exc_info.value.reason == McpClientErrorReason.AUTHORIZATION_INVALID
    assert len(presenter.authorization_urls) == 1
    first_scope = parse_qs(urlsplit(presenter.authorization_urls[0]).query)["scope"][0]
    assert first_scope == "calendar.read"
    assert len(store.codes) == 1


@pytest.mark.anyio
async def test_refresh_cancellation_identity_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(authorization_module.time, "time", lambda: now[0])
    cancellation = asyncio.CancelledError("cancel-refresh")
    requirement = _requirement()
    server = _MockOAuthServers()
    server.token_body["expires_in"] = 10
    config = _cimd_config(
        _descriptor(requirement),
        _trusted_source(tmp_path, requirement),
        presenter=_FlowPresenter(),
        token_store=_MemoryStore(),
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == server.token_endpoint:
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
            if form["grant_type"] == ["refresh_token"]:
                raise cancellation
        return server(request)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        auth=config,
    ) as client:
        assert (await _client_get_with_test_authorization(client, config)).status_code == 200
        now[0] += 11
        with pytest.raises(asyncio.CancelledError) as raised:
            await _client_get_with_test_authorization(client, config)

    assert raised.value is cancellation


@pytest.mark.anyio
async def test_expired_persisted_token_refreshes_without_interactive_reauthorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(authorization_module.time, "time", lambda: now[0])
    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    store = _MemoryStore()
    initial_server = _MockOAuthServers()
    initial_server.token_body["expires_in"] = 10
    initial = _cimd_config(
        descriptor,
        source,
        presenter=_FlowPresenter(),
        token_store=store,
    )
    assert (await _send(initial, initial_server)).status_code == 200

    now[0] += 11
    presenter = _Presenter()
    restarted = _cimd_config(
        descriptor,
        source,
        presenter=presenter,
        token_store=store,
    )
    refresh_server = _MockOAuthServers()
    refresh_server.token_body = {
        "access_token": "access-after-restart",
        "token_type": "Bearer",
        "expires_in": 60,
        "refresh_token": "refresh-after-restart",
        "scope": "calendar.read",
    }
    refresh_server.accepted_access_tokens.add("access-after-restart")

    response = await _send(restarted, refresh_server)

    assert response.status_code == 200
    assert [request.method for request in refresh_server.requests] == ["POST", "GET"]
    refresh_form = parse_qs(
        refresh_server.requests[0].content.decode("ascii"),
        strict_parsing=True,
    )
    assert refresh_form["grant_type"] == ["refresh_token"]
    assert refresh_form["refresh_token"] == ["refresh-value"]


@pytest.mark.anyio
async def test_rejected_refresh_is_atomically_invalidated_then_interactive_fallback_runs_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(authorization_module.time, "time", lambda: now[0])
    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    store = _MemoryStore()
    initial_server = _MockOAuthServers()
    initial_server.token_body["expires_in"] = 10
    assert (
        await _send(
            _cimd_config(
                descriptor,
                source,
                presenter=_FlowPresenter(code="initial-code"),
                token_store=store,
            ),
            initial_server,
        )
    ).status_code == 200

    now[0] += 11
    presenter = _SequencedPresenter()
    server = _MockOAuthServers()
    refresh_attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal refresh_attempts
        if str(request.url) == server.token_endpoint:
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
            if form["grant_type"] == ["refresh_token"]:
                refresh_attempts += 1
                return httpx2.Response(
                    400,
                    json={"error": "invalid_grant", "error_description": "stale-marker"},
                )
            server.token_body = {
                "access_token": "fallback-access",
                "token_type": "Bearer",
                "expires_in": 60,
                "refresh_token": "fallback-refresh",
                "scope": "calendar.read",
            }
            server.accepted_access_tokens.add("fallback-access")
        return server(request)

    config = _cimd_config(
        descriptor,
        source,
        presenter=presenter,
        token_store=store,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        auth=config,
    ) as client:
        assert (await _client_get_with_test_authorization(client, config)).status_code == 200
        assert (await _client_get_with_test_authorization(client, config)).status_code == 200

    restarted = _cimd_config(
        descriptor,
        source,
        presenter=_Presenter(),
        token_store=store,
    )
    server.requests.clear()
    assert (await _send(restarted, server)).status_code == 200
    assert refresh_attempts == 1
    assert presenter.callback_count == 1
    assert all(
        parse_qs(request.content.decode("ascii"), strict_parsing=True).get("grant_type")
        != ["refresh_token"]
        for request in server.requests
        if str(request.url) == server.token_endpoint
    )


@pytest.mark.anyio
@pytest.mark.parametrize("failure_kind", ["error", "cancellation"])
async def test_refresh_invalidation_failure_preserves_exact_stale_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(authorization_module.time, "time", lambda: now[0])
    marker = "delete-capability-marker"
    cancellation = asyncio.CancelledError("delete-cancel")
    failure: BaseException = RuntimeError(marker) if failure_kind == "error" else cancellation
    store = _FailingDeleteStore(failure)
    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    initial_server = _MockOAuthServers()
    initial_server.token_body["expires_in"] = 10
    assert (
        await _send(
            _cimd_config(
                descriptor,
                source,
                presenter=_FlowPresenter(code="initial-delete-code"),
                token_store=store,
            ),
            initial_server,
        )
    ).status_code == 200
    stale = dict(store.tokens)
    now[0] += 11
    server = _MockOAuthServers()
    server.token_status = 400
    server.token_body = {"error": "invalid_grant"}
    config = _cimd_config(
        descriptor,
        source,
        presenter=_Presenter(),
        token_store=store,
    )

    if failure_kind == "cancellation":
        with pytest.raises(asyncio.CancelledError) as raised:
            await _send(config, server)
        assert raised.value is cancellation
    else:
        with pytest.raises(McpClientError) as raised:
            await _send(config, server)
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_STORE_UNAVAILABLE
        assert marker not in _error_chain_text(raised.value)

    assert store.delete_calls == 1
    assert store.tokens == stale


@pytest.mark.anyio
@pytest.mark.parametrize("start_order", [(0, 1), (1, 0)])
async def test_multiple_provider_instances_serialize_shared_refresh_and_restart_on_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_order: tuple[int, int],
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(authorization_module.time, "time", lambda: now[0])
    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    store = _MemoryStore()
    initial_server = _MockOAuthServers()
    initial_server.token_body["expires_in"] = 10
    assert (
        await _send(
            _cimd_config(
                descriptor,
                source,
                presenter=_FlowPresenter(code="initial-race-code"),
                token_store=store,
            ),
            initial_server,
        )
    ).status_code == 200
    now[0] += 11

    server = _MockOAuthServers()
    server.token_body = {
        "access_token": "serialized-access",
        "token_type": "Bearer",
        "expires_in": 60,
        "refresh_token": "serialized-refresh",
        "scope": "calendar.read",
    }
    server.accepted_access_tokens.add("serialized-access")
    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    active_refreshes = 0
    max_active_refreshes = 0
    refresh_requests = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal active_refreshes, max_active_refreshes, refresh_requests
        if str(request.url) == server.token_endpoint:
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
            if form["grant_type"] == ["refresh_token"]:
                refresh_requests += 1
                active_refreshes += 1
                max_active_refreshes = max(max_active_refreshes, active_refreshes)
                refresh_entered.set()
                try:
                    await release_refresh.wait()
                finally:
                    active_refreshes -= 1
        return server(request)

    configs = [
        _cimd_config(descriptor, source, presenter=_Presenter(), token_store=store),
        _cimd_config(descriptor, source, presenter=_Presenter(), token_store=store),
    ]

    async def send(index: int) -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            auth=configs[index],
        ) as client:
            return await client.get("https://apps.example.com/mcp")

    first = asyncio.create_task(send(start_order[0]))
    await refresh_entered.wait()
    second = asyncio.create_task(send(start_order[1]))
    await asyncio.sleep(0)
    release_refresh.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == second_response.status_code == 200
    assert refresh_requests == 1
    assert max_active_refreshes == 1
    restarted = _cimd_config(
        descriptor,
        source,
        presenter=_Presenter(),
        token_store=store,
    )
    assert (await _send(restarted, server)).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("winner", ["local", "external"])
async def test_refresh_compare_and_store_never_rolls_back_opposite_completion_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winner: str,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(authorization_module.time, "time", lambda: now[0])
    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    store = _ExternalWinnerStore()
    initial_server = _MockOAuthServers()
    initial_server.token_body["expires_in"] = 10
    assert (
        await _send(
            _cimd_config(
                descriptor,
                source,
                presenter=_FlowPresenter(code="initial-cas-code"),
                token_store=store,
            ),
            initial_server,
        )
    ).status_code == 200
    now[0] += 11
    store.inject_external_winner = winner == "external"
    refresh_server = _MockOAuthServers()
    refresh_server.token_body = {
        "access_token": "local-winner-access",
        "token_type": "Bearer",
        "expires_in": 60,
        "refresh_token": "local-winner-refresh",
        "scope": "calendar.read",
    }
    refresh_server.accepted_access_tokens.update({"local-winner-access", "external-winner-access"})
    refresh = _cimd_config(
        descriptor,
        source,
        presenter=_Presenter(),
        token_store=store,
    )

    if winner == "external":
        with pytest.raises(McpClientError) as raised:
            await _send(refresh, refresh_server)
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    else:
        assert (await _send(refresh, refresh_server)).status_code == 200

    restarted = _cimd_config(
        descriptor,
        source,
        presenter=_Presenter(),
        token_store=store,
    )
    requests_before = len(refresh_server.requests)
    assert (await _send(restarted, refresh_server)).status_code == 200
    request = refresh_server.requests[requests_before]
    expected = "external-winner-access" if winner == "external" else "local-winner-access"
    assert request.headers["Authorization"] == f"Bearer {expected}"


@pytest.mark.anyio
async def test_cancelled_refresh_store_write_keeps_prior_revision_and_releases_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(authorization_module.time, "time", lambda: now[0])
    cancellation = asyncio.CancelledError("cancel-refresh-cas")
    store = _RefreshWriteCancellingStore(cancellation)
    requirement = _requirement()
    descriptor = _descriptor(requirement)
    source = _trusted_source(tmp_path, requirement)
    initial_server = _MockOAuthServers()
    initial_server.token_body["expires_in"] = 10
    assert (
        await _send(
            _cimd_config(
                descriptor,
                source,
                presenter=_FlowPresenter(code="initial-cancel-cas"),
                token_store=store,
            ),
            initial_server,
        )
    ).status_code == 200
    stale = dict(store.tokens)
    now[0] += 11
    store.cancel_next_write = True
    refresh_server = _MockOAuthServers()
    refresh_server.token_body = {
        "access_token": "cancelled-write-access",
        "token_type": "Bearer",
        "expires_in": 60,
        "refresh_token": "cancelled-write-refresh",
        "scope": "calendar.read",
    }
    refresh_server.accepted_access_tokens.add("cancelled-write-access")
    with pytest.raises(asyncio.CancelledError) as raised:
        await _send(
            _cimd_config(
                descriptor,
                source,
                presenter=_Presenter(),
                token_store=store,
            ),
            refresh_server,
        )
    assert raised.value is cancellation
    assert store.tokens == stale

    recovered_server = _MockOAuthServers()
    recovered_server.token_body = {
        "access_token": "recovered-access",
        "token_type": "Bearer",
        "expires_in": 60,
        "refresh_token": "recovered-refresh",
        "scope": "calendar.read",
    }
    recovered_server.accepted_access_tokens.add("recovered-access")
    assert (
        await _send(
            _cimd_config(
                descriptor,
                source,
                presenter=_Presenter(),
                token_store=store,
            ),
            recovered_server,
        )
    ).status_code == 200


@pytest.mark.parametrize(
    ("audience", "resource", "matches"),
    [
        # Miro publishes its resource WITH the root delimiter and mints tokens
        # whose audience carries none. RFC 3986 s6.2.3 makes those one resource
        # when an authority is present, and comparing the strings made them two
        # -- a sign-in that completed the token exchange, the MCP handshake and
        # tools/list was refused at the last step (live 2026-09-07).
        ("https://mcp.miro.com", "https://mcp.miro.com/", True),
        ("https://mcp.miro.com/", "https://mcp.miro.com", True),
        ("https://mcp.miro.com/", "https://mcp.miro.com/", True),
        (["https://mcp.miro.com"], "https://mcp.miro.com/", True),
        # Only the ROOT is a delimiter. A real path keeps its trailing slash,
        # because GitHub's resource IS ".../mcp/" and ".../mcp" is a different
        # one.
        ("https://api.githubcopilot.com/mcp/", "https://api.githubcopilot.com/mcp", False),
        ("https://api.githubcopilot.com/mcp", "https://api.githubcopilot.com/mcp/", False),
        # Everything that actually distinguishes a resource still does.
        ("https://other.example", "https://mcp.miro.com/", False),
        ("http://mcp.miro.com", "https://mcp.miro.com/", False),
        ("https://mcp.miro.com:8443", "https://mcp.miro.com/", False),
        ("https://mcp.miro.com/?tenant=other", "https://mcp.miro.com/", False),
        ("https://mcp.miro.com/#other", "https://mcp.miro.com/", False),
        # Still exactly one audience, still a string.
        (["https://mcp.miro.com", "https://mcp.miro.com"], "https://mcp.miro.com/", False),
        (None, "https://mcp.miro.com/", False),
    ],
)
def test_a_bare_origin_and_its_root_form_are_one_audience(
    audience: object, resource: str, matches: bool
) -> None:
    from mcp_platform.authorization import _audience_matches

    assert _audience_matches(audience, resource) is matches


@pytest.mark.parametrize(
    "resource",
    [
        # Miro publishes its resource as an origin with the root delimiter and
        # Stripe as a bare one. RFC 8707 makes a resource indicator a URI, and
        # an origin is an ordinary one -- but this refused any URI naming no
        # path, so Miro's sign-in completed its token exchange, its MCP
        # handshake and tools/list and then failed here (live 2026-09-07).
        "https://mcp.miro.com/",
        "https://mcp.stripe.com",
        # A resource that does name a path is unchanged.
        "https://api.githubcopilot.com/mcp/",
        "https://mcp.slack.com/mcp",
    ],
)
def test_a_resource_identifier_may_be_an_origin(resource: str) -> None:
    from mcp_platform.authorization import _canonical_uri

    assert _canonical_uri(resource, allow_origin=True) == resource


@pytest.mark.parametrize(
    "resource",
    [
        "http://mcp.miro.com/",
        "https://mcp.miro.com/?tenant=other",
        "https://mcp.miro.com/#fragment",
        "https://user@mcp.miro.com/",
        "https://MCP.miro.com/",
        "https://mcp.miro.com:443/",
    ],
)
def test_everything_else_a_resource_must_be_is_unchanged(resource: str) -> None:
    from mcp_platform.authorization import _canonical_uri

    with pytest.raises(McpClientError):
        _canonical_uri(resource, allow_origin=True)


@pytest.mark.parametrize("value", ["https://mcp.miro.com/", "https://mcp.miro.com"])
def test_a_redirect_or_metadata_document_still_has_to_name_something(value: str) -> None:
    """The allowance is opt-in, and neither of these opts in.

    A redirect target and a metadata document always name something; only a
    resource identifier may be an origin.
    """
    from mcp_platform.authorization import _canonical_uri

    with pytest.raises(McpClientError):
        _canonical_uri(value)
    with pytest.raises(McpClientError):
        _canonical_uri(value, metadata_document=True)
