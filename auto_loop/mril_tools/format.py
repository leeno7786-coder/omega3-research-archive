"""Format verified MRIL tool results for foldback and diagnostics.

Response-model grounding is owned by ``auto_loop.scaffold``. This module keeps
only the neutral result serializers still used by AUTO foldback and public
compatibility exports; it must not grow a second answer-prompt implementation.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def _titled(item: Mapping[str, Any]) -> str:
    """Render an evidence item with its distinct attribution title."""
    title = str(item.get("title") or "").strip()
    body = str(item.get("remembered_as") or "").strip()
    if title and body and title.lower() not in body.lower():
        return f"{title}: {body}"
    return body or title


def format_remembered_knowledge_block(results: Iterable[Mapping[str, Any]]) -> str:
    """Render forced remember_* outputs for execution foldback."""
    lines = ["Remembered Knowledge:"]
    for result in results:
        tool = result.get("tool", "remember")
        memory_type = result.get("memory_type", "none")
        if memory_type == "none" or not result.get("evidence"):
            lines.append(f"- [{tool}] (no memories)")
            continue
        for item in result.get("evidence") or []:
            rendered = _titled(item)
            if rendered:
                lines.append(f"- [{tool}] {rendered}")
    return "\n".join(lines)
