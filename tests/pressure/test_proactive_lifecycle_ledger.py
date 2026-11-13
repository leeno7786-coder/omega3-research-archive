import json
from dataclasses import replace

import pytest

import pressure_loop.proactive_lifecycle as lifecycle
from schema.proactive_lifecycle import (
    ApplicationRow,
    ProactiveFoldback,
    ProactiveResult,
)


@pytest.fixture()
def isolated_ledger(tmp_path, monkeypatch):
    path = tmp_path / "proactive.jsonl"
    monkeypatch.setattr(lifecycle, "_ledger_path", lambda: path)
    lifecycle._reset_cache_for_tests()
    yield path
    lifecycle._reset_cache_for_tests()


@pytest.fixture()
def result_fixture():
    return ProactiveResult(
        result_id="result-1",
        thread_id="thread-1",
        episode_id="ep-apply",
        origin_prompt_id="prompt-1",
        policy_id="policy-1",
        application_id="app-1",
        title="Practice result",
        summary="One verified classification.",
        practiced=("distribution selection",),
        learned=(),
        application=(
            ApplicationRow(
                prompt="Heads in 20 flips",
                answer="Binomial",
                expected="Binomial",
                correct=True,
                explanation="Fixed independent trials.",
                evidence=("executor:row-1",),
            ),
        ),
        evidence_refs=("executor:ep-apply",),
        confidence=0.9,
        mastery_status="passed",
        mastery_evidence=("1/1 verified",),
        mistakes=(),
        weak_patterns=(),
        next_step="More scenarios",
        operator_input_needed=False,
        status="ready",
    )


@pytest.fixture()
def foldback_fixture(result_fixture):
    return ProactiveFoldback(
        foldback_id="fold-1",
        thread_id=result_fixture.thread_id,
        episode_id=result_fixture.episode_id,
        origin_result_id=result_fixture.result_id,
        mastery_status="passed",
        mastery_delta=0.2,
        durable_rules=("Fixed trials imply Binomial.",),
        corrected_rules=(),
        unresolved_gaps=(),
        next_pressure_candidates=("Practice more scenarios.",),
        resolution="resolve",
        status="ready",
    )


def _seed_thread() -> tuple:
    thread = lifecycle.record_ask(
        prompt_id="prompt-1",
        episode_id="ep-ask",
        question="Continue or pivot?",
        why="Need curriculum direction.",
        pressure_winner="clarification_gap",
        expected_response="A bounded curriculum policy.",
        options=(),
    )
    policy = lifecycle.bind_operator_policy(
        prompt_id="prompt-1",
        answer_episode_id="ep-answer",
        operator_text="Continue one pass, then classify ten scenarios.",
    )
    return thread, policy


def test_question_policy_application_result_and_foldback_replay(isolated_ledger):
    thread, policy = _seed_thread()
    active = lifecycle.structure_operator_policy(
        policy_id=policy.policy_id,
        objective="Learn and apply distribution choice.",
        constraints=("Do not pivot while weak.",),
        learning_steps=("Study simple scenarios.",),
        requested_demonstrations=("Classify ten scenarios.",),
        continuation_rule="Continue boundary practice when weak.",
    )
    lifecycle.mark_learning(thread.thread_id, episode_id="ep-learn")
    lifecycle.mark_applying(thread.thread_id, episode_id="ep-learn")
    dispatch = lifecycle.pending_dialogue_dispatch()
    assert dispatch["kind"] == "PRESENT_RESULT"
    assert dispatch["thread_id"] == thread.thread_id
    assert dispatch["operator_policy"]["policy_id"] == active.policy_id

    lifecycle._reset_cache_for_tests()
    replayed = lifecycle.active_policy(thread_id=thread.thread_id)
    assert replayed is not None
    assert replayed.operator_text.startswith("Continue one pass")
    assert lifecycle.thread_state(thread.thread_id) == "applying"


