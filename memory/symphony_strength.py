"""Symphony §S8 — StrengthIndex orient snapshot builder.

Builds the live StrengthIndex snapshot consumed by GLOBAL_A orient
under symphony mode. The snapshot is the AUTHORITY for the body's
memory-salience frame — when `source == "live"` it overlays
`reflection_snapshot.performance_trends.strength_avg` so
`reflective_reasoner` gates on the live aggregate instead of the
stale telemetry-file batch (the symphony-side cure for the disease
class where reflection-time decisions ran on file-trend latency).

The cure shape mirrors the §S9 neuromod cure: a same-cycle live
read surface (StrengthIndex singleton) plus an `unavailable`
empty-shell fallback that keeps the orient frame writable even when
the index hasn't been wired (boot order, test isolation, etc).

Public surface:
    build_strength_index_snapshot(*, top_n=20) -> dict
        Returns a JSON-serializable snapshot dict. Never raises —
        failures (import, ensure_strength_index, aggregate, etc.)
        return an empty-shell with `source == "unavailable"` so the
        caller can still attach it to orient without conditional
        wrapping.

Snapshot shape (PACKET_S10 §4.1 design, v0.2 §S8 operator-locked):
    {
      "aggregate":          {... StrengthIndex.aggregate() output ...},
      "aggregate_strength": float,
      "top_hot": [
        {"memory_id": str, "strength": float, "touch_count": int},
        ...
      ],
      "source": "live" | "unavailable",
    }

`aggregate_strength` derivation: mean of `get_strength(mid)` over the
top_hot list. Uses live decay + touch boost (the per-memory current
strength) rather than the write-time `mean_recent_write_strength`
(which would echo the file-trend disease the overlay is curing).

ARCHITECTURAL RULES:
    - This module is pure I/O against `memory.strength_index`.
      No reasoning, no decisions — just the canonical snapshot the
      symphony harness consumes.
    - Never raises. Every failure mode returns the "unavailable"
      shell so the orient wire never needs a defensive try/except.
    - Module-level constants for the empty shell ensure the
      contract surface is single-sourced; tests assert on the same
      shape.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from guardian.terminal_feedback import emit_phase, emit_signal

log = logging.getLogger("omega.memory.symphony_strength")


# ---------------------------------------------------------------------
# Snapshot shape constants
# ---------------------------------------------------------------------

# `source` is the gate the orient wire reads to decide whether to
# overlay `performance_trends.strength_avg` (operator-locked contract).
SOURCE_LIVE: str = "live"
SOURCE_UNAVAILABLE: str = "unavailable"

# Default top-N for ranking. Mirrors PACKET_S10 §4.1's suggested value;
# callers can override via the kwarg if the orient frame should carry a
# wider hot tail.
_DEFAULT_TOP_N: int = 20


def _empty_shell() -> Dict[str, Any]:
    """Single source of truth for the failure-mode return shape.
    Mirrored by the §S8 audit's behavioral §5c assertions."""
    return {
        "aggregate": {},
        "aggregate_strength": 0.0,
        "top_hot": [],
        "source": SOURCE_UNAVAILABLE,
    }


