"""Model-owned research retains T9's unsupported lane-entry contract."""

from types import SimpleNamespace

import pytest

from auto_loop import lane_entry as gate
from auto_loop import loop as auto_loop
from auto_loop._auto_state import MemoryEnvelope
from auto_loop.promptpack.run import build_prompt_pack
from meta_loop.pack.run import _REQUIRED_CONSTRAINT_FLAGS
from tests.auto_loop.test_lane_entry_delivery import (
    THREAD,
    _continuation,
    _deliveries,
    _injection,
    _isolated as _isolated,
    _lane_mail,
    _publish,
)
from tests.helpers.blueprint_driver import BlueprintDriver, tool_call


CHOSEN_URL = "https://example.invalid/streaming"
FETCHED = "Streaming writers emit rows without retaining the complete file in memory."


@pytest.fixture(autouse=True)
def isolated_research(request, monkeypatch, tmp_path):
    request.getfixturevalue("_isolated")
    monkeypatch.setattr(
        "pressure_loop.policy.knowledge_artifact_store_path",
        lambda: tmp_path / "knowledge.jsonl",
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_knowledge_artifact_store_path",
        lambda: tmp_path / "knowledge.jsonl",
    )


@pytest.fixture
def research_seats(monkeypatch):
    generations = []
    fetches = []
    before_external = []

    def generate(**kwargs):
        for check in before_external:
            check()
        assert kwargs["operation"] == "web_research"
        generations.append(kwargs)
        if len(generations) == 1:
            return {"ok": True, "text": "", "tool_calls": [
                tool_call("web_fetch", {"query": CHOSEN_URL}, "read-document")
            ]}
        assert len(generations) == 2
        assert FETCHED in gate.prompt_text(messages=kwargs["messages"])
        return {"ok": True, "text": FETCHED, "tool_calls": []}

    def invoke(tool_name, args):
        for check in before_external:
            check()
        assert len(generations) == 1
        assert tool_name == "web_fetch" and args == {"query": CHOSEN_URL}
        fetches.append((tool_name, dict(args)))
        return {"ok": True, "elapsed_ms": 1, "result": {
            "query": CHOSEN_URL,
            "domain": "python",
            "results": [{"url": CHOSEN_URL, "title": "Streaming writers",
                         "content": FETCHED, "source": "example.invalid"}],
        }}

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", generate)
    return SimpleNamespace(generations=generations, fetches=fetches,
                           before_external=before_external, invoke=invoke)


def _research_continuation(successor, *, episode_id="ep-research"):
    episode, pack = _continuation(
        successor, episode_id=episode_id, action="USE_TOOL",
        inputs={"query": "streaming writers", "domain": "python",
                "web_research": True, "web_catalog": []},
    )
    goal = {"episode_id": episode_id, "intent": "USE_TOOL", "omega_lane": "agent"}
    pack.update(
        source_action_goal=goal,
        tool="web_fetch",
        tool_reason="model-owned research",
        memory_required=False,
        memory_scope=[],
        execution_constraints=dict(_REQUIRED_CONSTRAINT_FLAGS),
    )
    episode.meta = {"execution_action_pack": pack}
    episode.global_ = {"intent_decision": goal}
    assert auto_loop.validate_phase.validate_for_execution(pack) == (True, None, None)
    return episode, pack


def _assert_acquired(outputs, seats):
    assert outputs["tool_status"] == "success"
    assert outputs["tool_result"]["results"][0]["content"] == FETCHED
    assert seats.fetches == [("web_fetch", {"query": CHOSEN_URL})]
    assert len(seats.generations) == 2


def test_required_research_mail_refuses_before_inference_or_fetch(research_seats):
    parent, successor = _publish([_injection("required")])
    episode, _pack = _research_continuation(successor)
    before = _lane_mail()
    assert len(before) == 1

    outcome = auto_loop.run_auto(
        episode, runtime_client=object(), tool_invoker=research_seats.invoke,
    )

    assert outcome is not None
    assert episode.auto["failure_kind"] == "lane_entry_prerequisite_missing"
    assert episode.auto["outputs"] == {}
    assert research_seats.generations == research_seats.fetches == []
    assert _lane_mail() == before
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


