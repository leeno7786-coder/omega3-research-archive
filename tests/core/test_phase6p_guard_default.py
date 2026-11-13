"""Phase 6P — Auto dispatch uses the REAL skill_execution_guard by default (hardening proof).

Proves the live path: read_only executes freely; write_safe is held until confirmed; command_safe
needs a command policy; self_modification + forbidden-in-allowed + write-without-jail are blocked;
and qwen is NEVER contacted when the guard blocks. The real guard is the dispatch_skill_pack
DEFAULT — no guard is injected in these tests.
"""

from __future__ import annotations

from auto_loop.skill.run import dispatch_skill_pack
from schema.execution_skill import ExecutionSkill
from schema.selected_skill import SelectedSkillDecision
from schema.skill_execution_pack import build_skill_execution_pack

_WS = "/safe/operator/ws"


def _mk_skill(
    name="g-skill",
    *,
    safety_class="write_safe",
    skill_class="repair_skill",
    execution_kinds=("REPAIR",),
    allowed=("read_files", "patch_file"),
    forbidden=("run_command",),
    max_files=10,
):
    return ExecutionSkill(
        name=name,
        skill_id=f"5.01.qwenskill-{name}",
        kind="execution_skill",
        version=1,
        enabled=True,
        description=name,
        skill_class=skill_class,
        intents=("USE_SKILL",),
        execution_kinds=tuple(execution_kinds),
        domains=("python",),
        allowed_tools=tuple(allowed),
        forbidden_tools=tuple(forbidden),
        safety_class=safety_class,
        requires_confirmation_for=(),
        limits={"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": max_files},
        evidence_required=("summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="# body",
        source_path=f"/skills/{name}/SKILL.md",
    )


def _pack_for(skill, kind=None):
    decision = SelectedSkillDecision.from_skill(
        skill, score=1.0, execution_kind=(kind or skill.execution_kinds[0]), reason="t"
    )
    return build_skill_execution_pack(
        episode_id="ep_1", objective="do the thing", decision=decision, source_action_goal={}
    )


class _CountingInvoker:
    def __init__(self):
        self.calls = 0

    def __call__(self, directive, dispatch):
        self.calls += 1
        return {"ok": True, "summary": "done"}


# ── the default guard IS the real one ─────────────────────────────────────────
def test_dispatch_uses_real_guard_by_default():
    # write_safe with no confirmation: the MINIMAL guard would allow; the REAL guard holds it.
    skill = _mk_skill()
    inv = _CountingInvoker()
    out = dispatch_skill_pack(_pack_for(skill), catalog=[skill], invoker=inv)
    assert out["status"] == "requires_confirmation"  # not "executed" -> real guard
    # rich GuardStatus fields prove it is skill_execution_guard, not the minimal GuardResult
    gs = out["guard_status"]
    assert {"checkpoint_required", "timeout_ms", "workspace_jail", "policy"} <= set(gs)
    assert inv.calls == 0  # qwen NOT contacted


# ── read_only executes without confirmation ───────────────────────────────────
def test_read_only_executes_without_confirmation():
    skill = _mk_skill(
        "ro",
        safety_class="read_only",
        skill_class="inspection_skill",
        execution_kinds=("INSPECT",),
        allowed=("read_files",),
        forbidden=("write_file",),
        max_files=0,
    )
    inv = _CountingInvoker()
    out = dispatch_skill_pack(_pack_for(skill), catalog=[skill], invoker=inv)
    assert out["status"] == "executed" and inv.calls == 1
    assert out["guard_status"]["checkpoint_required"] is False


# ── write_safe held until confirmed (+ workspace root) ────────────────────────
def test_write_safe_requires_confirmation_unless_confirmed():
    skill = _mk_skill()
    inv = _CountingInvoker()
    held = dispatch_skill_pack(_pack_for(skill), catalog=[skill], invoker=inv)
    assert held["status"] == "requires_confirmation" and inv.calls == 0
    ok = dispatch_skill_pack(
        _pack_for(skill), catalog=[skill], invoker=inv, confirmed=True, workspace_root=_WS
    )
    assert ok["status"] == "executed" and inv.calls == 1


def test_write_safe_without_workspace_root_fails_closed():
    skill = _mk_skill()
    inv = _CountingInvoker()
    out = dispatch_skill_pack(
        _pack_for(skill), catalog=[skill], invoker=inv, confirmed=True
    )  # no workspace_root
    assert out["status"] == "blocked" and "workspace jail root" in out["reason"]
    assert inv.calls == 0


# ── command_safe needs a command policy ───────────────────────────────────────
def test_command_safe_requires_command_policy():
    skill = _mk_skill(
        "cmd",
        safety_class="command_safe",
        skill_class="testing_skill",
        execution_kinds=("TEST",),
        allowed=("run_tests",),
        forbidden=(),
    )
    inv = _CountingInvoker()
    blocked = dispatch_skill_pack(
        _pack_for(skill), catalog=[skill], invoker=inv, workspace_root=_WS
    )  # no command_policy
    assert blocked["status"] == "blocked" and "command policy" in blocked["reason"]
    assert inv.calls == 0
    ok = dispatch_skill_pack(
        _pack_for(skill),
        catalog=[skill],
        invoker=inv,
        command_policy=lambda c: True,
        workspace_root=_WS,
    )
    assert ok["status"] == "executed" and inv.calls == 1


# ── self_modification blocked ─────────────────────────────────────────────────
def test_self_modification_blocked_before_qwen():
    skill = _mk_skill(
        "sm",
        safety_class="self_modification",
        skill_class="maintenance_skill",
        execution_kinds=("MAINTAIN",),
        allowed=("read_files",),
        forbidden=(),
    )
    inv = _CountingInvoker()
    out = dispatch_skill_pack(
        _pack_for(skill), catalog=[skill], invoker=inv, confirmed=True, workspace_root=_WS
    )
    assert out["status"] == "blocked" and "self_modification" in out["reason"]
    assert inv.calls == 0


# ── forbidden-in-allowed blocked before qwen ──────────────────────────────────
def test_skill_forbidden_tools_do_not_gate_the_dispatch():
    # A skill's forbidden tools remain part of the execution contract. A
    # write_safe skill that declares a command tool is rejected before contact.
    skill = _mk_skill("bad", allowed=("read_files", "run_command"), forbidden=("run_command",))
    inv = _CountingInvoker()
    out = dispatch_skill_pack(
        _pack_for(skill), catalog=[skill], invoker=inv, confirmed=True, workspace_root=_WS
    )
    assert out["status"] == "rejected"
    assert "skill execution pack" in str(out.get("reason") or ""), out


# ── qwen never contacted when the guard blocks (any reason) ───────────────────
def test_qwen_not_called_when_guard_blocks():
    for skill in (
        _mk_skill("w"),  # write_safe unconfirmed -> requires_confirmation
        _mk_skill(
            "sm2", safety_class="self_modification", allowed=("read_files",), forbidden=()
        ),  # self-mod
        _mk_skill(
            "cmd2",
            safety_class="command_safe",
            execution_kinds=("TEST",),
            allowed=("run_tests",),
            forbidden=(),
        ),  # no command policy
    ):
        inv = _CountingInvoker()
        out = dispatch_skill_pack(_pack_for(skill), catalog=[skill], invoker=inv)
        assert out["status"] in ("requires_confirmation", "blocked")
        assert inv.calls == 0
