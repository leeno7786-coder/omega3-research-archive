"""Light keyword skill index — per-command skill matching (the redo of skill selection).

Operator doctrine: every command keyword-matches the relevant skills from the full
catalog (decoupled from the strict ExecutionSkill schema) so the executor gets good
context. These pin the matcher's behavior on the live catalog.
"""

from __future__ import annotations

from auto_loop.skill.skill_index import (
    load_skill_index,
    match_skills,
    skill_context_for_command,
)


def test_index_loads_the_catalog():
    idx = load_skill_index()
    assert len(idx) >= 20
    # every entry carries the keyword token sets the matcher scores on
    sample = idx[0]
    for key in ("name", "trigger_tokens", "name_tokens", "desc_tokens", "body_tokens", "body"):
        assert key in sample


def test_security_audit_matches_auditing_skill():
    hits = match_skills("audit this codebase for security vulnerabilities", top_n=4)
    assert hits
    assert hits[0]["name"] == "auditing-security"


def test_pytest_command_matches_pytest_skill():
    names = [h["name"] for h in match_skills("write unit tests with pytest", top_n=4)]
    assert "pytest" in names


def test_matches_dedupe_by_name():
    hits = match_skills("write unit tests with pytest", top_n=8)
    names = [h["name"] for h in hits]
    assert len(names) == len(set(names))  # a skill in several roots appears once


def test_context_block_bundles_guidance_and_tools_note():
    ctx = skill_context_for_command("build me a flask api with a health route", top_n=2)
    assert "Skill:" in ctx
    assert "read/grep/edit/write/shell" in ctx


def test_nonsense_command_matches_nothing():
    assert match_skills("zzxq wqvbn pllk", top_n=4) == []
    assert skill_context_for_command("zzxq wqvbn pllk") == ""
