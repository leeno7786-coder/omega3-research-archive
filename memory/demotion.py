"""Governed lifecycle demotion for contradiction-eligible memories."""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.memory_config import contradiction_rules, demotion_rules, memory_enabled

log = __import__("logging").getLogger("omega.memory.demotion")

# On-disk `type` values map to lifecycle rank (higher = more promoted).
_TYPE_RANK: dict[str, int] = {
    "observation": 0,
    "episode": 1,
    "concept": 1,
    "semantic": 2,
    "semantic_fact": 2,
    "procedural": 3,
    "skill": 3,
    "self_model": 4,
    "identity": 4,
}

_DEMOTED_TYPE: dict[int, str] = {
    4: "procedural",
    3: "semantic_fact",
    2: "concept",
    1: "observation",
    0: "observation",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_canonical(doc: dict[str, Any]) -> bool:
    flags = doc.get("flags") if isinstance(doc.get("flags"), dict) else {}
    return bool(flags.get("canonical") or doc.get("canonical"))


def count_contradiction_strikes(doc: dict[str, Any]) -> int:
    """Count loser adjudications recorded on this memory."""
    audit = doc.get("audit")
    if not isinstance(audit, list):
        return 0
    return sum(
        1
        for entry in audit
        if isinstance(entry, dict) and entry.get("what") == "contradiction_adjudicated"
    )


def demotion_trigger_after_contradiction(
    doc: dict[str, Any],
    *,
    confidence_after: float,
) -> tuple[bool, str]:
    """Whether lifecycle demotion should run after a contradiction penalty."""
    below = float(confidence_after) < float(
        contradiction_rules().get("demotion_eligible_below") or 0.25
    )
    if below:
        return True, "contradiction_confidence_below_threshold"

    threshold = int(demotion_rules().get("contradiction_threshold") or 2)
    strikes = count_contradiction_strikes(doc)
    if strikes >= max(1, threshold):
        return True, "contradiction_strike_threshold"
    return False, ""


def demotion_confidence_floor() -> float:
    return float(demotion_rules().get("confidence_floor") or 0.20)


def sustained_floor_cycle_threshold() -> int:
    return max(1, int(demotion_rules().get("sustained_floor_cycles") or 5))


def compute_next_below_floor_cycles(
    current: int,
    *,
    confidence: float,
    cycles: float,
    floor: float,
) -> int:
    """Advance or reset the below-floor cycle counter for one recall."""
    if confidence >= floor:
        return 0
    whole = int(math.floor(max(0.0, cycles)))
    if whole < 1:
        return current
    return current + whole


def sustained_floor_demotion_due(
    below_floor_cycles: int,
    *,
    threshold: int | None = None,
) -> bool:
    limit = sustained_floor_cycle_threshold() if threshold is None else max(1, threshold)
    return int(below_floor_cycles) >= limit


def _tick_interval_seconds() -> float:
    try:
        from pressure_loop.policy import pressure_tick_interval_seconds

        value = float(pressure_tick_interval_seconds())
        return value if value > 0 else 5.0
    except Exception:
        return 5.0


def _confidence(doc: dict[str, Any]) -> float:
    provenance = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
    raw = provenance.get("confidence", doc.get("confidence"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _atomic_write_at_path(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _persist_document(doc: dict[str, Any]) -> bool:
    memory_id = str(doc.get("id") or "")
    try:
        from memory.engine.validate_write import atomic_write

        atomic_write(doc, update_index=True)
        return True
    except Exception as exc:
        log.debug("atomic_write failed for %s (%s); fallback path write", memory_id, exc)
    try:
        from memory.engine.walker import path_for_id

        path_str = path_for_id(memory_id)
        if not path_str:
            return False
        _atomic_write_at_path(Path(path_str), doc)
        return True
    except Exception as exc:
        log.warning("sustained floor demotion persist failed for %s: %s", memory_id, exc)
        return False


def apply_recall_sustained_floor_demotion(memory_id: str) -> dict[str, Any]:
    """Demote when confidence stays below floor for sustained pressure cycles."""
    if not memory_id:
        return {"demoted": False, "reason": "missing_id"}
    if not memory_enabled():
        return {"demoted": False, "reason": "memory_disabled", "memory_id": memory_id}

    try:
        from memory.engine.walker import path_for_id

        path_str = path_for_id(memory_id)
        if not path_str:
            return {"demoted": False, "reason": "path_unresolved", "memory_id": memory_id}
        with open(path_str, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            return {"demoted": False, "reason": "invalid_document", "memory_id": memory_id}
    except Exception as exc:
        log.debug("load %s for sustained floor demotion failed: %s", memory_id, exc)
        return {"demoted": False, "reason": "load_failed", "memory_id": memory_id}

    if _is_canonical(doc):
        return {"demoted": False, "reason": "canonical_immune", "memory_id": memory_id}

    from memory.confidence_decay import cycles_elapsed_since
    from memory.strength_index import ensure_strength_index

    si = ensure_strength_index()
    rec = si.get_record(memory_id) or {}
    # NEVER substitute provenance.ingested_at for the access clock — see the identical
    # ownership guard in memory.confidence_decay. With no last_access after bulk
    # registration, access age is unknown: 0 cycles -> no accrual; touch() seeds the
    # access clock for the next recall.
    last_access = rec.get("last_access")

    cycles = cycles_elapsed_since(str(last_access) if last_access else None)
    confidence = _confidence(doc)
    floor = demotion_confidence_floor()
    before_cycles = int(rec.get("below_floor_cycles") or 0)
    after_cycles = si.advance_below_floor_cycles(
        memory_id,
        confidence=confidence,
        cycles=cycles,
        floor=floor,
    )

    result: dict[str, Any] = {
        "demoted": False,
        "memory_id": memory_id,
        "confidence": confidence,
        "floor": floor,
        "cycles": cycles,
        "below_floor_cycles_before": before_cycles,
        "below_floor_cycles_after": after_cycles,
    }

    if not sustained_floor_demotion_due(after_cycles):
        result["reason"] = "below_threshold"
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "sustained_floor_demotion",
                status="skip",
                summary=result["reason"],
                memory_id=memory_id,
                demoted=False,
                **{k: v for k, v in result.items() if k != "demotion"},
            )
        except Exception:
            pass
        return result

    result["trigger_cycles"] = after_cycles
    demotion = demote_one_lifecycle_stage(doc, reason="sustained_confidence_floor")
    result["demotion"] = demotion
    if not demotion.get("demoted"):
        result["reason"] = demotion.get("reason") or "demotion_skipped"
        return result

    persisted = _persist_document(doc)
    si.reset_below_floor_cycles(memory_id)
    result["demoted"] = persisted
    result["reason"] = "sustained_confidence_floor"
    result["persisted"] = persisted
    result["below_floor_cycles_after"] = 0
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "sustained_floor_demotion",
            status="ok" if persisted else "fail",
            summary=result["reason"],
            memory_id=memory_id,
            demoted=persisted,
            confidence=confidence,
            below_floor_cycles=after_cycles,
        )
    except Exception:
        pass
    return result


def demote_one_lifecycle_stage(
    doc: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Drop one lifecycle stage on the memory record (type field)."""

    def _emit_demotion(outcome: dict[str, Any]) -> None:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "lifecycle_demotion",
                summary=str(outcome.get("reason") or ""),
                memory_id=outcome.get("memory_id"),
                demoted=bool(outcome.get("demoted")),
                type_before=outcome.get("type_before") or outcome.get("type"),
                type_after=outcome.get("type_after"),
                reason=outcome.get("reason"),
            )
        except Exception:
            pass

    if _is_canonical(doc):
        outcome = {
            "demoted": False,
            "reason": "canonical_immune",
            "memory_id": doc.get("id"),
        }
        _emit_demotion(outcome)
        return outcome

    current_type = str(doc.get("type") or "concept").strip() or "concept"
    rank = _TYPE_RANK.get(current_type, 1)
    if rank <= 0:
        outcome = {
            "demoted": False,
            "reason": "already_floor",
            "memory_id": doc.get("id"),
            "type": current_type,
        }
        _emit_demotion(outcome)
        return outcome

    new_type = _DEMOTED_TYPE.get(rank, "observation")
    doc["type"] = new_type

    audit = list(doc.get("audit") or [])
    audit.append(
        {
            "when": _now_iso(),
            "what": "lifecycle_demoted",
            "by": "memory.demotion",
            "notes": f"{current_type}->{new_type}; reason={reason}",
        }
    )
    doc["audit"] = audit

    outcome = {
        "demoted": True,
        "memory_id": doc.get("id"),
        "type_before": current_type,
        "type_after": new_type,
        "reason": reason,
    }
    _emit_demotion(outcome)
    return outcome
