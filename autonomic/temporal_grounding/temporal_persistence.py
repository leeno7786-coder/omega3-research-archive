"""Lifetime clock persistence — temporal_state.json across reboots (operator 2026-07-16).

Today's TemporalGrounding is session-only: every boot resets epoch/cycle/tick to 0, so "how
long has Omega existed" and "how long was it offline" are unanswerable across restarts, and a
deadline stated one session can't survive a reboot. This module persists a lifetime clock:

    first_boot_at, last_boot_at, last_shutdown_at, boot_count,
    lifetime_epochs, lifetime_cycles, lifetime_seconds, last_was_clean_shutdown

Persistence > performance is the founding doctrine — a clock that forgets time on every boot
is not a real substrate. The store lives under guardian/state (gitignored, cleared by
day-zero), not the retired .omega_data tree.

Accumulation uses a BOOT-SNAPSHOT base: record_boot snapshots the prior lifetime totals into
``_base_*``; checkpoint/record_shutdown set ``lifetime_* = _base_* + this_session_totals``. So
repeated checkpoints are idempotent, and a crash loses only the partial since the last
checkpoint (exactly the durability the design wants). Pure + deterministic: ``now`` and the
path are injected.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("omega.autonomic.temporal_grounding.temporal_persistence")

_DEFAULT: Dict[str, Any] = {
    "first_boot_at": None,
    "last_boot_at": None,
    "last_shutdown_at": None,
    "boot_count": 0,
    "lifetime_epochs": 0,
    "lifetime_cycles": 0,
    "lifetime_seconds": 0.0,
    # Base snapshot captured at boot; this session's totals add on top (idempotent checkpoints).
    "_base_epochs": 0,
    "_base_cycles": 0,
    "_base_seconds": 0.0,
    "last_was_clean_shutdown": True,
}


def _state_path() -> Path:
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "temporal_state.json"


def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the persisted clock, merged over defaults. Fail-soft to a fresh clock on
    missing/corrupt file (logged loudly — a lost lifetime clock is a real loss)."""
    p = Path(path) if path is not None else _state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**_DEFAULT, **data}
        log.warning("temporal_state.json is not an object — re-initializing the lifetime clock")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("temporal_state.json unreadable (%s) — re-initializing the lifetime clock", exc)
    return dict(_DEFAULT)


def _write(state: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic swap


def record_boot(now: float, path: Optional[Path] = None) -> Dict[str, Any]:
    """Stamp this boot. Returns the new state PLUS derived ``offline_seconds`` (how long the
    system was down since the last CLEAN shutdown, else None) and ``prior_clean_shutdown``
    (False when the prior session died unclean). Marks the clock unclean until a clean
    shutdown is recorded."""
    p = Path(path) if path is not None else _state_path()
    prior = load_state(p)
    had_prior_boot = int(prior.get("boot_count") or 0) > 0
    prior_clean = bool(prior.get("last_was_clean_shutdown", True))
    prior_shutdown = prior.get("last_shutdown_at")

    if had_prior_boot and prior_clean and isinstance(prior_shutdown, (int, float)):
        offline_seconds: Optional[float] = max(0.0, float(now) - float(prior_shutdown))
    else:
        offline_seconds = None  # unknown when the prior shutdown was unclean / absent

    state = dict(prior)
    if state.get("first_boot_at") is None:
        state["first_boot_at"] = now
    state["last_boot_at"] = now
    state["boot_count"] = int(prior.get("boot_count") or 0) + 1
    state["_base_epochs"] = int(prior.get("lifetime_epochs") or 0)
    state["_base_cycles"] = int(prior.get("lifetime_cycles") or 0)
    state["_base_seconds"] = float(prior.get("lifetime_seconds") or 0.0)
    state["last_was_clean_shutdown"] = False
    _write(state, p)
    return {
        **state,
        "offline_seconds": offline_seconds,
        # None on a first boot: there is no prior session to call clean or unclean.
        "prior_clean_shutdown": prior_clean if had_prior_boot else None,
    }


def checkpoint(
    session_epochs: int = 0,
    session_cycles: int = 0,
    session_seconds: float = 0.0,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fold this session's running totals into the lifetime counters (base + totals). Leaves
    the clock UNCLEAN — only record_shutdown marks a clean stop."""
    p = Path(path) if path is not None else _state_path()
    state = load_state(p)
    state["lifetime_epochs"] = int(state.get("_base_epochs") or 0) + int(session_epochs or 0)
    state["lifetime_cycles"] = int(state.get("_base_cycles") or 0) + int(session_cycles or 0)
    state["lifetime_seconds"] = float(state.get("_base_seconds") or 0.0) + float(
        session_seconds or 0.0
    )
    _write(state, p)
    return state


def record_shutdown(
    now: float,
    session_epochs: int = 0,
    session_cycles: int = 0,
    session_seconds: float = 0.0,
    path: Optional[Path] = None,
    clean: bool = True,
) -> Dict[str, Any]:
    """Fold the session totals and stamp last_shutdown_at.

    ``clean`` says whether this was an intended stop. A crash that still walks the graceful
    teardown is NOT clean (operator 2026-09-15: the clock counted a crash as a clean shutdown
    because teardown ran); a session that was killed never gets here and stays unclean from
    ``record_boot``. Offline time is only measured from a clean stop.
    """
    p = Path(path) if path is not None else _state_path()
    state = checkpoint(session_epochs, session_cycles, session_seconds, path=p)
    state["last_shutdown_at"] = now
    state["last_was_clean_shutdown"] = bool(clean)
    _write(state, p)
    return state
