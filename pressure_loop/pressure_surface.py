"""Per-episode pressure surface — the foldback view of what's driving the mind right now.

INFORMATIONAL (minimal scaffolding, see feedback_minimal_scaffolding_omega_self_directs): we
show Omega its current pressure levels, the previous episode's, the deltas between them, and the
variables it can raise — and let it decide. No directives.

current = the episode's contributor weights; previous = the prior episode's (per-domain,
in-memory); deltas = current - previous. Failure-soft — never breaks packet assembly.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

# The pressure variables the mind sees + (for the *_debt_delta-backed ones) may raise. Excludes
# `trajectory_gap`: the self-authored trajectory now influences via the per-episode prompt, NOT
# via pressure (2026-06-30 decouple — operator). Omega drives all pressure per episode.
AVAILABLE_PRESSURE_VARIABLES = (
    "mastery_gap",
    "learning_debt",
    "application_gap",
    "execution_gap",
    "clarification_gap",
    "curiosity_gap",
    "tool_failure",
    "verification_needed",
    "confidence_reinforcement",
    "operator_priority",
)

_LOCK = threading.RLock()
_PREV: Dict[str, Dict[str, float]] = {}  # domain -> the prior episode's pressure dict


def _num(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def build_pressure_surface(
    current: Optional[Dict[str, float]], *, domain: str = ""
) -> Dict[str, Any]:
    """Assemble this episode's pressure surface from ``current`` ({contributor_type: weight}).

    Reads the prior episode's surface (per domain), computes deltas, then stores ``current`` as
    the new "previous". Failure-soft -> a degraded-but-stable shape on bad input."""
    cur = {str(k): round(float(v), 3) for k, v in (current or {}).items() if _num(v)}
    key = str(domain or "").strip().lower() or "_global"
    with _LOCK:
        prev = dict(_PREV.get(key) or {})
        _PREV[key] = dict(cur)
    deltas: Dict[str, float] = {}
    for k in set(cur) | set(prev):
        d = round(cur.get(k, 0.0) - prev.get(k, 0.0), 3)
        if d:
            deltas[k] = d
    return {
        "available_pressure_variables": list(AVAILABLE_PRESSURE_VARIABLES),
        "current_pressure": cur,
        "previous_pressure": prev,
        "pressure_deltas": deltas,
    }


def reset_for_tests() -> None:
    with _LOCK:
        _PREV.clear()
