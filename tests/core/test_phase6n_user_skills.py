"""Phase 6N — user-created execution skills foundation (disabled-by-default)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from memory.capability_catalog import capability_id_for_name
from runtime.execution_skills import selectable_execution_skills
from runtime.qwen_skills import BUNDLED_SKILL_DIRS
from runtime.user_execution_skills import (
    load_user_execution_skills,
    selectable_user_execution_skills,
)
from schema.execution_skill import ExecutionSkill

_BUNDLED = BUNDLED_SKILL_DIRS[0]

_VALID_FM = {
    "name": "user-doc-helper",
    "kind": "execution_skill",
    "version": 1,
    "enabled": True,
    "description": "a user-authored read-only doc helper",
    "skill_class": "review_skill",
    "intents": ["USE_SKILL"],
    "execution_kinds": ["REVIEW", "INSPECT"],
    "domains": ["engineering"],
    "allowed_tools": ["read_files"],
    "forbidden_tools": ["write_file", "run_command", "patch_file"],
    "safety_class": "read_only",
    "requires_confirmation_for": [],
    "max_steps": 30,
    "max_duration_ms": 600000,
    "max_files_changed": 0,
    "evidence_required": ["findings"],
    "completion_criteria": ["review_complete"],
    "failure_classes": ["blocked"],
    "output_schema": {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    },
}


def _write_skill(root: Path, name: str, **over) -> None:
    fm = {**_VALID_FM, "name": name}
    fm.update(over)
    sd = root / name
    sd.mkdir(parents=True, exist_ok=True)
    body = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# " + name + "\n\nbody.\n"
    (sd / "SKILL.md").write_text(body, encoding="utf-8")


def _write_raw(root: Path, name: str, text: str) -> None:
    sd = root / name
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "SKILL.md").write_text(text, encoding="utf-8")


# ── valid user skill loads but is DISABLED by default ─────────────────────────
def test_valid_user_skill_loads_disabled_by_default(tmp_path):
    _write_skill(tmp_path, "user-doc-helper")
    skills, diags = load_user_execution_skills(roots=[str(tmp_path)])
    by_name = {s.name: s for s in skills}
    assert "user-doc-helper" in by_name  # it loaded
    assert by_name["user-doc-helper"].enabled is False  # but disabled
    assert by_name["user-doc-helper"].selectable is False


def test_disabled_user_skill_not_selectable(tmp_path):
    _write_skill(tmp_path, "user-doc-helper")
    assert selectable_user_execution_skills(roots=[str(tmp_path)]) == []


# ── trust gating ──────────────────────────────────────────────────────────────
def test_trusted_user_skill_selectable_only_with_explicit_policy(tmp_path):
    _write_skill(tmp_path, "user-doc-helper")
    # named in the trust allow-list
    by_name = selectable_user_execution_skills(
        roots=[str(tmp_path)], trust_approved=["user-doc-helper"]
    )
    assert [s.name for s in by_name] == ["user-doc-helper"]
    # or via the global trust flag
    flagged = selectable_user_execution_skills(roots=[str(tmp_path)], trust_all=True)
    assert [s.name for s in flagged] == ["user-doc-helper"]
    # a DIFFERENT name in the allow-list does not unlock it
    assert (
        selectable_user_execution_skills(roots=[str(tmp_path)], trust_approved=["something-else"])
        == []
    )


# ── pure-guidance exception: no execution surface -> trust gate does not apply ─
def test_pure_guidance_user_skill_stays_enabled_without_trust(tmp_path):
    # agent-format skill (no kind) adapts to read_only/toolless/zero-write guidance
    sd = tmp_path / "azure-deploy"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\nname: azure-deploy\ndescription: Deploy apps to Azure.\n---\n\n# Guide\n",
        encoding="utf-8",
    )
    selectable = selectable_user_execution_skills(roots=[str(tmp_path)])
    assert [s.name for s in selectable] == ["azure-deploy"]
    s = selectable[0]
    assert s.safety_class == "read_only" and s.allowed_tools == ()
    assert s.limits["max_files_changed"] == 0


def test_tool_bearing_user_skill_still_forced_disabled(tmp_path):
    # the Phase 6N invariant is untouched for anything WITH an execution surface
    _write_skill(tmp_path, "user-doc-helper")  # read_only but declares read_files
    assert selectable_user_execution_skills(roots=[str(tmp_path)]) == []


# ── malformed / unsafe rejected ───────────────────────────────────────────────
def test_malformed_user_skill_rejected(tmp_path):
    _write_raw(tmp_path, "broken", "---\nname: [unclosed list\n  bad: yaml\n---\n\nbody")
    skills, diags = load_user_execution_skills(roots=[str(tmp_path)])
    assert "broken" not in {s.name for s in skills}
    assert diags  # surfaced as a diagnostic, never as a loaded skill


def test_unsafe_self_modification_user_skill_rejected(tmp_path):
    _write_skill(tmp_path, "evil", safety_class="self_modification")
    skills, diags = load_user_execution_skills(roots=[str(tmp_path)], trust_all=True)
    # even with trust, validation rejects self_modification -> never loads
    assert "evil" not in {s.name for s in skills}
    assert any("evil" in d.name or "self_modification" in (d.reason or "") for d in diags)


# ── deterministic skill_id + same schema ──────────────────────────────────────
def test_user_skill_gets_deterministic_skill_id(tmp_path):
    _write_skill(tmp_path, "user-doc-helper")
    skills, _ = load_user_execution_skills(roots=[str(tmp_path)])
    skill = next(s for s in skills if s.name == "user-doc-helper")
    assert skill.skill_id == capability_id_for_name("user-doc-helper", scope="dev")
    assert skill.skill_id == "5.01.qwenskill-user-doc-helper"
    assert isinstance(skill, ExecutionSkill)  # same schema as bundled


# ── contrast: bundled skills ARE enabled by default ───────────────────────────
def test_bundled_skills_enabled_by_default():
    bundled = selectable_execution_skills(roots=[_BUNDLED], config={})
    assert len(bundled) > 0  # bundled skills are selectable out of the box


from meta_loop.skill.run import select_skill_for_intent


def test_trusted_user_skill_can_be_selected_by_meta_a(tmp_path):
    _write_skill(tmp_path, "user-doc-helper")
    user = selectable_user_execution_skills(
        roots=[str(tmp_path)], trust_approved=["user-doc-helper"]
    )
    d = select_skill_for_intent(objective="review this document for quality", catalog=user)
    assert d.selected and d.skill_name == "user-doc-helper"


def test_selection_catalog_includes_trusted_user_skills(tmp_path, monkeypatch):
    _write_skill(tmp_path, "user-doc-helper")
    monkeypatch.setattr("meta_loop.skill.run._user_skill_roots", lambda: [tmp_path])
    monkeypatch.setenv("OMEGA_TRUST_USER_SKILLS", "user-doc-helper")
    from meta_loop.skill.run import _selection_catalog

    assert any(s.name == "user-doc-helper" for s in _selection_catalog())


# ── doc exists + covers the 12 contract items ─────────────────────────────────
def test_user_skills_contract_doc_exists_and_is_complete():
    doc = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "architecture"
        / "USER_EXECUTION_SKILLS_CONTRACT.md"
    )
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8").lower()
    for needle in [
        "directory layout",
        "frontmatter",
        "safety class",
        "tool declaration",
        "validation",
        "disabled-by-default",
        "trust",
        "capabilit",
        "skill_id",
        "reinforce",
        "failure",
        "reject",
    ]:
        assert needle in text, f"contract doc missing: {needle}"
