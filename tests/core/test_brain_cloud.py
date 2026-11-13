"""Cloud brain route (operator 2026-07-11, framework-testing mode): a frontier model in
the BRAIN seat. runtime/brain_cloud_config resolves the gitignored guardian-state config
(the portable vault wins over inherited environment credentials); runtime/brain_cloud serves the gateway /generate contract from an
OpenAI-compatible chat.completions endpoint; RuntimeClient routes its three cognition
entry points there when enabled — with NO GPU gate — and stays byte-identical local when
disabled."""

from __future__ import annotations

import json

import pytest

from schema.continuity_json import content_hash

import runtime.brain_cloud as bc
import runtime.brain_cloud_config as bcc
from runtime.client import RuntimeClient


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Never read the live guardian-state config or env from the dev box.
    monkeypatch.setattr(bcc, "_config_path", lambda: tmp_path / "mind_backend.json")
    monkeypatch.setattr(bcc, "_connector_store_root", lambda: tmp_path / "connectors")
    for env in (
        "OMEGA_MIND_BACKEND",
        "OMEGA_MIND_PROVIDER",
        "OMEGA_MIND_MODEL",
        "OMEGA_MIND_BASE_URL",
        "OMEGA_MIND_API_KEY",
        "MISTRAL_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("OMEGA_MIND_API_KEY", "test-key")
    yield


def _write_config(tmp_path, **overrides):
    data = {
        "enabled": True,
        "provider": "mistral",
        "model": "mistral-medium-3-5",
        "base_url": "https://api.mistral.ai/v1",
    }
    data.update(overrides)
    (tmp_path / "mind_backend.json").write_text(json.dumps(data), encoding="utf-8")


# ── config resolution ────────────────────────────────────────────────────────


def test_disabled_by_default_and_env_forces(monkeypatch, tmp_path):
    assert bcc.descriptor() is None  # no file, no env
    _write_config(tmp_path)
    assert bcc.descriptor() is not None
    monkeypatch.setenv("OMEGA_MIND_BACKEND", "local")
    assert bcc.descriptor() is None  # env force OFF beats file enabled
    monkeypatch.setenv("OMEGA_MIND_BACKEND", "cloud")
    monkeypatch.setenv("OMEGA_MIND_MODEL", "leanstral-1.5")
    desc = bcc.descriptor()
    assert desc is not None and desc["model"] == "leanstral-1.5"  # env model override


def test_corrupt_persisted_mind_config_is_an_attributable_failure(monkeypatch, tmp_path):
    """A broken selected-seat file must not turn into an implicit local route."""
    config_path = tmp_path / "mind_backend.json"
    config_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(bcc, "_config_path", lambda: config_path)

    with pytest.raises(bcc.MindBackendConfigError, match="mind_backend_config_unreadable"):
        bcc.backend()


def test_cloud_enabled_does_not_hide_a_corrupt_mind_config(monkeypatch, tmp_path):
    """The cloud adapter must preserve config failures for the boot boundary."""
    config_path = tmp_path / "mind_backend.json"
    config_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(bcc, "_config_path", lambda: config_path)

    with pytest.raises(bcc.MindBackendConfigError, match="mind_backend_config_invalid"):
        bc.enabled()


def test_enabled_but_keyless_is_not_usable(monkeypatch, tmp_path):
    _write_config(tmp_path)
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    monkeypatch.setattr("runtime.brain_cloud_config._dropdown_api_key", lambda _provider: None)
    assert bcc.is_enabled() is True
    assert bcc.descriptor() is None  # would 401 — cognition stays local


def test_selected_cloud_without_credentials_is_not_a_local_fallback(monkeypatch, tmp_path):
    _write_config(tmp_path, backend="cloud")
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    monkeypatch.setattr("runtime.brain_cloud_config._dropdown_api_key", lambda _provider: None)

    with pytest.raises(bcc.MindBackendConfigError, match="cloud_mind_backend_unusable"):
        bc.enabled()


def test_invalid_selected_cloud_descriptor_is_an_attributable_failure(tmp_path):
    _write_config(tmp_path, backend="cloud", model="")

    with pytest.raises(bcc.MindBackendConfigError, match="mind_backend_config_invalid"):
        bcc.descriptor()


def test_unknown_selected_cloud_provider_is_an_attributable_failure(tmp_path):
    _write_config(tmp_path, backend="cloud", provider="not-a-provider")

    with pytest.raises(bcc.MindBackendConfigError, match="mind_backend_config_invalid"):
        bcc.descriptor()


def test_retired_brain_environment_does_not_override_the_mind_seat(monkeypatch, tmp_path):
    _write_config(tmp_path, backend="local", enabled=False)
    monkeypatch.setenv("OMEGA_BRAIN_CLOUD", "1")
    monkeypatch.setenv("OMEGA_BRAIN_MODEL", "retired-model")

    assert bcc.backend() == "local"


def test_retired_mode_field_does_not_select_the_mind_backend(tmp_path):
    (tmp_path / "mind_backend.json").write_text(
        json.dumps({"mode": "cloud", "enabled": True}), encoding="utf-8"
    )

    assert bcc.backend() == "auto"


def test_lmstudio_is_an_unauthenticated_openai_compatible_mind_route(monkeypatch, tmp_path):
    _write_config(
        tmp_path,
        backend="local",
        provider="lmstudio",
        model="operator-selected-model",
        base_url="http://127.0.0.1:1234/v1",
        enabled=True,
    )
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    monkeypatch.setattr("runtime.brain_cloud_config._dropdown_api_key", lambda _provider: None)

    descriptor = bcc.local_descriptor()

    assert descriptor is not None
    assert descriptor["provider"] == "lmstudio"
    assert descriptor["model"] == "operator-selected-model"
    assert descriptor["base_url"] == "http://127.0.0.1:1234/v1"
    assert descriptor["api_key"] == ""


def test_public_state_never_leaks_the_key(monkeypatch, tmp_path):
    _write_config(tmp_path)
    monkeypatch.setenv("OMEGA_MIND_API_KEY", "super-secret")
    state = bcc.public_state()
    assert state["has_key"] is True
    assert "super-secret" not in json.dumps(state)


def test_mistral_key_resolves_from_provider_environment(monkeypatch, tmp_path):
    _write_config(tmp_path)
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    monkeypatch.setenv("MISTRAL_API_KEY", "provider-secret")
    monkeypatch.setattr(
        "runtime.brain_cloud_config._dropdown_api_key",
        lambda _provider: None,
    )

    assert bcc.api_key() == "provider-secret"


def test_mistral_key_resolves_from_encrypted_provider_store(monkeypatch, tmp_path):
    _write_config(tmp_path)
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    seen = []
    monkeypatch.setattr(
        "runtime.brain_cloud_config._dropdown_api_key",
        lambda provider: seen.append(provider) or "encrypted-provider-secret",
    )

    assert bcc.api_key() == "encrypted-provider-secret"
    assert seen == ["mistral"]


def test_portable_vault_key_wins_over_a_stale_provider_environment_key(monkeypatch, tmp_path):
    _write_config(tmp_path)
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    monkeypatch.setenv("MISTRAL_API_KEY", "stale-host-key")
    monkeypatch.setattr(
        "runtime.brain_cloud_config._dropdown_api_key",
        lambda _provider: "portable-vault-key",
    )

    assert bcc.api_key() == "portable-vault-key"


def test_provider_key_store_failure_reaches_the_mind_boundary(monkeypatch, tmp_path):
    """A vault failure is not the same state as a provider without a key."""
    _write_config(tmp_path)
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    failure = bcc.MindBackendConfigError("connector_secret_store_unreadable")
    monkeypatch.setattr(
        "runtime.brain_cloud_config._dropdown_api_key",
        lambda _provider: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(bcc.MindBackendConfigError, match="secret_store_unreadable"):
        bcc.api_key()


def test_catalog_provider_supplies_endpoint_and_key_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")

    state = bcc.configure_connector(
        {
            "backend": "cloud",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "api_key": "gemini-secret",
        }
    )

    assert state["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert bcc.api_key() == "gemini-secret"
    assert bcc.descriptor()["base_url"] == state["base_url"]


def test_catalog_provider_rejects_unknown_cloud_seat(tmp_path):
    with pytest.raises(ValueError, match="cloud_provider_unknown"):
        bcc.configure_connector(
            {
                "backend": "cloud",
                "provider": "not-a-provider",
                "model": "model",
                "base_url": "https://example.test/v1",
                "api_key": "secret",
            }
        )


def test_catalog_provider_requires_custom_endpoint(tmp_path):
    with pytest.raises(ValueError, match="custom_endpoint_required"):
        bcc.configure_connector(
            {
                "backend": "cloud",
                "provider": "foundry",
                "model": "deployment",
                "api_key": "secret",
            }
        )


def test_plaintext_config_key_is_ignored_and_removed_on_write(monkeypatch, tmp_path):
    _write_config(tmp_path, api_key="legacy-plaintext-secret")
    monkeypatch.delenv("OMEGA_MIND_API_KEY")
    monkeypatch.setattr("runtime.brain_cloud_config._dropdown_api_key", lambda _provider: None)

    assert bcc.api_key() == ""
    bcc.set_enabled(True)
    persisted = json.loads((tmp_path / "mind_backend.json").read_text(encoding="utf-8"))
    assert "api_key" not in persisted


# ── payload / response translation ───────────────────────────────────────────


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


def test_generate_completion_translates_both_directions(monkeypatch, tmp_path):
    _write_config(tmp_path)
    captured = {}
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "plugin_plan",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"decision": {"type": "string"}},
                "required": ["decision"],
                "additionalProperties": False,
            },
        },
    }

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None, stream=False):
            captured.update(url=url, body=json, headers=headers)
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "the thought",
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {"name": "web_search", "arguments": "{}"},
                                    },
                                ],
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            )

    monkeypatch.setattr(bc, "_ensure_session", lambda: _Session())

    body = bc.generate_completion(
        {
            "prompt": "think about X",
            "system": "you are omega",
            "max_tokens": 256,
            "temperature": 0.4,
            "top_k": 40,  # llama-native sampler — still dropped
            "chat_template_kwargs": {"enable_thinking": True},  # llama-native — also dropped
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
            "response_format": response_format,
        }
    )

    assert captured["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    sent = captured["body"]
    assert sent["model"] == "mistral-medium-3-5"
    assert sent["messages"][0] == {"role": "system", "content": "you are omega"}
    assert sent["messages"][1] == {"role": "user", "content": "think about X"}
    assert sent["max_tokens"] == 256 and sent["temperature"] == 0.4
    assert "top_k" not in sent  # llama-native sampler, not forwarded
    # ...and chat_template_kwargs obeys the SAME rule, which it used to be the
    # one exception to. It is a llama.cpp/vLLM extension, and a strict OpenAI
    # validator rejects the whole request over an unknown body key: Mistral
    # answered every generate with HTTP 422 extra_forbidden on 2026-08-15, so
    # every cascade failed with no output at all. Opt in per deployment
    # (OMEGA_BRAIN_TEMPLATE_KWARGS=1) for a seat that genuinely accepts it.
    assert "chat_template_kwargs" not in sent
    assert sent["tool_choice"] == "auto"
    assert sent["response_format"] == {"type": "json_object"}

    # Gateway body shape back out — downstream consumers untouched.
    assert body["ok"] is True
    assert body["text"] == "the thought"
    assert body["tool_calls"][0]["function"]["name"] == "web_search"
    assert body["tokens"] == {"prompt": 10, "completion": 5, "total": 15}
    assert body["backend"] == "cloud"


def test_mistral_thinking_chunks_are_separated_from_the_final_answer(
    monkeypatch, tmp_path
):
    _write_config(tmp_path)

    class _Session:
        def post(self, *args, **kwargs):
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {
                                        "type": "thinking",
                                        "thinking": [
                                            {"type": "text", "text": "private reasoning"}
                                        ],
                                    },
                                    {"type": "text", "text": "final answer"},
                                ]
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            )

    monkeypatch.setattr(bc, "_ensure_session", lambda: _Session())

    body = bc.generate_completion({"prompt": "solve", "lane": "meta"})

    assert body["text"] == "final answer"
    assert body["reasoning"] == "private reasoning"


