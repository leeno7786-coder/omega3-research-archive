"""One-shot extractor for HUD Phase 14 modules."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"
lines = (UI_SRC / "scripts.html").read_text(encoding="utf-8").splitlines()


def extract(start: int, end: int, dedent: int = 4) -> str:
    chunk = lines[start - 1 : end]
    if dedent:
        chunk = [ln[dedent:] if ln.startswith(" " * dedent) else ln for ln in chunk]
    return "\n".join(chunk).rstrip() + "\n"


def extract_script_block(start: int, end: int) -> str:
    chunk = lines[start - 1 : end]
    out: list[str] = []
    for ln in chunk:
        if ln.startswith("      "):
            out.append(ln[6:])
        elif ln.startswith("    "):
            out.append(ln[4:])
        else:
            out.append(ln)
    return "\n".join(out).rstrip() + "\n"


voice_body = extract(25, 367)
jarvis_body = extract_script_block(6681, 6836)
stt_body = extract_script_block(6838, 7244)
wake_body = extract_script_block(7246, 7383)

voice_runtime = f"""// Voice + Jarvis TTS + backend Whisper STT (hud/ui-src/core/voice-runtime.js)
// Depends on: Telemetry, ChatBridge, Vision, OmegaUI, appendLine, currentVoiceSettings
// Sections: voice (script 1), jarvis/stt/wake (LIVE WIRING script block)

// SECTION:voice
{voice_body}// SECTION:jarvis
{jarvis_body}// SECTION:stt
{stt_body}// SECTION:wake
{wake_body}"""
(UI_SRC / "core/voice-runtime.js").write_text(voice_runtime, encoding="utf-8")

vision_body = extract(368, 521)
facewatch_body = extract_script_block(5385, 5731)

vision_runtime = f"""// Vision capture + FaceWatch (hud/ui-src/core/vision-runtime.js)
// Depends on: Telemetry, Voice, OperatorIdentity, OmegaUI, openPanel
// Sections: vision (script 1), facewatch (LIVE WIRING script block)

// SECTION:vision
{vision_body}// SECTION:facewatch
{facewatch_body}"""
(UI_SRC / "core/vision-runtime.js").write_text(vision_runtime, encoding="utf-8")

canvas_panels = f"""// Panel-local canvas renderers (hud/ui-src/core/canvas-panels.js)
// Depends on: _escapeHtml, _traitVisualWeight, _render*Dashboard, _PANEL_LIVE_REFRESH, _refresh*Panel

{extract(4988, 5301)}"""
(UI_SRC / "core/canvas-panels.js").write_text(canvas_panels, encoding="utf-8")

print("voice-runtime.js", len(voice_runtime.splitlines()), "lines")
print("vision-runtime.js", len(vision_runtime.splitlines()), "lines")
print("canvas-panels.js", len(canvas_panels.splitlines()), "lines")
