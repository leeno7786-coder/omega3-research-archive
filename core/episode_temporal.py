"""Episode temporal envelope — Phase 2 §P2-S12.

Compact, JSON-safe representation of the temporal context at the moment
a CognitiveEpisode opens — wall-clock + tick / phase / cycle / epoch context.

The envelope rides on ``episode.temporal`` so every organ (Global, Meta,
Auto, Omega, DeepSleep, response_hold outlet, tick-state WAL archive) can
reference one canonical "this is the moment this episode lived" record.

Design constraints (PACKET_P2-S12.md §C):
  - JSON-safe primitives only — no live objects, no datetime objects.
  - Compact: bounded fields, not full system state.
  - Failure-soft: composer never raises into the cognitive transition.
  - Uses core.symphony_time for all timestamp reads (authority).

Public surface:
  - ``capture_temporal_context(...)`` -> dict
  - ``compact_temporal_summary(envelope)`` -> small subset
  - ``default_temporal_provider()`` -> zero-arg callable for CueDesk-style injection
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("omega.core.episode_temporal")


# Canonical field set the envelope carries. Each maps to a compact
# subsection; schema enum + WAL serializer + audit §2 all agree on
# this exact list.
TEMPORAL_FIELDS: tuple[str, ...] = (
    "captured_at_iso",  # ISO 8601 UTC timestamp at compose time
    "wall_clock_seconds",  # Unix epoch seconds (for duration math)
    "tick_count",  # Pressure loop tick number (since boot)
    "phase",  # Current cognition phase (pressure/global/meta/auto/omega/deepsleep)
    "cycle_id",  # Current cycle identifier (if available)
    "epoch_id",  # Current epoch identifier (if available)
    "source",  # "live" / "unavailable"
)


def _empty_section(source: str = "unavailable") -> Dict[str, Any]:
    return {"source": source}


def _empty_envelope(source: str = "unavailable") -> Dict[str, Any]:
    return {
        "captured_at_iso": "",
        "wall_clock_seconds": 0.0,
        "tick_count": 0,
        "phase": "",
        "cycle_id": "",
        "epoch_id": "",
        "source": str(source or "unavailable"),
    }


def _now_iso() -> str:
    """Route through temporal authority."""
    from core.symphony_time import now_iso

    return now_iso()


def _now_wall() -> float:
    """Route through temporal authority."""
    from core.symphony_time import now_wall

    return now_wall()


def capture_temporal_context(
    *,
    tick_count: Optional[int] = None,
    phase: Optional[str] = None,
    cycle_id: Optional[str] = None,
    epoch_id: Optional[str] = None,
    now_iso: Optional[str] = None,
    now_wall: Optional[float] = None,
) -> Dict[str, Any]:
    """Capture a compact temporal envelope at episode open time.

    Args:
      tick_count: optional pressure loop tick number. If None, defaults to 0.
      phase: optional current cognition phase. If None, defaults to
        "pressure" (the open happens in the pressure loop).
      cycle_id: optional current cycle identifier. If None, defaults to empty string.
      epoch_id: optional current epoch identifier. If None, defaults to empty string.
      now_iso: optional ISO timestamp override (test determinism).
      now_wall: optional wall-clock seconds override (test determinism).

    Returns a JSON-safe dict whose keys match ``TEMPORAL_FIELDS``.
    NEVER raises — any failure mode degrades to ``source="unavailable"``.
    """
    envelope = _empty_envelope(source="unavailable")
    envelope["captured_at_iso"] = str(now_iso) if now_iso is not None else _now_iso()
    envelope["wall_clock_seconds"] = float(now_wall) if now_wall is not None else _now_wall()

    # Tick count defaults to 0 if not provided
    envelope["tick_count"] = int(tick_count or 0)

    # Phase defaults to "pressure" (episode open happens in pressure loop)
    envelope["phase"] = str(phase or "pressure")

    # Cycle_id and epoch_id default to empty strings if not provided
    envelope["cycle_id"] = str(cycle_id or "")
    envelope["epoch_id"] = str(epoch_id or "")

    # Honest signal: "live" ONLY when a real tick was threaded in. The prior
    # `tick_count >= 0` test was always true (tick_count defaults to 0), so source
    # was hardcoded "live" even when the caller supplied no temporal context at all
    # — a fabricated measurement. Absent a real tick, degrade to "unavailable".
    envelope["source"] = "live" if tick_count is not None else "unavailable"

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "temporal_context",
            ok=True,
            tick_count=envelope["tick_count"],
            phase=envelope["phase"],
            cycle_id=envelope["cycle_id"],
            epoch_id=envelope["epoch_id"],
        )
    except Exception:
        pass

    return envelope


def compact_temporal_summary(envelope: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce a full envelope to the small subset receipts carry.

    Three fields — captured_at_iso, source, tick_count — so Global /
    Omega / DeepSleep receipts stay JSON-compact under P2-S5 archive
    rules while still naming "this is the temporal moment this receipt
    belongs to."
    """
    if not isinstance(envelope, dict) or not envelope:
        return {}
    return {
        "captured_at_iso": str(envelope.get("captured_at_iso") or ""),
        "source": str(envelope.get("source") or "unavailable"),
        "tick_count": int(envelope.get("tick_count") or 0),
    }


