"""The live-chat skill picker exposes only its read-only inventory."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_ROOT", str(tmp_path))
    (tmp_path / "boot.py").write_text("", encoding="utf-8")
    from hud import qwen_skills
    from hud.server import create_app

    user_dir = tmp_path / "skills-user"
    user_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(qwen_skills, "USER_SKILL_DIR", user_dir)
    monkeypatch.setattr(qwen_skills, "SEARCH_DIRS", [user_dir])

    return TestClient(create_app())


def test_live_chat_skill_inventory_is_read_only(client):
    response = client.get("/api/skills")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body["available"], list)
    assert isinstance(body["commands"], list)
    assert isinstance(body["builtins"], list)
    assert isinstance(body["plugins"], list)
    assert isinstance(body["active"], list)


def test_plugin_slash_commands_are_installed_only():
    from hud.qwen_skills import installed_plugin_commands

    commands = installed_plugin_commands(
        (
            {"plugin_id": "not-installed", "installed": False, "title": "Nope"},
            {"plugin_id": "installed.calendar", "installed": True, "title": "Calendar"},
        )
    )
    assert [item["name"] for item in commands] == ["/plugin:installed.calendar"]
    assert commands[0]["plugin_id"] == "installed.calendar"


def test_terminal_skill_management_routes_are_gone(client):
    assert client.post("/api/skills").status_code == 405
    for method, route in (
        (client.post, "/api/skills/example/toggle"),
        (client.delete, "/api/skills/example"),
        (client.get, "/api/skills/catalog"),
        (client.post, "/api/skills/install"),
    ):
        assert method(route).status_code == 404, route
