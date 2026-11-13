"""Sealed broker-authenticated MCP transport contracts."""

from __future__ import annotations

import copy
import pickle
import weakref
from dataclasses import fields, replace

import httpx2
import pytest

import mcp_platform.authorization as authorization_module
import mcp_platform.oauth_broker as oauth_broker_module
from mcp_platform.authorization import (
    _authorization_descriptor_binding,
    _authorization_request_binding,
    _authorization_response_binding,
    _begin_broker_transport_operation,
    _finish_broker_transport_operation,
    McpBrokerAuthorizationConfig,
    is_mcp_authorization_config,
)
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.oauth_broker import (
    BrokerTransportBinding,
    OAuthBroker,
    OAuthPreparedAuthorization,
)
from mcp_platform.oauth_profiles import (
    OAuthClientMode,
    OAuthProfile,
    computed_profile_revision,
)
from mcp_platform.oauth_protocol import (
    OAuthDiscovery,
    OAuthNetworkError,
    OAuthProtocolError,
    OAuthProviderError,
    OAuthTokenResponse,
)
from mcp_platform.oauth_vault import (
    OAuthCommittedGrant,
    OAuthPendingTransaction,
    OAuthPreparedToken,
    OAuthTokenPartition,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from plugin_platform.contracts import PluginAuthentication


_RESOURCE = "https://resource.example/mcp"
_ACCESS_TOKEN = "opaque-access"
_REFRESHED_TOKEN = "opaque-refreshed-access"
_ACCOUNT = "a" * 64
_AUTHORIZATION_CONTEXT = "b" * 64
_SCOPES = ("repo",)


class _BrokerCalls:
    def __init__(self, binding: BrokerTransportBinding) -> None:
        self.current_binding = [binding]
        self.validated: list[BrokerTransportBinding] = []
        self.current: list[BrokerTransportBinding] = []
        self.token_loads: list[str] = []
        self.refresh: list[dict[str, object]] = []
        self.refresh_error: Exception | None = None

    def resolve_current(self, binding: BrokerTransportBinding) -> BrokerTransportBinding:
        self.validated.append(binding)
        return self.current_binding[0]


def _profile(resource_url: str) -> OAuthProfile:
    profile = OAuthProfile(
        schema_version=1,
        profile_id="example.desktop",
        catalog_signer="omega.featured",
        allowed_plugin_publishers=("omega.first_party",),
        app_publisher="example",
        provider_id="example.official",
        authorization_issuer="https://issuer.example",
        resource_origins=(resource_url,),
        client_mode=OAuthClientMode.PREREGISTERED,
        client_id="signed-public-client",
        client_secret_ref=None,
        client_metadata_document_url=None,
        redirect_uri="http://127.0.0.1:13310/oauth/example.desktop/callback",
        account_partition="issuer_account",
        revision="sha256:" + "0" * 64,
    )
    return replace(profile, revision=computed_profile_revision(profile))


def _binding(
    *,
    app_id: str = "app.alpha",
    resource_url: str = _RESOURCE,
    capability_revision: str = "4" * 64,
) -> BrokerTransportBinding:
    profile = _profile(resource_url)
    credential_identity = oauth_broker_module._credential_identity(
        profile,
        "signed-public-client",
    )
    partition_key = oauth_broker_module._partition_digest(
        account_partition=profile.account_partition,
        authorization_issuer=profile.authorization_issuer,
        resource_origin=resource_url,
        account_key_sha256=_ACCOUNT,
        credential_identity=credential_identity,
    )
    return BrokerTransportBinding(
        plugin_id="plugin.alpha",
        app_id=app_id,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        partition_key=partition_key,
        resource_url=resource_url,
        connection_revision="3" * 64,
        capability_revision=capability_revision,
        authority_revision="5" * 64,
        capability_binding_revision="6" * 64,
    )


def _descriptor(binding: BrokerTransportBinding) -> McpConnectionDescriptor:
    return McpConnectionDescriptor(
        app_id=binding.app_id,
        publisher_id="example.publisher",
        deployment=McpClientDeployment.REMOTE,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin="https://resource.example",
        authorization_issuer="https://auth.example/issuer",
        authentication=PluginAuthentication.from_dict(
            {
                "kind": "oauth_authorization_code_pkce",
                "scopes": ["example.read"],
                "secret_fields": [],
            }
        ),
        declared_capabilities={},
        extension_versions={},
        capability_revision=binding.capability_revision,
        limits=McpClientLimits(),
    )


class _Registry:
    def __init__(self, profile: OAuthProfile) -> None:
        self.profile = profile

    def get_exact(self, profile_id: str, revision: str) -> OAuthProfile:
        if profile_id != self.profile.profile_id or revision != self.profile.revision:
            raise ValueError("oauth_profile_unavailable")
        return self.profile


class _Vault:
    def __init__(
        self,
        binding: BrokerTransportBinding,
        profile: OAuthProfile,
        calls: _BrokerCalls,
    ) -> None:
        self.binding = binding
        self.calls = calls
        self.grant = OAuthCommittedGrant(
            receipt_id="7" * 64,
            plugin_id=binding.plugin_id,
            app_id=binding.app_id,
            account_key_sha256=_ACCOUNT,
            profile_id=binding.profile_id,
            profile_revision=binding.profile_revision,
            token_partition_key=binding.partition_key,
            connection_revision=binding.connection_revision,
            capability_revision=binding.capability_revision,
            authority_revision=binding.authority_revision,
            capability_binding_revision=binding.capability_binding_revision,
            scopes=_SCOPES,
            execution_policy="direct_turn",
        )
        self.partition = OAuthTokenPartition(
            partition_key=binding.partition_key,
            profile_id=binding.profile_id,
            profile_revision=binding.profile_revision,
            authorization_issuer=profile.authorization_issuer,
            resource_origin=binding.resource_url,
            account_key_sha256=_ACCOUNT,
            client_id="signed-public-client",
            authorization_context_revision=_AUTHORIZATION_CONTEXT,
            token=OAuthPreparedToken(
                access_token=_ACCESS_TOKEN,
                refresh_token="opaque-refresh",
                token_type="Bearer",
                scopes=_SCOPES,
                expires_at=500.0,
            ),
            dependent_plugin_ids=(binding.plugin_id,),
            revision=1,
            account_partition=profile.account_partition,
            credential_identity=oauth_broker_module._credential_identity(
                profile,
                "signed-public-client",
            ),
        )
        self.transaction = OAuthPendingTransaction(
            profile_id=binding.profile_id,
            plugin_id=binding.plugin_id,
            app_id=binding.app_id,
            account_key_sha256=_ACCOUNT,
            partition_key=binding.partition_key,
            authorization_issuer=profile.authorization_issuer,
            resource_origin=binding.resource_url,
            client_id="signed-public-client",
            state_sha256="9" * 64,
            pkce_verifier="opaque-verifier",
            redirect_uri=profile.redirect_uri,
            scopes=_SCOPES,
            authorization_context_revision=_AUTHORIZATION_CONTEXT,
            profile_revision=binding.profile_revision,
            issued_at=50.0,
            expires_at=200.0,
            attempt_revision=0,
            prepared_token=OAuthPreparedToken(
                access_token=_ACCESS_TOKEN,
                refresh_token="opaque-refresh",
                token_type="Bearer",
                scopes=_SCOPES,
                expires_at=500.0,
            ),
            requested_scopes=_SCOPES,
            account_partition=profile.account_partition,
            credential_identity=oauth_broker_module._credential_identity(
                profile,
                "signed-public-client",
            ),
            attempt_context={
                "omega_authorization_context_revision": _AUTHORIZATION_CONTEXT,
            },
        )

    def load_transaction(self, state_sha256: str) -> OAuthPendingTransaction:
        if state_sha256 != self.transaction.state_sha256:
            raise ValueError("oauth_transaction_missing")
        return self.transaction

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
        binding = self.binding
        if (
            plugin_id,
            app_id,
            profile_id,
            profile_revision,
            partition_key,
            connection_revision,
            capability_revision,
            authority_revision,
            capability_binding_revision,
        ) != (
            binding.plugin_id,
            binding.app_id,
            binding.profile_id,
            binding.profile_revision,
            binding.partition_key,
            binding.connection_revision,
            binding.capability_revision,
            binding.authority_revision,
            binding.capability_binding_revision,
        ):
            raise ValueError("oauth_transport_binding_invalid")
        self.calls.current.append(binding)
        return self.grant

    def load_token_partition(self, partition_key: str) -> OAuthTokenPartition:
        if partition_key != self.partition.partition_key:
            raise ValueError("oauth_token_partition_missing")
        self.calls.token_loads.append(partition_key)
        return self.partition

    def replace_token_partition(
        self,
        partition_key: str,
        token: OAuthPreparedToken,
        *,
        expected_revision: int,
    ) -> OAuthTokenPartition:
        if (
            partition_key != self.partition.partition_key
            or expected_revision != self.partition.revision
        ):
            raise ValueError("oauth_token_revision_conflict")
        self.partition = replace(
            self.partition,
            token=token,
            revision=self.partition.revision + 1,
        )
        return self.partition

    def mark_partition_reauthorization_required(self, partition_key: str) -> None:
        if partition_key != self.partition.partition_key:
            raise ValueError("oauth_token_partition_missing")
        self.partition = replace(self.partition, reauthorization_required=True)


class _Protocol:
    def __init__(self, calls: _BrokerCalls) -> None:
        self.calls = calls

    async def discover(self, profile: OAuthProfile, *, resource_origin: str) -> OAuthDiscovery:
        return OAuthDiscovery(
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            client_mode=profile.client_mode,
            client_identifier="signed-public-client",
            client_secret_advertised=False,
            redirect_uri=profile.redirect_uri,
            issuer=profile.authorization_issuer,
            resource_origin=resource_origin,
            authorization_endpoint="https://login.example/authorize",
            token_endpoint="https://login.example/token",
            revocation_endpoint=None,
            code_challenge_methods_supported=("S256",),
            authorization_response_iss_parameter_supported=True,
            scopes_supported=_SCOPES,
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
        self.calls.refresh.append(
            {
                "client_id": client_id,
                "profile_revision": discovery.profile_revision,
                "refresh_token_present": bool(refresh_token),
                "client_secret_present": client_secret is not None,
            }
        )
        if self.calls.refresh_error is not None:
            raise self.calls.refresh_error
        return OAuthTokenResponse(
            access_token=_REFRESHED_TOKEN,
            refresh_token="opaque-refreshed-refresh",
            token_type="Bearer",
            scopes=required_scopes,
            expires_at=500.0,
        )


_TEST_AUTHORITIES: weakref.WeakKeyDictionary[OAuthBroker, object] = weakref.WeakKeyDictionary()
_TEST_CALLS: weakref.WeakKeyDictionary[OAuthBroker, _BrokerCalls] = weakref.WeakKeyDictionary()


def _broker(
    binding: BrokerTransportBinding | None = None,
) -> tuple[OAuthBroker, _BrokerCalls]:
    selected = _binding() if binding is None else binding
    profile = _profile(selected.resource_url)
    calls = _BrokerCalls(selected)
    broker = OAuthBroker(
        registry=_Registry(profile),  # type: ignore[arg-type]
        vault=_Vault(selected, profile, calls),  # type: ignore[arg-type]
        protocol=_Protocol(calls),  # type: ignore[arg-type]
        clock=lambda: 100.0,
    )
    authority = oauth_broker_module._issue_broker_transport_authority(
        broker,
        current_binding=calls.resolve_current,
    )
    _TEST_AUTHORITIES[broker] = authority
    _TEST_CALLS[broker] = calls
    return broker, calls


def _authorization(
    binding: BrokerTransportBinding,
    broker: OAuthBroker,
    *,
    current: list[BrokerTransportBinding] | None = None,
) -> McpBrokerAuthorizationConfig:
    calls = _TEST_CALLS[broker]
    if current is not None:
        calls.current_binding = current
    return broker.authorize_transport(binding, _TEST_AUTHORITIES[broker])


def _prepared(binding: BrokerTransportBinding) -> OAuthPreparedAuthorization:
    return OAuthPreparedAuthorization(
        profile_id=binding.profile_id,
        plugin_id=binding.plugin_id,
        app_id=binding.app_id,
        account_key_sha256=_ACCOUNT,
        state_sha256="9" * 64,
        partition_key=binding.partition_key,
        profile_revision=binding.profile_revision,
        authorization_context_revision=_AUTHORIZATION_CONTEXT,
        scopes=_SCOPES,
        attempt_context={
            "omega_authorization_context_revision": _AUTHORIZATION_CONTEXT,
        },
    )


@pytest.mark.anyio
async def test_prepared_probe_uses_exact_sealed_broker_authorization_before_commit() -> None:
    binding = _binding()
    broker, calls = _broker(binding)

    auth = broker.authorize_prepared_transport(
        _prepared(binding),
        binding,
        _TEST_AUTHORITIES[broker],
    )
    request = httpx2.Request("POST", binding.resource_url)
    flow = auth.async_auth_flow(request)
    authorized = await anext(flow)

    assert type(auth) is McpBrokerAuthorizationConfig
    assert authorized.headers["authorization"] == f"Bearer {_ACCESS_TOKEN}"
    assert calls.current == []
    assert calls.token_loads == []


@pytest.mark.anyio
async def test_prepared_probe_expires_with_transaction_and_never_refreshes() -> None:
    binding = _binding()
    broker, calls = _broker(binding)
    auth = broker.authorize_prepared_transport(
        _prepared(binding),
        binding,
        _TEST_AUTHORITIES[broker],
    )
    vault = broker._vault
    vault.load_transaction = lambda _state: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ValueError("oauth_transaction_missing")
    )

    with pytest.raises(McpClientError) as raised:
        await anext(auth.async_auth_flow(httpx2.Request("POST", binding.resource_url)))

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
    assert calls.refresh == []


@pytest.mark.anyio
async def test_prepared_probe_rejects_host_binding_drift_before_token_read() -> None:
    binding = _binding()
    broker, calls = _broker(binding)
    auth = broker.authorize_prepared_transport(
        _prepared(binding),
        binding,
        _TEST_AUTHORITIES[broker],
    )
    calls.current_binding[0] = replace(binding, authority_revision="8" * 64)

    with pytest.raises(McpClientError) as raised:
        await anext(auth.async_auth_flow(httpx2.Request("POST", binding.resource_url)))

    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    assert calls.current == []
    assert calls.token_loads == []


@pytest.mark.anyio
async def test_prepared_probe_401_requires_reauthorization_without_commit_or_refresh() -> None:
    binding = _binding()
    broker, calls = _broker(binding)
    auth = broker.authorize_prepared_transport(
        _prepared(binding),
        binding,
        _TEST_AUTHORITIES[broker],
    )
    flow = auth.async_auth_flow(httpx2.Request("POST", binding.resource_url))
    authorized = await anext(flow)

    with pytest.raises(McpClientError) as raised:
        await flow.asend(httpx2.Response(401, request=authorized))

    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
    assert calls.refresh == []
    assert calls.current == []


@pytest.mark.anyio
async def test_broker_auth_attaches_bearer_only_after_current_binding_validation() -> None:
    binding = _binding()
    broker, calls = _broker(binding)
    auth = _authorization(binding, broker)
    assert calls.validated == [binding] * 2
    assert calls.current == [binding]

    request = httpx2.Request("POST", binding.resource_url)
    flow = auth.async_auth_flow(request)
    authorized = await anext(flow)

    assert calls.validated == [binding] * 5
    assert calls.current == [binding] * 2
    assert authorized.headers["authorization"] == f"Bearer {_ACCESS_TOKEN}"

    response = httpx2.Response(200, request=authorized)
    with pytest.raises(StopAsyncIteration):
        await flow.asend(response)
    assert "authorization" not in authorized.headers

    wrong = httpx2.Request("POST", "https://attacker.invalid/mcp")
    with pytest.raises(McpClientError) as raised:
        await anext(auth.async_auth_flow(wrong))
    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    assert calls.current == [binding] * 2


@pytest.mark.anyio
async def test_broker_response_boundary_immediately_scrubs_bearer_and_proof_state() -> None:
    binding = _binding()
    descriptor = _descriptor(binding)
    broker, _calls = _broker()
    auth = _authorization(binding, broker)
    assert (
        _authorization_descriptor_binding(auth, descriptor, endpoint=binding.resource_url) is auth
    )
    flow = auth.async_auth_flow(httpx2.Request("POST", binding.resource_url))
    request = await anext(flow)
    await _authorization_request_binding(auth, descriptor, request)

    _authorization_response_binding(auth, descriptor, request)

    assert "authorization" not in request.headers
    assert all("authorization" not in name for name in request.extensions)
    await flow.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connection_revision", "7" * 64),
        ("profile_revision", "sha256:" + "8" * 64),
        ("capability_revision", "9" * 64),
    ],
)
async def test_broker_auth_rejects_stale_current_revisions_before_token_load(
    field: str,
    value: str,
) -> None:
    binding = _binding()
    broker, calls = _broker()
    current = [binding]
    auth = _authorization(binding, broker, current=current)
    current[0] = replace(binding, **{field: value})

    with pytest.raises(McpClientError) as raised:
        await anext(auth.async_auth_flow(httpx2.Request("POST", binding.resource_url)))

    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    assert calls.current == [binding]
    assert calls.refresh == []