def build_strength_index_snapshot(
    *,
    top_n: int = _DEFAULT_TOP_N,
) -> Dict[str, Any]:
    """Build the live StrengthIndex snapshot for GLOBAL_A orient.

    See module docstring for the snapshot shape contract. Returns the
    "unavailable" empty shell on any failure mode (never raises).

    Args:
        top_n: Maximum number of top-strength memory_ids to include in
            the snapshot. Clamped to >= 0; values <= 0 yield an empty
            top_hot list (the snapshot remains "live" if the aggregate
            read succeeded).
    """
    # Defensive import — keeps the symphony harness boot-safe even
    # when memory.strength_index hasn't been wired yet.
    try:
        from core.memory_config import memory_enabled

        if not memory_enabled():
            return _empty_shell()
    except Exception:
        pass

    try:
        from memory.strength_index import ensure_strength_index
    except Exception as exc:
        log.debug("build_strength_index_snapshot: import failed: %s", exc)
        return _empty_shell()

    try:
        si = ensure_strength_index()
    except Exception as exc:
        log.debug(
            "build_strength_index_snapshot: ensure_strength_index raised: %s",
            exc,
        )
        return _empty_shell()

    try:
        from core.strength_index_config import aggregate_recency_seconds

        aggregate = dict(si.aggregate(**aggregate_recency_seconds()))
    except Exception as exc:
        log.debug(
            "build_strength_index_snapshot: aggregate() raised: %s",
            exc,
        )
        return _empty_shell()

    try:
        items = si.items_snapshot()
    except Exception as exc:
        log.debug(
            "build_strength_index_snapshot: items_snapshot() raised: %s",
            exc,
        )
        return _empty_shell()

    # Rank by current strength (live decay + touch boost applied via
    # get_strength). Records that resolve to <= 0 strength are dropped
    # — they wouldn't contribute meaningful body-state authority.
    ranked: List[Tuple[str, float, int]] = []
    for mid, rec in items:
        try:
            current_strength = float(si.get_strength(mid))
        except Exception:
            continue
        if current_strength <= 0.0:
            continue
        try:
            touches = int(rec.get("touch_count", 0))
        except (TypeError, ValueError):
            touches = 0
        ranked.append((mid, current_strength, touches))

    ranked.sort(key=lambda kv: kv[1], reverse=True)
    bounded_top_n = max(0, int(top_n))
    top_slice = ranked[:bounded_top_n] if bounded_top_n > 0 else []

    top_hot: List[Dict[str, Any]] = [
        {"memory_id": mid, "strength": strength, "touch_count": touch_count}
        for mid, strength, touch_count in top_slice
    ]

    if top_hot:
        aggregate_strength = sum(entry["strength"] for entry in top_hot) / len(top_hot)
    else:
        aggregate_strength = 0.0

    snapshot = {
        "aggregate": aggregate,
        "aggregate_strength": float(aggregate_strength),
        "top_hot": top_hot,
        "source": SOURCE_LIVE,
    }
    try:
        emit_phase("symphony", "strength_index", status="enter", summary="building snapshot")
    except Exception:
        pass
    try:
        emit_phase(
            "symphony",
            "strength_index",
            status="ok",
            summary=(
                f"source=live agg={aggregate_strength:.3f} "
                f"hot={len(top_hot)} tracked={aggregate.get('total_tracked', '?')}"
            )[:200],
            aggregate_strength=float(aggregate_strength),
            hot_count=len(top_hot),
            total_tracked=aggregate.get("total_tracked"),
        )
        emit_signal(
            "symphony.strength_index.aggregate",
            float(aggregate_strength),
            source=SOURCE_LIVE,
            hot_count=len(top_hot),
        )
    except Exception:
        pass
    return snapshot


def hot_count_for_domain(
    domain: str,
    *,
    hot_age_seconds: float | None = None,
    strength_index: Any = None,
) -> int:
    """Count memories in ``domain`` whose engram is currently HOT.

    Joins the coord index (``mid -> domain``) with the StrengthIndex
    (``mid -> last_access``) to answer "how many hot memories does this
    domain have right now". This is the per-domain practice-bar input for
    the ``execution_gap`` pressure contributor (Phase 5b): a learned-but-
    unapplied domain becomes practice-ready only when ITS OWN substrate is
    warm — never because some unrelated domain happens to be hot. That is
    the sharpening of the Phase 5a global-aggregate bar, which could fire
    while the domain being practiced was stone cold.

    Args:
        domain: the mastery ``domain_id`` (matches the engram ``domain``
            field written by the gates / coding helper). Empty → 0.
        hot_age_seconds: hotness window; defaults to the governed
            ``strength_index`` recency bundle's ``hot_age_seconds`` (the
            same window ``StrengthIndex.aggregate`` uses), 3600.0 on
            config failure.
        strength_index: optional injected StrengthIndex (tests); production
            resolves the singleton via ``ensure_strength_index()``.

    Failure-soft: any missing wire (no coord index, unwired StrengthIndex,
    bad domain, lookup error) returns 0 — a cold/unknowable domain
    manufactures NO execution pressure (the conservative direction: never
    practice into the void). NEVER raises.
    """
    if not domain:
        return 0
    try:
        if hot_age_seconds is None:
            try:
                from core.strength_index_config import aggregate_recency_seconds

                hot_age_seconds = float(aggregate_recency_seconds().get("hot_age_seconds", 3600.0))
            except Exception:
                hot_age_seconds = 3600.0

        if strength_index is None:
            from memory.strength_index import ensure_strength_index

            strength_index = ensure_strength_index()
        if strength_index is None:
            return 0

        from memory.engine.walker import list_ids_by_domain, load_coord_index

        load_coord_index()  # pick up any newly-landed memories before the join
        mids = list_ids_by_domain(domain)
        return sum(1 for mid in mids if strength_index.is_hot(mid, float(hot_age_seconds)))
    except Exception as exc:  # noqa: BLE001 - failure-soft per contributor contract
        log.debug("hot_count_for_domain(%s): failure-soft -> 0 (%s)", domain, exc)
        return 0
