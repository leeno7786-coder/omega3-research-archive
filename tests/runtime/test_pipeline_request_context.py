from __future__ import annotations

from typing import Any

import pytest

from runtime.client import RuntimeClient
from runtime.socket import pipeline_context_for_episode


@pytest.mark.parametrize("pipeline_id", [True, False, -1, 4, 1.0, "1"])
def test_generate_completion_rejects_non_physical_pipeline_ids(pipeline_id):
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    with pytest.raises(ValueError, match="pipeline_id"):
        client.generate_completion("hi", pipeline_id=pipeline_id)


def test_generate_completion_serializes_only_supplied_pipeline_context(monkeypatch):
    seen: dict[str, Any] = {}

    def fake(self, payload, *, op, timeout=None, local_only=False):
        seen.update(payload)
        return {"ok": True, "text": "done", **{key: payload[key] for key in (
            "pipeline_id", "work_kind", "cycle_id", "job_id"
        )}}

    monkeypatch.setattr(RuntimeClient, "_cloud_or_local_generate", fake)
    body = RuntimeClient(base_url="http://127.0.0.1:11434").generate_completion(
        "hi",
        pipeline_id=1,
        work_kind="operator_turn",
        cycle_id="cycle-1",
        job_id="job-1",
    )

    assert {key: seen[key] for key in ("pipeline_id", "work_kind", "cycle_id", "job_id")} == {
        "pipeline_id": 1,
        "work_kind": "operator_turn",
        "cycle_id": "cycle-1",
        "job_id": "job-1",
    }
    assert body["pipeline_id"] == 1
    assert "lane" not in seen


class _StreamResponse:
    status_code = 200
    text = ""

    def __init__(self, lines):
        self.lines = lines

    def iter_lines(self, decode_unicode=False):
        yield from self.lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Session:
    def __init__(self, lines):
        self.lines = lines
        self.body = None

    def post(self, url, json=None, **kwargs):
        self.body = json
        return _StreamResponse(self.lines)


def test_streamed_result_keeps_original_context(monkeypatch):
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    monkeypatch.setattr(client, "_gpu_clear_to_generate", lambda op: True)
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: False)
    lines = [
        "event: token",
        'data: {"token":"ok","pipeline_id":2,"work_kind":"autonomous","cycle_id":"c","job_id":"j"}',
        "",
        "event: done",
        'data: {"finish_reason":"stop","pipeline_id":2,"work_kind":"autonomous","cycle_id":"c","job_id":"j"}',
        "",
    ]
    session = _Session(lines)
    monkeypatch.setattr(client, "_ensure_session", lambda: session)

    body = client.generate_completion_stream(
        "hi", pipeline_id=2, work_kind="autonomous", cycle_id="c", job_id="j"
    )

    assert body["text"] == "ok"
    assert {key: body[key] for key in ("pipeline_id", "work_kind", "cycle_id", "job_id")} == {
        "pipeline_id": 2,
        "work_kind": "autonomous",
        "cycle_id": "c",
        "job_id": "j",
    }
    assert session.body["pipeline_id"] == 2


def test_brain_cloud_stream_normalization_keeps_original_context(monkeypatch):
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: True)
    seen = {}

    def stream(payload, timeout=None):
        seen.update(payload)
        yield "token", "cloud-token"
        yield "done", {"finish_reason": "stop", "model_id": "cloud-model"}

    monkeypatch.setattr("runtime.brain_cloud.generate_stream_events", stream)

    events = list(
        client._stream_events(
            {
                "prompt": "hi",
                "pipeline_id": 3,
                "work_kind": "reflection",
                "cycle_id": "cloud-cycle",
                "job_id": "cloud-job",
            }
        )
    )

    expected = {
        "pipeline_id": 3,
        "work_kind": "reflection",
        "cycle_id": "cloud-cycle",
        "job_id": "cloud-job",
    }
    assert events == [
        ("token", {"token": "cloud-token", **expected}),
        ("done", {"finish_reason": "stop", "model_id": "cloud-model", **expected}),
    ]
    assert seen["pipeline_id"] == 3


