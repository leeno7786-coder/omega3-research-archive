"""
Failure Record Schema
=====================

A failure that the system observed and is keeping as data. Required
by the Failure-Is-Signal directive — failures are first-class
persistent objects, not silent errors.

This is distinct from CycleRecord: a CycleRecord may have
cycle_kind != "success" (which is itself a failure observation), but
some failures aren't cycle-bound — tool errors mid-recall, contradiction
events during memory consolidation, guardian denials, etc.

The FailureRecord dataclass itself was removed 2026-07-14 (never
instantiated — production carries failure observations as plain dicts with a
`failure_fingerprint` string field; see memory/pathway_performance.py,
pressure_loop/episode_journal.py, schema/pathway_outcome.py). This module's
live exports are `CAUSE_RESOLUTION_STATES` (consumed by
core/shared_state_keys.py) and `failure_fingerprint()`. There is NO
centralized `failure_policy` module — it was never built and
`config/failure.yaml` was purged as verified-dead (`4266f76`). Do NOT
fabricate one (the resurrect-retired-architecture trap). The failure
RESPONSE is DISTRIBUTED: each consumer applies its own (severity is a
deterministic kind-floor set at record time in
`core.failure._KIND_SEVERITY`, overridable via explicit `severity=`).

Used by (each owns its own response to the observation):
    - global_loop           (reflects on failure trends)
    - deep_sleep            (consolidates failure patterns)
    - guardian              (cites failures when intervening)
    - autonomous loop Phase 4 (writes failure records alongside cycle
                                records; queue-retry on non-success)
    - brain verify          (escalates the cycle on flagged/failed)

ARCHITECTURAL RULES:
    - Every failure that leaves a phase produces a failure observation.
    - kind discriminates the failure's type and selects the
      deterministic severity floor (`core.failure._KIND_SEVERITY`);
      the response itself is the consuming subsystem's, not a policy
      module's.
    - severity is in [0, 1]; escalation thresholds are applied by
      the failure-handling code (the former config/failure.yaml was
      deleted as verified-dead — no live loader).
    - This schema MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

import hashlib
from typing import Literal

import logging

log = logging.getLogger("omega.schema.failure_record")


FailureKind = Literal[
    "meta_silence",  # no orders from meta layer
    "inference_error",  # model call errored
    "tool_error",  # one or more tool calls failed
    "schema_violation",  # data didn't match required schema
    "policy_block",  # policy refused an operation
    "guardian_deny",  # guardian denied a proposal
    "verification_fail",  # verification check failed
    "memory_contradiction",  # detected during memory consolidation
    "drift_flag",  # drift detector raised a concern
    "timeout",  # operation exceeded its budget
    "resource_exhaustion",  # OOM, rate limit, etc.
    "unknown",  # last-resort catch-all (logged + audited)
]


# Causal resolution state — FINAL VERDICT hardening #1.
# Every failure record carries how well its CAUSE is understood, for
# causal precision + correct curiosity escalation. THIS is the canonical
# definition; per the schema rule above it MUST NOT be redefined
# elsewhere — other modules (e.g. shared_state_keys) import this set.
CauseResolutionState = Literal[
    "resolved",  # cause identified AND verified
    "hypothesis",  # plausible cause, not yet verified
    "unknown",  # cause not yet investigated (honest default)
    "contradictory",  # evidence conflicts — cause cannot be asserted
]

CAUSE_RESOLUTION_STATES: frozenset[str] = frozenset(
    ("resolved", "hypothesis", "unknown", "contradictory")
)


def failure_fingerprint(
    failure_class: str,
    cause: str,
    subsystem_origin: str,
    blocked_capability: str,
) -> str:
    """Canonical repeat-failure identity — FINAL VERDICT hardening #2.

    Deterministic across processes and reboots: SAME failure -> SAME
    fingerprint forever (the Omega Mantra — deterministic home). The
    LAW writes `hash(...)` conceptually; Python's builtin `hash()` on
    strings is salted per-process (PYTHONHASHSEED), which would make
    repeat_count non-deterministic across boots. We use SHA-256.

    `cause` is normalized (case-folded, whitespace-collapsed) so
    trivially-different cause strings collapse to ONE repeat identity —
    without this, repeat_count silently stays 1 and every recurrence
    looks novel.
    """

    def _norm(s: str) -> str:
        return " ".join(str(s).split()).casefold()

    # \x1f (unit separator) makes component boundaries unambiguous so
    # ("ab","c",...) cannot collide with ("a","bc",...).
    canonical = "\x1f".join(
        (
            _norm(failure_class),
            _norm(cause),
            _norm(subsystem_origin),
            _norm(blocked_capability),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
