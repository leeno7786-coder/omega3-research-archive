"""Retired process-local GPU gate retained as inert model-control bookkeeping.

The current cognition topology has one configured model/runtime and four
physical pipelines permanently bound to Chat, Browser, Agent, and Window.
Semantic lane identity selects the worker; descriptive work kind does not.
Consequently this module must not serialize cognition:
``_enabled`` is always false, ``wait_for_gpu`` returns immediately, and the old
acquire/release call sites only update process-local diagnostic counters.

This module is not a scheduler, queue, residency policy, or canonical memory
surface. The durable cognition queue and physical pipeline dispatcher own
admission; the selected mind and local-services process own model serving.
"""

from __future__ import annotations

import threading
from typing import Optional

_lock = threading.RLock()
_count = 0
_holder: Optional[str] = None
# Set when the GPU is free (count == 0); cleared while a build holds it. Waiters block on it.
_free_event = threading.Event()
_free_event.set()

_DISABLED_VALUES = frozenset({"0", "false", "no", "off", ""})


def _enabled() -> bool:
    """Four bound lanes share the resident brain; no build hotswap gate."""
    return False


def acquire_gpu_for_build(holder: str = "build") -> None:
    """Record an inert historical acquire for diagnostics; never gate cognition."""
    global _count, _holder
    with _lock:
        _count += 1
        _holder = str(holder or "build")
        _free_event.clear()
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "runtime",
            "gpu_lease_acquire",
            summary=f"holder={_holder} count={_count}",
            holder=_holder,
            count=_count,
        )
    except Exception:
        pass


def release_gpu_for_build() -> None:
    """Balance diagnostic bookkeeping; releasing with no holder is a no-op."""
    global _count, _holder
    with _lock:
        if _count > 0:
            _count -= 1
        if _count <= 0:
            _count = 0
            _holder = None
            _free_event.set()
        holder = _holder
        count = _count
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "runtime",
            "gpu_lease_release",
            summary=f"holder={holder} count={count}",
            holder=holder,
            count=count,
        )
    except Exception:
        pass


def gpu_held_by_build() -> bool:
    """Always false: execution bookkeeping never blocks shared cognition."""
    if not _enabled():
        return False
    with _lock:
        return _count > 0


def current_holder() -> Optional[str]:
    with _lock:
        return _holder


def wait_for_gpu(timeout: float) -> bool:
    """Return immediately; lane FIFO and runtime capacity own admission waits."""
    if not _enabled():
        return True
    if not gpu_held_by_build():
        return True
    acquired = _free_event.wait(timeout=max(0.0, float(timeout)))
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "runtime",
            "gpu_lease_wait",
            status="ok" if acquired else "skip",
            summary=f"acquired={acquired} timeout={timeout}",
            acquired=acquired,
            timeout=float(timeout),
            current_holder=current_holder(),
        )
    except Exception:
        pass
    return acquired


def _reset_for_tests() -> None:
    global _count, _holder
    with _lock:
        _count = 0
        _holder = None
        _free_event.set()
