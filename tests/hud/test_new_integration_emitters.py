"""Failure-log coverage for NEW integration HUD endpoints (emitter audit fixes)."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from guardian.telemetry import writer
from hud.server import create_app


def _failure_records():
    p = writer.FAILURE_LOG
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _has_failure(*, phase: str, status: str | None = None) -> bool:
    for rec in _failure_records():
        if rec.get("phase") != phase:
            continue
        if status is not None and rec.get("status") != status:
            continue
        return True
    return False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "FAILURE_LOG", tmp_path / "failures.jsonl")
    monkeypatch.setattr(writer, "TELEMETRY_DIR", tmp_path / "telemetry")
    writer.init_run_id("hud-integration-test")
    return TestClient(create_app())


def test_vision_identify_upload_empty_file_reaches_failure_log(client):
    r = client.post(
        "/api/vision/identify-upload",
        files={"file": ("face.jpg", b"", "image/jpeg")},
    )
    assert r.status_code == 400
    assert _has_failure(phase="vision_identify_upload", status="fail")


def test_vision_identify_upload_degraded_reaches_failure_log(client):
    with patch(
        "runtime.face.recognition.identify",
            return_value={"degraded": True, "reason": "face_models_unavailable"},
    ):
        r = client.post(
            "/api/vision/identify-upload",
            files={"file": ("face.jpg", b"fake", "image/jpeg")},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert _has_failure(phase="vision_identify_upload", status="degraded")


def test_vision_upload_face_no_embedding_reaches_failure_log(client):
    with (
        patch("hud.server._require_face_runtime", return_value=None),
        patch(
            "runtime.face.recognition.identify",
            return_value={"ok": True, "embedding": [0.1] * 8},
        ),
    ):
        r = client.post(
            "/api/vision/upload-face",
            data={"name": "operator", "context": ""},
            files={"file": ("face.jpg", b"fake", "image/jpeg")},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert _has_failure(phase="vision_upload_face", status="fail")


def test_stt_status_exception_reaches_failure_log(client):
    with patch("runtime.stt.status", side_effect=RuntimeError("stt down")):
        r = client.get("/api/stt/status")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert _has_failure(phase="stt_status", status="degraded")


def test_uploads_get_unknown_kind_reaches_failure_log(client):
    r = client.get("/api/uploads/badkind/file.txt")
    assert r.status_code == 404
    assert _has_failure(phase="uploads_get", status="fail")
