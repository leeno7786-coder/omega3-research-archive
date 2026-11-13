from types import SimpleNamespace

import pytest

from global_loop.loop import run_global_a
from pressure_loop.contributors import EpisodeIgnition, make_contributor


@pytest.mark.parametrize(
    ("lane", "intent"),
    [("browser", "BROWSE"), ("agent", "RESPOND")],
)
def test_global_a_honors_a_host_admitted_specialist_handoff(lane, intent, monkeypatch):
    metadata = {
        "lane_work_handoff": True,
        "lane": lane,
        "operator_route": lane,
        "directive_intent": intent,
        "message": "Continue the same task",
        "original_cycle_id": "operator-cycle",
        "thread_id": "conversation-1",
        "route_context": "the current task",
    }
    contributor = make_contributor("omega", "lane_work_dispatch", 1.0, metadata)
    ignition = EpisodeIgnition(
        "specialist-episode", 1.0, contributor.type, contributor, (contributor,), None,
    )
    episode = SimpleNamespace(episode_id="specialist-episode", global_={}, pressure={})
    browser_turn_flags = []
    if lane == "browser":
        monkeypatch.setattr(
            "global_loop.browser_workflow.bind_browser_workflow",
            lambda _ignition, *, operator_turn: (
                browser_turn_flags.append(operator_turn)
                or {"workflow_id": "workflow-1", "objective": "Continue the same task", "status": "active"}
            ),
        )

    decision = run_global_a(episode, ignition)

    assert (decision.intent, decision.omega_lane) == (intent, lane)
    routed = episode.global_["operator_route"]
    assert (routed["source"], routed["lane"], routed["request"]) == (
        "handoff", lane, "Continue the same task",
    )
    if lane == "browser":
        assert browser_turn_flags == [False]


def test_global_a_preserves_an_explicit_skill_on_the_agent_handoff():
    metadata = {
        "lane_work_handoff": True,
        "lane": "agent",
        "operator_route": "agent",
        "directive_intent": "USE_SKILL",
        "selected_skill": "repo-helper",
        "message": "Add a health check endpoint",
        "original_cycle_id": "operator-cycle",
        "thread_id": "conversation-1",
    }
    contributor = make_contributor("omega", "lane_work_dispatch", 1.0, metadata)
    ignition = EpisodeIgnition(
        "specialist-skill-episode", 1.0, contributor.type, contributor, (contributor,), None,
    )
    episode = SimpleNamespace(episode_id="specialist-skill-episode", global_={}, pressure={})

    decision = run_global_a(episode, ignition)

    assert (decision.intent, decision.omega_lane) == ("USE_SKILL", "agent")
    assert episode.global_["operator_route"]["route"] == "CODING_AGENT"
