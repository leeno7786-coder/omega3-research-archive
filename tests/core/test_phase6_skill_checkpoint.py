"""High #3 — the guard computes checkpoint_required but nothing consumed it: a write/
destructive dispatch ran with NO mind-state checkpoint, so a failed (or self-mod) run left
Omega's mind mutated with no rollback. dispatch_skill_pack must now take a mind-state
checkpoint BEFORE a checkpoint-required dispatch (fail CLOSED if it can't), restore on any
non-success / invoker_error outcome, and surface the checkpoint id for the caller.
"""

from __future__ import annotations

import pytest

from auto_loop.skill.run import dispatch_skill_pack
from schema.execution_skill import ExecutionSkill
from schema.selected_skill import SelectedSkillDecision
from schema.skill_execution_pack import build_skill_execution_pack

_LIMITS = {"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": 10}


def _mk_skill(
    name="wr-skill",
    *,
    safety_class="write_safe",
    execution_kinds=("REPAIR",),
    allowed=("read_files", "patch_file"),
    forbidden=("run_command",),
):
    return ExecutionSkill(
        name=name,
        skill_id=f"5.01.qwenskill-{name}",
        kind="execution_skill",
        version=1,
        enabled=True,
        description=name,
        skill_class="repair_skill",
        intents=("USE_SKILL",),
        execution_kinds=tuple(execution_kinds),
        domains=("python",),
        allowed_tools=tuple(allowed),
        forbidden_tools=tuple(forbidden),
        safety_class=safety_class,
        requires_confirmation_for=(),
        limits=dict(_LIMITS),
        evidence_required=("summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="# Skill\nbody",
        source_path=f"/skills/{name}/SKILL.md",
    )


def _pack_for(skill, objective="fix the bug"):
    decision = SelectedSkillDecision.from_skill(
        skill, score=1.0, execution_kind=skill.execution_kinds[0], reason="t"
    )
    return build_skill_execution_pack(
        episode_id="ep_1",
        objective=objective,
        decision=decision,
        source_action_goal={"opened_by": "operator_command"},
    )


class _Recorder:
    def __init__(self):
        self.checkpoints = 0
        self.restores = []
        self.fail_checkpoint = False

    def checkpoint(self, **kw):
        self.checkpoints += 1
        if self.fail_checkpoint:
            raise RuntimeError("disk full")
        return type("Cp", (), {"checkpoint_id": "cp-test"})()

    def restore(self, cid, **kw):
        self.restores.append(cid)
        return type("Rep", (), {"ok": True})()


def _run(skill, invoker, rec, tmp_path):
    return dispatch_skill_pack(
        _pack_for(skill),
        catalog=[skill],
        invoker=invoker,
        confirmed=True,
        workspace_root=str(tmp_path),
        checkpoint_fn=rec.checkpoint,
        restore_fn=rec.restore,
    )


def test_failed_write_dispatch_restores_mind_state(tmp_path):
    rec = _Recorder()
    out = _run(_mk_skill(), lambda directive, d: {"ok": False, "error": "boom"}, rec, tmp_path)
    assert out["status"] == "executed"
    assert out["qwen_result"]["status"] == "failure"
    assert rec.checkpoints == 1  # checkpoint taken before qwen
    assert rec.restores == ["cp-test"]  # rolled back on the failure


def test_successful_write_dispatch_keeps_mind_state(tmp_path):
    rec = _Recorder()
    out = _run(
        _mk_skill(),
        lambda directive, d: {"ok": True, "summary": "done", "files_changed": ["project/x.py"]},
        rec,
        tmp_path,
    )
    assert out["qwen_result"]["status"] == "success"
    assert rec.checkpoints == 1
    assert rec.restores == []  # success keeps the work
    assert out.get("mind_checkpoint_id") == "cp-test"  # surfaced for the caller


def test_checkpoint_failure_blocks_dispatch_fail_closed(tmp_path):
    rec = _Recorder()
    rec.fail_checkpoint = True
    called = []
    out = _run(_mk_skill(), lambda directive, d: called.append(1) or {"ok": True}, rec, tmp_path)
    assert out["status"] == "blocked"
    assert "checkpoint" in out["reason"].lower()
    assert called == []  # qwen NEVER contacted without reversibility


def test_read_only_dispatch_takes_no_checkpoint(tmp_path):
    rec = _Recorder()
    skill = _mk_skill(
        "ro-skill",
        safety_class="read_only",
        execution_kinds=("INSPECT",),
        allowed=("read_files",),
        forbidden=("write_file", "patch_file", "run_command"),
    )
    out = _run(skill, lambda directive, d: {"ok": True, "summary": "looked"}, rec, tmp_path)
    assert out["qwen_result"]["status"] == "success"
    assert rec.checkpoints == 0  # read_only needs no checkpoint
    assert rec.restores == []
