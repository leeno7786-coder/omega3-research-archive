"""Operator turns stay on Chat while semantic lanes progress independently."""

import pytest

from pressure_loop.contributors import from_operator_message, make_contributor


@pytest.fixture
def queue(tmp_path, monkeypatch):
    import pressure_loop.cognition_queue as module

    monkeypatch.setattr(module, "_queue_path", lambda: tmp_path / "cognition_queue.json")
    module._reset_cache_for_tests()
    yield module
    module._reset_cache_for_tests()


def _operator_turn(seq, message="same"):
    return from_operator_message({
        "message": message,
        "cycle_id": f"turn-{seq}",
        "arrival_seq": seq,
    })


def _lane_work(seq, lane):
    return make_contributor("omega", "lane_work_dispatch", 1.0, {
        "lane": lane,
        "cycle_id": f"{lane}-{seq}",
        "arrival_seq": seq,
    })


@pytest.mark.parametrize("message", ["hello", "build an app", "remember this", "learn Python"])
def test_operator_turns_serialize_on_chat_while_other_lanes_stay_independent(queue, message):
    first, second = _operator_turn(1, message), _operator_turn(2, message)
    queue.enqueue_input(first, operator_activity=False)
    queue.enqueue_input(second, operator_activity=False)

    chat_claim = queue.lease_next_input(0)
    assert chat_claim.metadata["arrival_seq"] == 1
    assert queue.lease_next_input(0) is None
    assert [queue.lease_next_input(pid) for pid in (1, 2, 3)] == [None, None, None]

    assert queue.acknowledge_input(chat_claim, 0)
    assert queue.lease_next_input(0).metadata["arrival_seq"] == 2


def test_each_physical_pipeline_leases_only_its_bound_lane(queue):
    chat = _operator_turn(1)
    browser = _lane_work(2, "browser")
    agent = _lane_work(3, "agent")
    window = _lane_work(4, "window")
    for item in (chat, browser, agent, window):
        queue.enqueue_input(item, operator_activity=False)

    assert queue.lease_next_input(0).metadata["cycle_id"] == "turn-1"
    assert queue.lease_next_input(1).metadata["cycle_id"] == "browser-2"
    assert queue.lease_next_input(2).metadata["cycle_id"] == "agent-3"
    assert queue.lease_next_input(3).metadata["cycle_id"] == "window-4"


def test_release_reopens_only_the_owned_lane_claim(queue):
    for item in (_operator_turn(1), _operator_turn(2), _lane_work(3, "browser")):
        queue.enqueue_input(item, operator_activity=False)

    chat_claim = queue.lease_next_input(0)
    browser_claim = queue.lease_next_input(1)
    assert queue.release_input_lease(0)
    assert queue.owns_input_lease(browser_claim, 1)
    assert queue.lease_next_input(0).queue_id == chat_claim.queue_id
    assert not queue.acknowledge_input(chat_claim, 1)
    assert queue.acknowledge_input(browser_claim, 1)
