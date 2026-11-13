"""Phase 6H — Auto loads the skill + builds the bounded qwen dispatch (fake invoker)."""

from __future__ import annotations

import pytest

from auto_loop.skill.run import GuardResult, dispatch_skill_pack
from runtime.execution_skills import load_execution_skills
from runtime.qwen_skills import BUNDLED_SKILL_DIRS
from schema.execution_skill import ExecutionSkill
from schema.selected_skill import SelectedSkillDecision
from schema.skill_execution_pack import SkillExecutionPack, build_skill_execution_pack

_BUNDLED = BUNDLED_SKILL_DIRS[0]


def _mk_skill(
    name="syn-fixer", *, enabled=True, body="# Syn\n\nbody text marker."
) -> ExecutionSkill:
    return ExecutionSkill(
        name=name,
        skill_id=f"5.01.qwenskill-{name}",
        kind="execution_skill",
        version=1,
        enabled=enabled,
        description=name,
        skill_class="repair_skill",
        intents=("USE_SKILL",),
        execution_kinds=("REPAIR", "PATCH"),
        domains=("python",),
        allowed_tools=("read_files", "patch_file"),
        forbidden_tools=("run_command",),
        safety_class="write_safe",
        requires_confirmation_for=(),
        limits={"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": 10},
        evidence_required=("diff_summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown=body,
        source_path=f"/skills/{name}/SKILL.md",
    )


def _pack_for(skill: ExecutionSkill, objective="fix this bug") -> SkillExecutionPack:
    decision = SelectedSkillDecision.from_skill(
        skill, score=1.0, execution_kind="REPAIR", reason="t"
    )
    return build_skill_execution_pack(
        episode_id="ep_1",
        objective=objective,
        decision=decision,
        source_action_goal={"goal_kind": "operator_task", "desired_outcome": objective},
    )


class _FakeInvoker:
    def __init__(self, result=None):
        self.calls = []
        self.result = (
            result if result is not None else {"ok": True, "evidence": {"summary": "did it"}}
        )

    def __call__(self, directive, dispatch):
        self.calls.append((directive, dispatch))
        return self.result


# ── loads exact skill + pushes markdown ───────────────────────────────────────
def test_auto_loads_exact_skill_and_pushes_markdown():
    skill = _mk_skill()
    inv = _FakeInvoker()
    out = dispatch_skill_pack(
        _pack_for(skill), catalog=[skill], invoker=inv, confirmed=True, workspace_root="/ws"
    )
    assert out["status"] == "executed"
    assert out["dispatch"]["skill_id"] == skill.skill_id
    assert out["dispatch"]["skill_markdown"] == skill.body_markdown
    assert "body text marker." in out["directive"]  # the SKILL.md body reached qwen


def test_qwen_receives_bounded_skill_context():
    skill = _mk_skill()
    inv = _FakeInvoker()
    out = dispatch_skill_pack(
        _pack_for(skill), catalog=[skill], invoker=inv, confirmed=True, workspace_root="/ws"
    )
    directive = inv.calls[0][0]
    assert "bounded Omega Auto dispatch" in directive
    assert "Do not create new top-level goals." in directive
    assert "Do not use forbidden tools." in directive
    assert "read_files" in directive and "patch_file" in directive  # allowed tools
    assert "run_command" in directive  # forbidden tools surfaced
    assert "restore_project_from_memory" not in directive  # write_safe cannot overwrite a workspace


def test_auto_executes_real_migrated_skill():
    catalog, _ = load_execution_skills(roots=[_BUNDLED], config={})
    from meta_loop.skill.run import select_skill_for_intent

    decision = select_skill_for_intent(objective="fix this bug", catalog=catalog)
    pack = build_skill_execution_pack(
        episode_id="ep_1", objective="fix this bug", decision=decision, source_action_goal={}
    )
    out = dispatch_skill_pack(
        pack, catalog=catalog, invoker=_FakeInvoker(), confirmed=True, workspace_root="/ws"
    )
    assert out["status"] == "executed" and out["dispatch"]["skill_markdown"]


def test_default_execution_transport_is_the_brain(monkeypatch):
    import auto_loop.skill.run as skill_run

    calls: list[str] = []

    def _brain(directive, dispatch, **kwargs):
        calls.append(dispatch.dispatch_id)
        assert kwargs["jail_root"] == "/ws"
        assert kwargs["pipeline_id"] == 2
        assert kwargs["work_kind"] == "agent_continuation"
        assert kwargs["cycle_id"] == "cycle-2"
        assert kwargs["job_id"] == "job-2"
        return {"ok": True, "summary": "brain executed it"}

    monkeypatch.setattr(skill_run, "default_brain_skill_invoker", _brain)
    skill = _mk_skill()

    out = skill_run.dispatch_skill_pack(
        _pack_for(skill),
        catalog=[skill],
        guard=lambda dispatch: GuardResult(True),
        workspace_root="/ws",
        pipeline_id=2,
        work_kind="agent_continuation",
        cycle_id="cycle-2",
        job_id="job-2",
    )

    assert out["status"] == "executed"
    assert calls == [out["dispatch_id"]]


# ── the reject cases ──────────────────────────────────────────────────────────
def test_rejects_missing_skill():
    pack = _pack_for(_mk_skill("gone"))
    out = dispatch_skill_pack(pack, catalog=[], invoker=_FakeInvoker())
    assert out["status"] == "rejected" and "not found" in out["reason"]


def test_rejects_disabled_skill():
    skill = _mk_skill("disabled-one", enabled=True)  # build the pack from an enabled selection
    pack = _pack_for(skill)
    disabled = _mk_skill("disabled-one", enabled=False)  # but the catalog now has it disabled
    out = dispatch_skill_pack(pack, catalog=[disabled], invoker=_FakeInvoker())
    assert out["status"] == "rejected" and "disabled" in out["reason"]


def test_rejects_skill_mismatch():
    skill = _mk_skill("the-skill")
    pack = _pack_for(skill)
    # same skill_id, different name on disk -> stale pack -> rejected
    renamed = ExecutionSkill(**{**skill.__dict__, "name": "renamed"})
    renamed = ExecutionSkill(  # rebuild keeping skill_id but changing name
        name="renamed",
        skill_id=skill.skill_id,
        kind=skill.kind,
        version=1,
        enabled=True,
        description="x",
        skill_class=skill.skill_class,
        intents=skill.intents,
        execution_kinds=skill.execution_kinds,
        domains=skill.domains,
        allowed_tools=skill.allowed_tools,
        forbidden_tools=skill.forbidden_tools,
        safety_class=skill.safety_class,
        requires_confirmation_for=(),
        limits=skill.limits,
        evidence_required=skill.evidence_required,
        completion_criteria=skill.completion_criteria,
        failure_classes=skill.failure_classes,
        output_schema=skill.output_schema,
        body_markdown="b",
        source_path=skill.source_path,
    )
    out = dispatch_skill_pack(pack, catalog=[renamed], invoker=_FakeInvoker())
    assert out["status"] == "rejected" and "match" in out["reason"]


def test_rejects_invalid_pack_missing_ids():
    bad = SkillExecutionPack(
        episode_id="",
        dispatch_id="",
        objective="x",
        selected_skill={
            "skill_name": "s",
            "skill_id": "5.01.qwenskill-s",
            "skill_class": "repair_skill",
            "execution_kind": "REPAIR",
            "source_path": "/p",
        },
        allowed_tools=(),
        forbidden_tools=(),
        safety={"safety_class": "read_only"},
        limits={},
        evidence_required=(),
        completion_criteria=(),
        expected_output_schema={},
        source_action_goal={},
    )
    out = dispatch_skill_pack(bad, catalog=[_mk_skill("s")], invoker=_FakeInvoker())
    assert out["status"] == "rejected"


# ── guard runs before qwen ────────────────────────────────────────────────────
def test_blocking_guard_prevents_qwen_call():
    skill = _mk_skill()
    inv = _FakeInvoker()
    out = dispatch_skill_pack(
        _pack_for(skill),
        catalog=[skill],
        invoker=inv,
        guard=lambda d: GuardResult(False, "denied by test"),
    )
    assert out["status"] == "blocked" and out["reason"] == "denied by test"
    assert inv.calls == []  # qwen was NOT called before/after the guard denial


def test_allowing_guard_lets_qwen_run():
    skill = _mk_skill()
    inv = _FakeInvoker()
    order = []
    out = dispatch_skill_pack(
        _pack_for(skill),
        catalog=[skill],
        invoker=inv,
        guard=lambda d: (order.append("guard"), GuardResult(True))[1],
    )
    assert out["status"] == "executed"
    assert order == ["guard"] and len(inv.calls) == 1  # guard ran, then qwen


def test_default_guard_blocks_self_modification():
    selfmod = _mk_skill("selfmod")
    selfmod = ExecutionSkill(**{**selfmod.__dict__})
    # override safety_class to self_modification via the pack's safety envelope
    pack = _pack_for(selfmod)
    pack = SkillExecutionPack(
        **{**pack.__dict__, "safety": {**pack.safety, "safety_class": "self_modification"}}
    )
    inv = _FakeInvoker()
    out = dispatch_skill_pack(pack, catalog=[selfmod], invoker=inv)
    assert out["status"] == "rejected" and "safety contract" in out["reason"]
    assert inv.calls == []
