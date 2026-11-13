"""CPU face-identification contract.

The retired runtime HTTP face service is not part of this test. The brain owns
the CPU ONNX sessions and still returns an explicit degraded state when the
local model bundle cannot be loaded.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from runtime.face import recognition


def test_local_face_identify_normalizes_recognized_keys() -> None:
    recognizer = MagicMock()
    recognizer.load.return_value = {"ok": True}
    recognizer.identify.return_value = {
        "ok": True,
        "degraded": False,
        "name": "Noah",
        "confidence": 0.91,
        "bbox": [1, 2, 3, 4],
    }
    with patch("runtime.face.recognizer.get_recognizer", return_value=recognizer):
        result = recognition.identify(b"fake-jpeg")

    assert result["name"] == "Noah"
    assert result["recognized_name"] == "Noah"
    assert result["degraded"] is False


def test_local_face_identify_distinguishes_no_face_from_model_failure() -> None:
    recognizer = MagicMock()
    recognizer.load.return_value = {"ok": True}
    recognizer.identify.return_value = {
        "ok": True,
        "degraded": False,
        "name": None,
        "recognized_name": None,
        "reason": "no_face_detected",
        "face_count": 0,
    }
    with patch("runtime.face.recognizer.get_recognizer", return_value=recognizer):
        no_face = recognition.identify(b"fake-jpeg")

    assert no_face["degraded"] is False
    assert no_face["reason"] == "no_face_detected"

    unavailable = MagicMock()
    unavailable.load.side_effect = RuntimeError("models missing")
    with patch("runtime.face.recognizer.get_recognizer", return_value=unavailable):
        down = recognition.identify(b"fake-jpeg")

    assert down["degraded"] is True
    assert down["reason"] == "face_models_unavailable"
