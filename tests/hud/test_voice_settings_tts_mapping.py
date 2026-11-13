from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from hud import settings as hud_settings
from hud.server import create_app


def _route(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_voice_speed_and_pitch_persist_and_feed_tts(monkeypatch, tmp_path):
    import runtime.tts as tts

    settings_path = tmp_path / "voice_settings.json"
    monkeypatch.setattr(hud_settings, "_HUD_VOICE_SETTINGS_PATH", settings_path)

    app = create_app()
    voice_post = _route(app, "/api/settings/voice", "POST")
    voice_get = _route(app, "/api/settings/voice", "GET")
    tts_speak = _route(app, "/api/tts/speak", "POST")

    saved_response = asyncio.run(
        voice_post(
            {
                "tts_voice": "Ryan",
                "tts_speed": 1.3,
                "tts_pitch": 1.15,
            }
        )
    )
    saved = json.loads(saved_response.body.decode("utf-8"))
    assert saved["settings"]["tts_speed"] == 1.3
    assert saved["settings"]["tts_pitch"] == 1.15

    loaded = json.loads(asyncio.run(voice_get()).body.decode("utf-8"))
    assert loaded["settings"]["tts_speed"] == 1.3
    assert loaded["settings"]["tts_pitch"] == 1.15

    seen: dict[str, object] = {}

    def fake_synthesize(text, *, voice=None, length_scale=None, pitch=1.0):
        seen["text"] = text
        seen["voice"] = voice
        seen["length_scale"] = length_scale
        seen["pitch"] = pitch
        return b"RIFFwav"

    monkeypatch.setattr(tts, "is_available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", fake_synthesize)

    response = asyncio.run(tts_speak({"text": "voice systems active"}))

    assert response.status_code == 200
    assert seen == {
        "text": "voice systems active",
        "voice": "Ryan",
        "length_scale": 1.3 / 1.15,
        "pitch": 1.15,
    }

    seen.clear()
    response = asyncio.run(
        tts_speak(
            {
                "text": "override voice systems",
                "voice": "Dylan",
                "rate": 0.9,
                "pitch": 0.95,
            }
        )
    )

    assert response.status_code == 200
    assert seen == {
        "text": "override voice systems",
        "voice": "Dylan",
        "length_scale": 0.9 / 0.95,
        "pitch": 0.95,
    }


def test_voice_settings_parse_explicit_boolean_strings(monkeypatch, tmp_path):
    settings_path = tmp_path / "voice_settings.json"
    monkeypatch.setattr(hud_settings, "_HUD_VOICE_SETTINGS_PATH", settings_path)

    saved = hud_settings.save_voice_settings(
        {
            "tts_enabled": "false",
            "auto_speak": "false",
            "stt_enabled": "true",
        }
    )

    assert saved["settings"]["tts_enabled"] is False
    assert saved["settings"]["auto_speak"] is False
    assert saved["settings"]["stt_enabled"] is True
    assert hud_settings.load_voice_settings()["settings"]["tts_enabled"] is False


def test_voice_settings_reject_ambiguous_boolean_values():
    with pytest.raises(ValueError, match="invalid tts_enabled"):
        hud_settings.normalize_voice_settings({"tts_enabled": "sometimes"})


@pytest.mark.parametrize("invalid", [-2, 0, 9, "nan", "inf", "-inf"])
def test_voice_settings_reject_speed_outside_panel_contract(invalid):
    with pytest.raises(ValueError, match="invalid tts_speed"):
        hud_settings.normalize_voice_settings({"tts_speed": invalid})


@pytest.mark.parametrize("invalid", [-2, 0, 9, "nan", "inf", "-inf"])
def test_voice_settings_reject_pitch_outside_panel_contract(invalid):
    with pytest.raises(ValueError, match="invalid tts_pitch"):
        hud_settings.normalize_voice_settings({"tts_pitch": invalid})


def test_voice_settings_accept_numeric_strings_from_form_options():
    normalized = hud_settings.normalize_voice_settings(
        {
            "tts_speed": "1.15",
            "tts_pitch": "0.95",
        }
    )

    assert normalized["settings"]["tts_speed"] == 1.15
    assert normalized["settings"]["tts_pitch"] == 0.95


def test_loading_legacy_numeric_values_preserves_other_voice_preferences(monkeypatch, tmp_path):
    settings_path = tmp_path / "voice_settings.json"
    monkeypatch.setattr(hud_settings, "_HUD_VOICE_SETTINGS_PATH", settings_path)
    settings_path.write_text(
        json.dumps(
            {
                "tts_enabled": False,
                "tts_voice": "Dylan",
                "tts_speed": 1.2,
                "tts_pitch": 1.1,
                "auto_speak": False,
                "stt_enabled": True,
                "stt_language": "es",
                "stt_task": "translate",
                "stt_model": "parakeet_tdt_06b_v3",
                "wake_word_enabled": False,
                "wake_word": "hey omega",
                "voice_activity_detection": True,
                "noise_cancellation": True,
            }
        ),
        encoding="utf-8",
    )

    loaded = hud_settings.load_voice_settings()["settings"]

    assert loaded["tts_enabled"] is False
    assert loaded["tts_voice"] == "Dylan"
    assert loaded["auto_speak"] is False
    assert loaded["stt_language"] == "es"
    assert loaded["stt_task"] == "translate"
    assert loaded["tts_speed"] == hud_settings.HUD_VOICE_DEFAULTS["tts_speed"]
    assert loaded["tts_pitch"] == hud_settings.HUD_VOICE_DEFAULTS["tts_pitch"]


def test_concurrent_voice_settings_saves_do_not_share_a_temp_file(monkeypatch, tmp_path):
    settings_path = tmp_path / "voice_settings.json"
    monkeypatch.setattr(hud_settings, "_HUD_VOICE_SETTINGS_PATH", settings_path)

    payloads = [
        {
            "tts_enabled": index % 2 == 0,
            "tts_voice": "Aiden" if index % 2 == 0 else "Dylan",
            "tts_speed": 1.0 if index % 2 == 0 else 1.15,
            "tts_pitch": 1.0 if index % 2 == 0 else 0.95,
            "auto_speak": index % 3 == 0,
        }
        for index in range(24)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(hud_settings.save_voice_settings, payloads))

    assert all(result["ok"] is True for result in results)
    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert stored["tts_voice"] in {"Aiden", "Dylan"}
    assert not list(tmp_path.glob("*.tmp"))
