from __future__ import annotations

import threading

import pytest

from hud import chat_socket
from pressure_loop.operator_chat_responses import reply_token_sink


@pytest.fixture(autouse=True)
def _clean_chat_socket():
    chat_socket._reset_for_tests()
    yield
    chat_socket._reset_for_tests()


def test_interleaved_pipeline_events_keep_their_cycle_and_monotonic_sequence():
    first = chat_socket.publish_chat_progress(
        "cycle-a", "chat.tool_call", pipeline_id=2, job_id="job-a"
    )
    second = chat_socket.publish_chat_progress(
        "cycle-b", "chat.reasoning_delta", pipeline_id=0
    )
    third = chat_socket.publish_chat_progress(
        "cycle-a", "chat.tool_result", pipeline_id=2, job_id="job-a"
    )

    assert [first["cycle_id"], second["cycle_id"], third["cycle_id"]] == [
        "cycle-a", "cycle-b", "cycle-a"
    ]
    assert first["event_seq"] < second["event_seq"] < third["event_seq"]
    assert first["pipeline_id"] == third["pipeline_id"] == 2
    assert "job_id" not in second


def test_concurrent_allocation_and_publication_have_the_same_observed_order():
    observed: list[int] = []
    barrier = threading.Barrier(5)
    chat_socket.subscribe(lambda event: observed.append(event["event_seq"]))

    def publish(index: int) -> None:
        barrier.wait()
        chat_socket.publish_chat_progress(
            f"cycle-{index}", "chat.reasoning_delta", pipeline_id=index
        )

    threads = [threading.Thread(target=publish, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert observed == sorted(observed)
    assert len(set(observed)) == 4


def test_final_reply_keeps_the_owning_pipeline_job_and_work_kind():
    observed: list[dict[str, object]] = []
    chat_socket.subscribe(observed.append)

    chat_socket.publish_reply({
        "cycle_id": "cycle-final",
        "episode_id": "episode-final",
        "text": "done",
        "pipeline_id": 3,
        "job_id": "job-final",
        "work_kind": "operator_turn",
    })

    assert observed == [{
        "kind": "chat.reply",
        "event_seq": observed[0]["event_seq"],
        "cycle_id": "cycle-final",
        "episode_id": "episode-final",
        "text": "done",
        "ts": "",
        "pipeline_id": 3,
        "job_id": "job-final",
        "work_kind": "operator_turn",
    }]


def test_reasoning_progress_frames_do_not_expose_provider_private_trace():
    observed: list[dict[str, object]] = []
    chat_socket.subscribe(observed.append)

    chat_socket.publish_event(
        kind="chat.reasoning_delta",
        cycle_id="cycle-reasoning",
        episode_id="episode-reasoning",
        pipeline_id=0,
        text="private provider trace",
    )

    assert observed[0]["kind"] == "chat.reasoning_delta"
    assert observed[0]["reasoning_state"] == "in_progress"
    assert "text" not in observed[0]


def test_token_and_tool_frames_keep_the_sink_correlation():
    observed: list[dict[str, object]] = []
    chat_socket.subscribe(observed.append)
    sink = reply_token_sink(
        "cycle-stream",
        "episode-stream",
        pipeline_id=1,
        job_id="job-stream",
        work_kind="operator_turn",
    )

    sink("hello")
    sink.event("chat.tool_call", tool="read_file")

    assert [event["kind"] for event in observed] == [
        "chat.reply_delta", "chat.tool_call"
    ]
    assert all(event["pipeline_id"] == 1 for event in observed)
    assert all(event["job_id"] == "job-stream" for event in observed)
    assert all(event["work_kind"] == "operator_turn" for event in observed)
    assert observed[0]["event_seq"] < observed[1]["event_seq"]


def test_socket_follows_only_jobs_learned_from_its_owned_server_events():
    owner = chat_socket.JobFollowing()
    other = chat_socket.JobFollowing()
    owner.add("origin")
    first = {"kind": "chat.tool_call", "cycle_id": "origin", "job_id": "job-a"}
    assert owner.route(first) == first
    owner.discard("origin")
    continuation = {"kind": "chat.reply_delta", "cycle_id": "continuation", "job_id": "job-a"}
    assert owner.route(continuation) == {**continuation, "job_continuation": True}
    assert other.route(continuation) is None
    assert owner.route({**continuation, "job_id": "job-b"}) is None
    # Following one continuation does not let its unrelated correlation donate a job.
    assert owner.route({"cycle_id": "continuation", "job_id": "job-b"}) is None
    assert chat_socket.JobFollowing().route(continuation) is None  # reconnect uses durable orphan recovery


def test_socket_job_following_is_bounded():
    owner = chat_socket.JobFollowing(max_jobs=2)
    for number in range(3):
        cycle = f"cycle-{number}"
        owner.add(cycle)
        assert owner.route({"cycle_id": cycle, "job_id": f"job-{number}"})
        owner.discard(cycle)
    assert owner.route({"cycle_id": "later", "job_id": "job-0"}) is None
    assert owner.route({"cycle_id": "later", "job_id": "job-2"})


def test_socket_keeps_a_specialist_cycle_open_until_its_terminal_update():
    owner = chat_socket.JobFollowing()
    owner.add("origin")
    acknowledgement = {
        "kind": "chat.reply", "cycle_id": "origin",
        "lane_work": {"lane": "browser", "status": "queued"},
    }
    assert owner.route(acknowledgement) == acknowledgement
    progress = {
        "kind": "chat.update", "cycle_id": "origin", "lane": "browser",
        "status": "progress", "text": "Browser is reading the page.",
    }
    assert owner.route(progress) == progress
    completed = {**progress, "status": "completed", "text": "Browser finished."}
    assert owner.route(completed) == completed
    assert owner.route(progress) is None, "updates after terminal close do not leak to this socket"
