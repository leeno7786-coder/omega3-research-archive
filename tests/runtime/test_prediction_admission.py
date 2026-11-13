"""Runtime inference admission is four-wide and serialized within each lane."""

from queue import Queue
import threading

from runtime.client import RuntimeClient


def test_four_lanes_overlap_and_a_second_prediction_in_one_lane_waits(monkeypatch):
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    started = Queue()
    releases = [threading.Event() for _ in range(5)]
    call_ids = iter(range(5))
    call_lock = threading.Lock()

    def fake_generate(payload, **_kwargs):
        with call_lock:
            call_id = next(call_ids)
        lane_id = payload["pipeline_id"]
        started.put((call_id, lane_id))
        assert releases[call_id].wait(3)
        return {"ok": True, "text": "done"}

    monkeypatch.setattr(client, "_cloud_or_local_generate", fake_generate)
    lanes = (0, 1, 2, 3, 0)
    workers = [
        threading.Thread(
            target=client.generate,
            args=(f"request-{index}",),
            kwargs={"pipeline_id": lane},
        )
        for index, lane in enumerate(lanes)
    ]
    for worker in workers:
        worker.start()

    first_four = [started.get(timeout=1) for _ in range(4)]
    assert {lane_id for _, lane_id in first_four} == {0, 1, 2, 3}
    assert started.empty(), "the second prediction in Chat must wait for Chat's first"

    chat_call = next(call_id for call_id, lane_id in first_four if lane_id == 0)
    releases[chat_call].set()
    fifth_call = started.get(timeout=1)
    assert fifth_call[1] == 0
    for release in releases:
        release.set()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_specialist_lane_work_kind_reaches_its_runtime_prediction(monkeypatch):
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    captured = {}

    def fake_generate(payload, **_kwargs):
        captured.update(payload)
        return {"ok": True, "text": "specialist prediction"}

    monkeypatch.setattr(client, "_cloud_or_local_generate", fake_generate)

    result = client.generate(
        "continue browser task",
        pipeline_id=1,
        work_kind="operator_lane_work",
        cycle_id="specialist-cycle",
    )

    assert result == "specialist prediction"
    assert captured["pipeline_id"] == 1
    assert captured["work_kind"] == "operator_lane_work"
    assert captured["cycle_id"] == "specialist-cycle"
