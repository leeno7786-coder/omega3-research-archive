"""Slice 3 B6 — execution adapters (fake runtime + tool invoker)."""

from __future__ import annotations

import json
import sys
import pytest
from types import SimpleNamespace

from auto_loop.auto import (
    MemoryEnvelope,
    PromptPack,
    build_prompt_pack,
    execute as _execute,
    tool_call_is_direct,
)
from guardian import terminal_feedback as tf


@pytest.fixture(autouse=True)
def _authorized_operator_tool_state(tmp_path, monkeypatch):
    """Give this module's write-path tests an isolated explicit grant.

    The product default is read-only. These tests exercise the already
    authorized operator execution surface, so they must not depend on a
    permission file left by a prior live run.
    """
    from auto_loop.execute import chat_permissions

    monkeypatch.setattr(chat_permissions, "CHAT_PERMISSION_PATH", tmp_path / "permission-mode.json")
    chat_permissions.set_chat_permission_mode("always-allow")


def execute(*args, **kwargs):
    """Direct AUTO tests supply the physical owner normally installed by pressure."""
    if str(kwargs.get("opened_by") or "").startswith("operator"):
        from core import pipeline_state
        pipeline_id = kwargs.setdefault("pipeline_id", 0)
        kwargs.setdefault("work_kind", "operator_turn")
        cycle_id = kwargs.get("chat_cycle_id") or kwargs.get("request_cycle_id") or args[4]
        pipeline_state._write(pipeline_id, state="INFERENCE", cycle_id=cycle_id, work_kind="operator_turn")
    return _execute(*args, **kwargs)


def _job_reply(generate, **kwargs):
    from auto_loop.execute.run import _operator_tools_reply
    return _operator_tools_reply(generate, system="sys", prompt="build", objective="build",
                                 episode_id="ep-job", max_tokens=100, enable_thinking=False,
                                 timeout=None, chat_cycle_id="origin", warnings=[],
                                 arrival_seq=1,
                                 permission_mode="always_allow", **kwargs)


def _job_call(name, args=None, call_id="call"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


def test_global_playback_interrupt_does_not_cancel_cognition(tmp_path, monkeypatch):
    from auto_loop.execute import brain_skill_executor as executor
    from pressure_loop import operator_chat_responses as replies
    monkeypatch.setattr(executor, "operator_workspace_jail", lambda: tmp_path)
    monkeypatch.setattr(replies, "interrupt_requested", lambda cycle: True)
    result, _, stop = _job_reply(lambda **kwargs: {"text": "answer"})
    assert result["text"] == "answer"
    assert stop == ""


def test_other_active_turn_is_only_system_context(tmp_path, monkeypatch):
    from auto_loop.execute import brain_skill_executor as executor
    from core import pipeline_state as state
    monkeypatch.setattr(executor, "operator_workspace_jail", lambda: tmp_path)
    state._write(2, state="INFERENCE", cycle_id="other", work_kind="operator_turn")
    seen = []
    def generate(**kw):
        seen.extend(kw["messages"])
        return {"text": "answer"}
    _job_reply(generate)
    assert "other" in seen[0]["content"]
    assert [r["role"] for r in seen] == ["system", "user"]


def test_actual_streamed_auto_failure_settles_original_turn_without_regeneration(tmp_path, monkeypatch):
    from runtime.client import RuntimeClient, RuntimeStreamError
    from pressure_loop import cognition_queue as queue
    from auto_loop.execute import brain_skill_executor as executor
    monkeypatch.setattr(executor, "operator_workspace_jail", lambda: tmp_path)
    seq = queue.reserve_operator_arrival("failed-cycle")
    from pressure_loop.contributors import make_contributor
    queue.enqueue_input(make_contributor("operator", "operator_query", 1.0,
                        {"cycle_id": "failed-cycle", "arrival_seq": seq, "message": "hello"}),
                        operator_activity=False, admission_cycle_id="failed-cycle")
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    def events(payload, **kwargs):
        raise RuntimeStreamError("transport", "original transport cause", context={k: payload[k] for k in ("pipeline_id", "cycle_id", "work_kind")})
        yield
    monkeypatch.setattr(client, "_stream_events", events)
    def no_regeneration(**kwargs):
        raise AssertionError("unexpected blocking regeneration")
    monkeypatch.setattr(client, "generate_completion", no_regeneration)
    def invoke(*, operation, episode_id, client, method="generate_completion", **kwargs):
        return getattr(client, method)(**kwargs)
    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", invoke)
    import importlib
    monkeypatch.setattr(importlib.import_module("auto_loop.execute.run"), "response_streaming_enabled", lambda: True)
    res = execute(_operator_pp("hello"), "TEXT_FAST", _pack(), _mem(), "ep-fail",
                  runtime_client=client, opened_by="operator_query", chat_cycle_id="failed-cycle",
                  arrival_seq=seq, pipeline_id=0, request_cycle_id="failed-cycle", work_kind="operator_turn")
    assert res["failure_kind"]
    assert "original transport cause" in res["failure_reason"]
    assert queue.earlier_operator_turns_interpreted(seq + 1)


class FakeRuntime:
    def __init__(
        self,
        *,
        proceed=True,
        text="answer",
        tool_calls=None,
        image=b"bytes",
    ):
        self.proceed = proceed
        self.calls = []
        self._text = text
        self._tool_calls = tool_calls or []
        self._image = image

    def health_body(self):
        return {
            "status": "ok",
            "capabilities": {
                "coding_model": self.proceed,
                "image_generation": self.proceed,
            },
        }

    def generate_completion(self, **kw):
        self.calls.append(("generate", kw))
        if not self.proceed:
            return None
        return {
            "text": self._text,
            "tool_calls": self._tool_calls,
            "tokens_in": 1,
            "tokens_out": 2,
        }

    def image_generate(self, **kw):
        self.calls.append(("image", kw))
        return self._image if self.proceed else None

    def generate_completion_stream(self, *, on_token=None, on_reasoning=None, **kw):
        body = self.generate_completion(**kw)
        if body is not None and on_token is not None and body.get("text"):
            on_token(body["text"])
        return body


def _answer_generate(runtime):
    """The response inference is the generate without an auxiliary seat override."""
    return [
        kw
        for op, kw in runtime.calls
        if op == "generate" and not kw.get("model")
    ][0]


def _mem():
    return MemoryEnvelope(
        memory_required=False,
        memory_status="skipped",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )


def _pack(**kw):
    base = {
        "action": "RESPONSE",
        "tool": None,
        "execution_inputs": {},
        "source_action_goal": {},
    }
    base.update(kw)
    return base


def test_plugin_execute_branch_populates_the_chat_response(monkeypatch) -> None:
    outcome = SimpleNamespace(
        status="completed",
        plan=None,
        step_results=(),
        permission_request=None,
        failure_reason=None,
        plugin_result={"data": {"id": "event-1"}},
    )
    monkeypatch.setattr(
        "auto_loop.execute.plugin_plan.plan_and_execute_plugin_request",
        lambda *args, **kwargs: outcome,
    )

    runtime = FakeRuntime(text="The connected calendar returned event-1.")
    result = execute(
        _operator_pp("show my calendar"),
        "TEXT_FAST",
        _pack(
            action="PLUGIN_OPERATION",
            execution_inputs={"plugin_objective": "show my calendar"},
        ),
        _mem(),
        "ep-plugin-response",
        runtime_client=runtime,
        opened_by="operator_query",
        chat_cycle_id="chat-plugin-response",
    )

    assert result["failure_kind"] is None
    assert result["outputs"]["response_text"] == "The connected calendar returned event-1."
    assert '"id": "event-1"' in runtime.calls[0][1]["prompt"]
    assert runtime.calls[0][1].get("tools") is None


def test_plugin_execution_ignores_chat_tool_permission_mode(monkeypatch) -> None:
    seen: dict[str, object] = {}
    outcome = SimpleNamespace(
        status="completed",
        plan=None,
        step_results=(),
        permission_request=None,
        failure_reason=None,
        plugin_result={"data": {"id": "event-2"}},
    )

    def plan(*args, **kwargs):
        seen.update(kwargs)
        return outcome

    monkeypatch.setattr(
        "auto_loop.execute.plugin_plan.plan_and_execute_plugin_request",
        plan,
    )

    result = execute(
        _operator_pp("create the approved calendar event"),
        "TEXT_FAST",
        _pack(
            action="PLUGIN_OPERATION",
            execution_inputs={"plugin_objective": "create the approved calendar event"},
        ),
        _mem(),
        "ep-plugin-settings-authority",
        runtime_client=FakeRuntime(),
        opened_by="operator_query",
        chat_cycle_id="chat-plugin-settings-authority",
    )

    assert result["failure_kind"] is None
    assert "permission_mode" not in seen
    assert seen["active_contributor"] == {"source": "operator", "type": "command"}


def test_response_action_generates_terminal_reply():
    """A terminal RESPONSE (reasoning/question/freeform) generates its reply AT AUTO
    after engineer composition; the cognition_chain gate then skips Omega fold-back."""
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_REASONING",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="hello",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="reasoning",
        tool_call_spec=None,
        image_spec=None,
    )
    res = execute(pp, "TEXT_REASONING", _pack(), _mem(), "ep_1", runtime_client=rt)
    assert res["outputs"].get("response_text")
    assert any(c[0] == "generate" for c in rt.calls)


def test_response_generation_failure_resolves_the_chat_with_the_real_error():
    """A failed brain call is an AUTO result, never an empty hanging bubble."""
    pp = PromptPack(
        prompt_type="RESPONSE_FREEFORM",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="hello",
        context_blocks={},
        response_mode="freeform",
        tool_call_spec=None,
        image_spec=None,
    )

    result = execute(
        pp,
        "TEXT_FAST",
        _pack(),
        _mem(),
        "ep-response-failed",
        runtime_client=FakeRuntime(proceed=False),
    )

    assert result["failure_kind"] == "runtime_unavailable"
    assert result["outputs"]["response_text"] == (
        "I could not generate a response: response generation preflight declined."
    )
    assert result["outputs"]["deterministic_response_kind"] == "response_failure"


