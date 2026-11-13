from types import SimpleNamespace
import json

import pytest

import pressure_loop.proactive_lifecycle as lifecycle
from auto_loop._auto_state import MemoryEnvelope
from auto_loop.promptpack.run import build_prompt_pack
from global_loop.intent.run import run as run_global_a
from meta_loop.action.run import run as run_meta_action
from meta_loop.intake.run import MetaAInput
from meta_loop.memory.run import run as memory_run
from pressure_loop.contributors import EpisodeIgnition
from pressure_loop.pressure import _proactive_lifecycle_contributor


class _DialogueRuntime:
    def __init__(self):
        self.calls = []

    def health_body(self):
        return {"status": "ok", "capabilities": {"coding_model": True}}

    def generate_completion(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "text": "1. Heads in 20 flips: Binomial, because trials are fixed.",
            "tokens_in": 12,
            "tokens_out": 18,
        }


@pytest.fixture()
def applying_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_ledger_path", lambda: tmp_path / "proactive_lifecycle.jsonl")
    lifecycle._reset_cache_for_tests()
    thread = lifecycle.record_ask(
        prompt_id="prompt-1",
        episode_id="ep-ask",
        question="Continue?",
        why="curriculum",
        pressure_winner="clarification_gap",
        expected_response="policy",
        options=(),
    )
    policy = lifecycle.bind_operator_policy(
        prompt_id="prompt-1",
        answer_episode_id="ep-answer",
        operator_text="Continue one pass, then classify ten scenarios.",
    )
    lifecycle.structure_operator_policy(
        policy_id=policy.policy_id,
        objective="Distinguish Binomial, Poisson, and Normal through scenarios.",
        constraints=("Do not pivot while classification is weak.",),
        learning_steps=("Study simple examples.",),
        requested_demonstrations=("Classify 10 scenarios and explain each choice.",),
        continuation_rule="Continue Binomial-vs-Poisson boundary practice if weak.",
    )
    lifecycle.mark_learning(thread.thread_id, episode_id="ep-learn")
    lifecycle.mark_applying(thread.thread_id, episode_id="ep-learn")
    yield thread
    lifecycle._reset_cache_for_tests()


def _meta_input(contributor):
    return MetaAInput(
        episode_id="ep-present",
        pressure={
            "reason": "proactive_update",
            "contributors": [
                {
                    "id": contributor.type,
                    "weight": contributor.weight,
                    "detail": dict(contributor.metadata),
                }
            ],
            "aggregate_pressure": contributor.weight,
        },
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep-present",
            "intent": "PROACTIVE_RESPONSE",
            "confidence": 1.0,
            "reason": "application complete presentation",
        },
    )


def test_pending_dialogue_application_uses_existing_proactive_route(applying_policy):
    contributor = _proactive_lifecycle_contributor()
    assert contributor is not None
    assert contributor.type == "proactive_update"
    assert contributor.metadata["lane"] == "chat"
    from core.pipelines import lane_for_contributor

    assert lane_for_contributor(contributor) == "chat"
    assert contributor.weight < 1.0
    assert contributor.metadata["presentation_kind"] == "PRESENT_RESULT"
    ignition = EpisodeIgnition(
        episode_id="ep-present",
        opened_by="proactive_update",
        aggregate_pressure=1.0,
        contributors=(contributor,),
        dominant_contributor=contributor,
        trajectory_snapshot=None,
    )
    assert run_global_a(ignition) == "PROACTIVE_RESPONSE"


def test_meta_a_preserves_structured_dispatch_without_new_action(applying_policy):
    contributor = _proactive_lifecycle_contributor()
    state = run_meta_action(_meta_input(contributor))
    assert state.action == "PROACTIVE_RESPONSE"
    assert state.execution_inputs["presentation_kind"] == "PRESENT_RESULT"
    assert state.execution_inputs["operator_policy"]["operator_text"]
    assert state.execution_inputs["proactive_application_id"]


def test_present_result_recalls_policy_topic(applying_policy):
    contributor = _proactive_lifecycle_contributor()
    state = run_meta_action(_meta_input(contributor))
    out = memory_run(state)
    assert out.memory_required is True
    assert "Binomial" in out.memory_query
    assert "Classify 10 scenarios" in out.memory_query


def test_present_result_prompt_orders_executor_to_apply_and_explain(applying_policy):
    contributor = _proactive_lifecycle_contributor()
    state = run_meta_action(_meta_input(contributor))
    execution_pack = {
        "episode_id": state.episode_id,
        "action": state.action,
        "execution_intent": state.execution_intent,
        "execution_inputs": state.execution_inputs,
        "source_action_goal": state.source,
        "execution_constraints": state.constraints,
        "memory_required": True,
        "memory_scope": ("knowledge",),
    }
    envelope = MemoryEnvelope(
        memory_required=True,
        memory_status="ready",
        memory_reason="policy application",
        memory_scope=("knowledge",),
        items=(),
        error=None,
    )
    prompt = build_prompt_pack(execution_pack, envelope, pressure={})
    assert prompt.prompt_type == "PROACTIVE_REASONING"
    assert prompt.system_prompt == ""
    assert "complete the requested dialogue application" in prompt.context_blocks[
        "response_execution_directive"
    ].lower()
    assert "classify 10 scenarios" in prompt.user_prompt.lower()
    assert prompt.context_blocks["proactive_policy"]


def test_run_auto_executes_proactive_dialogue_application(applying_policy):
    """Pin the live seam: the built proactive prompt must reach a generator."""
    from auto_loop.loop import run_auto

    contributor = _proactive_lifecycle_contributor()
    state = run_meta_action(_meta_input(contributor))
    execution_pack = {
        "episode_id": state.episode_id,
        "action": state.action,
        "execution_intent": state.execution_intent,
        "execution_inputs": state.execution_inputs,
        "source_action_goal": state.source,
        "execution_constraints": state.constraints,
        "memory_required": False,
        "memory_scope": (),
    }
    episode = SimpleNamespace(
        episode_id="ep-present",
        pressure=_meta_input(contributor).pressure,
        global_={},
        meta={"execution_action_pack": execution_pack},
        auto=None,
    )
    runtime = _DialogueRuntime()

    outcome = run_auto(episode, runtime_client=runtime)

    assert outcome is not None
    assert len(runtime.calls) == 1, "the response model runs alone now"
    assert "model" not in runtime.calls[0]
    assert episode.auto["outputs"]["response_text"].startswith("1. Heads")
    assert "classify 10 scenarios" in runtime.calls[0]["prompt"].lower()


def test_gather_contains_lifecycle_loader_failure(monkeypatch):
    """A lifecycle side-store fault cannot terminate the organism's tick."""
    import pressure_loop.pressure as pressure

    monkeypatch.setattr(
        pressure,
        "_proactive_lifecycle_contributor",
        lambda: (_ for _ in ()).throw(RuntimeError("corrupt ledger")),
    )

    contributors = pressure.gather_contributors(None)

    assert isinstance(contributors, list)
