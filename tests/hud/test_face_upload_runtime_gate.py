from __future__ import annotations

from unittest.mock import patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from hud.server import create_app


def test_upload_face_blocked_when_brain_face_models_unavailable():
    client = TestClient(create_app())
    blocked = JSONResponse(
        {
            "ok": False,
            "error": "brain-local CPU face model bundle unavailable",
            "reason": "face_models_unavailable",
            "runtime": "brain-local-cpu",
        }
    )
    with patch("hud.server._require_face_runtime", return_value=blocked):
        response = client.post(
            "/api/vision/upload-face",
            data={"name": "operator"},
            files={"file": ("face.jpg", b"fake-jpeg", "image/jpeg")},
        )
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert body["reason"] == "face_models_unavailable"
    assert "brain-local-cpu" in body["runtime"]
