"""Operator INPUT queue box — durable FIFO, one per pressure cycle. RED-first.

Operator spec 2026-06-12: incoming operator queries hold in a durable FIFO
(order of input) and apply ONE per pressure cycle, so no input is ever lost
or unanswered: today's drain-all merges every queued message into one
contest and silently discards the losers, and ordering is an accident of
max(). (The OUTBOX side is deliberately NOT built here — the operator has a
different design for it.)

Each test asserts the configured VALUE and the FAIL-MODE, both directions.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


from pressure_loop.contributors import from_operator_message
from pressure_loop.pressure import evaluate
from pressure_loop import cognition_queue as cq


@pytest.fixture(autouse=True)
def _isolated_box(tmp_path, monkeypatch):
    """Point the input box at a per-test temp file and reset its cache."""
    import pressure_loop.cognition_queue as queue_mod

    monkeypatch.setattr(queue_mod, "_queue_path", lambda: tmp_path / "operator_input_queue.json")
    queue_mod._reset_cache_for_tests()
    yield


def _take_input():
    contributor = cq.lease_next_input(0)
    if contributor is not None:
        assert cq.acknowledge_input(contributor, 0)
    return contributor


def _msg(text: str, cycle_id: str):
    return from_operator_message(
        {
            "message": text,
            "ingress_class": "epistemic_request",
            "weight": 1.0,
            "cycle_id": cycle_id,
        }
    )


# --- input box: FIFO, one per cycle, nothing lost ------------------------------


def test_fifo_one_per_cycle_no_input_lost():
    from pressure_loop.cognition_queue import (
        enqueue_input,
        peek_inputs,
    )

    m1, m2, m3 = (
        _msg("first question?", "c-1"),
        _msg("second question?", "c-2"),
        _msg("third question?", "c-3"),
    )
    assert enqueue_input(m1, operator_activity=False)
    assert enqueue_input(m2, operator_activity=False)
    assert enqueue_input(m3, operator_activity=False)

    seen: list[str] = []
    for _ in range(3):
        ignition = evaluate(list(cq.peek_inputs()[:1]), active_episode=False, neuromod={})
        assert ignition is not None, "a queued input must ignite its own cycle"
        assert ignition.dominant_contributor.source == "operator"
        seen.append(str(ignition.dominant_contributor.metadata.get("message")))
        admitted = _take_input()
        assert admitted is not None
        assert admitted.metadata.get("cycle_id") == ignition.dominant_contributor.metadata.get(
            "cycle_id"
        )

    # Strict input order — and every input got its episode (none lost).
    assert seen == ["first question?", "second question?", "third question?"]
    assert peek_inputs() == ()

    # Other direction: empty box + empty field → no ignition.
    assert evaluate(list(cq.peek_inputs()[:1]), active_episode=False, neuromod={}) is None


def test_box_pop_beats_lingering_ingress_record():
    from pressure_loop.cognition_queue import enqueue_input, peek_inputs

    # M1 waits in the box; M3's TTL-decaying ingress copy is still in the
    # gathered field at the same nominal weight. Order is LAW: M1 wins.
    m1 = _msg("first question?", "c-1")
    enqueue_input(m1, operator_activity=False)

    ignition = evaluate([cq.lease_next_input(0)], active_episode=False, neuromod={})
    assert ignition is not None
    assert ignition.dominant_contributor.metadata.get("message") == "first question?"
    operator_cycles = [
        c.metadata.get("cycle_id") for c in ignition.contributors if c.source == "operator"
    ]
    assert operator_cycles == ["c-1"]
    assert [c.metadata.get("cycle_id") for c in peek_inputs()] == ["c-1"]


def test_evaluate_leases_oldest_input_without_consuming_it():
    from pressure_loop.cognition_queue import enqueue_input, peek_inputs

    enqueue_input(_msg("survive admission?", "c-lease"), operator_activity=False)

    ignition = evaluate(list(cq.peek_inputs()[:1]), active_episode=False, neuromod={})

    assert ignition is not None
    assert ignition.dominant_contributor.metadata.get("cycle_id") == "c-lease"
    assert [c.metadata.get("cycle_id") for c in peek_inputs()] == ["c-lease"]


def test_active_episode_enqueues_into_box_in_order():
    from pressure_loop.cognition_queue import peek_inputs

    # Messages arriving mid-episode flow INTO the box (never dropped)...
    cq.enqueue_input(_msg("while busy A?", "c-a"), operator_activity=False)
    held = evaluate([_msg("while busy A?", "c-a")], active_episode=True, neuromod={})
    assert held is None
    cq.enqueue_input(_msg("while busy B?", "c-b"), operator_activity=False)
    held2 = evaluate([_msg("while busy B?", "c-b")], active_episode=True, neuromod={})
    assert held2 is None

    queued = peek_inputs()
    assert [c.metadata.get("message") for c in queued] == [
        "while busy A?",
        "while busy B?",
    ]

    # ...and an active episode never POPS the box (the moment is taken).
    assert len(peek_inputs()) == 2


def test_mixed_main_and_proactive_turns_keep_fifo_across_episode_boundaries():
    """Every distinct operator turn is a queue item, even when two main-chat
    turns repeat the same text around a proactive-panel answer.

    Regression caught: normal chat deduped on message text before ``cycle_id``,
    so the second intentional send vanished while an episode was active.
    """
    from pressure_loop.cognition_queue import peek_inputs

    main_first = from_operator_message(
        {
            "message": "build the queue contract",
            "ingress_class": "operator_command",
            "cycle_id": "chat-1",
        }
    )
    proactive_answer = from_operator_message(
        {
            "message": "yes, preserve arrival order",
            "ingress_class": "answer_to_question",
            "cycle_id": "pin-1",
            "system_query_id": "question-1",
        }
    )
    main_repeated = from_operator_message(
        {
            "message": "build the queue contract",
            "ingress_class": "operator_command",
            "cycle_id": "chat-2",
        }
    )

    for contributor in (main_first, proactive_answer, main_repeated):
        cq.enqueue_input(contributor, operator_activity=False)
        assert evaluate([contributor], active_episode=True, neuromod={}) is None

    # Every operator turn uses the one chat line. Intent classification later
    # decides whether AUTO responds conversationally or runs tools; it must not
    # fork the HUD ingress queue.
    assert [c.metadata["cycle_id"] for c in peek_inputs()] == [
        "chat-1",
        "pin-1",
        "chat-2",
    ]
    assert len(peek_inputs()) == 3

    admitted = []
    for _ in range(3):
        ignition = evaluate(list(cq.peek_inputs()[:1]), active_episode=False, neuromod={})
        assert ignition is not None
        admitted.append(ignition.dominant_contributor.metadata["cycle_id"])
        popped = _take_input()
        assert popped is not None
        assert popped.metadata["cycle_id"] == admitted[-1]

    # Every send is admitted exactly once, and the two same-text builds keep
    # their arrival order relative to each other — the regression this test was
    # written for.
    assert sorted(admitted) == ["chat-1", "chat-2", "pin-1"]
    assert admitted.index("chat-1") < admitted.index("chat-2")
    assert peek_inputs() == ()


def test_enqueue_dedup_same_message_once():
    from pressure_loop.cognition_queue import (
        enqueue_input,
        peek_inputs,
    )

    m = _msg("am I echoing?", "c-echo")
    assert enqueue_input(m, operator_activity=False) is True
    # The same utterance arriving again (TTL record re-gathered every tick
    # while an episode runs) must not double-queue.
    assert enqueue_input(_msg("am I echoing?", "c-echo"), operator_activity=False) is False
    assert len(peek_inputs()) == 1


def test_sync_pin_never_pops_the_box():
    from pressure_loop.cognition_queue import (
        enqueue_input,
        peek_inputs,
    )

    enqueue_input(_msg("queued earlier?", "c-q"), operator_activity=False)
    pin = _msg("the synchronous turn", "c-pin")

    ignition = evaluate([pin], active_episode=False, neuromod={}, force_dominant=pin)
    assert ignition is not None
    assert ignition.dominant_contributor.metadata.get("cycle_id") == "c-pin"
    # The queued input still waits its turn — order intact.
    assert len(peek_inputs()) == 1



def test_input_box_persists_across_reload():
    import pressure_loop.cognition_queue as queue_mod
    from pressure_loop.cognition_queue import (
        enqueue_input,
        peek_inputs,
    )

    enqueue_input(_msg("survive a restart?", "c-s"), operator_activity=False)
    # Cut 7: the operator's turn is durable on the CHAT line, not the retired
    # single-file queue.
    path = queue_mod._queue_path()
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))["queue"]
    assert len(on_disk) == 1
    assert on_disk[0]["metadata"]["message"] == "survive a restart?"

    # Simulate process restart: cache dropped, box reloads from disk.
    queue_mod._reset_cache_for_tests()
    queued = peek_inputs()
    assert [c.metadata.get("message") for c in queued] == ["survive a restart?"]

    popped = _take_input()
    assert popped is not None
    assert json.loads(path.read_text(encoding="utf-8"))["queue"] == []


def test_plugin_permission_queue_identity_is_request_id_not_decision() -> None:
    """Allow and Cancel for one request are one queue decision, never two turns."""
    from pressure_loop.contributors import make_contributor
    from pressure_loop.cognition_queue import _box_dedup_key

    common = {
        "request_id": "a" * 48,
        "expected_revision": "b" * 64,
        "prior_episode_id": "episode-before-permission",
        "task_id": "logical-plugin-task",
        "plan_id": "c" * 64,
        "plan_digest": "d" * 64,
    }
    allow = make_contributor(
        "operator",
        "plugin_permission_decision",
        1.0,
        {**common, "decision": "allow"},
    )
    cancel = make_contributor(
        "operator",
        "plugin_permission_decision",
        1.0,
        {**common, "decision": "cancel"},
    )

    assert (
        _box_dedup_key(allow)
        == _box_dedup_key(cancel)
        == (
            "plugin_permission_decision",
            "a" * 48,
        )
    )


def test_external_observation_keeps_arrival_order_on_its_own_line() -> None:
    from pressure_loop.contributors import make_contributor
    from pressure_loop.cognition_queue import (
        enqueue_input,
        peek_inputs,
    )

    late = _msg("operator later", "operator-later")
    late.metadata["arrival_seq"] = 30
    external = make_contributor(
        "external",
        "sensory_observation",
        1.0,
        {"lane": "chat", "cycle_id": "external-middle", "arrival_seq": 20},
    )
    early = _msg("operator first", "operator-first")
    early.metadata["arrival_seq"] = 10

    assert enqueue_input(late, operator_activity=False)
    assert enqueue_input(external, operator_activity=False)
    assert enqueue_input(early, operator_activity=False)
    assert [item.metadata["arrival_seq"] for item in peek_inputs()] == [10, 20, 30]


def test_arrival_stamp_is_not_part_of_contributor_semantic_equality() -> None:
    """The queue's operational stamp must not make equality non-transitive."""
    from pressure_loop.contributors import make_contributor

    unstamped = make_contributor(
        "external",
        "sensory_observation",
        1.0,
        {"lane": "chat", "cycle_id": "semantic-observation"},
    )
    first = make_contributor(
        "external",
        "sensory_observation",
        1.0,
        {"lane": "chat", "cycle_id": "semantic-observation", "arrival_seq": 1},
    )
    second = make_contributor(
        "external",
        "sensory_observation",
        1.0,
        {"lane": "chat", "cycle_id": "semantic-observation", "arrival_seq": 2},
    )

    assert unstamped == first == second