def test_operator_skill_result_resolves_the_same_chat_bubble(monkeypatch):
    """A build reports through chat at AUTO instead of a second agent notice."""
    captured: dict = {}
    from pressure_loop import operator_chat_responses as replies

    monkeypatch.setattr(replies, "interrupt_requested", lambda cycle: True)
    monkeypatch.setattr(replies, "work_halt_requested", lambda cycle: False)

    def _invoke(directive, dispatch, *, should_stop=None, **kwargs):
        captured["skill_should_stop"] = should_stop()
        return {"status": "success", "summary": "Created the requested project and its tests pass."}

    monkeypatch.setattr("auto_loop.skill.run.default_brain_skill_invoker", _invoke)

    def _dispatch(pack, **kwargs):
        captured["runtime_client"] = kwargs["invoker"].__closure__ is not None
        kwargs["invoker"](object(), {})
        captured["request_context"] = {
            key: kwargs[key]
            for key in ("pipeline_id", "work_kind", "cycle_id", "job_id")
        }
        return {
            "status": "executed",
            "dispatch_id": pack.dispatch_id,
            "reason": "",
            "guard_status": {"allowed": True},
            "qwen_result": {
                "status": "success",
                "summary": "Created the requested project and its tests pass.",
                "files_changed": [],
            },
        }

    monkeypatch.setattr("auto_loop.skill.run.dispatch_skill_pack", _dispatch)
    skill_pack = {
        "episode_id": "ep-skill",
        "dispatch_id": "dispatch-skill",
        "objective": "build the project",
        "selected_skill": {},
        "allowed_tools": [],
        "forbidden_tools": [],
        "safety": {},
        "limits": {},
        "evidence_required": [],
        "completion_criteria": [],
        "expected_output_schema": {},
        "source_action_goal": {"opened_by": "operator_command"},
    }
    pp = PromptPack(
        prompt_type="SKILL_EXECUTION",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="build the project",
        context_blocks={},
        response_mode=None,
        tool_call_spec=None,
        image_spec=None,
    )

    result = execute(
        pp,
        None,
        _pack(action="USE_SKILL", execution_inputs={"skill_execution_pack": skill_pack}),
        _mem(),
        "ep-skill",
        runtime_client=FakeRuntime(),
        opened_by="operator_command",
        chat_cycle_id="cycle-skill",
        pipeline_id=0,
        work_kind="operator_turn",
        request_cycle_id="operator-cycle",
        job_id="operator-job",
    )

    assert captured["runtime_client"] is True
    assert captured["skill_should_stop"] is False
    assert captured["request_context"] == {
        "pipeline_id": 0,
        "work_kind": "operator_turn",
        "cycle_id": "operator-cycle",
        "job_id": "operator-job",
    }
    assert result["outputs"]["response_text"] == (
        "Created the requested project and its tests pass."
    )
    assert result["outputs"]["deterministic_response_kind"] == "skill_execution_result"
    assert result["model_id"] == "brain_runtime"


def test_skill_dispatch_carries_the_carrier_thread_to_the_brain_invoker(monkeypatch):
    """Cross-lane T7: a job the dispatch opens records this thread, so operator mail sent
    while it runs can be addressed to it. AUTO is the only place the carrier's thread is
    known on this path."""
    captured: dict = {}

    def _dispatch(pack, **kwargs):
        captured["dispatch_thread"] = kwargs["thread_id"]
        kwargs["invoker"]("directive", SimpleNamespace(dispatch_id=pack.dispatch_id))
        return {"status": "executed", "dispatch_id": pack.dispatch_id, "reason": "",
                "guard_status": {"allowed": True},
                "qwen_result": {"status": "success", "summary": "done", "files_changed": []}}

    def _invoker(directive, dispatch, **kwargs):
        captured["invoker_thread"] = kwargs["thread_id"]
        return {"ok": True}

    monkeypatch.setattr("auto_loop.skill.run.dispatch_skill_pack", _dispatch)
    monkeypatch.setattr("auto_loop.skill.run.default_brain_skill_invoker", _invoker)
    skill_pack = {
        "episode_id": "ep-skill", "dispatch_id": "dispatch-skill", "objective": "build it",
        "selected_skill": {}, "allowed_tools": [], "forbidden_tools": [], "safety": {},
        "limits": {}, "evidence_required": [], "completion_criteria": [],
        "expected_output_schema": {}, "source_action_goal": {"opened_by": "operator_command"},
    }
    pp = PromptPack(prompt_type="SKILL_EXECUTION", system_prompt="sys", developer_prompt=None,
                    user_prompt="build it", context_blocks={}, response_mode=None,
                    tool_call_spec=None, image_spec=None)

    execute(pp, None, _pack(action="USE_SKILL", execution_inputs={"skill_execution_pack": skill_pack}),
            _mem(), "ep-skill", runtime_client=FakeRuntime(), opened_by="operator_command",
            chat_cycle_id="cycle-skill", request_cycle_id="operator-cycle", thread_id="t-carrier")

    assert captured == {"dispatch_thread": "t-carrier", "invoker_thread": "t-carrier"}


def test_skill_result_pairs_ledger_snapshot_with_project_history(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from memory import dev_project_store, episode_sram

    captured: dict[str, object] = {}
    episode_id = "ep-dev-memory"
    objective = "build the widget"
    source = tmp_path / "src" / "widget.py"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path))
    source.parent.mkdir()
    source.write_bytes(b"VALUE = 42\n")

    def _run_brain_skill_dispatch(_directive, _dispatch, *, on_event, **_kwargs):
        # The live skill adapter reports touched files as a ledger event. The registered handler
        # snapshots those bytes into the open episode SRAM before AUTO pairs them with history.
        on_event(
            {
                "type": "ledger",
                "data": {"touched_files": [{"path": "src/widget.py"}]},
            }
        )
        return {
            "status": "success",
            "summary": "Created the widget.",
            "files_changed": ["src/widget.py"],
            "evidence": {"all_green": True},
        }

    def _dispatch(pack, *, invoker, **_kwargs):
        qwen_result = invoker(
            "directive",
            SimpleNamespace(episode_id=episode_id, objective=objective),
        )
        return {
            "status": "executed",
            "dispatch_id": pack.dispatch_id,
            "reason": "",
            "guard_status": {"allowed": True},
            "qwen_result": qwen_result,
        }

    original_persist = dev_project_store.persist_project_files

    def _persist(paths, **kwargs):
        changes = original_persist(paths, **kwargs)
        captured.update(paths=list(paths), kwargs=kwargs, changes=changes)
        return changes

    monkeypatch.setattr(
        "auto_loop.execute.brain_skill_executor.run_brain_skill_dispatch",
        _run_brain_skill_dispatch,
    )
    monkeypatch.setattr("auto_loop.skill.run.dispatch_skill_pack", _dispatch)
    monkeypatch.setattr(dev_project_store, "persist_project_files", _persist)
    episode_sram.reset()
    episode_sram.open_bucket(episode_id, pipeline_id=0)
    skill_pack = {
        "episode_id": episode_id,
        "dispatch_id": "dispatch-dev-memory",
        "objective": objective,
        "selected_skill": {},
        "allowed_tools": [],
        "forbidden_tools": [],
        "safety": {},
        "limits": {"max_files_changed": 3},
        "evidence_required": [],
        "completion_criteria": [],
        "expected_output_schema": {},
        "source_action_goal": {"message": "Build the widget app"},
    }
    pp = PromptPack(
        prompt_type="SKILL_EXECUTION",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="build the widget",
        context_blocks={},
        response_mode=None,
        tool_call_spec=None,
        image_spec=None,
    )

    execute(
        pp,
        None,
        _pack(action="USE_SKILL", execution_inputs={"skill_execution_pack": skill_pack}),
        _mem(),
        episode_id,
        runtime_client=FakeRuntime(),
        opened_by="operator_command",
        chat_cycle_id="cycle-dev-memory",
        pipeline_id=0,
        work_kind="operator_turn",
    )

    assert captured["paths"] == ["src/widget.py"]
    assert captured["kwargs"] == {"objective": objective, "episode_id": episode_id}
    changes = captured["changes"]
    assert len(changes) == 1
    assert changes[0].project == "build-the-widget"
    assert changes[0].relpath == "src/widget.py"
    assert changes[0].before_content is None
    assert changes[0].after_content == "VALUE = 42\n"
    held = [
        item
        for item in episode_sram.pending(episode_id)
        if getattr(item.envelope, "type", None) == "dev_artifact_snapshot"
    ]
    assert len(held) == 1
    episode_sram.reset()


def test_validation_failure_keeps_the_folded_auto_contract():
    from types import SimpleNamespace

    from auto_loop.loop import run_auto

    episode_id = "ep-invalid-pack"
    pack = {
        "episode_id": episode_id,
        "action": "USE_TOOL",
        "source_action_goal": {"episode_id": episode_id},
    }
    episode = SimpleNamespace(
        episode_id=episode_id,
        pressure={},
        global_={},
        meta={"execution_action_pack": pack},
        auto=None,
    )
    outcome = run_auto(episode)

    assert outcome is not None
    assert episode.auto["execution_pack"] == pack
    assert episode.auto["failure_kind"] == "invalid_execution_pack"
    assert episode.auto["status"] == "failed"


def test_response_ack_is_canned_with_no_inference():
    """An ACK reply is a canned acknowledgment with NO model call — the fold-back
    (Omega) reasons over the whole command and raises pressure instead."""
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_ACK",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="build me a flask api",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="ack",
        tool_call_spec=None,
        image_spec=None,
    )
    res = execute(pp, "TEXT_FAST", _pack(), _mem(), "ep_2", runtime_client=rt)
    assert res["outputs"].get("response_text")
    assert res["outputs"]["deterministic_response_kind"] == "command_acknowledgement"
    assert not any(c[0] == "generate" for c in rt.calls)


def test_remember_receipt_reports_the_actual_sram_intake(monkeypatch):
    """A remember ack describes records admitted by AUTO, never its classifier label."""
    from memory import episode_sram

    monkeypatch.setattr(
        episode_sram,
        "prepare_persona_intake_for_action",
        lambda text: ["relationship:best_friend=Mitchell", "family:daughters=two"],
        raising=False,
    )
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_ACK",
        system_prompt="",
        developer_prompt=None,
        user_prompt="Remember that Mitchell has two daughters.",
        context_blocks={},
        response_mode="ack",
        tool_call_spec=None,
        image_spec=None,
    )

    result = execute(pp, "TEXT_FAST", _pack(), _mem(), "ep_remember", runtime_client=rt)

    assert result["outputs"] == {
        "response_text": (
            "Captured for memory: relationship:best_friend=Mitchell; family:daughters=two."
        ),
        "deterministic_response_kind": "memory_intake_admitted",
        "memory_intake_records": [
            "relationship:best_friend=Mitchell",
            "family:daughters=two",
        ],
    }
    assert rt.calls == []


