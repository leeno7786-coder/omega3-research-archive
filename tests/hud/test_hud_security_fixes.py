"""Security remediation tests from parallel code review."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.hud_path_guard import resolve_face_image_path, resolve_repo_path
from hud.server import create_app


def test_resolve_repo_path_rejects_outside_omega_root(tmp_path, monkeypatch):
    monkeypatch.setattr("core.hud_path_guard.OMEGA_ROOT", tmp_path)
    inside = tmp_path / "hud" / "server.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("# ok", encoding="utf-8")
    assert resolve_repo_path(str(inside)) == inside.resolve()

    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ValueError, match="outside repo"):
        resolve_repo_path(str(outside.resolve()))


def test_resolve_face_image_path_blocks_traversal():
    with pytest.raises(ValueError):
        resolve_face_image_path("..", "save_state.json")
    with pytest.raises(ValueError):
        resolve_face_image_path("operator", "../save_state.json")


def test_face_images_route_rejects_traversal(tmp_path, monkeypatch):
    state_root = tmp_path / "guardian" / "state"
    face_root = state_root / "face_images" / "operator"
    face_root.mkdir(parents=True)
    (face_root / "photo.jpg").write_bytes(b"jpeg")

    monkeypatch.setattr("core.paths.GUARDIAN_STATE_ROOT", state_root)
    monkeypatch.setattr("core.hud_path_guard.GUARDIAN_STATE_ROOT", state_root)
    client = TestClient(create_app())

    ok = client.get("/api/face-images/operator/photo.jpg")
    assert ok.status_code == 200

    bad = client.get("/api/face-images/operator/..%2F..%2Fsave_state.json")
    assert bad.status_code in (400, 404)

    bad_pid = client.get("/api/face-images/..%2Fsave_state.json")
    assert bad_pid.status_code in (400, 404)


def test_system_shutdown_starts_shutdown_script(monkeypatch, tmp_path):
    omega_root = tmp_path / "omega"
    omega_root.mkdir()
    launched = []

    monkeypatch.setattr("core.paths.OMEGA_ROOT", omega_root)
    monkeypatch.setattr("hud.server.OMEGA_ROOT", omega_root)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: launched.append((args, kwargs)))

    client = TestClient(create_app())
    shutdown = client.post("/api/shutdown")

    assert shutdown.status_code == 200
    assert shutdown.json()["ok"] is True
    assert launched


def test_system_shutdown_script_is_windowless_and_detached(monkeypatch, tmp_path):
    omega_root = tmp_path / "omega"
    omega_root.mkdir()
    launched = []

    monkeypatch.setattr("core.paths.OMEGA_ROOT", omega_root)
    monkeypatch.setattr("hud.server.OMEGA_ROOT", omega_root)
    monkeypatch.setattr("subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )

    client = TestClient(create_app())
    response = client.post("/api/shutdown")

    assert response.status_code == 200
    _args, kwargs = launched[-1]
    assert kwargs["creationflags"] & 0x08000000
    assert kwargs["stdin"] is __import__("subprocess").DEVNULL
    assert kwargs["stdout"] is __import__("subprocess").DEVNULL
    assert kwargs["stderr"] is __import__("subprocess").DEVNULL


def test_hud_shutdown_releases_face_models(monkeypatch):
    released = []
    monkeypatch.setattr(
        "runtime.face.lifecycle.release_face_models",
        lambda: released.append(True) or {"ok": True},
    )

    with TestClient(create_app()):
        pass

    assert released == [True]
