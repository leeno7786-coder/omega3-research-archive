"""Phase 7M — taxonomy advisory PRESSURE seam (read-only, flag-gated).

A single, bounded Contributor recalled from the COMPILED memory graph's pressure signals
(``memory.taxonomy.pressure_signals.analyze_memory_pressure``). It contributes attention
EVIDENCE to ``gather_contributors`` — and nothing else:

  * read-only: it only READS the compiled graph (never compiles, never writes the ledger,
    never mutates graph state)
  * advisory: it never opens an episode, never chooses Global intent, never chooses an
    action — Pressure arbitration stays the authority, Global A stays the intent parser,
    Meta stays the planner. The contributor is just one candidate among many.
  * bounded: weight is deterministic and capped at 0.60 so taxonomy memory can never
    overpower operator input (operators ignite at a far higher weight).
  * fail-soft: a missing / uncompiled / corrupt graph, or no recommended pressure, emits
    NOTHING (returns None).

Lives in pressure_loop (the consumer), so the taxonomy package stays standalone. Gated by
``OMEGA_MEMORY_TAXONOMY_PRESSURE`` (default OFF -> byte-identical gather).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pressure_loop.contributors import Contributor, make_contributor

# Deterministic, bounded weight by the most-urgent signal present (work-order §13).
_SIGNAL_WEIGHTS = {
    "memory_reconciliation": 0.55,
    "sense_resolution": 0.40,
    "memory_gap": 0.30,
    "safety_block_cluster": 0.45,
    "skill_failure_cluster": 0.45,
    "repeated_failure": 0.45,
    "bridge_failure_cluster": 0.45,
    "tool_failure_cluster": 0.45,
    "unverified_change": 0.35,
    "incomplete_task": 0.35,
    "repeated_continuation_pressure": 0.30,
}
_SIGNAL_PRIORITY = {
    "memory_reconciliation": 0,
    "safety_block_cluster": 1,
    "sense_resolution": 2,
    "skill_failure_cluster": 3,
    "repeated_failure": 4,
    "bridge_failure_cluster": 5,
    "tool_failure_cluster": 6,
    "incomplete_task": 7,
    "unverified_change": 8,
    "memory_gap": 9,
    "repeated_continuation_pressure": 10,
    "low_confidence_claims": 11,
}
_BASE_WEIGHT = 0.25  # any other single signal
_WEIGHT_CAP = 0.60  # never overpower operator input

# ── Staleness decay (operator 2026-07-08, first ungated live boot) ──
# A graph-derived signal that keeps firing off the SAME snapshot is
# advice the mind has already heard: memory_gap re-fired at a fixed
# 0.30 every gather (re-reading the same graph never changes it) and
# permanently out-bid learning_debt's 0.25 base — 22 onboarding-seeded
# mastery gaps never got a LEARN. After a grace window of identical
# emissions the weight halves per further emission down to a floor, so
# a static advisory YIELDS to grounded drives; a changed snapshot (new
# memories compiled — e.g. by the LEARN it yielded to) restores full
# weight. Process-local like the loop it advises.
_STALE_GRACE = 3
_STALE_DECAY_FACTOR = 0.5
_STALE_FLOOR = 0.05
_stale_state = {"key": None, "count": 0}


def _stale_decayed(key: tuple, weight: float) -> float:
    if _stale_state["key"] == key:
        _stale_state["count"] += 1
    else:
        _stale_state["key"] = key
        _stale_state["count"] = 1
    over = max(0, _stale_state["count"] - _STALE_GRACE)
    if over <= 0:
        return weight
    return max(_STALE_FLOOR, weight * (_STALE_DECAY_FACTOR**over))


def _reset_staleness_for_tests() -> None:
    _stale_state["key"] = None
    _stale_state["count"] = 0


def taxonomy_pressure_contributor(root: Optional[Path | str] = None) -> Optional[Contributor]:
    """Return a bounded advisory Contributor from the compiled graph, or None.

    None whenever: the flag's caller decided not to call us; the graph is missing /
    uncompiled / corrupt (fail-soft); analyze_memory_pressure recommends no pressure; or no
    signals fire. The returned contributor carries source="memory_taxonomy", type=the
    recommended pressure kind, a capped weight, and full evidence in metadata.
    """
    try:
        # READ-ONLY: analyze_memory_pressure opens the compiled graph and SELECTs; it never
        # compiles, writes, or opens an episode.
        from memory.taxonomy.pressure_signals import analyze_memory_pressure

        result = analyze_memory_pressure(root=root)
    except Exception:  # noqa: BLE001 — missing/uncompiled/corrupt graph -> no pressure
        result = {
            "signals": [],
            "recommended_pressure": "",
            "evidence_event_ids": [],
            "snapshot_hash": "",
        }

    signals = list(result.get("signals") or [])
    signals.sort(
        key=lambda s: (
            _SIGNAL_PRIORITY.get(str(s.get("signal") or ""), 99),
            str(s.get("signal") or ""),
        )
    )

    recommended = str((signals[0] or {}).get("recommended_pressure") or "") if signals else ""
    if not recommended or not signals:
        return None  # nothing to attend to

    weight = min(
        _WEIGHT_CAP,
        max(
            (_SIGNAL_WEIGHTS.get(str(s.get("signal")), _BASE_WEIGHT) for s in signals), default=0.0
        ),
    )
    if weight <= 0.0:
        return None
    stale_key = (
        str(result.get("snapshot_hash") or ""),
        recommended,
        tuple(sorted(str(s.get("signal")) for s in signals)),
    )
    weight = _stale_decayed(stale_key, weight)

    metadata = {
        "signals": [str(s.get("signal")) for s in signals],
        "evidence_event_ids": sorted(
            str(eid) for eid in (result.get("evidence_event_ids") or []) if eid
        ),
        "snapshot_hash": str(result.get("snapshot_hash") or ""),
        "recommended_pressure": recommended,
    }
    return make_contributor("memory_taxonomy", recommended, round(weight, 4), metadata)