@pytest.mark.anyio
async def test_broker_auth_401_refreshes_once_and_yields_one_sanitized_clone() -> None:
    binding = _binding()
    broker, calls = _broker()
    auth = _authorization(binding, broker)
    request = httpx2.Request(
        "POST",
        binding.resource_url,
        headers={"content-type": "application/json"},
        content=b'{"jsonrpc":"2.0"}',
        extensions={"timeout": {"read": 1.0}, "opaque.secret": _ACCESS_TOKEN},
    )
    flow = auth.async_auth_flow(request)
    authorized = await anext(flow)

    retry = await flow.asend(httpx2.Response(401, request=authorized))

    assert retry is not authorized
    assert retry.method == authorized.method
    assert retry.url == authorized.url
    assert retry.content == authorized.content
    assert retry.headers["authorization"] == f"Bearer {_REFRESHED_TOKEN}"
    assert retry.extensions.get("timeout") == {"read": 1.0}
    assert "opaque.secret" not in retry.extensions
    assert "authorization" not in authorized.headers
    assert calls.current == [binding] * 5
    assert len(calls.refresh) == 1

    with pytest.raises(McpClientError) as raised:
        await flow.asend(httpx2.Response(401, request=retry))
    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_REQUIRED
    assert len(calls.refresh) == 1
    assert "authorization" not in retry.headers