def test_remember_receipt_says_nothing_was_stored_when_intake_is_empty(monkeypatch):
    from memory import episode_sram

    monkeypatch.setattr(
        episode_sram,
        "prepare_persona_intake_for_action",
        lambda text: [],
        raising=False,
    )
    pp = PromptPack(
        prompt_type="RESPONSE_ACK",
        system_prompt="",
        developer_prompt=None,
        user_prompt="Remember this.",
        context_blocks={},
        response_mode="ack",
        tool_call_spec=None,
        image_spec=None,
    )

    result = execute(pp, "TEXT_FAST", _pack(), _mem(), "ep_empty", runtime_client=FakeRuntime())

    assert result["outputs"]["response_text"] == "Nothing was stored; no memory facts were found."
    assert result["outputs"]["deterministic_response_kind"] == "memory_intake_empty"


def test_freeform_sends_emergent_traits_through_the_answer_engineer(monkeypatch):
    """Verified traits are data for the engineer and response model, never a static pack."""
    rk = [
        {
            "tool": "remember_identity",
            "memory_type": "identity",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "trait.curious.deadbeefcafe",
                    "coord": [0, 2, 0],
                    "title": "Identity Trait - curious",
                    "remembered_as": "curious emergent trait",
                }
            ],
        }
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="freeform",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env))
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_FREEFORM",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="hey",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="freeform",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="freeform",
        memory_query="hey",
        memory_tools=({"tool": "remember_identity"},),
    )
    res = execute(pp, "TEXT_FAST", pack, _mem(), "ep_ff", runtime_client=rt)
    assert res["outputs"].get("response_text")
    gen = _answer_generate(rt)
    assert "curious" in gen["prompt"]
    assert "You are Omega" in gen["system"]
    assert "model internals" in gen["system"]


def test_response_execution_retains_verified_use_receipt_for_deepsleep(monkeypatch):
    rk = [
        {
            "tool": "remember_identity",
            "memory_type": "identity",
            "verdict": "verified",
            "evidence": [{"memory_id": "memory-a", "remembered_as": "verified fact"}],
            "receipt": {
                "dme_activation_traces": [
                    {
                        "projection_generation": "g1",
                        "associations": [
                            {
                                "association_id": "association-a",
                                "source_id": "memory-a",
                                "target_id": "memory-b",
                                "relation_type": "SUPPORTS",
                                "attribution_status": "proven",
                                "activation_strength": 0.75,
                            }
                        ],
                    }
                ]
            },
        }
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="freeform",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env))
    pp = PromptPack(
        prompt_type="RESPONSE_FREEFORM",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="what do you know",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="freeform",
        tool_call_spec=None,
        image_spec=None,
    )

    result = execute(
        pp,
        "TEXT_FAST",
        _pack(memory_required=True, memory_query="what do you know", memory_tools=({"tool": "remember_identity"},)),
        _mem(),
        "ep-verified-use",
        runtime_client=FakeRuntime(),
    )

    receipt = result["outputs"]["association_use_receipt"]
    assert receipt["verified_memory_ids"] == ["memory-a"]
    assert [row["association_id"] for row in receipt["associations"]] == ["association-a"]


def test_freeform_project_query_grounds_dev_memory_in_system(monkeypatch):
    rk = [
        {
            "tool": "remember_identity",
            "memory_type": "identity",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "id.omega",
                    "coord": [0, 0, 0],
                    "remembered_as": "Omega founding identity",
                }
            ],
        },
        {
            "tool": "remember_dev",
            "memory_type": "episodic",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "3.00.todo-app",
                    "remembered_as": "todo-app exists in dev memory and already has models.py",
                }
            ],
        },
        {
            "tool": "remember_workflow",
            "memory_type": "workflow",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "6.00.todo-next",
                    "remembered_as": "next step is to add pytest coverage for the todo routes",
                }
            ],
        },
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="freeform",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env))
    monkeypatch.setattr(
        "deepsleep_loop.identity_store.emergent_traits",
        lambda min_evidence=2: [],
    )
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_FREEFORM",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="lets continue our work on the todo-app and add tests to it",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="freeform",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="freeform",
        memory_query="lets continue our work on the todo-app and add tests to it",
        memory_tools=({"tool": "remember_identity"}, {"tool": "remember_dev"}),
    )
    execute(pp, "TEXT_FAST", pack, _mem(), "ep_ff_project", runtime_client=rt)
    gen = _answer_generate(rt)
    assert "todo-app exists in dev memory" in gen["prompt"]
    assert "next step is to add pytest coverage" in gen["prompt"]
    assert gen["system"].startswith("You are Omega")
    assert "sys" not in gen["system"]


def test_freeform_nonproject_query_grounds_relevant_recall_in_system(monkeypatch):
    # G5: a NON-project freeform turn must still ground RELEVANT recall (previously discarded unless
    # the narrow project regex fired), while staying a be-Omega voice turn and adding NO project hint.
    rk = [
        {
            "tool": "remember_identity",
            "memory_type": "identity",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "id.omega",
                    "coord": [0, 0, 0],
                    "title": "Omega identity",
                    "remembered_as": "Omega, built by the operator",
                }
            ],
        },
        {
            "tool": "remember_knowledge",
            "memory_type": "knowledge",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "k.mars",
                    "coord": [2, 0, 0],
                    "title": "Mars moons",
                    "remembered_as": "Mars has two moons, Phobos and Deimos",
                }
            ],
        },
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="freeform",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env))
    monkeypatch.setattr("deepsleep_loop.identity_store.emergent_traits", lambda min_evidence=2: [])
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_FREEFORM",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="tell me about the moons of mars",  # NOT a project query
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="freeform",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="freeform",
        memory_query="tell me about the moons of mars",
        memory_tools=({"tool": "remember_identity"}, {"tool": "remember_knowledge"}),
    )
    execute(pp, "TEXT_FAST", pack, _mem(), "ep_ff_np", runtime_client=rt)
    gen = _answer_generate(rt)
    assert "Phobos and Deimos" in gen["prompt"]
    assert "Do not answer a project/build request" not in gen["prompt"]


def test_generic_response_grounds_recall_on_system_not_user(monkeypatch):
    # G6: the generic (proactive/ask) branch must ground on the SYSTEM directive (gemma4 obeys
    # system > user), not the user channel where it previously appended the block.
    rk = [
        {
            "tool": "remember_knowledge",
            "memory_type": "knowledge",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "k.fr",
                    "coord": [2, 0, 0],
                    "title": "France",
                    "remembered_as": "Paris is the capital of France",
                }
            ],
        },
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="ask",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env))
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_ASK",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="what is the capital of france",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="ask",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="ask",
        memory_query="what is the capital of france",
        memory_tools=({"tool": "remember_knowledge"},),
    )
    execute(pp, "TEXT_FAST", pack, _mem(), "ep_generic", runtime_client=rt)
    gen = _answer_generate(rt)
    assert "Paris is the capital of France" in gen["prompt"]
    assert "Paris is the capital of France" not in gen["system"]


def test_response_temporal_grounds_dated_recall_in_system(monkeypatch):
    # RESPONSE_TEMPORAL grounds on its OWN date-range recall, rendered DATED, with the temporal
    # contract — NOT the standard rk weave.
    monkeypatch.setattr(
        "memory.temporal_recall.recall_temporal",
        lambda user, *a, **k: {
            "label": "yesterday",
            "records": [
                {
                    "observed_at": "2026-07-15T16:00:00+00:00",
                    "title": "TODO tests",
                    "summary": "added pytest coverage",
                    "coord": [3, 0, 0],
                },
            ],
            "range": {},
        },
    )
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_TEMPORAL",
        system_prompt="STATIC TEMPORAL PACK",
        developer_prompt=None,
        user_prompt="what did we build yesterday",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="temporal",
        tool_call_spec=None,
        image_spec=None,
    )
    execute(pp, "TEXT_FAST", _pack(memory_required=False), _mem(), "ep_temporal", runtime_client=rt)
    gen = _answer_generate(rt)
    assert "[2026-07-15] TODO tests: added pytest coverage" in gen["prompt"]
    assert "STATIC TEMPORAL PACK" not in gen["system"]
    assert "STATIC TEMPORAL PACK" not in gen["prompt"]


def test_response_temporal_empty_window_says_nothing_recorded(monkeypatch):
    monkeypatch.setattr(
        "memory.temporal_recall.recall_temporal",
        lambda user, *a, **k: {"label": "yesterday", "records": [], "range": {}},
    )
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_TEMPORAL",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="what did we build yesterday",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="temporal",
        tool_call_spec=None,
        image_spec=None,
    )
    execute(
        pp,
        "TEXT_FAST",
        _pack(memory_required=False),
        _mem(),
        "ep_temporal_empty",
        runtime_client=rt,
    )
    gen = _answer_generate(rt)
    assert "do not recall the requested fact" in gen["prompt"]
    assert "what did we build yesterday" in gen["prompt"]


def test_general_question_with_empty_recall_still_uses_composer(monkeypatch):
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="unknown",
        memory_reason="question recall attempt",
        memory_scope=("knowledge",),
        items=(),
        error=None,
    )
    monkeypatch.setattr(
        "auto_loop.mril_tools.run_forced_memory_tools",
        lambda *args, **kwargs: ([], env),
    )
    rt = FakeRuntime(text="Paris is the capital of France.")
    pp = PromptPack(
        prompt_type="RESPONSE_QUESTION",
        system_prompt="retired static pack",
        developer_prompt=None,
        user_prompt="what is the capital of France?",
        context_blocks={},
        response_mode="question",
        tool_call_spec=None,
        image_spec=None,
    )

    result = execute(
        pp,
        "TEXT_FAST",
        _pack(memory_required=True, memory_answer_required=False),
        env,
        "ep-general-question",
        runtime_client=rt,
    )

    assert result["outputs"]["response_text"] == "Paris is the capital of France."
    assert "do not recall" not in _answer_generate(rt)["prompt"]


def test_composed_answer_receives_recent_conversation():
    from memory.hot_buffer import append_turn

    append_turn("user", "My preferred editor is Helix.")
    append_turn("assistant", "I will keep Helix in mind.")
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_FREEFORM",
        system_prompt="retired static pack",
        developer_prompt=None,
        user_prompt="why do I like it?",
        context_blocks={},
        response_mode="freeform",
        tool_call_spec=None,
        image_spec=None,
    )

    execute(pp, "TEXT_FAST", _pack(memory_required=False), _mem(), "ep-recent", runtime_client=rt)

    prompt = _answer_generate(rt)["prompt"]
    assert "My preferred editor is Helix." in prompt
    assert "I will keep Helix in mind." in prompt


