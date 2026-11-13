"""Exercise shipped packages across the real lifecycle/connection boundary."""

from pathlib import Path

import pytest

from mcp_platform.oauth_broker import OAuthAuthorizationStart, OAuthBroker
from mcp_platform.oauth_profiles import OAuthAuthorizationFlow
from mcp_platform.plugin_lifecycle import compose_plugin_lifecycle_service
from tests.helpers.plugin_consent import full_permission_settings


@pytest.mark.anyio
@pytest.mark.parametrize(
    "plugin_id,flow",
    [
        ("github.official", OAuthAuthorizationFlow.DEVICE_CODE),
        ("google.workspace.drive", OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE),
        ("google.workspace.calendar", OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE),
        ("google.workspace.gmail", OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE),
    ],
)
async def test_shipped_oauth_permissions_survive_connection_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin_id: str,
    flow: OAuthAuthorizationFlow,
) -> None:
    """Scope normalization must not discard a valid provider authorization."""
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "test-operator")

    async def begin(_broker, request):
        # Only the external authorization boundary is replaced. The signed
        # package, registry, scope selection and coordinator are all real.
        assert request.plugin_id == plugin_id
        assert request.expected_authorization_flow is flow
        return OAuthAuthorizationStart(
            profile_id="test.desktop", plugin_id=plugin_id,
            authorization_url="https://auth.example.test/authorize",
            redirect_uri=None, expires_at=2000000000.0,
            state_sha256="a" * 64, authorization_flow=flow,
            user_code="ABCD-1234" if flow is OAuthAuthorizationFlow.DEVICE_CODE else None,
            verification_uri="https://auth.example.test/device",
            poll_interval_seconds=5,
        )

    monkeypatch.setattr(OAuthBroker, "begin", begin)
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path, oauth_key_provider=lambda: b"t" * 32,
    )
    try:
        review = service.review(plugin_id)
        service.install(plugin_id, confirmation=review.sha256)
        selection = await service.permissions(plugin_id)
        started = await service.start_connection(
            plugin_id,
            revision=service.revision,
            settings=full_permission_settings(selection),
        )
        assert started.status == "connecting"
        assert started.authorization_flow == flow.value
        assert started.request_id == "a" * 64
        assert started.authorization_url == "https://auth.example.test/authorize"
        assert service.plugin_registry.get(plugin_id).enabled is False
    finally:
        service.close()
