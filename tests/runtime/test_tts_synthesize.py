from __future__ import annotations

import io
import wave

import pytest

from runtime import tts


def _tiny_wav(sample_rate: int = 22050) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * 8)
    return out.getvalue()


def test_synthesize_passes_voice_and_length_scale(monkeypatch):
    seen: dict[str, object] = {}

    class _Client:
        def synthesise(self, text, *, voice="af_bella", length_scale=1.0):
            seen["text"] = text
            seen["voice"] = voice
            seen["length_scale"] = length_scale
            return b"RIFFwav"

    monkeypatch.setattr(tts, "get_runtime_client", lambda: _Client())

    out = tts.synthesize(
        "systems online",
        voice="af_bella",
        length_scale=0.87,
    )
    assert out == b"RIFFwav"
    assert seen["text"] == "systems online"
    assert seen["voice"] == "af_bella"
    assert seen["length_scale"] == 0.87


def test_synthesize_pcm_delegates_voice_settings(monkeypatch):
    seen: dict[str, object] = {}

    def _fake_synthesize(text, *, voice="af_bella", length_scale=1.0):
        seen["voice"] = voice
        seen["length_scale"] = length_scale
        return b""

    monkeypatch.setattr(tts, "synthesize", _fake_synthesize)
    tts.synthesize_pcm("hello", voice="nova", length_scale=1.2)
    assert seen["voice"] == "nova"
    assert seen["length_scale"] == 1.2


def test_synthesize_applies_pitch_to_wav_output(monkeypatch):
    class _Client:
        def synthesise(self, text, *, voice="af_bella", length_scale=1.0):
            return _tiny_wav(sample_rate=22050)

    monkeypatch.setattr(tts, "get_runtime_client", lambda: _Client())

    out = tts.synthesize("systems online", pitch=1.15)

    with wave.open(io.BytesIO(out), "rb") as wf:
        assert wf.getframerate() == 25358


def test_availability_and_status_come_from_the_runtime(monkeypatch):
    """The brain is a thin client — the local service owns Qwen.

    Availability is service health and the device/provider come from the
    service's actual health contract.
    """
    calls = {"health": 0}

    class _Client:
        base_url = "http://runtime.invalid"

        def health(self):
            calls["health"] += 1
            return True

        def health_body(self):
            return {
                "status": "ok",
                "capability_health": {"tts": {
                    "engine": "qwen3-tts", "ok": True, "resident": True, "warmed": True,
                }},
                "devices": {
                    "tts": {"device": "gpu", "provider": "DmlExecutionProvider"},
                },
            }

    monkeypatch.setattr(tts, "get_runtime_client", lambda: _Client())
    monkeypatch.setattr(tts, "_tts_config", lambda: {})

    assert tts.is_available() is True
    assert tts.status() == {
        "base": "http://runtime.invalid",
        "available": True,
        "service": "tts",
        "engine": "qwen3-tts",
        "backend": "local_services",
        "service_available": True,
        "resident": True,
        "warmed": True,
        "expected_engine": "qwen3-tts",
        "reason": None,
        "device": "gpu",
        "execution_provider": "DmlExecutionProvider",
        "synthesise_timeout_seconds": 180,
    }
    assert calls["health"] >= 1


def test_status_reports_unavailable_when_the_runtime_is_down(monkeypatch):
    class _Client:
        base_url = "http://runtime.invalid"

        def health(self):
            return False

        def health_body(self):
            return None

    monkeypatch.setattr(tts, "get_runtime_client", lambda: _Client())
    monkeypatch.setattr(tts, "_tts_config", lambda: {})

    assert tts.is_available() is False
    assert tts.status()["backend"] == "unavailable"


@pytest.mark.parametrize("engine", ["kokoro", "unknown", ""])
def test_status_refuses_non_qwen_gateway(monkeypatch, engine):
    class _Client:
        base_url = "http://runtime.invalid"

        def health(self):
            return True

        def health_body(self):
            capability = {"ok": True, "resident": True, "warmed": True}
            if engine:
                capability["engine"] = engine
            return {"devices": {"tts": {}}, "capability_health": {"tts": capability}}

    monkeypatch.setattr(tts, "get_runtime_client", lambda: _Client())
    monkeypatch.setattr(tts, "_tts_config", lambda: {})

    result = tts.status()

    assert result["available"] is False
    assert result["reason"] == "tts_engine_mismatch"
    assert result["expected_engine"] == "qwen3-tts"


def test_the_brain_carries_no_in_process_tts_engine():
    """Doctrine guard: model loading lives in local services, not the brain."""
    import importlib.util

    assert importlib.util.find_spec("runtime.local_tts") is None
