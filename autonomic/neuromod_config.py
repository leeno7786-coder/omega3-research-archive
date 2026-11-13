"""Failure-soft, cached access to the live autonomic dopamine config.

The neuromod formula (``autonomic/symphony_neuromod.py``) reads its config
THROUGH this module. The raw loader schema-validates and RAISES on a
missing/malformed yaml — but the neuromod formula must NEVER raise (a bad config has to degrade to the
documented default, not zero out cognition). These accessors wrap the loaders, return the inner config
section, and fall back to ``{}`` (then per-key documented defaults) on ANY error.

This is the single read-path the phased neuromod wiring uses to make the previously-dead config live:
- ``dopamine.limits`` -> the dopamine output clamp (behavior-neutral; defaults match).
- ``dopamine.thresholds`` and ``dopamine.reinforce`` -> bounded surprise/reinforcement paths.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


def _section(loader, key: str) -> Dict[str, Any]:
    """Inner config section (e.g. doc['dopamine']) or {} on any loader/parse error."""
    try:
        doc = loader()
    except Exception:
        return {}
    if isinstance(doc, dict):
        sec = doc.get(key)
        if isinstance(sec, dict):
            return sec
    return {}


def dopamine_cfg() -> Dict[str, Any]:
    from config.loader import load_dopamine_config

    return _section(load_dopamine_config, "dopamine")


def _num(cfg: Dict[str, Any], path: str, default: float) -> float:
    """Walk a dotted path in a config section; coerce to float; any miss/garbage -> default."""
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return float(default)
        cur = cur[part]
    try:
        f = float(cur)
    except (TypeError, ValueError):
        return float(default)
    return f if f == f else float(default)  # NaN -> default


# --- Typed getters (documented defaults == the symphony_neuromod constants today) -------------


def dopamine_limits() -> Tuple[float, float]:
    """(min, max) clamp for the dopamine output. Default (-1.0, 1.0) — matches the RPE range."""
    cfg = dopamine_cfg()
    return (_num(cfg, "limits.min", -1.0), _num(cfg, "limits.max", 1.0))


def dopamine_thresholds() -> Tuple[float, float]:
    """(positive, negative) RPE gate. Outside [negative, positive] the cycle is a
    "clearly rewarding" / "clearly punished" surprise; inside it the outcome was
    predicted and earns no credit reassignment. Default (0.20, -0.20) — matches
    dopamine.yaml. Phase 1 reinforce only credits engrams on a gated (surprising) RPE."""
    cfg = dopamine_cfg()
    return (_num(cfg, "thresholds.positive", 0.20), _num(cfg, "thresholds.negative", -0.20))


def dopamine_reinforce_scale() -> float:
    """Scale mapping a dopamine RPE to a write_strength delta in reinforce(). A maximal
    RPE (|dopamine| == 1) on a fully-relevant engram shifts its write_strength by this
    much. Default 0.10 — modest per-cycle plasticity that accumulates over repeats."""
    return _num(dopamine_cfg(), "reinforce.strength_delta_scale", 0.10)
