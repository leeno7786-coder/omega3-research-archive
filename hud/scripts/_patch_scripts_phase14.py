"""Patch scripts.html for Phase 14 inject markers."""

from __future__ import annotations

from pathlib import Path

UI_SRC = Path(__file__).resolve().parents[2] / "hud" / "ui-src"
path = UI_SRC / "scripts.html"
lines = path.read_text(encoding="utf-8").splitlines()

# 1-based inclusive line numbers from pre-patch scripts.html
VOICE_START, VOICE_END = 25, 367
VISION_START, VISION_END = 368, 521
CANVAS_PANELS_START, CANVAS_PANELS_END = 4988, 5301
FACEWATCH_START, FACEWATCH_END = 5385, 5731
JARVIS_START, JARVIS_END = 6681, 6836
STT_START, STT_END = 6838, 7244
WAKE_START, WAKE_END = 7246, 7383


def replace_range(start: int, end: int, replacement: list[str]) -> None:
    del lines[start - 1 : end]
    for i, ln in enumerate(replacement):
        lines.insert(start - 1 + i, ln)


patches = [
    (
        JARVIS_START,
        WAKE_END,
        [
            "      // ── Jarvis TTS + Whisper STT + wake routing — hud/ui-src/core/voice-runtime.js ──",
            "      // HUD_UI_SRC:INJECT_VOICE_JARVIS_START",
            "      // HUD_UI_SRC:INJECT_VOICE_JARVIS_END",
            "      // HUD_UI_SRC:INJECT_VOICE_STT_START",
            "      // HUD_UI_SRC:INJECT_VOICE_STT_END",
            "      // HUD_UI_SRC:INJECT_VOICE_WAKE_START",
            "      // HUD_UI_SRC:INJECT_VOICE_WAKE_END",
        ],
    ),
    (
        FACEWATCH_START,
        FACEWATCH_END,
        [
            "      // ── FaceWatch — hud/ui-src/core/vision-runtime.js (SECTION:facewatch) ──",
            "      // HUD_UI_SRC:INJECT_VISION_FACEWATCH_START",
            "      // HUD_UI_SRC:INJECT_VISION_FACEWATCH_END",
        ],
    ),
    (
        CANVAS_PANELS_START,
        CANVAS_PANELS_END,
        [
            "    // ── Panel canvas renderers — hud/ui-src/core/canvas-panels.js ──",
            "    // HUD_UI_SRC:INJECT_CANVAS_PANELS_START",
            "    // HUD_UI_SRC:INJECT_CANVAS_PANELS_END",
        ],
    ),
    (
        VOICE_START,
        VISION_END,
        [
            "    // ── Voice + Vision — hud/ui-src/core/voice-runtime.js + vision-runtime.js ──",
            "    // HUD_UI_SRC:INJECT_VOICE_RUNTIME_START",
            "    // HUD_UI_SRC:INJECT_VOICE_RUNTIME_END",
            "    // HUD_UI_SRC:INJECT_VISION_RUNTIME_START",
            "    // HUD_UI_SRC:INJECT_VISION_RUNTIME_END",
        ],
    ),
]

for start, end, replacement in patches:
    replace_range(start, end, replacement)

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Patched {path} ({len(lines)} lines)")
