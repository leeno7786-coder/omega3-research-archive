"""Normalize SKILL.md files so execution + Qwen loaders register them correctly."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
BUNDLED_SKILLS = ROOT / "skills"
WORKSPACE_SKILLS = ROOT / "skills"

CONTRACT = (
    "> **Execution contract (Omega):** This skill executes ONLY inside an Omega Auto "
    "dispatch. It must NOT create new top-level goals, continue work beyond the dispatch, "
    "or use any tool outside its declared `allowed_tools`. It returns structured evidence "
    "matching the declared `output_schema`."
)

_RO_OUT = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "title": {"type": "string"},
                    "location": {"type": "string"},
                    "recommendation": {"type": "string"},
                },
                "required": ["severity", "title"],
            },
        },
    },
    "required": ["summary"],
}

AUDITING_SECURITY_FM = {
    "name": "auditing-security",
    "kind": "execution_skill",
    "version": 1,
    "enabled": True,
    "description": (
        "Perform a systematic security audit of a codebase, checking for OWASP Top 10 "
        "vulnerabilities, secrets exposure, and insecure patterns."
    ),
    "long_description": (
        "Systematic security audit workflow: secret scanning, auth review, injection checks, "
        "dependency CVEs, CORS/CSP, data exposure, and a severity-rated findings report."
    ),
    "author": "omega",
    "tags": ["security", "audit", "owasp"],
    "keywords": [],
    "skill_class": "inspection_skill",
    "intents": ["USE_SKILL"],
    "execution_kinds": ["INSPECT", "SEARCH", "REPORT"],
    "domains": ["engineering", "security"],
    "allowed_tools": ["read_files", "search_repo", "inspect_git_diff"],
    "forbidden_tools": ["patch_file", "write_file", "run_command", "run_tests", "web_fetch"],
    "safety_class": "read_only",
    "requires_confirmation_for": [],
    "max_steps": 30,
    "max_duration_ms": 600000,
    "max_files_changed": 0,
    "evidence_required": ["findings", "summary"],
    "completion_criteria": ["audit_complete"],
    "failure_classes": ["blocked", "missing_context"],
    "output_schema": _RO_OUT,
}

SUPERPOWERS_DESCRIPTION = (
    "Advanced terminal agent capabilities: multi-step orchestrations, evidence seeking, "
    "tool authority, and custom workflows"
)
BRAINSTORMING_FM = {
    "name": "brainstorming",
    "kind": "execution_skill",
    "version": 1,
    "enabled": True,
    "description": (
        "Transform rough ideas into fully-formed designs through structured questioning "
        "and alternative exploration. Useful early in concept work."
    ),
    "long_description": (
        "Curated brainstorming workflow for concept exploration, design alternatives, "
        "and structured questioning before implementation."
    ),
    "author": "omega",
    "tags": ["brainstorm", "ideation", "design"],
    "keywords": ["brainstorm", "ideation", "concept exploration", "design alternatives"],
    "skill_class": "planning_skill",
    "intents": ["USE_SKILL"],
    "execution_kinds": ["PLAN", "REPORT", "INSPECT"],
    "domains": ["creative-direction", "planning"],
    "allowed_tools": ["read_files", "search_repo", "inspect_git_diff"],
    "forbidden_tools": ["patch_file", "write_file", "run_command", "run_tests", "web_fetch"],
    "safety_class": "read_only",
    "requires_confirmation_for": [],
    "max_steps": 20,
    "max_duration_ms": 300000,
    "max_files_changed": 0,
    "evidence_required": ["plan", "summary"],
    "completion_criteria": ["design_explored"],
    "failure_classes": ["blocked", "missing_context"],
    "output_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "alternatives": {"type": "array", "items": {"type": "string"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary"],
    },
}
SUPERPOWERS_LONG = (
    "Activates advanced terminal agent capabilities: multi-step orchestration with task "
    "tracking, evidence-seeking investigation mode for unknown codebases, tool-chaining "
    "patterns for common workflows, live bridge command discovery via /help and /status, "
    "browser-agent lane switching, and skill stacking for combined expertise."
)


def _dedupe_execution_contract(body: str) -> str:
    lines = body.split("\n")
    out: list[str] = []
    in_contract = False
    seen_contract = False
    for line in lines:
        if line.startswith("## Execution contract"):
            if seen_contract:
                in_contract = True
                continue
            seen_contract = True
            in_contract = False
            out.append(line)
            continue
        if in_contract:
            if line.startswith("## ") or line.startswith("# "):
                in_contract = False
                out.append(line)
            continue
        out.append(line)
    return "\n".join(out)


def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    m = re.match(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n?", text)
    if not m:
        return None, text.strip()
    fm = yaml.safe_load(m.group(1))
    if not isinstance(fm, dict):
        return None, text[m.end() :].strip()
    return fm, text[m.end() :].strip()


def _dump_skill(fm: dict[str, Any], body: str) -> str:
    body = body.rstrip() + "\n"
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body


def _rename_schema_property_descriptions(node: Any) -> None:
    if isinstance(node, dict):
        if "properties" in node and isinstance(node["properties"], dict):
            props = node["properties"]
            if "description" in props and isinstance(props["description"], dict):
                props["issue_detail"] = props.pop("description")
            for prop in props.values():
                if isinstance(prop, dict) and "description" in prop:
                    prop.setdefault("title", prop.pop("description"))
                _rename_schema_property_descriptions(prop)
        if "items" in node:
            _rename_schema_property_descriptions(node["items"])
        if "required" in node and isinstance(node["required"], list):
            node["required"] = [
                "issue_detail" if item == "description" else item for item in node["required"]
            ]
        for value in node.values():
            if isinstance(value, (dict, list)):
                _rename_schema_property_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            _rename_schema_property_descriptions(item)


def _insert_contract(body: str) -> str:
    if "Omega Auto dispatch" in body:
        return body
    lines = body.split("\n")
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.startswith("# "):
            out.extend(["", CONTRACT])
            inserted = True
    if not inserted:
        return CONTRACT + "\n\n" + body
    return "\n".join(out)


def fix_execution_skill_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    if not isinstance(fm, dict) or fm.get("kind") != "execution_skill":
        return False

    changed = False
    if fm.get("name") == "superpowers":
        if fm.get("description") != SUPERPOWERS_DESCRIPTION:
            fm["description"] = SUPERPOWERS_DESCRIPTION
            changed = True
        if fm.get("long_description") != SUPERPOWERS_LONG:
            fm["long_description"] = SUPERPOWERS_LONG
            changed = True

    schema = fm.get("output_schema")
    if isinstance(schema, dict):
        before = yaml.safe_dump(schema, sort_keys=False)
        _rename_schema_property_descriptions(schema)
        after = yaml.safe_dump(schema, sort_keys=False)
        if before != after:
            fm["output_schema"] = schema
            changed = True

    new_body = _dedupe_execution_contract(_insert_contract(body))
    if new_body != body:
        body = new_body
        changed = True

    if changed:
        path.write_text(_dump_skill(fm, body), encoding="utf-8")
    return changed


def create_auditing_security(target_dir: Path, body_source: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    _, body = _split_frontmatter(body_source.read_text(encoding="utf-8"))
    body = body.replace("# Security Audit", "# auditing-security")
    body = _insert_contract(body)
    out = target_dir / "SKILL.md"
    out.write_text(_dump_skill(dict(AUDITING_SECURITY_FM), body), encoding="utf-8")


def migrate_brainstorming(workspace_path: Path, omega_path: Path) -> None:
    _, body = _split_frontmatter(workspace_path.read_text(encoding="utf-8"))
    body = _dedupe_execution_contract(_insert_contract(body))
    text = _dump_skill(dict(BRAINSTORMING_FM), body)
    workspace_path.write_text(text, encoding="utf-8")
    omega_path.parent.mkdir(parents=True, exist_ok=True)
    omega_path.write_text(text, encoding="utf-8")


def sync_workspace_from_omega_cli() -> list[str]:
    synced: list[str] = []
    for src in sorted(BUNDLED_SKILLS.iterdir()):
        if not src.is_dir():
            continue
        skill_file = src / "SKILL.md"
        if not skill_file.is_file():
            continue
        dst_dir = WORKSPACE_SKILLS / src.name
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_file, dst_dir / "SKILL.md")
        synced.append(src.name)
    return synced


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from runtime.execution_skills import load_execution_skills
    from runtime.qwen_skills import BUNDLED_SKILL_DIRS, load_skills

    auditing_src = WORKSPACE_SKILLS / "auditing-security" / "SKILL.md"
    if auditing_src.is_file():
        create_auditing_security(BUNDLED_SKILLS / "auditing-security", auditing_src)
        print("created: skills/auditing-security")

    fixed = 0
    for skill_file in sorted(BUNDLED_SKILLS.glob("*/SKILL.md")):
        if fix_execution_skill_file(skill_file):
            fixed += 1
            print(f"fixed: {skill_file.relative_to(ROOT)}")

    synced = sync_workspace_from_omega_cli()
    print(f"synced {len(synced)} skills/ copies from the local catalog")

    brainstorm = WORKSPACE_SKILLS / "brainstorming" / "SKILL.md"
    if brainstorm.is_file():
        migrate_brainstorming(
            brainstorm,
            BUNDLED_SKILLS / "brainstorming" / "SKILL.md",
        )
        print("migrated: brainstorming")

    exec_skills, diags = load_execution_skills(roots=[BUNDLED_SKILLS], config={})
    ws_exec, ws_diags = load_execution_skills(roots=[WORKSPACE_SKILLS], config={})
    qwen = load_skills(roots=list(BUNDLED_SKILL_DIRS), config={})

    print(f"local skill execution: {len(exec_skills)} loaded, {len(diags)} failed")
    print(f"skills/ execution: {len(ws_exec)} loaded, {len(ws_diags)} failed")
    print(f"bundled qwen: {len(qwen)} loaded")

    suspicious = [
        (n, s.description)
        for n, s in sorted(qwen.items())
        if len(s.description) < 20 or s.description in ("|", n)
    ]
    if suspicious:
        print("suspicious qwen descriptions:")
        for name, desc in suspicious:
            print(f"  {name}: {desc!r}")
        return 1

    if diags or ws_diags:
        for d in diags + ws_diags:
            print(f"FAIL {d.name}: {'; '.join(d.errors[:3])}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
