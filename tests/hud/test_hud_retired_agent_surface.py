"""The HUD no longer ships the retired terminal-agent surfaces."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from hud.server import create_app


ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"


def test_retired_agent_modules_are_not_in_the_hud_manifest() -> None:
    manifest = json.loads((UI_SRC / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("core", []) + manifest.get("panels", [])
    entry_ids = {entry.get("id") for entry in entries}

    assert "dev-panel" not in entry_ids
    assert "agent_panel" not in entry_ids
    assert "settings_terminal" not in entry_ids
    assert not (UI_SRC / "panels" / "dev.js").exists()
    assert not (UI_SRC / "panels" / "agent.js").exists()
    assert not (UI_SRC / "panels" / "settings-terminal.js").exists()


def test_unregistered_legacy_telemetry_panels_are_removed() -> None:
    for name in ("identity.js", "deepsleep.js"):
        assert not (UI_SRC / "panels" / name).exists(), name


def test_retired_agent_markup_and_hooks_are_absent() -> None:
    shell = (UI_SRC / "shell.html").read_text(encoding="utf-8")
    scripts = (UI_SRC / "scripts.html").read_text(encoding="utf-8")
    register = (UI_SRC / "panels" / "register.js").read_text(encoding="utf-8")
    omega_ui = (UI_SRC / "core" / "omega-ui.js").read_text(encoding="utf-8")

    assert 'data-panel="agent"' not in shell
    assert 'data-settings-view="terminal"' not in shell
    assert "INJECT_AGENT_PANEL" not in scripts
    assert "AgentFeed" not in register
    assert "devWsReady" not in omega_ui
    assert "window.devSend" not in omega_ui


def test_retired_agent_routes_are_not_registered() -> None:
    client = TestClient(create_app())
    retired_routes = (
        "/api/dev/launch",
        "/api/dev/shutdown",
        "/api/dev/command",
        "/api/dev/kill",
        "/api/dev/quit",
        "/api/dev/snapshots",
        "/api/dev/snapshot",
        "/api/dev/rollback",
        "/api/agent-log/save",
        "/api/agent-log/download",
        "/api/chat/skill-activity/recent",
        "/api/chat/skill-activity/clear",
    )

    for route in retired_routes:
        assert client.get(route).status_code == 404, route
