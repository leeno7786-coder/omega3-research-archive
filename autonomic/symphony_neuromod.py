"""Symphony S9 — native neuromod weave (RPE formula + salience latch).

Computes episode body-state from AUTO outcomes + learned success baseline,
latches ambient memory salience for encode-scale/tau modulation, and exposes
pure helpers consumed by pressure_field and cognition_chain.

Public surface:
    compute_episode_neuromod(auto, baseline_success_rate) -> dict
        Pure RPE neuromod from AUTO outcome + learned baseline.

    compute_closed_body_spike(neuromod) -> float
        Cross-tick pressure scalar [0, 1]; NOT success-gated.

    compute_memory_salience(neuromod) -> dict
        Pure encode-scale + tau_factor from neuromod dict.

    set_current_memory_salience(neuromod) / current_memory_salience()
        Ambient latch sampled by memory writes within a cycle.

ARCHITECTURAL RULES:
    - Pure I/O against AUTO envelope dicts + episode carriers. No model calls.
    - Formula constants live HERE (single source of truth).
    - This module MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from guardian.terminal_feedback import emit_data

log = logging.getLogger("omega.autonomic.symphony_neuromod")

# Serotonin proxy: patience rises after aversive (negative dopamine) outcomes.
_SEROTONIN_AVERSIVE_GAIN: float = 1.0

# Memory-salience constants (single source of truth; tune live like DECAY_*).
ENCODE_DOPAMINE_GAIN = 0.6  # reward (positive dopamine) -> stronger encoding
ENCODE_PLASTICITY_GAIN = 0.4  # plastic state -> stronger encoding
STRESS_TAU_PULL = 0.5  # stress shortens retention (tau_factor < 1)
SEROTONIN_TAU_PUSH = 0.5  # serotonin lengthens retention (tau_factor > 1)
TAU_FACTOR_MIN = 0.5
TAU_FACTOR_MAX = 1.5

# Ambient latch — the "current bloodstream hormone level" a memory-write samples.
_CURRENT_MEMORY_SALIENCE: Optional[Dict[str, float]] = None


def _clamp_unit(v: Any) -> float:
    """Coerce to [0, 1] -- NaN/None/garbage -> 0.0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _serotonin_signal(neuromod: Optional[Dict[str, Any]]) -> float:
    """Serotonin patience signal, with legacy aversive-dopamine fallback."""
    if not isinstance(neuromod, dict):
        return 0.0
    if "serotonin" in neuromod:
        return _clamp_unit(neuromod.get("serotonin"))
    dop = _safe_float(neuromod.get("dopamine"))
    return _clamp_unit(max(0.0, -dop) * _SEROTONIN_AVERSIVE_GAIN)


def _emit_neuromod_compute(
    operation: str,
    *,
    dopamine: float = 0.0,
    plasticity: float = 0.0,
    stress: float = 0.0,
    serotonin: float = 0.0,
    strength: float = 0.0,
    spike: float = 0.0,
    scale: float = 0.0,
    tau_factor: float = 0.0,
) -> None:
    try:
        emit_data(
            "autonomic",
            "neuromod_compute",
            operation=operation,
            dopamine=dopamine,
            plasticity=plasticity,
            stress=stress,
            serotonin=serotonin,
            strength=strength,
            spike=spike,
            scale=scale,
            tau_factor=tau_factor,
        )
    except Exception:
        pass


def compute_memory_salience(neuromod: Any) -> Dict[str, float]:
    """Pure: canonical neuromod dict -> {"scale": float, "tau_factor": float}."""
    if not isinstance(neuromod, dict):
        return {"scale": 1.0, "tau_factor": 1.0}
    dop = _safe_float(neuromod.get("dopamine"))
    plast = _clamp_unit(neuromod.get("plasticity"))
    stress = _clamp_unit(neuromod.get("stress"))
    sero = _serotonin_signal(neuromod)
    reward = _clamp_unit(max(0.0, dop))
    scale = 1.0 + ENCODE_DOPAMINE_GAIN * reward + ENCODE_PLASTICITY_GAIN * plast
    tau_factor = 1.0 - STRESS_TAU_PULL * stress + SEROTONIN_TAU_PUSH * sero
    tau_factor = max(TAU_FACTOR_MIN, min(TAU_FACTOR_MAX, tau_factor))
    result = {"scale": float(scale), "tau_factor": float(tau_factor)}
    _emit_neuromod_compute(
        "memory_salience",
        dopamine=dop,
        plasticity=plast,
        stress=stress,
        serotonin=sero,
        scale=result["scale"],
        tau_factor=result["tau_factor"],
    )
    return result