def test_empty_cloud_completion_is_reported_as_a_failed_generation(monkeypatch, tmp_path):
    _write_config(tmp_path)

    class _Session:
        def post(self, *args, **kwargs):
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {"content": None},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 64},
                }
            )

    monkeypatch.setattr(bc, "_ensure_session", lambda: _Session())

    body = bc.generate_completion({"prompt": "answer", "cycle_id": "empty-1"})

    assert body["ok"] is False
    assert body["reason"] == "cloud_empty_completion"
    assert body["finish_reason"] == "length"
    assert body["cycle_id"] == "empty-1"


def test_a_completion_truncated_mid_thought_is_named_separately(monkeypatch, tmp_path):
    """EARLY-V01, 2026-09-21. Two answerless bodies, two causes, two remedies.

    The test above pins the generic refusal: finish_reason 'length' with NO trace stays
    ``cloud_empty_completion``. This one is the shape the trial measured from a live
    endpoint — ``finish_reason: "length"`` WITH a reasoning trace and ``content: null``.
    The ceiling cut the thought off mid-sentence, so the remedy is a higher ceiling, and a
    reader handed "provider returned HTTP success without text" would go looking in the
    wrong place. The trace itself must also survive onto the failure as its evidence.
    """
    _write_config(tmp_path)

    class _Session:
        def post(self, *args, **kwargs):
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                # The other OpenAI-dialect spelling; only
                                # `reasoning_content` used to be read.
                                "reasoning": "I should compare the receipts across the",
                            },
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 64},
                }
            )

    monkeypatch.setattr(bc, "_ensure_session", lambda: _Session())

    body = bc.generate_completion({"prompt": "reflect", "cycle_id": "truncated-1"})

    assert body["ok"] is False
    assert body["reason"] == "cloud_reasoning_budget_exhausted", (
        f"a thought cut off by the token ceiling reported {body['reason']!r}; that is the "
        "generic empty-body cause and points at the provider instead of the ceiling"
    )
    assert "token ceiling" in body["detail"]
    assert body["reasoning"] == "I should compare the receipts across the", (
        "the trace arrived under 'reasoning' and is the only evidence of what the model was "
        "doing when the budget ran out; it must not be dropped"
    )
    assert body["finish_reason"] == "length"
    assert body["cycle_id"] == "truncated-1"


