"""WO-05 carry-over: capture wired into the real producing seams, not only the receipt builders.

WO-05 built the receipt contracts and wired ONE seam (``run_operator_tool``). Its report declared
what was left: ``evidence_class`` was not bound per event (``record_event`` hardcoded
``host_observation``), and artifact, verification and delivery receipts were not emitted from the
real writers. These tests drive the real seams.
"""

from __future__ import annotations

import pytest

from tests.helpers.episode_owner import owned


class _Episode:
    def __init__(self, episode_id: str) -> None:
        self.episode_id = episode_id


# ── evidence class per event ─────────────────────────────────────────────────


def test_an_event_carries_the_evidence_class_its_producer_states():
    from memory.episode_events import read_episode_events, record_event

    with owned("ep-class"):
        event = record_event("ep-class", operation_id="op-class-1", phase="auto",
                             event_kind="auto.test_observation", payload={"note": "x"},
                             evidence_class="external_content")
        default = record_event("ep-class", operation_id="op-class-2", phase="auto",
                               event_kind="auto.test_observation", payload={"note": "y"})
        stored = {e.operation_id: e.evidence_class for e in read_episode_events("ep-class")}
    assert event.evidence_class == "external_content"
    assert default.evidence_class == "host_observation"
    assert stored == {"op-class-1": "external_content", "op-class-2": "host_observation"}


def test_an_unknown_evidence_class_is_refused():
    from memory.episode_events import record_event
    from schema.continuity_json import ContinuityContractError

    with owned("ep-class-bad"), pytest.raises(ContinuityContractError):
        record_event("ep-class-bad", operation_id="op-bad", phase="auto",
                     event_kind="auto.test_observation", payload={}, evidence_class="rumour")


def test_a_verification_is_recorded_as_a_verification_result_not_a_plain_observation():
    from memory.auto_evidence import capture_verification

    episode = _Episode("ep-verify-class")
    with owned(episode):
        event = capture_verification(episode, criterion="the parser accepts empty input",
                                     path="parser.py", revision=1,
                                     content_sha256="a" * 64, passed=True)
    assert event.evidence_class == "verification_result"


# ── artifact revisions from the real writers ─────────────────────────────────


