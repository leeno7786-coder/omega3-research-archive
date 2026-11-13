"""Strict content-free HUD endpoints for signed MCP user controls."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import hud.server as hud_server
from hud.server import create_app
from mcp_platform.external_switch import (
    ExternalMcpSwitchConflict,
    ExternalMcpSwitchUnreadable,
)
from mcp_platform.host import McpHostControlError
from mcp_platform.registry import McpRegistryConflict, McpRegistryUnreadable


REVISION = "a" * 64
REGISTRY_REVISION = "b" * 64
MCP_MUTATION_PATHS = (
    "/api/mcp/external-switch",
    "/api/mcp/packages/omega.first_party.local/enabled",
)


def _status() -> dict[str, Any]:
    return {
        "external_mcp": {"enabled": False, "revision": REVISION},
        "registry_revision": REGISTRY_REVISION,
        "packages": [
            {
                "server_id": "omega.first_party.local",
                "server_version": "1.0.0",
                "platform": "windows-amd64",
                "publisher_id": "omega.first_party",
                "signing_key_id": "release-2026",
                "manifest_digest": "c" * 64,
                "network_class": "local_only",
                "enabled": True,
                "status": "installed",
                "blocker": None,
                "crash_count": 0,
                "session_state": "ready",
                "protocol_version": "2025-11-25",
                "tool_catalog_sha256": "d" * 64,
                "resource_catalog_sha256": "e" * 64,
            }
        ],
    }


def test_mcp_status_is_content_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hud_server, "mcp_status", _status)

    response = TestClient(create_app()).get("/api/mcp/status")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok", "external_mcp", "registry_revision", "packages"}
    assert body["ok"] is True
    assert body["external_mcp"]["enabled"] is False
    assert body["packages"][0]["session_state"] == "ready"
    serialized = json.dumps(body).lower()
    for forbidden in (
        "token",
        "secret",
        "email_body",
        "document_text",
        "provider_content",
        "arguments",
        "command",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": "false", "revision": REVISION},
        {"enabled": False},
        {"enabled": False, "revision": "A" * 64},
        {"enabled": False, "revision": REVISION, "command": "run"},
        {"enabled": False, "revision": REVISION, "path": "server.exe"},
    ],
)
def test_switch_post_requires_exact_boolean_and_current_revision(
    payload: dict[str, object],
) -> None:
    response = TestClient(create_app()).post(
        "/api/mcp/external-switch",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "invalid_mcp_control_request"}


def test_switch_off_awaits_public_drain_before_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, str]] = []

    async def disable(enabled: bool, revision: str) -> dict[str, Any]:
        calls.append((enabled, revision))
        return {
            "drain": {
                "drained_calls": 2,
                "uncertain_writes": 0,
                "failed_reads": 0,
                "sessions_closed": 1,
                "sessions_forced": 0,
                "errors": 0,
            },
            "status": _status(),
        }

    monkeypatch.setattr(hud_server, "set_external_mcp_enabled", disable)

    response = TestClient(create_app()).post(
        "/api/mcp/external-switch",
        json={"enabled": False, "revision": REVISION},
    )

    assert response.status_code == 200
    assert calls == [(False, REVISION)]
    assert response.json()["drain"]["uncertain_writes"] == 0
    assert response.json()["ok"] is True


@pytest.mark.parametrize(
    ("error", "status_code", "stable_error"),
    [
        (ExternalMcpSwitchConflict("do not leak"), 409, "external_mcp_switch_conflict"),
        (ExternalMcpSwitchUnreadable("do not leak"), 503, "external_mcp_switch_unreadable"),
        (McpRegistryConflict("do not leak"), 409, "mcp_registry_conflict"),
        (McpRegistryUnreadable("do not leak"), 503, "mcp_registry_unreadable"),
        (McpHostControlError("do not leak"), 503, "mcp_control_reconcile_failed"),
        (RuntimeError("sensitive runtime detail"), 503, "mcp_control_unavailable"),
    ],
)
def test_switch_errors_are_stable_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status_code: int,
    stable_error: str,
) -> None:
    async def fail(_enabled: bool, _revision: str) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(hud_server, "set_external_mcp_enabled", fail)

    response = TestClient(create_app()).post(
        "/api/mcp/external-switch",
        json={"enabled": True, "revision": REVISION},
    )

    assert response.status_code == status_code
    assert response.json() == {"ok": False, "error": stable_error}
    assert "do not leak" not in response.text
    assert "sensitive runtime detail" not in response.text


def test_status_failure_does_not_collapse_to_an_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> dict[str, Any]:
        raise RuntimeError("host internals")

    monkeypatch.setattr(hud_server, "mcp_status", unavailable)

    response = TestClient(create_app()).get("/api/mcp/status")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "mcp_control_unavailable"}
    assert "host internals" not in response.text


@pytest.mark.parametrize("path", MCP_MUTATION_PATHS)
@pytest.mark.parametrize(
    "content_type",
    ("text/plain", "application/x-www-form-urlencoded", "application/json-patch+json"),
)
def test_mcp_mutations_require_application_json_media_type(
    path: str,
    content_type: str,
) -> None:
    response = TestClient(create_app()).post(
        path,
        content=json.dumps({"enabled": True, "revision": REVISION}),
        headers={"content-type": content_type},
    )

    assert response.status_code == 415
    assert response.json() == {"ok": False, "error": "invalid_mcp_control_request"}
    assert REVISION not in response.text


@pytest.mark.parametrize("path", MCP_MUTATION_PATHS)
@pytest.mark.parametrize(
    "headers",
    (
        {"host": "evil.example:13310", "origin": "http://evil.example:13310"},
        {"origin": "null"},
        {"origin": "https://127.0.0.1:13310"},
        {"origin": "http://127.0.0.1:13311"},
        {"referer": "http://evil.example:13310/settings"},
        {"referer": "not a url"},
    ),
)
def test_mcp_mutations_reject_unconfigured_browser_authorities_without_control_calls(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    headers: dict[str, str],
) -> None:
    calls: list[str] = []

    async def switch(_enabled: bool, _revision: str) -> dict[str, Any]:
        calls.append("switch")
        return {"status": _status()}

    async def package(
        _server_id: str,
        _enabled: bool,
        _revision: str,
    ) -> dict[str, Any]:
        calls.append("package")
        return {"registry_revision": REGISTRY_REVISION, "package": {}}

    monkeypatch.delenv("OMEGA_HUD_HOST", raising=False)
    monkeypatch.delenv("OMEGA_HUD_PORT", raising=False)
    monkeypatch.setattr(hud_server, "set_external_mcp_enabled", switch)
    monkeypatch.setattr(hud_server, "set_mcp_package_enabled", package)

    response = TestClient(create_app(), base_url="http://127.0.0.1:13310").post(
        path,
        json={"enabled": True, "revision": REVISION},
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "invalid_mcp_control_request"}
    assert calls == []


@pytest.mark.parametrize("path", MCP_MUTATION_PATHS)
@pytest.mark.parametrize(
    ("configured_host", "configured_port", "base_url", "headers"),
    (
        (None, None, "http://127.0.0.1:13310", {}),
        (
            None,
            None,
            "http://127.0.0.1:13310",
            {"origin": "http://127.0.0.1:13310"},
        ),
        (
            "localhost",
            "14443",
            "https://localhost:14443",
            {"referer": "https://localhost:14443/settings?panel=connectivity"},
        ),
        (
            "[::1]",
            "13310",
            "http://localhost:13310",
            {"origin": "http://[::1]:13310"},
        ),
    ),
)
def test_mcp_mutations_accept_native_or_configured_loopback_browser_authority(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    configured_host: str | None,
    configured_port: str | None,
    base_url: str,
    headers: dict[str, str],
) -> None:
    async def switch(enabled: bool, revision: str) -> dict[str, Any]:
        return {"status": _status(), "enabled": enabled, "revision": revision}

    async def package(server_id: str, enabled: bool, revision: str) -> dict[str, Any]:
        return {
            "registry_revision": revision,
            "reconciliation": "ready",
            "package": {"server_id": server_id, "enabled": enabled},
        }

    monkeypatch.setattr(hud_server, "set_external_mcp_enabled", switch)
    monkeypatch.setattr(hud_server, "set_mcp_package_enabled", package)
    if configured_host is None:
        monkeypatch.delenv("OMEGA_HUD_HOST", raising=False)
    else:
        monkeypatch.setenv("OMEGA_HUD_HOST", configured_host)
    if configured_port is None:
        monkeypatch.delenv("OMEGA_HUD_PORT", raising=False)
    else:
        monkeypatch.setenv("OMEGA_HUD_PORT", configured_port)

    response = TestClient(create_app(), base_url=base_url).post(
        path,
        json={"enabled": True, "revision": REVISION},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.parametrize("path", MCP_MUTATION_PATHS)
@pytest.mark.parametrize("configured_host", ("0.0.0.0", "hud.internal"))
def test_mcp_browser_mutations_fail_closed_for_non_loopback_hud_bindings(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    configured_host: str,
) -> None:
    calls: list[str] = []

    async def switch(_enabled: bool, _revision: str) -> dict[str, Any]:
        calls.append("switch")
        return {"status": _status()}

    async def package(
        _server_id: str,
        _enabled: bool,
        _revision: str,
    ) -> dict[str, Any]:
        calls.append("package")
        return {"registry_revision": REGISTRY_REVISION, "package": {}}

    monkeypatch.setenv("OMEGA_HUD_HOST", configured_host)
    monkeypatch.setenv("OMEGA_HUD_PORT", "13310")
    monkeypatch.setattr(hud_server, "set_external_mcp_enabled", switch)
    monkeypatch.setattr(hud_server, "set_mcp_package_enabled", package)

    response = TestClient(create_app(), base_url=f"http://{configured_host}:13310").post(
        path,
        json={"enabled": True, "revision": REVISION},
        headers={"origin": f"http://{configured_host}:13310"},
    )

    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "invalid_mcp_control_request"}
    assert calls == []


def test_standalone_authority_propagation_controls_browser_mutation_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hud.standalone as standalone

    monkeypatch.setenv("OMEGA_HUD_HOST", "stale.example")
    monkeypatch.setenv("OMEGA_HUD_PORT", "13310")
    monkeypatch.setattr(standalone, "_port_in_use", lambda _host, _port: False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *_a, **_k: None))
    assert (
        standalone.main(["--no-boot", "--host", "localhost", "--port", "13311"])
        == 0
    )

    calls: list[tuple[bool, str]] = []

    async def switch(enabled: bool, revision: str) -> dict[str, Any]:
        calls.append((enabled, revision))
        return {"status": _status()}

    monkeypatch.setattr(hud_server, "set_external_mcp_enabled", switch)
    client = TestClient(create_app(), base_url="http://localhost:13311")
    payload = {"enabled": True, "revision": REVISION}

    accepted = client.post(
        "/api/mcp/external-switch",
        json=payload,
        headers={"origin": "http://localhost:13311"},
    )
    wrong_default = client.post(
        "/api/mcp/external-switch",
        json=payload,
        headers={"origin": "http://localhost:13310"},
    )
    evil = client.post(
        "/api/mcp/external-switch",
        json=payload,
        headers={"host": "evil.example:13311", "origin": "http://evil.example:13311"},
    )
    native = client.post("/api/mcp/external-switch", json=payload)

    assert accepted.status_code == 200
    assert wrong_default.status_code == 403
    assert evil.status_code == 403
    assert native.status_code == 200
    assert calls == [(True, REVISION), (True, REVISION)]


@pytest.mark.parametrize("path", MCP_MUTATION_PATHS)
@pytest.mark.parametrize(
    "raw_body",
    (
        '{"enabled":false,"enabled":true,"revision":"' + REVISION + '"}',
        '{"enabled":true,"revision":"' + REVISION + '","extra":{"x":1,"x":2}}',
        "{malformed",
        "[]",
        '"string"',
        '{"enabled":true,"revision":"' + REVISION + '","unknown":false}',
    ),
)
def test_mcp_mutations_reject_duplicate_or_malformed_json_at_the_boundary(
    path: str,
    raw_body: str,
) -> None:
    response = TestClient(create_app()).post(
        path,
        content=raw_body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "invalid_mcp_control_request"}
    assert REVISION not in response.text
