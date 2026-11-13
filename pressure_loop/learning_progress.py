"""Learning-progress plateau signal — Step 5 of the metacognitive pressure evolution.

(docs/architecture/metacognitive-pressure-evolution.md §5 inversion #3, §6.1 science,
§7 Step 5.)

Steps 2-4 already let Omega WRITE ``application_debt_delta`` (it bleeds learning→
application by MODEL JUDGMENT). Step 5 GROUNDS that bleed in a MEASURED learning-
progress signal so it is principled, not whim: it reads the per-domain improvement
trace (``MasteryRecord.recent_knowledge_deltas``) and reports whether learning has
PLATEAUED (recent ``knowledge_delta → ~0`` — §6.1 "learning progress = decrease in
prediction error over time", which abandons both mastered and unlearnable material).

DeepSleep's mastery pass keeps the delta buffer (``append_delta``), reads the plateau
(``plateau_from``) into each record's ``last_plateau_*`` fields and emits it as telemetry
(``emit_plateau``). A per-episode prompt advisory once consumed a projection of this signal;
it rendered only into a prompt half no live lane sent and was retired (2026-09-21), taking
its ``OMEGA_HANDOFF_ENABLED`` flag and the ``plateau_signal`` projection with it.

v1 is a simple, testable heuristic (the doc's permitted "N≈3 successes", expressed as
the §6.1 improvement plateau) whose state shape (the delta ring + evidence_count as
n_i) is exactly what the grounded model — ``r = C1·exp(-C2·‖Er(t)-Er_σ‖²)`` — reads,
so it swaps in cleanly.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Sequence, Tuple

# v1 plateau heuristic (operator-tunable; surfaced in telemetry). K_MIN = the doc's
# "N≈3 successes"; EPSILON = the |knowledge_delta| below which improvement is "~0".
K_MIN: int = 3
EPSILON: float = 0.02
DELTA_CAP: int = 5  # ring-buffer length on MasteryRecord.recent_knowledge_deltas

# §6.1 decay terms (recorded for the grounded model; not yet gating v1):
# novelty D_i = m + k·n^π (empirical π≈-0.791); uncertainty σ_i = S/√n.
NOVELTY_M: float = 0.0
NOVELTY_K: float = 1.0
NOVELTY_PI: float = -0.791
UNCERTAINTY_S: float = 1.0


def append_delta(buffer: Sequence[float], value: float, cap: int = DELTA_CAP) -> Tuple[float, ...]:
    """Append a rounded delta to a bounded oldest-evicted ring buffer (pure)."""
    try:
        out = tuple(float(x) for x in (buffer or ())) + (round(float(value), 4),)
    except (TypeError, ValueError):
        out = tuple(float(x) for x in (buffer or ()))
    if cap > 0 and len(out) > cap:
        out = out[-cap:]
    return out


def plateau_from(deltas: Sequence[float], sample_count: int) -> Dict[str, Any]:
    """The learning-progress facts from a per-domain improvement trace (pure, total).

    plateau (v1): at least K_MIN samples AND the last K_MIN |knowledge_delta| all below
    EPSILON — recent improvement has flattened (§6.1). knowledge_improving: the latest
    delta is still clearly positive. Plus the §6.1 decay scalars from the sample count.
    """
    try:
        ds = [float(x) for x in (deltas or ())]
    except (TypeError, ValueError):
        ds = []
    try:
        n = max(0, int(sample_count or 0))
    except (TypeError, ValueError):
        n = 0
    recent = ds[-K_MIN:]
    plateau = len(recent) >= K_MIN and all(abs(d) < EPSILON for d in recent)
    knowledge_improving = bool(ds) and ds[-1] > EPSILON
    mean_abs = (sum(abs(d) for d in recent) / len(recent)) if recent else 0.0
    novelty_decay = (NOVELTY_M + NOVELTY_K * (n**NOVELTY_PI)) if n > 0 else 1.0
    uncertainty = (UNCERTAINTY_S / math.sqrt(n)) if n > 0 else 1.0
    return {
        "plateau": bool(plateau),
        "knowledge_improving": bool(knowledge_improving),
        "sample_count": n,
        "recent_mean_abs_delta": round(mean_abs, 4),
        "novelty_decay": round(novelty_decay, 4),
        "uncertainty": round(uncertainty, 4),
    }


def emit_plateau(domain: str, episode_id: str, facts: Dict[str, Any]) -> None:
    """Failure-soft telemetry — the plateau signal is observable before it is consumed."""
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "learning_progress",
            "plateau",
            episode_id=str(episode_id or ""),
            summary=(
                f"domain={domain or '(none)'} plateau={facts.get('plateau')} "
                f"improving={facts.get('knowledge_improving')} n={facts.get('sample_count')}"
            ),
            domain=str(domain or ""),
            plateau=bool(facts.get("plateau")),
            knowledge_improving=bool(facts.get("knowledge_improving")),
            sample_count=int(facts.get("sample_count", 0)),
        )
    except Exception:
        pass