def test_composed_answer_receives_active_system_question_thread():
    thread = (
        "CONVERSATION THREAD — you asked which bug was fixed.\n"
        "Operator answered: the recall bug"
    )
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_FREEFORM",
        system_prompt="retired static pack",
        developer_prompt=None,
        user_prompt="the recall bug",
        context_blocks={"conversation_thread": thread},
        response_mode="freeform",
        tool_call_spec=None,
        image_spec=None,
    )

    execute(pp, "TEXT_FAST", _pack(memory_required=False), _mem(), "ep-thread", runtime_client=rt)

    assert thread in _answer_generate(rt)["prompt"]


def test_composed_proactive_answer_receives_execution_directive():
    directive = "Complete the requested dialogue application now and answer every requested item."
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="PROACTIVE_REASONING",
        system_prompt="retired static pack",
        developer_prompt=None,
        user_prompt="apply the result",
        context_blocks={"response_execution_directive": directive},
        response_mode="reasoning",
        tool_call_spec=None,
        image_spec=None,
    )

    execute(
        pp,
        "TEXT_REASONING",
        _pack(action="PROACTIVE_RESPONSE", memory_required=False),
        _mem(),
        "ep-proactive-directive",
        runtime_client=rt,
    )

    assert directive in _answer_generate(rt)["prompt"]


def test_question_grounds_identity_and_knowledge_in_system(monkeypatch):
    """STEP 2 wiring: RESPONSE_QUESTION composes recalled IDENTITY/persona + emergent traits into the
    DYNAMIC system directive (be Omega, never name the base model) AND the recalled KNOWLEDGE grounding
    into the SYSTEM directive too — gemma4 (the AUTO responder) follows system directives more reliably
    than user-prompt content (operator directive 2026-06-29). Guards the leak fix for identity/creator
    questions."""
    # Recalled evidence mirrors the real seed shape: the identity body lacks the creator name; the
    # TITLE carries it. The creator name here is a neutral PLACEHOLDER — the real operator/creator
    # name lives ONLY in the per-instance identity seed (memory), never hardcoded in shipped source.
    rk = [
        {
            "tool": "remember_identity",
            "memory_type": "identity",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "id.creator",
                    "coord": [0, 0, 0],
                    "title": "Creator — Ada Lovelace",
                    "remembered_as": "Built Omega 3.0 by hand",
                },
                {
                    "memory_id": "trait.direct.deadbeefcafe",
                    "coord": [0, 2, 0],
                    "title": "Identity Trait - direct",
                    "remembered_as": "direct emergent trait",
                },
            ],
        },
        {
            "tool": "remember_knowledge",
            "memory_type": "knowledge",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "2.00.france",
                    "remembered_as": "Paris is the capital of France",
                }
            ],
        },
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="q",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env))
    # The trait must be ESTABLISHED (evidence_count >= 2) to surface — emergent_traits is the floor.
    monkeypatch.setattr(
        "deepsleep_loop.identity_store.emergent_traits",
        lambda min_evidence=2: [SimpleNamespace(name="direct")],
    )
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_QUESTION",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="who is your creator?",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="question",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="q",
        memory_query="who is your creator?",
        memory_tools=({"tool": "remember_identity"},),
    )
    res = execute(pp, "TEXT_FAST", pack, _mem(), "ep_q", runtime_client=rt)
    gen = _answer_generate(rt)
    assert "Creator — Ada Lovelace" in gen["prompt"]
    assert "direct emergent trait" in gen["prompt"]
    assert "Paris is the capital of France" in gen["prompt"]
    assert gen["system"].startswith("You are Omega")
    assert "sys" not in gen["system"]


def test_reasoning_grounds_knowledge_and_coding_in_system_no_identity(monkeypatch):
    """STEP 3 wiring: RESPONSE_REASONING is a chain-of-thought helper, NOT a "be Omega" turn — so
    recalled KNOWLEDGE + CODING ground the chain of thought, and they go into the SYSTEM directive
    (gemma4 — the AUTO responder — follows system directives more reliably than user-prompt content;
    operator directive 2026-06-29). The system stays free of any identity/personality directive
    (unlike freeform/question); the user turn stays the operator's actual message."""
    rk = [
        {
            "tool": "remember_knowledge",
            "memory_type": "knowledge",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "2.00.asyncio",
                    "remembered_as": "asyncio futures resolve on the loop thread",
                }
            ],
        },
        {
            "tool": "remember_code",
            "memory_type": "coding",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "5.00.retry-loop",
                    "remembered_as": "retry_loop() awaits gather() without a timeout",
                }
            ],
        },
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="reasoning",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env))
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_REASONING",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="work through why the retry loop deadlocks",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="reasoning",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="reasoning",
        memory_query="work through why the retry loop deadlocks",
        memory_tools=({"tool": "remember_knowledge"}, {"tool": "remember_code"}),
    )
    res = execute(pp, "TEXT_REASONING", pack, _mem(), "ep_reason", runtime_client=rt)
    gen = _answer_generate(rt)
    assert "asyncio futures resolve on the loop thread" in gen["prompt"]
    assert "retry_loop() awaits gather() without a timeout" in gen["prompt"]
    assert gen["system"].startswith("You are Omega")
    assert "sys" not in gen["system"]


def test_memory_enriched_question_profile_reasons_without_self_insertion(monkeypatch):
    """A memory-enriched question promoted to TEXT_REASONING must become a real reasoning turn.

    The live regression selected TEXT_REASONING but executed RESPONSE_QUESTION with thinking off,
    then injected Omega's identity and answered about itself instead of comparing Noah and Mitchell.
    """
    query = (
        "Yes, that is half correct. Mitchell is a part owner, but Noah is also a part owner, "
        "and the question was: what do Mitchell and Noah do at Omega AI?"
    )
    rk = [
        {
            "tool": "remember_identity",
            "memory_type": "identity",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "0.00.omega_identity",
                    "coord": [0, 0, 0],
                    "remembered_as": (
                        "Omega is a persistent autonomous partner that remembers, learns, "
                        "builds, and grows."
                    ),
                }
            ],
        },
        {
            "tool": "remember_persona",
            "memory_type": "persona",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "1.01.mitchell_owner",
                    "coord": [1, 1, 1],
                    "remembered_as": "Mitchell Ray is part owner of Omega AI LLC.",
                    "relation_facets": [
                        {
                            "subject": "mitchell ray",
                            "facet": "organization_role",
                            "relation": "part owner",
                            "object": "Omega AI LLC",
                        }
                    ],
                },
                {
                    "memory_id": "1.00.noah_owner",
                    "coord": [1, 0, 1],
                    "remembered_as": "Noah Lee is part owner of Omega AI LLC.",
                    "relation_facets": [
                        {
                            "subject": "operator",
                            "facet": "organization_role",
                            "relation": "part owner",
                            "object": "Omega AI LLC",
                        }
                    ],
                },
            ],
        },
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="operator comparison",
        memory_scope=(),
        items=(),
        error=None,
    )
    from auto_loop.mril_tools.candidate_hygiene import finalize_recall_results

    clean_rk = finalize_recall_results(rk, query)
    monkeypatch.setattr(
        "auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (clean_rk, env)
    )
    rt = FakeRuntime(text="Mitchell and Noah are both part owners of Omega AI LLC.")
    pp = PromptPack(
        prompt_type="RESPONSE_QUESTION",
        system_prompt="question-system",
        developer_prompt=None,
        user_prompt=query,
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="question",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="operator comparison",
        memory_query=query,
        memory_tools=({"tool": "remember_identity"}, {"tool": "remember_persona"}),
    )

    execute(pp, "TEXT_REASONING", pack, _mem(), "ep_compare_roles", runtime_client=rt)

    gen = _answer_generate(rt)
    assert gen["enable_thinking"] is True
    assert gen["timeout"] == 600.0
    assert "Mitchell Ray is part owner of Omega AI LLC" in gen["prompt"]
    assert "Noah Lee is part owner of Omega AI LLC" in gen["prompt"]
    assert "persistent autonomous partner" not in gen["prompt"]
    assert gen["prompt"].rstrip().endswith(query)


def test_verified_recall_reaches_the_answer_model_as_data(monkeypatch):
    query = "What do Mitchell and I both do at Omega AI?"
    rk = [
        {
            "tool": "remember_persona",
            "memory_type": "persona",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "1.01.mitchell_owner",
                    "remembered_as": "Mitchell Ray is part owner of Omega AI LLC.",
                },
                {
                    "memory_id": "1.00.noah_owner",
                    "remembered_as": "Noah Lee is part owner of Omega AI LLC.",
                },
            ],
        }
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="operator comparison",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr(
        "auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env)
    )

    rt = FakeRuntime(text="You and Mitchell are both part owners of Omega AI LLC.")
    pp = PromptPack(
        prompt_type="RESPONSE_QUESTION",
        system_prompt="STATIC QUESTION PACK",
        developer_prompt=None,
        user_prompt=query,
        context_blocks={},
        response_mode="question",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="operator comparison",
        memory_query=query,
        memory_tools=({"tool": "remember_persona"},),
    )

    result = execute(pp, "TEXT_REASONING", pack, _mem(), "ep_composed", runtime_client=rt)

    calls = [kw for op, kw in rt.calls if op == "generate"]
    # ONE hop. The composer that used to run first was retired 2026-08-13.
    assert len(calls) == 1
    assert "VERIFIED MEMORY DATA" in calls[0]["prompt"]
    assert "Mitchell Ray is part owner of Omega AI LLC." in calls[0]["prompt"]
    assert "Noah Lee is part owner of Omega AI LLC." in calls[0]["prompt"]
    assert calls[0]["prompt"].rstrip().endswith(query)
    assert "STATIC QUESTION PACK" not in calls[0]["system"]
    # The routed model profile owns the reasoning posture outright now — with the
    # composer gone nothing can promote or demote it mid-turn.
    assert calls[0]["enable_thinking"] is True
    assert calls[0]["timeout"] == 600.0
    assert result["outputs"]["response_text"].startswith("You and Mitchell")


