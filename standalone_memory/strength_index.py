"""
Strength Index — engram strength tracking with reinforcement and decay.

Inspired by Omega3.0's StrengthIndex, this module tracks per-memory
strength scores that inform ranking but never gate writes. Key properties:

- Strength is observational: low-strength memories are still recallable,
  just ranked lower
- Recall reinforces (reconsolidation): touching a memory boosts its strength
- Decay is lazy: unaccessed memories decay toward a floor, never fully forgotten
- Strength is centered: the neutral point is 0.5, so fresh memories nudge UP
  and decayed ones nudge DOWN in scoring

This is a simplified, dependency-free version of the full Omega3.0 system.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("standalone_memory.strength_index")

# Default configuration
DEFAULT_CONFIG = {
    "decay_per_cycle": 0.005,  # 0.5% decay per cycle for unaccessed memories
    "decay_floor": 0.10,  # Minimum strength floor
    "touch_boost": 0.05,  # Strength boost per touch
    "touch_max": 100,  # Max touch count before boost diminishes
    "write_strength_base": 0.5,  # Base strength at write time
    "canonical_immune": True,  # Canonical memories don't decay
}


class StrengthIndex:
    """Per-memory engram strength tracker.

    Tracks write_strength, touches, and decay for each memory.
    Always accessed via ensure_strength_index(); canonical path
    resolved by the module.

    Thread-safe: all operations are locked.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize the strength index.

        Args:
            path: Path to persist the index to disk. If None, runs in-memory only.
            config: Override default configuration values.
        """
        self._config = {**DEFAULT_CONFIG, **(config or {})}
        self._records: Dict[str, Dict[str, Any]] = {}  # id -> {strength, touches, last_access, ...}
        self._dirty = False
        self._lock = threading.RLock()
        self._path = path

        if path:
            self._load()

    def _load(self) -> None:
        """Load index from disk."""
        if not self._path:
            return
        try:
            p = Path(self._path)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    with self._lock:
                        self._records = data
                        log.debug("Loaded %d strength records from %s", len(self._records), self._path)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Strength index load failed (%s) — starting fresh", exc)

    def _save(self) -> None:
        """Persist index to disk (atomic write)."""
        if not self._path or not self._dirty:
            return
        try:
            p = Path(self._path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            json.dump(self._records, tmp.open("w", encoding="utf-8"), indent=2)
            tmp.replace(p)  # atomic swap
            self._dirty = False
        except OSError as exc:
            log.warning("Strength index save failed (%s)", exc)

    def register(self, memory_id: str, write_strength: float = 0.5) -> None:
        """Register a new memory in the index.

        Called at write time. The memory is tracked from birth.

        Args:
            memory_id: The memory's unique ID
            write_strength: Initial strength (typically from confidence)
        """
        with self._lock:
            if memory_id in self._records:
                # Already registered — update strength if higher
                current = self._records[memory_id]["strength"]
                if write_strength > current:
                    self._records[memory_id]["strength"] = write_strength
                    self._dirty = True
            else:
                self._records[memory_id] = {
                    "strength": max(0.0, min(1.0, write_strength)),
                    "touches": 0,
                    "last_access": None,
                    "registered_at": time.time(),
                }
                self._dirty = True

    def touch(self, memory_id: str) -> float:
        """Touch a memory — reinforces it (reconsolidation).

        Called at recall time for every retrieved memory.

        Args:
            memory_id: The memory's unique ID

        Returns:
            The updated strength value
        """
        with self._lock:
            if memory_id not in self._records:
                return 0.0

            rec = self._records[memory_id]
            rec["touches"] += 1
            rec["last_access"] = time.time()

            # Touch boost: diminishing returns after many touches
            touch_count = rec["touches"]
            if touch_count < self._config["touch_max"]:
                boost = self._config["touch_boost"] * (1.0 - touch_count / self._config["touch_max"])
                rec["strength"] = min(1.0, rec["strength"] + boost)

            self._dirty = True
            return rec["strength"]

    def get_strength(self, memory_id: str) -> float:
        """Get the current strength of a memory.

        Args:
            memory_id: The memory's unique ID

        Returns:
            Strength value [0.0, 1.0], or 0.0 if unknown
        """
        with self._lock:
            rec = self._records.get(memory_id)
            if rec is None:
                return 0.0
            return rec["strength"]

    def get_record(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Get the full strength record for a memory.

        Args:
            memory_id: The memory's unique ID

        Returns:
            Dict with strength, touches, last_access, or None if unknown
        """
        with self._lock:
            return self._records.get(memory_id)

    def decay_tick(self, cycle_id: str = "") -> Dict[str, int]:
        """Apply decay to all unaccessed memories.

        Called periodically (e.g., at DeepSleep or cycle boundaries).
        Decay is lazy: only applied to memories not accessed since last decay.

        Args:
            cycle_id: Optional cycle identifier for logging

        Returns:
            Dict with counts of affected memories
        """
        with self._lock:
            decayed = 0
            floored = 0
            now = time.time()

            for mid, rec in self._records.items():
                # Skip canonical memories
                if self._config.get("canonical_immune") and rec.get("canonical"):
                    continue

                # Only decay if not accessed since last decay tick
                last_access = rec.get("last_access")
                if last_access is not None and last_access >= rec.get("last_decay_tick", 0):
                    continue

                strength = rec["strength"]
                if strength <= self._config["decay_floor"]:
                    floored += 1
                    continue

                # Apply decay
                new_strength = strength * (1.0 - self._config["decay_per_cycle"])
                new_strength = max(new_strength, self._config["decay_floor"])

                if new_strength < strength:
                    rec["strength"] = new_strength
                    rec["last_decay_tick"] = now
                    decayed += 1

            if decayed > 0 or floored > 0:
                self._dirty = True
                log.debug(
                    "Decay tick %s: decayed=%d, floored=%d",
                    cycle_id or "unknown",
                    decayed,
                    floored,
                )

            return {"decayed": decayed, "floored": floored}

    def set_canonical(self, memory_id: str, is_canonical: bool = True) -> None:
        """Mark a memory as canonical (immune to decay).

        Args:
            memory_id: The memory's unique ID
            is_canonical: Whether to mark as canonical
        """
        with self._lock:
            if memory_id in self._records:
                self._records[memory_id]["canonical"] = is_canonical
                self._dirty = True

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics.

        Returns:
            Dict with records count, dirty flag, and strength distribution
        """
        with self._lock:
            if not self._records:
                return {"records": 0, "dirty": False}

            strengths = [rec["strength"] for rec in self._records.values()]
            return {
                "records": len(self._records),
                "dirty": self._dirty,
                "mean_strength": sum(strengths) / len(strengths),
                "min_strength": min(strengths),
                "max_strength": max(strengths),
                "total_touches": sum(rec["touches"] for rec in self._records.values()),
            }

    def clear(self) -> None:
        """Clear all records."""
        with self._lock:
            self._records.clear()
            self._dirty = True

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def __contains__(self, memory_id: str) -> bool:
        with self._lock:
            return memory_id in self._records
