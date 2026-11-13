"""An explicit /skill selection bypasses Global A's intent router and binds exactly that skill."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import pressure_loop.cognition_queue as oq
import pressure_loop.operator_chat as oc
from hud.qwen_skills import QwenSkill, resolve_skill_command

SKILLS = {
    "repo-helper": QwenSkill(name="repo-helper", description="Helps inside a repository"),
    "custom": QwenSkill(name="custom", description="Custom command", command="ship"),
    "sleepy": QwenSkill(name="sleepy", description="Disabled skill", enabled=False),
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_loop.promptpack.intent_classifier.classify_chat_intent", lambda *a, **k: None)
    monkeypatch.setattr(oq, "_queue_path", lambda: tmp_path / "operator_queue.json")
    monkeypatch.setenv("OMEGA_BROWSER_WORKFLOW_PATH", str(tmp_path / "browser_workflow.json"))
    monkeypatch.setattr("hud.qwen_skills.load_skills", lambda: dict(SKILLS))
    oq._reset_cache_for_tests()
    yield
    oq.clear_inputs()


def _runtime():
    runtime = MagicMock()
    runtime.episode_store.current.return_value = None
    return runtime


@pytest.mark.parametrize("text,expected", [
    ("/skill:repo-helper add a health check endpoint",
     {"ok": True, "skill": "repo-helper", "command": "/skill:repo-helper", "objective": "add a health check endpoint"}),
    ("/ship\ncut the 2.4 release notes",
     {"ok": True, "skill": "custom", "command": "/ship", "objective": "cut the 2.4 release notes"}),
])
def test_skill_command_resolves_the_named_skill_and_task(text, expected):
    assert resolve_skill_command(text, SKILLS) == expected


@pytest.mark.parametrize("text", [
    "/help", "/mode", "/skills", "/plugin:calendar what's tomorrow", "hello /skill:repo-helper",
])
def test_other_text_is_not_a_skill_selection(text):
    assert resolve_skill_command(text, SKILLS) is None


@pytest.mark.parametrize("text,fragment", [
    ("/skill:nope do things", "No installed skill named 'nope'"),
    ("/skill:sleepy wake up", "disabled"),
    ("/skill:repo-helper", "Add the task after /skill:repo-helper"),
])
def test_unusable_skill_selection_is_answered_at_ingress(text, fragment):
    result = oc.handle_operator_chat(text, runtime=_runtime())

    assert result["queued"] is False
    assert fragment in result["message"]
    assert not oq.peek_inputs()


def test_skill_selection_rides_use_skill_into_the_agent_lane(monkeypatch):
    from global_loop.intent.route import routes_operator_turn
    from global_loop.loop import run_global_a
    from pressure_loop.contributors import EpisodeIgnition

    monkeypatch.setattr("global_loop.intent.router.get_router",
                        lambda **_: pytest.fail("a /skill selection must not reach the intent router"))

    result = oc.handle_operator_chat("/skill:repo-helper add a health check endpoint", runtime=_runtime())

    assert result["queued"] is True
    row = oq.peek_inputs()[0]
    assert (row.metadata["directive_intent"], row.metadata["selected_skill"], row.metadata["message"]) == (
        "USE_SKILL", "repo-helper", "add a health check endpoint")
    ignition = EpisodeIgnition("ep-skill", 1.0, row.type, row, (row,), None)
    assert routes_operator_turn(ignition) is True
    episode = SimpleNamespace(episode_id="ep-skill", global_={}, pressure={})
    decision = run_global_a(episode, ignition)
    assert (decision.intent, decision.omega_lane) == ("RESPOND", "chat")
    assert episode.global_["operator_route"]["route"] == "CODING_AGENT"
    assert episode.global_["operator_route"]["lane"] == "agent"


def test_meta_a_binds_the_selected_skill():
    from meta_loop.action.run import run as run_action
    from meta_loop.intake.run import MetaAInput
    from meta_loop.skill.run import _bundled_catalog, select_named_skill

    catalog = _bundled_catalog()
    assert catalog, "bundled execution skills are required"
    skill = catalog[-1]

    decision = select_named_skill(skill.name, objective="review the request parser", catalog=catalog)
    assert decision.selected and (decision.skill_name, decision.skill_id) == (skill.name, skill.skill_id)
    assert decision.execution_kind in skill.execution_kinds
    missing = select_named_skill("not-a-skill", objective="review", catalog=catalog)
    assert (missing.selected, missing.failure_reason) == (False, "selected_skill_unavailable:not-a-skill")

    mi = MetaAInput(
        episode_id="ep-meta",
        pressure={"contributors": [{"source": "operator", "type": "operator_query", "metadata": {
            "message": "review the request parser", "selected_skill": skill.name, "directive_intent": "USE_SKILL"}}],
            "aggregate_pressure": 1.0},
        trajectory_snapshot={},
        action_goal={"episode_id": "ep-meta", "intent": "USE_SKILL"},
    )
    assert run_action(mi).execution_inputs["selected_skill"] == skill.name


def test_attached_skill_pack_carries_the_operator_selected_skill():
    from meta_loop.skill.run import _bundled_catalog, attach_skill_execution_pack

    catalog = _bundled_catalog()
    skill = catalog[-1]
    episode = SimpleNamespace(episode_id="ep-attach", meta={}, global_={},
                              pressure={"opened_by": "operator_query", "contributors": []},
                              opened_by="operator_query")
    pack = {"episode_id": "ep-attach", "action": "USE_SKILL", "source_action_goal": {"intent": "USE_SKILL"},
            "execution_inputs": {"objective": "review the request parser", "selected_skill": skill.name}}

    skill_pack = attach_skill_execution_pack(episode, pack, catalog=catalog)

    assert skill_pack is not None, episode.meta.get("skill_selection_failure")
    assert (skill_pack["selected_skill"]["skill_name"], skill_pack["selected_skill"]["skill_id"]) == (
        skill.name, skill.skill_id)
