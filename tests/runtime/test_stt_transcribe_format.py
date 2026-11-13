from __future__ import annotations

from pathlib import Path

import pytest

from runtime import stt


def test_transcribe_passes_webm_format_to_runtime(tmp_path, monkeypatch):
    audio_path = tmp_path / "clip.webm"
    audio_path.write_bytes(b"\x00\x01fake-webm")

    seen: dict[str, str] = {}

    class _Client:
        def transcribe(self, audio_bytes, *, audio_format="wav", language="en", vad_filter=True):
            seen["format"] = audio_format
            seen["bytes"] = len(audio_bytes)
            return "hello"

    monkeypatch.setattr(stt, "get_runtime_client", lambda: _Client())
    monkeypatch.setattr(stt, "emit_inference", lambda *a, **k: None, raising=False)

    text = stt.transcribe(audio_path, language="en")
    assert text == "hello"
    assert seen["format"] == "webm"
    assert seen["bytes"] == len(audio_path.read_bytes())
