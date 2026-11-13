"""The teardown halt flag — "the workers you are waiting for are being killed".

RISK 4(b), measured on this appliance 2026-08-11. A graceful stop took the
llama workers down while the cognition threads were still using them: SIGTERM
at 14:14:22, the gateway gone by 14:14:30, and the agent lane's cascade still
running at 14:15:42 — 72 seconds of deepsleep, memory commit and embed backfill
issuing loopback requests at processes that no longer existed. systemd saw a
1m20s gap between "shutdown complete" and "Stopped"; the journal filled with
URLError.

Two things were wrong. The FIRST is ordering, and its fix lives in
``core.boot_lifecycle``: the cognition threads are stopped BEFORE the workers
they depend on are swept.

The SECOND is that stopping them cannot be instant, and must not be. A cascade
caught mid-flight is bounded but not immediate — it finishes its deepsleep
terminus and its memory commit, which is exactly what it should do — and every
embed request it makes in that window meets the local-resident retry envelope:
14 attempts backing off to a 3s ceiling, ~33.5s per request
(the retired dense embedding retry envelope). That envelope is RIGHT while the
appliance is alive — it exists to outlive a worker respawn, and shortening it is
what crash-looped the brain before commit 649e8cde — and exactly wrong once
teardown has begun, because nothing is respawning. This flag is how a retry loop
tells those two situations apart.

WHAT IT IS NOT. Not a kill switch, not a cancellation, and never a reason to
skip durable work: nothing consults it to abandon a write, drop a commit or
shorten a journal append. It shortens WAITS on a runtime that is being torn
down, and nothing else. Fail-hard is untouched — genuine cognition corruption
still crashes the brain. What changes is only that the mind no longer waits 33
seconds at a time for a worker the shutdown already killed.

Process-global and one-way within a boot: ``request_halt`` is called once from
teardown, and only ``reset_for_tests`` (and ``reset_lifecycle_state``, which
run_continuous calls before every live boot) clears it. Stdlib only, no project
imports — a retry loop in the memory layer must be able to ask this question
without dragging the boot lifecycle in behind it.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_halted = False
_reason = ""


def request_halt(reason: str = "shutdown") -> None:
    """Teardown has begun: the runtime's workers are going away.

    Idempotent — the FIRST reason wins, so a later step's generic "shutdown"
    can never overwrite the specific one that started the teardown.
    """
    global _halted, _reason
    with _lock:
        if not _halted:
            _halted = True
            _reason = str(reason or "shutdown")


def halt_requested() -> bool:
    """Is the runtime being torn down? Cheap enough for a retry loop."""
    with _lock:
        return _halted


def halt_reason() -> str:
    with _lock:
        return _reason


def reset_for_tests() -> None:
    """Clear the flag. Also the live path: ``run_continuous`` resets the whole
    lifecycle before boot, so a re-boot in one process starts unhalted."""
    global _halted, _reason
    with _lock:
        _halted = False
        _reason = ""
