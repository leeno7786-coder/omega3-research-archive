"""
Consolidation Worker — Background Memory Maintenance
======================================================

Low-priority worker that performs offline memory hygiene:
    - StrengthIndex persistence (batch save dirty records)
    - Future: deduplication, link-graph rewiring, recency reindexing

Per the contract:
    - Resource-capped (must yield CPU on demand)
    - Pausable by the orchestrator (no in-flight transactions held
      across yields)
    - Never invokes the cognitive model
    - Read-only with respect to memory contents — touches only the
      index/strength-tracker, never rewrites memory files

Used by:
    - memory.api.decay_tick(now_ts)
    - Orchestrator's slow tick when cognitive load is low

ARCHITECTURAL RULES:
    - All writes go through the same atomic-write contract used by
      gates (validate → tmp → fsync → rename).
    - The worker is a pure side-effect call; it returns a small report
      dict for telemetry but does not raise on per-record failures.
    - This module MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

log = logging.getLogger("omega.memory.consolidation")


def run_consolidation_once(*, limit_resources: bool = True) -> Dict[str, Any]:
    """Run one consolidation pass.

    Returns a report dict with:
        - status: "ok" | "degraded"
        - persisted_strength_records: number of dirty records flushed
        - elapsed_seconds: wall-clock duration
        - timestamp: ISO timestamp of the run

    `limit_resources=True` means the worker yields CPU between sub-steps
    so it can be pre-empted by foreground cognitive work.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    persisted = 0
    status = "ok"
    dirty_after = 0

    try:
        from memory.strength_index import ensure_strength_index

        si = ensure_strength_index()

        before_dirty = getattr(si, "_dirty", 0)
        si.save(force=False)  # only writes if past threshold by default
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "strength_index_save",
                summary=f"dirty_before={before_dirty}",
                dirty_before=before_dirty,
                dirty_after=int(getattr(si, "_dirty", 0)),
                force=False,
            )
        except Exception:
            pass
        # If still dirty after a non-force pass and we're resource-capped,
        # do one forced save to flush the tail of changes.
        if getattr(si, "_dirty", 0) > 0 and not limit_resources:
            si.save(force=True)
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "memory",
                    "strength_index_save",
                    summary="force_flush",
                    dirty_after=int(getattr(si, "_dirty", 0)),
                    force=True,
                )
            except Exception:
                pass
        persisted = before_dirty - getattr(si, "_dirty", 0)
        dirty_after = int(getattr(si, "_dirty", 0))
    except Exception as exc:
        log.warning("consolidation StrengthIndex flush failed: %s", exc)
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "consolidation_strength_flush_failed",
            status="degraded",
            ok=False,
            error=str(exc),
        )
        status = "degraded"

    contradiction_report: Dict[str, Any] = {}
    try:
        from memory.contradiction_resolution import process_contradiction_candidates

        contradiction_report = process_contradiction_candidates(max_groups=5)
    except Exception as exc:
        log.warning("consolidation contradiction pass failed: %s", exc)
        status = "degraded"

    if limit_resources:
        # Cooperative yield — gives a runaway scheduler a chance to
        # pre-empt before any future heavier sub-step is added.
        time.sleep(0)

    report = {
        "status": status,
        "persisted_strength_records": int(max(0, persisted)),
        "contradiction_groups": int(contradiction_report.get("groups_processed") or 0),
        "contradiction_adjudications": len(contradiction_report.get("adjudications") or []),
        "contradiction_persisted": int(contradiction_report.get("persisted_count") or 0),
        "contradiction_demoted": int(contradiction_report.get("demoted_count") or 0),
        "elapsed_seconds": round(time.time() - t0, 4),
        "timestamp": started_at,
    }
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "run_consolidation_once",
            summary=f"flushed={report['persisted_strength_records']}",
            flushed=report["persisted_strength_records"],
            pruned=0,
            dirty_records=dirty_after,
            status=status,
        )
    except Exception:
        pass
    return report