def test_duplicate_result_and_foldback_are_idempotent(
    isolated_ledger, result_fixture, foldback_fixture
):
    lifecycle.record_result(result_fixture)
    lifecycle.record_result(result_fixture)
    lifecycle.mark_result_presented(result_fixture.result_id, message_id="chat-1")
    lifecycle.mark_result_presented(result_fixture.result_id, message_id="chat-1")
    lifecycle.record_foldback(foldback_fixture)
    lifecycle.record_foldback(foldback_fixture)
    rows = [json.loads(line) for line in isolated_ledger.read_text(encoding="utf-8").splitlines()]
    assert sum(row["event_type"] == "result_ready" for row in rows) == 1
    assert sum(row["event_type"] == "result_presented" for row in rows) == 1
    assert sum(row["event_type"] == "folded" for row in rows) == 1


def test_conflicting_duplicate_fails_closed(isolated_ledger, result_fixture):
    lifecycle.record_result(result_fixture)
    changed = ProactiveResult.from_dict({**result_fixture.to_dict(), "summary": "changed"})
    with pytest.raises(ValueError, match="conflicting"):
        lifecycle.record_result(changed)


def test_malformed_trailing_line_does_not_hide_valid_history_or_next_append(
    isolated_ledger,
):
    lifecycle.record_ask(
        prompt_id="prompt-1",
        episode_id="ep-ask",
        question="Continue?",
        why="direction",
        pressure_winner="clarification_gap",
        expected_response="policy",
        options=(),
    )
    with isolated_ledger.open("ab") as fh:
        fh.write(b'{"event_id":')
    lifecycle._reset_cache_for_tests()
    assert lifecycle.thread_for_prompt("prompt-1") is not None
    lifecycle.bind_operator_policy(
        prompt_id="prompt-1",
        answer_episode_id="ep-answer",
        operator_text="Continue.",
    )
    lifecycle._reset_cache_for_tests()
    assert lifecycle.active_policy() is not None
    assert all(
        isinstance(json.loads(line), dict)
        for line in isolated_ledger.read_text(encoding="utf-8").splitlines()
    )


def test_malformed_non_final_line_is_quarantined_without_killing_cognition(isolated_ledger):
    isolated_ledger.write_text('{"event_id":\n{"event_id":"valid"}\n', encoding="utf-8")
    lifecycle._reset_cache_for_tests()
    assert lifecycle.thread_for_prompt("prompt-1") is None
    assert not isolated_ledger.exists()
    quarantined = list(isolated_ledger.parent.glob("proactive.jsonl.corrupt.*"))
    assert len(quarantined) == 1


def test_dispatch_attempts_are_bounded_then_thread_is_parked(isolated_ledger):
    thread, policy = _seed_thread()
    lifecycle.structure_operator_policy(
        policy_id=policy.policy_id,
        objective="Practice distributions.",
        constraints=(),
        learning_steps=("Study scenarios.",),
        requested_demonstrations=("Classify scenarios.",),
        continuation_rule="Continue if weak.",
    )
    lifecycle.mark_learning(thread.thread_id, episode_id="ep-learn")
    lifecycle.mark_applying(thread.thread_id, episode_id="ep-apply")

    first = lifecycle.claim_dialogue_dispatch(max_attempts=2)
    second = lifecycle.claim_dialogue_dispatch(max_attempts=2)
    parked = lifecycle.claim_dialogue_dispatch(max_attempts=2)

    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert first["dispatch_id"] == second["dispatch_id"]
    assert parked["kind"] == "PARKED"
    assert lifecycle.thread_state(thread.thread_id) == "continuing"
    assert lifecycle.pending_dialogue_dispatch() is None