def test_question_does_not_ground_unverified_recall(monkeypatch):
    rk = [
        {
            "tool": "remember_knowledge",
            "memory_type": "none",
            "evidence": [
                {
                    "memory_id": "unverified-claim",
                    "text": "UNVERIFIED_POISONED_CLAIM",
                }
            ],
            "verdict": "conflict",
            "verdict_reason": "verified canonical memories disagree",
            "pressure_signal": "memory_reconciliation",
        }
    ]
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="conflict",
        memory_reason="q",
        memory_scope=(),
        items=(),
        error=None,
    )
    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", lambda *a, **k: (rk, env))
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_QUESTION",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="what changed?",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="question",
        tool_call_spec=None,
        image_spec=None,
    )
    pack = _pack(
        memory_required=True,
        memory_reason="q",
        memory_query="what changed?",
        memory_tools=({"tool": "remember_knowledge"},),
    )
    execute(pp, "TEXT_FAST", pack, _mem(), "ep_q_conflict", runtime_client=rt)
    gen = _answer_generate(rt)
    assert "UNVERIFIED_POISONED_CLAIM" not in gen["prompt"]
    assert "MEMORY RECORDS:" not in gen["prompt"]


def test_non_memory_response_uses_engineer_not_static_prompt_pack(monkeypatch):
    rt = FakeRuntime()
    pp = PromptPack(
        prompt_type="RESPONSE_REASONING",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="why is the sky blue",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="reasoning",
        tool_call_spec=None,
        image_spec=None,
    )
    res = execute(pp, "TEXT_REASONING", _pack(), _mem(), "ep_r", runtime_client=rt)
    gen = _answer_generate(rt)
    assert gen["system"].startswith("You are Omega")
    assert gen["system"] != "sys"
    assert gen["prompt"].rstrip().endswith("why is the sky blue")


def test_web_fetch_overlay_and_invoke():
    rt = FakeRuntime(
        tool_calls=[{"arguments": {"query": "model chosen query"}}],
    )
    invoked = {}

    def fake_tool(name, args):
        invoked["name"] = name
        invoked["args"] = args
        return {"ok": True, "result": {"hits": 1}, "elapsed_ms": 5}

    pack = _pack(
        action="USE_TOOL",
        tool="web_fetch",
        tool_reason="learn",
        execution_inputs={"query": "Python"},
    )
    assert tool_call_is_direct(pack) is True
    pp = build_prompt_pack(pack, _mem())
    res = execute(
        pp,
        None,
        pack,
        _mem(),
        "ep_1",
        runtime_client=rt,
        tool_invoker=fake_tool,
    )
    assert invoked["name"] == "web_fetch"
    assert invoked["args"]["query"] == "Python"
    assert invoked["args"]["max_results"] == 3
    assert res["outputs"]["tool_result"] == {"hits": 1}


def test_stamped_web_research_is_not_a_direct_invoke():
    pack = _pack(
        action="USE_TOOL",
        tool="web_fetch",
        tool_reason="learn",
        execution_inputs={
            "query": "Python",
            "web_research": True,
            "web_catalog": [{"name": "Python docs", "url": "https://docs.python.org/3/"}],
        },
    )
    assert tool_call_is_direct(pack) is False


def test_learn_web_research_invokes_the_model_chosen_url():
    invoked: list[tuple[str, dict]] = []
    order: list[str] = []
    chosen = "https://docs.python.org/3/library/asyncio.html"

    def fake_tool(name, args):
        order.append("invoke")
        invoked.append((name, dict(args)))
        return {
            "ok": True,
            "result": {
                "query": args.get("query"),
                "domain": "python",
                "results": [
                    {
                        "title": "asyncio",
                        "url": str(args.get("query") or ""),
                        "content": "asyncio is a library to write concurrent code using async/await.",
                        "source": "python-docs",
                    }
                ],
            },
            "elapsed_ms": 5,
        }

    class ResearchRuntime(FakeRuntime):
        def generate_completion(self, **kw):
            order.append("generate")
            self.calls.append(("generate", kw))
            if kw.get("tools") is not None and not any(
                isinstance(turn, dict) and turn.get("role") == "tool"
                for turn in kw.get("messages") or ()
            ):
                return {
                    "text": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "web_fetch",
                                "arguments": json.dumps({"query": chosen}),
                            },
                        }
                    ],
                    "tokens_in": 1,
                    "tokens_out": 1,
                }
            return {
                "text": "asyncio is Python's concurrent I/O library.",
                "tool_calls": [],
                "tokens_in": 1,
                "tokens_out": 1,
            }

    pack = _pack(
        action="USE_TOOL",
        tool="web_fetch",
        tool_reason="learn",
        source_action_goal={"intent": "LEARN"},
        execution_inputs={
            "query": "Python asyncio",
            "domain": "python",
            "web_research": True,
            "web_catalog": [
                {
                    "name": "Python docs",
                    "url": "https://docs.python.org/3/",
                    "tier": "primary",
                    "domain": "python",
                    "note": "",
                }
            ],
        },
    )
    res = execute(
        build_prompt_pack(pack, _mem()),
        None,
        pack,
        _mem(),
        "ep_learn_research",
        runtime_client=ResearchRuntime(),
        tool_invoker=fake_tool,
    )
    assert order[0] == "generate"
    assert order.index("generate") < order.index("invoke")
    assert invoked == [("web_fetch", {"query": chosen})]
    assert res["failure_kind"] is None
    assert res["outputs"]["tool_result"]["results"][0]["url"] == chosen
    assert res["outputs"]["knowledge_artifact_id"]


def test_learn_web_research_fails_closed_without_a_tool_call():
    invoked: list = []

    def fake_tool(name, args):
        invoked.append((name, args))
        raise AssertionError("direct Wikipedia invoke must not run")

    pack = _pack(
        action="USE_TOOL",
        tool="web_fetch",
        tool_reason="learn",
        source_action_goal={"intent": "LEARN"},
        execution_inputs={
            "query": "Python asyncio",
            "domain": "python",
            "web_research": True,
            "web_catalog": [{"name": "Python docs", "url": "https://docs.python.org/3/"}],
        },
    )
    res = execute(
        build_prompt_pack(pack, _mem()),
        None,
        pack,
        _mem(),
        "ep_learn_no_call",
        runtime_client=FakeRuntime(text="I already know asyncio."),
        tool_invoker=fake_tool,
    )
    assert invoked == []
    assert res["failure_kind"] == "web_research_no_tool_call"


@pytest.mark.parametrize("failure", ["invoker", "reader"])
def test_learn_web_research_preserves_failed_acquisition(failure):
    class FailedResearchRuntime(FakeRuntime):
        def generate_completion(self, **kwargs):
            if not any(turn.get("role") == "tool" for turn in kwargs.get("messages") or ()):
                return {
                    "text": "",
                    "tool_calls": [{
                        "id": "fetch",
                        "type": "function",
                        "function": {
                            "name": "web_fetch",
                            "arguments": json.dumps({"query": "ftp://example.invalid/research"}),
                        },
                    }],
                }
            return {"text": "The fetch failed.", "tool_calls": []}

    def invoke(name, args):
        if failure == "invoker":
            return {"ok": False, "error": "reader unavailable", "result": {}}
        from schema.tools.web_fetch import web_fetch

        return {"ok": True, "result": web_fetch(**args)}

    pack = _pack(
        action="USE_TOOL",
        tool="web_fetch",
        tool_reason="learn",
        source_action_goal={"intent": "LEARN"},
        execution_inputs={
            "query": "research",
            "domain": "python",
            "web_research": True,
            "web_catalog": [],
        },
    )
    res = execute(
        build_prompt_pack(pack, _mem()),
        None,
        pack,
        _mem(),
        f"ep-learn-web-failure-{failure}",
        runtime_client=FailedResearchRuntime(),
        tool_invoker=invoke,
    )
    assert res["failure_kind"] == "web_research_failed"
    assert res["outputs"]["tool_status"] == "failed"
    assert res["outputs"]["tool_error"]
    assert "knowledge_artifact_id" not in res["outputs"]


def test_learn_web_research_https_timeout_is_not_knowledge_success(monkeypatch):
    """A timed-out catalog URL must fail LEARN, not extract knowledge from emptiness."""
    import schema.tools.web_fetch as wf

    def _boom(url: str) -> dict:
        raise TimeoutError(f"timed out fetching {url}")

    monkeypatch.setattr(wf, "_safe_direct_fetch", _boom)

    class FailedHttpsRuntime(FakeRuntime):
        def generate_completion(self, **kwargs):
            if not any(turn.get("role") == "tool" for turn in kwargs.get("messages") or ()):
                return {
                    "text": "",
                    "tool_calls": [{
                        "id": "fetch",
                        "type": "function",
                        "function": {
                            "name": "web_fetch",
                            "arguments": json.dumps(
                                {"query": "https://docs.python.org/3/library/asyncio.html"}
                            ),
                        },
                    }],
                }
            return {"text": "The fetch failed.", "tool_calls": []}

    def invoke(name, args):
        return {"ok": True, "result": wf.web_fetch(**args)}

    pack = _pack(
        action="USE_TOOL",
        tool="web_fetch",
        tool_reason="learn",
        source_action_goal={"intent": "LEARN"},
        execution_inputs={
            "query": "asyncio event loop",
            "domain": "python",
            "web_research": True,
            "web_catalog": [
                {
                    "name": "Python docs",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "tier": "primary",
                    "domain": "python",
                    "note": "",
                }
            ],
        },
    )
    res = execute(
        build_prompt_pack(pack, _mem()),
        None,
        pack,
        _mem(),
        "ep-learn-https-timeout",
        runtime_client=FailedHttpsRuntime(),
        tool_invoker=invoke,
    )
    assert res["failure_kind"] == "web_research_failed"
    assert res["outputs"]["tool_status"] == "failed"
    assert "timed out" in str(res["outputs"]["tool_error"])
    assert "knowledge_artifact_id" not in res["outputs"]


def test_web_fetch_audit_carries_compact_source_provenance():
    tf._reset_recent_events_ring_for_tests()
    try:
        pack = _pack(
            action="USE_TOOL",
            tool="web_fetch",
            tool_reason="learn",
            execution_inputs={"query": "Python"},
        )
        pp = build_prompt_pack(pack, _mem())

        def fake_tool(name, args):
            return {
                "ok": True,
                "result": {
                    "domain": "python",
                    "results": [
                        {
                            "url": "https://docs.python.org/3/",
                            "source": "python-docs",
                        }
                    ],
                },
                "elapsed_ms": 5,
            }

        execute(
            pp,
            None,
            pack,
            _mem(),
            "ep_provenance",
            runtime_client=FakeRuntime(),
            tool_invoker=fake_tool,
        )
        events = [
            row
            for row in tf.recent_audit_events(limit=0)
            if row.get("kind") == "data"
            and row.get("phase") == "tool_invocation"
            and row.get("episode_id") == "ep_provenance"
        ]
        assert events
        latest = events[-1]
        assert latest["tool_name"] == "web_fetch"
        assert latest["domain"] == "python"
        assert latest["learned_link"] == "https://docs.python.org/3/"
        assert latest["source_name"] == "python-docs"
    finally:
        tf._reset_recent_events_ring_for_tests()


