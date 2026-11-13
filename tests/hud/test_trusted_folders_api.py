from __future__ import annotations

import asyncio
import json
from pathlib import Path

import core.trusted_folders as trusted_folders
from hud.server import create_app


def _route(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_settings_grant_selects_first_root_for_chat_tools(monkeypatch, tmp_path: Path):
    """A Settings grant must become usable by the next chat turn.

    Persisting a grant without selecting it left the model with the
    fail-closed no-workspace tool surface, even though the panel reported a
    successful add. The first explicit grant is both permission and the active
    root; later grants keep the existing active selection.
    """
    omega = tmp_path / "omega"
    trusted = tmp_path / "project"
    omega.mkdir()
    trusted.mkdir()
    monkeypatch.setattr(trusted_folders, "OMEGA_ROOT", omega)

    post = _route(create_app(), "/api/settings/trusted-folders", "POST")
    response = asyncio.run(post({"action": "grant", "path": str(trusted)}))
    body = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["active"] == str(trusted.resolve())
    assert trusted_folders.active_trusted_folder(omega) == trusted.resolve()
