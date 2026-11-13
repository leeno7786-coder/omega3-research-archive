"""Phase 6J — QwenSkillExecutionResult normalization (fail-closed)."""

from __future__ import annotations

import pytest

from auto_loop.skill.run import dispatch_skill_pack
from schema.execution_skill import ExecutionSkill
from schema.qwen_skill_dispatch import QwenSkillExecutionDispatch
from schema.qwen_skill_result import ALLOWED_STATUS, normalize_qwen_result
from schema.selected_skill import SelectedSkillDecision
from schema.skill_execution_pack import build_skill_execution_pack

_LIMITS = {"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": 10}


def _dispatch(*, dispatch_id="dispatch_x", episode_id="ep_1", forbidden=("run_command",)):
    return QwenSkillExecutionDispatch(
        episode_id=episode_id,
        dispatch_id=dispatch_id,
        skill_name="codex-grade-coding",
        skill_id="5.01.qwenskill-codex-grade-coding",
        skill_class="repair_skill",
        execution_kind="REPAIR",
        objective="fix the bug",
        skill_markdown="# body",
        allowed_tools=("read_files", "patch_file"),
        forbidden_tools=tuple(forbidden),
        safety={"safety_class": "write_safe"},
        limits=dict(_LIMITS),
        evidence_required=("diff_summary",),
        completion_criteria=("done",),
        expected_output_schema={},
        source_action_goal={},
        source_skill_execution_pack={},
    )


