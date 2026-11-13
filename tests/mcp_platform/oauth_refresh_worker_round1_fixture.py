"""Spawn-safe OAuth refresh worker fixtures for round-one regressions."""

from __future__ import annotations

import os
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from mcp_platform.canonical import canonical_bytes
from mcp_platform.oauth_protocol import OAuthDiscovery, OAuthProviderError


class _RejectingProtocol:
    async def discover(self, profile: Any, *, resource_origin: str) -> OAuthDiscovery:
        client_identifier = profile.client_id or profile.client_metadata_document_url
        assert client_identifier is not None
        return OAuthDiscovery(
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            client_mode=profile.client_mode,
            client_identifier=client_identifier,
            client_secret_advertised=profile.client_secret_ref is not None,
            redirect_uri=profile.redirect_uri,
            issuer=profile.authorization_issuer,
            resource_origin=resource_origin,
            authorization_endpoint="https://issuer.example/authorize",
            token_endpoint="https://issuer.example/token",
            revocation_endpoint=None,
            code_challenge_methods_supported=("S256",),
            authorization_response_iss_parameter_supported=True,
            scopes_supported=None,
            authorization_flow=profile.authorization_flow,
        )

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        raise OAuthProviderError("provider fixture rejected refresh")

    async def aclose(self) -> None:
        return None


class _DynamicInvalidClientProtocol:
    async def discover(self, profile: Any, *, resource_origin: str) -> OAuthDiscovery:
        assert profile.registration is not None
        return OAuthDiscovery(
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            client_mode=profile.client_mode,
            client_identifier=None,
            client_secret_advertised=False,
            redirect_uri=profile.redirect_uri,
            issuer=profile.authorization_issuer,
            resource_origin=resource_origin,
            authorization_endpoint="https://issuer.example/authorize",
            token_endpoint="https://issuer.example/token",
            revocation_endpoint=None,
            registration_endpoint="https://issuer.example/register",
            code_challenge_methods_supported=("S256",),
            authorization_response_iss_parameter_supported=True,
            scopes_supported=None,
            authorization_flow=profile.authorization_flow,
        )

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        raise OAuthProviderError("oauth_client_invalid")

    async def aclose(self) -> None:
        return None


def _response(
    body: dict[str, Any],
    credential: dict[str, Any],
    *,
    access_token: str,
    refresh_token: str,
    expires_at: float,
) -> dict[str, Any]:
    assert set(body) == {"operation", "profile", "request", "worker_authority"}
    assert body["operation"] == "oauth_refresh"
    assert set(credential) == {"client_secret", "refresh_token"}
    assert credential == {
        "client_secret": "desktop-secret",
        "refresh_token": "opaque-refresh",
    }
    serialized_body = canonical_bytes(body)
    assert b"desktop-secret" not in serialized_body
    assert b"opaque-refresh" not in serialized_body
    request = body["request"]
    assert type(request) is dict
    return {
        "operation": "oauth_refresh",
        "request_sha256": sha256(canonical_bytes(request)).hexdigest(),
        "partition_key": request["partition_key"],
        "expected_revision": request["expected_revision"],
        "token": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "scopes": request["required_scopes"],
            "expires_at": expires_at,
        },
    }


def rotating_refresh_handler(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    return _response(
        body,
        credential,
        access_token="rotated-access",
        refresh_token="rotated-refresh",
        expires_at=500.0,
    )


def winner_refresh_handler(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    return _response(
        body,
        credential,
        access_token="winner-access",
        refresh_token="winner-refresh",
        expires_at=600.0,
    )


def stale_refresh_handler(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    release_path = Path(os.environ["OMEGA_OAUTH_REFRESH_TEST_RELEASE_PATH"])
    deadline = time.monotonic() + 20.0
    while not release_path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("test release marker missing")
        time.sleep(0.01)
    return _response(
        body,
        credential,
        access_token="stale-access",
        refresh_token="stale-refresh",
        expires_at=550.0,
    )


def blocked_refresh_handler(
    _body: dict[str, Any], _credential: dict[str, Any]
) -> dict[str, Any]:
    time.sleep(60.0)
    return {}


def cancellable_refresh_handler(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    Path(os.environ["OMEGA_OAUTH_REFRESH_CANCEL_STARTED_PATH"]).write_text(
        str(os.getpid()),
        encoding="utf-8",
    )
    release_path = os.environ.get("OMEGA_OAUTH_REFRESH_CANCEL_RELEASE_PATH")
    if release_path is None:
        time.sleep(
            float(os.environ.get("OMEGA_OAUTH_REFRESH_CANCEL_DELAY_SECONDS", "1.0"))
        )
    else:
        release = Path(release_path)
        deadline = time.monotonic() + 60.0
        while not release.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("refresh cancellation release missing")
            time.sleep(0.01)
    Path(os.environ["OMEGA_OAUTH_REFRESH_LATE_EFFECT_PATH"]).write_text(
        "late",
        encoding="utf-8",
    )
    return _response(
        body,
        credential,
        access_token="cancelled-access",
        refresh_token="cancelled-refresh",
        expires_at=700.0,
    )


def crashing_refresh_handler(
    _body: dict[str, Any], _credential: dict[str, Any]
) -> dict[str, Any]:
    os._exit(73)


def rejecting_refresh_handler(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    Path(os.environ["OMEGA_OAUTH_REFRESH_TEST_PID_PATH"]).write_text(
        str(os.getpid()),
        encoding="utf-8",
    )
    import mcp_platform.oauth_broker as oauth_broker

    oauth_broker.OAuthProtocolClient = _RejectingProtocol
    return oauth_broker._execute_oauth_refresh_operation(body, credential)


def dynamic_invalid_client_refresh_handler(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    assert body["profile"]["registration"]["client_name"] == "Omega"
    assert body["request"]["client_id"] == "dynamic-client-1"
    assert credential == {
        "client_secret": None,
        "refresh_token": "opaque-refresh",
    }
    import mcp_platform.oauth_broker as oauth_broker

    oauth_broker.OAuthProtocolClient = _DynamicInvalidClientProtocol
    return oauth_broker._execute_oauth_refresh_operation(body, credential)
