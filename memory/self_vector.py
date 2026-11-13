"""
Current Self-Vector — derived state for identity drift detection
==================================================================

The "current self-vector" is the system's present self-model
representation in L0 vector space. It is **derived state** computed
from recent identity-lobe memory writes; it is NOT a memory record
in its own right.

Storage: sidecar JSON at ``guardian/state/identity/current_self_vector.json``.
Recomputed on every identity-lobe write (coord[0] == 0) as part of
the atomic L0 encoding (step 4 of `project_memory_api_atomic_encoding`).

The former dense drift consumer has been retired. This sidecar remains only as
historical forensic state until the separate identity-panel cleanup removes it.

Why a sidecar (not a memory record):
- Self-vector is COMPUTED FROM the memory system. Storing it as a
  memory item would create a recursion (its own write would trigger
  another self-vector update).
- It is recomputable from the identity lobe at any time, so loss is
  recoverable. Sidecar is therefore safe to keep separate.
- Honors "the logic embraces the memory" — the source-of-truth
  identity memories live in the cortex; this is just a cached
  derivation for fast comparison.

Update strategy: weighted mean of the last K identity-vectors,
weighted by confidence. Default K=20 (configurable later if needed).
The mean is L2-normalized so it lives on the unit hypersphere with
the inputs (consistent cosine geometry).

ARCHITECTURAL RULES:
    - Atomic write: temp file + fsync + rename (same pattern as
      memory.engine.validate_write).
    - Never crashes the caller. On any failure, returns False; the
      encoding step decides whether to roll back.
    - History is appended to a separate jsonl trail so drift
      detection can compare against past selves, not just the
      single most recent vector.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.memory.self_vector")


# Per `project_guardian_sole_writer_doctrine` (2026-05-08): identity
# self-vector is load-bearing state across boots (drift tracking,
# coherence calc) — lives under guardian/state/, not .omega_data/.
_IDENTITY_DIR = OMEGA_ROOT / "guardian" / "state" / "identity"
CURRENT_PATH = _IDENTITY_DIR / "current_self_vector.json"
HISTORY_PATH = _IDENTITY_DIR / "self_vector_history.jsonl"

DEFAULT_WINDOW = 20  # how many recent identity vectors feed the mean
_lock = threading.Lock()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(v: List[float]) -> List[float]:
    """L2-normalize. Returns the input unchanged if zero-norm."""
    s = sum(x * x for x in v) ** 0.5
    if s == 0.0:
        return list(v)
    return [x / s for x in v]


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        return True
    except OSError as exc:
        log.warning("self_vector: atomic write to %s failed: %s", path.name, exc)
        return False


def update_self_vector(
    new_identity_vec: Sequence[float],
    *,
    source_memory_id: str,
    confidence: float = 0.5,
    window: int = DEFAULT_WINDOW,
) -> bool:
    """Recompute the current self-vector after an identity-lobe write.

    Append the new vector + confidence to the history; recompute the
    weighted-mean over the trailing `window` history entries; write
    the result atomically to CURRENT_PATH.

    Returns True on durable update, False on any I/O failure.
    """
    if not new_identity_vec:
        log.debug("self_vector: empty input, skipping update")
        return False

    vec = list(new_identity_vec)
    conf = float(max(0.0, min(1.0, confidence)))

    with _lock:
        # 1. Append to history (durable trail of past selves).
        history_entry = {
            "vector": vec,
            "confidence": conf,
            "source_memory_id": source_memory_id,
            "stamped_at": _iso(),
        }
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(history_entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            log.warning("self_vector: history append failed: %s", exc)
            return False

        # P3-S28 HUD live-feed doctrine v3 — publish to RECENT_SELF_VECTORS
        # SharedState ring AFTER the disk write lands. HUD's
        # _telemetry_omega + panel_identity read from the ring instead
        # of tail-reading self_vector_history.jsonl. Doctrine: audit log
        # is source of truth, ring is live feed.
        try:
            from core.shared_state_keys import RECENT_SELF_VECTORS
            from pressure_loop.runtime import get_default_runtime

            ss = get_default_runtime().shared_state
            existing = ss.read(RECENT_SELF_VECTORS) or []
            if not isinstance(existing, list):
                existing = []
            existing.append(history_entry)
            ss.write(RECENT_SELF_VECTORS, existing[-20:])
        except Exception as exc:
            log.debug(
                "self_vector ring publish failed (non-fatal): %s",
                exc,
            )

        # 2. Recompute the current self-vector from the trailing window.
        recent = _read_history_tail(window)
        if not recent:
            log.warning("self_vector: history empty after append (impossible)")
            return False

        # Confidence-weighted mean of normalized vectors.
        dim = len(recent[0]["vector"])
        accum = [0.0] * dim
        weight_total = 0.0
        for entry in recent:
            v = entry.get("vector") or []
            if len(v) != dim:
                continue
            w = float(entry.get("confidence", 0.5))
            nv = _normalize(v)
            for i in range(dim):
                accum[i] += nv[i] * w
            weight_total += w
        if weight_total <= 0.0:
            log.warning("self_vector: zero total weight — cannot mean")
            return False
        mean = [x / weight_total for x in accum]
        current = _normalize(mean)

        ok = _atomic_write_json(
            CURRENT_PATH,
            {
                "vector": current,
                "computed_at": _iso(),
                "window_size": len(recent),
                "source_memory_id": source_memory_id,  # most recent source
                "model": "embed-gemma:300m",
            },
        )
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "self_vector",
                summary=f"ok={ok} window={len(recent)}",
                source_memory_id=source_memory_id,
                window_size=len(recent),
                ok=ok,
            )
        except Exception:
            pass
        return ok


def _read_history_tail(window: int) -> List[Dict[str, Any]]:
    """Read the last `window` entries from HISTORY_PATH. Cheap for the
    expected sizes (identity writes are sparse — well under 10K entries
    over a typical session)."""
    if not HISTORY_PATH.exists() or window <= 0:
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        log.warning("self_vector: history read failed: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-window:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_current_self_vector() -> Optional[Dict[str, Any]]:
    """Read CURRENT_PATH and return its payload, or None if missing /
    unreadable. Used by Omega producer + drift detection."""
    if not CURRENT_PATH.exists():
        return None
    try:
        with open(CURRENT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("self_vector: read CURRENT failed: %s", exc)
        return None


def read_self_vector_history(limit: int = 100) -> List[Dict[str, Any]]:
    """Read up to `limit` most-recent history entries (newest last).
    Used by drift detection to compare current against historical."""
    return _read_history_tail(limit)
