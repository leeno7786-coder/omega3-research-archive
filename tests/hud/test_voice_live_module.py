"""The live conversation module: registered, bundled, and behaving (Node VM)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"
MONOLITH = ROOT / "hud" / "omega_interface.html"


def test_voice_live_is_registered_after_the_stt_section() -> None:
    manifest = json.loads((UI_SRC / "manifest.json").read_text(encoding="utf-8"))
    ids = [entry.get("id") for entry in manifest["core"]]
    assert "voice-live" in ids
    assert ids.index("voice-live") == ids.index("voice-stt") + 1
    entry = next(e for e in manifest["core"] if e.get("id") == "voice-live")
    assert entry["file"] == "core/voice-live.js" and entry["inject"] == "voice_stt"
    assert (UI_SRC / "core" / "voice-live.js").is_file()


def test_bundle_carries_the_live_loop_and_the_settings_toggle() -> None:
    html = MONOLITH.read_text(encoding="utf-8")
    assert "const OmegaLive = (() => {" in html
    assert 'name="live_mode"' in html
    assert "chat.interrupt" in html


def test_voice_runtime_prefers_live_capture_and_never_double_speaks() -> None:
    source = (UI_SRC / "core" / "voice-runtime.js").read_text(encoding="utf-8")
    assert "_startLiveCapture()" in source
    assert "liveSettings.live_mode === false" in source
    assert "OmegaLive.alreadySpoken(String(text), opts && opts.cycleId)" in source
    assert "isSpeaking() { return !!playing || QUEUE.length > 0 || _streamSources.length > 0; }" in source
    feed = (UI_SRC / "core" / "chat-feed.js").read_text(encoding="utf-8")
    assert "!(delivery && delivery.interrupted)" in feed


def test_live_mode_is_a_default_on_voice_setting() -> None:
    from hud.settings import HUD_VOICE_DEFAULTS

    assert HUD_VOICE_DEFAULTS["live_mode"] is True
    theme = (UI_SRC / "panels" / "settings-theme.js").read_text(encoding="utf-8")
    assert "live_mode: true," in theme and "live_mode: parseBool(data.get('live_mode'))" in theme


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_voice_live_runtime_behaviour_mjs() -> None:
    script = ROOT / "tests" / "hud" / "test_voice_live.mjs"
    result = subprocess.run(["node", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
