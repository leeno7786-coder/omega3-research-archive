"""Factory reset coverage for the canonical plugin and MCP state roots."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.day_zero_reset as day_zero_reset


def test_factory_reset_wipes_plugin_mcp_authorization_and_upload_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "guardian" / "state"
    state_files = (
        state_root / "mcp" / "authorization" / "oauth-vault.json",
        state_root / "mcp" / "authorization" / "connections.json",
        state_root / "mcp" / "plugin_authority.json",
        state_root / "mcp" / "plugin_permissions.json",
        state_root / "mcp" / "registry.json",
        state_root / "plugins" / "registry.json",
        state_root / "plugins" / "packages" / "example" / "plugin.json",
        state_root / "connectors" / "omega-connect.key",
        state_root / "connectors" / "omega-connect.secrets.json",
        state_root / "startup_setup.json",
        state_root / "uploads" / "operator-document.pdf",
    )
    for path in state_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime-state")

    monkeypatch.setattr(day_zero_reset, "GUARDIAN_STATE_ROOT", state_root)
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", tmp_path)   # Cut 13: the verifier also checks memory/projection residue

    day_zero_reset.wipe_guardian_state_completely()

    assert all(not path.exists() for path in state_files)
    assert day_zero_reset.verify_day_zero_surfaces() == set()


def test_reset_has_no_retired_native_connector_or_workspace_port_dependency() -> None:
    source = Path(day_zero_reset.__file__).read_text(encoding="utf-8")

    assert "WORKSPACE_CONNECTOR_FILES" not in source
    assert 'GUARDIAN_STATE_ROOT / "connectors"' not in source
    assert "core.workspace_port" not in source
