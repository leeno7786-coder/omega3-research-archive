from __future__ import annotations

from runtime import vision


class FakeRuntimeClient:
    def __init__(self, response, *, now=None):
        self.response = response
        self.calls = []
        self.now = now

    def generate_completion(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.now is not None:
            self.now[0] += 11.0
        return self.response


def test_vision_describe_uses_the_local_multimodal_seat_and_lane_admission(monkeypatch) -> None:
    client = FakeRuntimeClient({"ok": True, "text": "A local scene description."})
    monkeypatch.setattr(vision, "_selected_backend", lambda: ("local", None))
    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: client)

    result = vision.describe_image(
        b"jpeg", prompt="What is visible?", image_format="jpg",
        pipeline_id=0, work_kind="operator_lane_work", cycle_id="vision-cycle",
    )

    assert result == "A local scene description."
    prompt, request = client.calls[0]
    assert prompt == ""
    assert request["messages"][0]["content"][0] == {
        "type": "text",
        "text": "What is visible?",
    }
    image_part = request["messages"][0]["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert request["timeout"] == 30.0
    assert request["local_only"] is True
    assert request["pipeline_id"] == 0
    assert request["work_kind"] == "operator_lane_work"
    assert request["cycle_id"] == "vision-cycle"


def test_vision_describe_uses_the_cloud_multimodal_seat(monkeypatch) -> None:
    client = FakeRuntimeClient({"ok": True, "text": "A cloud scene description."})
    monkeypatch.setattr(
        vision,
        "_selected_backend",
        lambda: ("cloud", {"provider": "mistral", "model": "pixtral"}),
    )
    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: client)

    result = vision.describe_image(b"png", prompt="Read the scene.", image_format="png")

    assert result == "A cloud scene description."
    assert len(client.calls) == 1
    assert client.calls[0][1]["timeout"] == 30.0
    assert client.calls[0][1]["local_only"] is False
    assert client.calls[0][1]["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_vision_tool_socket_keeps_the_active_prediction_lane(monkeypatch) -> None:
    observed = {}

    def describe(_image_bytes, **kwargs):
        observed.update(kwargs)
        return "socket scene description"

    monkeypatch.setattr("runtime.vision.describe_image", describe)
    from runtime.socket import invoke_runtime_tool

    result = invoke_runtime_tool(
        "vision_reasoning_tool",
        operation="tool_vision_reasoning_tool",
        episode_id="vision-local-dispatch",
        image_bytes=b"image",
        prompt="Describe it.",
        image_format="jpeg",
        pipeline_id=1,
        work_kind="operator_lane_work",
        cycle_id="vision-cycle",
        job_id="job-1",
    )

    assert result == {
        "ok": True,
        "text": "socket scene description",
        "backend": "mind",
    }
    assert {
        key: observed[key]
        for key in ("pipeline_id", "work_kind", "cycle_id", "job_id")
    } == {
        "pipeline_id": 1,
        "work_kind": "operator_lane_work",
        "cycle_id": "vision-cycle",
        "job_id": "job-1",
    }


def test_vision_clamps_its_provider_timeout_to_the_turn_budget(monkeypatch) -> None:
    from pressure_loop import turn_deadline as td

    now = [1_000.0]
    monkeypatch.setattr(td, "_now", lambda: now[0])
    td.reset_for_tests()
    try:
        td.start("cyc-vision", budget_s=10.0)
        now[0] += 6.0
        client = FakeRuntimeClient(
            {"ok": True, "text": "The remaining four seconds were passed."}
        )
        monkeypatch.setattr(vision, "_selected_backend", lambda: ("local", None))
        monkeypatch.setattr("runtime.client.get_runtime_client", lambda: client)

        result = vision.describe_image(
            b"jpeg", cycle_id="cyc-vision", timeout=30.0
        )

        assert result == "The remaining four seconds were passed."
        assert client.calls[0][1]["timeout"] == 4.0
    finally:
        td.reset_for_tests()


def test_vision_provider_result_after_deadline_is_rejected(monkeypatch) -> None:
    import pytest

    from pressure_loop import turn_deadline as td

    now = [1_000.0]
    monkeypatch.setattr(td, "_now", lambda: now[0])
    td.reset_for_tests()
    try:
        td.start("cyc-vision", budget_s=10.0)
        client = FakeRuntimeClient(
            {"ok": True, "text": "too late"}, now=now
        )
        monkeypatch.setattr(vision, "_selected_backend", lambda: ("local", None))
        monkeypatch.setattr("runtime.client.get_runtime_client", lambda: client)

        with pytest.raises(td.TurnDeadlineExpired) as caught:
            vision.describe_image(b"jpeg", cycle_id="cyc-vision")

        assert caught.value.stage == "vision"
    finally:
        td.reset_for_tests()