def test_missing_tool():
    pack = _pack(action="USE_TOOL", tool=None)
    pp = build_prompt_pack(pack, _mem())
    res = execute(pp, None, pack, _mem(), "ep_1", runtime_client=FakeRuntime())
    assert res["failure_kind"] == "missing_tool"


def test_image_artifact_metadata_no_raw_bytes():
    rt = FakeRuntime(image=b"\x00\x01")
    pack = _pack(
        action="IMAGE_CREATION",
        tool="image_tool",
        execution_inputs={"image_spec": {"prompt": "cube", "width": 256}},
    )
    pp = build_prompt_pack(pack, _mem())
    res = execute(pp, "IMAGE_GENERATION", pack, _mem(), "ep_1", runtime_client=rt)
    art = res["outputs"]["image_artifact"]
    assert art["bytes_len"] == len(b"\x00\x01")
    assert "sha256" in art
    assert not any(isinstance(v, (bytes, bytearray)) for v in art.values())


def test_idle_no_op():
    pack = _pack(action="IDLE")
    pp = build_prompt_pack(pack, _mem())
    res = execute(pp, None, pack, _mem(), "ep_1", runtime_client=FakeRuntime())
    assert res["outputs"] == {}


# ── tools on demand (operator 2026-09-05) ─────────────────────────────────────
# "one pipeline for chat ... it will chat, use tools, search the web etc on
# command": an operator turn's reply pass carries the bounded tool table; the
# model decides whether to call one; the calls run inside AUTO; the reply is
# written after. Same rail, same bubble.

from auto_loop.execute.brain_skill_executor import OPERATOR_TOOL_NAMES  # noqa: E402


class ToolRuntime(FakeRuntime):
    """Scripted passes: each entry is the tool_calls one pass returns. A pass
    offered NO tools (the forced final answer) or one past the script answers
    in text, the way a real seat does."""

    def __init__(self, scripted, final_text="done", *, project_root=None):
        super().__init__(text="")
        self._scripted = [list(calls) for calls in scripted]
        if project_root is not None:
            self._scripted.insert(0, [_call("start_job", {"project_root": project_root}, "claim")])
        self._final = final_text

    def generate_completion(self, **kw):
        self.calls.append(("generate", kw))
        if kw.get("tools") is not None and self._scripted:
            return {
                "text": "",
                "tool_calls": self._scripted.pop(0),
                "tokens_in": 1,
                "tokens_out": 1,
            }
        return {"text": self._final, "tool_calls": [], "tokens_in": 1, "tokens_out": 1}


def test_browser_route_requires_meta_a_perception_instead_of_pre_navigation():
    from auto_loop.execute.run import _operator_tools_reply

    def forbidden(**kwargs):
        raise AssertionError("legacy browser route must not infer or actuate")

    result, records, failure = _operator_tools_reply(
        forbidden, system="sys", prompt="open example.org", objective="open example.org",
        episode_id="browser", max_tokens=100, enable_thinking=False, timeout=None,
        chat_cycle_id="", warnings=[], operator_route="browser")
    assert result is None and records == []
    assert "BROWSER_ACTION" in failure


def test_web_search_route_lets_the_model_choose_the_query(tmp_path, monkeypatch):
    """Chat does not run web_search with the raw sentence before the first generate."""
    from auto_loop.execute import brain_skill_executor as executor
    from auto_loop.execute.run import _operator_tools_exchange

    monkeypatch.setattr(executor, "operator_workspace_jail", lambda: tmp_path / "workspace")
    seen: list[tuple[str, dict[str, object]]] = []

    def fake_run_operator_tool(name, raw_args, _ctx):
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        seen.append((name, args))
        page = {
            "query": args.get("query"),
            "results": [
                {
                    "title": "VLMs",
                    "url": "https://example.org/vlms",
                    "content": "Vision-language models combine images and text.",
                }
            ],
        }
        return executor.ToolResult(True, output="search result", meta={"page": page}), args

    monkeypatch.setattr(executor, "run_operator_tool", fake_run_operator_tool)

    def generate(**kwargs):
        messages = kwargs["messages"]
        if messages[-1]["role"] == "user":
            assert kwargs.get("tools")
            assert kwargs.get("tool_choice") in (None, "auto")
            return {
                "text": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": "latest vision language models"}),
                        },
                    }
                ],
            }
        return {"text": "Here is what the search found.", "tool_calls": []}

    result, records, stop_reason = _operator_tools_exchange(
        generate,
        system="sys",
        prompt="search the web for the latest VLMs",
        objective="search the web for the latest VLMs",
        episode_id="ep-web-search-model",
        max_tokens=100,
        enable_thinking=False,
        timeout=None,
        chat_cycle_id="cycle-web-search-model",
        warnings=[],
        forced_tool="web_search",
        runtime_client=object(),
        session={"records": [], "owned": False, "job_id": "", "cursor": 0},
    )

    assert stop_reason == ""
    assert seen == [("web_search", {"query": "latest vision language models"})]
    assert records[0]["tool_name"] == "web_search"
    assert records[0]["result"]["page"]["results"]
    assert result == {"text": "Here is what the search found.", "tool_calls": []}


def test_workspace_inspection_route_lists_the_active_trusted_folder_before_answer(tmp_path, monkeypatch):
    from auto_loop.execute.run import _operator_tools_exchange

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("private contents", encoding="utf-8")
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(workspace))

    def generate(**kwargs):
        assert kwargs["messages"][-1]["role"] == "tool"
        assert "notes.txt" in kwargs["messages"][-1]["content"]
        assert "private contents" not in kwargs["messages"][-1]["content"]
        return {"text": "I found notes.txt in the active folder.", "tool_calls": []}

    result, records, stop_reason = _operator_tools_exchange(
        generate,
        system="sys",
        prompt="Inspect the current directory",
        objective="Inspect the current directory",
        episode_id="ep-workspace-first",
        max_tokens=100,
        enable_thinking=False,
        timeout=None,
        chat_cycle_id="cycle-workspace-first",
        warnings=[],
        forced_tool="list_directory",
        runtime_client=object(),
        operator_route="agent",
        session={"records": [], "owned": False, "job_id": "", "cursor": 0},
    )

    assert stop_reason == ""
    assert records[0]["tool_name"] == "list_directory"
    assert records[0]["status"] == "success"
    assert result == {"text": "I found notes.txt in the active folder.", "tool_calls": []}


def test_execute_workspace_route_selects_the_deterministic_listing_pack(tmp_path, monkeypatch):
    from memory import episode_sram

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("private contents", encoding="utf-8")
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(workspace))
    runtime = ToolRuntime([], final_text="The directory is open and inspected.")
    episode_sram.open_bucket("ep-workspace-pack", pipeline_id=0)

    result = execute(
        _operator_pp("Inspect the current directory"),
        "TEXT_FAST",
        _pack(execution_inputs={"operator_route": "agent"}),
        _mem(),
        "ep-workspace-pack",
        runtime_client=runtime,
        opened_by="operator_query",
        chat_cycle_id="cycle-workspace-pack",
    )

    records = result["outputs"]["operator_tool_calls"]
    assert records[0]["tool_name"] == "list_directory"
    assert records[0]["status"] == "success"
    assert records[0]["dev_memory"]["status"] == "held"
    assert {tuple(item.envelope.coord) for item in episode_sram.pending("ep-workspace-pack") if item.kind == "memory"} == {
        (11, 4, 0),
        (11, 5, 0),
    }
    assert result["outputs"]["response_text"] == "The directory is open and inspected."


def _operator_pp(user="write hello.txt containing hi"):
    return PromptPack(
        prompt_type="RESPONSE_FREEFORM",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt=user,
        context_blocks={},
        response_mode="freeform",
        tool_call_spec=None,
        image_spec=None,
    )


