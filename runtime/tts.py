"""Text-to-speech via Omega's portable Qwen runtime service."""

from __future__ import annotations

import io
import logging
import wave
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import AsyncIterator, Union

from runtime.client import get_runtime_client

log = logging.getLogger("omega.runtime.tts")

# Qwen native rate — playback callers need this sample rate.
TTS_SAMPLE_RATE = 24000


def _tts_config() -> dict:
    try:
        from config.loader import load_subsystem_bundle

        block = (load_subsystem_bundle("tts").get("config") or {}).get("tts") or {}
        return dict(block) if isinstance(block, dict) else {}
    except Exception:
        return {}


def _positive_float(value: object, fallback: float = 1.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return fallback
    return out if out > 0 else fallback


def _apply_pitch_to_wav(wav: bytes, pitch: float) -> bytes:
    if not wav or abs(pitch - 1.0) < 0.001:
        return wav
    try:
        src = io.BytesIO(wav)
        with wave.open(src, "rb") as wf:
            params = wf.getparams()
            frames = wf.readframes(params.nframes)

        shifted_rate = max(
            1,
            int(
                (Decimal(params.framerate) * Decimal(str(pitch))).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            ),
        )
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(params.nchannels)
            wf.setsampwidth(params.sampwidth)
            wf.setframerate(shifted_rate)
            wf.writeframes(frames)
        return out.getvalue()
    except Exception as exc:
        log.warning("tts: WAV pitch adjustment failed: %s", exc)
        return wav


def synthesize(
    text: str,
    *,
    voice: str | None = None,
    length_scale: float | None = None,
    pitch: float | None = None,
) -> bytes:
    import time

    if not text or not text.strip():
        return b""
    cfg = _tts_config()
    resolved_voice = voice if voice is not None else str(cfg.get("default_voice") or "Aiden")
    resolved_scale = (
        _positive_float(length_scale)
        if length_scale is not None
        else _positive_float(cfg.get("length_scale"), 1.0)
    )
    resolved_pitch = (
        _positive_float(pitch) if pitch is not None else _positive_float(cfg.get("pitch"), 1.0)
    )
    t0 = time.perf_counter()
    audio = get_runtime_client().synthesise(
        text,
        voice=resolved_voice,
        length_scale=resolved_scale,
    )
    if audio:
        audio = _apply_pitch_to_wav(audio, resolved_pitch)
    ok = bool(audio)
    try:
        from guardian.terminal_feedback import emit_inference

        emit_inference(
            "runtime",
            "tts",
            ok=ok,
            status="ok" if ok else "fail",
            latency_ms=int((time.perf_counter() - t0) * 1000),
            text_len=len(text),
            audio_bytes=len(audio) if audio else 0,
            pitch=resolved_pitch,
        )
    except Exception:
        pass
    return audio or b""


def synthesize_pcm(
    text: str,
    *,
    voice: str | None = None,
    length_scale: float | None = None,
    pitch: float | None = None,
) -> bytes:
    kwargs = {"voice": voice, "length_scale": length_scale}
    if pitch is not None:
        kwargs["pitch"] = pitch
    wav = synthesize(text, **kwargs)
    if not wav:
        return b""
    try:
        with wave.open(io.BytesIO(wav), "rb") as wf:
            return wf.readframes(wf.getnframes())
    except Exception as exc:
        log.warning("tts: WAV → PCM extraction failed: %s", exc)
        return b""


def synthesize_stream(
    text: str,
    *,
    voice: str | None = None,
    length_scale: float | None = None,
) -> AsyncIterator[bytes]:
    """Yield raw 24 kHz mono PCM from the runtime-owned streaming lane."""
    cfg = _tts_config()
    resolved_voice = voice if voice is not None else str(cfg.get("default_voice") or "Aiden")
    resolved_scale = (
        _positive_float(length_scale)
        if length_scale is not None
        else _positive_float(cfg.get("length_scale"), 1.0)
    )
    return get_runtime_client().synthesise_stream(
        text,
        voice=resolved_voice,
        length_scale=resolved_scale,
    )


def synthesize_to_file(text: str, path: Union[str, Path]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(synthesize(text))
    return out.resolve()


def is_available() -> bool:
    return status()["available"]


def status() -> dict:
    rt = get_runtime_client()
    service_available = bool(rt.health())
    health = rt.health_body() or {}
    device = ((health.get("devices") or {}).get("tts") or {})
    cfg = _tts_config()
    capability = ((health.get("capability_health") or {}).get("tts") or {})
    expected_engine = "qwen3-tts"
    engine = str(capability.get("engine") or "unknown")
    engine_matches = engine.casefold() == expected_engine
    resident = service_available and capability.get("resident") is True
    warmed = resident and capability.get("warmed") is True
    available = warmed and capability.get("ok") is True and engine_matches
    return {
        "base": rt.base_url,
        "available": available,
        "service": "tts",
        "engine": engine,
        "backend": "local_services" if service_available else "unavailable",
        "service_available": service_available,
        "expected_engine": expected_engine,
        "reason": None if engine_matches else "tts_engine_mismatch",
        "resident": resident,
        "warmed": warmed,
        "device": str(device.get("device") or "unknown"),
        "execution_provider": str(device.get("provider") or "unknown"),
        "synthesise_timeout_seconds": int(cfg.get("synthesise_timeout_seconds") or 180),
    }


def warmup() -> bool:
    """Confirm boot warmup without submitting another synthesis request."""
    cfg = _tts_config()
    if not cfg.get("warmup_on_boot", True):
        return False
    health = get_runtime_client().health_body() or {}
    speech = (health.get("capability_health") or {}).get("tts") or {}
    if not all(speech.get(key) is True for key in ("ok", "warmed", "resident")):
        raise RuntimeError(f"TTS boot warmup is not ready: {speech}")
    return True