def set_current_memory_salience(neuromod: Any) -> None:
    """Latch ambient salience from the active cycle's neuromod.

    Called from ``cognition_chain._start_auto`` after each AUTO cycle.
    """
    global _CURRENT_MEMORY_SALIENCE
    _CURRENT_MEMORY_SALIENCE = compute_memory_salience(neuromod)


def current_memory_salience() -> Optional[Dict[str, float]]:
    """Return the latched ambient salience, or None before first latch."""
    return _CURRENT_MEMORY_SALIENCE


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Permissive float parse for scalars that may span negative ranges."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp_range(v: Any, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return lo
    if f != f:
        return lo
    if f < lo:
        return lo
    if f > hi:
        return hi
    return f


EXPECTED_MS: float = 4000.0

_NEUROMOD_ZERO_RESULT: Dict[str, Any] = {
    "dopamine": 0.0,
    "plasticity": 0.0,
    "stress": 0.0,
    "serotonin": 0.0,
    "strength": 0.0,
    "cycle_kind": "",
}


def compute_episode_neuromod(
    auto: Any,
    baseline_success_rate: Any,
) -> Dict[str, Any]:
    """Pure RPE neuromod from AUTO outcome + learned success baseline.

    Never raises; garbage inputs yield the all-zero baseline dict.
    """
    try:
        if not isinstance(auto, dict):
            return dict(_NEUROMOD_ZERO_RESULT)

        baseline = float(baseline_success_rate)
        if baseline != baseline:
            return dict(_NEUROMOD_ZERO_RESULT)
        baseline_clamped = _clamp_unit(baseline)

        cycle_kind = auto.get("cycle_kind")
        cycle_kind_str = str(cycle_kind) if cycle_kind is not None else ""
        if cycle_kind == "success":
            v = 1.0
        elif cycle_kind == "fail":
            v = -1.0
        else:
            v = 0.0

        expected = 2.0 * baseline_clamped - 1.0
        rpe = _clamp_range(v - expected, -2.0, 2.0) / 2.0

        runtime_ms: Any = None
        telemetry = auto.get("telemetry")
        if isinstance(telemetry, dict):
            runtime_ms = telemetry.get("runtime_ms")
        if runtime_ms is None:
            runtime_ms = auto.get("runtime_ms")

        overrun = 0.0
        try:
            runtime_val = float(runtime_ms)
            if runtime_val > 0.0:
                overrun = _clamp_unit((runtime_val - EXPECTED_MS) / EXPECTED_MS)
        except (TypeError, ValueError):
            overrun = 0.0

        # Phase 0: the dopamine output clamp is config-driven (dopamine.limits), making the
        # previously-dead limits keys live. Defaults (-1.0, 1.0) match the RPE range, so this is
        # behavior-neutral until the operator narrows the yaml. Read failure-soft — never raises.
        from autonomic.neuromod_config import dopamine_limits

        _dop_min, _dop_max = dopamine_limits()
        dopamine = _clamp_range(rpe, _dop_min, _dop_max)
        plasticity = abs(rpe)
        stress = _clamp_unit(0.6 * max(0.0, -v) + 0.4 * max(0.0, -rpe) + 0.3 * overrun)
        serotonin = _clamp_unit(baseline_clamped - 0.5 * stress)
        strength = abs(rpe)

        result = {
            "dopamine": float(dopamine),
            "plasticity": float(plasticity),
            "stress": float(stress),
            "serotonin": float(serotonin),
            "strength": float(strength),
            "cycle_kind": cycle_kind_str if cycle_kind else "",
        }
        _emit_neuromod_compute(
            "episode_neuromod",
            dopamine=result["dopamine"],
            plasticity=result["plasticity"],
            stress=result["stress"],
            serotonin=result["serotonin"],
            strength=result["strength"],
        )
        return result
    except Exception:
        return dict(_NEUROMOD_ZERO_RESULT)


def compute_closed_body_spike(neuromod: Any) -> float:
    """Cross-tick body-state pressure scalar [0, 1]. NOT success-gated."""
    try:
        if not isinstance(neuromod, dict):
            return 0.0
        stress = _safe_float(neuromod.get("stress"))
        plasticity = _safe_float(neuromod.get("plasticity"))
        spike = _clamp_unit(0.6 * stress + 0.5 * plasticity)
        _emit_neuromod_compute(
            "closed_body_spike",
            stress=stress,
            plasticity=plasticity,
            spike=spike,
        )
        return spike
    except Exception:
        return 0.0
