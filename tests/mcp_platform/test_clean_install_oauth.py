"""Clean-install OAuth must not depend on the publisher's vault."""

import base64
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from mcp_platform.oauth_broker import OAuthBroker
from mcp_platform.oauth_profiles import (
    OAuthClientMode,
    OAuthProfile,
    OAuthProfileMatchInput,
    computed_profile_revision,
)
from mcp_platform.oauth_vault import OAuthClientCredential, OAuthCredentialVault
from mcp_platform.oauth_protocol import OAuthRevocationReceipt, OAuthTokenResponse
from scripts.provision_oauth_profiles import load_oauth_providers, provision_oauth_profiles
from tests.scripts.test_provision_oauth_profiles import _FIXTURE_SEED, _fixture_ring
from tests.mcp_platform.test_oauth_broker import (
    _Clock,
    _Protocol,
    _Registry,
    _TrackingVault,
    _binding,
    _commit,
    _connected,
    _profile as broker_profile,
    _request,
)


def _profile(
    profile_id: str,
    *,
    mode: OAuthClientMode = OAuthClientMode.PREREGISTERED,
    secret_ref: str | None = None,
) -> OAuthProfile:
    profile = OAuthProfile(
        schema_version=1,
        profile_id=profile_id,
        catalog_signer="omega.featured",
        allowed_plugin_publishers=("omega.first_party",),
        app_publisher="example",
        provider_id="example.official",
        authorization_issuer="https://issuer.example",
        resource_origins=("https://resource.example/mcp",),
        client_mode=mode,
        client_id=(
            "public-client"
            if mode in (OAuthClientMode.PREREGISTERED, OAuthClientMode.HOSTED_BRIDGE)
            else None
        ),
        client_secret_ref=secret_ref,
        client_metadata_document_url=(
            "https://issuer.example/client.json"
            if mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT
            else None
        ),
        redirect_uri=f"http://127.0.0.1:13310/oauth/{profile_id}/callback",
        account_partition="issuer_account",
        revision="sha256:" + "0" * 64,
    )
    return replace(profile, revision=computed_profile_revision(profile))


def _broker(path: Path) -> OAuthBroker:
    return OAuthBroker(
        registry=object(),
        vault=OAuthCredentialVault(path, key_provider=lambda: b"k" * 32),
        protocol=object(),
    )