@pytest.mark.anyio
async def test_broker_auth_rejects_a_different_second_401_response_identity() -> None:
    binding = _binding()
    broker, calls = _broker()
    auth = _authorization(binding, broker)
    operation, context_token = _begin_broker_transport_operation(auth)
    flow = auth.async_auth_flow(httpx2.Request("POST", binding.resource_url))
    try:
        authorized = await anext(flow)
        retry = await flow.asend(httpx2.Response(401, request=authorized))
        mismatched_request = httpx2.Request("POST", binding.resource_url)

        with pytest.raises(McpClientError) as raised:
            await flow.asend(httpx2.Response(401, request=mismatched_request))
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    finally:
        await flow.aclose()
        _finish_broker_transport_operation(auth, operation, context_token)

    assert len(calls.refresh) == 1
    assert "authorization" not in retry.headers
    assert "authorization" not in mismatched_request.headers


@pytest.mark.anyio
async def test_broker_auth_rejects_replacement_of_the_marked_retry_response() -> None:
    binding = _binding()
    descriptor = _descriptor(binding)
    broker, calls = _broker()
    auth = _authorization(binding, broker)
    assert (
        _authorization_descriptor_binding(
            auth,
            descriptor,
            endpoint=binding.resource_url,
        )
        is auth
    )
    operation, context_token = _begin_broker_transport_operation(auth)
    flow = auth.async_auth_flow(httpx2.Request("POST", binding.resource_url))
    try:
        authorized = await anext(flow)
        await _authorization_request_binding(auth, descriptor, authorized)
        _authorization_response_binding(auth, descriptor, authorized)
        retry = await flow.asend(httpx2.Response(401, request=authorized))
        await _authorization_request_binding(auth, descriptor, retry)
        marked_response = httpx2.Response(401, request=retry)
        marker = authorization_module._mark_broker_transport_response(
            auth,
            descriptor,
            marked_response,
        )
        assert marker is not None
        _authorization_response_binding(auth, descriptor, retry)

        with pytest.raises(McpClientError) as raised:
            await flow.asend(httpx2.Response(401, request=retry))
        assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    finally:
        await flow.aclose()
        _finish_broker_transport_operation(auth, operation, context_token)

    assert len(calls.refresh) == 1
    assert "authorization" not in retry.headers


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            OAuthProviderError("opaque-provider-rejection"),
            McpClientErrorReason.AUTHORIZATION_REQUIRED,
        ),
        (
            OAuthNetworkError("opaque-provider-network-content"),
            McpClientErrorReason.TRANSPORT_UNAVAILABLE,
        ),
        (
            OAuthProtocolError("opaque-provider-protocol-content"),
            McpClientErrorReason.AUTHORIZATION_INVALID,
        ),
    ],
)
async def test_broker_auth_refresh_failures_are_distinct_and_content_free(
    error: Exception,
    reason: McpClientErrorReason,
) -> None:
    binding = _binding()
    broker, calls = _broker()
    calls.refresh_error = error
    auth = _authorization(binding, broker)
    flow = auth.async_auth_flow(httpx2.Request("POST", binding.resource_url))
    authorized = await anext(flow)

    with pytest.raises(McpClientError) as raised:
        await flow.asend(httpx2.Response(401, request=authorized))

    assert raised.value.reason is reason
    assert "opaque-" not in repr(raised.value)
    assert "opaque-" not in str(raised.value)
    assert "authorization" not in authorized.headers
    assert len(calls.refresh) == 1


