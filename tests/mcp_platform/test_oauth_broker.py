"""Durable provider-neutral OAuth broker contracts."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mcp_platform.oauth_broker import (
    BrokerTransportBinding,
    OAuthAuthorizationStart,
    OAuthBroker,
    OAuthConnectionCommit,
    OAuthConnectionRequest,
    OAuthPreparedAuthorization,
)
from mcp_platform.oauth_profiles import (
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthDynamicRegistrationTemplate,
    OAuthProfile,
    OAuthProfileMatchInput,
    computed_profile_revision,
)
from mcp_platform.oauth_protocol import (
    OAuthAuthorizationParameters,
    OAuthClientRegistrationResponse,
    OAuthDeviceAuthorization,
    OAuthDiscovery,
    OAuthNetworkError,
    OAuthProviderError,
    OAuthProtocolError,
    OAuthRevocationReceipt,
    OAuthTokenResponse,
)
from mcp_platform.oauth_vault import (
    OAuthClientCredential,
    OAuthClientRegistration,
    OAuthCredentialVault,
    OAuthTokenPartition,
)


_KEY = b"b" * 32
_AAD = b"omega-oauth-vault-v1"
_RESOURCE = "https://resource.example/mcp"
_RESOURCE_TWO = "https://resource.example/mail-mcp"
_ISSUER = "https://issuer.example"
_REDIRECT = "http://127.0.0.1:13310/oauth/example.desktop/callback"


def _profile(
    mode: OAuthClientMode = OAuthClientMode.PREREGISTERED,
    *,
    secret_ref: bool = True,
    profile_id: str = "example.desktop",
    resource_origin: str = _RESOURCE,
    issuer: str = _ISSUER,
    client_id: str = "signed-public-client",
    secret_reference: str | None = None,
) -> OAuthProfile:
    selected_secret_ref = (
        secret_reference if secret_reference is not None else "oauth-client/example.desktop/secret"
    )
    profile = OAuthProfile(
        schema_version=1,
        profile_id=profile_id,
        catalog_signer="omega.featured",
        allowed_plugin_publishers=("omega.first_party",),
        app_publisher="example",
        provider_id="example.official",
        authorization_issuer=issuer,
        resource_origins=(resource_origin,),
        client_mode=mode,
        client_id=client_id if mode is OAuthClientMode.PREREGISTERED else None,
        client_secret_ref=(
            selected_secret_ref if mode is OAuthClientMode.PREREGISTERED and secret_ref else None
        ),
        client_metadata_document_url=(
            "https://omega.example/oauth/example-client.json"
            if mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT
            else None
        ),
        redirect_uri=f"http://127.0.0.1:13310/oauth/{profile_id}/callback",
        account_partition="issuer_account",
        revision="sha256:" + "0" * 64,
    )
    return replace(profile, revision=computed_profile_revision(profile))


def _device_profile() -> OAuthProfile:
    profile = replace(
        _profile(secret_ref=False),
        authorization_flow=OAuthAuthorizationFlow.DEVICE_CODE,
        redirect_uri=None,
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


def _match() -> OAuthProfileMatchInput:
    return OAuthProfileMatchInput(
        catalog_signer="omega.featured",
        plugin_publisher="omega.first_party",
        app_publisher="example",
        provider_id="example.official",
        authorization_issuer=_ISSUER,
        resource_origin=_RESOURCE,
    )


def _match_for(
    *, resource_origin: str = _RESOURCE, issuer: str = _ISSUER
) -> OAuthProfileMatchInput:
    return replace(
        _match(),
        authorization_issuer=issuer,
        resource_origin=resource_origin,
    )


def _request(
    *,
    plugin_id: str = "plugin.alpha",
    authorization_flow: OAuthAuthorizationFlow = OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
) -> OAuthConnectionRequest:
    return OAuthConnectionRequest(
        profile_match=_match(),
        plugin_id=plugin_id,
        app_id=f"app.{plugin_id}",
        account_key_sha256="1" * 64,
        scopes=("repo", "read:user"),
        authorization_context_revision="a" * 64,
        expected_authorization_flow=authorization_flow,
    )


def _commit() -> OAuthConnectionCommit:
    return OAuthConnectionCommit(
        connection_revision="2" * 64,
        capability_revision="3" * 64,
        authority_revision="4" * 64,
        capability_binding_revision="5" * 64,
        execution_policy="direct_turn",
    )


def _legacy_partition(partition: OAuthTokenPartition) -> OAuthTokenPartition:
    legacy_key = sha256(
        json.dumps(
            {
                "account_key_sha256": partition.account_key_sha256,
                "authorization_context_revision": partition.authorization_context_revision,
                "authorization_issuer": partition.authorization_issuer,
                "client_id": partition.client_id,
                "profile_id": partition.profile_id,
                "profile_revision": partition.profile_revision,
                "resource_origin": partition.resource_origin,
                "scopes": list(partition.token.scopes),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return replace(
        partition,
        partition_key=legacy_key,
        reauthorization_required=False,
        credential_identity=sha256(
            json.dumps(
                {"legacy_client_id": partition.client_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        migration_status="legacy_v1",
    )


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Registry:
    def __init__(self, profile: OAuthProfile) -> None:
        self.profile = profile
        self.resolve_calls: list[OAuthProfileMatchInput] = []
        self.exact_available = True

    def resolve(self, match: OAuthProfileMatchInput) -> OAuthProfile:
        self.resolve_calls.append(match)
        if match.normalized() != _match():
            raise ValueError("oauth_profile_unavailable")
        return self.profile

    def get_exact(self, profile_id: str, revision: str) -> OAuthProfile:
        if (
            not self.exact_available
            or profile_id != self.profile.profile_id
            or revision != self.profile.revision
        ):
            raise ValueError("oauth_profile_unavailable")
        return self.profile


class _MultiRegistry:
    def __init__(self, *profiles: OAuthProfile) -> None:
        self.profiles = profiles

    def resolve(self, match: OAuthProfileMatchInput) -> OAuthProfile:
        normalized = match.normalized()
        matches = tuple(
            profile
            for profile in self.profiles
            if profile.authorization_issuer == normalized.authorization_issuer
            and normalized.resource_origin in profile.resource_origins
        )
        if len(matches) != 1:
            raise ValueError("oauth_profile_unavailable")
        return matches[0]

    def get_exact(self, profile_id: str, revision: str) -> OAuthProfile:
        matches = tuple(
            profile
            for profile in self.profiles
            if profile.profile_id == profile_id and profile.revision == revision
        )
        if len(matches) != 1:
            raise ValueError("oauth_profile_unavailable")
        return matches[0]


class _TrackingVault(OAuthCredentialVault):
    def __init__(self, root: Path, clock: _Clock) -> None:
        super().__init__(root, key_provider=lambda: _KEY, clock=clock)
        self.credential_reads: list[str] = []

    def load_credential(self, credential_ref: str) -> tuple[int, OAuthClientCredential] | None:
        self.credential_reads.append(credential_ref)
        return super().load_credential(credential_ref)


class _Protocol:
    def __init__(self) -> None:
        self.discover_calls: list[tuple[str, str]] = []
        self.exchange_calls: list[dict[str, object]] = []
        self.registration_calls: list[str] = []
        self.deregister_calls: list[dict[str, str]] = []
        self.refresh_calls: list[dict[str, object]] = []
        self.revoke_calls: list[dict[str, object]] = []
        self.exchange_entered: asyncio.Event | None = None
        self.exchange_release: asyncio.Event | None = None
        self.refresh_entered: asyncio.Event | None = None
        self.refresh_release: asyncio.Event | None = None
        self.refresh_error: Exception | None = None
        self.exchange_error: Exception | None = None
        self.registration_error: Exception | None = None
        #: What a CONFIDENTIAL server issues at registration. None is the
        #: ordinary public-client case, which is every provider that lists
        #: ``none`` among its token-endpoint auth methods.
        #: Does the fake server's token endpoint refuse a public client?
        self.requires_client_authentication = False
        self.registered_client_secret: str | None = None
        self.registered_secret_expires_at: float | None = None
        self.deregister_error: Exception | None = None
        self.registered_client_ids = ["dynamic-client-1", "dynamic-client-2"]
        self.revoke_error: Exception | None = None
        self.exchange_access_token = "opaque-access"
        self.exchange_refresh_token = "opaque-refresh"
        self.exchange_expires_at = 130.0
        self.revoke_receipt = OAuthRevocationReceipt(
            endpoint_advertised=True,
            provider_request_sent=True,
        )
        self.device_poll_calls = 0

    async def discover(self, profile: OAuthProfile, *, resource_origin: str) -> OAuthDiscovery:
        self.discover_calls.append((profile.revision, resource_origin))
        client_identifier = profile.client_id or profile.client_metadata_document_url
        return OAuthDiscovery(
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            client_mode=profile.client_mode,
            client_identifier=client_identifier,
            client_secret_advertised=profile.client_secret_ref is not None,
            redirect_uri=profile.redirect_uri,
            issuer=profile.authorization_issuer,
            resource_origin=resource_origin,
            authorization_endpoint=(
                "https://login.example/authorize"
                if profile.authorization_flow
                is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
                else None
            ),
            token_endpoint="https://tokens.example/token",
            revocation_endpoint="https://tokens.example/revoke",
            registration_endpoint="https://issuer.example/register",
            code_challenge_methods_supported=(
                ("S256",)
                if profile.authorization_flow
                is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
                else ()
            ),
            authorization_response_iss_parameter_supported=True,
            scopes_supported=("repo", "read:user"),
            authorization_flow=profile.authorization_flow,
            device_authorization_endpoint=(
                "https://login.example/device"
                if profile.authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE
                else None
            ),
            grant_types_supported=(
                ("urn:ietf:params:oauth:grant-type:device_code",)
                if profile.authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE
                else ()
            ),
            token_endpoint_auth_methods_supported=(
                ("client_secret_post",)
                if self.requires_client_authentication
                else ()
            ),
        )

    async def register(
        self,
        discovery: OAuthDiscovery,
        *,
        profile: OAuthProfile,
    ) -> OAuthClientRegistrationResponse:
        del discovery
        self.registration_calls.append(profile.profile_id)
        if self.registration_error is not None:
            raise self.registration_error
        client_id = self.registered_client_ids[len(self.registration_calls) - 1]
        return OAuthClientRegistrationResponse(
            client_id=client_id,
            registration_access_token="management-token",
            registration_client_uri=f"https://issuer.example/register/{client_id}",
            client_secret=self.registered_client_secret,
            client_secret_expires_at=self.registered_secret_expires_at,
        )

    async def deregister(
        self,
        registration_client_uri: str,
        *,
        registration_access_token: str,
    ) -> None:
        self.deregister_calls.append(
            {
                "registration_client_uri": registration_client_uri,
                "registration_access_token": registration_access_token,
            }
        )
        if self.deregister_error is not None:
            raise self.deregister_error

    async def begin_device_authorization(
        self,
        discovery: OAuthDiscovery,
        *,
        client_id: str,
        scopes: tuple[str, ...],
    ) -> OAuthDeviceAuthorization:
        return OAuthDeviceAuthorization(
            profile_id=discovery.profile_id,
            profile_revision=discovery.profile_revision,
            client_id=client_id,
            scopes=scopes,
            resource_origin=discovery.resource_origin,
            device_code="opaque-device-code",
            user_code="ABCD-EFGH",
            verification_uri="https://login.example/device",
            verification_uri_complete="https://login.example/device?user_code=ABCD-EFGH",
            expires_at=1000.0,
            interval_seconds=5,
        )

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
        return OAuthDeviceAuthorization(
            profile_id=discovery.profile_id,
            profile_revision=discovery.profile_revision,
            client_id=client_id,
            scopes=scopes,
            resource_origin=discovery.resource_origin,
            device_code=device_code,
            user_code="withheld-after-start",
            verification_uri="https://login.example/device",
            verification_uri_complete=None,
            expires_at=expires_at,
            interval_seconds=interval_seconds,
        )

    async def poll_device_token(
        self,
        discovery: OAuthDiscovery,
        authorization: OAuthDeviceAuthorization,
        *,
        client_id: str,
        required_scopes: tuple[str, ...],
    ) -> OAuthTokenResponse:
        del discovery, authorization, client_id
        self.device_poll_calls += 1
        if self.device_poll_calls == 1:
            raise OAuthProtocolError("oauth_device_authorization_pending")
        return OAuthTokenResponse(
            access_token="device-access",
            refresh_token=None,
            token_type="Bearer",
            scopes=required_scopes,
            expires_at=None,
        )

    def build_authorization_url(
        self,
        discovery: OAuthDiscovery,
        parameters: OAuthAuthorizationParameters,
    ) -> str:
        query = {
            "client_id": parameters.client_id,
            "code_challenge": parameters.code_challenge,
            "code_challenge_method": "S256",
            "redirect_uri": parameters.redirect_uri,
            "resource": parameters.resource_origin,
            "response_type": "code",
            "scope": " ".join(parameters.scopes),
            "state": parameters.state,
        }
        return f"{discovery.authorization_endpoint}?{urlencode(query)}"

    async def exchange(
        self,
        discovery: OAuthDiscovery,
        parameters: OAuthAuthorizationParameters,
        *,
        code: str,
        code_verifier: str,
        client_secret: str | None,
    ) -> OAuthTokenResponse:
        self.exchange_calls.append(
            {
                "client_id": parameters.client_id,
                "code": code,
                "verifier": code_verifier,
                "client_secret": client_secret,
            }
        )
        if self.exchange_entered is not None:
            self.exchange_entered.set()
        if self.exchange_release is not None:
            await self.exchange_release.wait()
        if self.exchange_error is not None:
            raise self.exchange_error
        return OAuthTokenResponse(
            access_token=self.exchange_access_token,
            refresh_token=self.exchange_refresh_token,
            token_type="Bearer",
            scopes=parameters.scopes,
            expires_at=self.exchange_expires_at,
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
        self.refresh_calls.append(
            {
                "client_id": client_id,
                "refresh_token": refresh_token,
                "client_secret": client_secret,
            }
        )
        if self.refresh_entered is not None:
            self.refresh_entered.set()
        if self.refresh_release is not None:
            await self.refresh_release.wait()
        if self.refresh_error is not None:
            raise self.refresh_error
        return OAuthTokenResponse(
            access_token="refreshed-access",
            refresh_token="refreshed-refresh",
            token_type="Bearer",
            scopes=required_scopes,
            expires_at=500.0,
        )

    async def revoke(
        self,
        discovery: OAuthDiscovery,
        *,
        client_id: str,
        token: str,
        client_secret: str | None,
    ) -> OAuthRevocationReceipt:
        self.revoke_calls.append(
            {"client_id": client_id, "token": token, "client_secret": client_secret}
        )
        if self.revoke_error is not None:
            raise self.revoke_error
        return self.revoke_receipt


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def registry() -> _Registry:
    return _Registry(_profile())


@pytest.fixture
def vault(tmp_path: Path, clock: _Clock) -> _TrackingVault:
    value = _TrackingVault(tmp_path, clock)
    value.store_credential(
        "oauth-client/example.desktop/secret",
        OAuthClientCredential("signed-public-client", "desktop-secret"),
        expected_revision=None,
    )
    return value


@pytest.fixture
def protocol() -> _Protocol:
    return _Protocol()


def _broker(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> OAuthBroker:
    return OAuthBroker(registry=registry, vault=vault, protocol=protocol, clock=clock)


def _state(start: OAuthAuthorizationStart) -> str:
    return parse_qs(urlsplit(start.authorization_url).query, strict_parsing=True)["state"][0]


@pytest.mark.anyio
async def test_begin_rejects_signed_flow_mismatch_before_protocol_or_persistence(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    registry = _Registry(_device_profile())
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(registry, vault, protocol, clock)

    with pytest.raises(ValueError, match="^oauth_authorization_flow_mismatch$"):
        await broker.begin(_request())

    assert protocol.discover_calls == []
    assert broker.recover() == ((), ())


@pytest.mark.anyio
async def test_public_device_flow_survives_restart_and_prepares_without_a_secret(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    profile = _device_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(registry, vault, protocol, clock)

    start = await broker.begin(_request(authorization_flow=OAuthAuthorizationFlow.DEVICE_CODE))

    assert start.authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE
    assert start.authorization_url == "https://login.example/device?user_code=ABCD-EFGH"
    assert start.user_code == "ABCD-EFGH"
    assert start.poll_interval_seconds == 5
    assert vault.credential_reads == []
    restarted = _broker(registry, vault, protocol, clock)
    with pytest.raises(OAuthProtocolError, match="^oauth_device_authorization_pending$"):
        await restarted.complete_device(start.state_sha256)
    clock.value += 5
    prepared = await restarted.complete_device(start.state_sha256)

    assert prepared.profile_id == profile.profile_id
    assert vault.load_transaction(start.state_sha256).prepared_token is not None
    assert b"opaque-device-code" not in vault.path.read_bytes()


def _callback(start: OAuthAuthorizationStart, *, state: str | None = None) -> str:
    chosen_state = _state(start) if state is None else state
    return (
        f"{start.redirect_uri}?code=opaque-code&state={chosen_state}&iss={quote(_ISSUER, safe='')}"
    )


def _binding(prepared: OAuthPreparedAuthorization) -> BrokerTransportBinding:
    commit = _commit()
    return BrokerTransportBinding(
        plugin_id=prepared.plugin_id,
        app_id=prepared.app_id,
        profile_id=prepared.profile_id,
        profile_revision=prepared.profile_revision,
        partition_key=prepared.partition_key,
        resource_url=_RESOURCE,
        connection_revision=commit.connection_revision,
        capability_revision=commit.capability_revision,
        authority_revision=commit.authority_revision,
        capability_binding_revision=commit.capability_binding_revision,
    )


def _plaintext(vault: OAuthCredentialVault) -> bytes:
    envelope = json.loads(vault.path.read_text(encoding="utf-8"))
    return AESGCM(_KEY).decrypt(
        base64.b64decode(envelope["nonce"], validate=True),
        base64.b64decode(envelope["ciphertext"], validate=True),
        _AAD,
    )


def _rewrite_partition_as_legacy_v1(
    vault: OAuthCredentialVault,
    partition_key: str,
) -> tuple[str, str]:
    payload = json.loads(_plaintext(vault))
    document = payload["records"]
    record = document["tokens"].pop(partition_key)
    value = record["value"]
    legacy = _legacy_partition(vault.load_token_partition(partition_key))
    value["partition_key"] = legacy.partition_key
    for field in ("account_partition", "credential_identity", "migration_status"):
        value.pop(field)
    document["tokens"][legacy.partition_key] = record
    document["account_partitions"] = {}
    document["migrations"] = {}
    # The genuine oldest (pre-wrapper) legacy format predates the
    # registrations map entirely; a live vault write always includes it
    # (empty), so strip it to keep this fixture realistically unpadded.
    document.pop("registrations")
    migrated_binding_revision: str | None = None
    for grant in document["committed_grants"].values():
        if grant["token_partition_key"] != partition_key:
            continue
        grant["token_partition_key"] = legacy.partition_key
        grant.pop("connection_generation")
        tool_binding_revision = grant.pop("capability_binding_revision")
        grant["tool_binding_revision"] = tool_binding_revision
        migrated_binding_revision = sha256(
            json.dumps(
                {
                    "capability_binding_identities": [
                        {
                            "binding_sha256": tool_binding_revision,
                            "capability_kind": "tool",
                        }
                    ]
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    plaintext = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    nonce = b"l" * 12
    envelope = {
        "ciphertext": base64.b64encode(AESGCM(_KEY).encrypt(nonce, plaintext, _AAD)).decode(
            "ascii"
        ),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "version": 1,
    }
    vault.path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    assert migrated_binding_revision is not None
    return legacy.partition_key, migrated_binding_revision


@pytest.mark.anyio
async def test_begin_persists_only_state_hash_and_builds_exact_pkce_request(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)

    start = await broker.begin(_request())

    query = parse_qs(urlsplit(start.authorization_url).query, strict_parsing=True)
    state = query["state"][0]
    state_sha256 = sha256(state.encode("utf-8")).hexdigest()
    transaction = vault.load_transaction(state_sha256)
    assert registry.resolve_calls == [_match()]
    assert vault.credential_reads == ["oauth-client/example.desktop/secret"]
    assert query["client_id"] == ["signed-public-client"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert query["redirect_uri"] == [_REDIRECT]
    assert urlsplit(start.redirect_uri).path == "/oauth/example.desktop/callback"
    assert transaction.client_id == "signed-public-client"
    assert transaction.state_sha256 == state_sha256
    assert transaction.authorization_context_revision == "a" * 64
    assert state.encode() not in _plaintext(vault)
    assert state not in repr(start)
    assert start.authorization_url not in repr(start)


@pytest.mark.anyio
async def test_dynamic_begin_registers_once_and_reuses_the_stored_client_id(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(registry, vault, protocol, clock)

    first = await broker.begin(_request(plugin_id="plugin.alpha"))
    second = await broker.begin(_request(plugin_id="plugin.beta"))

    assert protocol.registration_calls == [profile.profile_id]
    assert parse_qs(urlsplit(first.authorization_url).query)["client_id"] == [
        "dynamic-client-1"
    ]
    assert parse_qs(urlsplit(second.authorization_url).query)["client_id"] == [
        "dynamic-client-1"
    ]
    stored = vault.load_registration(_ISSUER, profile.profile_id)
    assert stored is not None
    assert stored.client_id == "dynamic-client-1"
    assert stored.response_sha256 == sha256(
        b'{"client_id":"dynamic-client-1","registration_access_token":"management-token","registration_client_uri":"https://issuer.example/register/dynamic-client-1"}'
    ).hexdigest()


@pytest.mark.anyio
async def test_invalid_dynamic_client_replaces_registration_without_reusing_old_code(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(registry, vault, protocol, clock)
    start = await broker.begin(_request())
    protocol.exchange_error = OAuthProviderError("oauth_client_invalid")

    with pytest.raises(ValueError, match="^oauth_client_registration_rejected$"):
        await broker.complete(profile_id=start.profile_id, callback_uri=_callback(start))

    assert len(protocol.exchange_calls) == 1
    assert protocol.exchange_calls[0]["client_id"] == "dynamic-client-1"
    assert protocol.registration_calls == [profile.profile_id, profile.profile_id]
    assert vault.is_transaction_consumed(start.state_sha256) is True
    replacement = vault.load_registration(_ISSUER, profile.profile_id)
    assert replacement is not None and replacement.client_id == "dynamic-client-2"
    next_start = await broker.begin(_request())
    assert parse_qs(urlsplit(next_start.authorization_url).query)["client_id"] == [
        "dynamic-client-2"
    ]


@pytest.mark.anyio
async def test_invalid_dynamic_client_requires_reconnect_without_reusing_refresh_token(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(registry, vault, protocol, clock)
    start = await broker.begin(_request())
    prepared = await broker.complete(profile_id=start.profile_id, callback_uri=_callback(start))
    broker.commit(prepared, _commit())
    protocol.refresh_error = OAuthProviderError("oauth_client_invalid")

    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.refresh(prepared.partition_key, force=True)

    assert len(protocol.refresh_calls) == 1
    assert protocol.refresh_calls[0]["client_id"] == "dynamic-client-1"
    assert protocol.registration_calls == [profile.profile_id]
    assert vault.load_registration(_ISSUER, profile.profile_id) is None
    assert vault.load_token_partition(prepared.partition_key).reauthorization_required is True
    next_start = await broker.begin(_request())
    assert protocol.registration_calls == [profile.profile_id, profile.profile_id]
    assert parse_qs(urlsplit(next_start.authorization_url).query)["client_id"] == [
        "dynamic-client-2"
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("replacement_connect", (False, True))
async def test_rejected_dynamic_partition_revokes_with_its_original_client_binding(
    tmp_path: Path,
    clock: _Clock,
    replacement_connect: bool,
) -> None:
    profile = _dynamic_profile()
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(_Registry(profile), vault, protocol, clock)
    start = await broker.begin(_request())
    prepared = await broker.complete(profile_id=start.profile_id, callback_uri=_callback(start))
    broker.commit(prepared, _commit())
    protocol.refresh_error = OAuthProviderError("oauth_client_invalid")
    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.refresh(prepared.partition_key, force=True)

    replacement_start = None
    if replacement_connect:
        replacement_start = await broker.begin(_request())

    result = await broker.revoke(prepared.partition_key, plugin_id="plugin.alpha")

    assert result.local_token_deleted is True
    assert protocol.revoke_calls[-1]["client_id"] == "dynamic-client-1"
    assert protocol.revoke_calls[-1]["token"] == "opaque-refresh"
    if replacement_start is None:
        assert vault.load_registration(_ISSUER, profile.profile_id) is None
    else:
        assert parse_qs(urlsplit(replacement_start.authorization_url).query)["client_id"] == [
            "dynamic-client-2"
        ]
        replacement = vault.load_registration(_ISSUER, profile.profile_id)
        assert replacement is not None and replacement.client_id == "dynamic-client-2"
        assert protocol.deregister_calls == []


@pytest.mark.anyio
async def test_dynamic_prepared_authorization_stays_bound_to_its_registered_client(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    profile = _dynamic_profile()
    vault = _TrackingVault(tmp_path, clock)
    broker = _broker(_Registry(profile), vault, _Protocol(), clock)
    start = await broker.begin(_request())
    prepared = await broker.complete(profile_id=start.profile_id, callback_uri=_callback(start))

    token = broker._validate_prepared_transport(prepared, _binding(prepared))

    assert token.access_token == "opaque-access"


@pytest.mark.anyio
async def test_delete_registration_logs_remote_failure_once_and_removes_local_record(
    tmp_path: Path,
    clock: _Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile = _dynamic_profile()
    vault = _TrackingVault(tmp_path, clock)
    vault.store_registration(
        OAuthClientRegistration(
            authorization_issuer=_ISSUER,
            profile_id=profile.profile_id,
            client_id="dynamic-client-1",
            registration_access_token="management-token",
            registration_client_uri="https://issuer.example/register/dynamic-client-1",
            registered_at=clock.value,
            response_sha256="a" * 64,
        )
    )
    protocol = _Protocol()
    protocol.deregister_error = OAuthNetworkError("oauth_provider_unreachable")
    broker = _broker(_Registry(profile), vault, protocol, clock)

    with caplog.at_level(logging.WARNING):
        await broker.delete_registration(_ISSUER, profile.profile_id)

    assert vault.load_registration(_ISSUER, profile.profile_id) is None
    assert caplog.messages == ["oauth_registration_deletion_failed:oauth_provider_unreachable"]
    assert "dynamic-client-1" not in caplog.text
    assert "management-token" not in caplog.text


@pytest.mark.anyio
async def test_delete_registration_preserves_a_registration_with_a_pending_flow(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    profile = _dynamic_profile()
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(_Registry(profile), vault, protocol, clock)
    await broker.begin(_request())

    await broker.delete_registration(_ISSUER, profile.profile_id)

    assert vault.load_registration(_ISSUER, profile.profile_id) is not None
    assert protocol.deregister_calls == []


@pytest.mark.anyio
async def test_delete_registration_waits_for_every_active_account_using_the_profile(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    profile = _dynamic_profile()
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(_Registry(profile), vault, protocol, clock)
    first_start = await broker.begin(_request(plugin_id="plugin.alpha"))
    first = await broker.complete(
        profile_id=first_start.profile_id,
        callback_uri=_callback(first_start),
    )
    broker.commit(first, _commit())
    second_request = replace(
        _request(plugin_id="plugin.beta"),
        account_key_sha256="2" * 64,
        authorization_context_revision="b" * 64,
    )
    second_start = await broker.begin(second_request)
    second = await broker.complete(
        profile_id=second_start.profile_id,
        callback_uri=_callback(second_start),
    )
    broker.commit(second, replace(_commit(), connection_revision="6" * 64))

    await broker.revoke(first.partition_key, plugin_id="plugin.alpha")
    await broker.delete_registration(_ISSUER, profile.profile_id)

    assert vault.load_registration(_ISSUER, profile.profile_id) is not None
    assert protocol.deregister_calls == []

    await broker.revoke(second.partition_key, plugin_id="plugin.beta")

    assert vault.load_registration(_ISSUER, profile.profile_id) is None
    assert len(protocol.deregister_calls) == 1


@pytest.mark.anyio
async def test_begin_exposes_state_digest_and_recovers_exact_content_free_attempt(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    attempt_context = {
        "execution_policy": "confirm_each",
        "source_constraints": {"workspace": "signed"},
        "descriptor_revision": "6" * 64,
        "package_revision": "7" * 64,
    }
    request = replace(_request(), attempt_context=attempt_context)

    start = await broker.begin(request)
    state = _state(start)
    prepared = await broker.complete(
        profile_id=start.profile_id,
        callback_uri=_callback(start),
    )

    assert start.state_sha256 == sha256(state.encode("utf-8")).hexdigest()
    assert dict(prepared.attempt_context) == {
        **attempt_context,
        "omega_authorization_context_revision": "a" * 64,
    }
    recovered, _receipts = broker.recover()
    assert recovered == (prepared,)


@pytest.mark.anyio
async def test_signed_issuer_account_partition_shares_profiles_and_unions_scopes(
    tmp_path: Path,
    clock: _Clock,
    protocol: _Protocol,
) -> None:
    first_profile = _profile(secret_ref=False)
    second_profile = _profile(
        profile_id="example.mail",
        resource_origin=_RESOURCE_TWO,
        secret_ref=False,
    )
    registry = _MultiRegistry(first_profile, second_profile)
    vault = _TrackingVault(tmp_path, clock)
    vault.store_credential(
        "oauth-client/example.desktop/secret",
        OAuthClientCredential("signed-public-client", "desktop-secret"),
        expected_revision=None,
    )
    broker = OAuthBroker(registry=registry, vault=vault, protocol=protocol, clock=clock)
    first_request = replace(
        _request(plugin_id="plugin.alpha"),
        profile_match=_match_for(),
        scopes=("repo",),
    )
    second_request = replace(
        _request(plugin_id="plugin.beta"),
        profile_match=_match_for(resource_origin=_RESOURCE_TWO),
        scopes=("read:user",),
        authorization_context_revision="b" * 64,
    )

    first_start = await broker.begin(first_request)
    first_prepared = await broker.complete(
        profile_id=first_start.profile_id,
        callback_uri=_callback(first_start),
    )
    broker.commit(first_prepared, replace(_commit(), connection_generation=1))
    second_start = await broker.begin(second_request)
    first_partition_key = first_prepared.partition_key
    second_partition_key = vault.load_transaction(second_start.state_sha256).partition_key

    assert second_partition_key == first_partition_key
    assert parse_qs(urlsplit(second_start.authorization_url).query)["scope"] == ["read:user repo"]
    second_prepared = await broker.complete(
        profile_id=second_start.profile_id,
        callback_uri=_callback(second_start),
    )
    assert second_prepared.scopes == ("read:user",)
    broker.commit(
        second_prepared,
        replace(
            _commit(),
            connection_revision="6" * 64,
            authority_revision="7" * 64,
            connection_generation=1,
        ),
    )

    partition = vault.load_token_partition(first_partition_key)
    assert partition.token.scopes == ("read:user", "repo")
    assert partition.dependent_plugin_ids == ("plugin.alpha", "plugin.beta")
    assert await broker.current_access_token(_binding(first_prepared)) == "opaque-access"
    remaining = await broker.revoke(first_partition_key, plugin_id="plugin.alpha")
    assert remaining.dependents == ("plugin.beta",)
    assert remaining.local_token_deleted is False


@pytest.mark.anyio
async def test_signed_partitions_isolate_account_credential_and_issuer(
    tmp_path: Path,
    clock: _Clock,
    protocol: _Protocol,
) -> None:
    base = _profile()
    other_credential = _profile(
        profile_id="example.other-client",
        resource_origin=_RESOURCE_TWO,
        secret_reference="oauth-client/example.other-client/secret",
    )
    other_issuer = _profile(
        profile_id="example.other-issuer",
        resource_origin="https://other-resource.example/mcp",
        issuer="https://other-issuer.example",
        secret_ref=False,
    )
    registry = _MultiRegistry(base, other_credential, other_issuer)
    vault = _TrackingVault(tmp_path, clock)
    vault.store_credential(
        "oauth-client/example.desktop/secret",
        OAuthClientCredential("signed-public-client", "desktop-secret"),
        expected_revision=None,
    )
    vault.store_credential(
        "oauth-client/example.other-client/secret",
        OAuthClientCredential("signed-public-client", "other-secret"),
        expected_revision=None,
    )
    broker = OAuthBroker(registry=registry, vault=vault, protocol=protocol, clock=clock)

    base_start = await broker.begin(replace(_request(), profile_match=_match_for()))
    other_account = await broker.begin(
        replace(
            _request(plugin_id="plugin.account"),
            profile_match=_match_for(),
            account_key_sha256="8" * 64,
        )
    )
    other_client = await broker.begin(
        replace(
            _request(plugin_id="plugin.credential"),
            profile_match=_match_for(resource_origin=_RESOURCE_TWO),
        )
    )
    other_issuer_start = await broker.begin(
        replace(
            _request(plugin_id="plugin.issuer"),
            profile_match=_match_for(
                resource_origin="https://other-resource.example/mcp",
                issuer="https://other-issuer.example",
            ),
        )
    )
    starts = (base_start, other_account, other_client, other_issuer_start)

    assert len({vault.load_transaction(start.state_sha256).partition_key for start in starts}) == 4


@pytest.mark.anyio
async def test_cimd_uses_exact_metadata_url_without_reading_any_credential(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    registry = _Registry(_profile(OAuthClientMode.CLIENT_METADATA_DOCUMENT))
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()

    start = await _broker(registry, vault, protocol, clock).begin(_request())

    query = parse_qs(urlsplit(start.authorization_url).query, strict_parsing=True)
    assert query["client_id"] == ["https://omega.example/oauth/example-client.json"]
    assert vault.credential_reads == []
    transaction = vault.load_transaction(sha256(_state(start).encode()).hexdigest())
    assert transaction.client_id == "https://omega.example/oauth/example-client.json"


@pytest.mark.anyio
async def test_begin_uses_the_exact_normalized_resource_from_signed_resolution(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    request = replace(
        _request(),
        profile_match=replace(
            _match(),
            authorization_issuer="HTTPS://ISSUER.EXAMPLE:443",
            resource_origin="HTTPS://RESOURCE.EXAMPLE:443/mcp",
        ),
    )

    start = await _broker(registry, vault, protocol, clock).begin(request)

    query = parse_qs(urlsplit(start.authorization_url).query, strict_parsing=True)
    transaction = vault.load_transaction(sha256(_state(start).encode()).hexdigest())
    assert query["resource"] == [_RESOURCE]
    assert transaction.resource_origin == _RESOURCE


@pytest.mark.anyio
@pytest.mark.parametrize(
    "credential",
    [
        None,
        OAuthClientCredential("wrong-public-client", "desktop-secret"),
        OAuthClientCredential("signed-public-client", None),
    ],
)
async def test_preregistered_profile_never_falls_back_or_cross_binds_credentials(
    tmp_path: Path,
    clock: _Clock,
    credential: OAuthClientCredential | None,
) -> None:
    registry = _Registry(_profile())
    vault = _TrackingVault(tmp_path, clock)
    if credential is not None:
        vault.store_credential(
            "oauth-client/example.desktop/secret", credential, expected_revision=None
        )

    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        await _broker(registry, vault, _Protocol(), clock).begin(_request())


@pytest.mark.anyio
async def test_callback_completes_after_restart_then_replay_fails(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    start = await _broker(registry, vault, protocol, clock).begin(_request())
    restarted = _broker(registry, vault, protocol, clock)

    prepared = await restarted.complete(profile_id=start.profile_id, callback_uri=_callback(start))

    assert prepared.plugin_id == "plugin.alpha"
    assert protocol.exchange_calls[0]["code"] == "opaque-code"
    assert protocol.exchange_calls[0]["client_secret"] == "desktop-secret"
    assert "opaque-code" not in repr(prepared)
    assert "opaque-access" not in repr(prepared)
    recovered, receipts = restarted.recover()
    assert recovered == (prepared,)
    assert receipts == ()

    grant = restarted.commit(prepared, _commit())
    assert restarted.recover() == ((), (grant,))
    assert restarted.committed_grant_receipts() == (grant,)
    assert restarted.grant_receipt_state(grant) == "unclaimed"
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        await restarted.complete(profile_id=start.profile_id, callback_uri=_callback(start))
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        restarted.commit(prepared, _commit())
    assert (
        restarted.claim_grant_receipt_exact(grant, grant.authority_revision)
        == "claimed"
    )
    assert restarted.unclaimed_grant_receipts() == ()
    assert restarted.grant_receipt_state(grant) == "claimed"


@pytest.mark.anyio
async def test_prepared_transaction_survives_authorization_ttl_restart_and_commits_once(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    start = await broker.begin(_request())
    prepared = await broker.complete(
        profile_id=start.profile_id,
        callback_uri=_callback(start),
    )
    clock.value = start.expires_at + 1.0
    restarted = _broker(registry, vault, protocol, clock)

    recovered, receipts = restarted.recover()

    assert recovered == (prepared,)
    assert receipts == ()
    grant = restarted.commit(recovered[0], _commit())
    assert vault.load_token_partition(prepared.partition_key).token.access_token == "opaque-access"
    assert len(protocol.exchange_calls) == 1
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        await restarted.complete(
            profile_id=start.profile_id,
            callback_uri=_callback(start),
        )
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        restarted.commit(prepared, _commit())
    assert restarted.unclaimed_grant_receipts() == (grant,)


@pytest.mark.anyio
async def test_discard_connection_attempts_is_exact_and_preserves_other_connections(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    request = _request()
    selected = await broker.begin(request)
    await broker.complete(profile_id=selected.profile_id, callback_uri=_callback(selected))
    siblings = []
    for other in (
        replace(request, plugin_id="plugin.beta"),
        replace(request, app_id="app.other"),
        replace(request, account_key_sha256="2" * 64),
    ):
        start = await broker.begin(other)
        siblings.append(
            await broker.complete(
                profile_id=start.profile_id,
                callback_uri=_callback(start),
            )
        )

    broker.discard_connection_attempts(
        request.plugin_id,
        app_id=request.app_id,
        account_key_sha256=request.account_key_sha256,
    )
    # Cancellation is idempotent and survives reconstruction from the encrypted vault.
    restarted = _broker(registry, vault, protocol, clock)
    restarted.discard_connection_attempts(
        request.plugin_id,
        app_id=request.app_id,
        account_key_sha256=request.account_key_sha256,
    )
    assert {item.state_sha256 for item in restarted.recover()[0]} == {
        item.state_sha256 for item in siblings
    }
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        await restarted.complete(profile_id=selected.profile_id, callback_uri=_callback(selected))
    assert vault.load_credential("oauth-client/example.desktop/secret") is not None


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["base", "issuer", "profile"])
async def test_callback_requires_exact_base_profile_route_and_issuer(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
    kind: str,
) -> None:
    start = await _broker(registry, vault, protocol, clock).begin(_request())
    callback = _callback(start)
    profile_id = start.profile_id
    if kind == "base":
        callback = callback.replace("/callback?", "/other?")
    elif kind == "issuer":
        callback = callback.replace(quote(_ISSUER, safe=""), quote("https://evil.example", safe=""))
    else:
        profile_id = "other.desktop"

    with pytest.raises(ValueError, match="^oauth_callback_(invalid|mismatch)$"):
        await _broker(registry, vault, protocol, clock).complete(
            profile_id=profile_id, callback_uri=callback
        )
    assert protocol.exchange_calls == []


@pytest.mark.anyio
async def test_denial_consumes_state_without_exchanging_code(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    start = await broker.begin(_request())
    denial = (
        f"{start.redirect_uri}?error=access_denied&state={_state(start)}&"
        f"iss={quote(_ISSUER, safe='')}"
    )

    with pytest.raises(ValueError, match="^oauth_authorization_denied$"):
        await broker.complete(profile_id=start.profile_id, callback_uri=denial)
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        await broker.complete(profile_id=start.profile_id, callback_uri=denial)
    assert protocol.exchange_calls == []


@pytest.mark.anyio
async def test_expired_superseded_and_profile_drift_fail_closed(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    first = await broker.begin(_request())
    second = await broker.begin(_request())
    with pytest.raises(ValueError, match="^oauth_transaction_superseded$"):
        await broker.complete(profile_id=first.profile_id, callback_uri=_callback(first))

    registry.exact_available = False
    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        await broker.complete(profile_id=second.profile_id, callback_uri=_callback(second))
    registry.exact_available = True
    clock.value = second.expires_at
    with pytest.raises(ValueError, match="^oauth_transaction_expired$"):
        await broker.complete(profile_id=second.profile_id, callback_uri=_callback(second))


@pytest.mark.anyio
async def test_shared_partition_plugins_keep_independent_attempt_lanes_across_restart(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    alpha = await broker.begin(_request(plugin_id="plugin.alpha"))
    beta = await broker.begin(_request(plugin_id="plugin.beta"))
    restarted = _broker(registry, vault, protocol, clock)

    alpha_prepared = await restarted.complete(
        profile_id=alpha.profile_id,
        callback_uri=_callback(alpha),
    )
    beta_prepared = await restarted.complete(
        profile_id=beta.profile_id,
        callback_uri=_callback(beta),
    )

    assert alpha_prepared.plugin_id == "plugin.alpha"
    assert beta_prepared.plugin_id == "plugin.beta"
    assert alpha_prepared.partition_key == beta_prepared.partition_key
    assert alpha_prepared.state_sha256 != beta_prepared.state_sha256
    recovered, _receipts = restarted.recover()
    assert {item.plugin_id for item in recovered} == {"plugin.alpha", "plugin.beta"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization_issuer", "https://evil.example"),
        ("resource_origin", "https://evil.example/mcp"),
        ("authorization_context_revision", "b" * 64),
    ],
)
async def test_transaction_binding_drift_is_rejected_before_exchange(
    tmp_path: Path,
    registry: _Registry,
    protocol: _Protocol,
    clock: _Clock,
    field: str,
    value: str,
) -> None:
    source_vault = _TrackingVault(tmp_path / "source", clock)
    source_vault.store_credential(
        "oauth-client/example.desktop/secret",
        OAuthClientCredential("signed-public-client", "desktop-secret"),
        expected_revision=None,
    )
    source = _broker(registry, source_vault, protocol, clock)
    start = await source.begin(_request())
    transaction = source_vault.load_transaction(sha256(_state(start).encode()).hexdigest())
    drifted_vault = _TrackingVault(tmp_path / "drifted", clock)
    drifted_vault.store_credential(
        "oauth-client/example.desktop/secret",
        OAuthClientCredential("signed-public-client", "desktop-secret"),
        expected_revision=None,
    )
    drifted_vault.begin_transaction(replace(transaction, **{field: value}))

    with pytest.raises(ValueError, match="^oauth_transaction_binding_drift$"):
        await _broker(registry, drifted_vault, protocol, clock).complete(
            profile_id=start.profile_id, callback_uri=_callback(start)
        )
    assert protocol.exchange_calls == []


@pytest.mark.anyio
async def test_concurrent_callback_completion_exchanges_once(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    start = await broker.begin(_request())
    protocol.exchange_entered = asyncio.Event()
    protocol.exchange_release = asyncio.Event()
    calls = [
        asyncio.create_task(
            broker.complete(profile_id=start.profile_id, callback_uri=_callback(start))
        )
        for _ in range(2)
    ]
    await asyncio.wait_for(protocol.exchange_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert len(protocol.exchange_calls) == 1
    protocol.exchange_release.set()

    first, second = await asyncio.gather(*calls)
    assert first == second
    assert len(protocol.exchange_calls) == 1


async def _connected(
    broker: OAuthBroker,
    request: OAuthConnectionRequest,
) -> tuple[OAuthPreparedAuthorization, BrokerTransportBinding]:
    start = await broker.begin(request)
    prepared = await broker.complete(profile_id=start.profile_id, callback_uri=_callback(start))
    broker.commit(prepared, _commit())
    return prepared, _binding(prepared)


@pytest.mark.anyio
async def test_current_access_token_revalidates_every_transport_binding(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    prepared, binding = await _connected(broker, _request())
    clock.value = 101.0
    assert await broker.current_access_token(binding) == "opaque-access"

    mutations = {
        "plugin_id": "plugin.forged",
        "app_id": "app.forged",
        "profile_id": "other.desktop",
        "profile_revision": "sha256:" + "f" * 64,
        "partition_key": "f" * 64,
        "resource_url": "https://evil.example/mcp",
        "connection_revision": "f" * 64,
        "capability_revision": "f" * 64,
        "authority_revision": "f" * 64,
        "capability_binding_revision": "f" * 64,
    }
    for field, value in mutations.items():
        with pytest.raises(ValueError, match="^oauth_transport_binding_invalid$"):
            await broker.current_access_token(replace(binding, **{field: value}))
    assert prepared.partition_key == binding.partition_key


@pytest.mark.anyio
async def test_concurrent_refresh_is_serialized_once_per_partition(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    _prepared, binding = await _connected(broker, _request())
    clock.value = 200.0
    protocol.refresh_entered = asyncio.Event()
    protocol.refresh_release = asyncio.Event()
    calls = [asyncio.create_task(broker.current_access_token(binding)) for _ in range(2)]
    await asyncio.wait_for(protocol.refresh_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert len(protocol.refresh_calls) == 1
    protocol.refresh_release.set()

    assert await asyncio.gather(*calls) == ["refreshed-access", "refreshed-access"]
    assert len(protocol.refresh_calls) == 1


@pytest.mark.anyio
async def test_refresh_rejection_marks_grants_for_reauthorization_without_retry(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    _prepared, binding = await _connected(broker, _request())
    clock.value = 200.0
    protocol.refresh_error = OAuthProviderError("oauth_provider_rejected")

    with pytest.raises(OAuthProviderError, match="^oauth_provider_rejected$"):
        await broker.current_access_token(binding)
    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.current_access_token(binding)
    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.refresh(binding.partition_key)
    assert len(protocol.refresh_calls) == 1


@pytest.mark.anyio
async def test_legacy_partition_marker_blocks_transport_and_refresh(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    prepared, binding = await _connected(broker, _request())
    grant = vault.claim_committed_grants()[0]
    legacy = _legacy_partition(vault.load_token_partition(prepared.partition_key))
    legacy_grant = replace(grant, token_partition_key=legacy.partition_key)
    legacy_binding = replace(binding, partition_key=legacy.partition_key)
    monkeypatch.setattr(vault, "load_token_partition", lambda _key: legacy)
    monkeypatch.setattr(
        vault,
        "validate_transport_grant",
        lambda **_values: legacy_grant,
    )

    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.current_access_token(legacy_binding)
    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.refresh(legacy.partition_key, force=True)

    assert protocol.refresh_calls == []


@pytest.mark.anyio
async def test_migrated_legacy_partition_revokes_through_real_vault_and_broker(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    prepared, binding = await _connected(broker, _request())
    legacy_key, migrated_binding_revision = _rewrite_partition_as_legacy_v1(
        vault, prepared.partition_key
    )
    restarted_vault = _TrackingVault(vault.path.parent, clock)
    restarted = _broker(registry, restarted_vault, protocol, clock)
    legacy_binding = replace(
        binding,
        partition_key=legacy_key,
        capability_binding_revision=migrated_binding_revision,
    )

    migrated = restarted_vault.load_token_partition(legacy_key)
    assert migrated.migration_status == "legacy_v1"
    assert migrated.reauthorization_required is True
    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await restarted.current_access_token(legacy_binding)
    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await restarted.refresh(legacy_key, force=True)

    result = await restarted.revoke(legacy_key, plugin_id="plugin.alpha")

    assert result.local_token_deleted is True
    assert len(protocol.revoke_calls) == 1
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        restarted_vault.load_token_partition(legacy_key)


@pytest.mark.anyio
async def test_migrated_legacy_revoke_failure_retries_after_restart(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    prepared, binding = await _connected(broker, _request())
    legacy_key, migrated_binding_revision = _rewrite_partition_as_legacy_v1(
        vault, prepared.partition_key
    )
    restarted_vault = _TrackingVault(vault.path.parent, clock)
    restarted = _broker(registry, restarted_vault, protocol, clock)
    legacy_binding = replace(
        binding,
        partition_key=legacy_key,
        capability_binding_revision=migrated_binding_revision,
    )
    protocol.revoke_error = OAuthNetworkError("oauth_provider_unreachable")

    with pytest.raises(OAuthNetworkError, match="^oauth_provider_unreachable$"):
        await restarted.revoke(legacy_key, plugin_id="plugin.alpha")

    pending = restarted_vault.load_token_partition(legacy_key)
    assert pending.migration_status == "legacy_v1"
    assert pending.revocation_pending_plugin_id == "plugin.alpha"
    with pytest.raises(ValueError, match="^oauth_revocation_pending$"):
        await restarted.current_access_token(legacy_binding)
    with pytest.raises(ValueError, match="^oauth_revocation_pending$"):
        await restarted.refresh(legacy_key, force=True)
    with pytest.raises(ValueError, match="^oauth_revocation_pending$"):
        restarted_vault.replace_token_partition(
            legacy_key,
            pending.token,
            expected_revision=pending.revision,
        )

    after_failure_vault = _TrackingVault(vault.path.parent, clock)
    after_failure = _broker(registry, after_failure_vault, protocol, clock)
    protocol.revoke_error = None
    result = await after_failure.revoke(legacy_key, plugin_id="plugin.alpha")

    assert result.local_token_deleted is True
    assert len(protocol.revoke_calls) == 2
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        after_failure_vault.load_token_partition(legacy_key)


@pytest.mark.anyio
async def test_refresh_rejection_can_reauthorize_exact_shared_partition_after_restarts(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    _first, first_binding = await _connected(broker, _request(plugin_id="plugin.alpha"))
    _second, second_binding = await _connected(broker, _request(plugin_id="plugin.beta"))
    for receipt in vault.claim_committed_grants():
        vault.mark_grant_claimed(
            receipt.receipt_id,
            authority_revision=receipt.authority_revision,
        )
    clock.value = 200.0
    protocol.refresh_error = OAuthProviderError("oauth_provider_rejected")

    with pytest.raises(OAuthProviderError, match="^oauth_provider_rejected$"):
        await broker.current_access_token(first_binding)
    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.current_access_token(second_binding)

    protocol.refresh_error = None
    protocol.exchange_access_token = "reauthorized-access"
    protocol.exchange_refresh_token = "reauthorized-refresh"
    protocol.exchange_expires_at = 500.0
    restarted = _broker(registry, vault, protocol, clock)
    start = await restarted.begin(_request(plugin_id="plugin.alpha"))
    prepared = await restarted.complete(
        profile_id=start.profile_id,
        callback_uri=_callback(start),
    )

    after_exchange_restart = _broker(registry, vault, protocol, clock)
    recovered, receipts = after_exchange_restart.recover()
    assert recovered == (prepared,)
    assert receipts == ()
    after_exchange_restart.commit(recovered[0], _commit())

    partition = vault.load_token_partition(prepared.partition_key)
    assert partition.dependent_plugin_ids == ("plugin.alpha", "plugin.beta")
    assert partition.reauthorization_required is False
    assert await after_exchange_restart.current_access_token(first_binding) == (
        "reauthorized-access"
    )
    assert await after_exchange_restart.current_access_token(second_binding) == (
        "reauthorized-access"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("forgery", "reason"),
    [
        ("grant-scopes", "oauth_transport_binding_invalid"),
        ("grant-connection-revision", "oauth_transport_binding_invalid"),
        ("grant-capability-revision", "oauth_transport_binding_invalid"),
        ("grant-authority-revision", "oauth_transport_binding_invalid"),
        ("grant-capability-binding-revision", "oauth_transport_binding_invalid"),
        ("partition-reauthorization", "oauth_reauthorization_required"),
    ],
)
async def test_current_access_token_cross_checks_validated_grant_and_partition_state(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
    reason: str,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    prepared, binding = await _connected(broker, _request())
    grant = vault.claim_committed_grants()[0]
    partition = vault.load_token_partition(prepared.partition_key)
    if forgery == "grant-scopes":
        monkeypatch.setattr(
            vault,
            "validate_transport_grant",
            lambda **_values: replace(grant, scopes=("forged",)),
        )
    elif forgery.startswith("grant-"):
        field = forgery.removeprefix("grant-").replace("-", "_")
        monkeypatch.setattr(
            vault,
            "validate_transport_grant",
            lambda **_values: replace(grant, **{field: "f" * 64}),
        )
    else:
        monkeypatch.setattr(
            vault,
            "load_token_partition",
            lambda _partition_key: replace(partition, reauthorization_required=True),
        )

    with pytest.raises(ValueError, match=f"^{reason}$"):
        await broker.current_access_token(binding)


@pytest.mark.anyio
async def test_refresh_unreachable_preserves_token_and_provider_neutral_error(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    prepared, binding = await _connected(broker, _request())
    clock.value = 200.0
    protocol.refresh_error = OAuthNetworkError("oauth_provider_unreachable")

    with pytest.raises(OAuthNetworkError, match="^oauth_provider_unreachable$"):
        await broker.current_access_token(binding)
    assert vault.load_token_partition(prepared.partition_key).token.access_token == "opaque-access"


@pytest.mark.anyio
async def test_shared_revoke_keeps_token_for_remaining_dependent(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    first, first_binding = await _connected(broker, _request(plugin_id="plugin.alpha"))
    second, second_binding = await _connected(broker, _request(plugin_id="plugin.beta"))
    assert first.partition_key == second.partition_key

    result = await broker.revoke(first.partition_key, plugin_id="plugin.alpha")

    assert result.revoked_provider_token is False
    assert result.dependents == ("plugin.beta",)
    assert result.local_token_deleted is False
    assert protocol.revoke_calls == []
    with pytest.raises(ValueError, match="^oauth_transport_binding_invalid$"):
        await broker.current_access_token(first_binding)
    assert await broker.current_access_token(second_binding) == "opaque-access"


@pytest.mark.anyio
async def test_last_dependent_revocation_is_recoverable_and_retains_token_until_success(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    broker = _broker(registry, vault, protocol, clock)
    prepared, binding = await _connected(broker, _request())
    protocol.revoke_error = OAuthNetworkError("oauth_provider_unreachable")

    with pytest.raises(OAuthNetworkError, match="^oauth_provider_unreachable$"):
        await broker.revoke(prepared.partition_key, plugin_id="plugin.alpha")
    assert vault.load_token_partition(prepared.partition_key).token.access_token == "opaque-access"
    with pytest.raises(ValueError, match="^oauth_revocation_pending$"):
        await broker.current_access_token(binding)

    protocol.revoke_error = None
    restarted = _broker(registry, vault, protocol, clock)
    result = await restarted.revoke(prepared.partition_key, plugin_id="plugin.alpha")
    assert result.revoked_provider_token is True
    assert result.dependents == ()
    assert result.local_token_deleted is True
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        vault.load_token_partition(prepared.partition_key)
    assert len(protocol.revoke_calls) == 2


@pytest.mark.anyio
async def test_a_provider_without_revocation_still_loses_omegas_copy(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    """RFC 7009 revocation is OPTIONAL and five shipped providers omit it.

    GitHub, Slack, Google, Hugging Face and Netlify publish no
    `revocation_endpoint` at the issuer Omega resolves. Raising here happened
    BEFORE `finalize_revocation`, so uninstalling any of them was impossible AND
    Omega kept the credential it had just been told to destroy -- the worse of
    both outcomes, and one no retry could ever fix, because a provider does not
    grow a revocation endpoint. The operator hit it on all six installed
    plugins at once (live 2026-09-07).
    """
    broker = _broker(registry, vault, protocol, clock)
    prepared, _binding = await _connected(broker, _request())
    protocol.revoke_receipt = OAuthRevocationReceipt(
        endpoint_advertised=False,
        provider_request_sent=False,
    )

    result = await broker.revoke(prepared.partition_key, plugin_id="plugin.alpha")

    assert result.local_token_deleted is True
    # Reported honestly: the provider was never told.
    assert result.revoked_provider_token is False
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        vault.load_token_partition(prepared.partition_key)


@pytest.mark.anyio
async def test_a_refused_revocation_still_loses_omegas_copy(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    """Google answers 400 for a token that already expired.

    RFC 7009 s2.2 says a server answers 200 even for a token it does not
    recognise, so the usual cause of a refusal is a token that is already dead.
    Gmail's was still live and uninstalled; Calendar's and Drive's were not, and
    the refusal pinned both plugins in place.
    """
    broker = _broker(registry, vault, protocol, clock)
    prepared, _binding = await _connected(broker, _request())
    protocol.revoke_error = OAuthProviderError("oauth_provider_rejected")

    result = await broker.revoke(
        prepared.partition_key,
        plugin_id="plugin.alpha",
        tolerate_provider_refusal=True,
    )

    assert result.local_token_deleted is True
    assert result.revoked_provider_token is False
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        vault.load_token_partition(prepared.partition_key)


@pytest.mark.anyio
async def test_an_advertised_endpoint_that_was_never_called_is_still_refused(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    """The one case that stays an error, because it is a contradiction.

    The provider HAS a revocation endpoint and nothing was sent to it. That is
    Omega failing to do something it can do, not a provider that cannot -- so
    the token is retained and the attempt stays retryable.
    """
    broker = _broker(registry, vault, protocol, clock)
    prepared, binding = await _connected(broker, _request())
    protocol.revoke_receipt = OAuthRevocationReceipt(
        endpoint_advertised=True,
        provider_request_sent=False,
    )

    with pytest.raises(ValueError, match="^oauth_revocation_unsupported$"):
        await broker.revoke(prepared.partition_key, plugin_id="plugin.alpha")

    pending = vault.load_token_partition(prepared.partition_key)
    assert pending.revocation_pending_plugin_id == "plugin.alpha"
    assert pending.token.access_token == "opaque-access"
    with pytest.raises(ValueError, match="^oauth_revocation_pending$"):
        await broker.current_access_token(binding)

    protocol.revoke_receipt = OAuthRevocationReceipt(
        endpoint_advertised=True,
        provider_request_sent=True,
    )
    result = await _broker(registry, vault, protocol, clock).revoke(
        prepared.partition_key,
        plugin_id="plugin.alpha",
    )
    assert result.local_token_deleted is True


def test_public_result_reprs_are_content_free() -> None:
    prepared = OAuthPreparedAuthorization(
        profile_id="example.desktop",
        plugin_id="plugin.alpha",
        app_id="app.alpha",
        account_key_sha256="1" * 64,
        state_sha256="2" * 64,
        partition_key="3" * 64,
        profile_revision="sha256:" + "4" * 64,
        authorization_context_revision="5" * 64,
        scopes=("repo",),
    )
    rendered = repr(prepared)
    assert "app.alpha" not in rendered
    assert "repo" not in rendered
    assert "1" * 64 not in rendered


@pytest.mark.anyio
async def test_relay_redirect_completes_only_from_the_loopback_landing(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    """An HTTPS relay redirect is registered and exchanged as-is, but the
    browser lands on the loopback HUD (the relay's 302 target) and only that
    landing completes the flow; a callback presented at the relay itself or
    at any other origin is rejected."""
    from mcp_platform.oauth_profiles import callback_landing_uri

    relay = "https://plugins.omega2ai.com/oauth/example.desktop/callback"
    profile = replace(
        _profile(OAuthClientMode.DYNAMIC_REGISTRATION),
        redirect_uri=relay,
        registration=OAuthDynamicRegistrationTemplate(
            client_name="Omega",
            redirect_uris=(relay,),
            token_endpoint_auth_method="none",
            grant_types=("authorization_code", "refresh_token"),
            response_types=("code",),
            scope="repo read:user",
            software_id="org.omega2ai.desktop",
        ),
    )
    profile = replace(profile, revision=computed_profile_revision(profile))
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(_Registry(profile), vault, protocol, clock)

    start = await broker.begin(_request())

    assert start.redirect_uri == relay
    assert parse_qs(urlsplit(start.authorization_url).query)["redirect_uri"] == [relay]
    landing = callback_landing_uri(relay)
    assert landing == "http://127.0.0.1:13310/oauth/example.desktop/callback"
    query = f"?code=opaque-code&state={_state(start)}&iss={quote(_ISSUER, safe='')}"
    for wrong in (relay, "http://127.0.0.1:13311/oauth/example.desktop/callback"):
        with pytest.raises(ValueError, match="^oauth_callback_invalid$"):
            await broker.complete(profile_id=start.profile_id, callback_uri=wrong + query)
    assert protocol.exchange_calls == []

    prepared = await broker.complete(profile_id=start.profile_id, callback_uri=landing + query)

    assert prepared.profile_id == profile.profile_id
    assert len(protocol.exchange_calls) == 1


@pytest.mark.anyio
async def test_a_dynamically_issued_secret_is_stored_and_handed_back(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    """Supabase issues a secret at registration and requires it at the token endpoint.

    The broker threw it away with the rest of the response and handed back
    ``OAuthClientCredential(client_id, None)``, so the exchange went out
    unauthenticated and Supabase answered 422 -- the plugin could register and
    then never connect.
    """
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    protocol.registered_client_secret = "issued-secret"
    broker = _broker(registry, vault, protocol, clock)

    started = await broker.begin(_request(plugin_id="plugin.alpha"))

    # A secret must never reach a browser redirect. The authorization request
    # carries a client_id, redirect_uri, scopes, state, challenge and resource,
    # and the secret belongs only in the token request.
    assert "issued-secret" not in started.authorization_url
    assert "client_secret" not in parse_qs(urlsplit(started.authorization_url).query)

    stored = vault.load_registration(_ISSUER, profile.profile_id)
    assert stored is not None
    assert stored.client_secret == "issued-secret"
    # The digest identifies WHICH registration was stored; a credential does not
    # belong in one, so it stays out.
    assert "issued-secret" not in stored.response_sha256
    assert "issued-secret" not in repr(stored)

    # A second begin reuses the stored registration, secret included, without
    # registering again.
    reused = broker._stored_dynamic_credential(profile)
    assert reused.client_secret == "issued-secret"
    assert protocol.registration_calls == [profile.profile_id]


@pytest.mark.anyio
async def test_an_expired_client_secret_registers_again(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    """A confidential client whose secret has expired cannot exchange anything.

    Nothing else in the flow would notice before the token endpoint refused it,
    so the expiry the server stated is the only warning there is.
    """
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    protocol.registered_client_secret = "first-secret"
    protocol.registered_secret_expires_at = clock.value + 10.0
    broker = _broker(registry, vault, protocol, clock)

    await broker.begin(_request(plugin_id="plugin.alpha"))
    assert protocol.registration_calls == [profile.profile_id]

    clock.value += 60.0
    protocol.registered_client_secret = "second-secret"
    protocol.registered_secret_expires_at = None
    await broker.begin(_request(plugin_id="plugin.beta"))

    assert protocol.registration_calls == [profile.profile_id, profile.profile_id]
    stored = vault.load_registration(_ISSUER, profile.profile_id)
    assert stored is not None
    assert stored.client_secret == "second-secret"


@pytest.mark.anyio
async def test_a_public_dynamic_registration_stores_no_secret(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    """The ordinary case, and the one every connected plugin uses today."""
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(registry, vault, protocol, clock)

    await broker.begin(_request(plugin_id="plugin.alpha"))

    stored = vault.load_registration(_ISSUER, profile.profile_id)
    assert stored is not None
    assert stored.client_secret is None
    assert broker._stored_dynamic_credential(profile).client_secret is None


@pytest.mark.anyio
async def test_a_registration_stored_without_a_secret_is_replaced(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    """Every registration already on disk predates Omega keeping a secret.

    Reusing one against a server that requires client authentication would send
    an unauthenticated exchange forever, and nothing would ever prompt a fresh
    registration -- the operator's Supabase entry was in exactly that state.
    """
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = _broker(registry, vault, protocol, clock)

    # A public registration, exactly as it was stored before.
    await broker.begin(_request(plugin_id="plugin.alpha"))
    assert vault.load_registration(_ISSUER, profile.profile_id).client_secret is None

    # The server is now known to require client authentication.
    protocol.requires_client_authentication = True
    protocol.registered_client_secret = "issued-secret"
    await broker.begin(_request(plugin_id="plugin.beta"))

    assert protocol.registration_calls == [profile.profile_id, profile.profile_id]
    stored = vault.load_registration(_ISSUER, profile.profile_id)
    assert stored is not None
    assert stored.client_secret == "issued-secret"


@pytest.mark.anyio
async def test_a_refused_revocation_is_retryable_for_a_retirement(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    """The other caller wants the opposite.

    Retiring a predecessor during an update is not an instruction to let go --
    the plugin is staying and the old token really should be revoked -- so a
    refusal propagates and the attempt survives for a retry.
    """
    broker = _broker(registry, vault, protocol, clock)
    prepared, _binding = await _connected(broker, _request())
    protocol.revoke_error = OAuthProviderError("oauth_provider_rejected")

    with pytest.raises(OAuthProviderError, match="^oauth_provider_rejected$"):
        await broker.revoke(prepared.partition_key, plugin_id="plugin.alpha")

    assert vault.load_token_partition(prepared.partition_key) is not None


@pytest.mark.anyio
async def test_a_server_that_demands_a_secret_and_issues_none_is_asked_once(
    tmp_path: Path,
    clock: _Clock,
) -> None:
    """Miro advertises only confidential methods and issues no secret.

    The self-healing upgrade assumed that asking again would produce one, so
    every sign-in deleted a working registration and made a new one -- Miro's
    `POST /register` answered 201 on every attempt, and the condition never
    cleared. Asking once is the repair; asking forever is a loop.
    """
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    protocol.requires_client_authentication = True
    protocol.registered_client_secret = None
    broker = _broker(registry, vault, protocol, clock)

    await broker.begin(_request(plugin_id="plugin.alpha"))
    await broker.begin(_request(plugin_id="plugin.beta"))
    await broker.begin(_request(plugin_id="plugin.gamma"))

    assert protocol.registration_calls == [profile.profile_id]
    stored = vault.load_registration(_ISSUER, profile.profile_id)
    assert stored is not None
    assert stored.client_secret is None
    assert stored.secret_upgrade_attempted is True


@pytest.mark.anyio
async def test_forced_refresh_of_a_live_unrefreshable_token_does_not_condemn_it(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    """A credential that cannot be refreshed is not a credential that is dead.

    Marking a partition is durable and says the operator must sign in again.
    A token with no expiry and no refresh token is perfectly usable -- it simply
    cannot be refreshed -- so forcing a refresh on it is the caller's error, not
    evidence against the credential.

    Live cost (2026-09-08): a maintainer sweep forced a refresh across every
    installed plugin to capture their surfaces, and marked Slack's working
    non-expiring connection. Supabase's mark then blocked its own update
    rollback, which stranded the lifecycle and stopped the HUD from starting.
    """

    protocol.exchange_refresh_token = None
    protocol.exchange_expires_at = None
    broker = _broker(registry, vault, protocol, clock)
    prepared, _binding = await _connected(broker, _request())

    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.refresh(prepared.partition_key, force=True)

    # The refusal stands, but the connection is not condemned for it.
    partition = vault.load_token_partition(prepared.partition_key)
    assert partition.reauthorization_required is False
    assert [
        receipt.status
        for receipt in vault.committed_grant_receipts()
        if receipt.token_partition_key == prepared.partition_key
    ] == ["active"]


@pytest.mark.anyio
async def test_forced_refresh_of_an_expired_unrefreshable_token_still_condemns_it(
    registry: _Registry,
    vault: _TrackingVault,
    protocol: _Protocol,
    clock: _Clock,
) -> None:
    """The mark is still right when the credential really is spent."""

    protocol.exchange_refresh_token = None
    protocol.exchange_expires_at = 200.0
    broker = _broker(registry, vault, protocol, clock)
    prepared, _binding = await _connected(broker, _request())
    clock.value = 300.0

    with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
        await broker.refresh(prepared.partition_key, force=True)

    assert vault.load_token_partition(prepared.partition_key).reauthorization_required
