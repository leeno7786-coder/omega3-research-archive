"""Phase 6D — migrate the bundled Omega skills to execution-skill frontmatter.

Replaces each skill's legacy frontmatter with the full ExecutionSkill contract (carrying
description/long_description/tags/keywords forward for the legacy QwenSkill loader), and
inserts the bounded-dispatch doctrine into the body. Bodies are otherwise preserved.

Idempotent: re-running re-derives the frontmatter + re-inserts a single contract block.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runtime.qwen_skills import _attr_list, _attr_str, _parse_frontmatter  # noqa: E402

SKILLS_DIR = ROOT / "skills"

KNOWN_TOOLS = {
    "read_files",
    "search_repo",
    "patch_file",
    "write_file",
    "run_command",
    "run_tests",
    "inspect_git_diff",
    "web_fetch",
}

_RO_OUT = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary"],
}
_WR_OUT = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "diff_summary": {"type": "object"},
        "tests_run": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary"],
}
_TEST_OUT = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "passed": {"type": "integer"},
        "failed": {"type": "integer"},
    },
    "required": ["summary"],
}

# Per-skill classification (work-order recommendations).
CONFIG: dict[str, dict] = {
    "omega-planner-executor": dict(
        skill_class="planning_skill",
        execution_kinds=["PLAN", "INSPECT", "REPORT"],
        safety_class="read_only",
        allowed=["read_files", "search_repo", "inspect_git_diff"],
        max_files_changed=0,
        rcf=[],
        evidence=["plan", "summary"],
        completion=["plan_delivered"],
        failure=["blocked", "missing_context"],
        output=_RO_OUT,
        domains=["engineering", "planning"],
    ),
    "omega-refactor-surgeon": dict(
        skill_class="refactor_skill",
        execution_kinds=["REFACTOR", "PATCH", "VERIFY", "REPAIR"],
        safety_class="write_safe",
        allowed=["read_files", "search_repo", "patch_file", "run_tests", "inspect_git_diff"],
        max_files_changed=15,
        rcf=[],
        evidence=["diff_summary", "tests_run"],
        completion=["refactor_applied", "tests_pass"],
        failure=["test_failure", "patch_conflict"],
        output=_WR_OUT,
        domains=["engineering", "refactoring"],
    ),
    "codex-grade-coding": dict(
        skill_class="repair_skill",
        execution_kinds=["INSPECT", "PATCH", "WRITE", "TEST", "VERIFY", "REPAIR"],
        safety_class="write_safe",
        allowed=[
            "read_files",
            "search_repo",
            "patch_file",
            "write_file",
            "run_tests",
            "inspect_git_diff",
        ],
        max_files_changed=25,
        rcf=[],
        evidence=["diff_summary", "tests_run"],
        completion=["tests_pass"],
        failure=["test_failure", "patch_conflict", "build_error"],
        output=_WR_OUT,
        domains=["engineering", "coding"],
    ),
    "testing-strategy": dict(
        skill_class="testing_skill",
        execution_kinds=["TEST", "VERIFY", "DEBUG"],
        safety_class="command_safe",
        allowed=["read_files", "search_repo", "run_tests", "inspect_git_diff"],
        max_files_changed=10,
        rcf=[],
        evidence=["tests_run", "coverage"],
        completion=["tests_executed"],
        failure=["test_failure", "flaky_test"],
        output=_TEST_OUT,
        domains=["testing"],
    ),
    "debugging-methodology": dict(
        skill_class="debugging_skill",
        execution_kinds=["INSPECT", "DEBUG", "REPAIR", "VERIFY"],
        safety_class="write_safe",
        allowed=["read_files", "search_repo", "patch_file", "run_tests", "inspect_git_diff"],
        max_files_changed=15,
        rcf=[],
        evidence=["root_cause", "diff_summary", "tests_run"],
        completion=["bug_resolved", "tests_pass"],
        failure=["unresolved", "test_failure"],
        output=_WR_OUT,
        domains=["engineering", "debugging"],
    ),
    "code-review-excellence": dict(
        skill_class="review_skill",
        execution_kinds=["REVIEW", "INSPECT", "REPORT"],
        safety_class="read_only",
        allowed=["read_files", "search_repo", "inspect_git_diff"],
        max_files_changed=0,
        rcf=[],
        evidence=["findings", "summary"],
        completion=["review_complete"],
        failure=["blocked", "missing_context"],
        output=_RO_OUT,
        domains=["engineering", "review"],
    ),
    "skill-system-maintainer": dict(
        skill_class="maintenance_skill",
        execution_kinds=["MAINTAIN", "PATCH", "VERIFY"],
        safety_class="write_safe",
        allowed=[
            "read_files",
            "search_repo",
            "patch_file",
            "write_file",
            "run_tests",
            "inspect_git_diff",
        ],
        max_files_changed=20,
        rcf=[],
        evidence=["diff_summary", "tests_run"],
        completion=["maintenance_applied", "tests_pass"],
        failure=["test_failure", "validation_error"],
        output=_WR_OUT,
        domains=["engineering", "skills"],
    ),
    "omega-yolo-safe-edits": dict(
        skill_class="repair_skill",
        execution_kinds=["PATCH", "REPAIR", "VERIFY"],
        safety_class="write_safe",
        allowed=[
            "read_files",
            "search_repo",
            "patch_file",
            "write_file",
            "run_tests",
            "inspect_git_diff",
        ],
        max_files_changed=10,
        rcf=["write", "patch"],
        evidence=["diff_summary", "tests_run"],
        completion=["edit_applied", "tests_pass"],
        failure=["test_failure", "patch_conflict"],
        output=_WR_OUT,
        domains=["engineering", "coding"],
    ),
}

CONTRACT = (
    "> **Execution contract (Omega):** This skill executes ONLY inside an Omega Auto "
    "dispatch. It must NOT create new top-level goals, continue work beyond the dispatch, "
    "or use any tool outside its declared `allowed_tools`. It returns structured evidence "
    "matching the declared `output_schema`."
)


def _insert_contract(body: str) -> str:
    # already migrated? collapse the prior contract block first (idempotent)
    body = "\n".join(ln for ln in body.split("\n") if ln.strip() != CONTRACT)
    lines = body.split("\n")
    out: list[str] = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.startswith("# "):
            out.extend(["", CONTRACT])
            inserted = True
    if not inserted:
        return CONTRACT + "\n\n" + body
    return "\n".join(out)


def main() -> None:
    for name, cfg in CONFIG.items():
        path = SKILLS_DIR / name / "SKILL.md"
        if not path.is_file():
            print(f"  SKIP (absent): {name}")
            continue
        old_attrs, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        description = _attr_str(old_attrs, "description") or name
        long_description = (
            _attr_str(old_attrs, "long_description", "longdescription") or description
        )
        allowed = list(cfg["allowed"])
        forbidden = sorted(KNOWN_TOOLS - set(allowed))
        fm = {
            "name": name,
            "kind": "execution_skill",
            "version": 1,
            "enabled": True,
            "description": description,
            "long_description": long_description,
            "author": _attr_str(old_attrs, "author") or "omega",
            "tags": _attr_list(old_attrs, "tags"),
            "keywords": _attr_list(old_attrs, "keywords"),
            "skill_class": cfg["skill_class"],
            "intents": ["USE_SKILL"],
            "execution_kinds": cfg["execution_kinds"],
            "domains": cfg["domains"],
            "allowed_tools": allowed,
            "forbidden_tools": forbidden,
            "safety_class": cfg["safety_class"],
            "requires_confirmation_for": cfg["rcf"],
            "max_steps": cfg.get("max_steps", 30),
            "max_duration_ms": cfg.get("max_duration_ms", 600000),
            "max_files_changed": cfg["max_files_changed"],
            "evidence_required": cfg["evidence"],
            "completion_criteria": cfg["completion"],
            "failure_classes": cfg["failure"],
            "output_schema": cfg["output"],
        }
        new_body = _insert_contract(body)
        text = (
            "---\n"
            + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
            + "---\n\n"
            + new_body.rstrip()
            + "\n"
        )
        path.write_text(text, encoding="utf-8")
        print(f"  migrated: {name}  ({cfg['skill_class']} / {cfg['safety_class']})")


if __name__ == "__main__":
    main()
