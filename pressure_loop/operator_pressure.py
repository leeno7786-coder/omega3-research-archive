"""Operator-pressure surface — S6 scaffold.

Wires the operator's learning directives into the symphony pressure
field + episode-open path. Three responsibilities:

  1. `publish_operator_directive(...)` — receiver helper. Detects
     'learn X' / 'study X' / etc. in operator chat, normalizes into
     the deterministic `OPERATOR_DIRECTIVE` SS record, persists via
     `write_canonical`.

  2. `read_operator_directive(...)` + `is_directive_active(...)` --
     TTL-aware readers that pressure_field + cue_desk consume.

  3. `seed_trajectory_from_operator_directive(...)` -- thin wrapper
     that takes an active directive + writes a minimal
     OmegaTrajectory shape to `LAST_OMEGA_TRAJECTORY` so META_A's
     first read sees a real guidance trajectory pointing at the
     operator's domain. Single-cycle override; the omega loop's own
     phase_4_commit later supersedes via the canonical
     `trajectory_store` path (which writes the durable identity-lobe
     record).

Why a module separate from `brain_receiver`:
  - Keeps the pressure-side logic testable in isolation (no boot,
    no orchestrator).
  - Cue sheet declares `operator_pressure` as one of the legitimate
    `LAST_OMEGA_TRAJECTORY` writers alongside `omega_conduct`; this
    module IS that writer.
  - The receiver still owns 'when' to publish; this module owns
    'what shape' + 'how to read' + 'how to merge into trajectory'.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.shared_state import SharedState
from core.shared_state_keys import (
    LAST_OMEGA_TRAJECTORY,
    OPERATOR_DIRECTIVE,
)

log = logging.getLogger("omega.pressure_loop.operator_pressure")


# ---------------------------------------------------------------------
# Detection — deterministic, no model inference on the receiver path
# ---------------------------------------------------------------------

# Learning-verb patterns. Multi-word patterns precede single-word so
# 'study up on X' wins over the bare 'study' token if both appear.
_LEARNING_PATTERNS = (
    re.compile(r"\b(?:please\s+)?(?:learn|study|research|investigate|explore)\b", re.IGNORECASE),
    re.compile(r"\bteach\s+me\b", re.IGNORECASE),
    re.compile(r"\b(?:i\s+want\s+to|i\s+need\s+to)\s+(?:learn|understand|study)\b", re.IGNORECASE),
)

# Default pressure weight for an active directive. 0.7 was chosen so a
# lone directive on a fresh SS (presence=PRESENT, no curiosity) clears
# the default 0.55 open_threshold without needing other contributors,
# but a directive alone doesn't max the aggregate -- room for stacking
# with curiosity / guardian / etc. when they fire.
DEFAULT_DIRECTIVE_WEIGHT = 0.7

# TTL default -- 5 minutes. A directive that hasn't opened an episode
# within 5 minutes of being uttered is stale; either the operator
# moved on OR pressure stayed sub-threshold for other reasons. Either
# way, don't keep auto-firing episodes on it.
DEFAULT_DIRECTIVE_TTL_SECONDS = 300

# Phase 2 §P2-S1 — ingress pressure TTL. Short because operator-turn
# ingress pressure should influence the NEXT one or two ticks, then
# decay so a single utterance does not keep opening episodes. CueDesk
# tick interval is typically 1-3 seconds; 60s window covers the
# realistic "did this operator turn open an episode in the next few
# ticks" question. read_operator_message applies linear decay over this TTL.
DEFAULT_INGRESS_TTL_SECONDS = 60


def _now_iso() -> str:
    # Phase 2 §P2-S20: route through the temporal authority so operator
    # directive issued_at timestamps pin under fake time alongside
    # episode / pack / archive surfaces.
    from core.symphony_time import now_iso

    return now_iso()


def detect_learning_directive(text: str) -> bool:
    """True iff the operator utterance is a learning directive.
    Cheap regex scan; never raises. Used by brain_receiver before
    calling publish_operator_directive."""
    if not isinstance(text, str) or not text.strip():
        return False
    return any(p.search(text) for p in _LEARNING_PATTERNS)


# ---------------------------------------------------------------------
# Redirect / pivot directives — flag-gated (default OFF)
# ---------------------------------------------------------------------

# When ON, an operator REDIRECT ("change of plan — focus on X") is
# classified as operator_command (so it rides supremacy + the enqueue
# path and reaches Omega as operator_command) and immediately seeds the
# trajectory (approach A: the directive text as free-text guidance;
# Omega decomposes the real subcategory + supersedes at window close).
# OFF -> byte-identical: a redirect stays `phatic` and seeds nothing.
OMEGA_OPERATOR_REDIRECT: bool = True


def redirect_directive_enabled() -> bool:
    """Read the redirect-directive flag at CALL time (so tests / a future
    config flip can monkeypatch the module constant and be observed)."""
    return bool(OMEGA_OPERATOR_REDIRECT)


# Pivot/redirect patterns — the operator re-points the agenda mid-session.
# Distinct from the learn-verbs of `_LEARNING_PATTERNS`. Kept tight to avoid
# false positives on ordinary chat; a stray match self-corrects (the seed has
# a TTL and Omega supersedes at window close).
_REDIRECT_PATTERNS = (
    re.compile(r"\bchange of (?:plan|focus|direction|priorities)\b", re.IGNORECASE),
    re.compile(r"\b(?:switch|pivot)\s+(?:to|over)\b", re.IGNORECASE),
    re.compile(r"\bredirect\b", re.IGNORECASE),
    re.compile(r"\bfocus(?:\s+instead)?\s+on\b", re.IGNORECASE),
    # Require a directive shape ("new priority: X", "new priority is/to X", or
    # end-of-clause) so a DESCRIPTIVE phrase ("the new priority queue is faster")
    # does not match.
    re.compile(
        r"\bnew\s+(?:priority|priorities|focus|direction|plan)\b"
        r"(?=\s*[:,\-]|\s+(?:is|to)\b|\s*$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:drop|forget|pause|stop|abandon)\b.*\b(?:and|then)\b.*"
        r"\b(?:focus|work|start|switch|do|move|build|learn|study)\b",
        re.IGNORECASE,
    ),
)

# An utterance ASKING ABOUT a redirect/switch (a question, a knowledge request,
# or a self-referential aside) is NOT a directive to re-point the agenda. Checked
# before the pattern scan so "what is a 301 redirect" / "how do I redirect stdout?"
# / "should we focus on X?" / "I want to focus on my own work" stay non-directives.
_REDIRECT_NEGATIVE = (
    re.compile(
        r"^\s*(?:what|how|why|when|where|who|which|is|are|do|does|can|could|should|would)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:explain|define|meaning of|difference between)\b", re.IGNORECASE),
    # Self-referential aside ("focus on my own work"), not an agenda pivot. Kept
    # narrow to "my own" so a legitimate directive ("focus on my deploy pipeline")
    # is not over-suppressed.
    re.compile(r"\bmy own\b", re.IGNORECASE),
)


def detect_redirect_directive(text: str) -> bool:
    """True iff the operator utterance re-points the agenda (a pivot/redirect
    directive). Cheap regex scan; never raises. Distinct from
    detect_learning_directive (learn-verbs). An interrogative / knowledge /
    self-referential utterance is rejected even if it contains a redirect word."""
    if not isinstance(text, str) or not text.strip():
        return False
    if any(p.search(text) for p in _REDIRECT_NEGATIVE):
        return False
    return any(p.search(text) for p in _REDIRECT_PATTERNS)


# ---------------------------------------------------------------------
# Publish — canonical SS writer (receiver-side)
# ---------------------------------------------------------------------


def publish_operator_directive(
    shared_state: SharedState,
    text: str,
    *,
    domain: str = "",
    weight: float = DEFAULT_DIRECTIVE_WEIGHT,
    ttl_seconds: float = DEFAULT_DIRECTIVE_TTL_SECONDS,
    issued_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a learning directive to LAST_OPERATOR_DIRECTIVE via the
    canonical writer.

    Caller (typically brain_receiver.handle_user_input) is responsible
    for deciding WHEN to publish; this helper owns the SHAPE +
    persistence + validator round-trip. Returns the record dict so the
    caller can log / surface to UI.

    Defensive: never raises -- a validator rejection logs a warning
    and returns the record without persisting (caller can inspect the
    return for the .persisted flag if/when surfaced). NEVER blocks the
    brain cycle on a directive publish failure."""
    record: Dict[str, Any] = {
        "text": (text or "").strip(),
        "domain": (domain or "").strip(),
        "weight": float(max(0.0, min(1.0, weight))),
        "issued_at": issued_at or _now_iso(),
        "ttl_seconds": float(ttl_seconds),
    }
    try:
        shared_state.write_canonical(OPERATOR_DIRECTIVE, record)
        log.info(
            "operator_pressure: published directive (domain=%r weight=%.2f ttl=%.0fs text=%r)",
            record["domain"],
            record["weight"],
            record["ttl_seconds"],
            record["text"][:60],
        )
        record["_persisted"] = True
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "publish_operator_directive",
                ok=True,
                domain=record["domain"],
                weight=record["weight"],
            )
        except Exception:
            pass
    except Exception as exc:
        log.warning(
            "operator_pressure: publish failed (%s: %s); directive will NOT contribute to pressure",
            type(exc).__name__,
            exc,
        )
        record["_persisted"] = False
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "publish_operator_directive",
                ok=False,
                error=str(exc),
            )
        except Exception:
            pass
    return record