def _call(name, args, call_id="call1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_operator_reply_without_tool_calls_is_just_the_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    rt = FakeRuntime(text="Hey. What is up?")

    res = execute(
        _operator_pp("hello"),
        "TEXT_FAST",
        _pack(),
        _mem(),
        "ep-plain",
        runtime_client=rt,
        opened_by="operator_query",
        chat_cycle_id="cyc-plain",
    )

    assert res["outputs"]["response_text"] == "Hey. What is up?"
    assert "operator_tool_calls" not in res["outputs"]
    assert len([c for c in rt.calls if c[0] == "generate"]) == 1
    assert rt.calls[0][1]["tools"], "tools are offered even when the model does not use them"


def test_operator_tool_loop_forces_an_answer_when_the_budget_runs_out(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    import auto_loop.execute.brain_skill_executor as bse

    monkeypatch.setattr(bse, "OPERATOR_TOOL_MAX_ROUNDS", 1)
    rt = ToolRuntime(
        [
            [_call("search_repo", {"query": "x"})],
            [_call("search_repo", {"query": "y"}, "call2")],
        ],
        final_text="Here is what I found so far.",
    )

    res = execute(
        _operator_pp("find x"),
        "TEXT_FAST",
        _pack(),
        _mem(),
        "ep-budget",
        runtime_client=rt,
        opened_by="operator_query",
        chat_cycle_id="cyc-budget",
    )

    generates = [kw for op, kw in rt.calls if op == "generate"]
    assert len(generates) == 2
    assert generates[0]["tools"] and generates[1].get("tools") is None, "the last pass is tool-less"
    assert res["outputs"]["response_text"] == "Here is what I found so far."
    assert any("budget exhausted" in w for w in res["warnings"])
    assert len(res["outputs"]["operator_tool_calls"]) == 1


def test_operator_barge_in_leaves_cognition_running(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    import pressure_loop.operator_chat_responses as ocr

    ocr.request_interrupt("cyc-stop")
    try:
        rt = ToolRuntime([[_call("search_repo", {"query": "x"})]])
        res = execute(
            _operator_pp("find x"),
            "TEXT_FAST",
            _pack(),
            _mem(),
            "ep-stop",
            runtime_client=rt,
            opened_by="operator_query",
            chat_cycle_id="cyc-stop",
        )
    finally:
        ocr.clear_interrupt("cyc-stop")

    assert res["outputs"]["response_text"] == "done"
    assert [c for c in rt.calls if c[0] == "generate"], "playback cancellation must not stop inference"


def test_autonomous_response_keeps_the_tool_less_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    rt = FakeRuntime(text="status update")

    res = execute(
        _operator_pp("progress?"),
        "TEXT_FAST",
        _pack(action="PROACTIVE_RESPONSE"),
        _mem(),
        "ep-auto",
        runtime_client=rt,
        opened_by="proactive_update",
    )

    assert res["outputs"]["response_text"] == "status update"
    assert rt.calls[0][1].get("tools") is None
    assert "messages" not in rt.calls[0][1]


# ── the tool loop streams, and the work behind the reply reaches the bubble ──
# (operator 2026-09-05: "the tool calls and reasoning prose etc, diff files,
# markdown, all that appends to the chat pane")


class StreamingToolRuntime(ToolRuntime):
    """A seat that streams: prose to on_token, the think trace to on_reasoning,
    and — on a pass that names tools — whole tool_calls in the returned body,
    exactly as the real client reassembles them."""

    def __init__(self, scripted, final_text="done", reasoning="", *, project_root=None):
        super().__init__(scripted, final_text, project_root=project_root)
        self._reasoning = reasoning
        self.stream_calls = []

    def generate_completion_stream(self, **kw):
        self.stream_calls.append(kw)
        on_reasoning = kw.get("on_reasoning")
        if self._reasoning and on_reasoning is not None:
            on_reasoning(self._reasoning)
        body = self.generate_completion(**{k: v for k, v in kw.items() if k not in ("on_token", "on_reasoning")})
        if self._reasoning:
            body["reasoning"] = self._reasoning  # the real client returns the trace whole too
        on_token = kw.get("on_token")
        if on_token is not None and body.get("tool_calls"):
            # the narration streams AND comes back whole in text, as the real client does
            on_token("Let me write that. ")
            body["text"] = "Let me write that. "
        elif on_token is not None:
            for piece in (body["text"][: len(body["text"]) // 2], body["text"][len(body["text"]) // 2 :]):
                on_token(piece)
        body["streamed"] = True
        return body


def _watch_socket():
    import hud.chat_socket as cs

    frames = []
    token = cs.subscribe(frames.append)
    return frames, lambda: cs.unsubscribe(token)


def test_a_barge_in_silences_speech_but_never_the_visible_work(tmp_path, monkeypatch):
    """A barge-in means "stop talking", not "stop showing me what you are doing".

    Amended 2026-09-22 (demo track, B3/B5/H4): this test previously asserted `frames == []`,
    which blinded the operator surface for the rest of the turn — the tool prose stopped
    scrolling and the browser stopped reporting while the work was still genuinely running.
    Silence is now scoped to what is SPOKEN; what is merely seen keeps flowing, because a
    surface that freezes on interruption is indistinguishable from work that died.

    The predecessor was also a tautology: it built the sink with no ``pipeline_id``, and
    ``hud.chat_socket.publish_event`` drops every frame that lacks one, so ``frames == []`` was
    guaranteed whether or not an interrupt had been requested. This one carries a pipeline id, so
    the suppression rule is actually exercised (AGENTS.md section 8: never a tautology).
    """
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    import pressure_loop.operator_chat_responses as ocr

    frames, stop = _watch_socket()
    ocr.request_interrupt("cyc-quiet")
    try:
        sink = ocr.reply_token_sink("cyc-quiet", "ep-quiet", pipeline_id=0)
        sink("token")                                    # spoken: suppressed
        sink.event("chat.say", text="I am still reading") # spoken: suppressed
        sink.reasoning("thought")                        # redacted marker: still flows
        sink.event("chat.tool_call", tool="x")           # seen: still flows
        sink.event("chat.tool_result", output="y")       # seen: still flows
        sink.event("chat.browser_frame", url="https://example.test")
    finally:
        stop()
        ocr.clear_interrupt("cyc-quiet")

    kinds = [str(frame.get("kind")) for frame in frames]
    assert "chat.reply_delta" not in kinds, "a barge-in must stop the speech in flight"
    assert "chat.say" not in kinds, "narration is spoken, so it is silenced too"
    assert kinds.count("chat.tool_call") == 1, "the work stayed invisible after a barge-in"
    assert kinds.count("chat.tool_result") == 1
    assert kinds.count("chat.browser_frame") == 1
    assert "chat.reasoning_delta" in kinds, "the redacted progress marker is not speech"


def test_the_speakable_kinds_are_declared_not_inferred(tmp_path, monkeypatch):
    """The suppression rule is a named set, so a new frame kind cannot accidentally become
    silenceable — or accidentally keep talking after an interrupt."""
    import pressure_loop.operator_chat_responses as ocr

    assert "chat.say" in ocr.SPEAKABLE_FRAME_KINDS
    assert "chat.reply_delta" in ocr.SPEAKABLE_FRAME_KINDS
    for seen_only in ("chat.tool_call", "chat.tool_result", "chat.browser_frame",
                      "chat.reasoning_delta"):
        assert seen_only not in ocr.SPEAKABLE_FRAME_KINDS, seen_only


# ── dev work is remembered (operator 2026-09-05: "save and recall dev work") ──
# The built substrate: memory.dev_artifact_intake holds a dev_artifact_snapshot
# per touched file in the episode's SRAM; DeepSleep commits it to the dev lobe
# (11); remember_dev recalls it. Only the retired skill path ever fed it.


# ── the agent lane runs one tool batch per episode (operator 2026-09-14) ──────
# Chat keeps web and connected-app tools; workspace work is the agent lane's, and
# every further step after a batch is an Omega successor episode.


def _agent_pack(**inputs):
    return _pack(execution_inputs={"operator_route": "agent", **inputs})


def test_agent_turn_runs_one_tool_batch_then_stops(tmp_path, monkeypatch):
    jail = tmp_path / "workspace"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(jail))

    rt = ToolRuntime(
        [[_call("start_job", {"project_root": "."}, "claim"), _call("task_plan", {"steps": ["do the work the operator asked for"]}, "plan"),
          _call("write_file", {"path": "hello.txt", "content": "hi"})]],
        final_text="never asked for",
    )

    res = execute(_operator_pp(), "TEXT_FAST", _agent_pack(), _mem(), "ep-batch",
                  runtime_client=rt, opened_by="operator_query", chat_cycle_id="cyc-batch")

    assert (jail / "hello.txt").read_text(encoding="utf-8") == "hi"
    generates = [kw for op, kw in rt.calls if op == "generate"]
    assert len(generates) == 1, "one model call per agent episode"
    assert [t["function"]["name"] for t in generates[0]["tools"]] == list(OPERATOR_TOOL_NAMES)
    assert res["failure_kind"] is None
    assert "response_text" not in res["outputs"], "Omega answers when it settles the work"
    ledger = res["outputs"]["operator_tool_calls"]
    assert [(row["tool_name"], row["status"]) for row in ledger] == [
        ("start_job", "success"), ("task_plan", "success"), ("write_file", "success")]


def test_chat_turn_is_offered_only_web_and_connected_app_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    rt = ToolRuntime([[_call("write_file", {"path": "x.txt", "content": "x"})]], final_text="I can't write files here.")

    res = execute(_operator_pp("write x.txt"), "TEXT_FAST", _pack(), _mem(), "ep-chat-tools",
                  runtime_client=rt, opened_by="operator_query", chat_cycle_id="cyc-chat-tools")

    first = rt.calls[0][1]
    # Chat keeps web research and connected apps. Writes belong to the agent lane, and so does
    # job control: a stop is resolved by ambiguity, not by lane (spec section 11, T12).
    assert [t["function"]["name"] for t in first["tools"]] == [
        "web_search", "web_fetch", "discover_plugin_tools", "call_plugin_tool"]
    assert not (tmp_path / "ws" / "x.txt").exists()
    assert "not available in the chat lane" in res["outputs"]["operator_tool_calls"][0]["error"]
    assert res["outputs"]["response_text"] == "I can't write files here."


def test_chat_web_search_files_knowledge_for_memory(tmp_path, monkeypatch):
    """A chat-lane lookup writes the same tool_result + artifact LEARN does."""
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    import schema.tools.web_fetch as wf

    def fake_web_fetch(query, max_results=3, domain=None, **kwargs):
        return {
            "query": query,
            "domain": "python",
            "results": [
                {
                    "title": "asyncio — Asynchronous I/O",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "content": "asyncio is a library to write concurrent code using the async/await syntax.",
                    "source": "python-docs",
                }
            ],
            "credible_sources": [{"name": "Python docs", "url": "https://docs.python.org/3/"}],
        }

    monkeypatch.setattr(wf, "web_fetch", fake_web_fetch)
    rt = ToolRuntime(
        [[_call("web_search", {"query": "python asyncio official docs"})]],
        final_text="asyncio is Python's concurrent I/O library.",
    )
    catalog = [
        {
            "name": "Python docs",
            "url": "https://docs.python.org/3/",
            "tier": "primary",
            "domain": "python",
            "note": "",
        }
    ]
    res = execute(
        _operator_pp("what is asyncio"),
        "TEXT_FAST",
        _pack(
            execution_inputs={
                "query": "what is asyncio",
                "web_research": True,
                "web_catalog": catalog,
                "domain": "python",
            }
        ),
        _mem(),
        "ep-chat-web-mem",
        runtime_client=rt,
        opened_by="operator_query",
        chat_cycle_id="cyc-chat-web-mem",
    )

    first = [kw for op, kw in rt.calls if op == "generate"][0]
    assert first["messages"][-1]["role"] == "user"
    assert "https://docs.python.org/3/" in first["messages"][-1]["content"]
    assert res["outputs"]["tool_result"]["results"][0]["url"] == (
        "https://docs.python.org/3/library/asyncio.html"
    )
    assert res["outputs"]["knowledge_artifact_id"]
    assert res["outputs"]["response_text"] == "asyncio is Python's concurrent I/O library."


def test_agent_tool_failures_are_ledger_facts_not_crashes(tmp_path, monkeypatch):
    jail = tmp_path / "workspace"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(jail))
    rt = ToolRuntime([[
        _call("read_files", {"path": "../escape.txt"}),
        _call("no_such_tool", {}, "call2"),
    ]])

    res = execute(_operator_pp(), "TEXT_FAST", _agent_pack(), _mem(), "ep-toolfail",
                  runtime_client=rt, opened_by="operator_query", chat_cycle_id="cyc-toolfail")

    ledger = res["outputs"]["operator_tool_calls"]
    assert [row["status"] for row in ledger] == ["failure", "failure"]
    assert "outside workspace jail" in ledger[0]["error"]
    assert "unknown tool" in ledger[1]["error"]
    assert res["failure_kind"] is None


def test_an_agent_batch_streams_reasoning_calls_and_results_to_the_bubble(tmp_path, monkeypatch):
    jail = tmp_path / "workspace"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(jail))
    jail.mkdir()
    (jail / "hello.txt").write_text("old line\n", encoding="utf-8")
    rt = StreamingToolRuntime(
        [[_call("start_job", {"project_root": "."}, "claim"), _call("task_plan", {"steps": ["do the work the operator asked for"]}, "plan"),
          _call("edit_file", {"path": "hello.txt", "old_text": "old line", "new_text": "new line"})]],
        reasoning="the operator wants the line changed",
    )
    frames, stop = _watch_socket()
    try:
        res = execute(_operator_pp("change old line to new line in hello.txt"), "TEXT_FAST", _agent_pack(), _mem(),
                      "ep-stream", runtime_client=rt, opened_by="operator_query", chat_cycle_id="cyc-stream")
    finally:
        stop()

    assert (jail / "hello.txt").read_text(encoding="utf-8") == "new line\n"
    assert res["outputs"]["reasoning"] == "the operator wants the line changed"
    assert "response_text" not in res["outputs"], "the batch's narration is progress, not the answer"
    assert len(rt.stream_calls) == 1
    assert [f["kind"] for f in frames] == [
        "chat.reasoning_delta", "chat.reply_delta",
        "chat.tool_call", "chat.tool_result",   # start_job
        "chat.tool_call", "chat.tool_result",   # task_plan
        "chat.tool_call", "chat.tool_result",   # edit_file
    ]
    assert all(f["cycle_id"] == "cyc-stream" for f in frames)
    call = [f for f in frames if f["kind"] == "chat.tool_call"][2]
    assert call["tool"] == "edit_file" and call["call_id"] == "call1"
    result = [f for f in frames if f["kind"] == "chat.tool_result"][2]
    assert result["ok"] is True and "-old line" in result["diff"] and "+new line" in result["diff"]
    assert "+new line" in res["outputs"]["operator_tool_calls"][2]["diff"]   # claim, plan, then the edit


def test_a_failed_agent_tool_streams_its_error_as_a_result_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    rt = StreamingToolRuntime([[_call("read_files", {"path": "../secret"})]])
    frames, stop = _watch_socket()
    try:
        execute(_operator_pp("read ../secret"), "TEXT_FAST", _agent_pack(), _mem(), "ep-toolerr",
                runtime_client=rt, opened_by="operator_query", chat_cycle_id="cyc-toolerr")
    finally:
        stop()
    result = [f for f in frames if f["kind"] == "chat.tool_result"][0]
    assert result["ok"] is False and "outside workspace jail" in result["error"] and result["diff"] == ""


@pytest.mark.parametrize("tool_name", [
    "web_search", "web_fetch", "discover_plugin_tools", "call_plugin_tool",
])
def test_slow_external_tools_stream_wait_progress(tmp_path, monkeypatch, tool_name):
    import threading

    from auto_loop.execute import brain_skill_executor as executor
    import importlib

    auto_run = importlib.import_module("auto_loop.execute.run")
    from pressure_loop.operator_chat_responses import reply_token_sink

    monkeypatch.setattr(auto_run, "_OPERATOR_PROGRESS_INTERVAL_S", 0.01, raising=False)
    entered = threading.Event()
    release = threading.Event()

    def slow_tool(name, _arguments, _ctx):
        entered.set()
        assert release.wait(2), "test did not release the simulated external call"
        return executor.ToolResult(True, output=f"{name} completed"), json.loads(_arguments)

    monkeypatch.setattr(executor, "run_operator_tool", slow_tool)
    frames, stop = _watch_socket()
    result = []
    args = {"query": "test"} if tool_name in {"web_search", "web_fetch", "discover_plugin_tools"} else {
        "candidate_id": "test", "arguments": {},
    }
    sink = reply_token_sink("cyc-external-progress", "ep-external-progress", pipeline_id=0)

    def generate(**kwargs):
        if kwargs.get("tools"):
            return {"tool_calls": [_job_call(tool_name, args)]}
        return {"text": "finished", "tool_calls": []}

    def run_turn():
        result.append(_job_reply(generate, token_sink=sink))

    worker = threading.Thread(target=run_turn)
    try:
        worker.start()
        assert entered.wait(2), "the external tool handler did not start"
        import time

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any(
            frame.get("kind") == "chat.tool_progress" for frame in frames
        ):
            time.sleep(0.005)
    finally:
        release.set()
        worker.join(2)
        stop()

    assert not worker.is_alive(), "the simulated operator turn did not finish"
    progress = [frame for frame in frames if frame.get("kind") == "chat.tool_progress"]
    assert progress, f"{tool_name} stayed silent while its handler was waiting"
    assert progress[0]["call_id"] == "call"
    assert progress[0]["tool"] == tool_name
    assert "elapsed" in progress[0]["message"]
    assert result and result[0][0]["text"] == "finished"


def test_files_the_agent_tools_touch_are_held_for_the_dev_lobe(tmp_path, monkeypatch):
    jail = (tmp_path / "workspace").resolve()
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(jail))
    from memory import episode_sram

    episode_sram.reset()
    episode_sram.open_bucket("ep-remember", pipeline_id=0)
    try:
        rt = ToolRuntime([[
            _call("start_job", {"project_root": "."}, "claim"),
            _call("task_plan", {"steps": ["do the work the operator asked for"]}, "plan"),
            _call("write_file", {"path": "todo.py", "content": "tasks = []\n"}),
            _call("run_command", {"command": "Set-Content -LiteralPath made.txt -Value x"}, "c2"),
        ]])
        res = execute(_operator_pp("build a todo app"), "TEXT_FAST", _agent_pack(), _mem(), "ep-remember",
                      runtime_client=rt, opened_by="operator_query", chat_cycle_id="cyc-remember")
        held = [item.envelope for item in episode_sram.pending("ep-remember") if item.kind == "memory"]
    finally:
        episode_sram.reset()

    artifacts = [env for env in held if env.type == "dev_artifact_snapshot"]
    observations = [env for env in held if env.type == "dev_tool_observation"]
    assert sorted(env.payload["relative_path"] for env in artifacts) == ["made.txt", "todo.py"]
    assert all(env.coord == [11, 0, 0] for env in artifacts)
    assert {tuple(env.coord) for env in observations} == {(11, 4, 0), (11, 7, 0)}
    todo = next(env for env in artifacts if env.payload["relative_path"] == "todo.py")
    assert todo.payload["content"] == "tasks = []\n" and todo.payload["objective"] == "build a todo app"
    ledger = res["outputs"]["operator_tool_calls"]
    assert ledger[2]["touched_files"] == [str(jail / "todo.py")] and ledger[2]["held_in_memory"] == 1
    assert ledger[2]["dev_artifact_hold"] == {"status": "held", "held_count": 1}
    assert [p.split("\\")[-1].split("/")[-1] for p in ledger[3]["touched_files"]] == ["made.txt"]


def test_dev_artifact_hold_distinguishes_empty_intake_from_failure(monkeypatch):
    from auto_loop.execute.run import _hold_dev_artifacts
    from memory import dev_artifact_intake, episode_sram

    episode_sram.reset()
    episode_sram.open_bucket("ep-hold-result", pipeline_id=0)
    try:
        monkeypatch.setattr(dev_artifact_intake, "hold_dev_artifact_snapshots", lambda **_kwargs: [])
        empty = _hold_dev_artifacts("ep-hold-result", "build", ["a.txt"])

        def fail_intake(**_kwargs):
            raise RuntimeError("snapshot store unavailable")

        monkeypatch.setattr(dev_artifact_intake, "hold_dev_artifact_snapshots", fail_intake)
        failed = _hold_dev_artifacts("ep-hold-result", "build", ["a.txt"])
    finally:
        episode_sram.reset()

    assert empty == {
        "status": "empty", "held_count": 0, "reason": "no eligible file snapshots",
    }
    assert failed["status"] == "failed" and failed["held_count"] == 0
    assert failed["reason"] == "RuntimeError: snapshot store unavailable"


def test_no_open_sram_bucket_means_the_agent_work_still_happens(tmp_path, monkeypatch):
    jail = tmp_path / "ws"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(jail))
    from memory import episode_sram

    episode_sram.reset()
    frames, stop = _watch_socket()
    rt = ToolRuntime([[_call("start_job", {"project_root": "."}, "claim"), _call("task_plan", {"steps": ["do the work the operator asked for"]}, "plan"),
                       _call("write_file", {"path": "a.txt", "content": "x"})]])
    try:
        res = execute(_operator_pp("write a.txt"), "TEXT_FAST", _agent_pack(), _mem(), "ep-nobucket",
                      runtime_client=rt, opened_by="operator_query", chat_cycle_id="cyc-nobucket")
    finally:
        stop()
    assert (jail / "a.txt").read_text() == "x"
    ledger = res["outputs"]["operator_tool_calls"][2]   # start_job, task_plan, then the write
    assert ledger["touched_files"] and ledger["held_in_memory"] == 0
    assert ledger["dev_artifact_hold"]["status"] == "unavailable"
    assert "no open SRAM bucket" in ledger["dev_artifact_hold"]["reason"]
    result_frame = [frame for frame in frames if frame.get("kind") == "chat.tool_result"][-1]
    assert result_frame["dev_artifact_hold"] == ledger["dev_artifact_hold"]