def test_broker_auth_is_sealed_token_only_and_has_no_interactive_surface() -> None:
    binding = _binding()
    broker, _calls = _broker()
    first = _authorization(binding, broker)
    second = _authorization(binding, broker)

    with pytest.raises(McpClientError) as raised:
        McpBrokerAuthorizationConfig()
    assert raised.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID
    assert fields(first) == ()
    assert first is not second
    assert first != second
    assert repr(first) == "McpBrokerAuthorizationConfig(<sealed>)"
    assert is_mcp_authorization_config(first) is True
    with pytest.raises(AttributeError):
        _ = first.__dict__

    public_names = {name for name in dir(first) if not name.startswith("_")}
    forbidden = {
        "access_token",
        "authorization_url",
        "callback",
        "client_id",
        "client_secret",
        "credentials",
        "issue",
        "presenter",
        "refresh_token",
        "token",
    }
    assert public_names.isdisjoint(forbidden)


def test_broker_auth_has_no_public_self_issuance_api() -> None:
    assert not hasattr(McpBrokerAuthorizationConfig, "issue")


class _BrokerSubclass(OAuthBroker):
    pass


class _BrokerAuthorizationSubclass(McpBrokerAuthorizationConfig):
    pass


def test_broker_owned_factory_rejects_object_new_subclass_and_monkeypatched_brokers() -> None:
    binding = _binding()
    for forged in (object.__new__(OAuthBroker), object.__new__(_BrokerSubclass)):
        with pytest.raises(ValueError, match="^oauth_broker_invalid$"):
            oauth_broker_module._issue_broker_transport_authority(
                forged,
                current_binding=lambda _binding: binding,
            )

    broker, _calls = _broker(binding)
    authority = _TEST_AUTHORITIES[broker]

    async def attacker_token(_binding: BrokerTransportBinding) -> str:
        return "attacker-token"

    broker.current_access_token = attacker_token  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="^oauth_broker_invalid$"):
        broker.authorize_transport(binding, authority)


