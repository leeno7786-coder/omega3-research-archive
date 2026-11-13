"""Always-on advisory pressure from recent non-verified recall verdicts.

Unlike ``taxonomy_pressure_contributor`` (flag-gated and graph-heavy), this seam
only reads ``memory/recall_verifier/verdict_log.jsonl``. It never manufactures
failure-cluster pressure from the compiled graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pressure_loop.contributors import Contributor, make_contributor
from pressure_loop.taxonomy_pressure import _SIGNAL_PRIORITY, _SIGNAL_WEIGHTS, _WEIGHT_CAP


def recall_verdict_pressure_contributor(root: Optional[Path | str] = None) -> Optional[Contributor]:
    try:
        from memory.taxonomy.pressure_signals import analyze_recall_verdict_pressure

        result = analyze_recall_verdict_pressure(root=root)
    except Exception:
        return None

    recommended = str(result.get("recommended_pressure") or "")
    signals = list(result.get("signals") or [])
    if not recommended or not signals:
        return None

    signals.sort(
        key=lambda s: (
            _SIGNAL_PRIORITY.get(str(s.get("signal") or ""), 99),
            str(s.get("signal") or ""),
        )
    )
    weight = min(
        _WEIGHT_CAP,
        max((_SIGNAL_WEIGHTS.get(str(s.get("signal")), 0.25) for s in signals), default=0.0),
    )
    if weight <= 0.0:
        return None

    metadata = {
        "signals": [str(s.get("signal")) for s in signals],
        "evidence_event_ids": list(result.get("evidence_event_ids") or []),
        "snapshot_hash": str(result.get("snapshot_hash") or ""),
        "recommended_pressure": recommended,
        "source": "recall_verdict_log",
    }
    return make_contributor("recall_verdict", recommended, round(weight, 4), metadata)
