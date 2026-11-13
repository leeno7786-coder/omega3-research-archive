"""The HUD settings surface reports whether settings were ever stored.

A factory reset clears ``guardian/state``, so the theme and voice files are gone. The
operator's browser profile is not on the SSD, so the HUD client cannot be wiped from
disk — it learns a reset happened from this flag and drops its cached ``omega.hud.*``
state (tests/hud/test_hud_client_state_purge.mjs).

Law §2.1 in miniature: "never configured" and "configured to the defaults" must not
share a return value. A file that exists but cannot be READ is still stored — that
failure is already reported degraded, and the client cache may hold the only copy of
the operator's preferences left.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hud.settings as hud_settings


@pytest.fixture
def settings_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {"theme": tmp_path / "theme_settings.json", "voice": tmp_path / "voice_settings.json"}
    monkeypatch.setattr(hud_settings, "_HUD_THEME_SETTINGS_PATH", paths["theme"])
    monkeypatch.setattr(hud_settings, "_HUD_VOICE_SETTINGS_PATH", paths["voice"])
    return paths


def test_absent_theme_settings_report_stored_false(settings_paths: dict[str, Path]) -> None:
    result = hud_settings.load_hud_theme_settings()

    assert result["stored"] is False, "an absent file is not a configured default"
    assert result["settings"] == dict(hud_settings.HUD_THEME_DEFAULTS)
    assert result["ok"] is True


def test_absent_voice_settings_report_stored_false(settings_paths: dict[str, Path]) -> None:
    result = hud_settings.load_voice_settings()

    assert result["stored"] is False
    assert result["settings"] == dict(hud_settings.HUD_VOICE_DEFAULTS)


def test_saved_settings_report_stored_true(settings_paths: dict[str, Path]) -> None:
    hud_settings.save_hud_theme_settings({"theme": "night_garden"})

    result = hud_settings.load_hud_theme_settings()

    assert result["stored"] is True
    assert result["settings"]["theme"] == "night_garden"


def test_a_corrupt_file_still_counts_as_stored(settings_paths: dict[str, Path]) -> None:
    """Unreadable is not absent: purging the client would destroy the last copy."""
    settings_paths["theme"].write_text("{not json", encoding="utf-8")
    settings_paths["voice"].write_text("{not json", encoding="utf-8")

    theme = hud_settings.load_hud_theme_settings()
    voice = hud_settings.load_voice_settings()

    assert theme["stored"] is True, "a file that exists was configured, however broken"
    assert voice["stored"] is True
    assert theme["settings"] == dict(hud_settings.HUD_THEME_DEFAULTS), "still serves defaults"


def test_the_flag_travels_with_every_settings_read(settings_paths: dict[str, Path]) -> None:
    """The client keys its purge on this field, so it is never optional."""
    for reader in (hud_settings.load_hud_theme_settings, hud_settings.load_voice_settings):
        assert "stored" in reader(), f"{reader.__name__} must report stored"
