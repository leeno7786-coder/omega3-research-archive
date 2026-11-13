"""Patch scripts.html for Phase 16 MemoryGraph inject markers."""

from __future__ import annotations

from pathlib import Path

UI_SRC = Path(__file__).resolve().parents[2] / "hud" / "ui-src"
path = UI_SRC / "scripts.html"
lines = path.read_text(encoding="utf-8").splitlines()

MG_START, MG_END = 4601, 7466


def replace_range(start: int, end: int, replacement: list[str]) -> None:
    del lines[start - 1 : end]
    for i, ln in enumerate(replacement):
        lines.insert(start - 1 + i, ln)


replace_range(
    MG_START,
    MG_END,
    [
        "    // ── Memory graph visualization — hud/ui-src/core/memory-graph.js ──",
        "    // HUD_UI_SRC:INJECT_MEMORY_GRAPH_START",
        "    // HUD_UI_SRC:INJECT_MEMORY_GRAPH_END",
    ],
)

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Patched {path} ({len(lines)} lines)")
