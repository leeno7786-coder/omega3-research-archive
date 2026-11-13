"""Per-episode trajectory-influence shadow store (operator 2026-06-30).

Each episode's mind-authored ``trajectory_influence`` {decision, domain, reason} (the richer
output schema) is shadow-recorded here as the episode closes. The 10-episode WINDOW reads the
collected influences and feeds them into its self-authoring prompt so the window aggregates the
mind's per-episode steers into one trajectory decision — WITHOUT any of it touching pressure
(2026-06-30 decouple). In-memory, bounded, failure-soft — a SHADOW store, never on the hot tick.

Mirrors the pressure_loop/omega_intent.py shadow-overlay pattern.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()
_INFLUENCES: List[Dict[str, Any]] = []
# Keep ~2 windows of history so a window read never starves; oldest evicted past the cap.
_CAP = 24


def record_influence(
    influence: Optional[Dict[str, Any]], *, episode_id: str = "", domain: str = ""
) -> None:
    """Append one episode's trajectory_influence. Ignores empty/invalid (validate-and-drop)."""
    if not isinstance(influence, dict):
        return
    decision = str(influence.get("decision") or "").strip()
    if not decision:
        return
    row = {
        "episode_id": str(episode_id or ""),
        "decision": decision,
        "domain": str(influence.get("domain") or domain or "").strip(),
        "reason": str(influence.get("reason") or "").strip()[:200],
    }
    with _LOCK:
        _INFLUENCES.append(row)
        if len(_INFLUENCES) > _CAP:
            del _INFLUENCES[: len(_INFLUENCES) - _CAP]


def recent_influences(cap: int = 12) -> List[Dict[str, Any]]:
    """The most-recent influences (newest last), bounded to ``cap``."""
    with _LOCK:
        rows = list(_INFLUENCES)
    return rows[-cap:] if cap and len(rows) > cap else rows


def clear() -> None:
    with _LOCK:
        _INFLUENCES.clear()
