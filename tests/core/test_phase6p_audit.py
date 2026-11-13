"""Phase 6P — the lock audit as a runnable test (the 14-point checklist).

Every check the audit doc claims is verified here so it cannot silently rot.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
# The cognition spine - the decision cascade. The Python qwen bridge is retired;
# nothing in the spine or Auto may reach it.
_SPINE_DIRS = ["global_loop", "meta_loop", "omega_loop", "pressure_loop", "deepsleep_loop"]
_SPINE_FILES = ["cognition_chain.py"]
_SANCTIONED_BRIDGE = set()


def _py_files(*dirs_and_files):
    out = []
    for d in dirs_and_files:
        p = _REPO / d
        if p.is_dir():
            out += sorted(p.rglob("*.py"))
        elif p.is_file():
            out.append(p)
    return out


def _scan(files, needles):
    hits = []
    for f in files:
        if not f.is_file():
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for n in needles:
            if n in txt:
                hits.append(f"{f.relative_to(_REPO).as_posix()} :: {n}")
    return hits


# 1. no direct qwen AgentCore call in the cognition spine
def test_audit_1_no_agentcore_in_spine():
    assert _scan(_py_files(*_SPINE_DIRS, *_SPINE_FILES), ("AgentCore", "agent_core")) == []


# 2. no raw qwen bridge call outside the sanctioned Auto/guard path
def test_audit_2_no_raw_bridge_outside_auto():
    spine_hits = _scan(
        _py_files(*_SPINE_DIRS, *_SPINE_FILES),
        ("_dispatch_ws", "default_apply_invoker", "default_qwen_skill_invoker", "websockets"),
    )
    assert spine_hits == []
    auto_hits = [
        h
        for h in _scan(
            _py_files("auto_loop"), ("_dispatch_ws", "default_apply_invoker", "websockets.connect")
        )
        if h.split(" :: ")[0] not in _SANCTIONED_BRIDGE
    ]
    assert auto_hits == []


# 3. no raw file/write/command tool is a first-class cascade tool (they are qwen-internal)
def test_audit_3_no_raw_world_mutating_first_class_tool():
    from core.paths import TOOL_SCHEMA_ROOT
    from schema.routing_policy import is_world_mutating_raw

    names = [p.stem for p in Path(TOOL_SCHEMA_ROOT).glob("*.json")]
    assert not [n for n in names if is_world_mutating_raw(n)], (
        f"raw tool exposed as first-class: {names}"
    )


# 4. Global routes an operator's existing selection; Meta A resolves the skill.
def test_audit_4_global_never_selects_skill():
    # Naming selected_skill while validating a durable handoff is not selection.
    # Guard the selector/catalog APIs, and keep the behavioral handoff coverage
    # in tests/global_loop/test_specialist_lane_route.py.
    assert (
        _scan(_py_files("global_loop"), (
            "select_skill_for_intent", "selectable_execution_skills", "skill_id",
        ))
        == []
    )


# 5. Meta never executes / calls qwen (no bridge call in meta_loop)
def test_audit_5_meta_never_calls_qwen():
    assert (
        _scan(
            _py_files("meta_loop"),
            (
                "_dispatch_ws",
                "default_qwen_skill_invoker",
                "dispatch_skill_pack",
                "qwen_cli_client",
            ),
        )
        == []
    )


# 6. Auto never selects the skill (it consumes Meta A's selection)
def test_audit_6_auto_never_selects_skill():
    assert _scan(_py_files("auto_loop"), ("select_skill_for_intent",)) == []


# 7. qwen is told never to write canonical memory / new goal / continue outside dispatch
def test_audit_7_qwen_forbidden_canonical_memory():
    from schema.qwen_skill_dispatch import BOUNDARY_STATEMENTS

    joined = " ".join(BOUNDARY_STATEMENTS).lower()
    assert "do not write canonical omega memory" in joined
    assert "do not create new top-level goals" in joined
    assert "do not continue beyond this dispatch" in joined


# 8. every dispatch produces exactly one Meta B outcome (ledger)
def test_audit_8_one_outcome_per_dispatch():
    import pytest

    from meta_loop.skill.foldback import MetaBSkillLedger, SkillFoldbackError, fold_skill_result

    r = {
        "status": "success",
        "episode_id": "e",
        "dispatch_id": "d1",
        "skill_name": "s",
        "skill_id": "5.01.qwenskill-s",
        "execution_kind": "TEST",
    }
    ledger = MetaBSkillLedger()
    fold_skill_result(r, objective="o", ledger=ledger)
    with pytest.raises(SkillFoldbackError):
        fold_skill_result(r, objective="o", ledger=ledger)  # duplicate fails


# 9. failed / malformed qwen result fails closed
def test_audit_9_malformed_result_fails_closed():
    from schema.qwen_skill_dispatch import QwenSkillExecutionDispatch
    from schema.qwen_skill_result import normalize_qwen_result

    d = QwenSkillExecutionDispatch(
        episode_id="e",
        dispatch_id="d",
        skill_name="s",
        skill_id="i",
        skill_class="repair_skill",
        execution_kind="REPAIR",
        objective="o",
        skill_markdown="b",
        allowed_tools=(),
        forbidden_tools=(),
        safety={},
        limits={},
        evidence_required=(),
        completion_criteria=(),
        expected_output_schema={},
        source_action_goal={},
        source_skill_execution_pack={},
    )
    assert normalize_qwen_result("not a dict", dispatch=d).status == "malformed_result"


# 10. user-created skills are disabled by default
def test_audit_10_user_skills_disabled_by_default(tmp_path):
    import yaml

    from runtime.user_execution_skills import load_user_execution_skills

    fm = {
        "name": "u",
        "kind": "execution_skill",
        "version": 1,
        "enabled": True,
        "description": "u",
        "skill_class": "review_skill",
        "intents": ["USE_SKILL"],
        "execution_kinds": ["REVIEW"],
        "domains": ["x"],
        "allowed_tools": ["read_files"],
        "forbidden_tools": ["write_file"],
        "safety_class": "read_only",
        "requires_confirmation_for": [],
        "max_steps": 5,
        "max_duration_ms": 1000,
        "max_files_changed": 0,
        "evidence_required": ["s"],
        "completion_criteria": ["d"],
        "failure_classes": ["b"],
        "output_schema": {"type": "object"},
    }
    (tmp_path / "u").mkdir()
    (tmp_path / "u" / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump(fm) + "---\n\nbody", encoding="utf-8"
    )
    skills, _ = load_user_execution_skills(roots=[str(tmp_path)])
    assert skills and skills[0].enabled is False and skills[0].selectable is False


# 11. skill validation catches unsafe (self_modification) declarations
def test_audit_11_validation_catches_unsafe():
    from schema.execution_skill import validate_execution_skill_frontmatter

    errs = validate_execution_skill_frontmatter({"safety_class": "self_modification"})
    assert errs  # rejected (self_modification not permitted without policy)


# 12. all bundled EXECUTION skills validate + are selectable
def test_audit_12_bundled_execution_skills_validate():
    from runtime.execution_skills import selectable_execution_skills
    from runtime.qwen_skills import BUNDLED_SKILL_DIRS

    sel = {s.name for s in selectable_execution_skills(roots=[BUNDLED_SKILL_DIRS[0]], config={})}
    expected = {
        "omega-planner-executor",
        "omega-refactor-surgeon",
        "codex-grade-coding",
        "testing-strategy",
        "debugging-methodology",
        "code-review-excellence",
        "skill-system-maintainer",
        "omega-yolo-safe-edits",
    }
    assert expected <= sel, f"missing execution skills: {expected - sel}"
    # all bundled skills are migrated to ExecutionSkills now (21 of 22 dirs; 1 has no SKILL.md)
    assert len(sel) >= 20, f"expected the full bundled catalog migrated, got {len(sel)}"


# 13. capability sync is idempotent (deterministic ids)
def test_audit_13_capability_id_deterministic():
    from memory.capability_catalog import capability_id_for_name

    assert (
        capability_id_for_name("codex-grade-coding")
        == capability_id_for_name("codex-grade-coding")
        == "5.01.qwenskill-codex-grade-coding"
    )


# 14. capability reinforcement only on grounded success
def test_audit_14_reinforce_only_on_grounded_success():
    from deepsleep_loop.skill_consolidation import derive_capability_signal

    base = {"skill_name": "s", "skill_id": "i", "failure_class": None}
    grounded = derive_capability_signal(
        {**base, "outcome_kind": "skill_success"}, {"files_changed": ["a"]}
    )
    hollow = derive_capability_signal({**base, "outcome_kind": "skill_success"}, {})
    pending = derive_capability_signal({**base, "outcome_kind": "skill_requires_confirmation"}, {})
    assert grounded.signal == "reinforce"
    assert hollow.signal == "neutral" and pending.signal == "neutral"
