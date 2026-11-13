"""
Recent-Cycle History Reader
===========================

Shared helper used by the autonomic modules to read recent autonomous-loop
cycle events from the temporal lobe (lobe 9). The signals stamped on each
cycle record (dopamine, plasticity, strength, stress) are stored in the
memory's `score` block, so downstream readers can pull structured values
without parsing text bodies.

Used by:
    - dopamine module: per-domain expectation baseline
    - stress module: rolling integration of recent pressure
    - plasticity module: detection of repeated failure / chronic state

This helper is the only place autonomic modules touch the memory layer
directly. Each module receives lists of structured cycle records and
operates on them — no inline file reads in the modules themselves.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from memory.gates.lobe_gates import TemporalGate

log = logging.getLogger("omega.autonomic._history")

_temporal_gate = TemporalGate()


def _cycle_record_from_episode(mem: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Synthesize a cycle-event record (with the structured ``score`` block the
    autonomic extractors read) from a live ``type="episode"`` temporal record.

    write_episode_to_temporal stores the episode body as JSON in ``meta["text"]``
    with FLAT neuromod signals and no score block, while the extractors expect a
    nested ``score.dopamine/plasticity/stress``. Bridge the two here so the dopamine
    baseline (and stress/plasticity history) read live episodes instead of an empty
    list. Returns None when the record carries no usable neuromod signal.
    """
    if not isinstance(mem, dict) or str(mem.get("type") or "") != "episode":
        return None
    text = mem.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    neuromod = body.get("neuromod")
    if not isinstance(neuromod, dict) or not neuromod:
        return None

    domain = (
        (
            (body.get("topic_history") or {}) if isinstance(body.get("topic_history"), dict) else {}
        ).get("domain")
        or (
            (
                ((body.get("execution") or {}).get("execution_action_pack") or {}).get(
                    "execution_inputs"
                )
                or {}
            )
            if isinstance(body.get("execution"), dict)
            else {}
        ).get("domain")
        or ""
    )

    score: Dict[str, Any] = {
        "kind": "cycle_event",
        "domain": domain,
        "cycle_kind": neuromod.get("cycle_kind"),
    }
    dopamine = neuromod.get("dopamine")
    if isinstance(dopamine, (int, float)):
        # Both keys: _dopamine_avg_by_domain reads score.dopamine.score; the
        # extract_dopamine_outcomes fallback reads persisted_outcome.
        score["dopamine"] = {"score": float(dopamine), "persisted_outcome": float(dopamine)}
    plasticity = neuromod.get("plasticity")
    if plasticity is None:
        plasticity = neuromod.get("strength")
    if isinstance(plasticity, (int, float)):
        score["plasticity"] = {"strength": float(plasticity)}
    stress = neuromod.get("stress")
    if isinstance(stress, (int, float)):
        score["stress"] = {"level": float(stress)}

    return {
        "id": mem.get("id"),
        "domain": domain,
        "score": score,
        "type": "episode",
        "tags": mem.get("tags") or [],
    }


def recent_cycles(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the last `limit` cycle event records, newest first.

    Each entry is the full memory dict; callers extract signals from
    `entry["score"]` and metadata fields like `entry.get("tags")`.
    Records without a `score` block (legacy or non-cycle entries) are
    skipped — they cannot contribute signal history.
    """
    try:
        all_recent = _temporal_gate.recall_recent(limit=limit, lobe=9)
    except Exception as exc:
        log.warning("Failed to read recent cycles: %s", exc)
        try:
            from guardian.terminal_feedback import emit_data, emit_guardian_event

            emit_data(
                "autonomic",
                "history_read",
                operation="recent_cycles",
                ok=False,
                error=str(exc),
                limit=limit,
            )
            emit_guardian_event(
                "omega.autonomic",
                "history_read",
                operation="recent_cycles",
                ok=False,
                limit=limit,
            )
        except Exception:
            pass
        return []

    cycles = []
    for mem in all_recent:
        if not isinstance(mem, dict):
            continue
        score = mem.get("score")
        if isinstance(score, dict) and score.get("kind") == "cycle_event":
            cycles.append(mem)  # legacy/direct cycle_event record
            continue
        # Live shape: a type="episode" record — synthesize a cycle_event from its
        # body so the signal extractors keep working (I4). Records that yield no
        # usable neuromod signal are skipped, as before.
        synth = _cycle_record_from_episode(mem)
        if synth is not None:
            cycles.append(synth)
    try:
        from guardian.terminal_feedback import emit_data, emit_guardian_event

        emit_data(
            "autonomic",
            "history_read",
            operation="recent_cycles",
            ok=True,
            limit=limit,
            count=len(cycles),
        )
        emit_guardian_event(
            "omega.autonomic",
            "history_read",
            operation="recent_cycles",
            ok=True,
            limit=limit,
            count=len(cycles),
        )
    except Exception:
        pass
    return cycles


def extract_dopamine_outcomes(
    cycles: List[Dict[str, Any]],
    weights: Optional[Dict[str, float]] = None,
) -> List[float]:
    """Pull absolute dopamine outcomes from cycle records.

    New records persist `score.dopamine.persisted_outcome`; older records
    may carry only `outcome`. Legacy component-only records can be
    reconstructed from `components` plus current weights. RPE `score` is
    deliberately not used as an outcome fallback.
    """
    outcomes: List[float] = []
    for c in cycles:
        s = c.get("score", {})
        d = s.get("dopamine") if isinstance(s, dict) else None
        if not isinstance(d, dict):
            continue

        outcome = d.get("persisted_outcome")
        if isinstance(outcome, (int, float)):
            outcomes.append(float(outcome))
            continue

        outcome = d.get("outcome")
        if isinstance(outcome, (int, float)):
            outcomes.append(float(outcome))
            continue

        components = d.get("components")
        if isinstance(components, dict) and weights:
            total = 0.0
            saw_component = False
            for name, weight in weights.items():
                value = components.get(name)
                if isinstance(value, (int, float)):
                    total += float(value) * float(weight)
                    saw_component = True
            if saw_component:
                outcomes.append(total)
                continue

    return outcomes


def consecutive_failures(cycles: List[Dict[str, Any]]) -> int:
    """Count the run of consecutive failure cycles starting from the most
    recent (cycles[0]). Neutral kinds (skipped, idle) are transparent."""
    n = 0
    for c in cycles:
        s = c.get("score", {})
        kind = s.get("cycle_kind") if isinstance(s, dict) else None
        if kind == "success":
            break
        if kind in ("skipped", "idle"):
            continue
        if kind == "fail":
            n += 1
        else:
            break
    return n
