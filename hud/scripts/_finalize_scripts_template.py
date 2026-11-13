"""Strip stale baked bundles from scripts.html — keep inject markers only."""

from __future__ import annotations

import re
from pathlib import Path

UI_SRC = Path(__file__).resolve().parents[2] / "hud" / "ui-src"
path = UI_SRC / "scripts.html"
text = path.read_text(encoding="utf-8")

MARKER_PAIRS = (
    ("// HUD_UI_SRC:INJECT_CANVAS_START", "// HUD_UI_SRC:INJECT_CANVAS_END"),
    ("// HUD_UI_SRC:INJECT_PANELS_START", "// HUD_UI_SRC:INJECT_PANELS_END"),
    ("// HUD_UI_SRC:INJECT_CORE_START", "// HUD_UI_SRC:INJECT_CORE_END"),
)

for start, end in MARKER_PAIRS:
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(f"Missing marker pair: {start!r} … {end!r}")
    text = pattern.sub(f"{start}\n    {end}", text, count=1)

# Orphan IIFE close left from partial phase-10 splice (FaceWatch inject is top-level).
text = text.replace(
    "      // HUD_UI_SRC:INJECT_VISION_FACEWATCH_END\n})();",
    "      // HUD_UI_SRC:INJECT_VISION_FACEWATCH_END",
)

path.write_text(text, encoding="utf-8")
lines = text.count("\n") + (0 if text.endswith("\n") else 1)
print(f"Finalized {path} ({lines} lines)")
