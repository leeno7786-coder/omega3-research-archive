"""DeepSleep projection store (Slice 6d) — single-state JSON, failure-soft.

Production cognition reads live stores directly (``load_mastery``, trajectory
snapshot). ``projection.json`` is a debug/audit artifact written each window;
no prompt-builder consumes ``read_projection`` on the live path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deepsleep_loop._deepsleep_state import ProjectionState
from deepsleep_loop.store_locks import PROJECTION, guarded

_LOG = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


@guarded(PROJECTION)
def read_projection(path: str | Path | None = None) -> ProjectionState:
    """Read the latest ProjectionState.

    Fail-hard: a MISSING file (first boot) is absence → empty ProjectionState. A
    parse/decode/schema failure on an EXISTING file is corrupt self-view → CRASH.
    """
    from pressure_loop.policy import deepsleep_projection_path

    resolved = _resolve_path(path or deepsleep_projection_path())
    if not resolved.is_file():
        return ProjectionState()

    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return ProjectionState()
    return ProjectionState.from_dict(raw)


@guarded(PROJECTION)
def write_projection(state: ProjectionState, path: str | Path | None = None) -> None:
    """Persist ProjectionState.

    Fail-hard: a projection write failure CRASHES — a stale/missing self-view
    next cycle is silent cognitive drift."""
    from pressure_loop.policy import deepsleep_projection_path

    resolved = _resolve_path(path or deepsleep_projection_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)
    from pressure_loop.durable_write import write_text_atomic

    write_text_atomic(
        resolved,
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
