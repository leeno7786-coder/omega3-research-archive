"""hop_engine's backend receipt must surface the NPU lane honestly.

The lane is DORMANT until the sharded-session integration lands: the
receipt reports availability facts (is the VitisAI EP importable, is the
proven backend module present) without ever claiming execution — that
claim stays reserved for npu_backend's profile-based receipts.
"""

from __future__ import annotations

from memory.dme import hop_engine, npu_backend


def test_backend_receipt_surfaces_npu_lane():
    receipt = hop_engine.ensure_backend_loaded()
    lane = receipt.get("npu_lane")
    assert isinstance(lane, dict), "receipt must carry an npu_lane block"
    assert lane["backend_module"] == "memory.dme.npu_backend"
    assert lane["vitisai_available"] == npu_backend.vitisai_available()
    # Dormant lane never claims execution; only a live profile may.
    assert lane["provider_execution_proven"] is False


def test_vitisai_is_first_preference():
    assert hop_engine.PREFERRED_ONNX_PROVIDERS[0] == "VitisAIExecutionProvider"
    assert hop_engine.PREFERRED_ONNX_PROVIDERS[-1] == "CPUExecutionProvider"
