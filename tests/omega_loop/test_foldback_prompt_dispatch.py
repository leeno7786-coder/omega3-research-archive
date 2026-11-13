from dataclasses import replace

import pytest

from omega_loop.episode.types import OmegaEpisodeInput
from organ.promptorgan.packs.foldback import foldback_spec_for


WINDOW_VIEW = {
    "window_id": "win-lane",
    "episode_count": 2,
    "current_trajectory": {"target_domain": "python"},
    "episodes": [
        {"n": 1, "episode_id": "ep-1", "intent": "LEARN", "action": "USE_TOOL",
         "cascade_status": "completed", "outcome_status": "success", "outcome_detail": "", "turns": []},
        {"n": 2, "episode_id": "ep-2", "intent": "LEARN", "action": "USE_TOOL",
         "cascade_status": "failed", "outcome_status": "failure", "outcome_detail": "timeout", "turns": []},
    ],
    "failed_episodes": ["ep-2"],
}


def packet_fixture(omega_lane="chat", **kwargs):
    if omega_lane == "window":
        kwargs.setdefault("window_context", WINDOW_VIEW)
    return OmegaEpisodeInput(
        episode_id="lane-test", intent="APPLY", action="USE_SKILL", tool=None,
        domain="testing", operator_text="Check the result", outcome="success",
        failure_reason=None, raw_tool_result=None, recall=(), recall_error=None,
        prior_mastery=None, omega_lane=omega_lane, **kwargs,
    )


@pytest.mark.parametrize("lane", ["chat", "agent", "browser", "window"])
def test_foldback_dispatches_only_by_omega_lane(lane):
    packet = packet_fixture(lane)
    assert foldback_spec_for(packet).name == lane


def test_actions_do_not_change_agent_prompt_class():
    packet = packet_fixture("agent")
    for action in ("RESPONSE", "USE_TOOL", "ASK_OPERATOR", "IDLE", "NEW_ACTION"):
        assert foldback_spec_for(replace(packet, action=action)) is foldback_spec_for(packet)
