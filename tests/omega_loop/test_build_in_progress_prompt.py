"""Fold-back build loop — Omega is the QA/mind over an UNFINISHED build.

A YIELDED build surfaces as `build_in_progress` in the Omega packet; the agent lane's apply
prompt must show the progress digest AND instruct the continue|finish decision, so the mind owns
whether the engineer resumes.
"""

from __future__ import annotations

from omega_loop.episode.prompts import prompt_spec_for
from omega_loop.episode.types import OmegaEpisodeInput


def _apply_packet(**over):
    base = dict(
        omega_lane="agent",
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
        applied={
            "status": "yielded",
            "summary": "built planner",
            "has_diff": True,
            "files_touched": 1,
            "error": None,
        },
    )
    base.update(over)
    return OmegaEpisodeInput(**base)


def test_build_in_progress_renders_in_apply_prompt():
    pkt = _apply_packet(
        build_in_progress=(
            {
                "objective": "build the ai-agent framework",
                "progress_digest": "yielded after 12 rounds; 4 files; next: tool registry",
                "files_changed": ["src/core/types.ts"],
                "rounds_used": 12,
                "test_results": ["pytest tests/ -q"],
                "errors": ["missing tool registry"],
                "current_state": "scaffolded core types",
            },
        )
    )
    system, user = prompt_spec_for(pkt).build(pkt)
    # a PAUSED build routes to the Nelson-Narens monitor-controller prompt (objective-first),
    # NOT the mastery reflection — so no mastery-progression bias leaks in.
    low = system.lower()
    assert "monitoring" in low and "control" in low
    assert "[TRAJECTORY]" in system and "[COMPLETENESS]" in system and "[BLOCKERS]" in system
    assert "execution_debt_delta" in system and "ASK_OPERATOR" in system
    assert "SCHEMA NOTE" in system
    assert "raises application toward target" not in system  # the mastery-bias phrase is gone
    # user block: FOLD-BACK ACTIVE header + objective + the build-record signals, no failure framing
    assert "FOLD-BACK ACTIVE" in user
    assert "build the ai-agent framework" in user  # the operator's objective, stated first
    assert "tool registry" in user  # digest
    assert "iterations=12" in user
    assert "pytest tests/ -q" in user  # test_results
    assert "missing tool registry" in user  # errors
    assert "outcome: failure" not in user  # a yield is not framed as failure


def test_build_in_progress_empty_is_explicit_none():
    _system, user = prompt_spec_for(_apply_packet()).build(_apply_packet())
    assert "no build is mid-flight" in user


def test_packet_to_dict_roundtrips_build_in_progress():
    wip = ({"objective": "o", "progress_digest": "d", "files_changed": ["a"], "rounds_used": 3},)
    pkt = _apply_packet(build_in_progress=wip)
    d = pkt.to_dict()
    assert d["build_in_progress"] == [dict(wip[0])]
    # a default packet (no build) carries an empty tuple — byte-identical
    assert _apply_packet().to_dict()["build_in_progress"] == []


# ── the control pass COLLAPSED by the battery verdict (operator 2026-07-02) ────────────────
RED_WIP = {
    "objective": "build csv_stats",
    "original_request": "Build csv_stats with at least 4 tests and a README.",
    "verification": {
        "checks": [
            {"name": "request_coverage", "ok": False, "detail": "README missing"},
            {"name": "tests_green", "ok": True, "detail": "4 passed"},
        ],
        "all_green": False,
        "summary": "red: request_coverage",
    },
}
GREEN_WIP = dict(
    RED_WIP,
    verification={
        "checks": [{"name": "tests_green", "ok": True, "detail": "4 passed"}],
        "all_green": True,
        "summary": "all checks green",
    },
)


def test_red_prompt_has_no_completion_and_green_has_no_continue():
    red_sys, red_user = prompt_spec_for(_apply_packet(build_in_progress=(RED_WIP,))).build(
        _apply_packet(build_in_progress=(RED_WIP,))
    )
    green_sys, _ = prompt_spec_for(_apply_packet(build_in_progress=(GREEN_WIP,))).build(
        _apply_packet(build_in_progress=(GREEN_WIP,))
    )
    # red: CONTINUE menu, completion structurally absent
    assert "battery is RED" in red_sys and "CONTINUE" in red_sys
    assert "Completion is not available" in red_sys
    assert "COMPLETE (the default)" not in red_sys
    # green: COMPLETE menu, continuation off the table
    assert "battery is GREEN" in green_sys and "COMPLETE (the default)" in green_sys
    assert "Continuation is no longer on" in green_sys
    assert "CONTINUE (the default)" not in green_sys
    # both keep the shared monitoring pass + schema note
    for sysp in (red_sys, green_sys):
        assert "[TRAJECTORY]" in sysp and "[COMPLETENESS]" in sysp and "SCHEMA NOTE" in sysp
    # the user block carries the main meta question + the verbatim request + the battery table
    assert "does my work meet the requirements to satisfy it" in red_user
    assert "at least 4 tests and a README" in red_user
    assert "README missing" in red_user and "✗" in red_user


def test_stalled_red_prompt_opens_escalation():
    wip = dict(RED_WIP, verification_stalled=True)
    system, _ = prompt_spec_for(_apply_packet(build_in_progress=(wip,))).build(
        _apply_packet(build_in_progress=(wip,))
    )
    assert "ESCALATION IS OPEN" in system and "UNCHANGED" in system
    assert "genuinely NEW approach" in system
    assert "Completion is not available" in system


def test_no_verification_snapshot_reads_as_red():
    wip = {"objective": "o", "progress_digest": "d"}  # no battery snapshot
    system, _ = prompt_spec_for(_apply_packet(build_in_progress=(wip,))).build(
        _apply_packet(build_in_progress=(wip,))
    )
    assert "battery is RED" in system  # never a completion menu without proof
