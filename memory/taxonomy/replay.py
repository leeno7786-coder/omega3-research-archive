"""Phase 7O — deterministic replay.

``replay_memory_graph()`` recompiles the graph from the append-only ledger and reports
whether the rebuilt snapshot hash matches the one stored by the previous compile. A match
proves the graph is up-to-date AND the compile is deterministic; a mismatch is a
DIVERGENCE — the ledger grew since the last compile, or (the thing we are guarding against)
a non-deterministic rebuild. The graph is disposable, so replay also leaves it freshly
rebuilt + the manifest current.

Model-free, Guardian-free. Reports, never crashes the caller (a compile error is captured).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from memory.taxonomy import compiler as Cmp
from memory.taxonomy import paths as P


def _stored_snapshot_hash(root: Optional[Path | str]) -> str:
    f = P.compiled_root(root) / "latest_snapshot_hash.txt"
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


def replay_memory_graph(root: Optional[Path | str] = None) -> dict:
    """Recompile from the ledger and report match / divergence vs the stored snapshot."""
    previous = _stored_snapshot_hash(root)  # read BEFORE the recompile overwrites the manifest
    try:
        snap = Cmp.compile_memory_graph(root=root)
    except Exception as exc:  # noqa: BLE001 — replay reports, never raises into the caller
        return {
            "ok": False,
            "previous_snapshot_hash": previous,
            "rebuilt_snapshot_hash": "",
            "event_count": 0,
            "node_count": 0,
            "edge_count": 0,
            "errors": [f"compile_failed: {type(exc).__name__}: {exc}"],
        }

    rebuilt = snap.snapshot_hash
    errors: list[str] = []
    # A first-ever compile (no stored hash) is not a divergence — there was nothing to match.
    if previous and previous != rebuilt:
        errors.append(f"snapshot diverged: stored {previous} != rebuilt {rebuilt}")

    return {
        "ok": not errors,
        "previous_snapshot_hash": previous,
        "rebuilt_snapshot_hash": rebuilt,
        "event_count": snap.event_count,
        "node_count": snap.node_count,
        "edge_count": snap.edge_count,
        "errors": errors,
    }
