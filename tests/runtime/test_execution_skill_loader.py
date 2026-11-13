"""Phase 6C — ExecutionSkill object + loader. Synthetic SKILL.md fixtures in tmp dirs
(real bundled skills are migrated to execution frontmatter in Phase 6D)."""

from __future__ import annotations

from pathlib import Path

import yaml

from runtime.execution_skills import load_execution_skills, selectable_execution_skills
from memory.capability_catalog import capability_id_for_name


def _fm(**over) -> dict:
    fm = {
        "name": "codex-grade-coding",
        "kind": "execution_skill",
        "version": 1,
        "enabled": True,
        "description": "Repair code under guard.",
        "skill_class": "repair_skill",
        "intents": ["USE_SKILL"],
        "execution_kinds": ["INSPECT", "PATCH", "TEST", "VERIFY", "REPAIR"],
        "domains": ["python"],
        "allowed_tools": ["read_files", "patch_file", "run_tests"],
        "forbidden_tools": ["run_command"],
        "safety_class": "write_safe",
        "requires_confirmation_for": [],
        "max_steps": 20,
        "max_duration_ms": 600000,
        "max_files_changed": 10,
        "evidence_required": ["diff_summary"],
        "completion_criteria": ["tests_pass"],
        "failure_classes": ["test_failure"],
        "output_schema": {"type": "object"},
    }
    fm.update(over)
    return fm


def _write_skill(
    root: Path, dirname: str, *, frontmatter: dict, body: str = "# Skill\n\nDo the thing."
) -> None:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False)
    (d / "SKILL.md").write_text(f"---\n{fm_yaml}---\n\n{body}", encoding="utf-8")


def _write_raw(root: Path, dirname: str, content: str) -> None:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")


def test_valid_skill_loads(tmp_path):
    _write_skill(tmp_path, "codex-grade-coding", frontmatter=_fm())
    skills, diags = load_execution_skills(roots=[tmp_path], config={})
    assert diags == []
    assert len(skills) == 1
    assert skills[0].name == "codex-grade-coding"
    assert skills[0].skill_class == "repair_skill"
    assert skills[0].limits["max_steps"] == 20


def test_malformed_skill_fails_soft(tmp_path):
    _write_skill(tmp_path, "good", frontmatter=_fm(name="good"))
    _write_raw(tmp_path, "broken-yaml", "---\nname: bad\nallowed_tools: [unclosed\n---\nbody")
    _write_skill(
        tmp_path, "incomplete", frontmatter={"name": "incomplete", "kind": "execution_skill"}
    )
    skills, diags = load_execution_skills(roots=[tmp_path], config={})
    # the one good skill survives; the two bad ones are skipped, not raised.
    # the YAML-malformed skill can't expose its frontmatter name -> diagnostic uses the dir.
    assert [s.name for s in skills] == ["good"]
    assert {d.name for d in diags} == {"broken-yaml", "incomplete"}


def test_disabled_skill_loads_but_not_selectable(tmp_path):
    _write_skill(tmp_path, "disabled-skill", frontmatter=_fm(name="disabled-skill", enabled=False))
    skills, diags = load_execution_skills(roots=[tmp_path], config={})
    assert diags == []
    assert len(skills) == 1 and skills[0].enabled is False
    assert skills[0].selectable is False
    assert selectable_execution_skills(roots=[tmp_path], config={}) == []


def test_skill_id_is_deterministic(tmp_path):
    _write_skill(tmp_path, "codex-grade-coding", frontmatter=_fm())
    a, _ = load_execution_skills(roots=[tmp_path], config={})
    b, _ = load_execution_skills(roots=[tmp_path], config={})
    assert a[0].skill_id == b[0].skill_id


def test_skill_id_matches_capability_id_for_name(tmp_path):
    _write_skill(tmp_path, "codex-grade-coding", frontmatter=_fm())
    skills, _ = load_execution_skills(roots=[tmp_path], config={})
    assert skills[0].skill_id == capability_id_for_name("codex-grade-coding", scope="dev")
    assert skills[0].skill_id == "5.01.qwenskill-codex-grade-coding"


