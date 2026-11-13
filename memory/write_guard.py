"""
Memory Write Guard — front door for memory writes
==============================================

External/user-originated memory text passes through here before reaching
atomic_write. Internal cognition declares an autonomous source and bypasses
this user-input purity filter. The guard:

    1. Resolves the target lobe (3.0 integer lobe ID).
    2. Runs `MemoryPurity.check_leaf(text, ...)` for adversarial
       patterns (identity rewrite, emotional imprinting, ego projection).
    3. If the layer rejects, the write is refused.

This is a memory-level concern, not a guardian concern. The Guardian
focuses on security policy and decision authority, while memory controls
its own write validation.

Per the memory's purpose — keeping Omega's knowledge base clean —
the user is the threat vector for adversarial content. **External/UI
input is gated by default.** Internal cognition declares itself with
`source="omega:..."` and bypasses the guard (Omega must be free to
learn about itself).

Lobe taxonomy (3.0):

    0 (identity)   — full enforcement: identity-rewrite + emotional + ego
    1 (personal)   — full enforcement: identity-protection + adversarial
    2 (knowledge)  — adversarial only (academic content, no identity claims)
    3 (reasoning)  — adversarial only
    4 (context)    — adversarial only (session-level, low-risk)
    5 (skills)     — adversarial only
    6 (journal)    — adversarial only (Omega-only writers anyway)
    7 (vision)     — adversarial only
    9 (temporal)   — adversarial only
    11 (dev)       — bypass (legitimate code/test strings often look like attacks)

ARCHITECTURAL RULES:
    - Rejected external/user-originated writes never reach the filesystem.
    - Rejections are logged at WARNING and emitted as telemetry under
      `omega.memory.write_guard.rejected` with a `reason` tag.
    - The autonomous-source convention (`source="omega:..."`) is the
      contract. Adding a new internal subsystem doesn't require
      editing this file — just declare the source string properly.
    - Unknown / missing source defaults to *untrusted* (gate engages).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("omega.memory.write_guard")


# ── Lobe enforcement policy ──────────────────────────────────

# Lobes that bypass content policy entirely. Dev artefacts (lobe 11)
# legitimately contain strings that look like injection / jailbreak
# payloads (regex tests, security demos, comments describing attacks).
_BYPASS_LOBES = {11}

# Lobes 0/1 get FULL enforcement (identity-rewrite + emotional + ego); lobes 2-9 get
# adversarial-only. That distinction is made INSIDE MemoryPurity.check_leaf via the lobe name
# (the WHO-lobe checks only fire for identity/personal). The write guard only needs to know which
# lobes are enforced AT ALL — that is _ENFORCED_LOBES below (every mapped, non-bypass lobe).

# Canonical lobe names for MemoryPurity adversarial-pattern routing.
_LOBE_ID_TO_NAME = {
    0: "identity",
    1: "personal",
    2: "knowledge",
    3: "reasoning",
    4: "context",
    5: "skills",
    6: "journal",
    7: "vision",
    9: "temporal",
}

# Every lobe that receives adversarial-pattern enforcement (all mapped lobes). Only _BYPASS_LOBES
# and unknown/None lobes skip the check. The gate was previously {0, 1}, so the documented
# adversarial-only tier (lobes 2-9) was DEAD — those external writes reached disk unchecked.
_ENFORCED_LOBES = frozenset(_LOBE_ID_TO_NAME)


# ── Autonomous source convention ─────────────────────────────
#
# Memory gates EXTERNAL/UI input, not Omega's self-reflection.
# Any caller originating inside autonomous cognition declares itself
# by prefixing its `source=` with `_AUTONOMOUS_SOURCE_PREFIX`.
#
# Why a prefix and not a frozen set?
#   - Adding a new autonomous subsystem shouldn't require editing this file
#   - Unknown / missing source defaults to *untrusted* (safe default)
#   - `grep -r 'source="omega:' <omega-root>` enumerates every internal
#     cognition write site — a living audit list.
#
# Rule of thumb: if a human (or external tool acting on a human's
# behalf) originated the text, DO NOT use the `omega:` prefix.

_AUTONOMOUS_SOURCE_PREFIX = "omega:"


def _is_autonomous_source(source: str) -> bool:
    """True when `source` declares itself as internal cognition."""
    return bool(source) and source.startswith(_AUTONOMOUS_SOURCE_PREFIX)


# ── Lobe normalization ───────────────────────────────────────


def _coerce_lobe_id(value: Any) -> Optional[int]:
    """Normalize a lobe argument to an int or None.

    Accepts:
        int                   — used directly
        list/tuple of ints    — first element (X coord)
        str digits            — int(value)
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        return first if isinstance(first, int) else None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _emit_telemetry(event_name: str, **data: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event(
            "omega.memory.write_guard",
            event_name,
            **data,
        )
    except Exception as exc:
        # Telemetry sink unavailable — non-fatal, but a degraded
        # observability path inside the write gate is itself worth a
        # record on the structured surface.
        log.debug("write_guard: emit_telemetry sink failed: %s", exc)
        _emit_guard_signal(
            status="degraded",
            failure_class="telemetry_sink_unavailable",
            operation="emit_telemetry",
            error_type=type(exc).__name__,
            reason=str(exc)[:120],
            event_name=event_name,
            recovery="none",
        )


def _emit_guard_signal(*, status: str, failure_class: str, operation: str, **fields: Any) -> None:
    """Structured failure/degrade surface for the write gate. Lazy
    import of terminal_feedback so write_guard has no import-time
    dependency on the feedback package. Best-effort — never raises
    into the gate."""
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "memory",
            operation,
            status=status,
            summary=f"write_guard {operation} {status}: {failure_class}",
            component="write_guard",
            operation=operation,
            failure_class=failure_class,
            **fields,
        )
    except Exception as exc:
        log.debug("write_guard: structured emit failed: %s", exc)


