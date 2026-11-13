"""Rebuild stage timers + realized-work counters (instrumentation only).

Task 1 of ``docs/superpowers/plans/2026-07-27-projection-speed.md``: make the
projection rebuild's wall clock legible before anything tries to speed it up.
Everything here is additive -- it reads no pipeline state and changes no
control flow or output, so it cannot move ``evidence_sha256``.

Two capabilities:

1. ``stage_sink`` / ``stage_timer`` -- an EXCLUSIVE-time nested stage stack.
   A nested ``stage_timer`` (for example ``rebuild_sram_from_graph`` running
   inside ``compile_memory_graph``) subtracts its own elapsed time from the
   enclosing stage, so summing every bucket in one sink reproduces the outer
   wall-clock time exactly -- no double counting. Re-entering the same stage
   name twice in one rebuild (``parity_report`` currently runs twice per
   ``MemoryService.rebuild`` call, pending Task 3) accumulates rather than
   overwriting. ``stage_timer`` is a near-zero-cost no-op unless a caller has
   opened a ``stage_sink`` first, so every other caller of the instrumented
   functions (the autonomous live-rebuild loop, ingest checkpoints, the CLI,
   the existing test suite) pays effectively nothing.
2. Realized-work counters -- read as a before/after delta around one rebuild
   by ``realized_work_delta``. The counters themselves are plain module-level
   integers incremented at the exact call site that does the work (never a
   receipt of a configured value like ``batch_size``), living beside that
   work in ``coordinate_registry.py``, ``dme/adapter.py``, and
   ``taxonomy/graph_store.py``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_local = threading.local()


def _frames() -> list[dict[str, float]]:
    frames = getattr(_local, "frames", None)
    if frames is None:
        frames = []
        _local.frames = frames
    return frames


@contextmanager
def stage_sink(sink: dict[str, float] | None) -> Iterator[None]:
    """Make ``sink`` the active stage-timing destination for this thread.

    Nestable: restores whatever sink (if any) was active before this call on
    exit, rather than clobbering it.
    """
    previous = getattr(_local, "sink", None)
    _local.sink = sink
    try:
        yield
    finally:
        _local.sink = previous


@contextmanager
def stage_timer(name: str) -> Iterator[None]:
    """Time one stage, crediting only its EXCLUSIVE (non-nested-child) time.

    A no-op (one attribute lookup) when no ``stage_sink`` is active.
    """
    sink = getattr(_local, "sink", None)
    if sink is None:
        yield
        return
    frames = _frames()
    frame = {"child": 0.0}
    frames.append(frame)
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        frames.pop()
        exclusive = elapsed - frame["child"]
        sink[name] = sink.get(name, 0.0) + exclusive
        if frames:
            frames[-1]["child"] += elapsed


def _snapshot_counters() -> dict[str, int]:
    """Read every realized-work counter's current cumulative value.

    Flat ``dict[str, int]`` to match the ``RebuildResult.realized_work``
    contract.
    """
    from memory.dme.adapter import canonical_open_count
    from memory.dme.coordinate_registry import registry_save_count
    from memory.taxonomy.graph_store import graph_neighbor_call_count

    snapshot: dict[str, int] = {
        "registry_saves": registry_save_count(),
        "canonical_opens": canonical_open_count(),
        "graph_neighbor_calls": graph_neighbor_call_count(),
    }
    return snapshot


@contextmanager
def realized_work_delta(sink: dict[str, int]) -> Iterator[None]:
    """Populate ``sink`` with realized-work deltas observed during the block.

    Each key is (counter value at exit) minus (counter value at entry), so
    concurrent/unrelated activity on the same process before this call does
    not inflate the count attributed to this rebuild.
    """
    before = _snapshot_counters()
    try:
        yield
    finally:
        after = _snapshot_counters()
        for key, value in after.items():
            sink[key] = value - before.get(key, 0)
