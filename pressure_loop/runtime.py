"""Minimal runtime for pressure boot — shared state + episode store."""

from __future__ import annotations

import threading
from typing import Optional

from core.shared_state import SharedState


class PressureRuntime:
    """Owns shared state and the episode store for the pressure boot path."""

    def __init__(
        self,
        *,
        shared_state: SharedState | None = None,
        episode_store=None,
    ) -> None:
        self.shared_state = shared_state or SharedState()
        self._episode_store = episode_store

    @property
    def episode_store(self):
        if self._episode_store is None:
            from pressure_loop.episode import EpisodeStore

            self._episode_store = EpisodeStore()
        return self._episode_store


_runtime_lock = threading.RLock()
_default_runtime: Optional[PressureRuntime] = None


def set_default_runtime(rt: PressureRuntime) -> None:
    global _default_runtime
    with _runtime_lock:
        _default_runtime = rt


def try_get_default_runtime() -> Optional[PressureRuntime]:
    """Return the registered runtime, or None when boot has not registered one."""
    with _runtime_lock:
        return _default_runtime


def get_default_runtime() -> PressureRuntime:
    rt = try_get_default_runtime()
    if rt is None:
        raise RuntimeError(
            "No PressureRuntime registered; call pressure_loop.runtime.set_default_runtime"
        )
    return rt
