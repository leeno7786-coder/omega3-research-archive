"""cognition_substrate MEDIUM-11 — the mastery Step-8 SignalEvent must carry the
REAL episode_id.

deepsleep_loop.mastery.run._apply_grounded_mastery derives episode_id correctly
(used for emit_plateau) but the Step-8 SignalEvent re-derived it from
action_goal/effect — neither of which ever carries an 'episode_id' key — so the
recorded signal always had episode_id="" and plateau-episode provenance was lost
in the audit ledger. The SignalEvent must use the same derived episode_id.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepsleep_loop.mastery.run import _apply_grounded_mastery  # noqa: E402


def test_mastery_signal_event_carries_episode_id(tmp_path, monkeypatch):
    captured = []
    import pressure_loop.signal_ledger as ledger

    # Replace the (flag-gated) recorder so we capture the event regardless of
    # SIGNALS_RECORD; the function imports `record` from this module at call time.
    monkeypatch.setattr(ledger, "record", lambda ev: captured.append(ev))

    _apply_grounded_mastery(
        domain_name="ai_systems",
        effect={
            "domain": "ai_systems",
            "knowledge_mastery": 0.6,
            "application_mastery": 0.4,
        },
        action_goal={},  # the blind spot: action_goal never carries episode_id
        resolved_path=tmp_path / "mastery.json",
        episode_id="ep-7f3a",
    )

    assert captured, "Step-8 SignalEvent must be recorded"
    assert captured[0].episode_id == "ep-7f3a"
