"""cognition_substrate MEDIUM-8 — the strength (and embedding) recall lanes
are wired at the plan COMPILER, and must stay that way.

A posture's `strength_bias` is the operator's declared intent that
StrengthIndex-elevated memories should surface for that posture. The strength
lane only runs when the plan gives it a positive `lane_weight`. That weight is
NOT stored in POSTURE_CATALOG — it is injected by `compile_recall_plan` via
`_apply_posture_lane_bias` (docstring: "Enable dedicated lanes from posture
bias fields (audit MEDIUM-8)"): strength_bias -> strength=1.0, and
knowledge>=1.0 -> embedding=0.5.

These are REGRESSION guards: the cognition_substrate triage initially flagged
the lane as "dead" by inspecting only POSTURE_CATALOG (not the compiler), so the
wiring is easy to mistakenly "clean up". These tests pin it to the live path.
(The vision lane is intentionally NOT wired — no posture declares a vision
bias.)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.recall_posture import POSTURE_CATALOG  # noqa: E402
from memory.recall_lanes import LANE_STRENGTH, LANE_EMBEDDING, run_recall_lanes  # noqa: E402
from memory.recall_compiler import compile_recall_plan, _apply_posture_lane_bias  # noqa: E402


def test_compiler_enables_strength_lane_for_strength_biased_postures():
    """Every posture declaring strength_bias must emerge from the compiler with
    a positive strength-lane weight, or the lane (and the bias) is dead."""
    offenders = [
        name
        for name, p in POSTURE_CATALOG.items()
        if p.strength_bias is not None
        and float(_apply_posture_lane_bias(p.lane_weights, p).get(LANE_STRENGTH, 0.0) or 0.0) <= 0.0
    ]
    assert not offenders, f"compiler did not enable strength lane for: {offenders}"


def test_compiler_enables_embedding_lane_when_knowledge_weighted():
    """Postures that weight the knowledge lane >= 1.0 get the embedding lane
    enabled by the same compiler bias step."""
    offenders = [
        name
        for name, p in POSTURE_CATALOG.items()
        if float(p.lane_weights.get("knowledge", 0.0) or 0.0) >= 1.0
        and float(_apply_posture_lane_bias(p.lane_weights, p).get(LANE_EMBEDDING, 0.0) or 0.0)
        <= 0.0
    ]
    assert not offenders, f"compiler did not enable embedding lane for: {offenders}"


class _FakeQS:
    def __init__(self, text: str, limit: int = 10) -> None:
        self.cycle_id = "test-strength-lane"
        self.text = text
        self.limit = limit
        self.modality = "text"
        self.task_scope = None
        self.recall_policy_id = None
        self.domain = None


_SYN_INDEX = {
    "0.0.aaa": {"id": "0.0.aaa", "coord": [0, 0, 0], "path": "FAKE/identity_aaa.json"},
}
_SYN_META = {
    "FAKE/identity_aaa.json": {
        "id": "0.0.aaa",
        "coord": [0, 0, 0],
        "type": "identity",
        "title": "operator identity",
        "summary": "the operator",
        "text": "Operator identity record.",
        "tags": ["operator"],
        "provenance": {"ingested_at": "2026-04-01T00:00:00Z"},
    },
}


def _fake_load(path):
    return _SYN_META.get(path)


class _FakeStrengthIndex:
    def get_strength(self, mid: str) -> float:
        return 0.9 if mid == "0.0.aaa" else 0.0


def test_strength_lane_runs_end_to_end_for_identity_posture(monkeypatch):
    """End-to-end on the live path: an IDENTITY plan (strength_bias='high')
    surfaces a strength-lane hit for a high-strength memory — proving the
    compiler-injected weight actually reaches the dispatcher."""
    import memory.strength_index as si_mod

    monkeypatch.setattr(si_mod, "ensure_strength_index", lambda: _FakeStrengthIndex())

    plan = compile_recall_plan(_FakeQS("who is the operator"))
    assert float(plan.lane_weights.get(LANE_STRENGTH, 0.0) or 0.0) > 0.0  # compiler wired it

    hits = run_recall_lanes(plan, _SYN_INDEX, _fake_load)
    strength_hits = [h for h in hits if h.lane == LANE_STRENGTH]
    assert strength_hits, "strength lane produced no hits — it was not dispatched"
    assert strength_hits[0].memory_id == "0.0.aaa"
