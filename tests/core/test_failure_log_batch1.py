"""Batch 1 (P0+P1) — failure-log emits per FAILURE_LOG_IMPL_WORKORDER.md."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from auto_loop.auto import MemoryEnvelope, PromptPack, build_prompt_pack, execute
from global_loop.loop import run_global_a
from guardian.telemetry import writer
from memory.api import CanonicalEnvelope, intake_memory
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.episode import EpisodeStore
from pressure_loop.pressure import gather_contributors
from pressure_loop.temporal_store import read_trajectory
from runtime.socket import RuntimeSocket, _invoke_socket


def _failure_records():
    p = writer.FAILURE_LOG
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _has_failure(
    *, reason: str | None = None, phase: str | None = None, status: str | None = None
) -> bool:
    for rec in _failure_records():
        if reason is not None and rec.get("reason") != reason:
            continue
        if phase is not None and rec.get("phase") != phase:
            continue
        if status is not None and rec.get("status") != status:
            continue
        return True
    return False


def _setup_episode(store: EpisodeStore, *, episode_id: str | None = None) -> object:
    ep = store.current()
    if ep is None:
        raise RuntimeError("no active episode after open()")
    if episode_id:
        ep.episode_id = episode_id
    ep.global_ = {
        "action_goal": {"intent": "LEARN"},
        "outcome": {"status": "success"},
    }
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}
    ep.omega = {}
    return ep


# Row 1 — accumulator reset failure
def test_accumulator_reset_failure_is_logged(monkeypatch):
    import pressure_loop.pressure_field as pf

    def _boom():
        raise RuntimeError("induced reset failure")

    monkeypatch.setattr(pf, "reset_pressure_accumulator", _boom)
    store = EpisodeStore()
    store.open(pipeline_id=0, work_kind="operator_turn", opened_by="operator_command")
    _setup_episode(store)
    store.close()
    assert _has_failure(reason="accumulator_reset_failed", status="degraded"), (
        "accumulator reset failure must reach failures.jsonl"
    )


# Row 2 — ignition validation crash-cause logged via phase_span
def test_ignition_validation_failure_is_logged_before_crash():
    bad_ignition = EpisodeIgnition(
        episode_id="ep-bad",
        aggregate_pressure=1.0,
        opened_by="",
        dominant_contributor=None,
        contributors=(),
        trajectory_snapshot={},
    )
    episode = SimpleNamespace(episode_id="ep-bad")
    with pytest.raises(ValueError, match="opened_by"):
        run_global_a(episode, bad_ignition)
    assert _has_failure(phase="ignition", status="fail"), (
        "ignition validation failure must reach failures.jsonl before crash"
    )


# Row 3 — archive failure
def test_archive_failure_is_logged(tmp_path, monkeypatch):
    journal_path = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )
    import pressure_loop.episode_journal as epj

    def _boom(_ep, path=None):
        raise RuntimeError("archive induced")

    monkeypatch.setattr(epj, "archive_episode", _boom)
    store = EpisodeStore()
    store.open(pipeline_id=0, work_kind="operator_turn", opened_by="operator_command")
    _setup_episode(store)
    closed = store.close()
    assert closed is not None
    assert store.current() is None
    assert _has_failure(phase="episode_journal_archive", status="degraded")


# Row 4 — reflection window append failure
def test_reflection_window_append_failure_is_logged(monkeypatch):
    """Phase 4 cut 13 moved the append into the DeepSleep commit region, which
    ``close`` reaches through ``before_finalize``. The failure posture is the
    one this row exists to pin and is unchanged: it propagates out of close."""
    store = EpisodeStore()
    store.open(pipeline_id=0, work_kind="operator_turn", opened_by="operator_command")
    _setup_episode(store)

    def _boom(*_a, **_kw):
        raise RuntimeError("window append induced")

    monkeypatch.setattr(store, "_append_episode_to_reflection_window", _boom)
    with pytest.raises(RuntimeError, match="window append induced"):
        store.close(
            before_finalize=lambda ep, body: store.commit_reflection_window_append(
                ep, body
            )
        )


# Row 5 — malformed operator message ingress
def test_unreadable_operator_line_is_logged_and_raises(tmp_path, monkeypatch):
    """An unreadable operator line is loud, never a silent empty line.

    This row used to cover a malformed SharedState ingress record. That mirror
    is retired (AGENTS.md §4.0) — gather no longer rebuilds the operator's turn
    from SharedState, so there is no such record to malform. The equivalent
    failure now lives in the durable queue, and it is strictly worse if
    swallowed: a corrupt file read as "nobody is waiting" would be persisted
    back over the file by the next enqueue, losing the whole backlog (audit
    §1.9). So it emits AND raises.
    """
    import pressure_loop.cognition_queue as queue_mod

    path = tmp_path / "operator_input_queue.json"
    path.write_text('[{"source": "operator", "type": "operator_qu', encoding="utf-8")
    monkeypatch.setattr(queue_mod, "_queue_path", lambda: path)
    queue_mod._reset_cache_for_tests()
    try:
        with pytest.raises(queue_mod.CognitionQueueUnreadable):
            queue_mod.peek_inputs()
        assert _has_failure(reason="cognition_queue_load_failed", status="degraded")
    finally:
        queue_mod._reset_cache_for_tests()


# Row 6 — corrupt trajectory JSON crash-cause
def test_read_trajectory_corrupt_json_is_logged_before_crash():
    from memory.episode_sram import close_bucket, open_bucket

    env = CanonicalEnvelope(
        cycle_id="trajectory-corrupt-json",
        coord=[9, 2, 0],
        type="trajectory",
        payload={
            "title": "current_trajectory",
            "summary": "corrupt",
            "text": "not-valid-json{",
            "enrichment": {"closed_at": "2026-01-01T00:00:00+00:00"},
        },
        source="lobe:temporal",
    )
    open_bucket(env.cycle_id, pipeline_id=0)
    intake_memory(env, propose=False, bypass_guardian=True, skip_embed=True)
    close_bucket(env.cycle_id)
    with pytest.raises(RuntimeError, match="corrupt trajectory"):
        read_trajectory()
    assert _has_failure(reason="corrupt_record", status="fail")


class _FakeRuntime:
    def __init__(self, *, proceed=True, text="answer", tool_calls=None):
        self.proceed = proceed
        self._text = text
        self._tool_calls = tool_calls or []

    def health_body(self):
        return {
            "status": "ok",
            "capabilities": {"coding_model": self.proceed, "image_generation": self.proceed},
        }

    def generate_completion(self, **kw):
        if not self.proceed:
            return None
        return {"text": self._text, "tool_calls": self._tool_calls, "tokens_in": 1, "tokens_out": 2}


def _mem():
    return MemoryEnvelope(
        memory_required=False,
        memory_status="skipped",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )


def _exec_pack(**kw):
    base = {
        "action": "USE_TOOL",
        "tool": "web_fetch",
        "tool_reason": "learn",
        "execution_inputs": {"query": "Python"},
    }
    base.update(kw)
    return base


# Row 7 — tool ok=False behavior fix
def test_tool_ok_false_marks_failed_and_logs():
    rt = _FakeRuntime(tool_calls=[{"arguments": {"query": "q"}}])

    def fake_tool(_name, _args):
        return {"ok": False, "error": "tool declined", "elapsed_ms": 1}

    pack = _exec_pack()
    pp = build_prompt_pack(pack, _mem())
    res = execute(
        pp, "TOOL_EXECUTOR", pack, _mem(), "ep_tool_fail", runtime_client=rt, tool_invoker=fake_tool
    )
    assert res["outputs"].get("tool_status") == "failed"
    assert _has_failure(phase="tool_invocation", status="fail")


# Row 8 — tool invoke exception
def test_tool_invoke_exception_is_logged():
    rt = _FakeRuntime(tool_calls=[{"arguments": {"query": "q"}}])

    def fake_tool(_name, _args):
        raise RuntimeError("invoke boom")

    pack = _exec_pack()
    pp = build_prompt_pack(pack, _mem())
    res = execute(
        pp, "TOOL_EXECUTOR", pack, _mem(), "ep_tool_exc", runtime_client=rt, tool_invoker=fake_tool
    )
    assert res["outputs"].get("tool_status") == "failed"
    assert _has_failure(phase="tool_invocation", status="fail")


# Row 9 — MRIL recall failure (single-hop)
def test_mril_recall_failure_is_logged(monkeypatch):
    from auto_loop.mril_tools import route_memory_tool

    def _boom(*_a, **_kw):
        raise RuntimeError("mril hop1 induced")

    monkeypatch.setattr("auto_loop.mril_tools.run_forced_memory_tools", _boom)
    pack = {
        "action": "RESPONSE",
        "memory_required": True,
        "memory_delivery_mode": "single_hop_system",
        "memory_tools": (route_memory_tool("remember x"),),
        "memory_scope": ("coding",),
        "execution_inputs": {},
        "source_action_goal": {},
    }
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
    res = execute(pp, "TEXT_REASONING", pack, _mem(), "ep_mril1", runtime_client=_FakeRuntime())
    assert res["failure_kind"] == "memory_continuity_substrate_invalid"
    assert _has_failure(phase="mril_recall_failed", status="fail")


# Row 11 — socket preflight declined
def test_socket_preflight_declined_is_logged(monkeypatch):
    sock = RuntimeSocket(
        name="test_sock",
        endpoint="/test",
        capability="coding_model",
        method="generate_completion",
    )
    decision = SimpleNamespace(reason="runtime down", proceed=False)
    monkeypatch.setattr(
        "runtime.socket._preflight_socket",
        lambda *_a, **_kw: (False, decision),
    )
    result = _invoke_socket(sock, operation="test_op", episode_id="ep_sock")
    assert result is None
    assert _has_failure(phase="socket_preflight_declined", status="degraded")


# Row 12 — web_fetch direct URL failure
def test_web_fetch_direct_fetch_failure_is_logged(monkeypatch):
    import schema.tools.web_fetch as wf

    def _boom(*_a, **_kw):
        raise ConnectionError("fetch induced")

    monkeypatch.setattr(wf.requests, "get", _boom)
    out = wf.web_fetch("https://example.com/page", max_results=1)
    assert out["results"] == []
    assert _has_failure(phase="web_fetch_direct_fetch_failed", status="fail")


# REMOVED: identity_charter tests - module deleted per REVISED_DELETION_PLAN.md
# test_panel_identity_charter_load_failure_is_logged
# test_panel_identity_charter_verify_failure_is_logged
# test_panel_identity_ledger_verify_failure_is_logged
# test_probe_identity_charter_load_failure_is_logged
