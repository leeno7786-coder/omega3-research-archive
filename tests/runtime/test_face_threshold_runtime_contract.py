"""Brain-local CPU face threshold contract."""

from __future__ import annotations

def test_brain_face_status_exposes_cpu_provider_and_threshold():
    from runtime.face.anchor_store import RECOGNITION_THRESHOLD
    from runtime.face.recognition import face_status

    status = face_status()
    assert status["runtime"] == "brain-local-cpu"
    assert status["provider"] == "CPUExecutionProvider"
    assert RECOGNITION_THRESHOLD == 0.4