def test_an_agent_successor_runs_its_next_batch_with_the_earlier_results(tmp_path, monkeypatch):
    jail = tmp_path / "ws"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(jail))
    jail.mkdir()
    (jail / "notes.txt").write_text("todo: ship it", encoding="utf-8")
    rt = ToolRuntime([[_call("read_files", {"path": "notes.txt"})]])
    pack = _agent_pack(
        agent_continuation=True,
        agent_next_step="read notes.txt to find the open item",
        agent_observations=[{"tool": "list_directory", "args": '{"path": "."}', "status": "success",
                             "output": "FILE\tnotes.txt", "error": ""}],
    )

    res = execute(_operator_pp("tidy the notes"), "TEXT_FAST", pack, _mem(), "ep-next-batch",
                  runtime_client=rt, opened_by="omega_successor", chat_cycle_id="cyc-origin")

    generates = [kw for op, kw in rt.calls if op == "generate"]
    assert len(generates) == 1 and generates[0]["tools"]
    user_turn = generates[0]["messages"][1]["content"]
    assert "CONTINUING THE OPERATOR'S TASK" in user_turn
    assert "read notes.txt to find the open item" in user_turn and "FILE\tnotes.txt" in user_turn
    assert res["outputs"]["operator_tool_calls"][0]["status"] == "success"
