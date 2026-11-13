"""Phase 6D — the 8 bundled skills migrated to execution frontmatter load + validate."""

from __future__ import annotations

import pytest

from runtime.qwen_skills import BUNDLED_SKILL_DIRS, load_skills
from runtime.execution_skills import load_execution_skills, selectable_execution_skills

_BUNDLED = BUNDLED_SKILL_DIRS[0]  # local skills

_EXPECTED = {
    "omega-planner-executor": ("planning_skill", "read_only"),
    "omega-refactor-surgeon": ("refactor_skill", "write_safe"),
    "codex-grade-coding": ("repair_skill", "write_safe"),
    "testing-strategy": ("testing_skill", "command_safe"),
    "debugging-methodology": ("debugging_skill", "write_safe"),
    "code-review-excellence": ("review_skill", "read_only"),
    "skill-system-maintainer": ("maintenance_skill", "write_safe"),
    "omega-yolo-safe-edits": ("repair_skill", "write_safe"),
}


@pytest.fixture(scope="module")
def loaded():
    return load_execution_skills(roots=[_BUNDLED], config={})


def test_all_eight_migrated_skills_load_and_validate(loaded):
    skills, diags = loaded
    by_name = {s.name: s for s in skills}
    diag_names = {d.name for d in diags}
    for name, (skill_class, safety_class) in _EXPECTED.items():
        assert name in by_name, f"{name} did not load as ExecutionSkill"
        assert name not in diag_names, f"{name} produced a diagnostic"
        assert by_name[name].skill_class == skill_class
        assert by_name[name].safety_class == safety_class
        assert by_name[name].kind == "execution_skill"


def test_migrated_skills_carry_the_auto_dispatch_contract(loaded):
    skills, _ = loaded
    by_name = {s.name: s for s in skills}
    for name in _EXPECTED:
        body = by_name[name].body_markdown
        assert "Omega Auto dispatch" in body
        assert "top-level goals" in body  # forbids creating new top-level goals


def test_migrated_skills_are_selectable_and_deterministic_ids(loaded):
    skills, _ = loaded
    by_name = {s.name: s for s in skills}
    for name in _EXPECTED:
        s = by_name[name]
        assert s.selectable is True  # all migrated skills are enabled
        assert s.skill_id == f"5.01.qwenskill-{name}"  # deterministic, matches catalog
    sel = {s.name for s in selectable_execution_skills(roots=[_BUNDLED], config={})}
    assert set(_EXPECTED).issubset(sel)


def test_read_only_skills_declare_no_writes(loaded):
    skills, _ = loaded
    by_name = {s.name: s for s in skills}
    for name, (_cls, safety) in _EXPECTED.items():
        if safety == "read_only":
            assert by_name[name].limits["max_files_changed"] == 0
            assert not (
                set(by_name[name].allowed_tools) & {"write_file", "patch_file", "run_command"}
            )


def test_existing_legacy_catalog_still_loads(loaded):
    # the legacy QwenSkill loader must still read the migrated skills (name/description)
    legacy = load_skills(roots=[_BUNDLED], config={})
    for name in _EXPECTED:
        assert name in legacy
        assert legacy[name].description  # non-empty description survived the migration