def test_load_order_is_stable(tmp_path):
    for nm in ("zeta-skill", "alpha-skill", "mid-skill"):
        _write_skill(tmp_path, nm, frontmatter=_fm(name=nm))
    skills, _ = load_execution_skills(roots=[tmp_path], config={})
    assert [s.name for s in skills] == ["alpha-skill", "mid-skill", "zeta-skill"]


def test_body_markdown_is_preserved(tmp_path):
    body = "# Codex Coding\n\nExecute ONLY inside an Omega Auto dispatch.\n\n## Procedure\n1. inspect\n2. patch"
    _write_skill(tmp_path, "codex-grade-coding", frontmatter=_fm(), body=body)
    skills, _ = load_execution_skills(roots=[tmp_path], config={})
    assert skills[0].body_markdown == body
    assert "Omega Auto dispatch" in skills[0].body_markdown


def test_schema_diagnostics_are_emitted(tmp_path):
    _write_skill(tmp_path, "no-kind", frontmatter=_fm(name="no-kind", safety_class="yolo"))
    skills, diags = load_execution_skills(roots=[tmp_path], config={})
    assert skills == []
    assert len(diags) == 1
    assert diags[0].name == "no-kind"
    assert any("safety_class" in e for e in diags[0].errors)


# ── agent-format adaptation (open agent-skills libraries, e.g. ~/.agents) ─────
def test_agent_format_skill_adapts_to_pure_guidance(tmp_path):
    body = "# Azure Deploy\n\nGuidance for deploying to Azure App Service."
    _write_skill(
        tmp_path,
        "azure-deploy",
        frontmatter={"name": "azure-deploy", "description": "Deploy apps to Azure."},
        body=body,
    )
    skills, diags = load_execution_skills(roots=[tmp_path], config={})
    assert diags == []
    assert len(skills) == 1
    s = skills[0]
    # adapted contract: pure guidance — cannot act
    assert s.kind == "execution_skill"
    assert s.skill_class == "planning_skill"
    assert s.safety_class == "read_only"
    assert s.allowed_tools == ()
    assert s.limits["max_files_changed"] == 0
    assert s.body_markdown == body  # the guidance payload survives
    assert s.description == "Deploy apps to Azure."
    assert s.selectable is True


def test_agent_format_skill_without_name_uses_dirname(tmp_path):
    _write_skill(tmp_path, "graphify", frontmatter={"description": "Knowledge graph tools."})
    skills, diags = load_execution_skills(roots=[tmp_path], config={})
    assert diags == []
    assert [s.name for s in skills] == ["graphify"]


def test_declared_kind_null_is_not_adapted(tmp_path):
    # an authored contract with a broken kind must stay a diagnostic, not silently adapt
    _write_skill(tmp_path, "broken-decl", frontmatter={"name": "broken-decl", "kind": None})
    skills, diags = load_execution_skills(roots=[tmp_path], config={})
    assert skills == []
    assert len(diags) == 1 and diags[0].name == "broken-decl"


def test_adapted_skill_does_not_shadow_authored_contract(tmp_path):
    authored_root = tmp_path / "authored"
    agent_root = tmp_path / "agent"
    _write_skill(authored_root, "codex-grade-coding", frontmatter=_fm())
    _write_skill(
        agent_root,
        "codex-grade-coding",
        frontmatter={"name": "codex-grade-coding", "description": "agent-format duplicate"},
    )
    # later roots override earlier on name collision — the loader contract. Callers that
    # must keep authored contracts authoritative (meta_loop) pass bundled roots LAST or
    # merge with setdefault; here we assert the deterministic override order itself.
    skills, _ = load_execution_skills(roots=[agent_root, authored_root], config={})
    assert len(skills) == 1
    assert skills[0].skill_class == "repair_skill"  # authored contract won
