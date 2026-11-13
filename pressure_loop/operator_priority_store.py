"""Operator-directed learning priorities — domains the operator has flagged as important.

Read by ``pressure_loop.pressure_field.read_operator_priority`` -> the ``operator_priority``
pressure contributor (which routes to ASK_OPERATOR via global_loop intent mapping). Empty by
default; an operator affordance (future HUD control) sets priorities. In-memory, failure-soft —
a SUPPORT source, it never crashes the pressure tick.
"""

from __future__ import annotations

import threading
from typing import List

_LOCK = threading.RLock()
_PRIORITIES: List[str] = []


def set_priority(domain: str) -> None:
    """Flag a domain as an operator priority (dedup, case-insensitive)."""
    d = str(domain or "").strip()
    if not d:
        return
    with _LOCK:
        if d.lower() not in [p.lower() for p in _PRIORITIES]:
            _PRIORITIES.append(d)


def list_priorities() -> List[str]:
    with _LOCK:
        return list(_PRIORITIES)


def clear() -> None:
    with _LOCK:
        _PRIORITIES.clear()
