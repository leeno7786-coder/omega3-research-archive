"""The ASK-OPERATOR LEVER: clarification_debt_delta -> clarification_gap -> ASK_OPERATOR.

The mind needs its OWN pressure to raise when it genuinely needs the operator. Without it, an
ASK_OPERATOR intent_decision had no pressure to ignite — the decision died and no query ever
reached the HUD (operator 2026-06-27). This is the mirror of the mastery_gap learning lever and
the execution_gap build lever: a synthesizable, mind-raised pressure (source="omega") with a clean
intent route. clarification_gap is ALWAYS-addable (no deterministic field reader), so it
synthesizes whenever the mind raises it — like mastery_gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from global_loop.intent.run import _map_opened_by_to_intent  # noqa: E402
from omega_loop.episode.prompts import _ALLOWED_PRESSURE_PARAMS, _filter_pressure  # noqa: E402


def test_pressure_filter_keeps_clarification_delta():
    assert _filter_pressure({"clarification_debt_delta": 0.9}) == {"clarification_debt_delta": 0.9}


def test_clarification_gap_routes_to_ask_operator():
    assert _map_opened_by_to_intent("clarification_gap", "") == "ASK_OPERATOR"
    # the bug was ASK_OPERATOR having no pressure of its own; it must NOT fall to the
    # IDLE catch-all (formerly SELF_INQUIRY, removed 2026-07-09).
    assert _map_opened_by_to_intent("clarification_gap", "") != "IDLE"

