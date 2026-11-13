from __future__ import annotations

import cv2
import numpy as np

from core.paths import RUNTIME_MODELS_ROOT
from runtime.face.recognizer import FaceRecognizer, face_model_root


def test_cpu_face_engine_loads_declared_models_and_handles_valid_image() -> None:
    engine = FaceRecognizer()
    try:
        status = engine.load()
        assert status["ok"] is True
        assert status["provider"] == "CPUExecutionProvider"
        assert status["models_loaded"] is True

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", image)
        assert ok
        result = engine.identify(encoded.tobytes())

        assert result["ok"] is True
        assert result["degraded"] is False
        assert result["face_count"] == 0
        assert result["recognized_name"] is None
    finally:
        engine.unload()


def test_face_tool_socket_uses_brain_local_engine(monkeypatch) -> None:
    import runtime.face.recognition as face_recognition
    from runtime.socket import invoke_runtime_tool

    monkeypatch.setattr(
        face_recognition,
        "identify",
        lambda image_bytes: {"ok": True, "degraded": False, "face_count": 0},
    )

    result = invoke_runtime_tool(
        "face_tool",
        operation="tool_face_tool",
        episode_id="face-local-dispatch",
        image_bytes=b"jpeg",
    )

    assert result["ok"] is True
    assert result["degraded"] is False


def test_face_model_root_cannot_follow_an_external_machine_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OMEGA_FACE_MODEL_DIR", str(tmp_path))

    assert face_model_root() == RUNTIME_MODELS_ROOT / "onnxs" / "face"
