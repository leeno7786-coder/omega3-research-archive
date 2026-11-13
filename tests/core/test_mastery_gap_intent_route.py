"""Intent compatibility lock: an admitted mastery_gap maps to LEARN.

Memory maintenance no longer mints mastery pressure.  The mapping remains valid for an
explicit Omega-authored successor that names a mastery gap, and this test keeps that
already-admitted signal from falling through to the IDLE catch-all.

(Caught by the end-to-end map workflow wnfyqqlo0 — the unit tests for the lever/sustain
synthesize mastery_gap and the topic resolver picks the largest gap, but NONE exercised
opened_by=mastery_gap -> intent, the seam where the loop actually broke.)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from global_loop.intent.run import ALLOWED_INTENTS, _map_opened_by_to_intent  # noqa: E402


def test_mastery_gap_routes_to_learn():
    assert _map_opened_by_to_intent("mastery_gap", "") == "LEARN"


def test_mastery_gap_routes_with_the_other_learning_signals():
    # Keep compatibility for already-admitted Omega learning signals.  These names are
    # routing inputs, not authority for deterministic pressure polling.
    for ob in ("learning_debt", "curiosity_gap", "epistemic_emptiness", "mastery_gap"):
        assert _map_opened_by_to_intent(ob, "") == "LEARN"


def test_mastery_gap_not_idle_catchall():
    # the bug was the catch-all swallowing mastery_gap; that catch-all is now IDLE
    # (SELF_INQUIRY was removed from the system 2026-07-09 and collapsed to IDLE).
    # mastery_gap must still escape it and route to LEARN.
    assert _map_opened_by_to_intent("mastery_gap", "") != "IDLE"


def test_self_inquiry_intent_removed():
    # SELF_INQUIRY was removed as a live intent (operator directive 2026-07-09);
    # it must never reappear in the allowed intent set.
    assert "SELF_INQUIRY" not in ALLOWED_INTENTS
