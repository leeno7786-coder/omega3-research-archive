"""The per-episode foldback packet — including screened plugin execution evidence.

The packet is informational, not directive.
"""

from __future__ import annotations

from pressure_loop import pressure_surface as ps


def test_pressure_surface_tracks_current_previous_and_deltas():
    ps.reset_for_tests()
    s1 = ps.build_pressure_surface({"mastery_gap": 0.6}, domain="python")
    assert s1["current_pressure"] == {"mastery_gap": 0.6}
    assert s1["previous_pressure"] == {}
    assert s1["pressure_deltas"]["mastery_gap"] == 0.6  # appeared this episode

    s2 = ps.build_pressure_surface({"mastery_gap": 0.4, "execution_gap": 0.3}, domain="python")
    assert s2["previous_pressure"]["mastery_gap"] == 0.6
    assert s2["pressure_deltas"]["mastery_gap"] == -0.2  # decreased
    assert s2["pressure_deltas"]["execution_gap"] == 0.3  # appeared


def test_pressure_surface_lists_variables_and_excludes_trajectory_gap():
    ps.reset_for_tests()
    s = ps.build_pressure_surface({}, domain="x")
    avail = s["available_pressure_variables"]
    assert "verification_needed" in avail and "operator_priority" in avail
    assert "trajectory_gap" not in avail  # decoupled — trajectory influences via the prompt


def test_pressure_surface_failure_soft_on_bad_input():
    ps.reset_for_tests()
    s = ps.build_pressure_surface(None, domain="")
    assert s["current_pressure"] == {}
    assert isinstance(s["available_pressure_variables"], list)
    # non-numeric values dropped, not raised
    s2 = ps.build_pressure_surface({"mastery_gap": "oops", "execution_gap": 0.5}, domain="y")
    assert s2["current_pressure"] == {"execution_gap": 0.5}


def test_pressure_surface_is_per_domain():
    ps.reset_for_tests()
    ps.build_pressure_surface({"mastery_gap": 0.5}, domain="python")
    s = ps.build_pressure_surface({"mastery_gap": 0.2}, domain="algorithms")
    assert s["previous_pressure"] == {}  # different domain -> no prior


class _FakeGbi:
    pressure = {
        "contributors": [
            {"type": "mastery_gap", "weight": 0.6},
            {"type": "execution_gap", "weight": 0.2},
        ]
    }
    source_action_goal = {"intent": "LEARN", "reason": "study python"}


def test_assemble_foldback_packet_has_all_seven_sections():
    from omega_loop.episode.packet import _assemble_foldback_packet

    ps.reset_for_tests()
    fp = _assemble_foldback_packet(
        _FakeGbi(),
        {"result": "ok", "tool": "search", "disposition": "validated", "failure_reason": None},
        {"file.py": "..."},
        {"summary": "learned x"},
        "python",
    )
    assert set(fp.keys()) == {
        "pressure_surface",
        "episode_foldback",
        "plugin_execution",
        "plugin_evidence",
        "mastery_delta",
        "trajectory_influencer",
        # the trajectory joined with its mastery phase: Omega's CONDUCT guidance
        "conduct",
    }
    assert fp["pressure_surface"]["current_pressure"]["mastery_gap"] == 0.6
    mb = fp["episode_foldback"]["meta_b"]
    gb = fp["episode_foldback"]["global_b"]
    assert mb["outcome"] == "ok" and mb["tool_or_skill_used"] == "search"
    assert mb["validation"] == "validated"
    assert gb["goal_intent"] == "LEARN" and gb["summary"] == "learned x"
    assert gb["artifacts"] == ["file.py"]
    assert fp["plugin_execution"] is None
    assert fp["plugin_evidence"] == {}
    assert fp["mastery_delta"]["domain"] == "python"


def test_assemble_foldback_packet_failure_soft():
    from omega_loop.episode.packet import _assemble_foldback_packet

    ps.reset_for_tests()
    fp = _assemble_foldback_packet(object(), {}, None, None, "")
    assert isinstance(fp, dict) and "episode_foldback" in fp


def _packet_with_foldback(fp):
    from omega_loop.episode.types import OmegaEpisodeInput

    return OmegaEpisodeInput(
        episode_id="e1",
        intent="LEARN",
        action="learn",
        tool=None,
        domain="python",
        operator_text=None,
        outcome="ok",
        failure_reason=None,
        raw_tool_result=None,
        recall=(),
        recall_error=None,
        prior_mastery=None,
        foldback_packet=fp,
    )


_FULL_FP = {
    "pressure_surface": {
        "current_pressure": {"mastery_gap": 0.6},
        "pressure_deltas": {"mastery_gap": 0.2},
        "available_pressure_variables": ["mastery_gap", "verification_needed"],
    },
    "episode_foldback": {"meta_b": {"outcome": "ok"}, "global_b": {"summary": "learned x"}},
    "mastery_delta": {
        "domain": "python",
        "knowledge_before": 0.4,
        "knowledge_after": 0.6,
        "application_before": 0.0,
        "application_after": 0.0,
        "remaining_debt": 0.4,
    },
    "trajectory_influencer": {
        "active_domain": "python",
        "active_goal": "master basics",
        "last_decision": "continue",
        "reason": "good progress",
    },
}


def test_the_retired_foldback_block_is_not_rendered():
    """``_foldback_block`` rendered the packet as FOLDBACK / PRESSURE NOW / THIS EPISODE /
    MASTERY / TRAJECTORY INFLUENCE lines, but only into the system half of the old
    ``_build_knowledge`` / ``_build_apply``, which the live agent lane discarded, so no model ever
    read it. Retired (operator 2026-09-21), and those system halves with it: the agent and browser
    lanes carry the packet as data in their evidence, and the trajectory reaches every
    per-episode lane as CONDUCT. Checked against what the live agent lane actually sends."""
    from dataclasses import replace

    from omega_loop.episode import prompts
    from organ.promptorgan.packs.foldback import foldback_spec_for

    base = replace(_packet_with_foldback(_FULL_FP), omega_lane="agent")
    for action in ("USE_TOOL", "USE_SKILL"):  # acquisition and apply folds
        packet = replace(base, action=action)
        system, user = foldback_spec_for(packet).build(packet)
        for line in ("--- FOLDBACK (", "PRESSURE NOW:", "THIS EPISODE:", "MASTERY [python]",
                     "TRAJECTORY INFLUENCE"):
            assert line not in system + user, f"the {action} agent fold still renders {line!r}"
    assert not hasattr(prompts, "_foldback_block"), "the retired renderer is still defined"