def test_broker_owned_factory_rejects_class_level_validation_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    broker, _calls = _broker(binding)
    authority = _TEST_AUTHORITIES[broker]
    monkeypatch.setattr(
        OAuthBroker,
        "_validate_transport_grant",
        lambda _self, _binding: object(),
    )

    with pytest.raises(ValueError, match="^oauth_broker_invalid$"):
        broker.authorize_transport(binding, authority)


def test_broker_rejects_validation_replacement_before_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    profile = _profile(binding.resource_url)
    calls = _BrokerCalls(binding)
    monkeypatch.setattr(
        OAuthBroker,
        "_validate_transport_grant",
        lambda _self, _binding: object(),
    )

    with pytest.raises(ValueError, match="^oauth_broker_invalid$"):
        OAuthBroker(
            registry=_Registry(profile),  # type: ignore[arg-type]
            vault=_Vault(binding, profile, calls),  # type: ignore[arg-type]
            protocol=_Protocol(calls),  # type: ignore[arg-type]
            clock=lambda: 100.0,
        )

    assert calls.current == []
    assert calls.token_loads == []


def test_broker_and_transport_authority_reject_copy_deepcopy_and_pickle() -> None:
    binding = _binding()
    broker, _calls = _broker(binding)
    authority = _TEST_AUTHORITIES[broker]

    for value in (broker, authority):
        with pytest.raises(ValueError):
            copy.copy(value)
        with pytest.raises(ValueError):
            copy.deepcopy(value)
        with pytest.raises(ValueError):
            pickle.dumps(value)


