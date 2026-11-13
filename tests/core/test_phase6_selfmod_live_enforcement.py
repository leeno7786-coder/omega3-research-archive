"""Critical lane #1 — the LIVE USE_SKILL success branch must enforce the self-mod
denylist on the qwen-reported files_changed, not just the file COUNT.

Finding: ``auto_loop/execute/run.py`` folded a qwen "success" as a credited capability
(``model_id == "qwen_cli"``) as long as it stayed under ``max_files_changed`` — it never
called :func:`auto_loop.execute.self_mod_denylist.self_mod_violations`. So a dispatch that
edits its OWN guard, the denylist, or a mind-state dir (within the count limit) is accepted
and reinforced — qwen disarming the wall that contains it (invariant 2).

These tests drive the REAL ``execute`` USE_SKILL branch (not a pure helper) precisely
because the defect was that the live path didn't call the guard — a helper test would not
prove the seam is wired.
"""

from __future__ import annotations

import auto_loop.skill.run as skill_run
from auto_loop.auto import MemoryEnvelope, PromptPack, execute
from core.paths import OMEGA_ROOT
from pathlib import Path

# Absolute Omega-tree paths so the assertion is env-INDEPENDENT: a protected write is blocked
# regardless of whether OMEGA_OPERATOR_WORKSPACE is set. A RELATIVE path now resolves to the
# operator workspace (qwen's jail/cwd), which is legitimate — so these tests must name the real
# Omega-tree target the guard protects, exactly as a production ledger reports it (absolute).
_PROTECTED_GUARD = (Path(OMEGA_ROOT) / "auto_loop/execute/skill_execution_guards.py").as_posix()
_PROTECTED_MIND_STATE = (Path(OMEGA_ROOT) / "memory/000_identity/whoami.json").as_posix()


def _mem() -> MemoryEnvelope:
    return MemoryEnvelope(
        memory_required=False,
        memory_status="skipped",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )


def _pp() -> PromptPack:
    return PromptPack(
        prompt_type="RESPONSE_REASONING",
        system_prompt="sys",
        developer_prompt=None,
        user_prompt="do the work",
        context_blocks={
            "remembered_knowledge_tool_results": None,
            "tool_block": None,
            "constraints_block": None,
        },
        response_mode="reasoning",
        tool_call_spec=None,
        image_spec=None,
    )


def _use_skill_pack(*, max_files_changed: int):
    return {
        "action": "USE_SKILL",
        "tool": None,
        "source_action_goal": {},
        "execution_inputs": {
            "skill_execution_pack": {
                "dispatch_id": "d1",
                "episode_id": "ep_selfmod",
                "objective": "do the work",
                "selected_skill": {"name": "coding_skill"},
                "limits": {"max_files_changed": max_files_changed},
            }
        },
    }


def _fake_dispatch(files_changed):
    """Stand-in for dispatch_skill_pack: a clean 'executed' + qwen 'success' whose
    reported files_changed is whatever the test injects (within the count limit)."""

    def _dispatch(*_args, **_kwargs):
        return {
            "status": "executed",
            "dispatch_id": "d1",
            "reason": None,
            "guard_status": {},
            "qwen_result": {
                "status": "success",
                "files_changed": list(files_changed),
                "summary": "did the work",
            },
        }

    return _dispatch


def test_use_skill_success_writing_protected_code_is_blocked(monkeypatch):
    # qwen reports success having edited its OWN execution guard, well within the limit.
    monkeypatch.setattr(
        skill_run,
        "dispatch_skill_pack",
        _fake_dispatch([_PROTECTED_GUARD]),
    )
    res = execute(
        _pp(),
        None,
        _use_skill_pack(max_files_changed=5),
        _mem(),
        "ep_selfmod",
        runtime_client=object(),
    )

    assert res["outputs"]["skill_status"] == "failure"
    assert res["failure_kind"] == "skill_self_modification_blocked"
    # the violating paths are surfaced for the foldback / operator
    assert any(
        "skill_execution_guards.py" in str(v)
        for v in res["outputs"].get("skill_self_mod_violations", ())
    )
    # NEVER credited / reinforced as a successful qwen capability
    assert res["model_id"] != "qwen_cli"


def test_use_skill_success_writing_mind_state_dir_is_blocked(monkeypatch):
    monkeypatch.setattr(
        skill_run,
        "dispatch_skill_pack",
        _fake_dispatch([_PROTECTED_MIND_STATE]),
    )
    res = execute(
        _pp(),
        None,
        _use_skill_pack(max_files_changed=10),
        _mem(),
        "ep_selfmod2",
        runtime_client=object(),
    )

    assert res["outputs"]["skill_status"] == "failure"
    assert res["failure_kind"] == "skill_self_modification_blocked"
    assert res["model_id"] != "qwen_cli"


def test_use_skill_clean_operator_write_is_still_credited(monkeypatch):
    # A normal operator-workspace write (no protected/mind-state path) must remain a
    # credited success — the guard must not block legitimate skill work.
    monkeypatch.setattr(
        skill_run,
        "dispatch_skill_pack",
        _fake_dispatch(["project/src/main.py"]),
    )
    res = execute(
        _pp(),
        None,
        _use_skill_pack(max_files_changed=5),
        _mem(),
        "ep_clean",
        runtime_client=object(),
    )

    assert res["outputs"]["skill_status"] == "success"
    assert res["model_id"] == "brain_runtime"
    assert res["failure_kind"] is None