def human_temporal(
    envelope: Optional[Dict[str, Any]] = None, *, now_wall_override: Optional[float] = None
) -> Dict[str, Any]:
    """Derive a mind-readable LOCAL wall-clock view from an episode temporal envelope.

    The envelope carries the canonical UTC epoch (``wall_clock_seconds``, from symphony_time);
    this renders it in the operator's local timezone as human strings so the model can reason about
    "now" naturally. Falls back to a fresh symphony_time read when the envelope has no usable epoch,
    so a time view is ALWAYS available. Failure-soft: never raises (returns {} on hard failure).
    """
    from datetime import datetime, timezone

    epoch: Optional[float] = None
    if isinstance(envelope, dict):
        raw = envelope.get("wall_clock_seconds")
        if isinstance(raw, (int, float)) and raw > 0:
            epoch = float(raw)
    if epoch is None:
        epoch = float(now_wall_override) if now_wall_override is not None else _now_wall()

    try:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone()  # -> local tz
    except (OverflowError, OSError, ValueError):
        return {}

    weekday = dt.strftime("%A")
    month = dt.strftime("%B")
    hour12 = ((dt.hour + 11) % 12) + 1  # 0->12, 13->1 (portable; avoids %-I platform split)
    ampm = "AM" if dt.hour < 12 else "PM"
    tz = (dt.strftime("%Z") or dt.strftime("%z")).strip()
    human_date = f"{month} {dt.day} {dt.year}"
    human_time = f"{hour12}:{dt.minute:02d} {ampm} {tz}".strip()
    return {
        "weekday": weekday,
        "human_date": human_date,
        "human_time": human_time,
        "tz": tz,
        "local_iso": dt.isoformat(),
        "human": f"{weekday}, {human_date} at {human_time}".strip(),
    }


def temporal_prompt_line(envelope: Optional[Dict[str, Any]] = None) -> str:
    """One-line 'Current date & time: …' for injection into the mind's prompt. '' on failure."""
    try:
        view = human_temporal(envelope)
        human = view.get("human") if isinstance(view, dict) else None
        return f"Current date & time: {human}" if human else ""
    except Exception:  # noqa: BLE001 — prompt context is best-effort, never break a generation
        return ""


def prepend_time_line(system_prompt: str, envelope: Optional[Dict[str, Any]] = None) -> str:
    """Prepend the 'Current date & time: …' line to a model system prompt.

    FIRST-CLASS TIME (task #17, operator 2026-07-16): every cognitive model
    pass surfaces the wall clock so the mind knows *when it is*. ONE shared
    helper so every phase renders the SAME line (anti-drift — the AUTO
    promptpack, the omega per-episode/motor/window passes, and the fast
    reply all route here). Pass the episode's fixed ignition envelope for
    per-episode determinism, or a fresh capture_temporal_context(...) for
    "now" surfaces (window fold, fast reply). Failure-soft: on any failure
    the system prompt passes through unchanged."""
    try:
        line = temporal_prompt_line(envelope)
        return f"{line}\n\n{system_prompt}" if line else str(system_prompt or "")
    except Exception:  # noqa: BLE001 — prompt context is best-effort
        return str(system_prompt or "")


def default_temporal_provider() -> Callable[[], Dict[str, Any]]:
    """Return a zero-arg callable that builds a fresh envelope per call.

    Convenience for CueDesk-style injection. In the current rebuild,
    cognition_chain.py calls capture_temporal_context directly, but this
    provider is available for future CueDesk-style patterns.
    """

    def _provider() -> Dict[str, Any]:
        return capture_temporal_context()

    return _provider