def _sha(path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _call(call_id: str, name: str, **arguments) -> dict:
    import json

    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _agent_batch(calls: list[dict], *, episode: str, cycle: str):
    """ONE agent episode: the lane runs one tool batch per episode (run.py)."""
    from auto_loop.execute.run import _operator_tools_exchange

    responses = iter([{"text": "", "tool_calls": calls}, {"text": "done"}])
    return _operator_tools_exchange(
        lambda **_kwargs: next(responses), system="sys", prompt="build", objective="build",
        episode_id=episode, max_tokens=100, enable_thinking=False, timeout=None,
        chat_cycle_id=cycle, warnings=[], operator_route="agent",
        permission_mode="always_allow", runtime_client=object(), job_id="",
        session={"records": [], "owned": False, "job_id": "", "cursor": 0},
    )


def _artifacts(episode_id: str) -> list[dict]:
    from memory.auto_evidence import ARTIFACT_REVISION
    from memory.episode_events import read_episode_events

    return [dict(e.payload) for e in read_episode_events(episode_id)
            if e.event_kind == ARTIFACT_REVISION]


def test_the_agent_lane_records_each_revision_its_tools_actually_wrote(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(workspace))
    with owned("ep-artifacts"):
        _, records, _ = _agent_batch([
            _call("c0", "start_job", project_root="."),
            _call("p0", "task_plan", steps=["write a.txt", "make b.txt"]),
            _call("c1", "write_file", path="a.txt", content="one\ntwo\n"),
            _call("c2", "edit_file", path="a.txt", old_text="two", new_text="three"),
            _call("c3", "write_file", path="a.txt", content="one\nthree\n"),  # identical bytes
            _call("c4", "run_command", command="Set-Content -LiteralPath b.txt -Value made"),
        ], episode="ep-artifacts", cycle="cycle-artifacts")
        rows = _artifacts("ep-artifacts")

    assert [r.get("status") for r in records] == ["success"] * 6, records
    a, b = workspace / "a.txt", workspace / "b.txt"
    assert [(r["path"], r["revision"], r["change_kind"]) for r in rows] == [
        ("a.txt", 1, "created"), ("a.txt", 2, "changed"), ("b.txt", 1, "created")], (
        "one revision per real byte change; an identical rewrite is not a new revision")
    assert rows[1]["content_sha256"] == _sha(a)
    assert rows[2]["content_sha256"] == _sha(b)


# ── the USE_SKILL tool loop captures its attempts, results and revisions ─────


class _SkillBrain:
    """Claims the project, writes one file, tries a tool outside the dispatch, then answers."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_completion(self, prompt, **kwargs):
        self.calls += 1
        steps = {
            1: [("claim", "start_job", '{"project_root":"."}')],
            2: [("write", "write_file", '{"path":"hello.txt","content":"hello"}'),
                ("stray", "delete_everything", '{}')],
        }
        if self.calls in steps:
            return {"ok": True, "finish_reason": "tool_calls", "text": "", "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
                for cid, name, args in steps[self.calls]]}
        return {"ok": True, "text": "Created hello.txt.", "finish_reason": "stop"}


def test_the_skill_tool_loop_captures_attempts_results_and_revisions(tmp_path):
    from types import SimpleNamespace

    import core.trusted_folders as trusted
    from auto_loop.execute.brain_skill_executor import run_brain_skill_dispatch
    from memory.auto_evidence import TOOL_ATTEMPT, TOOL_RESULT
    from memory.episode_events import read_episode_events

    trusted.grant_trusted_folder(tmp_path)
    trusted.set_active_trusted_folder(tmp_path)
    dispatch = SimpleNamespace(
        episode_id="ep-skill", dispatch_id="d-skill", objective="create hello.txt",
        execution_route="development", allowed_tools=("write_file", "read_files"),
        forbidden_tools=(), limits={"max_steps": 5, "max_duration_ms": 30_000,
                                    "max_files_changed": 5})
    with owned("ep-skill"):
        run_brain_skill_dispatch("directive", dispatch, client=_SkillBrain(),
                                 workspace_root=tmp_path, cycle_id="cycle-skill", job_id="job-skill")
        events = read_episode_events("ep-skill")

    attempts = {e.payload["attempt_token"]: e.payload for e in events if e.event_kind == TOOL_ATTEMPT}
    results = {e.payload["attempt_token"]: e.payload for e in events if e.event_kind == TOOL_RESULT}
    by_tool = {row["tool"]: results[token] for token, row in attempts.items()}
    assert set(attempts) == set(results), "every attempt has its result under the same token"
    assert by_tool["write_file"]["ok"] is True and by_tool["write_file"]["effect"] == "applied"
    assert by_tool["delete_everything"]["effect"] == "no_effect", "refused before any handler ran"
    assert by_tool["delete_everything"]["error_kind"] == "not_in_lane"
    revisions = [(e.payload["path"], e.payload["revision"], e.payload["change_kind"])
                 for e in events if e.event_kind == "auto.artifact_revision"]
    assert revisions == [("hello.txt", 1, "created")]


# ── the browser lane captures every action attempt and its honest effect ─────


def _results_by_tool(events) -> dict[str, list[dict]]:
    from memory.auto_evidence import TOOL_ATTEMPT, TOOL_RESULT

    attempts = {e.payload["attempt_token"]: e.payload["tool"] for e in events
                if e.event_kind == TOOL_ATTEMPT}
    out: dict[str, list[dict]] = {}
    for event in events:
        if event.event_kind == TOOL_RESULT:
            out.setdefault(attempts[event.payload["attempt_token"]], []).append(dict(event.payload))
    return out


def test_the_browser_loop_records_each_action_with_the_effect_it_can_prove():
    from browser_agent.agent_loop import BrowserAgentRun
    from memory.auto_evidence import attempt_recorder
    from memory.episode_events import read_episode_events
    from tests.browser_agent.test_agent_loop import FakeController, Seat, call

    controller = FakeController()
    controller.fail_next = ["click"]
    seat = Seat(call("navigate", url="https://example.test/"),
                call("click", x=99999, y=5, target="off the screen"),   # refused by validation
                call("type_text", text="hi"),                            # no focused field
                call("click", x=784, y=300, target="search box"),        # the controller fails
                call("done", summary="stopped", evidence="n/a"))
    with owned("ep-browser"):
        recorder = attempt_recorder("ep-browser", lane="browser")
        BrowserAgentRun(seat, controller, objective="find pricing", capture=recorder).run()
        results = _results_by_tool(read_episode_events("ep-browser"))

    assert [r["effect"] for r in results["navigate"]] == ["applied"]
    clicks = results["click"]
    assert (clicks[0]["error_kind"], clicks[0]["effect"]) == ("arguments_invalid", "no_effect")
    assert (clicks[1]["error_kind"], clicks[1]["effect"]) == ("handler_raised", "unknown_effect"), (
        "a mutating action that raised may have happened; that is never a no-effect claim")
    typed = results["type_text"][0]
    assert (typed["error_kind"], typed["effect"]) == ("browser_unavailable", "no_effect")


def test_a_thread_owning_no_episode_gets_no_recorder():
    from memory.auto_evidence import attempt_recorder

    assert attempt_recorder("ep-nobody", lane="browser") is None


def test_the_auto_browser_run_hands_the_loop_a_recorder_for_its_episode():
    """Structural pin on the one call site: AUTO's browser run passes the episode's recorder."""
    import importlib
    import inspect

    run_mod = importlib.import_module("auto_loop.execute.run")
    source = inspect.getsource(run_mod._execute_browser_run)
    assert "agent_loop.run_browser_agent(" in source
    assert 'capture=attempt_recorder(episode_id, lane="browser")' in source


# ── plugin steps: attempts, results and — for a send — delivery stages ───────


def _plugin_binding(risk, *, capability="list_events", output_id=False):
    from dataclasses import replace

    from mcp_platform.canonical import canonical_bytes, sha256_hex
    from tests.auto_loop.test_plugin_plan import _binding

    binding = _binding(candidate="a" if risk.value == "read_only" else "b", capability=capability,
                       title=capability.replace("_", " "), risk=risk,
                       properties={"query": {"type": "string"}}, required=["query"])
    if output_id:
        schema = {"type": "object", "properties": {"item": {"type": "string"},
                                                    "messageId": {"type": "string"}},
                  "required": ["item"], "additionalProperties": False}
        binding = replace(binding, output_schema=schema,
                          output_schema_sha256=sha256_hex(canonical_bytes(schema)))
    return binding


def _plugin_events(episode_id):
    from memory.auto_evidence import DELIVERY_OBSERVATION
    from memory.episode_events import read_episode_events

    events = read_episode_events(episode_id)
    return _results_by_tool(events), [dict(e.payload) for e in events
                                      if e.event_kind == DELIVERY_OBSERVATION]


def test_a_read_only_plugin_step_records_its_attempt_and_result():
    from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
    from mcp_platform.package_contracts import RiskClass
    from tests.auto_loop.test_plugin_plan import _Runtime

    runtime = _Runtime((_plugin_binding(RiskClass.READ_ONLY),))
    with owned("ep-plugin-read"):
        outcome = plan_and_execute_plugin_request("show events", episode_id="ep-plugin-read",
                                                  runtime_client=runtime)
        results, deliveries = _plugin_events("ep-plugin-read")
    assert outcome.status == "completed", outcome.failure_reason
    [(tool, rows)] = results.items()
    assert tool == "plugin.synthetic:list_events" and rows[0]["effect"] == "applied"
    assert deliveries == [], "a read is not an outward delivery"


def test_a_failed_read_only_plugin_step_moved_nothing():
    from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
    from mcp_platform.package_contracts import RiskClass
    from mcp_platform.plugin_runtime import PluginRuntimeError
    from tests.auto_loop.test_plugin_plan import _RaisingBackend, _Runtime

    runtime = _Runtime((_plugin_binding(RiskClass.READ_ONLY),),
                       backend=_RaisingBackend(PluginRuntimeError("plugin_operation_timeout")))
    with owned("ep-plugin-read-fail"):
        plan_and_execute_plugin_request("show events", episode_id="ep-plugin-read-fail",
                                        runtime_client=runtime)
        results, _ = _plugin_events("ep-plugin-read-fail")
    [rows] = results.values()
    assert rows[0]["ok"] is False and rows[0]["effect"] == "no_effect"


class _SendingBackend:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def call_tool_sync(self, app_id, name, arguments):
        self.calls.append((app_id, name, dict(arguments)))
        if self.error is not None:
            raise self.error
        return {"item": "sent", "messageId": "msg-123"}


def _send_runtime(backend):
    from mcp_platform.package_contracts import RiskClass
    from mcp_platform.plugin_runtime import PluginRuntime
    from tests.auto_loop.test_plugin_plan import (
        _app_record, _AppRegistry, _DirectRuntime, _PluginRegistry)

    runtime = _DirectRuntime(_plugin_binding(RiskClass.SEND_PUBLISH, capability="follow_up",
                                             output_id=True))
    runtime.backend = backend
    runtime.plugin_runtime = PluginRuntime(plugin_registry=_PluginRegistry(),
                                           app_registry=_AppRegistry(_app_record()), backend=backend)
    return runtime


def test_a_confirmed_send_records_attempted_then_confirmed_with_the_provider_receipt():
    from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request

    runtime = _send_runtime(_SendingBackend())
    with owned("ep-plugin-send"):
        outcome = plan_and_execute_plugin_request("follow up with the team", episode_id="ep-plugin-send",
                                                  runtime_client=runtime)
        results, deliveries = _plugin_events("ep-plugin-send")
    assert outcome.status == "completed", outcome.failure_reason
    assert [d["state"] for d in deliveries] == ["attempted", "confirmed"]
    assert deliveries[1]["provider_receipt"] == "msg-123" and deliveries[1]["delivered"] is True


def test_an_interrupted_send_is_uncertain_and_never_marked_safe_to_retry():
    from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
    from mcp_platform.plugin_runtime import PluginRuntimeError

    runtime = _send_runtime(_SendingBackend(PluginRuntimeError("plugin_operation_timeout")))
    with owned("ep-plugin-send-fail"):
        plan_and_execute_plugin_request("follow up with the team", episode_id="ep-plugin-send-fail",
                                        runtime_client=runtime)
        results, deliveries = _plugin_events("ep-plugin-send-fail")
    [rows] = results.values()
    assert rows[0]["effect"] == "unknown_effect"
    assert [d["state"] for d in deliveries] == ["attempted", "uncertain"]
    assert deliveries[1]["safe_to_retry"] is False and "plugin_operation_timeout" in deliveries[1]["reason"]


# ── verification: a test run declared against one of the objective's criteria ─

CRITERION = "the parser accepts empty input"


def _fake_runner(monkeypatch, exit_code: int):
    """Only the subprocess is substituted; the handler, its criterion check and capture are real."""
    from auto_loop.execute import brain_skill_executor as bse

    monkeypatch.setattr(bse, "_run_command", lambda args, ctx: bse.ToolResult(
        exit_code == 0, output=f"[exit {exit_code}]",
        meta={"exit_code": exit_code, "stdout": "", "stderr": ""}))


def _criteria_batch(calls, *, episode, criteria=(CRITERION,)):
    from auto_loop.execute.run import _operator_tools_exchange

    responses = iter([{"text": "", "tool_calls": calls}, {"text": "done"}])
    return _operator_tools_exchange(
        lambda **_kwargs: next(responses), system="sys", prompt="build", objective="build",
        episode_id=episode, max_tokens=100, enable_thinking=False, timeout=None,
        chat_cycle_id=f"cycle-{episode}", warnings=[], operator_route="agent",
        permission_mode="always_allow", runtime_client=object(), job_id="",
        session={"records": [], "owned": False, "job_id": "", "cursor": 0},
        acceptance_criteria=tuple(criteria),
    )


def _program():
    return {"program_id": "mp-parser", "outcome_contract": {"acceptance_criteria": [CRITERION]}}


def test_a_declared_test_run_verifies_the_exact_bytes_and_a_later_edit_makes_it_stale(
        tmp_path, monkeypatch):
    from memory.auto_evidence import VERIFICATION_RESULT
    from memory.episode_events import read_episode_events
    from meta_loop.checks.run import run as check_criteria

    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    _fake_runner(monkeypatch, 0)
    head = [_call("c0", "start_job", project_root="."),
            _call("p0", "task_plan", steps=["write", "test", "refine"]),
            _call("c1", "write_file", path="parser.py", content="def parse(s):\n    return []\n"),
            _call("c2", "run_tests", command="pytest -q", criterion=CRITERION, path="parser.py")]
    with owned("ep-verify"):
        _, records, _ = _criteria_batch(head, episode="ep-verify")
        verified = check_criteria("ep-verify", _program())
        [receipt] = [dict(e.payload) for e in read_episode_events("ep-verify")
                     if e.event_kind == VERIFICATION_RESULT]
    assert [r["status"] for r in records] == ["success"] * 4, records
    assert receipt["passed"] is True and receipt["path"] == "parser.py"
    assert receipt["content_sha256"] == _sha(tmp_path / "ws" / "parser.py")
    assert verified["verdict"] == "passed", verified

    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws-stale"))  # its own project
    with owned("ep-verify-stale"):
        _criteria_batch(head + [_call("c3", "edit_file", path="parser.py",
                                      old_text="return []", new_text="return None")],
                        episode="ep-verify-stale")
        stale = check_criteria("ep-verify-stale", _program())
    assert stale["results"][0]["status"] == "stale", stale


def test_a_failing_declared_run_records_a_failed_verification(tmp_path, monkeypatch):
    from meta_loop.checks.run import run as check_criteria

    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    _fake_runner(monkeypatch, 1)
    with owned("ep-verify-fail"):
        _criteria_batch([_call("c0", "start_job", project_root="."),
                         _call("p0", "task_plan", steps=["write", "test"]),
                         _call("c1", "write_file", path="parser.py", content="x = 1\n"),
                         _call("c2", "run_tests", command="pytest -q", criterion=CRITERION,
                               path="parser.py")], episode="ep-verify-fail")
        verdict = check_criteria("ep-verify-fail", _program())
    assert verdict["verdict"] == "failed", verdict


def test_a_criterion_the_objective_never_stated_is_refused_before_anything_runs(tmp_path, monkeypatch):
    from memory.auto_evidence import VERIFICATION_RESULT
    from memory.episode_events import read_episode_events

    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    ran = []
    from auto_loop.execute import brain_skill_executor as bse

    monkeypatch.setattr(bse, "_run_command", lambda args, ctx: ran.append(args) or bse.ToolResult(True))
    with owned("ep-verify-bogus"):
        _, records, _ = _criteria_batch([
            _call("c0", "start_job", project_root="."),
            _call("p0", "task_plan", steps=["test"]),
            _call("c1", "write_file", path="parser.py", content="x = 1\n"),
            _call("c2", "run_tests", command="pytest -q", criterion="everything works",
                  path="parser.py")], episode="ep-verify-bogus")
        events = read_episode_events("ep-verify-bogus")
    assert records[-1]["status"] == "failure" and "acceptance criteria" in records[-1]["error"]
    assert ran == [], "the refused run never started"
    assert not [e for e in events if e.event_kind == VERIFICATION_RESULT]


def test_the_skill_loop_also_records_a_declared_verification(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import core.trusted_folders as trusted
    from auto_loop.execute.brain_skill_executor import run_brain_skill_dispatch
    from meta_loop.checks.run import run as check_criteria

    trusted.grant_trusted_folder(tmp_path)
    trusted.set_active_trusted_folder(tmp_path)
    _fake_runner(monkeypatch, 0)

    class _Brain:
        calls = 0

        def generate_completion(self, prompt, **kwargs):
            _Brain.calls += 1
            steps = {
                1: [("claim", "start_job", '{"project_root":"."}')],
                2: [("w", "write_file", '{"path":"parser.py","content":"x = 1"}'),
                    ("t", "run_tests",
                     '{"command":"pytest -q","criterion":"%s","path":"parser.py"}' % CRITERION)],
            }
            if _Brain.calls in steps:
                return {"ok": True, "finish_reason": "tool_calls", "text": "", "tool_calls": [
                    {"id": cid, "type": "function", "function": {"name": n, "arguments": a}}
                    for cid, n, a in steps[_Brain.calls]]}
            return {"ok": True, "text": "done", "finish_reason": "stop"}

    dispatch = SimpleNamespace(
        episode_id="ep-skill-verify", dispatch_id="d", objective="parser",
        execution_route="development", allowed_tools=("write_file", "run_tests"),
        forbidden_tools=(), limits={"max_steps": 5, "max_duration_ms": 30_000, "max_files_changed": 5},
        motor_program=_program())
    with owned("ep-skill-verify"):
        run_brain_skill_dispatch("directive", dispatch, client=_Brain(), workspace_root=tmp_path,
                                 cycle_id="cycle-sv", job_id="job-sv")
        verdict = check_criteria("ep-skill-verify", _program())
    assert verdict["verdict"] == "passed", verdict


# ── inference request identity: streamed requests too, recorded per model call ─


class _StreamReply:
    status_code = 200
    text = ""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def iter_lines(self, decode_unicode=True):
        yield from self._lines


class _StreamSession:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        self.sent.append(json)
        return _StreamReply(['data: {"choices":[{"delta":{"content":"hi"}}]}', "data: [DONE]"])


def _cloud_seat(monkeypatch, tmp_path):
    import json as _json

    import runtime.brain_cloud_config as config

    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    monkeypatch.setattr(config, "_connector_store_root", lambda: tmp_path / "connectors")
    for env in ("OMEGA_MIND_BACKEND", "OMEGA_MIND_PROVIDER", "OMEGA_MIND_MODEL"):
        monkeypatch.delenv(env, raising=False)
    (tmp_path / "mind_backend.json").write_text(_json.dumps({
        "enabled": True, "provider": "mistral", "model": "mistral-medium-3-5",
        "base_url": "https://api.mistral.ai/v1"}), encoding="utf-8")


def test_a_streamed_cloud_request_reports_the_digest_of_the_body_it_transmitted(monkeypatch, tmp_path):
    import runtime.brain_cloud as cloud
    from schema.continuity_json import content_hash

    _cloud_seat(monkeypatch, tmp_path)
    session = _StreamSession()
    monkeypatch.setattr(cloud, "_ensure_session", lambda: session)
    events = list(cloud.generate_stream_events({"prompt": "reflect", "system": "you are Omega"}))
    done = events[-1]
    assert done[0] == "done"
    assert done[1].get("request_digest") == content_hash(session.sent[0]), (
        "a streamed request carried no digest of the body it actually posted")


def test_the_streamed_completion_body_carries_the_request_digest(monkeypatch, tmp_path):
    import runtime.brain_cloud as cloud
    from runtime.client import RuntimeClient
    from schema.continuity_json import content_hash

    _cloud_seat(monkeypatch, tmp_path)
    session = _StreamSession()
    monkeypatch.setattr(cloud, "_ensure_session", lambda: session)
    body = RuntimeClient().generate_completion_stream("reflect", system="you are Omega")
    assert body["text"] == "hi"
    assert body.get("request_digest") == content_hash(session.sent[0])


def test_each_model_call_is_recorded_with_its_request_identity_or_its_absence():
    from memory.auto_evidence import INFERENCE_REQUEST, capture_inference
    from memory.episode_events import read_episode_events

    episode = _Episode("ep-inference")
    with owned(episode):
        capture_inference(episode, operation="auto_response",
                          body={"ok": True, "text": "x", "request_digest": "d" * 64,
                                "model": "m", "reasoning": "private"})
        capture_inference(episode, operation="auto_response", body={"ok": True, "text": "x"})
        rows = [dict(e.payload) for e in read_episode_events("ep-inference")
                if e.event_kind == INFERENCE_REQUEST]
    assert rows[0]["request_digest"] == "d" * 64 and "reasoning" not in rows[0]
    assert rows[1]["request_digest"] == "" and rows[1]["request_digest_state"] == "not_captured"


def test_auto_records_every_generate_pass_and_the_skill_loop_every_model_call(tmp_path):
    import importlib
    import inspect
    from types import SimpleNamespace

    import core.trusted_folders as trusted
    from auto_loop.execute.brain_skill_executor import run_brain_skill_dispatch
    from memory.auto_evidence import INFERENCE_REQUEST
    from memory.episode_events import read_episode_events

    source = inspect.getsource(importlib.import_module("auto_loop.execute.run").execute)
    assert "_record_inference(operation, res)" in source, "AUTO's _generate records each pass"

    trusted.grant_trusted_folder(tmp_path)
    trusted.set_active_trusted_folder(tmp_path)
    dispatch = SimpleNamespace(
        episode_id="ep-skill-inference", dispatch_id="d", objective="create hello.txt",
        execution_route="development", allowed_tools=("write_file",), forbidden_tools=(),
        limits={"max_steps": 5, "max_duration_ms": 30_000, "max_files_changed": 5})
    brain = _SkillBrain()
    with owned("ep-skill-inference"):
        run_brain_skill_dispatch("directive", dispatch, client=brain, workspace_root=tmp_path,
                                 cycle_id="cycle-si", job_id="job-si")
        rows = [e for e in read_episode_events("ep-skill-inference")
                if e.event_kind == INFERENCE_REQUEST]
    assert len(rows) == brain.calls, "one inference event per model call"


# ── the execution report carries the receipts, not only bare paths ──────────


def test_the_execution_report_is_bound_to_the_episodes_revision_and_verification_receipts(
        monkeypatch):
    from cognition_chain import _hold_execution_report_from_fold
    from memory.auto_evidence import capture_artifact_revision, capture_verification
    from memory.execution_report import ExecutionReport, current_execution_report

    episode = _Episode("ep-report")
    with owned(episode):
        capture_artifact_revision(episode, path="parser.py", revision=1,
                                  content_sha256="a" * 64, change_kind="created")
        capture_verification(episode, criterion=CRITERION, path="parser.py", revision=1,
                             content_sha256="a" * 64, passed=True)
        _hold_execution_report_from_fold(
            episode, {"files_changed": ["parser.py"], "status": "success"}, {},
            build_task="build the parser", pass_gate=(CRITERION,),
            verification={"checks": [], "all_green": True}, original_request="parser",
            origin_opened_by="operator")
        report = current_execution_report()

    assert [dict(r) for r in report.artifact_revisions] == [
        {"path": "parser.py", "revision": 1, "content_sha256": "a" * 64, "change_kind": "created"}]
    assert [dict(r) for r in report.verification_receipts] == [
        {"criterion": CRITERION, "path": "parser.py", "revision": 1,
         "content_sha256": "a" * 64, "passed": True}]
    again = ExecutionReport.from_payload(report.to_payload())
    assert again.verification_receipts == report.verification_receipts, "restart-complete"


def test_a_streamed_local_gateway_request_reports_the_digest_of_the_posted_body(monkeypatch):
    """The local mind route streams through the omega-runtime gateway. The service itself is not
    started here; the transport seam is faked, exactly like the cloud tests."""
    import runtime.brain_cloud as cloud
    from runtime.client import RuntimeClient
    from schema.continuity_json import content_hash

    class _Local:
        def __init__(self):
            self.sent = []

        def post(self, url, json=None, stream=False, timeout=None):
            self.sent.append(json)
            return _StreamReply(["event: token", 'data: {"token": "hi"}',
                                 "event: done", 'data: {"finish_reason": "stop"}'])

    session = _Local()
    client = RuntimeClient()
    monkeypatch.setattr(cloud, "enabled", lambda: False)
    monkeypatch.setattr(client, "_gpu_clear_to_generate", lambda *_a, **_k: True)
    monkeypatch.setattr(client, "_ensure_session", lambda: session)
    body = client.generate_completion_stream("reflect", system="you are Omega")
    assert body["text"] == "hi"
    assert body.get("request_digest") == content_hash(session.sent[0])


# ── the already-wired dispatcher: labels and refusals it lost ────────────────


def test_a_plugin_permission_request_is_recorded_as_no_effect_not_unknown(monkeypatch, tmp_path):
    """The plan asks for permission BEFORE any provider call, so nothing ran. It was recorded as
    ``tool_reported_failure`` / ``unknown_effect``, which tells a reader deciding whether to
    retry that the world may have moved."""
    from dataclasses import replace

    from auto_loop.execute import brain_skill_executor as bse
    from memory.episode_events import read_episode_events

    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    original = bse._TOOLS["call_plugin_tool"]
    monkeypatch.setitem(bse._TOOLS, "call_plugin_tool", replace(original, handler=lambda args, ctx: bse.ToolResult(
        False, error="plugin_permission_required", meta={"plugin_status": "permission_required"})))
    ctx = replace(bse.operator_tool_context("send a note", permission_mode="always_allow"),
                  episode_id="ep-plugin-permission")
    with owned("ep-plugin-permission"):
        bse.run_operator_tool("call_plugin_tool", {"request": "send a note"}, ctx)
        [row] = _results_by_tool(read_episode_events("ep-plugin-permission"))["call_plugin_tool"]
    assert (row["error_kind"], row["effect"]) == ("permission_required", "no_effect")


def test_a_refusal_made_by_the_exchange_itself_is_still_a_recorded_attempt(tmp_path, monkeypatch):
    """``plan_required`` (and the plugin call budget) refuse the model's call before
    ``run_operator_tool`` is ever reached; the attempt still happened and must not vanish."""
    from memory.episode_events import read_episode_events

    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    with owned("ep-exchange-refusal"):
        _agent_batch([_call("c0", "start_job", project_root="."),
                      _call("c1", "write_file", path="a.txt", content="x")],   # no task_plan yet
                     episode="ep-exchange-refusal", cycle="cycle-refusal")
        results = _results_by_tool(read_episode_events("ep-exchange-refusal"))
    [row] = results["write_file"]
    assert (row["error_kind"], row["effect"]) == ("plan_required", "no_effect")


def test_a_tool_attempt_carries_the_hosts_thread_job_arrival_and_arguments_digest(tmp_path, monkeypatch):
    """The attempt bound only its own token, so its thread fell back to the episode id and the
    job and arrival were lost; the arguments digest the contract provides was never filled."""
    from dataclasses import replace

    from auto_loop.execute import brain_skill_executor as bse
    from memory.auto_evidence import TOOL_ATTEMPT
    from memory.episode_events import read_episode_events
    from schema.continuity_json import content_hash

    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "ws"))
    ctx = replace(bse.operator_tool_context("look", permission_mode="always_allow"),
                  episode_id="ep-host-ids", thread_id="thread-7", job_id="job-7", arrival_seq=7)
    with owned("ep-host-ids"):
        bse.run_operator_tool("list_files", {"path": "."}, ctx)
        [attempt] = [e for e in read_episode_events("ep-host-ids") if e.event_kind == TOOL_ATTEMPT]
    assert (attempt.thread_id, attempt.job_id, attempt.arrival_seq) == ("thread-7", "job-7", 7)
    assert attempt.payload["arguments_digest"] == content_hash({"path": "."})