def test_openai_output_text_parts_are_returned_as_answer_text(monkeypatch, tmp_path):
    _write_config(tmp_path)

    class _Session:
        def post(self, *args, **kwargs):
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [{"type": "output_text", "text": "answer"}]
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            )

    monkeypatch.setattr(bc, "_ensure_session", lambda: _Session())

    body = bc.generate_completion({"prompt": "answer"})

    assert body["ok"] is True
    assert body["text"] == "answer"


def test_empty_cloud_completion_reaches_the_guardian_failure_log(monkeypatch, tmp_path):
    from guardian.telemetry import writer

    _write_config(tmp_path)

    class _Session:
        def post(self, *args, **kwargs):
            return _Resp({"choices": [{"message": {"content": None}}]})

    monkeypatch.setattr(bc, "_ensure_session", lambda: _Session())

    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    assert client.generate_completion("answer") is None
    records = [
        json.loads(line)
        for line in writer.FAILURE_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert any(
        row.get("system") == "runtime"
        and row.get("phase") == "generate_completion"
        and row.get("error") == "cloud_empty_completion"
        for row in records
    )


def test_generate_completion_http_error_degrades_quiet(monkeypatch, tmp_path):
    _write_config(tmp_path)

    class _ErrResp:
        status_code = 401
        text = '{"message":"Unauthorized"}'

    class _Session:
        def post(self, *a, **k):
            return _ErrResp()

    monkeypatch.setattr(bc, "_ensure_session", lambda: _Session())
    body = bc.generate_completion({"prompt": "x"})
    assert body["ok"] is False
    assert body["reason"] == "cloud_http_401"


# ── RuntimeClient routing ────────────────────────────────────────────────────


def test_client_routes_generate_to_cloud_without_gpu_gate(monkeypatch, tmp_path):
    _write_config(tmp_path)
    client = RuntimeClient(base_url="http://127.0.0.1:11434")

    def _no_local(*a, **k):
        raise AssertionError("local /generate must not be called in cloud-brain mode")

    def _no_gate(*a, **k):
        raise AssertionError("GPU gate must not be consulted for a cloud generate")

    monkeypatch.setattr(client, "_post", _no_local)
    monkeypatch.setattr(client, "_gpu_clear_to_generate", _no_gate)
    monkeypatch.setattr(
        bc,
        "generate_completion",
        lambda payload, timeout=None: {
            "ok": True,
            "text": "cloud says hi",
            "model": "mistral-medium-3-5",
        },
    )

    assert client.generate("hello") == "cloud says hi"
    full = client.generate_completion("hello")
    assert full["ok"] is True and full["text"] == "cloud says hi"


def test_local_only_completion_stays_on_omega_runtime(monkeypatch, tmp_path):
    """A cloud cognition seat cannot serve a local registered auxiliary model.

    The scaffold composer was this flag's only caller until it was retired
    2026-08-13. The contract outlives it: ``local_only`` is how ANY caller pins
    a completion to the local seat, so a cloud brain never sees data that was
    admitted for local use. Exercised here with a stand-in local model.
    """
    _write_config(tmp_path)
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    calls = {}

    monkeypatch.setattr(
        bc,
        "generate_completion",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a local-only completion must not route to the cloud brain")
        ),
    )
    monkeypatch.setattr(client, "_gpu_clear_to_generate", lambda op: True)

    def _fake_post(path, payload, **kwargs):
        calls.update(path=path, payload=dict(payload))
        return {"ok": True, "text": "{}", "model_id": "local_aux_model"}

    monkeypatch.setattr(client, "_post", _fake_post)
    result = client.generate_completion(
        "compose", model="local_aux_model", local_only=True
    )
    assert result and result["model_id"] == "local_aux_model"
    assert calls["path"] == "/generate"
    assert calls["payload"]["model"] == "local_aux_model"


