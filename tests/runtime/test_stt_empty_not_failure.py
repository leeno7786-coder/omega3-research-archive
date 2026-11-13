from __future__ import annotations

import json
from pathlib import Path

import pytest

from guardian.telemetry import writer
from runtime import stt


@pytest.fixture()
def failure_log(tmp_path, monkeypatch):
    path = tmp_path / "failures.jsonl"
    monkeypatch.setattr(writer, "FAILURE_LOG", path)
    monkeypatch.setattr(writer, "TELEMETRY_DIR", tmp_path / "telemetry")
    writer.reset_for_tests()
    writer.init_run_id("test-run")
    return path


def _failures(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_empty_transcript_does_not_reach_failure_log(tmp_path, failure_log, monkeypatch):
    audio_path = tmp_path / "clip.webm"
    audio_path.write_bytes(b"\x00\x01fake-webm")

    class _Client:
        def transcribe(self, audio_bytes, *, audio_format="wav", language="en", vad_filter=True):
            return ""

    monkeypatch.setattr(stt, "get_runtime_client", lambda: _Client())

    text = stt.transcribe(audio_path, language="en")
    assert text == ""
    assert not _failures(failure_log)


def test_runtime_unavailable_reaches_failure_log(tmp_path, failure_log, monkeypatch):
    audio_path = tmp_path / "clip.webm"
    audio_path.write_bytes(b"\x00\x01fake-webm")

    class _Client:
        def transcribe(self, audio_bytes, *, audio_format="wav", language="en", vad_filter=True):
            return None

    monkeypatch.setattr(stt, "get_runtime_client", lambda: _Client())

    text = stt.transcribe(audio_path, language="en")
    assert text == ""
    fails = _failures(failure_log)
    assert len(fails) == 1
    assert fails[0]["phase"] == "stt"
    assert fails[0]["status"] == "fail"


def test_benign_runtime_no_speech_returns_empty_string(monkeypatch):
    from runtime.client import RuntimeClient

    client = RuntimeClient(base_url="http://127.0.0.1:9")
    monkeypatch.setattr(
        client,
        "_post",
        lambda *a, **k: {"ok": False, "reason": "no_speech"},
    )
    assert client.transcribe(b"audio") == ""