# ── valid + status derivation ─────────────────────────────────────────────────
def test_valid_bridge_result_normalizes():
    raw = {
        "ok": True,
        "summary": "patched the parser",
        "ledger": {"touched_files": [{"path": "p.py", "tool": "patch_file"}]},
        "tool_calls": [
            {
                "tool_name": "patch_file",
                "args": {"path": "p.py"},
                "status": "success",
                "duration_ms": 12,
            },
            {
                "tool_name": "execute_command",
                "args": {"command": "pytest test_parser.py::test_ok"},
                "status": "success",
            },
        ],
        "files_changed": ["invented.py"],
        "tests_run": ["invented::test"],
        "diff_summary": {"added": 3, "removed": 1},
        "evidence": {"summary": "patched"},
        "confidence": 0.8,
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert r.status == "success" and r.summary == "patched the parser"
    assert r.type == "qwen_skill_execution_result" and r.episode_id == "ep_1"
    assert len(r.tool_calls) == 2 and r.tool_calls[0]["tool_name"] == "patch_file"
    assert r.files_changed == ("p.py",)
    assert r.tests_run == ("pytest test_parser.py::test_ok",)
    assert r.diff_summary == {
        "source": "bridge_ledger",
        "files_changed": 1,
        "paths": ["p.py"],
    }
    assert r.evidence == {} and r.confidence == 0.8


def test_explicit_status_partial_is_honored():
    r = normalize_qwen_result({"status": "partial", "summary": "half done"}, dispatch=_dispatch())
    assert r.status == "partial"


# ── tests_run / commands_run derived from command tool calls (bridge omits the fields) ──
def test_tests_run_derived_from_pytest_command_when_bridge_omits_it():
    # The bridge emits no tests_run field; a run_command 'pytest ...' IS evidence tests ran, so
    # derive it (operator 2026-07-01: tests_run never propagated -> a real build that ran pytest
    # failed the evidence gate as skill_no_evidence).
    raw = {
        "ok": True,
        "summary": "built + tested",
        "tool_calls": [
            {"tool_name": "write_file", "args": {"path": "todo_app/main.py"}, "status": "success"},
            {
                "tool_name": "execute_command",
                "args": {"command": "python -m pytest todo_app/ -q"},
                "status": "success",
            },
        ],
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert r.tests_run == ("python -m pytest todo_app/ -q",)
    assert "python -m pytest todo_app/ -q" in r.commands_run


def test_non_test_commands_are_commands_run_not_tests_run():
    raw = {
        "ok": True,
        "summary": "scaffolded",
        "tool_calls": [
            {"tool_name": "execute_command", "args": {"command": "ls -la"}, "status": "success"},
            {
                "tool_name": "execute_command",
                "args": {"command": "python main.py"},
                "status": "success",
            },
        ],
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert r.tests_run == ()  # neither invokes a test runner
    assert set(r.commands_run) == {"ls -la", "python main.py"}


def test_explicit_bridge_tests_run_is_ignored_in_favor_of_tool_fact():
    raw = {
        "ok": True,
        "summary": "x",
        "tests_run": ["suite::a"],
        "tool_calls": [
            {"tool_name": "execute_command", "args": {"command": "pytest"}, "status": "success"}
        ],
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert r.tests_run == ("pytest",)


def test_untrusted_top_level_execution_claims_are_never_normalized_as_facts():
    raw = {
        "ok": True,
        "summary": "I changed and verified everything",
        "files_changed": ["invented.py"],
        "commands_run": ["pytest -q"],
        "tests_run": ["pytest -q"],
        "artifacts": ["invented wheel"],
        "evidence": {"tests_passed": True},
    }

    result = normalize_qwen_result(raw, dispatch=_dispatch(forbidden=()))

    assert result.files_changed == ()
    assert result.commands_run == ()
    assert result.tests_run == ()
    assert result.artifacts == ()
    assert result.evidence == {}


def test_bridge_ledger_and_tool_results_remain_trusted_execution_facts():
    raw = {
        "ok": True,
        "ledger": {
            "touched_files": [{"path": "src/real.py", "tool": "write_file", "status": "success"}]
        },
        "tool_calls": [
            {
                "tool_call_id": "tc-pytest",
                "tool_name": "execute_command",
                "args": {"command": "python -m pytest -q"},
                "status": "success",
                "stdout": "1 passed",
                "result": {"exit_code": 0},
            }
        ],
    }

    result = normalize_qwen_result(raw, dispatch=_dispatch(forbidden=()))

    assert result.files_changed == ("src/real.py",)
    assert result.commands_run == ("python -m pytest -q",)
    assert result.tests_run == ("python -m pytest -q",)


def test_timeout_and_bridge_unavailable_status():
    assert (
        normalize_qwen_result(
            {"ok": False, "error": "run timed out after 600s"}, dispatch=_dispatch()
        ).status
        == "timeout"
    )
    assert (
        normalize_qwen_result(
            {"ok": False, "error": "connection refused (bridge down)"}, dispatch=_dispatch()
        ).status
        == "bridge_unavailable"
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"ok": False, "error": "HTTP 429 Too Many Requests"},
        {"status": "failure", "failure_class": "rate_limit"},
        {"status": "failure", "error": "provider throttled the request"},
    ],
)
def test_rate_limit_failures_normalize_to_transient_kind(raw):
    result = normalize_qwen_result(raw, dispatch=_dispatch())

    assert result.status == "failure"
    assert result.failure_class == "rate_limited"


def test_genuine_executor_failure_is_not_normalized_as_rate_limit():
    result = normalize_qwen_result(
        {"status": "failure", "error": "syntax error in generated file"},
        dispatch=_dispatch(),
    )

    assert result.failure_class == "failure"


# ── fail closed ───────────────────────────────────────────────────────────────
def test_malformed_result_fails_closed():
    r = normalize_qwen_result("not a dict", dispatch=_dispatch())
    assert r.status == "malformed_result" and r.failure_class == "malformed_result"


def test_unrecognizable_dict_is_malformed():
    r = normalize_qwen_result({"weird": 1}, dispatch=_dispatch())
    assert r.status == "malformed_result"


def test_missing_dispatch_id_fails_closed():
    r = normalize_qwen_result({"ok": True}, dispatch=_dispatch(dispatch_id=""))
    assert r.status == "invalid_dispatch" and r.failure_class == "invalid_dispatch"


# ── forbidden tool use -> failure ─────────────────────────────────────────────
def test_forbidden_tool_use_becomes_failure():
    raw = {
        "ok": True,
        "summary": "ran a command",
        "tool_calls": [
            {"tool_name": "run_command", "args": {"command": "ls"}, "status": "success"}
        ],
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch(forbidden=("run_command",)))
    assert r.status == "failure"
    assert r.failure_class and "forbidden_tool_use" in r.failure_class


# ── records ───────────────────────────────────────────────────────────────────
def test_files_changed_recorded_from_git_diff():
    raw = {"ok": True, "git_diff": "diff --git a/x.py b/x.py\n+++ b/x.py\n+code\n+++ b/y.py\n+more"}
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert set(r.files_changed) == {"x.py", "y.py"}
    assert r.diff_summary.get("git_diff_present") is True


def test_files_changed_recorded_from_bridge_ledger():
    # the live qwen bridge reports changes via ledger.touched_files (git_diff is always "")
    raw = {
        "ok": True,
        "git_diff": "",
        "ledger": {
            "touched_files": [{"path": "schema/x.py", "verdict": "keep"}, {"path": "schema/y.py"}]
        },
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert set(r.files_changed) == {"schema/x.py", "schema/y.py"}


def test_ledger_touched_files_synthesize_tool_calls_for_forbidden_check():
    raw = {
        "ok": True,
        "ledger": {
            "touched_files": [{"path": "a.py", "tool": "run_command"}],
        },
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch(forbidden=("run_command",)))
    assert r.status == "failure"
    assert r.failure_class and "forbidden_tool_use" in r.failure_class
    assert len(r.tool_calls) == 1 and r.tool_calls[0]["tool_name"] == "run_command"


def test_status_values_are_in_allowed_set():
    for raw in ({"ok": True}, {"ok": False}, {"status": "blocked"}, "x", {"error": "timeout"}):
        assert normalize_qwen_result(raw, dispatch=_dispatch()).status in ALLOWED_STATUS


# ── yielded (fold-back build loop) ────────────────────────────────────────────
def test_yielded_status_is_honored_and_not_a_failure():
    raw = {
        "status": "yielded",
        "summary": "landed the planner",
        "progress_digest": "Built core/types, memory, planner. Next: tool registry.",
        "rounds_used": 12,
        "files_changed": ["invented.ts"],
        "ledger": {"touched_files": [{"path": "src/planner/index.ts"}]},
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert r.status == "yielded"
    # a budget yield is progress, NOT a failure — it must carry no failure_class
    assert r.failure_class is None
    assert r.progress_digest == "Built core/types, memory, planner. Next: tool registry."
    assert r.rounds_used == 12
    assert r.files_changed == ("src/planner/index.ts",)


def test_yielded_is_in_allowed_status_set():
    assert "yielded" in ALLOWED_STATUS


def test_yielded_round_budget_still_records_touched_files_from_ledger():
    raw = {
        "status": "yielded",
        "rounds_used": 12,
        "ledger": {"touched_files": [{"path": "a.ts"}, {"path": "b.ts"}]},
    }
    r = normalize_qwen_result(raw, dispatch=_dispatch())
    assert r.status == "yielded" and set(r.files_changed) == {"a.ts", "b.ts"}
    assert r.failure_class is None


# ── converts to Auto outcome ──────────────────────────────────────────────────
def _mk_skill(name="codex-grade-coding"):
    return ExecutionSkill(
        name=name,
        skill_id=f"5.01.qwenskill-{name}",
        kind="execution_skill",
        version=1,
        enabled=True,
        description=name,
        skill_class="repair_skill",
        intents=("USE_SKILL",),
        execution_kinds=("REPAIR",),
        domains=("python",),
        allowed_tools=("read_files", "patch_file"),
        forbidden_tools=("run_command",),
        safety_class="write_safe",
        requires_confirmation_for=(),
        limits=dict(_LIMITS),
        evidence_required=("diff_summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="# body",
        source_path=f"/skills/{name}/SKILL.md",
    )


def test_result_converts_to_auto_outcome():
    skill = _mk_skill()
    decision = SelectedSkillDecision.from_skill(
        skill, score=1.0, execution_kind="REPAIR", reason="t"
    )
    pack = build_skill_execution_pack(
        episode_id="ep_1", objective="fix the bug", decision=decision, source_action_goal={}
    )
    out = dispatch_skill_pack(
        pack,
        catalog=[skill],
        invoker=lambda directive, d: {
            "ok": True,
            "summary": "done",
            "ledger": {"touched_files": [{"path": "a.py", "tool": "patch_file"}]},
        },
        confirmed=True,
        workspace_root="/ws",
    )
    assert out["status"] == "executed"
    qr = out["qwen_result"]
    assert qr["type"] == "qwen_skill_execution_result"
    assert qr["status"] == "success" and qr["files_changed"] == ["a.py"]
    assert qr["dispatch_id"] == pack.dispatch_id
