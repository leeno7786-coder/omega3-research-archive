"""Fix scripts.html corruption from phase 15 patch + inject panel-fx markers."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"
MONOLITH = ROOT / "hud" / "omega_interface.html"

scripts_lines = (UI_SRC / "scripts.html").read_text(encoding="utf-8").splitlines()
omega_lines = MONOLITH.read_text(encoding="utf-8").splitlines()

# Good reference from last built monolith (1-based line numbers)
MG_FIX_PART1 = (20736, 21084)  # spring forces … BloomAnimator.clear
MG_FIX_PART2 = (21116, 21191)  # LinkParticles … spawn end

PANEL_FX_START, PANEL_FX_END = 4597, 5033

CORRUPT_START = 5541  # inject markers wrongly inserted mid ForceLayout.step
CORRUPT_END = 5563  # through broken spawn fragment before tick


def replace_range(lines: list[str], start: int, end: int, replacement: list[str]) -> None:
    del lines[start - 1 : end]
    for i, ln in enumerate(replacement):
        lines.insert(start - 1 + i, ln)


# 1. Replace inline panel-fx block with inject markers
replace_range(
    scripts_lines,
    PANEL_FX_START,
    PANEL_FX_END,
    [
        "    // ── Panel visual effects — hud/ui-src/core/panel-fx.js ──",
        "    // HUD_UI_SRC:INJECT_PANEL_FX_START",
        "    // HUD_UI_SRC:INJECT_PANEL_FX_END",
    ],
)

# Line numbers shift after first replacement
delta = 3 - (PANEL_FX_END - PANEL_FX_START + 1)
corrupt_start = CORRUPT_START + delta
corrupt_end = CORRUPT_END + delta

fix_body = (
    omega_lines[MG_FIX_PART1[0] - 1 : MG_FIX_PART1[1]]
    + omega_lines[MG_FIX_PART2[0] - 1 : MG_FIX_PART2[1]]
)
replace_range(scripts_lines, corrupt_start, corrupt_end, fix_body)

# 3. Ensure ForceLayout + BloomAnimator are exported on __MG (single-IIFE layout)
for i, ln in enumerate(scripts_lines):
    if ln.strip() == "Object.assign(global.__MG, {" and i + 3 < len(scripts_lines):
        block = scripts_lines[i : i + 6]
        if any("LinkParticles" in x for x in block) and not any("ForceLayout" in x for x in block):
            scripts_lines.insert(i + 1, "    ForceLayout,")
            scripts_lines.insert(i + 2, "    BloomAnimator,")
            break

(UI_SRC / "scripts.html").write_text("\n".join(scripts_lines) + "\n", encoding="utf-8")
print(f"Fixed scripts.html ({len(scripts_lines)} lines)")