def read_operator_directive(
    shared_state: SharedState,
    *,
    now_iso: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the current operator directive if present AND within TTL.
    Returns None when absent, malformed, expired, or read failure --
    NEVER raises (callers are pressure_field + cue_desk on the live
    boot loop)."""
    try:
        rec = shared_state.read(OPERATOR_DIRECTIVE)
    except Exception:
        return None
    if not isinstance(rec, dict) or not rec.get("text"):
        return None
    # TTL check
    issued = rec.get("issued_at")
    ttl = rec.get("ttl_seconds")
    if isinstance(issued, str) and isinstance(ttl, (int, float)) and ttl > 0:
        try:
            # Phase 2 §P2-S20: derive "now" from the temporal authority
            # when the caller didn't pin one explicitly, so directive
            # TTL math is coherent with episode timestamps under fake time.
            from core.symphony_time import now_iso as _authority_now_iso

            issued_dt = datetime.fromisoformat(issued)
            if now_iso:
                now_dt = datetime.fromisoformat(now_iso)
            else:
                _auth_str = _authority_now_iso()
                now_dt = datetime.fromisoformat(
                    _auth_str[:-1] + "+00:00" if _auth_str.endswith("Z") else _auth_str
                )
            if issued_dt.tzinfo is None:
                # Defensive: directives published without tz info treat
                # as UTC so comparison doesn't TypeError.
                issued_dt = issued_dt.replace(tzinfo=timezone.utc)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            age = (now_dt - issued_dt).total_seconds()
            if age > float(ttl):
                return None  # expired
        except (ValueError, TypeError) as exc:
            # Malformed timestamp -- treat as expired (fail-closed,
            # don't open episodes off a directive we can't age-check).
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "read_operator_directive",
                status="degraded",
                reason="malformed_timestamp",
                ok=False,
                error=str(exc),
            )
            return None
    return rec


def is_directive_active(shared_state: SharedState) -> bool:
    """Cheap predicate. True iff there's a non-expired directive."""
    return read_operator_directive(shared_state) is not None


# ---------------------------------------------------------------------
# Trajectory seed — symphony S6 wire
# ---------------------------------------------------------------------


def seed_trajectory_from_operator_directive(
    shared_state: SharedState,
    directive: Dict[str, Any],
    *,
    cycle_id: str = "operator_directive_seed",
    epoch: int = 0,
    persistence_cycles: int = 3,
) -> Optional[Dict[str, Any]]:
    """Write a minimal LAST_OMEGA_TRAJECTORY record reflecting the
    operator's directive, so META_A's first read finds real guidance
    pointing at the right domain.

    Single-cycle seed -- the omega loop's own phase_4_commit will
    later supersede via the canonical trajectory_store path (which
    writes the durable identity-lobe record + republishes the SS
    slot). This seed is the override that gets META_A pointed in the
    right direction the FIRST time, before omega has had a chance to
    publish a real trajectory from the operator's intent.

    Cue sheet declares `operator_pressure` as a legitimate
    `LAST_OMEGA_TRAJECTORY` writer alongside `omega_conduct` -- this
    function IS that writer.

    Returns the trajectory dict that was written (or None on validator
    rejection / write failure -- never raises; symphony chain runs
    even without trajectory seed)."""
    if not isinstance(directive, dict):
        return None
    text = (directive.get("text") or "").strip()
    domain = (directive.get("domain") or "").strip() or "general"
    if not text:
        return None
    trajectory = {
        "trajectory_id": f"operator-intent-{epoch}-{abs(hash(text)) % 10**8:08d}",
        "timestamp": _now_iso(),
        "cycle_id": cycle_id,
        "epoch_emitted": int(epoch),
        "intent": "deepen_capability",  # closest blueprint enum for operator learning
        "target_domain": domain,
        # Operator directives don't carry a structured capability
        # field; use the raw text as the capability descriptor (the
        # LAW grounding doesn't depend on this being a specific
        # enum value -- it's free-text guidance for META_A).
        "target_capability": text[:120],
        "rationale": "operator-issued learning directive (S6 seed)",
        "influencer_text": text[:200],
        "persistence_cycles": int(persistence_cycles),
        "cycles_remaining": int(persistence_cycles),
        "completed": False,
    }
    try:
        shared_state.write_canonical(LAST_OMEGA_TRAJECTORY, trajectory)
        log.info(
            "operator_pressure: trajectory seeded (domain=%r trajectory_id=%s)",
            trajectory["target_domain"],
            trajectory["trajectory_id"],
        )
        return trajectory
    except Exception as exc:
        log.warning(
            "operator_pressure: trajectory seed failed (%s: %s); "
            "META_A will read whatever guidance was already in SS",
            type(exc).__name__,
            exc,
        )
        return None