def test_client_stays_local_when_disabled(monkeypatch, tmp_path):
    # No config written -> route disabled -> the original local path runs (gate + /generate).
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    calls = {}

    def _fake_gate(op):
        calls["gate"] = op
        return True

    def _fake_post(path, payload, **kw):
        calls["path"] = path
        return {"ok": True, "text": "local"}

    monkeypatch.setattr(client, "_gpu_clear_to_generate", _fake_gate)
    monkeypatch.setattr(client, "_post", _fake_post)
    assert client.generate("hello") == "local"
    assert calls == {"gate": "generate", "path": "/generate"}


def test_client_stream_routes_to_cloud(monkeypatch, tmp_path):
    _write_config(tmp_path)
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    monkeypatch.setattr(
        client,
        "_gpu_clear_to_generate",
        lambda op: (_ for _ in ()).throw(AssertionError("no GPU gate on cloud stream")),
    )
    monkeypatch.setattr(
        bc,
        "generate_stream_events",
        lambda payload, timeout=None: iter(
            [("token", "a"), ("token", "b"), ("done", {"finish_reason": "stop"})]
        ),
    )
    assert list(client.generate_stream("hello")) == ["a", "b"]


class _CloudToolStream:
    status_code = 200
    text = ""
    encoding = "utf-8"

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self, *, decode_unicode=False):
        for row in self.rows:
            yield "data: " + json.dumps(row)
        yield "data: [DONE]"


