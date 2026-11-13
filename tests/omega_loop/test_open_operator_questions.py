"""An open operator question rides every lane prompt until the operator answers (2026-06-27).

The questions Omega has asked and not yet had answered are threaded into each
per-episode prompt, so the mind knows it has a pending ask: it does not re-ask, and it
proceeds with what it can until the reply arrives in the HUD.
"""

from __future__ import annotations

from omega_loop.episode.prompts import _pending_questions_block, prompt_spec_for
from omega_loop.episode.types import OmegaEpisodeInput


def _pkt(**over):
    base = dict(
        episode_id="ep1",
        intent="APPLY",
        action="USE_SKILL",
        tool=None,
        domain="python",
        operator_text=None,
        outcome="none",
        failure_reason=None,
        raw_tool_result=None,
        recall=(),
        recall_error=None,
        prior_mastery=None,
    )
    base.update(over)
    return OmegaEpisodeInput(**base)


def test_open_question_rides_the_apply_prompt():
    pkt = _pkt(
        omega_lane="agent",
        applied={
            "status": "success",
            "summary": "",
            "has_diff": True,
            "files_touched": 1,
            "error": None,
        },
        open_questions=(
            {"question": "Which framework — Flask or FastAPI?", "windows_survived": 2},
        ),
    )
    system, _user = prompt_spec_for(pkt).build(pkt)
    assert "Which framework — Flask or FastAPI?" in system
    assert "2 window" in system
    assert "do NOT re-ask" in system


def test_pending_block_empty_is_byte_identical():
    assert _pending_questions_block(_pkt(open_questions=())) == ""