@pytest.mark.parametrize("requirement", ["preferred", "optional"])
def test_optional_research_runs_once_without_consuming_mail(requirement, research_seats):
    parent, successor = _publish([_injection(requirement)])
    episode, _pack = _research_continuation(successor)
    before = _lane_mail()
    assert len(before) == 1

    def executing_without_mail():
        assert _lane_mail() == before
        assert [row["state"] for row in _deliveries(parent).values()] == ["executing"]

    research_seats.before_external.append(executing_without_mail)
    auto_loop.run_auto(episode, runtime_client=object(), tool_invoker=research_seats.invoke)

    assert episode.auto["failure_kind"] is None
    _assert_acquired(episode.auto["outputs"], research_seats)
    assert _lane_mail() == before
    assert [row["state"] for row in _deliveries(parent).values()] == ["performed"]
    assert episode.thread_id == THREAD
    replay, _replay_pack = _research_continuation(successor, episode_id="ep-research-replay")

    auto_loop.run_auto(replay, runtime_client=object(), tool_invoker=research_seats.invoke)

    assert replay.auto["failure_kind"] == "lane_entry_effect_already_performed"
    assert replay.auto["outputs"] == {}
    assert len(research_seats.generations) == 2 and len(research_seats.fetches) == 1
    assert [row["state"] for row in _deliveries(parent).values()] == ["performed"]


def test_no_mail_learning_carries_fetched_content_into_the_real_fold(monkeypatch, tmp_path):
    from tests.e2e.test_cross_restart_memory_loop import (
        FETCHED as acquisition_text,
        _fetches_the_document,
        _knowledge_fold,
        _learning_topic,
    )

    driver = BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)
    seen = []

    def acquisition_fold(current):
        request = current.generations[-1]
        assert request["operation"] == "episode_cognition"
        assert acquisition_text in gate.prompt_text(
            request.get("system"), request.get("prompt"), request.get("messages"),
        )
        seen.append(request)
        return _knowledge_fold(current)

    driver.omega.append(acquisition_fold)
    calls = _fetches_the_document(driver)
    staged = _learning_topic(driver)
    assert "lane_entry" not in staged.metadata

    episode = driver.run_successor()

    assert len(seen) == 1
    assert [name for name, _args in calls] == ["web_fetch"]
    assert episode.auto["failure_kind"] is None
    assert episode.auto["outputs"]["tool_result"]["results"][0]["content"] == acquisition_text
    assert not driver.research_batches
    receipts = episode.global_["omega_result"]["knowledge_receipts"]
    assert any(row.get("accepted") for row in receipts)


@pytest.mark.parametrize("requirement", ["required", "preferred", "optional"])
def test_research_declares_a_real_claimed_entry_unconsumable(requirement, research_seats):
    parent, successor = _publish([_injection(requirement)])
    episode, renderable_pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, renderable_pack)
    assert entry.state == "claimed" and len(entry.rows) == 1
    _research_episode, pack = _research_continuation(successor, episode_id=episode.episode_id)
    envelope = MemoryEnvelope(
        memory_required=False, memory_status="skipped", memory_reason=None,
        memory_scope=(), items=(), error=None,
    )
    prompt = build_prompt_pack(pack, envelope)

    def released_before_execution():
        assert [row["status"] for row in _lane_mail().values()] == ["ready"]
        assert [row["state"] for row in _deliveries(parent).values()] == ["executing"]

    research_seats.before_external.append(released_before_execution)
    result = auto_loop._execute_with_lane_entry(entry)(
        prompt, "TEXT_REASONING", pack, envelope, episode.episode_id,
        runtime_client=object(), tool_invoker=research_seats.invoke,
        opened_by="omega_successor", thread_id=episode.thread_id,
    )

    assert [row["status"] for row in _lane_mail().values()] == ["ready"]
    if requirement == "required":
        assert result["failure_kind"] == "lane_entry_not_consumed"
        assert result["outputs"] == {}
        assert research_seats.generations == research_seats.fetches == []
        assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]
    else:
        assert result["failure_kind"] is None
        _assert_acquired(result["outputs"], research_seats)
        assert [row["state"] for row in _deliveries(parent).values()] == ["performed"]
