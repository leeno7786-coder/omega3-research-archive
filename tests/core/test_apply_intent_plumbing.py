"""Slice 1 of the cognition+CLI merge — APPLY threads through the cascade plumbing.

Covers the intent vocabulary, Global A's opened_by->intent routing, and the Meta A
intent->action map. No dispatch yet (that is Slice 2); these tests pin only that an
APPLY episode is *recognized and routed*, never that it executes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pressure_loop.contributors import ALLOWED_INTENTS, from_operator_message  # noqa: E402
from global_loop.intent.run import _map_opened_by_to_intent  # noqa: E402
from meta_loop.action.run import META_ACTIONS, _intent_to_action_map  # noqa: E402


def test_apply_is_retired_from_allowed_intents():
    assert "APPLY" not in ALLOWED_INTENTS
    assert "USE_SKILL" in ALLOWED_INTENTS


def test_execution_gap_opens_a_use_skill_intent():
    # Post-flip (Phase 6, OMEGA_USE_SKILL_ROUTING default True): the execution-pressure
    # signal routes through the SKILL layer (USE_SKILL has RETIRED the legacy APPLY route),
    # not the IDLE catch-all (SELF_INQUIRY was removed 2026-07-09 and collapsed to IDLE).
    assert _map_opened_by_to_intent("execution_gap", "") == "USE_SKILL"


def test_apply_action_is_retired():
    # APPLY is retired — USE_SKILL is the agentic execution action now.
    assert "APPLY" not in META_ACTIONS
    assert "USE_SKILL" in META_ACTIONS
    assert _intent_to_action_map()["APPLY"] == "USE_SKILL"
    assert _intent_to_action_map()["USE_SKILL"] == "USE_SKILL"


def test_legacy_apply_directive_shims_to_use_skill():
    c = from_operator_message({"message": "fix it", "directive_intent": "APPLY", "weight": 1.0})
    assert c.metadata.get("directive_intent") == "USE_SKILL"