def test_oldest_applying_thread_dispatches_first(isolated_ledger):
    first, first_policy = _seed_thread()
    lifecycle.structure_operator_policy(
        policy_id=first_policy.policy_id,
        objective="First curriculum.",
        constraints=(),
        learning_steps=("Learn first.",),
        requested_demonstrations=("Apply first.",),
        continuation_rule="Continue first.",
    )
    lifecycle.mark_learning(first.thread_id, episode_id="ep-first-learn")
    lifecycle.mark_applying(first.thread_id, episode_id="ep-first-apply")

    second = lifecycle.record_ask(
        prompt_id="prompt-2",
        episode_id="ep-ask-2",
        question="Second?",
        why="direction",
        pressure_winner="clarification_gap",
        expected_response="policy",
        options=(),
    )
    second_policy = lifecycle.bind_operator_policy(
        prompt_id="prompt-2",
        answer_episode_id="ep-answer-2",
        operator_text="Second policy.",
    )
    lifecycle.structure_operator_policy(
        policy_id=second_policy.policy_id,
        objective="Second curriculum.",
        constraints=(),
        learning_steps=("Learn second.",),
        requested_demonstrations=("Apply second.",),
        continuation_rule="Continue second.",
    )
    lifecycle.mark_learning(second.thread_id, episode_id="ep-second-learn")
    lifecycle.mark_applying(second.thread_id, episode_id="ep-second-apply")

    assert lifecycle.pending_dialogue_dispatch()["thread_id"] == first.thread_id


def test_projection_is_cached_until_the_event_stream_changes(isolated_ledger):
    first = lifecycle._projection()
    second = lifecycle._projection()
    assert second is first

    lifecycle.record_ask(
        prompt_id="cache-prompt",
        episode_id="cache-episode",
        question="Cache?",
        why="performance",
        pressure_winner="clarification_gap",
        expected_response="policy",
        options=(),
    )

    third = lifecycle._projection()
    assert third is not second
    assert "cache-prompt" in third[1]


def test_invalid_state_transition_fails_closed(isolated_ledger):
    thread, _ = _seed_thread()
    with pytest.raises(ValueError, match="invalid proactive lifecycle transition"):
        lifecycle.mark_applying(thread.thread_id, episode_id="ep-apply")


@pytest.mark.parametrize(
    ("mastery_status", "operator_input_needed", "expected_state", "expected_resolution"),
    (
        ("passed", False, "resolved", "resolve"),
        ("weak", False, "continuing", "continue"),
        ("blocked", True, "continuing", "ask_operator"),
    ),
)
def test_presented_result_folds_then_resolves_or_continues(
    isolated_ledger,
    result_fixture,
    mastery_status,
    operator_input_needed,
    expected_state,
    expected_resolution,
):
    thread, policy = _seed_thread()
    policy = lifecycle.structure_operator_policy(
        policy_id=policy.policy_id,
        objective="Practice distributions.",
        constraints=(),
        learning_steps=("Study scenarios.",),
        requested_demonstrations=("Classify scenarios.",),
        continuation_rule="Continue if weak.",
    )
    lifecycle.mark_learning(thread.thread_id, episode_id="ep-learn")
    lifecycle.mark_applying(thread.thread_id, episode_id="ep-learn")
    result = replace(
        result_fixture,
        result_id=f"result-{mastery_status}",
        thread_id=thread.thread_id,
        origin_prompt_id=policy.origin_prompt_id,
        policy_id=policy.policy_id,
        mastery_status=mastery_status,
        operator_input_needed=operator_input_needed,
        mastery_evidence=("verified",) if mastery_status == "passed" else (),
        weak_patterns=() if mastery_status == "passed" else ("boundary confusion",),
    )
    lifecycle.record_result(result)
    lifecycle.mark_result_presented(result.result_id, message_id="chat-1")
    foldback = lifecycle.fold_presented_result(result.result_id, mastery_delta=0.2)
    assert foldback is not None
    assert foldback.resolution == expected_resolution
    assert foldback.mastery_delta == (0.2 if mastery_status == "passed" else 0.0)
    assert lifecycle.thread_state(thread.thread_id) == expected_state