def test_brain_cloud_echoes_context_without_sending_it_to_hosted_api(monkeypatch):
    from runtime import brain_cloud, brain_cloud_config

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }

    class Session:
        body = None

        def post(self, url, json=None, **kwargs):
            self.body = json
            return Response()

    session = Session()
    monkeypatch.setattr(
        brain_cloud_config,
        "descriptor",
        lambda: {"base_url": "https://hosted.invalid/v1", "model": "mistral", "api_key": "secret"},
    )
    monkeypatch.setattr(brain_cloud, "_ensure_session", lambda: session)
    context = {"pipeline_id": 0, "work_kind": "reflection", "cycle_id": "c0", "job_id": "j0"}

    body = brain_cloud.generate_completion({"prompt": "hi", **context})

    assert {key: body[key] for key in context} == context
    assert not (set(context) & set(session.body))
    assert "api_key" not in session.body


def test_episode_context_uses_owning_pipeline_and_original_operator_contributor():
    class Episode:
        episode_id = "episode-1"
        pipeline_id = 3
        work_kind = "operator_turn"
        pressure = {
            "contributors": [
                {"id": "curiosity_gap", "detail": {"cycle_id": "wrong", "job_id": "wrong"}},
                {
                    "id": "operator_query",
                    "detail": {"cycle_id": "operator-cycle", "job_id": "operator-job"},
                },
            ]
        }

    assert pipeline_context_for_episode(Episode()) == {
        "pipeline_id": 3,
        "work_kind": "operator_turn",
        "cycle_id": "operator-cycle",
        "job_id": "operator-job",
    }


def test_local_stream_error_keeps_original_request_context(monkeypatch):
    from runtime.client import RuntimeStreamError

    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    monkeypatch.setattr(client, "_gpu_clear_to_generate", lambda op: True)
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: False)
    session = _Session(
        [
            "event: error",
            'data: {"reason":"worker_error","detail":"busy","pipeline_id":0}',
            "",
        ]
    )
    monkeypatch.setattr(client, "_ensure_session", lambda: session)
    context = {
        "pipeline_id": 2,
        "work_kind": "agent_continuation",
        "cycle_id": "local-error-cycle",
        "job_id": "local-error-job",
    }

    with pytest.raises(RuntimeStreamError) as raised:
        list(client._stream_events({"prompt": "hi", **context}))

    assert raised.value.context == context


def test_actual_brain_cloud_stream_error_keeps_original_context(monkeypatch):
    from runtime import brain_cloud, brain_cloud_config
    from runtime.client import RuntimeStreamError

    class CloudResponse(_StreamResponse):
        def __init__(self):
            super().__init__(['data: {"error":{"message":"provider failed"}}'])

    class CloudSession:
        body = None

        def post(self, url, json=None, **kwargs):
            self.body = json
            return CloudResponse()

    session = CloudSession()
    monkeypatch.setattr(
        brain_cloud_config,
        "descriptor",
        lambda: {"base_url": "https://hosted.invalid/v1", "model": "mistral", "api_key": "secret"},
    )
    monkeypatch.setattr(brain_cloud, "_ensure_session", lambda: session)
    monkeypatch.setattr(brain_cloud, "enabled", lambda: True)
    context = {
        "pipeline_id": 1,
        "work_kind": "operator_turn",
        "cycle_id": "cloud-error-cycle",
        "job_id": "cloud-error-job",
    }

    with pytest.raises(RuntimeStreamError) as raised:
        list(RuntimeClient()._stream_events({"prompt": "hi", **context}))

    from schema.continuity_json import content_hash

    # The failed stream keeps its pipeline context AND the identity of the body it actually
    # posted (WO-05 step 1: a request that failed is still a request that was sent).
    assert raised.value.context == {**context, "request_digest": content_hash(session.body)}
    assert not (set(context) & set(session.body))