@pytest.mark.anyio
async def test_broker_authorization_rejects_copy_deepcopy_pickle_subclass_and_object_new() -> None:
    binding = _binding()
    broker, _calls = _broker(binding)
    authorization = _authorization(binding, broker)

    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(McpClientError) as rejected:
            operation(authorization)
        assert rejected.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID

    for forged in (
        object.__new__(McpBrokerAuthorizationConfig),
        object.__new__(_BrokerAuthorizationSubclass),
    ):
        with pytest.raises(McpClientError) as rejected:
            await anext(forged.async_auth_flow(httpx2.Request("POST", binding.resource_url)))
        assert rejected.value.reason is McpClientErrorReason.AUTHORIZATION_INVALID


def test_broker_owned_factory_requires_current_host_and_durable_binding() -> None:
    binding = _binding()
    broker, calls = _broker(binding)
    authority = _TEST_AUTHORITIES[broker]
    stale = replace(binding, connection_revision="8" * 64)
    calls.current_binding[0] = stale

    with pytest.raises(ValueError, match="^oauth_transport_authority_invalid$"):
        broker.authorize_transport(binding, authority)
    assert calls.current == []

    with pytest.raises(ValueError, match="^oauth_transport_binding_invalid$"):
        broker.authorize_transport(stale, authority)
    assert calls.current == []


@pytest.mark.anyio
async def test_broker_auth_rejects_destination_drift_without_token_disclosure() -> None:
    binding = _binding()
    broker, calls = _broker()
    auth = _authorization(binding, broker)
    drifted = httpx2.Request("POST", f"{binding.resource_url}/")

    with pytest.raises(McpClientError) as raised:
        await anext(auth.async_auth_flow(drifted))

    assert raised.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    assert calls.current == [binding]
    assert _ACCESS_TOKEN not in repr(raised.value)


def test_broker_transport_issuance_does_not_load_token_material() -> None:
    binding = _binding()
    broker, calls = _broker(binding)

    _authorization(binding, broker)

    assert calls.current == [binding]
    assert calls.token_loads == []
