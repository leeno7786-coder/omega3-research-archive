"""
Shared State — the Orchestrator's data passing surface
========================================================

Per `project_brainloop_4_or_5_phase_doctrine.md`:

> *Once a loop completes its sequence, it takes the final data, emits
> it to orchestrator (shared state), which then initiates the next
> system. The next system takes the data from shared state to run its
> course with the data and so on until the cycle is complete.*

SharedState is the data container. Loops read from it (to consume what
the prior loop produced) and write to it (to publish their own output).
The Orchestrator owns one instance and passes it into every loop runner
it invokes.

ARCHITECTURAL RULES:
  - Thread-safe. Writes from multiple loops + reads from anywhere are
    safe to interleave.
  - Snapshots are independent — mutating a snapshot does NOT affect
    the live SharedState (callers can't accidentally corrupt state).
  - No coupling to schemas. The state is a free-form key→value map;
    keys are loop-defined contracts.
  - This module MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

import threading
from typing import Any, Dict
import logging

log = logging.getLogger("omega.core.shared_state")


def _emit_shared_state(op: str, **fields: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("runtime", f"shared_state_{op}", **fields)
    except Exception:
        pass


def _value_summary(value: Any) -> str:
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}[{len(value)}]"
    return type(value).__name__


class SharedState:
    """The Orchestrator's shared state container.

    Loops read from / write to a single instance held by the Orchestrator.
    Thread-safe. Snapshots are independent (deep copies under the lock).
    """

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def write(self, key: str, value: Any) -> None:
        """Set `key` to `value`. Overwrites any prior value.

        This surface is permissive — any key, any value. For
        canonical-key writes (the inter-loop wire), use
        `write_canonical(key, value)` which validates against the
        SLICE-2 registry.
        """
        with self._lock:
            self._data[key] = value
        if not str(key).startswith("_"):
            _emit_shared_state(
                "write",
                summary=f"key={key}",
                key=key,
                value_type=_value_summary(value),
            )

    def write_canonical(self, key: str, value: Any) -> None:
        """Strict-shape write for canonical inter-loop keys.

        The key MUST be in `core.shared_state_keys.KEY_REGISTRY` and
        the value MUST pass that key's shape validator. Otherwise
        raises (loud failure mode — silent shape drift on the inter-
        loop wire would be the worst-case bug).

        Raises:
            KeyError: if `key` is not a canonical key in the registry.
            ValueError: if `value` fails the key's shape validator.
        """
        # Lazy import to avoid an import cycle at module load
        # (shared_state_keys.py is doctrine; shared_state.py is mechanism).
        from core.shared_state_keys import KEY_REGISTRY, validate_write

        if key not in KEY_REGISTRY:
            raise KeyError(
                f"unknown canonical key {key!r}; "
                f"only {sorted(KEY_REGISTRY.keys())} are registered. "
                f"Use SharedState.write(...) for ad-hoc / test writes."
            )
        err = validate_write(key, value)
        if err is not None:
            raise ValueError(f"canonical key {key!r} write rejected: {err}")
        with self._lock:
            self._data[key] = value
        _emit_shared_state(
            "write_canonical",
            summary=f"key={key}",
            key=key,
            value_type=_value_summary(value),
        )

    def read(self, key: str, default: Any = None) -> Any:
        """Return value for `key`, or `default` if missing."""
        with self._lock:
            return self._data.get(key, default)

    def discard(self, key: str) -> bool:
        """Remove `key` when present and report whether it existed.

        This is intentionally separate from `write_canonical(key, None)`:
        canonical validators describe valid payload shapes, while removing
        a stale projection is a state-container operation.
        """
        with self._lock:
            existed = key in self._data
            self._data.pop(key, None)
        _emit_shared_state(
            "discard",
            summary=f"key={key} existed={existed}",
            key=key,
            existed=existed,
        )
        return existed

    def snapshot(self) -> Dict[str, Any]:
        """Return a deep copy under the lock.

        Loop consumers can safely mutate nested snapshot values without
        accidentally changing the live shared state.
        """
        import copy

        with self._lock:
            return {key: copy.deepcopy(value) for key, value in self._data.items()}

    def clear(self) -> None:
        """Drop every key. Used between cycles when the Orchestrator
        wants a fresh state.

        ┌─ FINAL VERDICT LAW Step 5 — CONTRACTUAL ORDERING ────────────┐
        │ This method currently has ZERO production callers, BY        │
        │ CONTRACT, not by accident. DeepSleep MUST snapshot+persist   │
        │ the cure's causal-evidence slots (LAST_META_CYCLE /          │
        │ LAST_CURIOSITY_CYCLE + the rest of                           │
        │ deepsleep_engine._UPSTREAM_SLOTS) BEFORE any clear() — see    │
        │ deepsleep_engine.py:440 + boot.py epoch order. If a future   │
        │ Orchestrator/SLICE wires a between-cycle clear(), it MUST be  │
        │ sequenced strictly AFTER the deepsleep consolidation pass in  │
        │ the epoch, or the goal-mortality cure's disposition-truth     │
        │ evaporates silently (the named disease mechanism). This       │
        │ invariant is enforced by                                     │
        │ tests/core/test_audit_persist_before_clear_contract.py —     │
        │ introducing a caller WILL turn it red; handle Step 5 first.   │
        └──────────────────────────────────────────────────────────────┘
        """
        with self._lock:
            self._data.clear()

    def merge(self, updates: Dict[str, Any]) -> None:
        """Bulk-write multiple keys. Equivalent to calling `write` for
        each key but holds the lock once."""
        if not updates:
            return
        with self._lock:
            self._data.update(updates)
        _emit_shared_state(
            "merge",
            summary=f"keys={list(updates.keys())}",
            keys=list(updates.keys()),
            count=len(updates),
        )

    def has(self, key: str) -> bool:
        """Return True if `key` is present (distinct from value=None)."""
        with self._lock:
            return key in self._data

    def __contains__(self, key: str) -> bool:
        return self.has(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __repr__(self) -> str:
        with self._lock:
            return f"SharedState(keys={list(self._data.keys())})"
