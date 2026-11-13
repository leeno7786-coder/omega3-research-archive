from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from runtime.face.lifecycle import (
    face_inference_session,
    face_model_ids,
    reset_face_session_state_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_face_session() -> None:
    reset_face_session_state_for_tests()


def test_face_model_ids_from_runtime_config() -> None:
    ids = face_model_ids()
    assert "face_det_10g" in ids
    assert "face_w600k_r50" in ids


def test_identify_loads_models_before_inference_and_unloads_after() -> None:
    from runtime.face.recognition import identify

    recognizer = MagicMock()
    recognizer.load.return_value = {"ok": True}
    recognizer.identify.return_value = {
        "ok": True,
        "recognized_name": "operator",
        "confidence": 0.91,
    }
    with patch("runtime.face.recognizer.get_recognizer", return_value=recognizer):
        result = identify(b"fake-jpeg")

    assert result["recognized_name"] == "operator"
    recognizer.load.assert_called_once()
    recognizer.unload.assert_called_once()
    recognizer.identify.assert_called_once_with(b"fake-jpeg")


def test_identify_degraded_when_model_load_fails() -> None:
    from runtime.face.recognition import identify

    recognizer = MagicMock()
    recognizer.load.side_effect = RuntimeError("models unavailable")
    with patch("runtime.face.recognizer.get_recognizer", return_value=recognizer):
        result = identify(b"fake-jpeg")

    assert result["degraded"] is True
    assert result["reason"] == "face_models_unavailable"


def test_face_inference_session_reentrant_single_load() -> None:
    recognizer = MagicMock()
    recognizer.load.return_value = {"ok": True}
    with patch("runtime.face.recognizer.get_recognizer", return_value=recognizer):
        with face_inference_session():
            with face_inference_session():
                pass

    recognizer.load.assert_called_once()
    recognizer.unload.assert_called_once()


def test_enroll_uses_single_load_cycle() -> None:
    from runtime.face.recognition import enroll

    recognizer = MagicMock()
    recognizer.load.return_value = {"ok": True}
    recognizer.identify.return_value = {
        "ok": True,
        "embedding": [0.1] * 512,
        "bbox": [0, 0, 10, 10],
    }
    with (
        patch("runtime.face.recognizer.get_recognizer", return_value=recognizer),
        patch("pressure_loop.operator_vision.queue_face_memory") as admit,
        patch(
            "sensory.operator_presence.enrolled_operator_name",
            return_value="operator",
        ),
    ):
        admit.return_value = {"ok": True, "queued": True, "cycle_id": "test-face-cycle"}
        result = enroll(b"fake-jpeg", "operator")

    assert result["ok"] is True
    assert result["queued"] is True
    admit.assert_called_once()
    recognizer.load.assert_called_once()
    recognizer.unload.assert_called_once()
