"""meta_loop — pure sequencer + per-phase callable sub-packages.

Meta A (front): IntentDecision → ExecutionActionPack → AUTO.
Meta B (back): AutoOutcomePack → MetaReflectionPack → Global B.
Deterministic; no models. The episode is the carrier.
"""

from meta_loop.loop import run_meta_a, run_meta_b

__all__ = ["run_meta_a", "run_meta_b"]
