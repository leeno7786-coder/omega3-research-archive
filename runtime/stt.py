"""Speech-to-text via Omega's portable Parakeet service."""

from __future__ import annotations

import io
import logging
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from runtime.client import get_runtime_client

log = logging.getLogger("omega.runtime.stt")

_SUFFIX_TO_FORMAT = {
    ".wav": "wav",
    ".webm": "webm",
    ".mp3": "mp3",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".m4a": "m4a",
    ".flac": "flac",
    ".mp4": "mp4",
}


@dataclass
class TranscriptionResult:
    text: str
    segments: List[dict] = field(default_factory=list)
    language: str = "en"
    detected_language: str = "en"
    language_probability: float = 1.0
    duration: float = 0.0


def infer_audio_format(audio: Any, *, explicit: Optional[str] = None) -> str:
    """Map browser/upload containers to the runtime ``format`` field."""
    if explicit:
        raw = str(explicit).strip().lower().lstrip(".")
        return raw or "wav"
    if isinstance(audio, (str, Path)):
        suffix = Path(audio).suffix.lower()
        return _SUFFIX_TO_FORMAT.get(suffix, "wav")
    return "wav"


def _read_audio_bytes(audio: Union[str, bytes, Path, Any]) -> Optional[bytes]:
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    if isinstance(audio, (str, Path)):
        try:
            return Path(audio).read_bytes()
        except OSError as exc:
            log.warning("stt: cannot read audio file %s: %s", audio, exc)
            return None
    if hasattr(audio, "read"):
        try:
            return audio.read()
        except Exception as exc:
            log.warning("stt: file-like audio read failed: %s", exc)
            return None
    try:
        import numpy as _np

        if isinstance(audio, _np.ndarray):
            pcm = (audio * 32767.0).clip(-32768, 32767).astype("int16").tobytes()
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)
            return buf.getvalue()
    except Exception as exc:
        log.warning("stt: numpy → wav conversion failed: %s", exc)
    return None


def _prepare_audio(
    audio: Any,
    *,
    audio_format: Optional[str] = None,
) -> Tuple[Optional[bytes], str]:
    payload = _read_audio_bytes(audio)
    fmt = infer_audio_format(audio, explicit=audio_format)
    return payload, fmt


def transcribe(
    audio: Any,
    *,
    language: str = "auto",
    audio_format: Optional[str] = None,
    task: Optional[str] = None,
) -> str:
    import time

    t0 = time.perf_counter()
    payload, fmt = _prepare_audio(audio, audio_format=audio_format)
    if payload is None:
        try:
            from guardian.terminal_feedback import emit_inference

            emit_inference(
                "runtime",
                "stt",
                ok=False,
                status="fail",
                latency_ms=int((time.perf_counter() - t0) * 1000),
                reason="audio_unreadable",
            )
        except Exception:
            pass
        return ""
    latency_ms = int((time.perf_counter() - t0) * 1000)
    client = get_runtime_client()
    kwargs = {
        "audio_format": fmt,
        "language": language,
    }
    import inspect

    try:
        sig = inspect.signature(client.transcribe)
        if "task" in sig.parameters:
            kwargs["task"] = task
    except Exception:
        pass
    text = client.transcribe(payload, **kwargs)
    try:
        from guardian.terminal_feedback import emit_inference

        if text is None:
            emit_inference(
                "runtime",
                "stt",
                ok=False,
                status="fail",
                latency_ms=latency_ms,
                language=language,
                audio_format=fmt,
                audio_bytes=len(payload),
                reason="runtime_unavailable",
            )
        elif not text:
            # Silence / VAD-filtered clips are normal during idle voice listen.
            emit_inference(
                "runtime",
                "stt",
                ok=True,
                status="ok",
                latency_ms=latency_ms,
                language=language,
                audio_format=fmt,
                audio_bytes=len(payload),
                outcome="empty",
            )
        else:
            emit_inference(
                "runtime",
                "stt",
                ok=True,
                status="ok",
                latency_ms=latency_ms,
                language=language,
                audio_format=fmt,
                audio_bytes=len(payload),
            )
    except Exception:
        pass
    return text or ""


def transcribe_with_segments(
    audio: Any,
    *,
    language: str = "auto",
    audio_format: Optional[str] = None,
    task: Optional[str] = None,
) -> TranscriptionResult:
    text = transcribe(audio, language=language, audio_format=audio_format, task=task)
    return TranscriptionResult(
        text=text,
        segments=[],
        language=language,
        detected_language=language,
        language_probability=1.0,
        duration=0.0,
    )


def is_available() -> bool:
    return get_runtime_client().health()


def status() -> dict:
    rt = get_runtime_client()
    available = rt.health()
    health = rt.health_body() or {}
    device = ((health.get("devices") or {}).get("stt") or {})
    return {
        "base": rt.base_url,
        "available": available,
        "service": "stt",
        "engine": "parakeet",
        "backend": str(device.get("backend") or "unknown"),
        "device": str(device.get("device") or "unknown"),
    }
