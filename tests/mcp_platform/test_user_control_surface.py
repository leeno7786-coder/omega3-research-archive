"""HUD transport cannot expand the public signed MCP control surface."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import hud.server as hud_server
from hud.server import create_app
from mcp_platform.registry import McpPackageNotInstalled


REVISION = "a" * 64


def test_package_toggle_forwards_only_signed_identity_and_user_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool, str]] = []

    async def set_enabled(
        server_id: str,
        enabled: bool,
        revision: str,
    ) -> dict[str, Any]:
        calls.append((server_id, enabled, revision))
        return {
            "registry_revision": "b" * 64,
            "reconciliation": "ready",
            "package": {
                "server_id": server_id,
                "server_version": "1.0.0",
                "platform": "windows-amd64",
                "publisher_id": "omega.first_party",
                "signing_key_id": "release-2026",
                "manifest_digest": "c" * 64,
                "network_class": "local_only",
                "enabled": enabled,
                "status": "installed",
                "blocker": None,
                "crash_count": 0,
                "session_state": "ready",
                "protocol_version": "2026-07-27",
                "tool_catalog_sha256": "d" * 64,
                "resource_catalog_sha256": "e" * 64,
            },
        }

    monkeypatch.setattr(hud_server, "set_mcp_package_enabled", set_enabled)

    response = TestClient(create_app()).post(
        "/api/mcp/packages/omega.first_party.local/enabled",
        json={"enabled": True, "revision": REVISION},
    )

    assert response.status_code == 200
    assert calls == [("omega.first_party.local", True, REVISION)]
    assert response.json()["package"]["server_id"] == "omega.first_party.local"
    assert response.json()["ok"] is True


@pytest.mark.parametrize(
    "execution_field",
    ["url", "path", "command", "manifest", "signature", "oauth_client_secret"],
)
def test_package_toggle_rejects_all_client_execution_material(
    execution_field: str,
) -> None:
    response = TestClient(create_app()).post(
        "/api/mcp/packages/omega.first_party.local/enabled",
        json={
            "enabled": True,
            "revision": REVISION,
            execution_field: "client supplied",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "invalid_mcp_control_request"}
    assert "client supplied" not in response.text


def test_package_toggle_rejects_unknown_or_unsigned_identity_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unknown(
        _server_id: str,
        _enabled: bool,
        _revision: str,
    ) -> dict[str, Any]:
        raise McpPackageNotInstalled("unsigned package detail")

    monkeypatch.setattr(hud_server, "set_mcp_package_enabled", unknown)

    response = TestClient(create_app()).post(
        "/api/mcp/packages/omega.unsigned/enabled",
        json={"enabled": False, "revision": REVISION},
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "mcp_package_not_installed"}
    assert "unsigned package detail" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": 1, "revision": REVISION},
        {"enabled": True, "revision": "short"},
        {"revision": REVISION},
        {"enabled": True},
    ],
)
def test_package_toggle_requires_exact_boolean_and_registry_revision(
    payload: dict[str, object],
) -> None:
    response = TestClient(create_app()).post(
        "/api/mcp/packages/omega.first_party.local/enabled",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "invalid_mcp_control_request"}
