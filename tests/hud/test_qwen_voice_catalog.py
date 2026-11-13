"""The voice picker must select actual Qwen speakers, not remapped Kokoro IDs."""
import pytest
import re
from pathlib import Path

from hud import settings


def test_qwen_speakers_are_selectable_and_preserved():
    speakers = {"Aiden", "Ryan", "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee"}
    assert {opt["id"] for opt in settings.HUD_VOICE_OPTIONS["tts_voice"]} == speakers
    assert settings.HUD_VOICE_DEFAULTS["tts_voice"] == "Aiden"
    for speaker in speakers:
        assert settings.normalize_voice_settings({"tts_voice": speaker})["settings"]["tts_voice"] == speaker


def test_retired_voice_names_do_not_silently_select_another_speaker():
    result = settings.normalize_voice_settings({"tts_voice": "af_bella"})
    assert result["settings"]["tts_voice"] == "Aiden"
    assert result["migrated_fields"] == ["tts_voice"]


@pytest.mark.parametrize(
    ("legacy", "qwen"),
    [
        ("af_bella", "Aiden"),
        ("af_alloy", "Aiden"),
        ("af_nova", "Aiden"),
        ("af_sky", "Aiden"),
        ("am_adam", "Ryan"),
        ("am_echo", "Ryan"),
        ("am_onyx", "Ryan"),
        ("bf_alice", "Aiden"),
        ("bm_fable", "Ryan"),
        ("jarvis", "Ryan"),
        ("shimmer", "Aiden"),
    ],
)
def test_legacy_voice_settings_migrate_to_a_qwen_speaker(tmp_path, monkeypatch, legacy, qwen):
    path = tmp_path / "voice_settings.json"
    path.write_text('{"tts_voice": "' + legacy + '"}\n', encoding="utf-8")
    monkeypatch.setattr(settings, "_HUD_VOICE_SETTINGS_PATH", path)

    loaded = settings.load_voice_settings()

    assert loaded["settings"]["tts_voice"] == qwen
    assert loaded["migrated_fields"] == ["tts_voice"]
    assert '"tts_voice": "' + qwen + '"' in path.read_text(encoding="utf-8")


def test_rendered_voice_picker_matches_server_catalog():
    source = (Path(__file__).resolve().parents[2] / "hud/ui-src/panels/settings-markup.js").read_text(encoding="utf-8")
    picker = source.split('id="hud-tts-voice"', 1)[1].split('</select>', 1)[0]
    assert set(re.findall(r'value="([^"]+)"', picker)) == {
        item["id"] for item in settings.HUD_VOICE_OPTIONS["tts_voice"]
    }
