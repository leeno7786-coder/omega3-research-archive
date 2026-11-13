"""Batch 2 (1 P1 straggler + 26 P2) — failure-log emits per FAILURE_LOG_IMPL_WORKORDER_BATCH2.md."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from auto_loop.auto import MemoryEnvelope, PromptPack, build_prompt_pack, execute
from deepsleep_loop._deepsleep_state import DeepSleepInput
from deepsleep_loop.consolidate.run import build_journal_entry
from global_loop.assemble.run import global_b_input_from_episode
from guardian.telemetry import writer
from memory.api import QuerySpec, read_memory
from memory.gates.lobe_gates import KnowledgeGate
from meta_loop.consistency.run import MetaBInput, run as run_consistency
from pressure_loop.open_questions import record_open_question
from pressure_loop.operator_pressure import read_operator_directive
from pressure_loop.pressure import _neuromod_from_last_body
from runtime.socket import RuntimeSocket, _invoke_socket
from tests.core.deepsleep_window_fixtures import producer_outcome_axes
from tests.memory.native_fixtures import isolated_memory  # noqa: F401 - pytest fixture


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


# Row 1 — assemble meta_b_input exception propagates with its cause. A logged
# "degraded" row followed by None would read as the legitimate "no AUTO" None.
def test_assemble_meta_b_input_failure_propagates_with_cause(monkeypatch):
    def _boom(_episode):
        raise RuntimeError("meta b induced")

    monkeypatch.setattr("meta_loop.meta_b.meta_b_input_from_episode", _boom)
    episode = SimpleNamespace(
        episode_id="ep-assemble",
        auto={"status": "ok"},
        global_={"action_goal": {"intent": "LEARN"}},
        meta={"execution_action_pack": {"action": "USE_TOOL"}},
        pressure={"opened_by": "operator_command", "contributors": []},
        opened_by="operator_command",
    )
    with pytest.raises(RuntimeError, match="meta b induced"):
        global_b_input_from_episode(episode, {})
    assert not _has_failure(reason="meta_b_input_failed", status="degraded")


# Row 2 — malformed operator directive timestamp
def test_operator_directive_malformed_timestamp_is_logged():
    class FakeState:
        def read(self, _key):
            return {
                "text": "learn Python",
                "issued_at": "not-a-valid-timestamp",
                "ttl_seconds": 3600,
            }

    read_operator_directive(FakeState(), now_iso="2026-06-08T12:00:00+00:00")
    assert _has_failure(reason="malformed_timestamp", status="degraded")


# Row 3 — open question append failure
def test_open_question_append_failed_is_logged(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _fake_span(*_a, **_kw):
        span = MagicMock()
        span.set.return_value = span
        yield span

    monkeypatch.setattr("pressure_loop.open_questions.phase_span", _fake_span)
    monkeypatch.setattr("pressure_loop.open_questions._append", lambda _rec: False)
    record_open_question(
        curiosity_id="cur-1",
        act="ask",
        question_text="Why does this fail?",
        asked_cycle="cycle-1",
    )
    assert _has_failure(reason="append_failed", status="fail")


# Row 4 — neuromod/spike extraction failure
def test_neuromod_extraction_failed_is_logged(monkeypatch):
    class FakeStore:
        def last_closed_body(self):
            return {"neuromod": {"dopamine": 0.5, "serotonin": 0.1, "stress": 0.0}}

    def _boom(_nm):
        raise RuntimeError("spike induced")

    monkeypatch.setattr("autonomic.symphony_neuromod.compute_closed_body_spike", _boom)
    _neuromod_from_last_body(FakeStore())
    assert _has_failure(reason="neuromod_extraction_failed", status="degraded")


# Row 6 — meta consistency critical mismatch
def test_meta_consistency_critical_mismatch_is_logged():
    mbi = MetaBInput(
        episode_id="ep-a",
        pressure={},
        source_action_goal={"episode_id": "ep-a"},
        execution_pack={"episode_id": "ep-a", "action": "USE_TOOL", "tool": "web_fetch"},
        auto_outcome={
            "episode_id": "ep-b",
            "source_action_goal": {},
            "execution_pack": {"action": "RESPOND"},
            "auto": {},
        },
    )
    _, critical = run_consistency(mbi)
    assert critical
    assert _has_failure(phase="consistency", reason="critical_mismatch", status="fail")


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
        if kw.get("operation") == "auto_synth":
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
        "source_action_goal": {"intent": "LEARN"},
    }
    base.update(kw)
    return base


# Row 7 — image persist failure
def test_image_persist_failed_is_logged(monkeypatch):
    rt = _FakeRuntime()
    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_tool",
        lambda *a, **kw: b"\x89PNG\r\n",
    )

    def _boom(_img, _prompt):
        raise OSError("persist induced")

    monkeypatch.setattr("runtime.image_store.persist_generated_image", _boom)
    pack = {
        "action": "IMAGE_CREATION",
        "tool": "image_tool",
        "execution_inputs": {},
        "source_action_goal": {},
    }
    pp = PromptPack(
        prompt_type="IMAGE_CREATION",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="draw a cat",
        context_blocks={},
        response_mode="image",
        tool_call_spec=None,
        image_spec={"prompt": "cat"},
    )
    execute(pp, "IMAGE_CREATION", pack, _mem(), "ep_img", runtime_client=rt)
    assert _has_failure(phase="image_persist_failed", status="degraded")


# Row 8 — knowledge extract failure
def test_knowledge_extract_failed_is_logged(monkeypatch):
    rt = _FakeRuntime(tool_calls=[{"arguments": {"query": "q"}}])
    monkeypatch.setattr(
        "pressure_loop.policy.knowledge_learn_integration_wired",
        lambda: True,
    )

    def _boom(*_a, **_kw):
        raise RuntimeError("extract induced")

    monkeypatch.setattr("knowledge_extractor.extract", _boom)

    def fake_tool(_name, _args):
        return {"ok": True, "result": "tool output text", "elapsed_ms": 1}

    pack = _exec_pack()
    pp = build_prompt_pack(pack, _mem())
    execute(pp, "TOOL_EXECUTOR", pack, _mem(), "ep_kx", runtime_client=rt, tool_invoker=fake_tool)
    assert _has_failure(phase="knowledge_extract_failed", status="degraded")


# Row 10 — web_fetch category resolution failure
def test_web_fetch_category_resolution_failed_is_logged(monkeypatch):
    import schema.tools.web_fetch as wf

    def _boom(*_a, **_kw):
        raise RuntimeError("category induced")

    monkeypatch.setattr("memory.dewey_map.category_for_domain", _boom)
    wf._resolve_catalog_domain("psychology")
    assert _has_failure(phase="web_fetch_category_resolution_failed", status="degraded")


# Row 11 — web sources config load failure
def test_web_sources_config_load_failed_is_logged(monkeypatch):
    """An unreadable source catalog is logged AND fails the load (AGENTS.md section 2): it used
    to cache an empty catalog for the life of the process and "degrade to the legacy wiki+DDG
    path" — a fallback, and one that also leaked into every later test in the same process
    because the caches were reset by assignment instead of through monkeypatch."""
    import schema.tools.web_fetch as wf

    monkeypatch.setattr(wf, "_sources_cache", None)
    monkeypatch.setattr(wf, "_tier_order_cache", None)
    monkeypatch.setattr(wf, "_domain_defaults_cache", None)

    def _boom():
        raise OSError("config induced")

    monkeypatch.setattr("config.loader.load_web_sources_config", _boom)
    with pytest.raises(wf.WebSourcesUnavailable, match="config induced") as caught:
        wf._load_sources_catalog()
    assert isinstance(caught.value.__cause__, OSError)
    assert _has_failure(phase="web_sources_config_load_failed", status="failed")
    assert wf._sources_cache is None, "a failed load must not be cached as an empty catalog"
    with pytest.raises(wf.WebSourcesUnavailable):
        wf.get_credible_sources("python")


# Row 12 — deepsleep knowledge artifact failure
def test_deepsleep_knowledge_artifact_failed_is_logged(monkeypatch):
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_knowledge_consume_on_consolidate",
        lambda: True,
    )

    def _boom(_outcome):
        raise RuntimeError("artifact induced")

    monkeypatch.setattr("knowledge_extractor.consume.knowledge_artifact_for_episode", _boom)
    ds_input = DeepSleepInput(
        window_id="win-1",
        window_reflection={"reflection": {}},
        episode_window=[{"intent": "LEARN", "action": "USE_TOOL",
                         "outcome_axes": producer_outcome_axes(success=None)}],
        trajectory_snapshot={},
    )
    build_journal_entry(ds_input)
    assert _has_failure(reason="knowledge_artifact_failed", status="degraded")


# Row 14 — enrichment failure
def test_enrichment_failed_is_logged(monkeypatch):
    gate = KnowledgeGate()

    def _boom(_text):
        raise RuntimeError("enrich induced")

    monkeypatch.setattr(gate, "DECOMPOSERS", {2: _boom})
    with pytest.raises(RuntimeError, match="enrich induced"):
        gate._enrich("oceanography text", 2)
    assert _has_failure(phase="enrichment_failed")


# Row 15 — gate projection recovery failure
def test_gate_projection_recovery_failure_is_logged(isolated_memory, monkeypatch):  # noqa: F811
    gate = KnowledgeGate()
    monkeypatch.setattr(
        "memory.projection_delta.open_projection_obligation",
        lambda *_args, **_kwargs: {
            "journal_id": None,
            "durable": False,
            "channel": "none",
        },
    )

    assert gate._write("test text", [2, 5, 0], None, None) is None
    assert _has_failure(reason="unprotected_hook_window", status="refused")


# Row 16 — strength register failure
def test_strength_register_failed_is_logged(isolated_memory, monkeypatch):  # noqa: F811
    # A real write in the hermetic tree. The old fake atomic_write returned the relative path
    # "memory/200_knowledge/_t/2.05.test.json"; the final projection step could not read it,
    # resolved its profile against the working directory, and marked the operator's live
    # projection dirty (found 2026-09-15).
    gate = KnowledgeGate()
    si = MagicMock()
    si.register.side_effect = RuntimeError("register induced")
    monkeypatch.setattr(gate, "_get_strength_index", lambda: si)
    path = gate._write("strength register test", [2, 5, 0], None, None)
    assert path
    assert _has_failure(phase="strength_register_failed")


# Row 17 — ingest intermediate write failure emits (not only final None)
def test_ingest_intermediate_failure_is_logged(monkeypatch):
    gate = KnowledgeGate()
    monkeypatch.setattr(gate, "_enrich", lambda _t, _l: {"title": "t"})
    monkeypatch.setattr(
        "memory.gates.base.atomic_write",
        lambda mem, update_index=True: None,
    )
    gate.ingest("ingest intermediate failure test", metadata={"domain": "test"})
    assert _has_failure(phase="ingest_error", reason="write_failed")


# Row 18 — walker recall failure
def test_walker_recall_failed_is_logged(monkeypatch):
    gate = KnowledgeGate()

    def _boom(*_a, **_kw):
        raise RuntimeError("walker induced")

    monkeypatch.setattr("memory.engine.walker.walker", _boom)
    monkeypatch.setattr("memory.engine.walker.load_coord_index", lambda: None)
    gate.recall("oceanography query")
    assert _has_failure(phase="walker_recall_failed")


# Row 19 — index load failure in recall_recent
def test_index_load_failed_is_logged(monkeypatch):
    gate = KnowledgeGate()

    def _boom():
        raise RuntimeError("index induced")

    monkeypatch.setattr("memory.engine.walker.load_coord_index", _boom)
    gate.recall_recent(limit=3)
    assert _has_failure(phase="index_load_failed")


# Row 20 — recall plan compilation failure
def test_recall_plan_compilation_failed_is_logged(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("compile induced")

    monkeypatch.setattr("memory.recall_compiler.compile_recall_plan", _boom)
    monkeypatch.setattr("memory.api.walker_query", lambda _q: ([], {}))
    read_memory(QuerySpec(cycle_id="cycle-rp", text="hello recall"))
    assert _has_failure(phase="recall_plan_compilation_failed")


# Row 21 — lane arbitration degraded
def test_lane_arbitration_degraded_is_logged(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("lane arb induced")

    monkeypatch.setattr("memory.recall_lanes.run_recall_lanes", _boom)
    monkeypatch.setattr("memory.api.walker_query", lambda _q: ([], {}))
    read_memory(QuerySpec(cycle_id="cycle-la", text="lane arbitration"))
    assert _has_failure(phase="lane_arbitration_degraded", status="degraded")


# Row 22 — walker arbitration shim failure
def test_walker_arbitration_failed_is_logged(monkeypatch):
    real_enumerate = enumerate

    def _enum(seq, start=0):
        if start == 1:
            raise RuntimeError("arb shim induced")
        return real_enumerate(seq, start=start)

    monkeypatch.setattr("builtins.enumerate", _enum)
    monkeypatch.setattr("memory.api.walker_query", lambda _q: ([], {}))
    read_memory(QuerySpec(cycle_id="cycle-wa", text="walker arbitration"))
    assert _has_failure(phase="walker_arbitration_failed", status="degraded")


# Row 23 — strength index save failure
def test_strength_index_save_failed_is_logged(monkeypatch, tmp_path):
    from memory.strength_index import StrengthIndex

    si = StrengthIndex(persist_path=tmp_path / "strength.json")
    si._records = {"id1": MagicMock()}
    si._dirty = 999

    real_open = open

    def _selective_open(file, *args, **kwargs):
        if "strength" in str(file):
            raise OSError("save induced")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _selective_open)
    si.save(force=True)
    assert _has_failure(phase="strength_index_save_failed")


# Row 24 — presence broker failure
def test_presence_broker_failed_is_logged(monkeypatch):
    from sensory.operator_presence import run_watcher

    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 1

    def _boom(*_a, **_kw):
        raise RuntimeError("broker induced")

    monkeypatch.setattr("runtime.face.recognition.face_status", _boom)
    from sensory.operator_presence import PresenceMachine

    cfg = {
        "assume_operator_present": False,
        "acquire_threshold": 0.5,
        "release_threshold": 0.3,
        "acquire_frames": 2,
        "release_seconds": 5.0,
        "degraded_holds_last_state": True,
    }
    run_watcher(
        stop,
        frame_source=lambda: None,
        sleep_fn=lambda _s: None,
        emit=False,
        machine=PresenceMachine(cfg=cfg),
    )
    assert _has_failure(phase="presence_broker_failed", status="degraded")


# Row 25 — hud journal check false positive on exception
def test_hud_journal_check_failed_is_logged(monkeypatch):
    import hud.telemetry as tel

    monkeypatch.setattr(
        tel,
        "_probe_retina",
        lambda: {"state": "live", "label": "ettin", "detail": "resident · 1 codes"},
    )
    broker_snapshot = {}
    monkeypatch.setattr(
        "memory.strength_index.ensure_strength_index", lambda: SimpleNamespace(_records={"a": 1})
    )

    def _boom(*_a, **_kw):
        raise RuntimeError("journal induced")

    # _probe_memory reads journal_status(), not find_unclosed_entries() — the
    # probe was widened to report malformed lines and errors, not just unclosed
    # entries. Patching the old collaborator raised nothing, so the probe
    # succeeded and this test asserted a failure that was never provoked.
    monkeypatch.setattr("memory.encoding_journal.journal_status", _boom)
    tel._probe_memory(broker_snapshot)
    assert _has_failure(phase="journal_check")


# Row 26 — shared state read failure (runtime present, read path broke)
def test_hud_shared_state_read_failed_is_logged(monkeypatch):
    import hud.telemetry as tel

    class _BrokenRuntime:
        @property
        def shared_state(self):
            raise RuntimeError("ss read induced")

    monkeypatch.setattr(
        "pressure_loop.runtime.try_get_default_runtime",
        lambda: _BrokenRuntime(),
    )
    assert tel._get_shared_state() is None
    assert _has_failure(phase="shared_state_read_failed")


def test_hud_missing_runtime_does_not_emit_shared_state_read_failed(monkeypatch):
    import hud.telemetry as tel

    tel._runtime_absent_logged = False
    monkeypatch.setattr("pressure_loop.runtime.try_get_default_runtime", lambda: None)
    before = sum(1 for rec in _failure_records() if rec.get("phase") == "shared_state_read_failed")
    for _ in range(5):
        assert tel._get_shared_state() is None
    after = sum(1 for rec in _failure_records() if rec.get("phase") == "shared_state_read_failed")
    assert after == before


# Row 27 — shared state key read failure
def test_hud_shared_state_key_read_failed_is_logged(monkeypatch):
    import hud.telemetry as tel

    class BadSS:
        def read(self, _key):
            raise RuntimeError("key read induced")

    monkeypatch.setattr(tel, "_get_shared_state", lambda: BadSS())
    assert tel._shared_state_read("SOME_KEY") is None
    assert _has_failure(phase="shared_state_key_read_failed")
