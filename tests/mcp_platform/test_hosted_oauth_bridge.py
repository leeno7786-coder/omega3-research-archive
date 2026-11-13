"""The unit side of the hosted OAuth bridge (docs/plugin-hosted-oauth-bridge.md).

The bridge holds the provider client secret; the unit holds the PKCE verifier and the tokens.
These tests pin the wire contract and that a bridge answer is validated exactly like a direct
token response.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx2
import pytest

from mcp_platform.oauth_profiles import OAuthClientMode, computed_profile_revision
from mcp_platform.oauth_protocol import (
    HttpHostedOAuthBridge,
    OAuthProtocolClient,
    OAuthProtocolError,
    OAuthProviderError,
    OAuthRevocationReceipt,
)
from tests.mcp_platform.test_clean_install_oauth import _profile

BASE = "https://plugins.example"
DIGEST = "a" * 64
CHALLENGE = "C" * 43
SCOPES = ("channels:read", "chat:write")
RESOURCE = "https://resource.example/mcp"


def _hosted_profile():
    profile = replace(
        _profile("hosted.desktop", mode=OAuthClientMode.HOSTED_BRIDGE),
        redirect_uri=f"{BASE}/oauth/hosted.desktop/callback",
    )
    return replace(profile, revision=computed_profile_revision(profile))


class _Server:
    def __init__(self, responses: dict[str, httpx2.Response]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.requests.append((f"{request.url.scheme}://{request.url.host}{path}", json.loads(request.content)))
        return self.responses[path]


def _bridge(server: _Server) -> HttpHostedOAuthBridge:
    profile = _hosted_profile()
    return HttpHostedOAuthBridge(
        lambda profile_id: profile if profile_id == profile.profile_id else None,
        protocol=OAuthProtocolClient(transport=httpx2.MockTransport(server), clock=lambda: 1_000.0),
    )


@pytest.mark.anyio
async def test_begin_posts_the_consented_scopes_to_the_signed_bridge_origin() -> None:
    url = f"https://slack.example/authorize?client_id=c&state={DIGEST}"
    server = _Server({"/bridge/v1/hosted.desktop/begin": httpx2.Response(200, json={"authorization_url": url})})

    result = await _bridge(server).begin("hosted.desktop", DIGEST, CHALLENGE, ("chat:write", "channels:read"))

    assert result == url
    assert server.requests == [(
        f"{BASE}/bridge/v1/hosted.desktop/begin",
        {"state_digest": DIGEST, "pkce_challenge": CHALLENGE, "scopes": list(SCOPES)},
    )]


@pytest.mark.anyio
async def test_redeem_validates_the_passed_through_token_like_a_direct_exchange() -> None:
    server = _Server({"/bridge/v1/hosted.desktop/redeem": httpx2.Response(200, json={
        "access_token": "xoxp-access", "refresh_token": "xoxe-refresh", "token_type": "Bearer",
        "scope": "chat:write channels:read", "expires_in": 3600,
    })})

    token = await _bridge(server).redeem("hosted.desktop", "handle", DIGEST, "verifier", SCOPES, RESOURCE)

    assert (token.access_token, token.refresh_token, token.scopes, token.expires_at) == (
        "xoxp-access", "xoxe-refresh", SCOPES, 4_600.0,
    )
    assert server.requests[0][1] == {
        "completion_handle": "handle", "state_digest": DIGEST, "pkce_verifier": "verifier",
    }


@pytest.mark.anyio
async def test_a_token_granting_other_scopes_than_consented_is_refused() -> None:
    server = _Server({"/bridge/v1/hosted.desktop/redeem": httpx2.Response(200, json={
        "access_token": "xoxp-access", "token_type": "Bearer", "scope": "chat:write admin",
    })})

    with pytest.raises(OAuthProtocolError, match="^oauth_scope_mismatch$"):
        await _bridge(server).redeem("hosted.desktop", "handle", DIGEST, "verifier", SCOPES, RESOURCE)


@pytest.mark.anyio
async def test_refresh_keeps_the_existing_refresh_token_when_none_is_rotated() -> None:
    server = _Server({"/bridge/v1/hosted.desktop/refresh": httpx2.Response(200, json={
        "access_token": "xoxp-new", "token_type": "Bearer",
    })})

    token = await _bridge(server).refresh("hosted.desktop", "xoxe-refresh", DIGEST, SCOPES, RESOURCE)

    assert (token.access_token, token.refresh_token, token.scopes) == ("xoxp-new", "xoxe-refresh", SCOPES)
    assert server.requests[0][1] == {"refresh_token": "xoxe-refresh", "state_digest": DIGEST}


@pytest.mark.anyio
@pytest.mark.parametrize(("error", "reason"), (
    ("invalid_client", "oauth_client_invalid"),
    ("invalid_grant", "oauth_provider_rejected"),
    ("oauth_provider_rejected", "oauth_provider_rejected"),
))
async def test_a_refusal_the_bridge_relays_is_a_provider_error(error: str, reason: str) -> None:
    server = _Server({"/bridge/v1/hosted.desktop/redeem": httpx2.Response(400, json={"error": error})})

    with pytest.raises(OAuthProviderError, match=f"^{reason}$"):
        await _bridge(server).redeem("hosted.desktop", "handle", DIGEST, "verifier", SCOPES, RESOURCE)


@pytest.mark.anyio
async def test_an_unconfigured_bridge_is_unavailable_not_a_refusal() -> None:
    server = _Server({"/bridge/v1/hosted.desktop/begin": httpx2.Response(503, json={"error": "bridge_unconfigured"})})

    with pytest.raises(OAuthProtocolError, match="^oauth_profile_unavailable$") as raised:
        await _bridge(server).begin("hosted.desktop", DIGEST, CHALLENGE, SCOPES)
    assert type(raised.value) is OAuthProtocolError


@pytest.mark.anyio
async def test_a_bridge_that_does_not_serve_the_profile_is_unavailable_not_a_refusal() -> None:
    """A 404 (bridge not deployed, or profile not configured there) never reached the provider."""
    server = _Server({"/bridge/v1/hosted.desktop/begin": httpx2.Response(404, text="Not Found")})

    with pytest.raises(OAuthProtocolError, match="^oauth_profile_unavailable$") as raised:
        await _bridge(server).begin("hosted.desktop", DIGEST, CHALLENGE, SCOPES)
    assert type(raised.value) is OAuthProtocolError


@pytest.mark.anyio
async def test_a_provider_the_bridge_could_not_reach_is_a_network_failure() -> None:
    from mcp_platform.oauth_protocol import OAuthNetworkError

    server = _Server({"/bridge/v1/hosted.desktop/redeem": httpx2.Response(502, json={"error": "provider_unavailable"})})

    with pytest.raises(OAuthNetworkError, match="^oauth_provider_unreachable$"):
        await _bridge(server).redeem("hosted.desktop", "handle", DIGEST, "verifier", SCOPES, RESOURCE)


@pytest.mark.anyio
@pytest.mark.parametrize("advertised", (True, False))
async def test_revoke_returns_the_bridge_receipt(advertised: bool) -> None:
    server = _Server({"/bridge/v1/hosted.desktop/revoke": httpx2.Response(200, json={
        "endpoint_advertised": advertised, "provider_request_sent": advertised,
    })})

    receipt = await _bridge(server).revoke("hosted.desktop", "xoxe-refresh", DIGEST)

    assert receipt == OAuthRevocationReceipt(endpoint_advertised=advertised, provider_request_sent=advertised)


@pytest.mark.anyio
async def test_a_profile_without_an_https_bridge_origin_is_unavailable() -> None:
    server = _Server({})
    loopback = _profile("hosted.desktop", mode=OAuthClientMode.HOSTED_BRIDGE)  # http loopback redirect
    bridge = HttpHostedOAuthBridge(
        lambda _profile_id: loopback, protocol=OAuthProtocolClient(transport=httpx2.MockTransport(server))
    )

    with pytest.raises(OAuthProtocolError, match="^oauth_profile_unavailable$"):
        await bridge.begin("hosted.desktop", DIGEST, CHALLENGE, SCOPES)
    assert server.requests == []



@pytest.mark.anyio
async def test_the_composed_broker_reaches_the_bridge_for_every_shipped_hosted_profile() -> None:
    """Every signed hosted_bridge profile resolves to a bridge at the https origin of its
    signed redirect; without the wiring a hosted profile could never start a connection.
    The bridge shares the broker's protocol client, so the service's one close releases it."""
    from core import paths as core_paths
    from mcp_platform.oauth_profiles import OAuthProfileRegistry
    from mcp_platform.plugin_lifecycle import _compose_oauth_broker
    from plugin_platform.trust import load_embedded_key_ring

    registry = OAuthProfileRegistry.from_path(core_paths.OAUTH_PROFILE_CATALOG_PATH, load_embedded_key_ring())
    protocol = OAuthProtocolClient()
    broker = _compose_oauth_broker(registry, vault=object(), protocol=protocol)
    assert broker._hosted_bridge._http is protocol
    await protocol.aclose()

    hosted = [
        profile for profile in json.loads(core_paths.OAUTH_PROFILE_CATALOG_PATH.read_text("utf-8"))["profiles"]
        if profile["client_mode"] == "hosted_bridge"
    ]
    assert hosted, "the shipped catalog carries at least one hosted profile (Slack)"
    for profile in hosted:
        origin = profile["redirect_uri"].split("/oauth/")[0]
        assert broker._hosted_bridge._endpoint(profile["profile_id"], "begin") == (
            f"{origin}/bridge/v1/{profile['profile_id']}/begin"
        )



@pytest.mark.anyio
@pytest.mark.parametrize("field", ("resource", "audience", "aud"))
async def test_a_relayed_token_for_another_resource_is_refused(field: str) -> None:
    """Validated exactly like a direct token response: a token bound to another resource
    or audience never reaches the vault."""
    server = _Server({"/bridge/v1/hosted.desktop/redeem": httpx2.Response(200, json={
        "access_token": "xoxp-access", "token_type": "Bearer", field: "https://other.example/mcp",
    })})

    with pytest.raises(OAuthProtocolError, match="^oauth_resource_mismatch$"):
        await _bridge(server).redeem("hosted.desktop", "handle", DIGEST, "verifier", SCOPES, RESOURCE)


@pytest.mark.anyio
async def test_a_relayed_token_for_this_resource_is_accepted_on_refresh() -> None:
    server = _Server({"/bridge/v1/hosted.desktop/refresh": httpx2.Response(200, json={
        "access_token": "xoxp-new", "token_type": "Bearer", "resource": RESOURCE,
    })})

    token = await _bridge(server).refresh("hosted.desktop", "xoxe-refresh", DIGEST, SCOPES, RESOURCE)

    assert token.access_token == "xoxp-new"


@pytest.mark.anyio
async def test_the_bridge_shares_the_callers_protocol_client_so_one_close_releases_it() -> None:
    protocol = OAuthProtocolClient(transport=httpx2.MockTransport(_Server({})))
    bridge = HttpHostedOAuthBridge(lambda _profile_id: _hosted_profile(), protocol=protocol)

    assert bridge._http is protocol
    await protocol.aclose()
