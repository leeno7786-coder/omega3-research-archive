"""Contracts for Omega's single supported current MCP SDK boundary."""

from __future__ import annotations

import importlib.metadata
import inspect
from datetime import date

from core.paths import OMEGA_ROOT
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION


def _current_sdk_client() -> type[object]:
    from mcp import Client

    return Client


def test_installed_mcp_distribution_is_the_exact_current_sdk() -> None:
    assert importlib.metadata.version("mcp") == "2.0.0"


def test_package_contract_defines_only_the_current_protocol() -> None:
    assert MCP_PROTOCOL_VERSION == "2026-07-28"


def test_current_sdk_client_uses_auto_mode_by_default() -> None:
    client = _current_sdk_client()

    assert inspect.signature(client).parameters["mode"].default == "auto"


def test_current_sdk_client_accepts_the_exact_protocol_mode() -> None:
    client = _current_sdk_client()

    client("omega-current-protocol-test", mode=MCP_PROTOCOL_VERSION)


def test_production_mcp_source_does_not_retain_the_retired_protocol_literal() -> None:
    retired_protocol = date(2025, 11, 25).isoformat()
    production_sources = (OMEGA_ROOT / "mcp_platform").rglob("*.py")

    retained = [
        source.relative_to(OMEGA_ROOT).as_posix()
        for source in production_sources
        if retired_protocol in source.read_text(encoding="utf-8")
    ]

    assert retained == []
