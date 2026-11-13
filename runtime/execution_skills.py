"""Execution-skill loader (Phase 6C) — every valid SKILL.md becomes an ExecutionSkill.

This is the structured-contract loader, sitting beside the legacy QwenSkill loader in
``runtime.qwen_skills`` (which is left untouched). It parses the SKILL.md frontmatter as
real YAML (execution frontmatter carries a nested ``output_schema`` + typed limits the
simple line-parser cannot represent), validates it against
``schema.execution_skill``, computes the DETERMINISTIC ``skill_id`` via
``capability_id_for_name`` (so it matches the capability catalog leaf), preserves the
body markdown, and returns skills sorted by normalized name.

Agent-format adaptation: a SKILL.md whose frontmatter has no ``kind`` field is the open
agent-skills format (name + description prompt guidance — the ~/.agents libraries).
Those are ADAPTED into a pure-guidance execution contract (read_only planning_skill,
toolless, zero writes) via ``adapt_agent_skill_frontmatter`` and then run through the
SAME validator as authored contracts — no validation bypass. Only frontmatter that
DECLARES ``kind: execution_skill`` and still fails validation is a per-skill WARNING;
foreign/malformed files are diagnostics + one summary line (no per-file warning flood).

Fail-soft per skill: a malformed SKILL.md is recorded as a diagnostic and skipped — it
never raises, so a single bad skill cannot abort boot.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from runtime.qwen_skills import BUNDLED_SKILL_DIRS, load_skill_config, normalize_skill_name
from schema.execution_skill import KIND, ExecutionSkill, validate_execution_skill_frontmatter

log = logging.getLogger("omega.runtime.execution_skills")

_FRONTMATTER_RE = re.compile(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n?")


@dataclass(frozen=True)
class SkillLoadDiagnostic:
    """A malformed/invalid SKILL.md that was skipped (fail-soft)."""

    source_path: str
    name: str
    errors: List[str] = field(default_factory=list)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (yaml_block, body_markdown). No frontmatter -> ('', whole text)."""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return "", (text or "").strip()
    return m.group(1), text[m.end() :].strip()


def adapt_agent_skill_frontmatter(fm: dict, *, fallback_name: str = "") -> dict:
    """Adapt open agent-skills frontmatter (name/description only) into a valid
    pure-guidance execution contract.

    The adapted skill CANNOT act: read_only planning_skill, empty allowed_tools,
    max_files_changed 0 — its value is the body markdown (prompt guidance). The
    result goes through validate_execution_skill_frontmatter like any authored
    contract, so this is a format bridge, not a validation bypass.
    """
    name = str(fm.get("name") or fallback_name or "").strip()
    description = str(fm.get("description") or "").strip()
    if not description:
        description = f"Agent-format guidance skill {name} (adapted)."
    return {
        "name": name,
        "kind": KIND,
        "version": 1,
        "enabled": bool(fm.get("enabled", True)),
        "description": description,
        "skill_class": "planning_skill",
        "intents": ["USE_SKILL"],
        "execution_kinds": ["PLAN", "REVIEW"],
        "domains": ["general"],
        "allowed_tools": [],
        "forbidden_tools": [],
        "safety_class": "read_only",
        "requires_confirmation_for": [],
        "max_steps": 20,
        "max_duration_ms": 300_000,
        "max_files_changed": 0,
        "evidence_required": [],
        "completion_criteria": ["guidance_delivered"],
        "failure_classes": ["blocked"],
        "output_schema": {"type": "object"},
    }


def load_execution_skills(
    roots: Optional[List[Path]] = None,
    config: Optional[Dict[str, bool]] = None,
) -> tuple[List[ExecutionSkill], List[SkillLoadDiagnostic]]:
    """Load every valid SKILL.md as an ExecutionSkill, sorted by normalized name.

    Returns (skills, diagnostics). ``skills`` includes DISABLED skills (they load but are
    not ``.selectable``); selection filtering is the caller's job. ``diagnostics`` holds
    one entry per skill that failed YAML parse or schema validation (fail-soft).
    """
    from memory.capability_catalog import capability_id_for_name  # lazy: avoid import cycle

    search = list(roots) if roots is not None else list(BUNDLED_SKILL_DIRS)
    prefs = config if config is not None else load_skill_config()

    by_name: Dict[str, ExecutionSkill] = {}
    diagnostics: List[SkillLoadDiagnostic] = []
    adapted_count = 0

    for root in search:
        try:
            if not Path(root).is_dir():
                continue
            children = sorted(Path(root).iterdir())
        except OSError:
            continue
        for child in children:
            skill_file = child / "SKILL.md"
            if not child.is_dir() or not skill_file.is_file():
                continue
            src = str(skill_file)
            try:
                text = skill_file.read_text(encoding="utf-8")
            except OSError as exc:
                diagnostics.append(SkillLoadDiagnostic(src, child.name, [f"read failed: {exc}"]))
                continue

            yaml_block, body = _split_frontmatter(text)
            try:
                fm = yaml.safe_load(yaml_block) if yaml_block else None
            except yaml.YAMLError as exc:
                diagnostics.append(
                    SkillLoadDiagnostic(src, child.name, [f"malformed YAML frontmatter: {exc}"])
                )
                log.debug("execution skill %s: malformed YAML frontmatter (skipped)", src)
                continue
            if not isinstance(fm, dict):
                diagnostics.append(
                    SkillLoadDiagnostic(src, child.name, ["no execution_skill frontmatter"])
                )
                continue

            adapted = False
            if "kind" not in fm:
                # Open agent-skills format (name/description prompt guidance) —
                # bridge it into a pure-guidance contract, then validate as usual.
                fm = adapt_agent_skill_frontmatter(fm, fallback_name=child.name)
                adapted = True

            errors = validate_execution_skill_frontmatter(fm)
            if errors:
                name = normalize_skill_name(str(fm.get("name") or child.name))
                diagnostics.append(SkillLoadDiagnostic(src, name, errors))
                if adapted:
                    log.debug(
                        "adapted agent skill %s still invalid (skipped): %s", src, "; ".join(errors)
                    )
                else:
                    # An AUTHORED execution contract that fails its own schema is
                    # real breakage — this is the only per-skill warning.
                    log.warning("execution skill %s invalid (skipped): %s", src, "; ".join(errors))
                continue

            if adapted:
                adapted_count += 1
            name = normalize_skill_name(str(fm["name"]))
            skill_id = capability_id_for_name(name, scope="dev")
            enabled = prefs.get(name, bool(fm.get("enabled", True)))
            by_name[name] = ExecutionSkill.from_validated_frontmatter(
                fm,
                skill_id=skill_id,
                body_markdown=body,
                source_path=src,
                enabled=enabled,
            )  # later roots override earlier on name collision

    skills = sorted(by_name.values(), key=lambda s: normalize_skill_name(s.name))
    if adapted_count or diagnostics:
        log.info(
            "execution skills: %d loaded (%d adapted from agent format), %d skipped",
            len(skills),
            adapted_count,
            len(diagnostics),
        )
    return skills, diagnostics


def selectable_execution_skills(
    roots: Optional[List[Path]] = None,
    config: Optional[Dict[str, bool]] = None,
) -> List[ExecutionSkill]:
    """Just the selectable (enabled + valid) skills — disabled ones are filtered out."""
    skills, _ = load_execution_skills(roots=roots, config=config)
    return [s for s in skills if s.selectable]
