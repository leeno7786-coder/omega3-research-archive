"""Patch scripts.html for Phase 15 inject markers."""

from __future__ import annotations

from pathlib import Path

UI_SRC = Path(__file__).resolve().parents[2] / "hud" / "ui-src"
path = UI_SRC / "scripts.html"
lines = path.read_text(encoding="utf-8").splitlines()

OMEGA_UI_START, OMEGA_UI_END = 4586, 5532
PANEL_FX_START, PANEL_FX_END = 5541, 5977


def replace_range(start: int, end: int, replacement: list[str]) -> None:
    del lines[start - 1 : end]
    for i, ln in enumerate(replacement):
        lines.insert(start - 1 + i, ln)


replace_range(
    OMEGA_UI_START,
    OMEGA_UI_END,
    [
        "      // ── Composer + OmegaUI — hud/ui-src/core/omega-ui.js ──",
        "      // HUD_UI_SRC:INJECT_OMEGA_UI_START",
        "      // HUD_UI_SRC:INJECT_OMEGA_UI_END",
    ],
)

replace_range(
    PANEL_FX_START,
    PANEL_FX_END,
    [
        "    // ── Panel visual effects — hud/ui-src/core/panel-fx.js ──",
        "    // HUD_UI_SRC:INJECT_PANEL_FX_START",
        "    // HUD_UI_SRC:INJECT_PANEL_FX_END",
    ],
)

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Patched {path} ({len(lines)} lines)")
