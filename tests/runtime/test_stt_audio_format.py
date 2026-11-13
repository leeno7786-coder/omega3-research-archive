from __future__ import annotations

from pathlib import Path

import pytest

from runtime.stt import infer_audio_format


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("recording.webm", "webm"),
        ("clip.WEBM", "webm"),
        (Path("audio.wav"), "wav"),
        ("voice.mp3", "mp3"),
        ("unknown.bin", "wav"),
    ],
)
def test_infer_audio_format_from_suffix(path, expected):
    assert infer_audio_format(path) == expected


def test_infer_audio_format_explicit_override():
    assert infer_audio_format("file.webm", explicit="ogg") == "ogg"
