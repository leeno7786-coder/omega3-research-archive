"""Phase 1 — dopamine RPE -> engram reinforcement (the central designed neuromod link).

``autonomic.symphony_neuromod.compute_episode_neuromod`` produces a dopamine reward-prediction
error every cycle, but nothing fed it back into memory: ``StrengthIndex.reinforce`` (the per-choice
plasticity primitive) had no caller in the cognition loop. This module closes that loop.

Credit assignment
    The memories that DROVE a cycle are exactly the ones the recall surfaced — they live on the
    episode at ``episode.auto["memory"]["items"]`` (each item carries a ``memory_id`` and a recall
    ``relevance``). On a *surprising* outcome (|dopamine| past ``dopamine.thresholds``) each of those
    engrams is nudged:

        delta = dopamine * relevance_weight * strength_delta_scale

    A clearly-rewarding cycle (positive RPE) STRENGTHENS them; a clearly-punished cycle (negative
    RPE) WEAKENS them — the operator-approved "strengthen and weaken". Relevance scaling means the
    memories that mattered most to the outcome move most. A *predicted* outcome (RPE inside the
    thresholds) earns no reassignment — only surprise teaches.

Safety
    - Flag-gated: OMEGA_DOPAMINE_REINFORCE (default OFF). With the flag off this is a pure no-op.
    - Failure-soft: every path is guarded; a strength-index hiccup returns a zero summary and never
      breaks the cascade (consistent with the neuromod stamp site it is called from).
    - Conservative: unknown mids are skipped (``reinforce`` never lazy-creates), so credit only ever
      lands on engrams that genuinely exist and were recalled.

Read all tunables through ``autonomic.neuromod_config`` so the previously-dead yaml stays the single
source of policy.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from autonomic.neuromod_config import dopamine_reinforce_scale, dopamine_thresholds

log = logging.getLogger("omega.autonomic.neuromod_reinforce")


def _reinforce_enabled() -> bool:
    """Phase-1 link is OFF until the operator flips OMEGA_DOPAMINE_REINFORCE on."""
    return os.environ.get("OMEGA_DOPAMINE_REINFORCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # NaN -> default


def _relevance_weight(relevance: Any) -> float:
    """Recall relevance as a credit weight in [0, 1]. A missing/garbage relevance means the
    remember tool reported no confidence — the memory was still retrieved and used, so it
    participates fully (weight 1.0) rather than being silently dropped."""
    try:
        w = float(relevance)
    except (TypeError, ValueError):
        return 1.0
    if w != w:  # NaN
        return 1.0
    return max(0.0, min(1.0, w))


def _recalled_items(episode: Any) -> List[dict]:
    """The provenance set: the recalled-memory items the cycle acted on, or [] if none.

    Shape (auto_loop/fold/run.py): episode.auto["memory"]["items"] is a list of dicts each with
    a "memory_id" (str|None) and "relevance" (float|None). Empty whenever recall didn't run."""
    auto = getattr(episode, "auto", None)
    if not isinstance(auto, dict):
        return []
    mem = auto.get("memory")
    if not isinstance(mem, dict):
        return []
    items = mem.get("items")
    return list(items) if isinstance(items, (list, tuple)) else []


def _zero_summary(enabled: bool, dopamine: float = 0.0, reason: str = "") -> Dict[str, Any]:
    return {
        "enabled": enabled,
        "reinforced": 0,
        "skipped": 0,
        "delta_sum": 0.0,
        "dopamine": dopamine,
        "reason": reason,
    }


def _emit_skip(reason: str, *, dopamine: float = 0.0, enabled: bool = True) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "autonomic",
            "dopamine_reinforce_skip",
            status="skip",
            summary=reason,
            reason=reason,
            dopamine=dopamine,
            enabled=enabled,
        )
    except Exception:
        pass


def reinforce_recalled_memories(
    episode: Any,
    neuromod: Any,
    *,
    strength_index: Optional[Any] = None,
) -> Dict[str, Any]:
    """Credit-assign the cycle's dopamine RPE to the engrams the recall surfaced.

    Returns a small summary ({enabled, reinforced, skipped, delta_sum, dopamine, reason}) for
    observability/tests. Never raises. ``strength_index`` is injectable for tests; in production it
    defaults to the wired ``memory.strength_index`` singleton (None before boot wires it -> no-op).
    """
    try:
        if not _reinforce_enabled():
            _emit_skip("flag_off", enabled=False)
            return _zero_summary(False, reason="flag_off")
        if not isinstance(neuromod, dict):
            _emit_skip("no_neuromod")
            return _zero_summary(True, reason="no_neuromod")

        dopamine = _safe_float(neuromod.get("dopamine"))
        pos, neg = dopamine_thresholds()
        # Only a *surprising* outcome teaches — a predicted one leaves engrams untouched.
        if neg < dopamine < pos:
            _emit_skip("predicted", dopamine=dopamine)
            return _zero_summary(True, dopamine=dopamine, reason="predicted")

        items = _recalled_items(episode)
        if not items:
            _emit_skip("no_recall", dopamine=dopamine)
            return _zero_summary(True, dopamine=dopamine, reason="no_recall")

        si = strength_index
        if si is None:
            from memory.strength_index import get_strength_index

            si = get_strength_index()
        if si is None:
            _emit_skip("no_strength_index", dopamine=dopamine)
            return _zero_summary(True, dopamine=dopamine, reason="no_strength_index")

        scale = dopamine_reinforce_scale()
        reinforced = 0
        skipped = 0
        delta_sum = 0.0
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            mid = item.get("memory_id")
            if not mid:
                skipped += 1
                continue
            delta = dopamine * _relevance_weight(item.get("relevance")) * scale
            if delta == 0.0:
                skipped += 1
                continue
            try:
                si.reinforce(mid, delta)
                reinforced += 1
                delta_sum += delta
            except Exception:
                # One bad engram must not abort credit for the rest, nor the cascade.
                skipped += 1

        _emit(
            episode, dopamine=dopamine, reinforced=reinforced, skipped=skipped, delta_sum=delta_sum
        )
        return {
            "enabled": True,
            "reinforced": reinforced,
            "skipped": skipped,
            "delta_sum": delta_sum,
            "dopamine": dopamine,
            "reason": "applied",
        }
    except Exception:
        # Failure-soft: the reinforce link is never allowed to break the neuromod stamp.
        _emit_skip("error", enabled=False)
        return _zero_summary(False, reason="error")


def _emit(episode: Any, **fields: Any) -> None:
    """Failure-soft observability for the reinforce pass."""
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "autonomic",
            "dopamine_reinforce",
            episode_id=str(getattr(episode, "episode_id", "") or ""),
            **fields,
        )
    except Exception:
        pass