def test_empty_install_uses_signed_public_client_without_secret(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    profile = _profile("public.desktop")

    assert broker._credential_for(profile) == OAuthClientCredential("public-client", None)


def test_empty_install_metadata_client_has_no_developer_secret(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    profile = _profile("metadata.desktop", mode=OAuthClientMode.CLIENT_METADATA_DOCUMENT)

    assert broker._credential_for(profile) is None


def test_confidential_profile_never_reads_another_profiles_secret(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    broker._vault.store_credential(
        "oauth-client/other.desktop/secret",
        OAuthClientCredential("public-client", "other-secret"),
        expected_revision=None,
    )
    profile = _profile("confidential.desktop", secret_ref="oauth-client/confidential.desktop/secret")

    with pytest.raises(ValueError, match="^oauth_profile_unavailable$") as unavailable:
        broker._credential_for(profile)
    assert type(unavailable.value).__name__ == "OAuthProfileUnavailable"


def test_signed_profile_cannot_name_another_profiles_secret(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    broker._vault.store_credential(
        "oauth-client/other.desktop/secret",
        OAuthClientCredential("public-client", "other-secret"),
        expected_revision=None,
    )
    profile = _profile("confidential.desktop", secret_ref="oauth-client/other.desktop/secret")

    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        broker._credential_for(profile)


def test_hosted_profile_is_unavailable_without_bridge(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    profile = _profile("hosted.desktop", mode=OAuthClientMode.HOSTED_BRIDGE)

    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        broker._credential_for(profile)


def test_hosted_profile_uses_only_its_signed_client_id_with_bridge(tmp_path: Path) -> None:
    broker = OAuthBroker(
        registry=object(),
        vault=OAuthCredentialVault(tmp_path, key_provider=lambda: b"k" * 32),
        protocol=object(),
        hosted_bridge=object(),
    )
    profile = _profile("hosted.desktop", mode=OAuthClientMode.HOSTED_BRIDGE)

    assert broker._credential_for(profile) == OAuthClientCredential("public-client", None)


@pytest.mark.anyio
async def test_hosted_begin_without_bridge_creates_no_attempt(tmp_path: Path) -> None:
    profile = broker_profile(secret_ref=False)
    profile = replace(profile, client_mode=OAuthClientMode.HOSTED_BRIDGE)
    profile = replace(profile, revision=computed_profile_revision(profile))
    clock = _Clock()
    vault = _TrackingVault(tmp_path, clock)
    broker = OAuthBroker(registry=_Registry(profile), vault=vault, protocol=_Protocol(), clock=clock)

    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        await broker.begin(_request())
    assert broker.recover() == ((), ())


@pytest.mark.anyio
async def test_every_signed_dynamic_or_metadata_profile_starts_in_empty_vault(
    tmp_path: Path,
) -> None:
    providers = load_oauth_providers(
        Path("plugin_platform/marketplace_build/oauth_providers.json")
    )
    registry = provision_oauth_profiles(
        providers=providers,
        catalog_path=tmp_path / "signed-catalog.json",
        signing_seed=_FIXTURE_SEED,
        key_ring=_fixture_ring(),
    ).registry
    clock = _Clock()
    vault = _TrackingVault(tmp_path / "empty-vault", clock)
    protocol = _Protocol()
    protocol.registered_client_ids = [f"install-client-{index}" for index in range(len(providers))]
    broker = OAuthBroker(registry=registry, vault=vault, protocol=protocol, clock=clock)
    selected = tuple(
        registry.resolve_by_profile_id(item.profile_id) for item in providers
    )
    selected = tuple(
        profile for profile in selected
        if profile.client_mode in (
            OAuthClientMode.DYNAMIC_REGISTRATION,
            OAuthClientMode.CLIENT_METADATA_DOCUMENT,
        )
    )
    assert selected
    for profile in selected:
        request = replace(
            _request(plugin_id=f"plugin.{profile.profile_id}"),
            profile_match=OAuthProfileMatchInput(
                catalog_signer=profile.catalog_signer,
                plugin_publisher=profile.allowed_plugin_publishers[0],
                app_publisher=profile.app_publisher,
                provider_id=profile.provider_id,
                authorization_issuer=profile.authorization_issuer,
                resource_origin=profile.resource_origins[0],
            ),
        )
        start = await broker.begin(request)
        assert start.profile_id == profile.profile_id
    assert not vault.credential_reads


class _Bridge:
    def __init__(self) -> None:
        self.profile_id = ""
        self.state_digest = ""
        self.challenge = ""
        self.redeemed = False
        self.authorization_url_override: str | None = None
        self.scopes: tuple[str, ...] = ()
        self.redeem_scopes: tuple[str, ...] = ()
        self.refresh_calls: list[tuple[str, str, str, tuple[str, ...]]] = []
        self.revoke_calls: list[tuple[str, str, str]] = []
        self.refresh_scopes_override: tuple[str, ...] | None = None
        self.revoke_receipt_override: object | None = None

    async def begin(
        self, profile_id: str, state_digest: str, pkce_challenge: str, scopes: tuple[str, ...]
    ) -> str:
        self.scopes = scopes
        self.profile_id = profile_id
        self.state_digest = state_digest
        self.challenge = pkce_challenge
        if self.authorization_url_override is not None:
            return self.authorization_url_override
        # A correct bridge returns exactly the request the unit would build itself.
        from mcp_platform.oauth_protocol import OAuthAuthorizationParameters

        profile = self.profile
        discovery = await self.protocol.discover(profile, resource_origin=profile.resource_origins[0])
        return self.protocol.build_authorization_url(
            discovery,
            OAuthAuthorizationParameters.for_profile(
                profile,
                credential=OAuthClientCredential(profile.client_id, None),
                scopes=scopes,
                state=state_digest,
                code_challenge=pkce_challenge,
                resource_origin=profile.resource_origins[0],
            ),
        )

    async def redeem(
        self,
        profile_id: str,
        completion_handle: str,
        state_digest: str,
        pkce_verifier: str,
        scopes: tuple[str, ...],
        resource_origin: str,
    ) -> OAuthTokenResponse:
        self.redeem_scopes = scopes
        self.resource_origin = resource_origin
        if self.redeemed:
            raise ValueError("oauth_callback_replayed")
        if (
            profile_id != self.profile_id
            or state_digest != self.state_digest
            or completion_handle != "single-use-handle"
            or sha256(pkce_verifier.encode("ascii")).digest()
            != base64.urlsafe_b64decode(self.challenge + "=")
        ):
            raise ValueError("oauth_callback_invalid")
        self.redeemed = True
        return OAuthTokenResponse(
            access_token="customer-access",
            refresh_token="customer-refresh",
            token_type="Bearer",
            scopes=("read:user", "repo"),
            expires_at=130.0,
        )

    async def refresh(
        self,
        profile_id: str,
        refresh_token: str,
        state_digest: str,
        scopes: tuple[str, ...],
        resource_origin: str,
    ) -> OAuthTokenResponse:
        assert resource_origin == self.resource_origin
        self.refresh_calls.append((profile_id, refresh_token, state_digest, scopes))
        return OAuthTokenResponse(
            access_token="customer-refreshed-access",
            refresh_token="customer-refreshed-refresh",
            token_type="Bearer",
            scopes=self.refresh_scopes_override or ("read:user", "repo"),
            expires_at=500.0,
        )

    async def revoke(
        self, profile_id: str, refresh_token: str, state_digest: str
    ) -> OAuthRevocationReceipt:
        self.revoke_calls.append((profile_id, refresh_token, state_digest))
        if self.revoke_receipt_override is not None:
            return cast(OAuthRevocationReceipt, self.revoke_receipt_override)
        return OAuthRevocationReceipt(endpoint_advertised=False, provider_request_sent=False)


def _hosted_setup(tmp_path: Path) -> tuple[OAuthBroker, OAuthProfile, _Bridge, _Clock]:
    profile = broker_profile(secret_ref=False)
    profile = replace(profile, client_mode=OAuthClientMode.HOSTED_BRIDGE)
    profile = replace(profile, revision=computed_profile_revision(profile))
    bridge = _Bridge()
    clock = _Clock()
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    bridge.profile, bridge.protocol = profile, protocol
    broker = OAuthBroker(
        registry=_Registry(profile), vault=vault, protocol=protocol,
        hosted_bridge=bridge, clock=clock,
    )

    return broker, profile, bridge, clock


@pytest.mark.anyio
async def test_hosted_begin_rejects_secret_bearing_authorization_url(tmp_path: Path) -> None:
    broker, _profile, bridge, _clock = _hosted_setup(tmp_path)
    bridge.authorization_url_override = "https://bridge.example/authorize?token=leaked"

    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        await broker.begin(_request())
    assert broker.recover() == ((), ())


@pytest.mark.anyio
@pytest.mark.parametrize("change", ("origin", "path", "scope", "state", "challenge", "client", "redirect", "extra"))
async def test_hosted_begin_accepts_only_the_exact_signed_authorization_request(
    tmp_path: Path, change: str
) -> None:
    """A compromised or misconfigured bridge cannot send the operator to another origin or
    change the request they consent to (wider scopes, another client, another state)."""
    from urllib.parse import urlencode

    broker, profile, bridge, _clock = _hosted_setup(tmp_path)
    good = await _Bridge.begin(bridge, profile.profile_id, "a" * 64, "C" * 43, ("read:user",))
    bridge.authorization_url_override = None
    parts = urlsplit(good)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    netloc, path = parts.netloc, parts.path
    if change == "origin":
        netloc = "evil.example"
    elif change == "path":
        path = "/other/authorize"
    elif change == "scope":
        query["scope"] = "read:user repo admin"
    elif change == "state":
        query["state"] = "b" * 64
    elif change == "challenge":
        query["code_challenge"] = "D" * 43
    elif change == "client":
        query["client_id"] = "another-client"
    elif change == "redirect":
        query["redirect_uri"] = "https://evil.example/cb"
    else:
        query["prompt"] = "none"
    tampered = f"{parts.scheme}://{netloc}{path}?{urlencode(query)}"

    real_begin = _Bridge.begin

    async def tamper(self, profile_id, state_digest, pkce_challenge, scopes):
        url = await real_begin(self, profile_id, state_digest, pkce_challenge, scopes)
        replay = urlsplit(url)
        replay_query = {key: values[0] for key, values in parse_qs(replay.query).items()}
        if change in ("state", "challenge", "scope", "client", "redirect"):
            replay_query[{"state": "state", "challenge": "code_challenge", "scope": "scope",
                          "client": "client_id", "redirect": "redirect_uri"}[change]] = (
                parse_qs(urlsplit(tampered).query)[
                    {"state": "state", "challenge": "code_challenge", "scope": "scope",
                     "client": "client_id", "redirect": "redirect_uri"}[change]][0]
            )
        elif change == "extra":
            replay_query["prompt"] = "none"
        replay_netloc = netloc if change == "origin" else replay.netloc
        replay_path = path if change == "path" else replay.path
        return f"{replay.scheme}://{replay_netloc}{replay_path}?{urlencode(replay_query)}"

    bridge.begin = tamper.__get__(bridge)
    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        await broker.begin(_request())
    assert broker.recover() == ((), ())


@pytest.mark.anyio
async def test_hosted_callback_uses_single_use_handle_without_code_or_token(tmp_path: Path) -> None:
    broker, profile, _bridge, _clock = _hosted_setup(tmp_path)
    start = await broker.begin(_request())
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    callback = (
        f"{start.redirect_uri}?state={state}&completion_handle=single-use-handle"
        "&iss=https%3A%2F%2Fissuer.example"
    )
    assert "code=" not in callback
    assert "token=" not in callback
    assert "secret=" not in callback
    assert state == start.state_sha256
    assert broker.callback_state_sha256(callback) == start.state_sha256
    prepared = await broker.complete(profile_id=profile.profile_id, callback_uri=callback)
    assert prepared.profile_id == profile.profile_id
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        await broker.complete(profile_id=profile.profile_id, callback_uri=callback)


@pytest.mark.anyio
async def test_hosted_error_without_handle_cannot_use_digest_state(tmp_path: Path) -> None:
    broker, profile, _bridge, _clock = _hosted_setup(tmp_path)
    start = await broker.begin(_request())
    callback = (
        f"{start.redirect_uri}?state={start.state_sha256}&error=access_denied"
        "&iss=https%3A%2F%2Fissuer.example"
    )

    assert broker.callback_state_sha256(callback) != start.state_sha256
    with pytest.raises(ValueError, match="^oauth_callback_invalid$"):
        await broker.complete(profile_id=profile.profile_id, callback_uri=callback)
    assert broker.is_transaction_consumed(start.state_sha256) is False


@pytest.mark.anyio
async def test_direct_pkce_rejects_digest_substitution_without_consuming_attempt(
    tmp_path: Path,
) -> None:
    profile = broker_profile(secret_ref=False)
    clock = _Clock()
    vault = _TrackingVault(tmp_path, clock)
    broker = OAuthBroker(
        registry=_Registry(profile), vault=vault, protocol=_Protocol(), clock=clock
    )
    start = await broker.begin(_request())
    callback = (
        f"{start.redirect_uri}?state={start.state_sha256}&error=access_denied"
        "&iss=https%3A%2F%2Fissuer.example"
    )

    with pytest.raises(ValueError, match="^oauth_callback_invalid$"):
        await broker.complete(profile_id=profile.profile_id, callback_uri=callback)
    assert broker.is_transaction_consumed(start.state_sha256) is False
    assert vault.load_transaction(start.state_sha256).prepared_token is None


@pytest.mark.anyio
async def test_hosted_refresh_and_revoke_never_use_direct_provider_http(tmp_path: Path) -> None:
    broker, profile, bridge, _clock = _hosted_setup(tmp_path)
    start = await broker.begin(_request())
    callback = (
        f"{start.redirect_uri}?state={start.state_sha256}"
        "&completion_handle=single-use-handle&iss=https%3A%2F%2Fissuer.example"
    )
    prepared = await broker.complete(profile_id=profile.profile_id, callback_uri=callback)
    broker.commit(prepared, _commit())

    refreshed = await broker.refresh(prepared.partition_key, force=True)
    assert refreshed.token.refresh_token == "customer-refreshed-refresh"
    # The broker passes the consented scopes on every hop; the bridge never widens them.
    assert bridge.scopes == bridge.redeem_scopes == ("read:user", "repo")
    assert bridge.refresh_calls == [
        (profile.profile_id, "customer-refresh", prepared.partition_key, ("read:user", "repo"))
    ]
    assert broker._protocol.refresh_calls == []
    assert await broker.current_access_token(_binding(prepared)) == "customer-refreshed-access"

    revoked = await broker.revoke(prepared.partition_key, plugin_id="plugin.alpha")
    assert revoked.local_token_deleted is True
    # The bridge's receipt is the record: a profile with no revocation endpoint sent nothing.
    assert revoked.revoked_provider_token is False
    assert bridge.revoke_calls == [
        (profile.profile_id, "customer-refreshed-refresh", prepared.partition_key)
    ]
    assert broker._protocol.revoke_calls == []


@pytest.mark.anyio
async def test_hosted_refresh_cannot_expand_signed_scopes(tmp_path: Path) -> None:
    broker, profile, bridge, _clock = _hosted_setup(tmp_path)
    start = await broker.begin(_request())
    callback = (
        f"{start.redirect_uri}?state={start.state_sha256}"
        "&completion_handle=single-use-handle&iss=https%3A%2F%2Fissuer.example"
    )
    prepared = await broker.complete(profile_id=profile.profile_id, callback_uri=callback)
    broker.commit(prepared, _commit())
    bridge.refresh_scopes_override = ("admin", "read:user", "repo")

    with pytest.raises(ValueError, match="^oauth_token_scope_conflict$"):
        await broker.refresh(prepared.partition_key, force=True)
    assert broker._vault.load_token_partition(prepared.partition_key).token.refresh_token == (
        "customer-refresh"
    )


@pytest.mark.anyio
async def test_hosted_revoke_rejects_a_bridge_result_that_is_not_a_receipt(tmp_path: Path) -> None:
    broker, profile, bridge, _clock = _hosted_setup(tmp_path)
    start = await broker.begin(_request())
    callback = (
        f"{start.redirect_uri}?state={start.state_sha256}"
        "&completion_handle=single-use-handle&iss=https%3A%2F%2Fissuer.example"
    )
    prepared = await broker.complete(profile_id=profile.profile_id, callback_uri=callback)
    broker.commit(prepared, _commit())
    bridge.revoke_receipt_override = SimpleNamespace(
        endpoint_advertised=True, provider_request_sent=True
    )

    with pytest.raises(ValueError, match="^oauth_revocation_unsupported$"):
        await broker.revoke(prepared.partition_key, plugin_id="plugin.alpha")
    assert broker._vault.load_token_partition(prepared.partition_key).token.refresh_token == (
        "customer-refresh"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("change", ("state", "profile", "code", "verifier", "expired"))
async def test_hosted_callback_rejects_wrong_or_expired_proof_before_token_commit(
    tmp_path: Path, change: str
) -> None:
    broker, profile, bridge, clock = _hosted_setup(tmp_path)
    start = await broker.begin(_request())
    state = start.state_sha256 if change != "state" else "0" * 64
    callback = (
        f"{start.redirect_uri}?state={state}&completion_handle=single-use-handle"
        "&iss=https%3A%2F%2Fissuer.example"
    )
    if change == "code":
        callback += "&code=stolen-code"
    if change == "verifier":
        bridge.challenge = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
    if change == "expired":
        clock.value = start.expires_at + 1
    route_profile = "wrong.desktop" if change == "profile" else profile.profile_id

    with pytest.raises(ValueError):
        await broker.complete(profile_id=route_profile, callback_uri=callback)
    if change == "expired":
        clock.value = 100.0
    assert broker._vault.load_transaction(start.state_sha256).prepared_token is None


@pytest.mark.anyio
async def test_independent_install_vaults_never_cross_account_tokens(tmp_path: Path) -> None:
    profile = broker_profile(secret_ref=False)
    clock = _Clock()
    vault_a = _TrackingVault(tmp_path / "install-a", clock)
    vault_b = _TrackingVault(tmp_path / "install-b", clock)
    protocol_a = _Protocol()
    protocol_b = _Protocol()
    protocol_a.exchange_access_token = "a-access"
    protocol_a.exchange_refresh_token = "a-refresh"
    protocol_b.exchange_access_token = "b-access"
    protocol_b.exchange_refresh_token = "b-refresh"
    broker_a = OAuthBroker(registry=_Registry(profile), vault=vault_a, protocol=protocol_a, clock=clock)
    broker_b = OAuthBroker(registry=_Registry(profile), vault=vault_b, protocol=protocol_b, clock=clock)

    prepared_a, binding_a = await _connected(broker_a, _request())
    prepared_b, binding_b = await _connected(
        broker_b, replace(_request(), account_key_sha256="2" * 64)
    )
    assert prepared_a.partition_key != prepared_b.partition_key
    assert vault_a.load_token_partition(prepared_a.partition_key).token.refresh_token == "a-refresh"
    assert vault_b.load_token_partition(prepared_b.partition_key).token.refresh_token == "b-refresh"
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        vault_a.load_token_partition(prepared_b.partition_key)
    with pytest.raises(ValueError):
        await broker_a.current_access_token(binding_b)
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        await broker_a.refresh(prepared_b.partition_key, force=True)
    with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
        await broker_a.revoke(prepared_b.partition_key, plugin_id="plugin.alpha")

    await broker_a.refresh(prepared_a.partition_key, force=True)
    assert protocol_a.refresh_calls[0]["refresh_token"] == "a-refresh"
    assert vault_b.load_token_partition(prepared_b.partition_key).token.refresh_token == "b-refresh"
    await broker_a.revoke(prepared_a.partition_key, plugin_id="plugin.alpha")
    assert vault_b.load_token_partition(prepared_b.partition_key).account_key_sha256 == "2" * 64
    assert await broker_b.current_access_token(binding_b) == "b-access"
