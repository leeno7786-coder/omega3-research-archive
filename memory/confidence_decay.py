"""Lazy provenance confidence decay applied when a memory is recalled."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from core.memory_config import decay_rules, memory_enabled

log = logging.getLogger("omega.memory.confidence_decay")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _tick_interval_seconds() -> float:
    try:
        from pressure_loop.policy import pressure_tick_interval_seconds

        value = float(pressure_tick_interval_seconds())
        return value if value > 0 else 5.0
    except Exception:
        return 5.0


def cycles_elapsed_since(last_access_iso: str | None) -> float:
    """Whole pressure cycles since ``last_access`` (0 if within one tick)."""
    last = _parse_iso(last_access_iso)
    if last is None:
        return 0.0
    elapsed = max(0.0, (_now_utc() - last).total_seconds())
    tick = _tick_interval_seconds()
    return elapsed / tick


def decay_confidence(
    confidence: float,
    *,
    cycles: float,
    rules: dict[str, Any] | None = None,
) -> tuple[float, bool]:
    """Return decayed confidence and whether it changed."""
    cfg = rules if isinstance(rules, dict) else decay_rules()
    rate = float(cfg.get("confidence_decay_per_cycle") or 0.005)
    floor = float(cfg.get("confidence_floor_no_recall") or 0.10)
    whole_cycles = int(math.floor(max(0.0, cycles)))
    if whole_cycles < 1:
        return confidence, False
    factor = (1.0 - rate) ** whole_cycles
    after = max(floor, confidence * factor)
    changed = after + 1e-9 < confidence
    return after, changed


def _is_canonical(doc: dict[str, Any]) -> bool:
    flags = doc.get("flags") if isinstance(doc.get("flags"), dict) else {}
    return bool(flags.get("canonical") or doc.get("canonical"))


def _confidence(doc: dict[str, Any]) -> float:
    provenance = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
    raw = provenance.get("confidence", doc.get("confidence"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def apply_recall_confidence_decay(memory_id: str) -> dict[str, Any]:
    """Decay provenance confidence for a memory being recalled (lazy, at recall)."""

    def _emit_decay(outcome: dict[str, Any]) -> None:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "apply_recall_confidence_decay",
                summary=f"applied={outcome.get('applied') or outcome.get('persisted')}",
                memory_id=outcome.get("memory_id", memory_id),
                confidence_before=outcome.get("confidence_before") or outcome.get("confidence"),
                confidence_after=outcome.get("confidence_after"),
                applied=bool(outcome.get("applied") or outcome.get("persisted")),
                cycles=outcome.get("cycles"),
            )
        except Exception:
            pass

    def _finish(outcome: dict[str, Any]) -> dict[str, Any]:
        _emit_decay(outcome)
        return outcome

    if not memory_id:
        return _finish({"applied": False, "reason": "missing_id"})
    if not memory_enabled():
        return _finish({"applied": False, "reason": "memory_disabled"})

    rules = decay_rules()
    if rules.get("canonical_immune", True) is False:
        pass  # explicit opt-out only

    try:
        from memory.engine.walker import path_for_id

        path_str = path_for_id(memory_id)
        if not path_str:
            return _finish({"applied": False, "reason": "path_unresolved", "memory_id": memory_id})
        with open(path_str, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            return _finish({"applied": False, "reason": "invalid_document", "memory_id": memory_id})
    except Exception as exc:
        log.debug("load %s for decay failed: %s", memory_id, exc)
        return _finish({"applied": False, "reason": "load_failed", "memory_id": memory_id})

    if rules.get("canonical_immune", True) and _is_canonical(doc):
        return _finish({"applied": False, "reason": "canonical_immune", "memory_id": memory_id})

    from memory.strength_index import ensure_strength_index

    si = ensure_strength_index()
    rec = si.get_record(memory_id) or {}
    # last_access is the clock owned by decay: register() stamps it at write and
    # touch() refreshes it on every recall. It is None when the strength index has
    # never seen this memory recalled — the common case for EVERY pre-existing memory
    # after a reboot, because bulk_register_from_coord_index deliberately stamps
    # last_access=None. provenance.ingested_at is observed intake time, but it answers a
    # different question and must not substitute for unknown access age. Unknown age ->
    # 0 cycles -> no decay;
    # touch() (called after decay in the recall path) seeds a real last_access so the NEXT
    # recall measures true elapsed time.
    last_access = rec.get("last_access")

    cycles = cycles_elapsed_since(str(last_access) if last_access else None)
    before = _confidence(doc)
    after, changed = decay_confidence(before, cycles=cycles, rules=rules)
    if not changed:
        return _finish(
            {
                "applied": False,
                "reason": "no_decay_due",
                "memory_id": memory_id,
                "cycles": cycles,
                "confidence": before,
            }
        )

    # Persist decay on the StrengthIndex only. Mutating provenance.confidence
    # on the canonical file invalidates immutable projection receipts (claim
    # hash includes provenance) and forces false recall conflicts.
    applied = False
    try:
        if si.get_record(memory_id) is None:
            si.register(memory_id, write_strength=before)
        updated = si.set_write_strength(memory_id, after)
        applied = updated is not None
    except Exception as exc:
        log.warning(
            "confidence decay strength update failed for %s: %s", memory_id, exc
        )
        applied = False

    return _finish({
        "applied": applied,
        "memory_id": memory_id,
        "confidence_before": before,
        "confidence_after": after,
        "cycles": cycles,
        "persisted": applied,
        "store": "strength_index",
    })