# ── Public API: text-shaped guard ────────────────────────────


def guard_raw_text(
    text: str,
    *,
    lobe_id: Optional[int] = None,
    source: str = "",
    # back-compat: accept the old keyword names from gates/base.py
    source_layer: str = "",
    dewey_code: str = "",
) -> bool:
    """Return True if the text is allowed to proceed into ingest.

    Used by gates/base.py:_guardian_check as the simplest possible
    pre-write check (no Decomposition, no envelope — just text + lobe).
    Autonomous sources bypass the guard.
    """
    if _is_autonomous_source(source):
        return True

    # Resolve the lobe: prefer explicit lobe_id, fall back to dewey_code
    # for back-compat.
    lobe = _coerce_lobe_id(lobe_id)
    if lobe is None:
        lobe = _coerce_lobe_id(dewey_code)

    if lobe is None:
        # Unknown lobe → conservative pass; gate's permission check
        # will catch obvious mistakes downstream. The guard's job is
        # content policy, not routing.
        return True

    if lobe in _BYPASS_LOBES:
        return True

    if not text or not text.strip():
        return True

    # MemoryPurity adversarial-pattern check
    if lobe in _ENFORCED_LOBES:
        try:
            from guardian.memory_purity import PurityDecision

            mp = _purity_singleton()
            lobe_name = _LOBE_ID_TO_NAME.get(lobe, "who")
            result = mp.check_leaf(text=text, lobe=lobe_name, source=source)
            if result.decision == PurityDecision.REJECT:
                log.warning(
                    "write_guard purity rejected on lobe %d (%s): %s",
                    lobe,
                    result.reason,
                    text[:80],
                )
                _emit_telemetry(
                    "rejected_purity",
                    lobe_id=lobe,
                    reason=result.reason,
                    rule_id=result.rule_id,
                    text_preview=text[:80],
                )
                return False
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "memory",
                    "leaf_check",
                    passed=True,
                    lobe_id=lobe,
                    source=source,
                    decision=result.decision.value,
                )
            except Exception:
                pass
            # FLAG decisions are written-with-marker — the gate handles
            # marker attachment if it cares; the guard lets the write
            # proceed.
        except Exception as exc:
            log.debug("guardian.memory_purity unavailable (%s) — passing", exc)
            _emit_guard_signal(
                status="degraded",
                failure_class="memory_purity_unavailable_fail_open",
                operation="authorize",
                error_type=type(exc).__name__,
                lobe_id=lobe,
                source=source,
                reason=str(exc)[:120],
                recovery="none",
            )

    return True


# ── Public API: envelope-shaped guard (used by memory.api / promotion) ──


def guard_envelope(envelope: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """Run the full pre-write policy over a `memory.api.CanonicalEnvelope`-shaped dict.

    The envelope is the in-memory pre-persistence shape used by
    `memory.api.write_memory` and `memory.promotion.create_staging`.
    Verifying it here lets the verification chain catch contamination
    before staging instead of after promotion.

    Returns (allowed, rejections) where rejections is a list of
    `{stage, reason, category, detail, ...}` dicts. If allowed is True,
    rejections may still be non-empty (e.g. FLAG-level findings); the
    caller decides whether to attach markers.
    """
    rejections: List[Dict[str, Any]] = []

    if not envelope:
        return True, rejections

    payload = envelope.get("payload") or {}
    text = payload.get("text") or envelope.get("text") or ""
    coord = envelope.get("coord") or [0, 0, 0]
    lobe = _coerce_lobe_id(coord)
    source = envelope.get("source") or ""

    if _is_autonomous_source(source):
        return True, rejections

    if lobe is None or lobe in _BYPASS_LOBES:
        return True, rejections

    # 1. memory_purity: adversarial patterns (high-enforcement lobes only)
    if lobe in _ENFORCED_LOBES:
        try:
            from guardian.memory_purity import PurityDecision

            mp = _purity_singleton()
            lobe_name = _LOBE_ID_TO_NAME.get(lobe, "who")
            purity = mp.check_leaf(text=text, lobe=lobe_name, source=source)
            if purity.decision == PurityDecision.REJECT:
                rejections.append(
                    {
                        "stage": "memory_purity",
                        "reason": purity.reason,
                        "category": "purity_violation",
                        "detail": purity.rule_id,
                    }
                )
                return False, rejections
            elif purity.decision == PurityDecision.FLAG:
                rejections.append(
                    {
                        "stage": "memory_purity",
                        "reason": purity.reason,
                        "category": "purity_flag",
                        "detail": purity.rule_id,
                    }
                )
                # Flagged but allowed — fall through.
        except Exception as exc:
            log.debug("memory_purity unavailable (%s)", exc)
            _emit_guard_signal(
                status="degraded",
                failure_class="memory_purity_unavailable_fail_open",
                operation="authorize",
                error_type=type(exc).__name__,
                lobe_id=lobe,
                source=source,
                surface="envelope",
                reason=str(exc)[:120],
                recovery="none",
            )

    return True, rejections


# ── Memory-purity singleton (cheap, stateless across cycles for now) ──

_PURITY: Any = None


def _purity_singleton():
    global _PURITY
    if _PURITY is None:
        from guardian.memory_purity import MemoryPurity

        _PURITY = MemoryPurity()
    return _PURITY
