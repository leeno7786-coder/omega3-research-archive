"""Autonomic neuromodulator subsystem.

Four signals modeled on biology:
    - dopamine        — reward prediction error (per-cycle, phasic)
    - plasticity      — three-factor encoding gate (per-cycle)
    - strength_index  — memory engram robustness snapshot (state)
    - stress          — HPA-axis integrator over rolling window (slow)

Signals are observational — they are written to memory and emitted to
telemetry, but they do NOT influence execution. Only the GLOBAL layer
reflects on their trends.
"""
