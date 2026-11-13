"""Symphony temporal authority — Phase 2 §P2-S20.

Single provider for product cognitive timestamps. Every cognitive
surface that needs to ask "what time is it now?" (CueDesk, EpisodeStore,
symphony_learning_state, symphony_global_action, symphony_runner,
episode_wal, response-hold receipts, Omega response intent, DeepSleep
archive snapshot) must call through this module.

Direct `datetime.now()`, `datetime.utcnow()`, `time.time()`, and
`time.monotonic()` calls on product cognitive paths are forbidden —
either migrate to this authority or document in the allowlist at
`docs/architecture/temporal_allowlist.md` with a named reason:

  - local timeout / duration measurement
  - third-party adapter boundary
  - test fixture
  - legacy compatibility pending removal

The authority exposes three read primitives:

  - `now_iso()`       → UTC ISO string (replaces `datetime.now(tz=UTC).isoformat()`)
  - `now_wall()`      → float unix seconds (replaces `time.time()`)
  - `monotonic()`     → float monotonic seconds (replaces `time.monotonic()`)

And three test seams that injected-fake-time tests use to pin every
surface to the same temporal reference frame:

  - `set_fake_time(*, iso=..., wall=..., monotonic=...)` — install fakes
  - `clear_fake_time()`                                   — return to real-time
  - `is_faked()`                                          — bool

When ANY of the three fake values is None at set-time, that read
primitive falls back to the real clock. So tests can pin iso+wall
together (the common case for episode/pack/archive/WAL convergence)
while letting `monotonic()` run real — or vice versa.

The authority is intentionally minimal:
  - No SharedState writes.
  - No durable persistence.
  - No background thread / poller.
  - The fake-time state is in-process module globals; tests must
    use `clear_fake_time()` on teardown (the
    `_fake_time_cleared` autouse fixture in audits does this).

Phase 2 §P2-S20 cure invariant: under
`set_fake_time(iso='X', wall=W)`, every product cognitive surface
that touches a timestamp this tick must record the SAME `X` /
`W` — no late-binding drift between episode open / pack / archive /
WAL captures.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

__all__ = [
    "now_iso",
    "now_wall",
    "monotonic",
    "set_fake_time",
    "clear_fake_time",
    "is_faked",
    "TemporalCategory",
    "TEMPORAL_ALLOWLIST",
    "classify_temporal_file",
]


# =====================================================================
# Phase 2 §P2-S20 RB#3 — code-readable allowlist for legitimate direct
# datetime.now / time.time / time.monotonic calls under the five
# cognitive packages (core, brain, autonomous, metacognition, memory).
#
# Every file under those packages that still contains a real
# Call-node-direct-time-call MUST appear here with a category. The
# audit at tests/regression/test_symphony_p2_s20_temporal_authority.py
# AST-walks the packages, collects every direct call, and asserts each
# call's file path is either:
#
#   (a) in this allowlist with a category, OR
#   (b) inside a function whose body also calls now_iso() / now_wall()
#       / monotonic() — i.e., the file MIGRATED to the authority.
#
# Both shapes are accepted because some files migrate ONE helper and
# leave other call sites under a different (allowlisted) category;
# the audit picks the more specific signal per (file, lineno).
#
# Categories mirror PACKET_P2_S20 RB#3 verbatim:
#   - LOCAL_TIMEOUT           — bounded local elapsed measurement
#   - THIRD_PARTY_ADAPTER     — wraps non-Symphony library / HUD / log
#   - LEGACY_COMPAT           — pending Kimi removal packet
#   - AUTHORITY               — core.symphony_time itself (the source)
#
# Tests (`tests/`) are NOT scanned, so they don't need entries.
# =====================================================================


class TemporalCategory(str, Enum):
    LOCAL_TIMEOUT = "local_timeout"
    THIRD_PARTY_ADAPTER = "third_party_adapter"
    LEGACY_COMPAT = "legacy_compat"
    AUTHORITY = "authority"


# File-path keys are repo-relative POSIX paths (forward slashes). The
# audit normalises Windows paths before lookup. Values are the
# TemporalCategory enum member.
TEMPORAL_ALLOWLIST: dict[str, "TemporalCategory"] = {
    # ---- The authority itself ----
    "core/symphony_time.py": TemporalCategory.AUTHORITY,
    # ---- Local timeout / duration measurement ----
    # Cooldown / latency / retry / scheduling deltas. These read time
    # for bounded local elapsed math; they do not write a timestamp
    # into a cognitive payload that another surface compares against.
    # (Retired brain/autonomous/metacognition paths removed 2026-06-05.)
    "boot.py": TemporalCategory.LOCAL_TIMEOUT,
    "cognition_chain.py": TemporalCategory.LOCAL_TIMEOUT,
    "pressure_loop/loop.py": TemporalCategory.LOCAL_TIMEOUT,
    "pressure_loop/pressure.py": TemporalCategory.LOCAL_TIMEOUT,
    "pressure_loop/pressure_field.py": TemporalCategory.LOCAL_TIMEOUT,
    "pressure_loop/episode.py": TemporalCategory.LOCAL_TIMEOUT,
    "pressure_loop/runtime.py": TemporalCategory.LOCAL_TIMEOUT,
    "global_loop/loop.py": TemporalCategory.LOCAL_TIMEOUT,
    "meta_loop/loop.py": TemporalCategory.LOCAL_TIMEOUT,
    "auto_loop/loop.py": TemporalCategory.LOCAL_TIMEOUT,
    "omega_loop/loop.py": TemporalCategory.LOCAL_TIMEOUT,
    "deepsleep_loop/loop.py": TemporalCategory.LOCAL_TIMEOUT,
    "pressure_loop/symphony_probes.py": TemporalCategory.LOCAL_TIMEOUT,
    "sensory/operator_presence.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/api.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/coding_project.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/consolidation.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/core.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/encoding.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/engine/validate_write.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/enrichment/journal.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/knowledge_router.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/promotion.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/queues.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/self_vector.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/strength_index.py": TemporalCategory.LOCAL_TIMEOUT,
    "schema/omega_trajectory.py": TemporalCategory.LOCAL_TIMEOUT,
    "memory/encoding_journal.py": TemporalCategory.LOCAL_TIMEOUT,
    # ---- Third-party adapter / log stream boundaries ----
    "runtime/client.py": TemporalCategory.THIRD_PARTY_ADAPTER,
    "guardian/data_logger.py": TemporalCategory.THIRD_PARTY_ADAPTER,
    # ---- Legacy compatibility pending removal ----
    "pressure_loop/cycle_cadence.py": TemporalCategory.LEGACY_COMPAT,
}


def classify_temporal_file(repo_relative_path: str) -> Optional["TemporalCategory"]:
    """Return the allowlist category for ``repo_relative_path`` or
    ``None`` if the file is not on the allowlist.

    Path is normalised: backslashes → forward slashes; leading "./"
    stripped. Lookup is case-sensitive.
    """
    norm = (repo_relative_path or "").replace("\\", "/").lstrip("./")
    return TEMPORAL_ALLOWLIST.get(norm)


# In-process fake-time state. None means "use real clock".
_FAKE_ISO: Optional[str] = None
_FAKE_WALL: Optional[float] = None
_FAKE_MONOTONIC: Optional[float] = None


def now_iso() -> str:
    """Return the canonical UTC ISO timestamp for cognitive surfaces.

    Replaces every per-module `_now_iso()` helper that previously
    called `datetime.now(timezone.utc).isoformat()` directly.

    Under `set_fake_time(iso=...)` returns the fake iso verbatim, so
    fake-time tests can pin episode/pack/archive/WAL timestamps to a
    single reference frame.
    """
    if _FAKE_ISO is not None:
        return _FAKE_ISO
    return datetime.now(timezone.utc).isoformat()


def now_wall() -> float:
    """Return wall-clock seconds since the unix epoch.

    Replaces direct `time.time()` calls on product cognitive paths.
    Under `set_fake_time(wall=...)` returns the fake float verbatim.
    """
    if _FAKE_WALL is not None:
        return _FAKE_WALL
    return _time.time()


def monotonic() -> float:
    """Return monotonic seconds for product cognitive duration math.

    Replaces direct `time.monotonic()` calls on product cognitive
    paths. Under `set_fake_time(monotonic=...)` returns the fake
    float verbatim. NOTE: most cognitive surfaces want `now_wall()`
    for receipt timestamps; `monotonic()` is for tick-duration math
    where wall-clock NTP jumps would corrupt the measurement.
    """
    if _FAKE_MONOTONIC is not None:
        return _FAKE_MONOTONIC
    return _time.monotonic()


def set_fake_time(
    *,
    iso: Optional[str] = None,
    wall: Optional[float] = None,
    monotonic: Optional[float] = None,
) -> None:
    """Install in-process fake clock values.

    Any of the three primitives can be faked independently — passing
    `iso='X'` alone pins `now_iso()` while leaving `now_wall()` and
    `monotonic()` on the real clock.

    Tests must call `clear_fake_time()` on teardown so the fake state
    doesn't leak to sibling tests.
    """
    global _FAKE_ISO, _FAKE_WALL, _FAKE_MONOTONIC
    _FAKE_ISO = str(iso) if iso is not None else _FAKE_ISO
    _FAKE_WALL = float(wall) if wall is not None else _FAKE_WALL
    _FAKE_MONOTONIC = float(monotonic) if monotonic is not None else _FAKE_MONOTONIC


def clear_fake_time() -> None:
    """Drop all in-process fakes. All three primitives return to the
    real clock. Tests MUST call this on teardown.
    """
    global _FAKE_ISO, _FAKE_WALL, _FAKE_MONOTONIC
    _FAKE_ISO = None
    _FAKE_WALL = None
    _FAKE_MONOTONIC = None


def is_faked() -> bool:
    """True iff at least one primitive is currently faked."""
    return _FAKE_ISO is not None or _FAKE_WALL is not None or _FAKE_MONOTONIC is not None