# Mistral's streamed answer to an agent turn: no prose, two parallel calls under one index.
_MISTRAL_AGENT_ROWS = [
    {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
    {"choices": [{"delta": {"tool_calls": [
        {"id": "start1", "index": 0, "function": {"name": "start_job", "arguments": "{\"project_root\": \"todo\"}"}},
        {"id": "write1", "index": 0, "function": {"name": "write_file", "arguments": "{\"path\": \"todo/index.html\"}"}},
    ]}, "finish_reason": "tool_calls"}]},
]


class _CloudToolSession:
    def __init__(self):
        self.body = {}

    def post(self, url, **kwargs):
        self.body = dict(kwargs.get("json") or {})
        return _CloudToolStream(_MISTRAL_AGENT_ROWS)


def test_cloud_stream_events_carry_tool_calls_and_keep_parallel_calls_apart(monkeypatch, tmp_path):
    """Live 2026-09-15: the cloud stream yielded content only, so a streamed agent turn on Mistral
    lost every tool call and never built. Parallel calls sharing an index stay separate calls."""
    _write_config(tmp_path)
    session = _CloudToolSession()
    monkeypatch.setattr(bc, "_ensure_session", lambda: session)

    events = list(bc.generate_stream_events({"prompt": "Build a basic todo app", "tools": [{"type": "function"}]}))

    assert session.body["stream"] is True and session.body["tools"] == [{"type": "function"}]
    assert events == [
        ("tool_call", {"index": 0, "id": "start1", "name": "start_job", "arguments": "{\"project_root\": \"todo\"}"}),
        ("tool_call", {"index": 1, "id": "write1", "name": "write_file", "arguments": "{\"path\": \"todo/index.html\"}"}),
        # WO-05 step 1: the done frame carries the digest of the exact body posted.
        ("done", {"finish_reason": "tool_calls", "model_id": "mistral-medium-3-5",
                  "request_digest": content_hash(session.body)}),
    ]


def test_streamed_operator_pass_on_a_cloud_seat_returns_whole_tool_calls(monkeypatch, tmp_path):
    """The path a chat-panel agent turn takes: a streamed, tool-carrying pass on the cloud seat."""
    _write_config(tmp_path)
    monkeypatch.setattr(bc, "_ensure_session", lambda: _CloudToolSession())
    client = RuntimeClient(base_url="http://127.0.0.1:11434")

    body = client.generate_completion_stream(
        "",
        messages=[{"role": "user", "content": "Build a basic todo app"}],
        tools=[{"type": "function", "function": {"name": "start_job"}}],
        tool_choice="auto",
    )

    assert body["finish_reason"] == "tool_calls"
    assert [call["function"]["name"] for call in body["tool_calls"]] == ["start_job", "write_file"]
    assert body["tool_calls"][0]["function"]["arguments"] == "{\"project_root\": \"todo\"}"


def test_chat_template_kwargs_is_opt_in_per_deployment(monkeypatch):
    """Default OFF so one unknown key cannot take cognition down; on when asked."""
    from runtime import brain_cloud

    payload = {
        "prompt": "think about X",
        "chat_template_kwargs": {"enable_thinking": False},
    }

    monkeypatch.delenv("OMEGA_BRAIN_TEMPLATE_KWARGS", raising=False)
    assert "chat_template_kwargs" not in brain_cloud._chat_body(payload, "m")

    monkeypatch.setenv("OMEGA_BRAIN_TEMPLATE_KWARGS", "1")
    assert brain_cloud._chat_body(payload, "m")["chat_template_kwargs"] == {
        "enable_thinking": False
    }


# ── pooled-connection hygiene ────────────────────────────────────────────────


def test_idle_pool_is_retired_before_it_can_blackhole(monkeypatch):
    """A reused DEAD keep-alive socket costs a full read timeout.

    Live 2026-08-15: the appliance idled ~7 minutes, then an operator build drew
    two dead sockets from the pool. Send-Q froze at 9236/11042 bytes with 23 and
    16 retransmissions and rto backed off to its 120s maximum, while a fresh
    connection served an 11KB body in 1.5s. Two lane episodes were held open for
    the entire 600s cap by sockets that had been dead for minutes.
    """
    import time as _time

    bc._session = None
    bc._last_request_at = 0.0

    first = bc._ensure_session()
    assert bc._ensure_session() is first  # hot path reuses

    bc._last_request_at = _time.monotonic() - (bc._IDLE_RECYCLE_S + 5)
    assert bc._ensure_session() is not first  # gone stale -> rebuilt


def test_session_retries_connects_but_never_replays_a_read():
    """A failed CONNECT sent nothing, so retry it. A read retry would re-send a
    prompt the model may already be answering — a duplicated generate is worse
    than an honest failure."""
    bc._session = None
    bc._last_request_at = 0.0
    adapter = bc._ensure_session().get_adapter("https://api.mistral.ai")

    assert adapter.max_retries.connect == 2
    assert adapter.max_retries.read == 0


def test_tcp_keepalive_is_installed_for_peers_that_die_mid_generate():
    """Idle recycling cannot see a peer that dies DURING a long generate."""
    import socket

    from urllib3.connection import HTTPConnection

    bc._session = None
    bc._last_request_at = 0.0
    bc._ensure_session()

    opts = HTTPConnection.default_socket_options
    assert any(o[1] == socket.SO_KEEPALIVE and o[2] == 1 for o in opts)


@pytest.mark.parametrize("failure,reason", [
    ("http", "cloud_http_503"), ("provider", "cloud_stream_error"),
    ("eof", "cloud_missing_done"), ("transport", "cloud_transport"),
])
def test_actual_cloud_stream_failure_preserves_request_owner(monkeypatch, tmp_path, failure, reason):
    from types import SimpleNamespace
    from runtime.client import RuntimeStreamError
    import requests

    _write_config(tmp_path)
    class Response:
        status_code = 503 if failure == "http" else 200
        text = "provider failed"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def iter_lines(self, **kwargs):
            if failure == "transport":
                raise requests.ConnectionError("lost transport")
            return iter(['data: {"error":{"message":"provider failed"}}'] if failure == "provider" else [])
    sent: dict = {}

    def post(*_args, **kwargs):
        sent["body"] = kwargs.get("json")
        return Response()

    monkeypatch.setattr(bc, "_ensure_session", lambda: SimpleNamespace(post=post))
    context = {"pipeline_id": 2, "cycle_id": "continuation", "job_id": "origin", "work_kind": "autonomous"}
    with pytest.raises(RuntimeStreamError) as captured:
        list(bc.generate_stream_events({"prompt": "work", **context}))
    assert captured.value.reason == reason
    # The owner survives, and so does the identity of the request that was sent (WO-05 step 1).
    assert captured.value.context == {**context, "request_digest": content_hash(sent["body"])}
    if failure == "transport":
        assert isinstance(captured.value.__cause__, requests.ConnectionError)


def test_cloud_sse_decodes_utf8_punctuation_even_when_endpoint_defaults_to_latin1(
    monkeypatch, tmp_path
):
    _write_config(tmp_path)

    class Response:
        status_code = 200
        text = ""
        encoding = "ISO-8859-1"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self, *, decode_unicode=False):
            assert decode_unicode is True
            row = {
                "choices": [{"delta": {"content": "I’m here now—what can I help you with?"}}]
            }
            # requests decodes text/event-stream with the response encoding when
            # the adapter does not set the UTF-8 SSE encoding explicitly.
            yield ("data: " + json.dumps(row, ensure_ascii=False)).encode(
                "utf-8"
            ).decode(self.encoding)
            yield "data: [DONE]"

    sent: dict = {}

    def post(*_args, **kwargs):
        sent["body"] = kwargs.get("json")
        return Response()

    monkeypatch.setattr(bc, "_ensure_session", lambda: type("Session", (), {"post": staticmethod(post)})())

    assert list(bc.generate_stream_events({"prompt": "hello"})) == [
        ("token", "I’m here now—what can I help you with?"),
        ("done", {"finish_reason": "stop", "model_id": "mistral-medium-3-5",
                  "request_digest": content_hash(sent["body"])}),
    ]

def test_tool_transcript_is_preserved_for_cloud_brain(monkeypatch, tmp_path):
    _write_config(tmp_path)
    captured = {}

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None, stream=False):
            captured["body"] = json
            return _Resp({"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(bc, "_ensure_session", lambda: _Session())
    turns = [
        {"role": "system", "content": "bounded"},
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_files", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_files", "content": "README"},
    ]

    bc.generate_completion(
        {
            "prompt": "",
            "system": "must not duplicate",
            "messages": turns,
            "tools": [{"type": "function", "function": {"name": "read_files"}}],
            "tool_choice": "auto",
            "seed": 11,
        }
    )

    assert captured["body"]["messages"] == turns
    assert captured["body"]["tool_choice"] == "auto"
    assert captured["body"]["random_seed"] == 11
