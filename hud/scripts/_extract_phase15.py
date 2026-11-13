"""Extract HUD Phase 15 modules: omega-ui.js + panel-fx.js."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"
lines = (UI_SRC / "scripts.html").read_text(encoding="utf-8").splitlines()

# 1-based inclusive line numbers in scripts.html (pre-patch)
COMPOSER_START, COMPOSER_END = 4586, 4975
SKILL_PICKER_START, SKILL_PICKER_END = 4977, 5316
OMEGA_UI_START, OMEGA_UI_END = 5317, 5532
PANEL_FX_START, PANEL_FX_END = 5546, 5977


def extract_block(start: int, end: int, dedent: int = 0) -> str:
    chunk = lines[start - 1 : end]
    if dedent:
        chunk = [ln[dedent:] if ln.startswith(" " * dedent) else ln for ln in chunk]
    return "\n".join(chunk).rstrip() + "\n"


omega_ui = f"""// Composer actions, skill picker, OmegaUI helpers (hud/ui-src/core/omega-ui.js)
// Depends on: appendLine, _escapeHtml, appendImageMessage, appendDocumentMessage, window.Build, Voice, openPanel

{extract_block(COMPOSER_START, COMPOSER_END, dedent=0)}
{extract_block(SKILL_PICKER_START, SKILL_PICKER_END, dedent=0)}
{extract_block(OMEGA_UI_START, OMEGA_UI_END, dedent=0)}"""

panel_fx = f"""// Zed panel visual effects — particles, sparklines, badges (hud/ui-src/core/panel-fx.js)
// Depends on: window.openPanel, window.closePanel

{extract_block(PANEL_FX_START, PANEL_FX_END, dedent=4)}"""

(UI_SRC / "core" / "omega-ui.js").write_text(omega_ui, encoding="utf-8")
(UI_SRC / "core" / "panel-fx.js").write_text(panel_fx, encoding="utf-8")

print("omega-ui.js", len(omega_ui.splitlines()), "lines")
print("panel-fx.js", len(panel_fx.splitlines()), "lines")
