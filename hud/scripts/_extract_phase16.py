"""One-shot extractor for HUD Phase 16 — MemoryGraph visualization."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"
lines = (UI_SRC / "scripts.html").read_text(encoding="utf-8").splitlines()

MG_START, MG_END = 4601, 7466


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


body = extract_script_block(MG_START, MG_END)
memory_graph = f"""// Memory graph visualization + settings integration (hud/ui-src/core/memory-graph.js)
// Depends on: Telemetry (settings live feeds), fetch /api/memory/lobes
// Exposes: global.MemoryGraph, window._memoryGraphInit, window._memoryGraphDestroy

{body}"""
(UI_SRC / "core" / "memory-graph.js").write_text(memory_graph, encoding="utf-8")
print("memory-graph.js", len(memory_graph.splitlines()), "lines")
