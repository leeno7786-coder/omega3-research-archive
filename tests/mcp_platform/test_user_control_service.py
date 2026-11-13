"""Public default-host MCP user-control forwarding tests."""

from __future__ import annotations

from typing import Any

import anyio

import mcp_platform.service as service


class _ControlHost:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def set_external_enabled(
        self,
        enabled: bool,
        expected_revision: str,
    ) -> dict[str, Any]:
        self.calls.append(("external", enabled, expected_revision))
        return {"external": enabled}

    async def set_package_enabled(
        self,
        server_id: str,
        enabled: bool,
        expected_registry_revision: str,
    ) -> dict[str, Any]:
        self.calls.append(
            ("package", server_id, enabled, expected_registry_revision)
        )
        return {"package": server_id, "enabled": enabled}


def test_service_forwards_external_control_only_through_default_host(
    monkeypatch: Any,
) -> None:
    host = _ControlHost()
    monkeypatch.setattr(service, "get_default_mcp_host", lambda: host)

    result = anyio.run(service.set_external_mcp_enabled, True, "a" * 64)

    assert result == {"external": True}
    assert host.calls == [("external", True, "a" * 64)]


def test_service_forwards_package_control_only_through_default_host(
    monkeypatch: Any,
) -> None:
    host = _ControlHost()
    monkeypatch.setattr(service, "get_default_mcp_host", lambda: host)

    result = anyio.run(
        service.set_mcp_package_enabled,
        "omega.first_party.fake",
        False,
        "b" * 64,
    )

    assert result == {"package": "omega.first_party.fake", "enabled": False}
    assert host.calls == [
        ("package", "omega.first_party.fake", False, "b" * 64)
    ]
