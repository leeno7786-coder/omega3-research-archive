"""TTS runtime reads config via subsystem bundle."""

from __future__ import annotations

from config import loader
from runtime import tts


def test_tts_config_reads_subsystem_bundle():
    loader.clear_cache()
    cfg = tts._tts_config()
    live = loader.load_subsystem_bundle("tts")["config"]["tts"]
    assert cfg.get("default_voice") == live.get("default_voice")
