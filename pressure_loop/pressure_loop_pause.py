"""Process-local pressure-loop pause ownership.

The live boot runs the pressure loop and HUD in the same Python process. HUD
routes use this module to pause pressure ticks while operator-controlled agents
own the shared local model runtime.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.RLock()
_OWNERS: dict[str, dict[str, Any]] = {}


def _clean_owner(owner: str) -> str:
    value = str(owner or "").strip()
    if not value:
        raise ValueError("pause owner required")
    return value


def pause_pressure_loop(owner: str, *, reason: str = "") -> dict[str, Any]:
    """Hold the pressure loop pause for ``owner`` and return current state."""

    key = _clean_owner(owner)
    with _LOCK:
        _OWNERS[key] = {
            "reason": str(reason or "").strip(),
            "since": time.time(),
        }
        state = pressure_loop_pause_state()
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "pressure_loop_pause",
            status="ok",
            summary=f"paused owner={key}",
            paused=True,
            owners=state.get("owners"),
            reason=str(reason or "").strip(),
        )
    except Exception:
        pass
    return state


def resume_pressure_loop(owner: str, *, reason: str = "") -> dict[str, Any]:
    """Release ``owner``'s pause hold and return current state."""

    key = _clean_owner(owner)
    with _LOCK:
        _OWNERS.pop(key, None)
        state = pressure_loop_pause_state(extra_reason=reason)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "pressure_loop_pause",
            status="ok",
            summary=f"resumed owner={key}",
            paused=bool(state.get("paused")),
            owners=state.get("owners"),
            last_release_reason=str(reason or "").strip(),
        )
    except Exception:
        pass
    return state


def is_pressure_loop_paused() -> bool:
    """True when any owner currently holds the pressure-loop pause."""

    with _LOCK:
        return bool(_OWNERS)


def pressure_loop_pause_state(*, extra_reason: str = "") -> dict[str, Any]:
    """Return a JSON-safe snapshot consumed by HUD telemetry and routes."""

    with _LOCK:
        owners = sorted(_OWNERS)
        reasons = {owner: meta.get("reason", "") for owner, meta in sorted(_OWNERS.items())}
        since = {owner: meta.get("since") for owner, meta in sorted(_OWNERS.items())}
    state: dict[str, Any] = {
        "ok": True,
        "paused": bool(owners),
        "owners": owners,
        "reasons": reasons,
        "since": since,
    }
    if extra_reason:
        state["last_release_reason"] = str(extra_reason)
    return state


def _reset_pressure_loop_pause_for_tests() -> None:
    """Test-only reset for process-local pause owners."""

    with _LOCK:
        _OWNERS.clear()
